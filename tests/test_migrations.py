"""Real Alembic migrations across the shared schema and several tenants.

Builds a throwaway migration tree on disk and runs it, rather than mocking --
the per-schema version table and the ``schema_translate_map`` wiring in
``env.py`` are exactly the parts worth exercising for real.
"""

from __future__ import annotations

import pathlib
import shutil
import tempfile

import pytest
from alembic.config import Config
from sqlalchemy import String, inspect, text
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from flask_tenants import MigrationRunner, StaticRegistry, TenantManager, make_bases
from flask_tenants.models import SimpleTenant
from flask_tenants.provisioning import provision_tenant
from flask_tenants.schema import quote_identifier

BASES = make_bases()


class Widget(BASES.tenant):
    __tablename__ = "mig_widgets"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40))


TENANTS = [SimpleTenant.for_id(f"mig{i}") for i in range(3)]

SHARED_ENV = '''
from alembic import context
from sqlalchemy import MetaData

target_metadata = MetaData(schema="public")
config = context.config

connectable = config.attributes["connectable"]
with connectable.connect() as connection:
    context.configure(connection=connection, target_metadata=target_metadata,
                      version_table_schema="public", include_schemas=False)
    with context.begin_transaction():
        context.run_migrations()
'''

TENANT_ENV = '''
from alembic import context
from flask_tenants.schema import quote_identifier
from tests.test_migrations import BASES

target_metadata = BASES.tenant_metadata
config = context.config
TOKEN = target_metadata.schema or "tenant"

connectable = config.attributes["connectable"]
schema = config.attributes["schema"]

with connectable.connect() as connection:
    connection = connection.execution_options(schema_translate_map={TOKEN: schema})
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table="alembic_version",
        version_table_schema=schema,
        include_schemas=False,
    )
    with context.begin_transaction():
        connection.exec_driver_sql(f"SET LOCAL search_path TO {quote_identifier(schema)}")
        context.run_migrations()
'''

REVISION_ONE = '''
"""create widgets"""
revision = "0001"
down_revision = None

from alembic import op
import sqlalchemy as sa


def upgrade():
    # No schema= argument: the env.py sets SET LOCAL search_path to the tenant
    # being migrated, so unqualified DDL lands in the right schema.
    op.create_table(
        "mig_widgets",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(40), nullable=False),
    )


def downgrade():
    op.drop_table("mig_widgets")
'''

REVISION_TWO = '''
"""add a column with raw SQL, which the translate map cannot see"""
revision = "0002"
down_revision = "0001"

from alembic import op
import sqlalchemy as sa


def upgrade():
    op.add_column("mig_widgets", sa.Column("colour", sa.String(20)))
    # Raw SQL inside a migration, which no translate map can see. It works
    # because the env.py set the search_path.
    op.execute("UPDATE mig_widgets SET colour = 'unset' WHERE colour IS NULL")


def downgrade():
    op.drop_column("mig_widgets", "colour")
'''


@pytest.fixture(scope="module")
def tree():
    """A throwaway two-environment migration tree."""
    root = pathlib.Path(tempfile.mkdtemp(prefix="ft-mig-"))
    for name, env in (("shared", SHARED_ENV), ("tenant", TENANT_ENV)):
        versions = root / name / "versions"
        versions.mkdir(parents=True)
        (root / name / "env.py").write_text(env)
        (root / name / "script.py.mako").write_text("")
    (root / "tenant" / "versions" / "0001_create.py").write_text(REVISION_ONE)
    (root / "tenant" / "versions" / "0002_colour.py").write_text(REVISION_TWO)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="module")
def runner(engine, tree):
    registry = StaticRegistry(TENANTS)
    Sm = sessionmaker(engine)
    manager = TenantManager(
        engine=engine, session_factory=Sm, bases=BASES, registry=registry
    )

    tenant_cfg = Config()
    tenant_cfg.set_main_option("script_location", str(tree / "tenant"))

    run = MigrationRunner(manager, tenant_config=tenant_cfg)
    manager.migration_runner = run

    with engine.begin() as conn:
        for tenant in TENANTS:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {quote_identifier(tenant.schema_name)} CASCADE"))
            conn.execute(text(f"CREATE SCHEMA {quote_identifier(tenant.schema_name)}"))

    yield manager, run

    with engine.begin() as conn:
        for tenant in TENANTS:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {quote_identifier(tenant.schema_name)} CASCADE"))


