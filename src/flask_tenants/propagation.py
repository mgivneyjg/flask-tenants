"""Carrying the tenant across a process boundary (decision Q17).

Three propagation behaviours get conflated constantly, and they differ:

* **asyncio tasks** inherit ``contextvars`` automatically.
* **Threads do not.** A ``ThreadPoolExecutor`` worker starts with a fresh
  context.
* **Queue workers** do not even share a process.

Enqueuing a task inside a tenant context and expecting the worker to know is
the single most common bug in systems like this: the task runs with no tenant,
the strict check raises, and the resulting flood of errors looks like a worker
misconfiguration.

This package depends on no queue. :func:`capture` and :func:`restore_into` are
the generic pair; the documentation carries Celery and RQ recipes built on
them. For new code, an explicit ``tenant_key`` task argument is the more honest
signature -- it makes the dependency visible at the call site. Header
propagation is the safety net for when someone forgets.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

from .context import capture, current_tenant_key

if TYPE_CHECKING:  # pragma: no cover
    from .manager import TenantManager

F = TypeVar("F", bound=Callable[..., Any])

#: Conventional key for the tenant in a task payload or message header.
TENANT_HEADER = "flask_tenants_tenant"


def capture_headers() -> dict[str, Any]:
    """The active tenant as a header dict, for attaching to a message."""
    key = capture()
    return {TENANT_HEADER: key} if key is not None else {}


def restore_into(manager: TenantManager, headers: dict[str, Any] | None):
    """Context manager re-entering the tenant a message was published under.

    .. code-block:: python

        with restore_into(manager, task.request.headers):
            do_the_work()

    A message with no tenant restores the public context, so a task enqueued
    outside any tenant behaves identically on the worker.
    """
    key = (headers or {}).get(TENANT_HEADER)
    return manager.restore(key)


def with_tenant(manager: TenantManager, key_arg: str = "tenant_key") -> Callable[[F], F]:
    """Decorator entering a tenant context from a named keyword argument.

    For the explicit style -- the tenant is part of the task's signature, so
    the dependency is visible wherever the task is called.

    .. code-block:: python

        @celery.task
        @with_tenant(manager)
        def rebuild_index(tenant_key, since=None):
            ...
    """

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            key = kwargs.get(key_arg)
            if key is None:
                raise TypeError(
                    f"{fn.__name__}() needs a {key_arg!r} argument naming the tenant to run as."
                )
            with manager.restore(key):
                return fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


def celery_signal_handlers(manager: TenantManager) -> dict[str, Callable[..., Any]]:
    """Celery handlers implementing header propagation.

    Returned rather than registered, so this package never imports Celery.
    Wire them up in your application::

        from celery.signals import before_task_publish, task_prerun, task_postrun

        handlers = celery_signal_handlers(manager)
        before_task_publish.connect(handlers["before_task_publish"])
        task_prerun.connect(handlers["task_prerun"])
        task_postrun.connect(handlers["task_postrun"])
    """
    stack: dict[int, Any] = {}

    def before_task_publish(sender=None, headers=None, **_: Any) -> None:
        key = current_tenant_key()
        if key is not None and headers is not None:
            headers[TENANT_HEADER] = key

    def task_prerun(task_id=None, task=None, **_: Any) -> None:
        headers = getattr(getattr(task, "request", None), "headers", None) or {}
        key = headers.get(TENANT_HEADER)
        ctx = manager.restore(key)
        ctx.__enter__()
        stack[id(task_id)] = ctx

    def task_postrun(task_id=None, **_: Any) -> None:
        ctx = stack.pop(id(task_id), None)
        if ctx is not None:
            ctx.__exit__(None, None, None)

    return {
        "before_task_publish": before_task_publish,
        "task_prerun": task_prerun,
        "task_postrun": task_postrun,
    }


__all__ = [
    "TENANT_HEADER",
    "capture_headers",
    "celery_signal_handlers",
    "restore_into",
    "with_tenant",
]
