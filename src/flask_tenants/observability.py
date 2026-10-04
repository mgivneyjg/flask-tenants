"""Logging and metrics hooks (decision Q15, paired half).

When something goes wrong in production the first question is always "which
tenant?". :class:`TenantLogFilter` answers it for every log line with no
changes at any call site.

Metrics and error tracking are deliberately **not** dependencies -- no Sentry
import, no OpenTelemetry. The counters are exposed; wiring them up is three
lines in your application. See the docs for the Sentry and OTel recipes.
"""

from __future__ import annotations

import logging
from typing import Any

from .binding import no_tenant_attempt_count
from .context import current_schema, current_tenant_key


class TenantLogFilter(logging.Filter):
    """Injects the active tenant into every log record.

    .. code-block:: python

        handler.addFilter(TenantLogFilter())
        handler.setFormatter(logging.Formatter("%(asctime)s [%(tenant)s] %(message)s"))

    Fifteen lines, no dependencies, and the difference between a debuggable
    incident and grepping by timestamp.
    """

    def __init__(self, default: str = "-", name: str = "") -> None:
        super().__init__(name)
        self.default = default

    def filter(self, record: logging.LogRecord) -> bool:
        key = current_tenant_key()
        record.tenant = str(key) if key is not None else self.default  # type: ignore[attr-defined]
        record.tenant_schema = current_schema() or self.default  # type: ignore[attr-defined]
        return True


def counters() -> dict[str, int]:
    """Counters worth exporting.

    ``no_tenant_attempts`` should be zero. Decision Q5 makes that condition
    raise, so in theory it is unreachable -- which is exactly why it is worth
    counting. A non-zero value in production means a code path nobody tested.
    """
    return {"no_tenant_attempts": no_tenant_attempt_count()}


def sentry_tag_hook() -> Any:  # pragma: no cover - documentation in code form
    """The Sentry integration, in full.

    Not imported or depended on -- copy it into your application::

        import sentry_sdk
        from flask_tenants import current_tenant_key

        @app.before_request
        def _tag_tenant():
            sentry_sdk.set_tag("tenant", current_tenant_key())
    """
    raise NotImplementedError(sentry_tag_hook.__doc__)


__all__ = ["TenantLogFilter", "counters"]
