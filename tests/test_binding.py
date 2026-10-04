"""The test that proves the isolation mechanism (decisions Q4, Q13, Q14).

Decision Q16 declined database-level enforcement -- a single application role,
no per-tenant roles, no RLS. Nothing below Python converts a wrong-tenant bug
into an error; it would simply return the wrong rows.

That makes this file load-bearing rather than nice-to-have. It is the only
thing that demonstrates the mechanism works, and it belongs in CI on every
commit.
"""

from __future__ import annotations

import pytest
from sqlalchemy import String, select, text
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from flask_tenants.binding import SchemaBinder
from flask_tenants.context import public_schema, tenant_context
from flask_tenants.errors import NoActiveTenantError, TenantError
from flask_tenants.models import SimpleTenant, make_bases
from flask_tenants.schema import quote_identifier

BASES = make_bases()


class Organization(BASES.shared):
    __tablename__ = "ft_test_orgs"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))


class Note(BASES.tenant):
    """A tenant-scoped table. Exists once per tenant schema."""

    __tablename__ = "ft_test_notes"
    id: Mapped[int] = mapped_column(primary_key=True)
    body: Mapped[str] = mapped_column(String(100))


ACME = SimpleTenant.for_id("acme")
GLOBEX = SimpleTenant.for_id("globex")


@pytest.fixture(scope="module")
def bound(engine):
    """Two provisioned tenants, each seeded with a distinguishable sentinel row."""
    binder = SchemaBinder()
    Sm = sessionmaker(engine)
    binder.install(Sm)

    with engine.begin() as conn:
        BASES.shared_metadata.create_all(conn)
        for tenant in (ACME, GLOBEX):
            conn.execute(text(f"DROP SCHEMA IF EXISTS {quote_identifier(tenant.schema_name)} CASCADE"))
            conn.execute(text(f"CREATE SCHEMA {quote_identifier(tenant.schema_name)}"))
            tenant_conn = conn.execution_options(
                schema_translate_map={"tenant": tenant.schema_name}
            )
            BASES.tenant_metadata.create_all(tenant_conn)

    for tenant in (ACME, GLOBEX):
        with tenant_context(tenant), Sm() as session:
            session.add(Note(body=f"sentinel::{tenant.tenant_key}"))
            session.commit()

    yield binder, Sm

    binder.uninstall(Sm)
    with engine.begin() as conn:
        for tenant in (ACME, GLOBEX):
            conn.execute(text(f"DROP SCHEMA IF EXISTS {quote_identifier(tenant.schema_name)} CASCADE"))
        BASES.shared_metadata.drop_all(conn)


# -- the core guarantee ---------------------------------------------------


@pytest.mark.tenancy
def test_orm_query_sees_only_the_active_tenant(bound):
    """The headline guarantee. If this fails, nothing else in the package matters."""
    _, Sm = bound
    for tenant in (ACME, GLOBEX):
        with tenant_context(tenant), Sm() as session:
            bodies = session.execute(select(Note.body)).scalars().all()
            assert bodies == [f"sentinel::{tenant.tenant_key}"]


@pytest.mark.tenancy
def test_raw_sql_sees_only_the_active_tenant(bound):
    """The search_path backstop (Q4).

    The translate map cannot rewrite textual SQL, so without SET LOCAL this
    unqualified query would resolve against the default search_path and read
    from `public` -- silently, and wrongly.
    """
    _, Sm = bound
    for tenant in (ACME, GLOBEX):
        with tenant_context(tenant), Sm() as session:
            bodies = session.execute(text("SELECT body FROM ft_test_notes")).scalars().all()
            assert bodies == [f"sentinel::{tenant.tenant_key}"]


@pytest.mark.tenancy
def test_no_crossover_when_switching_tenants_repeatedly(bound):
    """Alternating tenants on a pooled connection is where leaks surface."""
    _, Sm = bound
    for tenant in (ACME, GLOBEX, ACME, GLOBEX, ACME):
        with tenant_context(tenant), Sm() as session:
            bodies = session.execute(select(Note.body)).scalars().all()
            assert bodies == [f"sentinel::{tenant.tenant_key}"], (
                f"Tenant {tenant.tenant_key} observed {bodies} -- cross-tenant leak"
            )


