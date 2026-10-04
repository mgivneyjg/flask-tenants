"""Full-stack test: a real Flask app, a real tenant table, real Alembic migrations.

Exercises the path an application actually takes, rather than the pieces in
isolation -- including the worker handoff, which is where decision Q17 says the
sharpest edge lives.
"""

from __future__ import annotations

import pathlib
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from flask import Flask, jsonify
from sqlalchemy import ForeignKey, String, func, select, text
from sqlalchemy.orm import Mapped, mapped_column, relationship, sessionmaker

from flask_tenants import (
    DomainResolver,
    FlaskTenants,
    HeaderResolver,
    SQLAlchemyRegistry,
    StaticRegistry,
    SubdomainResolver,
    TenantManager,
    TenantState,
    capture_headers,
    current_tenant,
    for_each_tenant,
    make_bases,
    provision_tenant,
    restore_into,
    tenant_required,
)
from flask_tenants.errors import TenantNotReadyError, UnknownTenantError
from flask_tenants.models import SimpleTenant
from flask_tenants.testing import assert_no_leak, create_test_schemas, drop_test_schemas, make_leak_probe

BASES = make_bases()


class Organization(BASES.shared):
    __tablename__ = "e2e_orgs"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60))


class Patient(BASES.tenant):
    __tablename__ = "e2e_patients"
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(80))
    # Cross-MetaData foreign keys must reference the Column, not a string path:
    # ForeignKey string targets are resolved within a single MetaData.
    org_id: Mapped[int | None] = mapped_column(
        ForeignKey(Organization.__table__.c.id), nullable=True
    )
    org: Mapped["Organization | None"] = relationship()


ACME = SimpleTenant.for_id("e2eacme")
GLOBEX = SimpleTenant.for_id("e2eglobex")
PENDING = SimpleTenant("e2epending", "tenant_e2epending", TenantState.PENDING)


@pytest.fixture(scope="module")
def stack(engine):
    registry = StaticRegistry()
    registry.add(ACME, "acme.example.test")
    registry.add(GLOBEX, "globex.example.test")
    registry.add(PENDING, "pending.example.test")

    Sm = sessionmaker(engine)
    manager = TenantManager(
        engine=engine,
        session_factory=Sm,
        bases=BASES,
        registry=registry,
        resolvers=[
            SubdomainResolver("example.test", ignore=("www",)),
            DomainResolver(registry),
            HeaderResolver(),
        ],
    )
    create_test_schemas(engine, BASES, [ACME, GLOBEX])
    try:
        yield manager, Sm
    finally:
        drop_test_schemas(engine, BASES, [ACME, GLOBEX])
        manager.binder.uninstall(Sm)


@pytest.fixture
def seeded(stack):
    manager, Sm = stack
    for tenant in (ACME, GLOBEX):
        with manager.tenant_context(tenant), Sm() as session:
            session.execute(text("DELETE FROM e2e_patients"))
            session.add(Patient(label=f"sentinel::{tenant.tenant_key}"))
            session.commit()
    return stack


# -- resolution -----------------------------------------------------------


def test_subdomain_resolution(seeded):
    manager, _ = seeded
    app = Flask(__name__)
    app.testing = True
    FlaskTenants(manager, app)

    @app.get("/who")
    @tenant_required
    def who():
        return jsonify(tenant=str(current_tenant().tenant_key))

    client = app.test_client()
    assert client.get("/who", base_url="http://e2eacme.example.test").json == {"tenant": "e2eacme"}
    assert client.get("/who", base_url="http://e2eglobex.example.test").json == {"tenant": "e2eglobex"}


def test_request_scoped_queries_are_isolated(seeded):
    manager, Sm = seeded
    app = Flask(__name__)
    app.testing = True
    FlaskTenants(manager, app)

    @app.get("/patients")
    @tenant_required
    def patients():
        with Sm() as session:
            return jsonify(session.execute(select(Patient.label)).scalars().all())

    client = app.test_client()
    assert client.get("/patients", base_url="http://e2eacme.example.test").json == [
        "sentinel::e2eacme"
    ]
    assert client.get("/patients", base_url="http://e2eglobex.example.test").json == [
        "sentinel::e2eglobex"
    ]


def test_requests_with_no_tenant_run_in_public(seeded):
    """A health check must work before any tenant exists."""
    manager, _ = seeded
    app = Flask(__name__)
    app.testing = True
    FlaskTenants(manager, app)

    @app.get("/healthz")
    def healthz():
        return jsonify(ok=True, tenant=current_tenant() is not None)

    assert app.test_client().get("/healthz", base_url="http://www.example.test").json == {
        "ok": True,
        "tenant": False,
    }


