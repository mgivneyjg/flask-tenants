# flask-tenants

Schema-per-tenant multi-tenancy for Flask and SQLAlchemy, on PostgreSQL.

A port of the four things [`django-tenants`](https://github.com/django-tenants/django-tenants)
does — schema isolation, request routing, the shared/tenant model split, and
per-tenant migrations — rebuilt as a reusable Flask extension with a
framework-agnostic core.

```python
from flask_tenants import TenantManager, tenant_context

with tenant_context(acme):
    session.add(Patient(name="..."))
    session.commit()
```

- **PostgreSQL only.** Schema-per-tenant is a Postgres feature; MySQL has no
  equivalent and SQLite has none at all.
- **SQLAlchemy 2.0+, Flask 3.0+, Python 3.10+.** The Flask layer is optional —
  the core works in a Celery worker, a CLI command or a bare script.
- **Leak detection ships with the library.** The mechanism is only as good as
  the proof it works.

## Documentation

The full documentation site lives in [`docs/`](docs/). Build it locally:

```bash
pip install "flask-tenants[docs]"
mkdocs serve
```

- **[Set up a new Flask app](docs/getting-started.md)** — the complete walkthrough
- **[Celery and background workers](docs/workers.md)** — the part people get wrong
- **[Testing](docs/testing.md)** — the fixtures that prove isolation holds
- **[Limits and known gaps](docs/limits.md)** — read before production
- **[Why it works this way](docs/design.md)** — the decisions, and what was rejected

A runnable example is in [`examples/minimal/`](examples/minimal/).

## Tests

Tenancy tests need a real PostgreSQL — SQLite has no schemas.

```bash
docker run -d --name ft-pg -e POSTGRES_USER=tenants -e POSTGRES_PASSWORD=tenants \
  -e POSTGRES_DB=tenants -p 5432:5432 postgres:16-alpine

export FLASK_TENANTS_TEST_DATABASE_URL=postgresql+psycopg://tenants:tenants@localhost/tenants
pytest
```

## License

MIT
