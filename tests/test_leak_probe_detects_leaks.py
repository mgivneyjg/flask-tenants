"""Does the leak detector actually detect a leak?

The tests in ``test_binding.py`` prove the mechanism works. This proves the
*detector* works, by deliberately breaking isolation and checking it is
caught. A green leak test that cannot fail is worse than no leak test, because
it reads like evidence.
"""

from __future__ import annotations

import pytest
from sqlalchemy import String, select, text
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from flask_tenants.binding import SchemaBinder
from flask_tenants.context import tenant_context
from flask_tenants.errors import TenantLeakError
from flask_tenants.models import SimpleTenant, make_bases
from flask_tenants.testing import (
    LeakProbe,
    assert_no_leak,
    create_test_schemas,
    drop_test_schemas,
)

BASES = make_bases()


class Record(BASES.tenant):
    __tablename__ = "leak_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    body: Mapped[str] = mapped_column(String(80))


A = SimpleTenant.for_id("leak_a")
B = SimpleTenant.for_id("leak_b")


@pytest.fixture
def probe(engine):
    binder = SchemaBinder()
    Sm = sessionmaker(engine)
    binder.install(Sm)
    create_test_schemas(engine, BASES, [A, B])
    for tenant in (A, B):
        with tenant_context(tenant), Sm() as session:
            session.add(Record(body=f"sentinel::{tenant.tenant_key}"))
            session.commit()
    try:
        yield LeakProbe(
            tenants=(A, B),
            sentinels={t.tenant_key: f"sentinel::{t.tenant_key}" for t in (A, B)},
            session_factory=Sm,
        )
    finally:
        drop_test_schemas(engine, BASES, [A, B], drop_shared=True)
        binder.uninstall(Sm)


@pytest.mark.tenancy
def test_probe_passes_when_isolation_holds(probe):
    def read(tenant):
        with probe.session() as session:
            return session.execute(select(Record.body)).scalars().all()

    assert_no_leak(probe, read)


@pytest.mark.tenancy
def test_probe_catches_a_hardcoded_cross_tenant_read(probe, engine):
    """Simulates the bug class the whole design guards against.

    This reads tenant B's schema explicitly, which is what a broken binding
    would do implicitly. The detector must notice.
    """

    def read(tenant):
        with engine.connect() as conn:
            return conn.execute(
                text(f'SELECT body FROM "{B.schema_name}".leak_records')
            ).scalars().all()

    with pytest.raises(TenantLeakError, match="belongs to tenant"):
        assert_no_leak(probe, read)


@pytest.mark.tenancy
def test_probe_catches_a_tenant_seeing_nothing_of_its_own(probe, engine):
    """A binding pointed at the wrong-but-empty schema is still a failure."""

    def read(tenant):
        with engine.connect() as conn:
            conn.execute(text('CREATE SCHEMA IF NOT EXISTS leak_decoy'))
            conn.execute(
                text("CREATE TABLE IF NOT EXISTS leak_decoy.leak_records "
                     "(id serial primary key, body varchar(80))")
            )
            conn.execute(text("INSERT INTO leak_decoy.leak_records (body) VALUES ('unrelated')"))
            rows = conn.execute(text("SELECT body FROM leak_decoy.leak_records")).scalars().all()
            conn.execute(text("DROP SCHEMA leak_decoy CASCADE"))
            conn.commit()
            return rows

    with pytest.raises(TenantLeakError, match="did not see its own sentinel"):
        assert_no_leak(probe, read)