def test_tenant_required_rejects_an_unresolved_request(seeded):
    manager, _ = seeded
    app = Flask(__name__)
    app.testing = True
    FlaskTenants(manager, app)

    @app.get("/secret")
    @tenant_required
    def secret():  # pragma: no cover
        return "never"

    with pytest.raises(Exception):
        app.test_client().get("/secret", base_url="http://www.example.test")


def test_unknown_and_unready_tenants_are_refused(seeded):
    manager, _ = seeded
    with pytest.raises(UnknownTenantError):
        manager.registry.require("nope")
    with pytest.raises(TenantNotReadyError):
        with manager.tenant_context(PENDING):  # pragma: no cover
            pass


def test_context_unwinds_even_when_the_view_raises(seeded):
    """teardown_request must reset the contextvar on the error path too."""
    from flask_tenants import current_tenant_key

    manager, _ = seeded
    app = Flask(__name__)
    app.testing = True
    FlaskTenants(manager, app)

    @app.get("/boom")
    def boom():
        raise RuntimeError("kaboom")

    client = app.test_client()
    with pytest.raises(RuntimeError):
        client.get("/boom", base_url="http://e2eacme.example.test")
    assert current_tenant_key() is None


# -- cross-tenant operations ---------------------------------------------


def test_for_each_tenant_runs_once_per_tenant(seeded):
    manager, Sm = seeded

    def count(tenant):
        with Sm() as session:
            return session.execute(select(func.count()).select_from(Patient)).scalar_one()

    result = for_each_tenant(manager, count, tenants=[ACME, GLOBEX])
    assert result.all_ok
    assert result.values() == [1, 1]


def test_for_each_tenant_collects_errors_without_stopping(seeded):
    manager, _ = seeded

    def explode(tenant):
        if tenant.tenant_key == "e2eacme":
            raise ValueError("bad tenant")
        return "fine"

    result = for_each_tenant(manager, explode, tenants=[ACME, GLOBEX])
    assert len(result.failed) == 1
    assert result.values() == ["fine"]


def test_for_each_tenant_reports_what_it_skipped(seeded):
    """No silent truncation -- a partial run must not read like a full one."""
    manager, _ = seeded

    def explode(tenant):
        raise ValueError("nope")

    result = for_each_tenant(
        manager, explode, tenants=[ACME, GLOBEX], continue_on_error=False
    )
    assert result.stopped_early
    assert len(result.failed) == 1
    assert len(result.skipped) == 1


def test_parallel_workers_bind_their_own_context(seeded):
    """Threads do not inherit contextvars -- each worker must bind itself."""
    manager, Sm = seeded

    def read_label(tenant):
        with Sm() as session:
            return session.execute(select(Patient.label)).scalars().one()

    result = for_each_tenant(manager, read_label, tenants=[ACME, GLOBEX], max_workers=2)
    assert result.all_ok
    assert sorted(result.values()) == ["sentinel::e2eacme", "sentinel::e2eglobex"]


def test_threads_really_do_not_inherit_the_context(seeded):
    """Documents the behaviour the parallel path exists to work around."""
    from flask_tenants import current_tenant_key

    manager, _ = seeded
    with manager.tenant_context(ACME):
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(current_tenant_key).result() is None
        assert current_tenant_key() == "e2eacme"


# -- worker propagation ---------------------------------------------------


def test_tenant_crosses_a_simulated_process_boundary(seeded):
    """The Celery/RQ handoff: capture on publish, restore on consume."""
    manager, Sm = seeded

    with manager.tenant_context(ACME):
        headers = capture_headers()

    assert headers == {"flask_tenants_tenant": "e2eacme"}

    # ... message travels to a worker with no context of its own ...
    with restore_into(manager, headers):
        with Sm() as session:
            assert session.execute(select(Patient.label)).scalars().one() == "sentinel::e2eacme"


def test_a_message_with_no_tenant_restores_the_public_context(seeded):
    manager, Sm = seeded
    with restore_into(manager, {}):
        with Sm() as session:
            assert session.execute(select(Organization.name)).scalars().all() == []


# -- the leak probe shipped to users --------------------------------------


def test_shipped_leak_probe_detects_nothing_wrong(engine):
    """The fixture users inherit, run against the real binder."""

    def seed(tenant, session):
        session.add(Patient(label=f"sentinel::{tenant.tenant_key}"))

    probe_gen = make_leak_probe(engine, BASES, seed)
    probe = next(probe_gen)
    try:

        def read(tenant):
            with probe.session() as session:
                return session.execute(select(Patient.label)).scalars().all()

        assert_no_leak(probe, read)
    finally:
        probe_gen.close()
