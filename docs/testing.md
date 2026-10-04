# Testing

The library ships its test fixtures as a pytest plugin, registered via the
`pytest11` entry point. Install the package and they are available — no
`conftest.py` wiring.

```bash
pip install "flask-tenants[test]"
```

## Why this matters more than usual

The design chose **application-level enforcement only**: a single database
role, no per-tenant roles, no row-level security. Nothing below Python turns a
wrong-tenant bug into an error — it returns data, successfully, with a 200.

That makes the leak test load-bearing rather than a nicety. It is the only
thing that demonstrates the mechanism works, and it belongs in CI on **every
commit**.

## SQLite is not an option

SQLite has no schemas, so there is no in-memory shortcut. Every test touching
tenancy needs real PostgreSQL.

```yaml title=".github/workflows/test.yml"
services:
  postgres:
    image: postgres:16
    env:
      POSTGRES_USER: tenants
      POSTGRES_PASSWORD: tenants
      POSTGRES_DB: tenants_test
    ports: ["5432:5432"]
    options: >-
      --health-cmd pg_isready --health-interval 10s --health-timeout 5s --health-retries 5
```

```bash
export FLASK_TENANTS_TEST_DATABASE_URL=postgresql+psycopg://tenants:tenants@localhost/tenants_test
```

## The pattern: provision once, roll back per test

Migrating per test is correct and far too slow. Provision a small set of
fixture tenants once per session and wrap each test in a transaction that
rolls back.

This composes neatly with the binding design: `SET LOCAL` unwinds with the
transaction, so tenant state needs no separate cleanup.

```python title="conftest.py"
import pytest
from sqlalchemy.orm import sessionmaker

from flask_tenants import SimpleTenant, TenantManager, StaticRegistry
from flask_tenants.testing import create_test_schemas, drop_test_schemas, rollback_session

from myapp.models import bases

ACME = SimpleTenant.for_id("acme")
GLOBEX = SimpleTenant.for_id("globex")


@pytest.fixture(scope="session")
def manager(tenant_engine):                       # (1)!
    Session = sessionmaker(tenant_engine)
    registry = StaticRegistry([ACME, GLOBEX])
    mgr = TenantManager(
        engine=tenant_engine, session_factory=Session,
        bases=bases, registry=registry,
    )
    create_test_schemas(tenant_engine, bases, [ACME, GLOBEX])
    yield mgr
    drop_test_schemas(tenant_engine, bases, [ACME, GLOBEX])


@pytest.fixture
def session(tenant_engine, manager):
    """Rolled back after every test."""
    with rollback_session(tenant_engine, manager.session_factory) as s:   # (2)!
        yield s
```

1.  `tenant_engine` comes from the plugin. It skips the suite with a clear
    message if PostgreSQL is unreachable, rather than erroring out of every
    test.
2.  Uses `join_transaction_mode="create_savepoint"` so the test's work stays
    inside the outer transaction instead of committing through it.

Then write ordinary tests:

```python
from flask_tenants import tenant_context


def test_patients_are_scoped(manager, session):
    with manager.tenant_context(ACME):
        session.add(Patient(label="only acme"))
        session.flush()

    with manager.tenant_context(GLOBEX):
        assert session.execute(select(Patient.label)).scalars().all() == []
```

## The leak check

Two tenants, distinguishable sentinel rows, and an assertion that neither can
see the other's.

```python
from flask_tenants.testing import assert_no_leak, make_leak_probe


@pytest.fixture
def leak_probe(tenant_engine):
    def seed(tenant, session):
        session.add(Patient(label=f"sentinel::{tenant.tenant_key}"))

    yield from make_leak_probe(tenant_engine, bases, seed)


def test_no_cross_tenant_leak(leak_probe):
    def read(tenant):
        with leak_probe.session() as session:            # (1)!
            return session.execute(select(Patient.label)).scalars().all()

    assert_no_leak(leak_probe, read)
```

1.  Use the probe's **own** session factory. One you build separately has no
    binder installed, so nothing is translated.

`assert_no_leak` checks two things, and the second is easy to forget:

- No tenant sees another tenant's sentinel — the obvious direction.
- Every tenant **does** see its own — which catches a binding pointed at the
  wrong-but-empty schema, a failure that would otherwise look like a pass.

### Make sure it can fail

A green leak test that cannot fail is worse than no leak test, because it
reads like evidence. Prove the detector works by breaking isolation
deliberately:

```python
def test_the_detector_detects(leak_probe, tenant_engine):
    def read(tenant):
        with tenant_engine.connect() as conn:
            return conn.execute(
                text('SELECT label FROM "tenant_globex".patients')   # wrong on purpose
            ).scalars().all()

    with pytest.raises(TenantLeakError):
        assert_no_leak(leak_probe, read)
```

## Strict mode

The runtime already raises when a tenant-scoped query runs with no tenant
active. The `strict_tenancy` fixture asserts the counter did not move, which
catches a code path that swallowed the error:

```python
def test_nothing_slipped_through(strict_tenancy, manager):
    ...
```

## Testing Flask routes

```python
@pytest.fixture
def client(manager):
    app = Flask(__name__)
    app.testing = True                         # (1)!
    FlaskTenants(manager, app)
    register_routes(app)
    return app.test_client()


def test_patients_endpoint(client):
    response = client.get("/patients", base_url="http://acme.example.test")
    assert response.json == {"patients": []}
```

1.  Without this, Flask converts a view exception into a 500 and
    `pytest.raises` never fires.

## Testing worker code

The tenant context is not an application context, so a worker test needs no
Flask at all:

```python
def test_task_runs_in_the_right_tenant(manager):
    from flask_tenants import capture_headers, restore_into

    with manager.tenant_context(ACME):
        headers = capture_headers()

    with restore_into(manager, headers):
        assert rebuild_index() == "rebuilt acme"
```

## Fixtures reference

| Fixture | Scope | What it gives you |
| --- | --- | --- |
| `tenant_database_url` | session | The test database URL; override to relocate |
| `tenant_engine` | session | An engine, skipping the suite if PostgreSQL is absent |
| `strict_tenancy` | function | Fails the test if any query ran with no tenant |

| Helper | Purpose |
| --- | --- |
| `create_test_schemas` / `drop_test_schemas` | Build and tear down fixture schemas |
| `rollback_session` | A session rolled back on exit |
| `make_leak_probe` | Two seeded tenants plus a bound session factory |
| `assert_no_leak` | The assertion itself |
