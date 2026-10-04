"""Unit tests for the parts that need no database."""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from flask_tenants import (
    HeaderResolver,
    PathPrefixResolver,
    ResolverChain,
    SessionResolver,
    SubdomainResolver,
    TenantState,
    derive_schema_name,
    make_bases,
    quote_identifier,
    validate_schema_name,
)
from flask_tenants.errors import InvalidSchemaNameError, ModelLayoutError
from flask_tenants.models import validate_model_layout
from flask_tenants.observability import TenantLogFilter
from flask_tenants.schema import validate_tenant_schema_name


class FakeRequest:
    def __init__(self, host="", path="/", headers=None):
        self.host = host
        self.path = path
        self.headers = headers or {}


# -- schema naming is a security boundary ---------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "public",  # reserved: would point DDL at shared tables
        "pg_catalog",
        "pg_anything",
        'tenant"; DROP SCHEMA public--',
        "tenant acme",
        "Tenant",  # uppercase would need quoting to round-trip
        "1tenant",
        "",
        "t" * 64,  # exceeds NAMEDATALEN - 1
    ],
)
def test_unsafe_schema_names_are_rejected(bad):
    with pytest.raises(InvalidSchemaNameError):
        validate_tenant_schema_name(bad)


def test_public_is_quotable_but_never_a_tenant():
    """Quoting `public` for a search_path is legitimate; owning it is not."""
    assert quote_identifier("public") == '"public"'
    assert validate_schema_name("public", reserved_ok=True) == "public"
    with pytest.raises(InvalidSchemaNameError):
        validate_tenant_schema_name("public")


def test_schema_names_derive_from_the_surrogate_key():
    assert derive_schema_name(42) == "tenant_42"
    assert derive_schema_name("3f2a-9c11-4b") == "tenant_3f2a_9c11_4b"
    assert derive_schema_name("ACME") == "tenant_acme"
    assert derive_schema_name(7, prefix="t_") == "t_7"


def test_derivation_rejects_an_unusable_key():
    with pytest.raises(InvalidSchemaNameError):
        derive_schema_name("")
    with pytest.raises(InvalidSchemaNameError):
        derive_schema_name("drop schema public")


# -- the model layout rule ------------------------------------------------


def test_a_shared_table_may_not_point_at_a_tenant_table():
    """Which tenant's row would it reference? (decision Q9)"""
    bases = make_bases()

    class Chart(bases.tenant):
        __tablename__ = "layout_charts"
        id: Mapped[int] = mapped_column(primary_key=True)

    class AuditLog(bases.shared):
        __tablename__ = "layout_audit"
        id: Mapped[int] = mapped_column(primary_key=True)
        chart_id: Mapped[int] = mapped_column(ForeignKey(Chart.__table__.c.id))

    with pytest.raises(ModelLayoutError, match="cannot reference a tenant row"):
        validate_model_layout(bases)


def test_a_tenant_table_may_point_at_a_shared_table():
    bases = make_bases()

    class Org(bases.shared):
        __tablename__ = "layout_orgs"
        id: Mapped[int] = mapped_column(primary_key=True)

    class Visit(bases.tenant):
        __tablename__ = "layout_visits"
        id: Mapped[int] = mapped_column(primary_key=True)
        org_id: Mapped[int] = mapped_column(ForeignKey(Org.__table__.c.id))

    validate_model_layout(bases)  # does not raise


def test_bases_share_a_registry_but_not_metadata():
    bases = make_bases()
    assert bases.shared.registry is bases.tenant.registry
    assert bases.shared_metadata is not bases.tenant_metadata
    assert bases.shared_metadata.schema == "public"
    assert bases.tenant_metadata.schema == "tenant"


# -- resolvers ------------------------------------------------------------


def test_subdomain_resolver():
    r = SubdomainResolver("app.example.com")
    assert r.resolve(FakeRequest(host="acme.app.example.com")) == "acme"
    assert r.resolve(FakeRequest(host="acme.app.example.com:8080")) == "acme"
    assert r.resolve(FakeRequest(host="app.example.com")) is None
    assert r.resolve(FakeRequest(host="www.app.example.com")) is None
    assert r.resolve(FakeRequest(host="deep.acme.app.example.com")) is None
    assert r.resolve(FakeRequest(host="acme.other.com")) is None


def test_path_prefix_resolver():
    r = PathPrefixResolver("t")
    assert r.resolve(FakeRequest(path="/t/acme/patients")) == "acme"
    assert r.resolve(FakeRequest(path="/t/acme")) == "acme"
    assert r.resolve(FakeRequest(path="/healthz")) is None
    assert r.resolve(FakeRequest(path="/t")) is None


def test_header_resolver():
    r = HeaderResolver()
    assert r.resolve(FakeRequest(headers={"X-Tenant-ID": " acme "})) == "acme"
    assert r.resolve(FakeRequest(headers={})) is None


def test_session_resolver_reads_the_stored_choice():
    store = {"tenant_id": "acme"}
    r = SessionResolver(session_getter=lambda: store)
    assert r.resolve(FakeRequest()) == "acme"
    store.clear()
    assert r.resolve(FakeRequest()) is None


def test_chain_returns_the_first_opinion():
    chain = ResolverChain(
        [
            SubdomainResolver("app.example.com"),
            SessionResolver(session_getter=lambda: {"tenant_id": "from_session"}),
        ]
    )
    # Subdomain wins when present...
    assert chain.resolve(FakeRequest(host="acme.app.example.com")) == "acme"
    # ...and the session is the fallback for a bare domain.
    assert chain.resolve(FakeRequest(host="app.example.com")) == "from_session"


def test_chain_accepts_a_plain_function():
    chain = ResolverChain([lambda req: "from_callable"])
    assert chain.resolve(FakeRequest()) == "from_callable"


# -- lifecycle & observability -------------------------------------------


def test_only_active_tenants_are_servable():
    assert TenantState.ACTIVE.is_servable
    for state in (TenantState.PENDING, TenantState.FAILED, TenantState.INACTIVE):
        assert not state.is_servable


def test_log_filter_injects_the_tenant(caplog):
    from flask_tenants.context import tenant_context
    from flask_tenants.models import SimpleTenant

    record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", None, None)
    filt = TenantLogFilter()

    filt.filter(record)
    assert record.tenant == "-"

    with tenant_context(SimpleTenant.for_id("acme")):
        filt.filter(record)
        assert record.tenant == "acme"
        assert record.tenant_schema == "tenant_acme"