@pytest.mark.tenancy
def test_search_path_reverts_after_an_exception(bound, engine):
    """SET LOCAL reverts on rollback, not just commit (Q13).

    An exception mid-transaction is the path a manual reset is most likely to
    miss, which is precisely why the reset is Postgres's job here.
    """
    _, Sm = bound
    with pytest.raises(RuntimeError):
        with tenant_context(ACME), Sm() as session:
            session.execute(select(Note.body)).scalars().all()
            raise RuntimeError("boom")

    with engine.connect() as conn:
        path = conn.execute(text("SHOW search_path")).scalar()
    assert ACME.schema_name not in path


# -- failing loudly rather than quietly -----------------------------------


@pytest.mark.tenancy
def test_tenant_query_with_no_tenant_raises(bound):
    """Q5: no silent fallback to public."""
    _, Sm = bound
    with Sm() as session:
        with pytest.raises(NoActiveTenantError):
            session.execute(select(Note.body)).scalars().all()


@pytest.mark.tenancy
def test_shared_query_with_no_tenant_is_allowed(bound):
    """Shared tables need no tenant -- health checks and signup must work."""
    _, Sm = bound
    with Sm() as session:
        assert session.execute(select(Organization.name)).scalars().all() == []


@pytest.mark.tenancy
def test_public_schema_is_an_explicit_escape_hatch(bound):
    _, Sm = bound
    with public_schema(), Sm() as session:
        assert session.execute(select(Organization.name)).scalars().all() == []


@pytest.mark.tenancy
def test_switching_tenants_inside_a_transaction_raises(bound):
    """One tenant per transaction.

    search_path is set once per transaction while the translate map is applied
    per statement. Letting the tenant change would let the two disagree.
    """
    _, Sm = bound
    with Sm() as session:
        with tenant_context(ACME):
            session.execute(select(Note.body)).scalars().all()
        with tenant_context(GLOBEX):
            with pytest.raises(TenantError, match="belongs to exactly one tenant"):
                session.execute(select(Note.body)).scalars().all()


@pytest.mark.tenancy
def test_nested_public_lookup_restores_the_outer_tenant(bound):
    """Restoring via the contextvar token is what makes nesting correct (Q5)."""
    from flask_tenants.context import current_tenant_key

    with tenant_context(ACME):
        assert current_tenant_key() == "acme"
        with public_schema():
            assert current_tenant_key() is None
        assert current_tenant_key() == "acme"


@pytest.mark.tenancy
def test_no_crossover_on_a_single_connection_pool(engine):
    """Regression: a pooled connection handed back out must be rebound.

    With ``pool_size=1`` every transaction reuses the same DBAPI connection, so
    anything cached against the connection rather than the transaction leaks.
    An earlier draft keyed its idempotence check on ``Connection.info``, which
    is proxied to the pooled connection and survives checkin -- so the second
    transaction for a given tenant was skipped entirely, leaving no translate
    map and no ``search_path``.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.pool import QueuePool

    single = create_engine(engine.url, poolclass=QueuePool, pool_size=1, max_overflow=0)
    binder = SchemaBinder()
    Sm = sessionmaker(single)
    binder.install(Sm)
    try:
        for _ in range(3):
            for tenant in (ACME, GLOBEX):
                with tenant_context(tenant), Sm() as session:
                    bodies = session.execute(select(Note.body)).scalars().all()
                    assert bodies == [f"sentinel::{tenant.tenant_key}"]
    finally:
        binder.uninstall(Sm)
        single.dispose()


@pytest.mark.tenancy
def test_writes_are_routed_to_the_active_tenant(bound):
    """Flushes bypass do_orm_execute, so this covers the INSERT path separately."""
    _, Sm = bound
    with tenant_context(ACME), Sm() as session:
        session.add(Note(body="written under acme"))
        session.commit()

    with tenant_context(GLOBEX), Sm() as session:
        bodies = session.execute(select(Note.body)).scalars().all()
        assert "written under acme" not in bodies

    with tenant_context(ACME), Sm() as session:
        bodies = session.execute(select(Note.body)).scalars().all()
        assert "written under acme" in bodies
        session.execute(text("DELETE FROM ft_test_notes WHERE body = 'written under acme'"))
        session.commit()
