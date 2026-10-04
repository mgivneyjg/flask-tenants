"""Testing helpers, shipped as a pytest plugin (decision Q14).

Registered via the ``pytest11`` entry point, so these fixtures are available
in any project that installs this package -- no ``conftest.py`` wiring.

Two constraints shape everything here.

**SQLite is out.** It has no schemas, so there is no in-memory shortcut; every
test touching tenancy needs real PostgreSQL. Given that, the pattern is to
provision a small set of fixture tenants once per session and roll each test
back, rather than migrating per test.

**Decision Q16 declined database-level enforcement.** With a single app role
and no RLS, nothing below Python turns a wrong-tenant bug into an error -- it
returns data. That makes :func:`assert_no_leak` load-bearing rather than a
nicety: it is the only thing that demonstrates the mechanism works, and it
belongs in CI on every commit.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from .binding import SchemaBinder, no_tenant_attempt_count
from .context import tenant_context
from .errors import TenantLeakError
from .models import Bases, SimpleTenant, TenantProtocol
from .schema import quote_identifier

#: Environment variable naming the test database.
DATABASE_URL_ENV = "FLASK_TENANTS_TEST_DATABASE_URL"


# -- provisioning helpers -------------------------------------------------


def create_test_schemas(engine: Any, bases: Bases, tenants: Sequence[TenantProtocol]) -> None:
    """Build the shared schema and one schema per tenant, from metadata.

    Note this uses ``create_all`` rather than replaying migrations, which
    production provisioning deliberately does not do (decision Q11). That is
    correct *here*: a test suite wants the schema its models currently
    describe, and has no history to drift from. Use
    :func:`~flask_tenants.provisioning.provision_tenant` to exercise the real
    migration path.
    """
    with engine.begin() as conn:
        bases.shared_metadata.create_all(conn)
        token = bases.tenant_metadata.schema or "tenant"
        for tenant in tenants:
            schema = quote_identifier(tenant.schema_name)
            conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
            scoped = conn.execution_options(schema_translate_map={token: tenant.schema_name})
            bases.tenant_metadata.create_all(scoped)


def drop_test_schemas(
    engine: Any,
    bases: Bases,
    tenants: Sequence[TenantProtocol],
    *,
    drop_shared: bool = True,
) -> None:
    """Tear down what :func:`create_test_schemas` built.

    The only ``DROP SCHEMA`` in this package, confined to the test helpers
    where the schemas were created by the same code moments earlier.

    Tenant schemas go first, because a tenant table may hold a foreign key
    into a shared one and PostgreSQL will refuse to drop the shared table
    while any tenant still references it.

    :param drop_shared: Drop the shared tables too. Pass ``False`` when other
        tenant schemas built by a different fixture are still alive -- their
        foreign keys point at the same shared tables, and dropping those out
        from under them fails.
    """
    with engine.begin() as conn:
        for tenant in tenants:
            conn.execute(
                text(f"DROP SCHEMA IF EXISTS {quote_identifier(tenant.schema_name)} CASCADE")
            )
        if drop_shared:
            bases.shared_metadata.drop_all(conn)


@contextmanager
def rollback_session(engine: Any, session_factory: Any) -> Iterator[Session]:
    """A session whose work is rolled back on exit.

    The standard SQLAlchemy pattern, and it composes neatly with the binding
    design: ``SET LOCAL`` unwinds with the transaction, so tenant state needs
    no separate cleanup.

    ``join_transaction_mode="create_savepoint"`` is what keeps the test's work
    inside the outer transaction instead of committing through it.
    """
    connection = engine.connect()
    transaction = connection.begin()
    session = session_factory(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


# -- the leak check -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LeakProbe:
    """Two tenants seeded with distinguishable sentinel values."""

    tenants: tuple[TenantProtocol, TenantProtocol]
    sentinels: dict[Any, str]
    session_factory: Any = None
    """The **bound** session factory, with the binder installed.

    Use this one. A session factory built separately has no binder attached,
    so its statements are never translated -- the queries fail against the
    symbolic ``tenant`` schema, which is confusing rather than dangerous, but
    confusing enough to be worth handing the right object back.
    """

    def sentinel_for(self, tenant: TenantProtocol) -> str:
        return self.sentinels[tenant.tenant_key]

    def session(self) -> Session:
        """A new session from the bound factory."""
        if self.session_factory is None:  # pragma: no cover
            raise RuntimeError("This probe was built without a session factory")
        return self.session_factory()


def assert_no_leak(
    probe: LeakProbe,
    read: Callable[[TenantProtocol], Sequence[Any]],
) -> None:
    """Assert no tenant can observe another's rows.

    ``read`` is called once per tenant, with that tenant active, and must
    return whatever the code under test sees. Every value is checked against
    every *other* tenant's sentinel.

    .. code-block:: python

        def test_isolation(leak_probe, Session):
            def read(tenant):
                with Session() as s:
                    return s.execute(select(Note.body)).scalars().all()

            assert_no_leak(leak_probe, read)

    :raises TenantLeakError: naming both tenants and the offending value.
    """
    observed: dict[Any, Sequence[Any]] = {}
    for tenant in probe.tenants:
        with tenant_context(tenant):
            observed[tenant.tenant_key] = list(read(tenant))

    for tenant in probe.tenants:
        own = probe.sentinel_for(tenant)
        rows = observed[tenant.tenant_key]
        for other in probe.tenants:
            if other.tenant_key == tenant.tenant_key:
                continue
            foreign = probe.sentinel_for(other)
            for row in rows:
                if foreign in str(row):
                    raise TenantLeakError(
                        f"Tenant {tenant.tenant_key!r} observed {row!r}, which belongs "
                        f"to tenant {other.tenant_key!r}. Expected only {own!r}."
                    )
        if not any(own in str(row) for row in rows) and rows:
            raise TenantLeakError(
                f"Tenant {tenant.tenant_key!r} did not see its own sentinel {own!r}; "
                f"got {rows!r}. The binding may not be applied at all."
            )


# -- fixtures -------------------------------------------------------------


@pytest.fixture(scope="session")
def tenant_database_url() -> str:
    """The test database URL. Override to point somewhere else."""
    return os.environ.get(
        DATABASE_URL_ENV, "postgresql+psycopg://tenants:tenants@127.0.0.1:5432/tenants_test"
    )


@pytest.fixture(scope="session")
def tenant_engine(tenant_database_url: str):
    """A session-scoped engine, skipping the suite if PostgreSQL is absent."""
    from sqlalchemy import create_engine

    engine = create_engine(tenant_database_url, future=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - any connection failure should skip  # pragma: no cover
        # Driver, network and auth failures all surface differently; the useful
        # behaviour is identical for all of them -- skip with the reason shown.
        pytest.skip(f"PostgreSQL not reachable at {tenant_database_url}: {exc}")
    yield engine
    engine.dispose()


@pytest.fixture
def strict_tenancy() -> Iterator[None]:
    """Fail the test if any query ran with no tenant active.

    The runtime already raises (decision Q5), so this should be unreachable.
    That is exactly why it is worth asserting: a rise in the counter means a
    code path that swallowed the error.
    """
    before = no_tenant_attempt_count()
    yield
    after = no_tenant_attempt_count()
    if after > before:
        pytest.fail(
            f"{after - before} tenant-scoped quer{'y' if after - before == 1 else 'ies'} "
            "ran with no tenant active."
        )


def make_leak_probe(
    engine: Any,
    bases: Bases,
    seed: Callable[[TenantProtocol, Session], None],
    *,
    keys: tuple[str, str] = ("probe_a", "probe_b"),
    binder: SchemaBinder | None = None,
    drop_shared: bool = False,
) -> Iterator[LeakProbe]:
    """Build a two-tenant leak probe. Generator, for use inside a fixture.

    ``seed`` is called once per tenant with that tenant active, and should
    write a row containing ``sentinel::<tenant_key>``.
    """
    tenants = tuple(SimpleTenant.for_id(k) for k in keys)
    sentinels = {t.tenant_key: f"sentinel::{t.tenant_key}" for t in tenants}

    own_binder = binder or SchemaBinder()
    Sm = sessionmaker(engine)
    if binder is None:
        own_binder.install(Sm)

    create_test_schemas(engine, bases, tenants)
    try:
        for tenant in tenants:
            with tenant_context(tenant), Sm() as session:
                seed(tenant, session)
                session.commit()
        yield LeakProbe(
            tenants=tenants,  # type: ignore[arg-type]
            sentinels=sentinels,
            session_factory=Sm,
        )
    finally:
        drop_test_schemas(engine, bases, tenants, drop_shared=drop_shared)
        if binder is None:
            own_binder.uninstall(Sm)


__all__ = [
    "DATABASE_URL_ENV",
    "LeakProbe",
    "assert_no_leak",
    "create_test_schemas",
    "drop_test_schemas",
    "make_leak_probe",
    "rollback_session",
]
