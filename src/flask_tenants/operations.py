"""Running an operation across every tenant (decision Q15).

:func:`for_each_tenant` is a **maintenance primitive, not a query engine**.
Use it for backfills, bulk re-indexing, applying a fix to every tenant, and
the status report. Do not use it to build a dashboard.

Cross-tenant *analytics* is an ETL problem: replicate to a warehouse and query
there. Generated ``UNION ALL`` views across every tenant schema were rejected,
and not for performance. Decision Q10 guarantees tenants can sit at different
revisions, so the moment a migration adds a column and stops at tenant #212,
every union view is invalid. That would couple reporting correctness to the
migration run completing atomically -- exactly the property this design chose
not to require.

Parallelism is bounded, and off by default. ``ThreadPoolExecutor`` workers do
not inherit ``contextvars``, so each worker enters the tenant context itself;
and 400 concurrent tenants would exhaust the single shared pool.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterable, Sequence, TypeVar

from .models import TenantProtocol, TenantState

if TYPE_CHECKING:  # pragma: no cover
    from .manager import TenantManager

log = logging.getLogger("flask_tenants.operations")

T = TypeVar("T")


@dataclass(slots=True)
class TenantOutcome:
    """What happened for one tenant."""

    tenant_key: Any
    ok: bool
    value: Any = None
    error: BaseException | None = None
    skipped: bool = False


@dataclass(slots=True)
class BulkResult:
    """What happened across all of them."""

    outcomes: list[TenantOutcome] = field(default_factory=list)
    stopped_early: bool = False

    @property
    def succeeded(self) -> list[TenantOutcome]:
        return [o for o in self.outcomes if o.ok]

    @property
    def failed(self) -> list[TenantOutcome]:
        return [o for o in self.outcomes if not o.ok and not o.skipped]

    @property
    def skipped(self) -> list[TenantOutcome]:
        return [o for o in self.outcomes if o.skipped]

    @property
    def all_ok(self) -> bool:
        return not self.failed and not self.stopped_early

    def values(self) -> list[Any]:
        return [o.value for o in self.succeeded]

    def summary(self) -> str:
        parts = [f"{len(self.succeeded)} ok"]
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        if self.skipped:
            parts.append(f"{len(self.skipped)} not attempted")
        return ", ".join(parts)


def for_each_tenant(
    manager: "TenantManager",
    fn: Callable[[TenantProtocol], T],
    *,
    tenants: Iterable[TenantProtocol] | None = None,
    states: Sequence[TenantState] | None = (TenantState.ACTIVE,),
    continue_on_error: bool = True,
    max_workers: int = 1,
) -> BulkResult:
    """Run ``fn`` once per tenant, with that tenant active.

    :param fn: Called with the tenant. Its return value is collected.
    :param tenants: Explicit list. Defaults to the registry, filtered by
        ``states``.
    :param states: Which lifecycle states to include. Defaults to active only
        -- a pending or failed tenant has no usable schema.
    :param continue_on_error: Keep going past a failure. Defaults to ``True``,
        the opposite of the migration runner, because maintenance work is
        usually independent per tenant while a failing migration usually is
        not.
    :param max_workers: Above 1, tenants run concurrently. Keep it well below
        the connection pool size.

    Nothing is silently truncated: tenants not reached after an early stop are
    reported as skipped, because a run that quietly covered half the estate
    reads exactly like one that covered all of it.
    """
    targets = (
        list(tenants)
        if tenants is not None
        else list(manager.registry.all(states=list(states) if states else None))
    )
    result = BulkResult()

    if max_workers > 1:
        return _parallel(manager, fn, targets, max_workers, result)

    for index, tenant in enumerate(targets):
        try:
            with manager.tenant_context(tenant):
                value = fn(tenant)
            result.outcomes.append(TenantOutcome(tenant.tenant_key, ok=True, value=value))
        except Exception as exc:
            log.exception("Operation failed for tenant %s", tenant.tenant_key)
            result.outcomes.append(TenantOutcome(tenant.tenant_key, ok=False, error=exc))
            if not continue_on_error:
                result.stopped_early = True
                for remaining in targets[index + 1 :]:
                    result.outcomes.append(
                        TenantOutcome(remaining.tenant_key, ok=False, skipped=True)
                    )
                break

    log.info("for_each_tenant: %s", result.summary())
    return result


def _parallel(
    manager: "TenantManager",
    fn: Callable[[TenantProtocol], Any],
    targets: list[TenantProtocol],
    max_workers: int,
    result: BulkResult,
) -> BulkResult:
    """Fan out across threads.

    Each worker enters the tenant context *itself*. ``contextvars`` are not
    inherited by threads -- only by asyncio tasks -- so relying on the caller's
    context here would silently run every worker with no tenant active.
    """

    def _run(tenant: TenantProtocol) -> TenantOutcome:
        try:
            with manager.tenant_context(tenant):
                return TenantOutcome(tenant.tenant_key, ok=True, value=fn(tenant))
        except Exception as exc:
            log.exception("Operation failed for tenant %s", tenant.tenant_key)
            return TenantOutcome(tenant.tenant_key, ok=False, error=exc)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_run, t): t for t in targets}
        for future in as_completed(futures):
            result.outcomes.append(future.result())

    result.outcomes.sort(key=lambda o: str(o.tenant_key))
    log.info("for_each_tenant (parallel): %s", result.summary())
    return result


__all__ = ["BulkResult", "TenantOutcome", "for_each_tenant"]
