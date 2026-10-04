# Set up a new Flask app

A complete walkthrough, from an empty directory to a running multi-tenant app.
Every file here is real — the finished version lives in
[`examples/minimal/`](https://github.com/mattgivney/flask-tenants/tree/main/examples/minimal).

## 1. Install

```bash
pip install "flask-tenants[flask,cli]" "psycopg[binary]"
```

You need a PostgreSQL database. For local development:

```bash
docker run -d --name app-pg \
  -e POSTGRES_USER=app -e POSTGRES_PASSWORD=app -e POSTGRES_DB=app \
  -p 5432:5432 postgres:16-alpine
```

## 2. Define the bases

Two declarative bases: one for tables that exist **once**, one for tables
cloned into **every tenant's schema**.

```python title="myapp/models.py"
from flask_tenants import make_bases

bases = make_bases()          # (1)!

SharedBase = bases.shared     # tables in `public`
TenantBase = bases.tenant     # tables in each tenant's schema
```

1.  `make_bases()` creates one SQLAlchemy `registry` with two `MetaData`
    objects. Pass your own registry — `make_bases(registry=my_registry)` — if
    something else in your project already owns one.

!!! info "Why two bases and not two `declarative_base()` calls"
    Alembic autogenerates against a `MetaData`, so the shared and tenant
    tables must be in separate ones. But two independent bases also means two
    *registries*, and string-based `relationship()` cannot resolve across
    registries — you would get `InvalidRequestError: expression 'Organization'
    failed to locate a name` the first time a tenant model pointed at a shared
    one. `make_bases()` gives you one registry and two `MetaData`, so both work.

## 3. Define the tenant table

The library does not own your tenant table — it defines a contract and ships a
mixin that satisfies it. Add whatever columns you like.

```python title="myapp/models.py"
from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from flask_tenants import DomainMixin, TenantMixin


class Tenant(SharedBase, TenantMixin):
    __tablename__ = "tenants"
    __tenant_key__ = "slug"          # (1)!

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    billing_plan: Mapped[str] = mapped_column(String(40), default="trial")


class Domain(SharedBase, DomainMixin):
    """One tenant may answer to several hostnames."""

    __tablename__ = "domains"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("public.tenants.id"))
```

1.  The **routing key**: what resolvers return. Subdomain routing yields
    `"acme"`, not `7`, so the registry must look tenants up by slug. Get this
    wrong and the first request fails with
    `operator does not exist: integer = character varying`.

`TenantMixin` contributes `slug`, `schema_name` and `state`. The schema name is
derived from the **primary key**, not the slug — so a customer rebranding
changes a label rather than requiring a schema rename under live connections.
Those are two different keys on purpose; see
[Routing key vs. surrogate key](models.md#routing-key-vs-surrogate-key).

## 4. Define your application models

```python title="myapp/models.py"
class Organization(SharedBase):
    """Reference data, shared by every tenant."""

    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))


class Patient(TenantBase):
    """Lives in each tenant's own schema."""

    __tablename__ = "patients"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(120))

    org_id: Mapped[int | None] = mapped_column(
        ForeignKey(Organization.__table__.c.id), nullable=True   # (1)!
    )
    org: Mapped["Organization | None"] = relationship()          # (2)!
```

1.  **Cross-schema foreign keys must reference the `Column`, not a string
    path.** `ForeignKey("public.organizations.id")` resolves names within a
    single `MetaData` and will not find a table in the other one.
2.  String-based `relationship()` *does* work across the two bases, because
    they share a registry.

!!! warning "A shared table may never point at a tenant table"
    `tenant_x.patients.org_id → public.organizations.id` is a real, enforced
    foreign key and perfectly fine. The reverse is incoherent — which tenant's
    row would a `public` row reference? `TenantManager` asserts this at
    startup, so the mistake surfaces in development rather than while
    provisioning tenant #47.

## 5. Build the manager

```python title="myapp/tenancy.py"
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from flask_tenants import (
    DomainResolver,
    SQLAlchemyRegistry,
    SubdomainResolver,
    TenantManager,
    autoassign_schema_names,
)

from myapp.models import Domain, Tenant, bases

engine = create_engine(
    "postgresql+psycopg://app:app@localhost/app",
    pool_size=5,
    max_overflow=10,
)
Session = sessionmaker(engine)

autoassign_schema_names(Session)                 # (1)!

registry = SQLAlchemyRegistry(Session, Tenant, domain_model=Domain)

manager = TenantManager(
    engine=engine,
    session_factory=Session,
    bases=bases,
    registry=registry,
    resolvers=[                                      # (2)!
        SubdomainResolver("example.com"),
        DomainResolver(registry),
    ],
)
```

1.  Fills in `schema_name` right after the insert that assigns the id — the
    name derives from the primary key, which does not exist before then.
2.  An **ordered chain**: the first resolver returning a key wins. The
    subdomain check is free, so it goes first; the domain-table lookup catches
    vanity domains. See [Resolving the tenant](resolvers.md).

Constructing `TenantManager` installs the schema binder onto your session
factory. Every `Session` it produces is now tenant-aware.

## 6. Wire up Flask

```python title="myapp/app.py"
from flask import Flask, g, jsonify
from sqlalchemy import select

from flask_tenants import FlaskTenants, tenant_required
from myapp.models import Patient
from myapp.tenancy import Session, manager


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "change-me"

    FlaskTenants(manager, app)

    @app.get("/healthz")
    def healthz():
        """No tenant needed — this must work before any tenant exists."""
        return jsonify(ok=True)

    @app.get("/patients")
    @tenant_required                      # (1)!
    def list_patients():
        with Session() as session:
            labels = session.execute(select(Patient.label)).scalars().all()
        return jsonify(tenant=str(g.tenant.tenant_key), patients=labels)

    return app
```

1.  Requests resolving to no tenant run in the shared schema by default, so a
    health check and a signup page work. `@tenant_required` is how a route
    opts into strictness.

## 7. Set up migrations

Two Alembic environments — one per `MetaData`:

```
migrations/
  shared/   env.py  versions/
  tenant/   env.py  versions/
alembic.ini
```

Copy the templates shipped with the package:

```bash
python -c "import flask_tenants, pathlib, shutil; \
  src = pathlib.Path(flask_tenants.__file__).parent / 'alembic_templates'; \
  print(src)"
```

Then point each `env.py` at the right metadata and generate the first
revisions:

```bash
alembic -n shared revision --autogenerate -m "initial shared"
alembic -n tenant revision --autogenerate -m "initial tenant"
```

Attach a runner to the manager so the CLI can find it:

```python title="myapp/tenancy.py"
from alembic.config import Config
from flask_tenants import MigrationRunner

shared_cfg = Config("alembic.ini", ini_section="shared")
tenant_cfg = Config("alembic.ini", ini_section="tenant")

manager.migration_runner = MigrationRunner(
    manager, shared_config=shared_cfg, tenant_config=tenant_cfg
)
```

Apply the shared schema:

```bash
export FLASK_TENANTS_MANAGER=myapp.tenancy:manager
flask-tenants upgrade --shared
```

See [Migrations](migrations.md) for the full story — in particular, **tenant
migrations must not pass a `schema=` argument**.

## 8. Create your first tenant

```python title="scripts/create_tenant.py"
from flask_tenants import TenantState, provision_tenant
from myapp.models import Domain, Tenant
from myapp.tenancy import Session, manager


def create(name: str, slug: str, hostname: str) -> Tenant:
    with Session() as session:
        tenant = Tenant(name=name, slug=slug, state=TenantState.PENDING)
        session.add(tenant)
        session.flush()                       # (1)!
        session.add(Domain(domain=hostname, tenant_id=tenant.id, is_primary=True))
        session.commit()
        session.refresh(tenant)

    provision_tenant(                         # (2)!
        manager,
        tenant,
        runner=manager.migration_runner,
        on_state_change=lambda t, state: _persist_state(t, state),
    )
    return tenant


def _persist_state(tenant, state):
    with Session() as session:
        session.merge(tenant)
        session.commit()


if __name__ == "__main__":
    create("Acme Health", "acme", "acme.example.com")
```

1.  The schema name derives from the primary key, so the row needs an id
    first. `autoassign_schema_names` fills it in on this flush.
2.  Creates the schema and replays every tenant migration. Idempotent — safe to
    re-run after a failure.

!!! tip "Where should this run?"
    `provision_tenant()` is a plain callable with no queue dependency. Run it
    inline while you are small; move it into Celery or RQ when replaying
    migrations starts to outlast a request. Nothing in the library changes —
    the tenant's `state` field is what keeps a half-built schema from serving
    traffic either way.

## 9. Run it

```bash
flask --app myapp.app:create_app run --host 0.0.0.0 --port 5000
curl -H "Host: acme.example.com" localhost:5000/patients
```

## What to read next

- **[Celery and background workers](workers.md)** — the tenant does not cross a
  process boundary on its own.
- **[Testing](testing.md)** — the fixtures that prove isolation holds.
- **[Limits and known gaps](limits.md)** — before production.