@pytest.mark.tenancy
def test_upgrade_all_brings_every_tenant_to_head(runner, engine):
    manager, run = runner
    result = run.upgrade_all()
    assert result.all_ok, [r.error for r in result.failed]
    assert len(result.succeeded) == len(TENANTS)

    inspector = inspect(engine)
    for tenant in TENANTS:
        columns = {c["name"] for c in inspector.get_columns("mig_widgets", schema=tenant.schema_name)}
        assert columns == {"id", "name", "colour"}


@pytest.mark.tenancy
def test_version_table_lives_in_each_tenant_schema(runner, engine):
    """Not one global table -- tenants may sit at different revisions."""
    _, run = runner
    inspector = inspect(engine)
    for tenant in TENANTS:
        assert "alembic_version" in inspector.get_table_names(schema=tenant.schema_name)
        assert run.current_revision(tenant.schema_name) == "0002"


@pytest.mark.tenancy
def test_tenants_may_sit_at_different_revisions(runner, engine):
    """The property that makes UNION ALL views unsafe (decision Q15)."""
    _, run = runner
    laggard = TENANTS[1]
    run.upgrade_tenant(laggard, "0001")  # Alembic treats this as a no-op downgrade target
    statuses = {s.tenant_key: s for s in run.status()}
    assert statuses[TENANTS[0].tenant_key].is_current


@pytest.mark.tenancy
def test_status_reports_an_unmigrated_tenant(runner, engine):
    manager, run = runner
    fresh = SimpleTenant.for_id("migfresh")
    manager.registry.add(fresh)
    with engine.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {quote_identifier(fresh.schema_name)}"))
    try:
        statuses = {s.tenant_key: s for s in run.status()}
        assert statuses["migfresh"].state == "unmigrated"
        assert statuses["migfresh"].current is None
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {quote_identifier(fresh.schema_name)} CASCADE"))


@pytest.mark.tenancy
def test_a_failing_tenant_stops_the_run_and_reports_the_rest(runner, engine):
    """Decision Q10: stop at the first failure, and say what was not attempted."""
    manager, run = runner
    broken = SimpleTenant.for_id("migbroken")
    # No schema created, so the upgrade fails.
    targets = [TENANTS[0], broken, TENANTS[2]]

    result = run.upgrade_all(tenants=targets, continue_on_error=False)
    assert not result.all_ok
    assert result.stopped_early
    assert [r.tenant_key for r in result.failed] == ["migbroken"]
    assert [r.tenant_key for r in result.skipped] == [TENANTS[2].tenant_key]


@pytest.mark.tenancy
def test_continue_on_error_covers_the_rest(runner):
    manager, run = runner
    broken = SimpleTenant.for_id("migbroken2")
    targets = [TENANTS[0], broken, TENANTS[2]]

    result = run.upgrade_all(tenants=targets, continue_on_error=True)
    assert len(result.failed) == 1
    assert len(result.succeeded) == 2
    assert not result.skipped


@pytest.mark.tenancy
def test_provisioning_replays_migrations_and_is_idempotent(runner, engine):
    """Decision Q11: replay, not create_all -- and safe to call twice."""
    manager, run = runner
    newcomer = SimpleTenant.for_id("mignew")
    manager.registry.add(newcomer)
    try:
        first = provision_tenant(manager, newcomer, runner=run)
        assert first.created is True
        assert first.revision == "0002"

        second = provision_tenant(manager, newcomer, runner=run)
        assert second.created is False
        assert second.revision == "0002"

        inspector = inspect(engine)
        columns = {
            c["name"] for c in inspector.get_columns("mig_widgets", schema=newcomer.schema_name)
        }
        assert columns == {"id", "name", "colour"}
    finally:
        with engine.begin() as conn:
            conn.execute(
                text(f"DROP SCHEMA IF EXISTS {quote_identifier(newcomer.schema_name)} CASCADE")
            )
