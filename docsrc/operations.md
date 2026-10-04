# Operations across tenants

```python
from flask_tenants import for_each_tenant

result = for_each_tenant(manager, backfill)
print(result.summary())      # "398 ok, 2 failed"
```

`for_each_tenant` runs a callable once per tenant, with that tenant active.

## It is a maintenance primitive, not a query engine

Use it for backfills, bulk re-indexing, applying a fix everywhere, and
reporting on schema state. **Do not** use it to build a dashboard.

```python
def backfill_colour(tenant):
    with Session() as session:
        session.execute(
            update(Widget).where(Widget.colour.is_(None)).values(colour="unset")
        )
        session.commit()
        return session.execute(select(func.count()).select_from(Widget)).scalar_one()


result = for_each_tenant(manager, backfill_colour)
```

## Cross-tenant analytics is an ETL problem

"How many appointments were booked last month, across all customers?" is not a
question to answer with this. Replicate to a warehouse and query there.

!!! info "Why `UNION ALL` views were rejected"
    The obvious alternative is generating a view in `public` that unions every
    tenant's copy of a table. It was rejected on **correctness**, not
    performance — though a 400-branch union is genuinely rough on the planner.

    Tenants can legitimately sit at different revisions. The moment a
    migration adds a column and the run stops at tenant #212, tenants 1–211
    have the column and 213–400 do not, and every generated union view is
    invalid. That couples your reporting layer's correctness to the migration
    run completing atomically — precisely the property this design chose not
    to require. The feature and the failure are the same mechanism.

## Error handling

Defaults to **continuing** past a failure, the opposite of the migration
runner, because maintenance work is usually independent per tenant while a
failing migration usually is not.

```python
result = for_each_tenant(manager, backfill)

for outcome in result.failed:
    log.error("tenant %s: %s", outcome.tenant_key, outcome.error)

if not result.all_ok:
    raise SystemExit(1)
```

| Attribute | Contents |
| --- | --- |
| `result.succeeded` | Outcomes that worked |
| `result.failed` | Outcomes that raised, with `.error` |
| `result.skipped` | Not attempted after an early stop |
| `result.values()` | Return values from the successes |
| `result.all_ok` | True if nothing failed and nothing was skipped |

Stop at the first failure when the work is not independent:

```python
for_each_tenant(manager, risky, continue_on_error=False)
```

Nothing is silently truncated: tenants not reached are reported as skipped,
because a partial run should not read like a complete one.

## Selecting tenants

Defaults to **active** tenants only — pending and failed ones have no usable
schema.

```python
for_each_tenant(manager, fn, states=[TenantState.ACTIVE, TenantState.INACTIVE])
for_each_tenant(manager, fn, tenants=[acme, globex])
```

## Parallelism

Off by default, and bounded when you turn it on.

```python
for_each_tenant(manager, fn, max_workers=4)
```

Two constraints set the ceiling:

- **Threads do not inherit `contextvars`.** Each worker enters the tenant
  context itself — relying on the caller's context would silently run every
  worker with no tenant active.
- **One shared connection pool.** 400 concurrent tenants would exhaust it.
  Keep `max_workers` well under `pool_size`, and remember the web nodes are
  drawing from the same `max_connections`.

Start at 4. Measure before raising it.

## From the CLI

```bash
flask-tenants run myapp.maintenance:backfill_colour --workers 4
```

## Long backfills

Write them resumably rather than relying on the run completing:

```python
def backfill(tenant):
    with Session() as session:
        while True:
            rows = session.execute(
                select(Widget).where(Widget.colour.is_(None)).limit(1000)
            ).scalars().all()
            if not rows:
                return "done"
            for row in rows:
                row.colour = "unset"
            session.commit()        # (1)!
```

1.  Commit per batch. An interrupted run then resumes where it stopped, and
    no single transaction holds locks across the whole table.
