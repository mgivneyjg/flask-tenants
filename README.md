# flask-tenants

Schema-per-tenant multi-tenancy for Flask and SQLAlchemy, on PostgreSQL.

A port of the four things [`django-tenants`](https://github.com/django-tenants/django-tenants)
does — schema isolation, request routing, the shared/tenant model split, and
per-tenant migrations — rebuilt as a reusable Flask extension with a
framework-agnostic core.

You can [read the documentation here](https://mgivneyjg.github.io/flask-tenants/).


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

## Development

Everything runs through the Makefile, which drives `uv`:

```bash
make dev            # create the venv, install with all dev extras
make db-up          # start the test PostgreSQL (docker or podman)
make test           # run the suite
make docs-serve     # docs with live reload
make help           # every target
```

## Releasing

```bash
make bump V=0.2.0   # one file holds the version
make release        # lint, test, build, validate, smoke-test the wheel
make publish-test   # upload to TestPyPI and verify
make publish        # upload to PyPI (confirms first)
make tag            # tag and push
```

Publishing reads `UV_PUBLISH_TOKEN` from the environment. PyPI and TestPyPI
are separate accounts with separate tokens.

## Documentation

Markdown sources are in [`docsrc/`](docsrc/); `make docs` builds them into
`docs/`, which GitHub Pages serves.

- **[Set up a new Flask app](docsrc/getting-started.md)** — the complete walkthrough
- **[Celery and background workers](docsrc/workers.md)** — the part people get wrong
- **[Testing](docsrc/testing.md)** — the fixtures that prove isolation holds
- **[Limits and known gaps](docsrc/limits.md)** — read before production
- **[Why it works this way](docsrc/design.md)** — the decisions, and what was rejected

A runnable example is in [`examples/minimal/`](examples/minimal/).

## Tests

Tenancy tests need a real PostgreSQL — SQLite has no schemas.

```bash
make db-up test
```

`make test` fails rather than skips when no database is reachable — a run that
skipped every tenancy test looks exactly like one that passed.

## License

MIT
