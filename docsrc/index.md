# flask-tenants

Schema-per-tenant multi-tenancy for Flask and SQLAlchemy, on PostgreSQL.

A port of the four things [`django-tenants`](https://github.com/django-tenants/django-tenants)
does, rebuilt as a reusable Flask extension with a framework-agnostic core:

| Layer | What it does |
| --- | --- |
| **Schema isolation** | Each tenant's tables live in their own PostgreSQL schema. One connection pool, retargeted per transaction. |
| **Request routing** | An ordered chain of resolvers turns a request into a tenant — by subdomain, domain table, path prefix, header or session. |
| **Shared vs. tenant models** | Two declarative bases on one registry: tables that exist once, and tables cloned into every tenant. |
| **Per-tenant migrations** | Two Alembic environments, with `alembic_version` inside each tenant's own schema so tenants can legitimately differ. |

```python
from flask_tenants import tenant_context

with tenant_context(acme):
    session.add(Patient(label="..."))
    session.commit()
```

## What makes it different

**The core has no Flask dependency.** `TenantManager` works in a Celery worker,
a CLI command, a cron script and a test, with no application context to push.
The Flask layer is a thin adapter over the same primitive.

**Isolation is structural, not procedural.** The tenant is applied to a
transaction through `schema_translate_map` (SQLAlchemy's own mechanism, a
per-execution option with no connection state to reset) with `SET LOCAL
search_path` alongside it as a backstop for raw SQL. `SET LOCAL` is
transaction-scoped, so PostgreSQL reverts it at commit *or* rollback — there is
no cleanup step that can be skipped, and it is correct under PgBouncer
transaction pooling.

**Forgetting the tenant is an error, not wrong data.** A tenant-scoped query
with no tenant active raises. There is no silent fallback to `public`, because
a fallback turns "forgot to set the tenant" into a data-correctness incident
instead of a stack trace. `with public_schema():` is the explicit way to say
you meant the shared schema.

**The leak test ships with the library.** For a tool whose failure mode is
silent data crossover, shipping the proof of absence is close to the point.
See [Testing](testing.md).

## Requirements

- **PostgreSQL 13+.** Non-negotiable: schema-per-tenant is a Postgres feature.
  MySQL has no equivalent (its "schema" *is* a database) and SQLite has none at
  all.
- **SQLAlchemy 2.0+.** The one-registry/two-`MetaData` split and the testing
  fixtures both use 2.0 constructs.
- **Python 3.10+**, **Flask 3.0+** (optional).

## Install

```bash
pip install flask-tenants              # core
pip install "flask-tenants[flask,cli]" # Flask integration and the CLI
```

## Where to go next

- **[Set up a new Flask app](getting-started.md)** — the complete walkthrough.
- **[Celery and background workers](workers.md)** — the part people get wrong.
- **[Why it works this way](design.md)** — the decisions, and what was rejected.
- **[Limits and known gaps](limits.md)** — read this before going to production.
