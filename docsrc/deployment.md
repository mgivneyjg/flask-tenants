# Deployment

## Connection budget

**One shared pool for every tenant.** Per-tenant pools do not scale: 400
tenants at even two connections each is 800 against a `max_connections` that
is realistically 100–500, and they sit idle for every tenant not currently
being served. Schema-per-tenant's whole economic argument is that tenants
share infrastructure.

What needs sizing is that single pool, against every process that opens one:

```
nodes × (pool_size + max_overflow)
  + workers × (pool_size + max_overflow)
  + migration runs
  + admin and psql sessions
  + headroom
  ≤ max_connections
```

Six web nodes each quietly defaulting to SQLAlchemy's `pool_size=5,
max_overflow=10` is **90 connections** before a single Celery worker exists.

```python
engine = create_engine(
    DATABASE_URL,
    pool_size=5,
    max_overflow=5,
    pool_pre_ping=True,     # (1)!
    pool_recycle=1800,      # (2)!
)
```

1.  Cheap round trip that avoids handing out a connection a proxy or restart
    has already killed.
2.  Recycle below any idle timeout in PgBouncer or your cloud provider.

Put PgBouncer in front once the arithmetic stops working. **Transaction
pooling mode is safe here** — `SET LOCAL` and the per-transaction translate
map both live and die with the transaction, which is exactly the pooled unit.
That is not true of a `search_path`-only design.

## Sessions and horizontal scaling

Flask's default session is a signed cookie, so nodes need no affinity and no
shared store. Share `SECRET_KEY` across every node and you can scale out
freely. See [Resolving the tenant](resolvers.md#sessions-and-horizontal-scaling).

## Shared-schema permissions

Free, and worth doing regardless. Default PostgreSQL permissions on `public`
are looser than most people expect:

```sql
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app;
```

The application role needs `CREATE` on the *database* to create tenant
schemas. If you would rather it did not, run provisioning and migrations under
a separate, more privileged role and leave the request-serving role without
it — a reasonable split, and the one place this design gets a database-level
boundary for free.

## Deployment order

1. `flask-tenants upgrade --shared`
2. `flask-tenants upgrade --all`
3. Deploy application code
4. `flask-tenants status` as a gate — it exits non-zero if any tenant is
   behind

Shared first: tenant tables may hold foreign keys into shared ones.

Expand/contract applies, and matters more with many tenants, because the
tenant run is sequential. Ship the additive migration and the code that
tolerates both shapes; drop the old shape a release later.

## Migration windows

A 400-tenant run takes 400 × the per-tenant time, each in its own transaction
and therefore its own lock window. Budget accordingly, and prefer
`for_each_tenant` over a migration for anything that touches *data* at
volume — it is resumable and interruptible, and a migration is neither.

Re-running an interrupted run is safe: per-schema version tables make
completed tenants no-ops.

## Backups

`pg_dump` dumps every schema by default, which is what you want for a full
backup but slow once there are thousands.

Per-tenant export, for a customer data request:

```bash
pg_dump --schema=tenant_42 --schema=public -Fc -f acme.dump "$DATABASE_URL"
```

Include `public` or the dump will have dangling foreign keys.

Since this library never drops a schema, a per-tenant dump is also the natural
first step of the archival runbook you will need — see [Limits](limits.md).

## Health checks

Your health endpoint must work with **no tenant resolved**, which is the
default. Do not put `@tenant_required` on it.

```python
@app.get("/healthz")
def healthz():
    with Session() as session:
        session.execute(text("SELECT 1"))
    return {"ok": True}
```

A deeper check worth adding to a readiness probe:

```python
@app.get("/readyz")
def readyz():
    behind = [s for s in manager.migration_runner.status() if not s.is_current]
    if behind:
        return {"ok": False, "tenants_behind": len(behind)}, 503
    return {"ok": True}
```
