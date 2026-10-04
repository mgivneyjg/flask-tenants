# Configuration

## `TenantManager`

Constructor arguments are the real interface, because the core must work with
no Flask app in sight.

| Argument | Default | Purpose |
| --- | --- | --- |
| `engine` | *required* | The SQLAlchemy engine. One shared pool. |
| `session_factory` | *required* | A `sessionmaker` or `scoped_session`. The binder installs here. |
| `bases` | *required* | What `make_bases()` returned. |
| `registry` | *required* | Where tenants are looked up. |
| `resolvers` | `()` | Ordered chain; first non-`None` wins. |
| `shared_schema` | `"public"` | Real schema for shared tables. |
| `schema_prefix` | `"tenant_"` | Prefix for derived schema names. |
| `strict` | `True` | Raise on a tenant-scoped query with no tenant. **Leave on.** |
| `set_search_path` | `True` | Emit the raw-SQL backstop. Leave on unless you have audited every caller. |
| `membership_check` | `None` | Called before activation. **Required with `SessionResolver`.** |
| `validate_layout` | `True` | Assert no shared→tenant foreign keys at startup. |

## Flask config keys

Deployment-level settings only. Things that are *code* — the resolver chain,
the tenant model — stay as constructor arguments, because a list of resolver
instances in a config dict is awkward and untypeable.

| Key | Default | Purpose |
| --- | --- | --- |
| `TENANTS_STRICT_ROUTES` | `False` | Reject every request that resolves to no tenant. Off by default so health checks and signup work; use `@tenant_required` per route instead. |

## Environment variables

| Variable | Used by | Purpose |
| --- | --- | --- |
| `FLASK_TENANTS_MANAGER` | CLI | Import path to your manager, as `module:attribute`. |
| `FLASK_TENANTS_TEST_DATABASE_URL` | pytest plugin | Test database. |

## `make_bases`

| Argument | Default | Purpose |
| --- | --- | --- |
| `registry` | new one | Pass your own if something else owns it. |
| `shared_schema` | `"public"` | Real schema for shared tables. |
| `naming_convention` | sensible default | Applied to both `MetaData` objects. Strongly recommended — autogenerate produces far better migrations with deterministic constraint names. |

## With Flask-SQLAlchemy

The adapter is a shim: build the manager from `db.engine` and `db.session`.

```python
from flask_sqlalchemy import SQLAlchemy
from flask_tenants import FlaskTenants, TenantManager, make_bases

db = SQLAlchemy()
bases = make_bases()      # (1)!


def create_app():
    app = Flask(__name__)
    db.init_app(app)

    with app.app_context():
        manager = TenantManager(
            engine=db.engine,
            session_factory=db.session,
            bases=bases,
            registry=registry,
            resolvers=[...],
        )
        FlaskTenants(manager, app)

    return app
```

1.  Define models against `bases.shared` / `bases.tenant`, not `db.Model`.
    Flask-SQLAlchemy's mechanism for multiple `MetaData` is `__bind_key__`,
    which creates a second *engine and pool* against the same database — the
    wrong shape for two schemas in one database.
