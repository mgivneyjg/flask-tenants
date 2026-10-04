# Provisioning tenants

## How a new schema is built

By **replaying the migration history** — `CREATE SCHEMA`, then
`alembic upgrade head` against it.

```python
from flask_tenants import provision_tenant

result = provision_tenant(manager, tenant, runner=manager.migration_runner)
# ProvisionResult(tenant_key=7, schema_name='tenant_7', created=True, revision='4f2c9a1b8e03')
```

Replay is linear in migration count, so signup gets slower every time you add
a revision. That is a bounded, visible cost, and it buys the one property that
matters: a new tenant's schema is *by definition* identical to every existing
tenant's, because it took the same path.

!!! danger "Do not use `create_all()` + `stamp head`"
    It is fast and constant-time, and it is a trap. `create_all()` builds from
    the current model definitions, which is **not** the same as replaying the
    migrations. Anything a migration did outside SQLAlchemy metadata is
    silently absent:

    - `op.execute` raw SQL
    - partial and functional indexes
    - triggers
    - check constraints added by hand
    - seed and reference data
    - backfilled column defaults

    Tenants provisioned before and after such a migration end up structurally
    different, and nothing surfaces it until one customer's query behaves
    oddly months later. You would be trading a bounded, visible cost for an
    unbounded, invisible one.

## Idempotence

`provision_tenant()` is safe to call again — after a failure, from a retried
background job, or twice by accident. An existing schema is migrated rather
than recreated.

```python
first  = provision_tenant(manager, tenant, runner=runner)   # created=True
second = provision_tenant(manager, tenant, runner=runner)   # created=False
```

## Lifecycle states

Provisioning drives the tenant through `PENDING → ACTIVE`, or `PENDING →
FAILED`. Resolvers only serve `ACTIVE`, which is what stops a half-built
schema from ever answering a request.

Persisting the transition is your job, since the library does not own your
tenant table:

```python
def persist(tenant, state):
    with Session() as session:
        session.merge(tenant)
        session.commit()


provision_tenant(manager, tenant, runner=runner, on_state_change=persist)
```

## Where it should run

The library ships **no task queue** — the same reasoning that keeps Redis and
Celery out of its dependencies. What it ships is an operation safe to call
from anywhere, plus a state field that makes the "where" an application
decision rather than an architectural one.

=== "Inline (start here)"

    ```python
    @app.post("/signup")
    def signup():
        tenant = create_tenant_row(request.form)
        provision_tenant(manager, tenant, runner=runner, on_state_change=persist)
        return redirect(url_for("welcome"))
    ```

    Fine at low volume with few migrations. Degrades predictably into
    load-balancer timeouts as the history grows.

=== "Celery"

    ```python
    @app.post("/signup")
    def signup():
        tenant = create_tenant_row(request.form)     # state=PENDING
        provision.delay(tenant_key=tenant.id)
        return redirect(url_for("provisioning_status"))


    @celery.task
    def provision(tenant_key):
        tenant = manager.registry.require(tenant_key)
        provision_tenant(manager, tenant, runner=runner, on_state_change=persist)
    ```

    Note this task does **not** use `@with_tenant` — it runs *before* the
    tenant is servable, so entering its context would raise
    `TenantNotReadyError`.

=== "CLI"

    ```bash
    flask-tenants provision acme
    ```

## Speeding it up later: template cloning

When replay time genuinely hurts, clone a template schema instead. Keep one
canonical `tenant_template` that the normal migration run upgrades along with
everyone else, and provision by deep-copying it with the `clone_schema`
plpgsql function.

Fast *and* faithful, since the template got to head by the same migration
path. The invariant that keeps it safe:

!!! warning "The template must be a tenant"
    Register it as a real row so the migration runner upgrades it
    automatically, and assert it is at head before any provisioning is
    allowed. A stale template silently stamping new tenants at the wrong
    revision is `create_all()`'s failure mode wearing a disguise.

Move to it when replay time bothers you, not before.

## Offboarding

```python
from flask_tenants import deactivate_tenant

deactivate_tenant(tenant, on_state_change=persist)
```

Flips the state to `INACTIVE`. The resolver stops serving it; the schema and
every row in it are left exactly where they are.

**This library never drops a schema.** There is no `purge_tenant`, no guarded
CLI command, no `--force`. See [Limits](limits.md) for what that means for your
runbook.
