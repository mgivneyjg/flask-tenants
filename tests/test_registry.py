"""The tenant registry, and the routing-key/surrogate-key distinction.

Regression coverage for a mismatch that is easy to create and produces a
thoroughly unhelpful error: a subdomain resolver yields ``"acme"`` while the
registry looks up by integer primary key, and PostgreSQL answers with
``operator does not exist: integer = character varying``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import ForeignKey, String, select
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from flask_tenants import (
    DomainMixin,
    SQLAlchemyRegistry,
    TenantMixin,
    TenantState,
    autoassign_schema_names,
    make_bases,
)
from flask_tenants.errors import UnknownTenantError

BASES = make_bases()


class SlugTenant(BASES.shared, TenantMixin):
    """Routes on the slug, as a subdomain deployment must."""

    __tablename__ = "reg_slug_tenants"
    __tenant_key__ = "slug"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))


class SlugDomain(BASES.shared, DomainMixin):
    __tablename__ = "reg_slug_domains"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("public.reg_slug_tenants.id"))


@pytest.fixture
def registry(engine):
    Session = sessionmaker(engine)
    autoassign_schema_names(Session)
    BASES.shared_metadata.create_all(engine, tables=[SlugTenant.__table__, SlugDomain.__table__])

    with Session() as session:
        session.execute(SlugDomain.__table__.delete())
        session.execute(SlugTenant.__table__.delete())
        for slug, host in (("acme", "acme.example.test"), ("globex", "globex.example.test")):
            tenant = SlugTenant(name=slug.title(), slug=slug, state=TenantState.ACTIVE)
            session.add(tenant)
            session.flush()
            session.add(SlugDomain(domain=host, tenant_id=tenant.id, is_primary=True))
        session.commit()

    yield SQLAlchemyRegistry(Session, SlugTenant, domain_model=SlugDomain)

    BASES.shared_metadata.drop_all(engine, tables=[SlugDomain.__table__, SlugTenant.__table__])


@pytest.mark.tenancy
def test_lookup_uses_the_declared_routing_key(registry):
    """`__tenant_key__ = "slug"` makes get() take a slug, not an integer."""
    tenant = registry.get("acme")
    assert tenant is not None
    assert tenant.tenant_key == "acme"
    assert tenant.schema_name.startswith("tenant_")


@pytest.mark.tenancy
def test_schema_name_derives_from_the_primary_key_not_the_slug(registry):
    """A rebrand must not imply a schema rename."""
    tenant = registry.get("acme")
    assert tenant.schema_name == f"tenant_{tenant.surrogate_key}"
    assert "acme" not in tenant.schema_name


@pytest.mark.tenancy
def test_domain_lookup_joins_on_the_primary_key(registry):
    """The domain table points at the id, even when routing is by slug."""
    tenant = registry.get_by_domain("globex.example.test")
    assert tenant is not None and tenant.tenant_key == "globex"
    assert registry.get_by_domain("GLOBEX.EXAMPLE.TEST") is not None   # case-insensitive
    assert registry.get_by_domain("nope.example.test") is None


@pytest.mark.tenancy
def test_require_raises_for_an_unknown_key(registry):
    with pytest.raises(UnknownTenantError):
        registry.require("nobody")


@pytest.mark.tenancy
def test_all_is_stably_ordered_and_filterable(registry):
    """Stable ordering is what makes an interrupted migration run resumable."""
    first = [t.tenant_key for t in registry.all()]
    assert first == [t.tenant_key for t in registry.all()]
    assert set(first) == {"acme", "globex"}
    assert len(registry.active()) == 2
    assert registry.all(states=[TenantState.FAILED]) == []


@pytest.mark.tenancy
def test_autoassign_fills_the_schema_name_on_flush(engine):
    """The chicken-and-egg: the name derives from an id that does not exist yet."""
    Session = sessionmaker(engine)
    autoassign_schema_names(Session)
    BASES.shared_metadata.create_all(engine, tables=[SlugTenant.__table__])
    try:
        with Session() as session:
            tenant = SlugTenant(name="Fresh", slug="fresh")
            session.add(tenant)
            assert tenant.schema_name is None
            session.flush()
            assert tenant.schema_name == f"tenant_{tenant.id}"
            session.rollback()
    finally:
        BASES.shared_metadata.drop_all(engine, tables=[SlugTenant.__table__])
