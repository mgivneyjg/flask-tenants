# Defining models

## The two bases

```python
from flask_tenants import make_bases

bases = make_bases()
SharedBase = bases.shared
TenantBase = bases.tenant
```

| Base | Lives in | Exists | Use for |
| --- | --- | --- | --- |
| `SharedBase` | `public` | Once | The tenant registry, reference data, cross-tenant lookup tables |
| `TenantBase` | `tenant_<id>` | Once per tenant | Everything belonging to a single customer |

Under the hood that is **one `registry` with two `MetaData`**. Both halves
matter: separate `MetaData` is what lets Alembic autogenerate two independent
histories, and a shared `registry` is what lets `relationship("Organization")`
resolve by string from a tenant model to a shared one.

Pass your own registry when something else already owns one:

```python
from sqlalchemy.orm import registry

my_registry = registry()
bases = make_bases(registry=my_registry)
```

## Foreign keys

**Tenant → shared is allowed and enforced.** PostgreSQL supports cross-schema
foreign keys, so this is a real constraint:

```python
class Patient(TenantBase):
    __tablename__ = "patients"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey(Organization.__table__.c.id))
```

!!! danger "Reference the Column, not a string path"
    `ForeignKey("public.organizations.id")` will **not** resolve. `ForeignKey`
    string targets are looked up within a single `MetaData`, and the shared
    tables are in the other one. Use `ForeignKey(Organization.__table__.c.id)`.

    `relationship("Organization")` by string *does* work — relationships
    resolve through the registry, which is shared.

**Shared → tenant is rejected.** There is no way for a row in `public` to say
*which* tenant's row it references. `TenantManager` calls
`validate_model_layout()` at construction and raises `ModelLayoutError` rather
than letting you find out during provisioning:

```
ModelLayoutError: Shared table 'audit_log' has a foreign key 'chart_id' ->
charts.id, which points into a tenant-scoped table. A shared row cannot
reference a tenant row -- there is no way to say which tenant.
```

If you need the link, either move the shared table onto `TenantBase`, or drop
the constraint and carry the tenant key alongside the id.

## The tenant model

The library defines a `Protocol` and ships a mixin; you own the table.

```python
from flask_tenants import TenantMixin


class Tenant(SharedBase, TenantMixin):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    billing_plan: Mapped[str] = mapped_column(String(40), default="trial")
    region: Mapped[str] = mapped_column(String(20), default="eu-west-1")
```

### Routing key vs. surrogate key

Two different keys, and conflating them is the most common setup mistake.

```python
class Tenant(SharedBase, TenantMixin):
    __tablename__ = "tenants"
    __tenant_key__ = "slug"          # (1)!

    id: Mapped[int] = mapped_column(primary_key=True)
```

1.  Required if you route on subdomains, because `SubdomainResolver` yields
    `"acme"`, not `7`.

| | Which attribute | Changes? | Used for |
| --- | --- | --- | --- |
| **Routing key** (`tenant_key`) | Named by `__tenant_key__`, default `id` | Possibly | Resolvers, worker messages, CLI arguments |
| **Surrogate key** (`surrogate_key`) | The primary key | Never | Deriving the schema name |

Declaring `__tenant_key__` once means the registry and `tenant_key` cannot
disagree. If they do — a slug resolver against a primary-key lookup — the
symptom is not a configuration error but:

```
psycopg.errors.UndefinedFunction: operator does not exist: integer = character varying
```

The schema name always derives from the **surrogate** key, so changing a slug
is a label change and never a schema rename.

### Columns

`TenantMixin` contributes three columns:

| Column | Purpose |
| --- | --- |
| `slug` | Human-facing label. **Safe to change.** |
| `schema_name` | The real schema. Derived from the primary key; treat as immutable. **Nullable** — see below. |
| `state` | Lifecycle state; only `active` is served. |

Anything satisfying `TenantProtocol` works, so a registry backed by config or
a remote service is equally valid — see `SimpleTenant` and `StaticRegistry`.

### Why `schema_name` is nullable

The name derives from the primary key, and the primary key does not exist
until the row is inserted — so a `NOT NULL` column could never be satisfied on
the first flush. It is populated immediately afterwards:

```python
from flask_tenants import autoassign_schema_names

autoassign_schema_names(Session)      # once, at startup
```

That hooks `after_flush_postexec` — the first point at which the row has its
id — and writes in the same transaction as the insert, so a tenant row never
reaches a committed state without a schema name. Call
`tenant.assign_schema_name()` yourself if you prefer it explicit.

Provisioning refuses a tenant with no schema name, and the resolver never
serves an unprovisioned tenant, so the window where this is `None` is a single
flush.

### Why the schema name is not the slug

Slugs change. If `schema_name == slug`, a rebrand means renaming a schema in
production while connections still hold the old name in their `search_path`.
Deriving from the immutable surrogate key costs nothing and removes that
migration entirely. You lose a little readability in `psql`, which a view over
the tenant table solves.

### Schema names are a security boundary

Schema names are SQL *identifiers*, so they cannot be bind parameters — every
name is interpolated into DDL as text. Accordingly:

- `derive_schema_name()` produces `tenant_<key>`, lowercased, hyphens to
  underscores.
- `validate_tenant_schema_name()` enforces `^[a-z][a-z0-9_]*$`, rejects the
  `pg_` prefix, rejects `public` and friends, and caps length at 63.
- `quote_identifier()` validates then quotes, and is used on **every**
  interpolation.

Derivation already makes the name safe. The validator is what keeps it safe
when someone later adds a path that sets `schema_name` directly.

## Lifecycle states

```python
from flask_tenants import TenantState
```

| State | Servable | Meaning |
| --- | --- | --- |
| `PENDING` | No | Registered, schema not built yet |
| `ACTIVE` | **Yes** | Provisioned and serving |
| `FAILED` | No | Provisioning failed; terminal until a human intervenes |
| `INACTIVE` | No | Suspended or soft-deleted; data intact |

There is no `PURGED`. The library never drops a schema — see
[Limits](limits.md).

Resolvers refuse anything that is not `ACTIVE`, which is what keeps a
half-provisioned schema from ever answering a request.
