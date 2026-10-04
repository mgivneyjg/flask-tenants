# Migrations

Two Alembic environments, two independent histories, and `alembic_version`
inside **each tenant's own schema**.

```
migrations/
  shared/
    env.py
    versions/        # tables in `public`
  tenant/
    env.py
    versions/        # tables cloned into every tenant schema
alembic.ini
```

## The rule that will bite you

!!! danger "Tenant migrations must not pass a `schema=` argument"
    Write unqualified DDL and let the `search_path` place it.

    This is not style. `schema_translate_map` is a SQLAlchemy mechanism and
    Alembic does not route every operation through it: `op.add_column`,
    `op.drop_column` and the other `ALTER TABLE` operations format the schema
    name into the statement themselves. A symbolic `tenant` token therefore
    survives into the SQL and PostgreSQL rejects it:

    ```
    psycopg.errors.InvalidSchemaName: schema "tenant" does not exist
    [SQL: ALTER TABLE tenant.widgets ADD COLUMN colour VARCHAR(20)]
    ```

    `op.create_table` happens to work, which only makes the failure arrive
    later and look stranger than it is.

```python title="migrations/tenant/versions/0002_add_colour.py"
def upgrade():
    op.add_column("widgets", sa.Column("colour", sa.String(20)))   # (1)!
    op.execute("UPDATE widgets SET colour = 'unset' WHERE colour IS NULL")  # (2)!
```

1.  No `schema=`. The env.py has set `SET LOCAL search_path` to the tenant
    being migrated.
2.  Raw SQL works identically, for the same reason — which is a real benefit
    of doing it this way rather than a consolation.

`strip_tenant_schema` keeps `--autogenerate` from writing the argument back
in; it is wired into the shipped template.

## Why `alembic_version` is per-schema

Tenants legitimately sit at different revisions. One provisioned this morning
starts at head; one suspended through the last three releases does not; one in
a 400-tenant run that stopped at #212 is mid-upgrade. A single global version
table would assert something false.

Per-schema version tables also make an interrupted run **resumable**: re-run
it and the tenants that already completed are no-ops.

## Generating revisions

```bash
alembic -n shared revision --autogenerate -m "add organizations"
alembic -n tenant revision --autogenerate -m "add patients"
```

Each `env.py` points `target_metadata` at exactly one `MetaData`, so
autogenerate needs no filtering.

!!! tip "Autogenerate against a migrated tenant"
    The tenant environment compares against whichever schema it is pointed at.
    Generate revisions against a tenant that is already at head, or
    autogenerate will try to recreate everything.

## Applying them

```bash
export FLASK_TENANTS_MANAGER=myapp.tenancy:manager

flask-tenants upgrade --shared          # the public schema
flask-tenants upgrade --all             # every tenant
flask-tenants upgrade --tenant acme     # just one
```

Shared first, then tenants: a tenant table may hold a foreign key into a
shared one.

### Failure semantics

**One transaction per tenant, stopping at the first failure.** Tenants 1–211
are committed and done, #212 is rolled back cleanly, and 213–400 are untouched
and reported as skipped.

```
FAILED tenant_212: column "dob" contains null values
187 tenants not attempted after the failure.
211 upgraded, 1 failed, 188 not attempted
```

Stopping early is the right default because #212 failing usually means a
*class* of problem that will also hit #213 — finding out after one failure
beats finding out after 189.

For the 3am case where one tenant has bad data and the rest need to ship
tonight:

```bash
flask-tenants upgrade --all --continue-on-error
```

Nothing is silently truncated either way: tenants not reached are reported as
skipped, because a run that quietly covered half the estate reads exactly like
one that covered all of it.

## Checking for drift

Drift is possible by design, so it has to be observable.

```bash
$ flask-tenants status
head: 4f2c9a1b8e03

  ok  acme      tenant_1    4f2c9a1b8e03
  ok  globex    tenant_2    4f2c9a1b8e03
BEHIND  initech   tenant_3    9c1d4e7a2b55
 NONE  umbrella  tenant_4    -

4 tenants, 2 not at head
```

Exits non-zero when anything is behind, so it works as a deployment gate:

```yaml
- name: Verify every tenant is migrated
  run: flask-tenants status
```

This is the single most-missed piece in homegrown versions of this, and it is
about twenty lines. Without it, "are we migrated?" is a question nobody can
answer.

## Programmatic use

```python
from flask_tenants import MigrationRunner

runner = MigrationRunner(manager, shared_config=shared_cfg, tenant_config=tenant_cfg)

runner.upgrade_shared()
run = runner.upgrade_all(continue_on_error=False)

if not run.all_ok:
    for result in run.failed:
        log.error("tenant %s: %s", result.tenant_key, result.error)
    raise SystemExit(1)

for status in runner.status():
    print(status.tenant_key, status.state, status.current, status.head)
```

## Zero-downtime changes

Per-tenant transactions mean per-tenant locks, and the run is sequential, so a
400-tenant estate takes 400 × the per-tenant time. The usual expand/contract
discipline applies and matters more here:

1. **Expand** — add nullable columns and new tables. Safe to run while serving.
2. **Backfill** — use [`for_each_tenant`](operations.md), not a migration, so
   it is resumable and interruptible.
3. **Contract** — drop the old column in a later release, once no deployed
   code reads it.

Avoid anything taking an `ACCESS EXCLUSIVE` lock on a large table inside a
migration. `CREATE INDEX CONCURRENTLY` cannot run inside a transaction, so it
needs its own non-transactional path — run it through `for_each_tenant` with
an autocommit connection rather than inside a revision.
