# Limits and known gaps

Everything here is a deliberate trade, written down so the next person knows
it was chosen rather than missed. Read this before going to production.

## 1. Nothing below Python enforces isolation

The design uses a **single application role** — no per-tenant PostgreSQL
roles, no row-level security. The database is a willing participant, not a
guard.

**A bug that activates the wrong tenant returns the wrong data, successfully,
with a 200.** It does not raise.

With per-tenant roles and `SET LOCAL ROLE`, the same bug would produce
`ERROR: permission denied for schema tenant_y` — a stack trace in staging
rather than a finding in an audit. That was declined in favour of a simpler
deployment: no `CREATE ROLE` in provisioning, no grant-per-tenant burden, no
migration-runner carve-out.

**What follows from it:** [the leak test](testing.md#the-leak-check) is the
only thing that demonstrates the mechanism works. Run it in CI on every
commit, and make sure it can actually fail.

If you later want the floor, the chokepoint is one function — `SchemaBinder._apply`
— and adding `SET LOCAL ROLE` there is a small change. Provisioning and
offboarding are where the real work would be.

### Why not row-level security?

RLS is the right tool for the *other* tenancy model: shared tables with a
`tenant_id` column, where a `WHERE` clause is all that separates tenants. Here
there is physical separation, so RLS would add a per-row policy check inside a
schema that by construction holds one tenant's rows. Recurring cost, no new
protection.

## 2. Schemas accumulate forever

Offboarding is a **soft delete**. The library never emits `DROP SCHEMA` —
there is no purge command and no `--force` that would add one.

That is correct for a product with retention obligations, and it has a cost.
PostgreSQL has no hard schema limit, but every tenant's tables sit in
`pg_class` and `pg_attribute`. At a few thousand dead schemas you will notice:

- `pg_dump` duration
- autovacuum scheduling across tens of thousands of tables
- catalog-scanning queries, including some ORM reflection
- `\dt` and similar becoming unusable

**You need an external runbook.** Write it now, while the design is fresh,
not when someone needs it. A workable shape:

1. **Suspend** — `deactivate_tenant()`. Reversible, free, covers most cases.
2. **Archive** — `pg_dump --schema=tenant_42 --schema=public`, verified by a
   test restore, stored per your retention policy. This doubles as the
   customer's data export.
3. **Purge** — after the retention window, a DBA drops the schema by hand,
   having confirmed the archive restores.

Never skip a step. Purge only from an archived state.

## 3. `SET LOCAL` is inert outside a transaction

A raw `engine.connect()` in autocommit receives the `schema_translate_map` and
**no** `search_path`. PostgreSQL warns about the `SET LOCAL` and moves on.

ORM work is unaffected — SQLAlchemy `Session`s are transactional. The gap is
raw Core connections:

```python
# Wrong: raw SQL resolves against the default search_path
with engine.connect() as conn:
    conn.execute(text("SELECT * FROM patients"))

# Right: an explicit transaction, so SET LOCAL applies
with manager.connection(acme) as conn:
    conn.execute(text("SELECT * FROM patients"))
```

Make it audible in development with `warn_if_untracked(engine)`.

## 4. Textual SQL is invisible to the strict check

The "no active tenant" check inspects the statement to decide whether it
touches a tenant table. It can do that for ORM and Core statements, not for
`text("SELECT * FROM patients")`.

Such a statement with no tenant active runs against the default `search_path`
and generally fails — the right outcome, with a worse message. Prefer ORM or
Core constructs where you want the good error.

## 5. One tenant per transaction

Switching tenants inside an open transaction raises:

```
TenantError: Transaction is bound to tenant 'acme' but tenant 'globex' is now
active. A transaction belongs to exactly one tenant -- commit or roll back
before switching.
```

`search_path` is set once per transaction while the translate map is applied
per statement; letting the tenant change would let the two disagree. Commit or
roll back between tenants — which `for_each_tenant` does for you.

## 6. Alembic ignores `schema_translate_map` for `ALTER TABLE`

Tenant migrations must not pass `schema=`. See
[Migrations](migrations.md#the-rule-that-will-bite-you).

## 7. Cross-schema foreign keys need a `Column`, not a string

`ForeignKey("public.organizations.id")` does not resolve from a tenant model;
`ForeignKey(Organization.__table__.c.id)` does. `relationship("Organization")`
by string works fine. See [Defining models](models.md#foreign-keys).

## 8. The session resolver is shared across tabs

A user in two tenants with two tabs open can act on the wrong one. The
stateless resolvers are immune. See
[Resolving the tenant](resolvers.md#the-two-tab-problem).

## 9. Provisioning cost grows with migration count

Replay is linear in history length, so signup gets slower with every revision.
Deliberate — it is the only approach that cannot drift. Move to template
cloning when it hurts; see
[Provisioning](provisioning.md#speeding-it-up-later-template-cloning).

## 10. PostgreSQL only

Not a limitation so much as the premise. Schema-per-tenant is a Postgres
feature; MySQL's "schema" *is* a database and SQLite has none. Supporting
another backend would mean a different isolation model, not a driver swap.
