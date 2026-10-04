# Celery and background workers

This is the part people get wrong, so it is worth being precise about *why*
rather than just handing you a snippet.

## Three propagation behaviours, routinely conflated

The tenant lives in a `contextvars.ContextVar`. How that travels depends
entirely on what you are crossing:

| Boundary | Inherits the context? | What you must do |
| --- | --- | --- |
| **asyncio task** | **Yes**, automatically | Nothing. |
| **Thread** (`ThreadPoolExecutor`) | **No** | Set the tenant *inside* the worker. |
| **Process** (Celery, RQ, a subprocess) | **No** — different process entirely | Carry the key in the message and restore it. |

The middle row surprises people most. `contextvars` were designed for asyncio,
and threads start with a fresh context:

```python
from concurrent.futures import ThreadPoolExecutor
from flask_tenants import current_tenant_key, tenant_context

with tenant_context(acme):
    with ThreadPoolExecutor() as pool:
        assert pool.submit(current_tenant_key).result() is None   # not "acme"
```

This is why [`for_each_tenant(..., max_workers=4)`](operations.md) has each
worker enter the context itself rather than relying on inheritance.

## The failure mode

Enqueue a task inside a tenant context and expect the worker to know:

```python
with tenant_context(acme):
    rebuild_index.delay()          # the tenant does NOT travel
```

The worker runs with no tenant active, the strict check raises
`NoActiveTenantError`, and you get a flood of errors that looks like a worker
misconfiguration rather than a missing argument. That failure is the *good*
outcome — the alternative design, falling back to `public`, would have the
task silently operate on the wrong data.

## Option 1: explicit task arguments (recommended for new code)

The most honest signature. The dependency is visible at every call site, and
nothing magic has to work.

```python title="myapp/tasks.py"
from flask_tenants import with_tenant
from myapp.tenancy import Session, manager


@celery.task
@with_tenant(manager)                       # (1)!
def rebuild_index(tenant_key: str, since: str | None = None):
    with Session() as session:
        ...
```

1.  Reads the `tenant_key` keyword argument, looks the tenant up in the
    registry, and enters its context for the duration of the task. Raises a
    clear `TypeError` if the argument is missing.

Call it with the tenant spelled out:

```python
rebuild_index.delay(tenant_key=acme.tenant_key, since="2026-01-01")
```

## Option 2: header propagation (the safety net)

Attaches the tenant to the message automatically, so a task added by someone
who did not read this page still works.

```python title="myapp/celery_app.py"
from celery import Celery
from celery.signals import before_task_publish, task_prerun, task_postrun

from flask_tenants import celery_signal_handlers
from myapp.tenancy import manager

celery = Celery("myapp", broker="redis://localhost:6379/0")

handlers = celery_signal_handlers(manager)                       # (1)!
before_task_publish.connect(handlers["before_task_publish"])
task_prerun.connect(handlers["task_prerun"])
task_postrun.connect(handlers["task_postrun"])
```

1.  Returned rather than registered, so this package never imports Celery. The
    handlers stamp the active tenant into the message headers on publish, and
    re-enter that tenant's context for the duration of the task on the worker.

Now the naive version works:

```python
with tenant_context(acme):
    rebuild_index.delay()          # the tenant travels in the headers
```

A task published with **no** tenant active restores the *public* context on the
worker, so shared-schema tasks behave identically either way.

!!! tip "Use both"
    They compose. Explicit arguments make the dependency visible where it
    matters; header propagation catches the task somebody adds in a hurry.

## RQ

Same primitives, different plumbing:

```python title="myapp/rq_tasks.py"
from flask_tenants import capture_headers, restore_into
from myapp.tenancy import manager


def enqueue_rebuild(queue, **kwargs):
    queue.enqueue(rebuild_index, meta=capture_headers(), **kwargs)   # (1)!


def rebuild_index():
    from rq import get_current_job

    with restore_into(manager, get_current_job().meta):              # (2)!
        ...
```

1.  `capture_headers()` returns `{"flask_tenants_tenant": <key>}`, or an empty
    dict when no tenant is active.
2.  `restore_into()` re-enters that tenant, or the public context when the key
    is absent.

## Celery Beat and other schedulers

A scheduled task has no publisher context to capture, so headers cannot help.
Make the fan-out explicit:

```python
@celery.task
def nightly_rollup_all():
    """Beat calls this; it enqueues one task per tenant."""
    for tenant in manager.registry.active():
        nightly_rollup.delay(tenant_key=tenant.tenant_key)


@celery.task
@with_tenant(manager)
def nightly_rollup(tenant_key):
    ...
```

One task per tenant, rather than one task looping over all of them. A failure
then affects one customer instead of aborting the run, and retries are
per-tenant.

## Flask and Celery together

If your tasks need an application context (`current_app`, extensions), push it
*inside* the tenant context, not instead of it:

```python
@celery.task
@with_tenant(manager)
def send_welcome(tenant_key, user_id):
    with app.app_context():        # (1)!
        ...
```

1.  The tenant context is **not** an application context and does not need
    one — that is the whole point of owning the `ContextVar` rather than using
    `flask.g`. Push an app context only if your code needs Flask features.

## Checklist

- [ ] Workers connect to the same database with the same `SECRET_KEY` only if
      they read sessions — most do not.
- [ ] Task signatures name the tenant, or the signal handlers are connected.
- [ ] Scheduled jobs fan out per tenant rather than looping inside one task.
- [ ] Worker connection pools are counted in your
      [`max_connections` budget](deployment.md).
- [ ] The worker's logging has the [tenant log filter](observability.md)
      attached — "which tenant?" is the first question in every incident.
