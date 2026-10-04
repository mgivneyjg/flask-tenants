# Observability

When something goes wrong in production, the first question is always "which
tenant?".

## Logging

A `logging.Filter` that injects the active tenant into every record. Fifteen
lines, no dependencies, and the difference between a debuggable incident and
grepping by timestamp.

```python
import logging

from flask_tenants import TenantLogFilter

handler = logging.StreamHandler()
handler.addFilter(TenantLogFilter())
handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s [%(tenant)s] %(name)s: %(message)s")
)
logging.getLogger().addHandler(handler)
```

```
2026-10-04 14:22:01 INFO [acme] myapp.billing: invoice generated
2026-10-04 14:22:03 INFO [globex] myapp.billing: invoice generated
2026-10-04 14:22:04 ERROR [-] myapp.auth: login failed
```

Two attributes are added: `tenant` (the key) and `tenant_schema` (the real
schema). `-` means no tenant was active.

With structured logging:

```python
import structlog

def add_tenant(logger, method_name, event_dict):
    from flask_tenants import current_tenant_key
    event_dict["tenant"] = current_tenant_key()
    return event_dict

structlog.configure(processors=[add_tenant, ...])
```

## Metrics

The library exposes counters and depends on no metrics backend.

```python
from flask_tenants import counters

counters()     # {"no_tenant_attempts": 0}
```

### The one to alert on

`no_tenant_attempts` counts tenant-scoped queries that ran with no tenant
active. The runtime raises in that case, so in theory it is **unreachable** —
which is exactly why it is worth counting.

**A non-zero value in production means you have found a code path nobody
tested.** Alert on any increase.

```python
from prometheus_client import Gauge
from flask_tenants import counters

no_tenant = Gauge("flask_tenants_no_tenant_attempts", "Queries with no tenant active")


@app.before_request
def _export():
    no_tenant.set(counters()["no_tenant_attempts"])
```

## Error tracking

Not a dependency — three lines in your application.

=== "Sentry"

    ```python
    import sentry_sdk
    from flask_tenants import current_tenant_key


    @app.before_request
    def tag_tenant():
        sentry_sdk.set_tag("tenant", current_tenant_key())
    ```

=== "OpenTelemetry"

    ```python
    from opentelemetry import trace
    from flask_tenants import current_tenant_key


    @app.before_request
    def annotate_span():
        span = trace.get_current_span()
        if span.is_recording():
            span.set_attribute("tenant.id", str(current_tenant_key() or "-"))
    ```

Do the same in your Celery worker, after the tenant context is entered.

## Development warnings

`SET LOCAL` is inert outside a transaction, so a raw `engine.connect()` in
autocommit gets the translate map and no `search_path`. Make that audible
while developing:

```python
from flask_tenants import warn_if_untracked

if app.debug:
    warn_if_untracked(engine)
```

It costs a check per statement, so leave it off in production.

## What to put on a dashboard

| Signal | Why |
| --- | --- |
| `no_tenant_attempts` | Should be flat at zero. Any movement is a bug. |
| Tenants not at head | From `flask-tenants status`; non-zero after a deploy means a partial migration run. |
| Tenants in `failed` state | Provisioning that needs a human. |
| Connections in use vs. `max_connections` | One shared pool across all tenants and all nodes. |
| Count of schemas | Soft-delete means this only goes up. See [Limits](limits.md). |
