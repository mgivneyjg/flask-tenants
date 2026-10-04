"""The active-tenant context (decision Q5).

The current tenant lives in a module-level :class:`~contextvars.ContextVar`
owned by this package -- deliberately *not* in ``flask.g``.

Flask's ``g`` is itself ``ContextVar``-backed, so the two behave identically
inside a request and diverge completely outside one. A Celery worker, a CLI
command, a cron script or a pytest fixture has no application context, and
requiring every such caller to push one makes the failure surface worse for a
library whose entire job is never touching the wrong tenant's data.

So :func:`tenant_context` is the primitive. Flask's ``before_request`` is one
adapter among several, not the only way in.

Propagation (decision Q17) is worth knowing precisely, because the three cases
differ and get conflated constantly:

* **asyncio tasks** inherit the context automatically.
* **Threads do not.** ``ThreadPoolExecutor`` workers start with a fresh context,
  so anything fanning out across threads must set the tenant inside the worker.
* **Queue workers** do not even share a process. Use :func:`capture` and
  :func:`restore`, or pass the tenant key as an explicit task argument.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .errors import NoActiveTenantError

if TYPE_CHECKING:  # pragma: no cover
    from .models import TenantProtocol


@dataclass(frozen=True, slots=True)
class Binding:
    """What the context var actually holds.

    Kept separate from the application's tenant model so the binding layer never
    needs to touch the ORM, and so an explicit "operate on shared tables" state
    can be represented distinctly from "no tenant set".
    """

    key: Any
    """The tenant's stable identifier, or ``None`` for an explicit public binding."""

    schema: str
    """The real PostgreSQL schema that tenant-scoped tables resolve to."""

    tenant: TenantProtocol | None = None
    """The application's tenant object, when one was supplied."""

    is_public: bool = False
    """True when this binding came from :func:`public_schema` rather than a tenant."""

    def __repr__(self) -> str:  # pragma: no cover - debugging affordance
        if self.is_public:
            return f"<Binding public schema={self.schema!r}>"
        return f"<Binding key={self.key!r} schema={self.schema!r}>"


_active: ContextVar[Binding | None] = ContextVar("flask_tenants_active", default=None)


def current_binding() -> Binding | None:
    """Return the active :class:`Binding`, or ``None`` if nothing is active."""
    return _active.get()


def require_binding() -> Binding:
    """Return the active :class:`Binding`, raising if there is none.

    This is what the session binding layer calls. The hard failure is the point
    -- see :class:`~flask_tenants.errors.NoActiveTenantError`.
    """
    binding = _active.get()
    if binding is None:
        raise NoActiveTenantError(
            "No tenant is active. Wrap the operation in `with tenant_context(tenant):`, "
            "or use `with public_schema():` if you meant to work on shared tables."
        )
    return binding


def current_tenant() -> TenantProtocol | None:
    """Return the active tenant object, or ``None``.

    Returns ``None`` both when nothing is active and when the active binding is
    an explicit public one. Use :func:`current_binding` to tell those apart.
    """
    binding = _active.get()
    return binding.tenant if binding else None


def current_tenant_key() -> Any | None:
    """Return the active tenant's key, or ``None``.

    This is the value to hand to a background worker; see :func:`capture`.
    """
    binding = _active.get()
    return binding.key if binding else None


def current_schema() -> str | None:
    """Return the real schema tenant-scoped tables currently resolve to."""
    binding = _active.get()
    return binding.schema if binding else None


def set_binding(binding: Binding | None) -> Token:
    """Set the active binding, returning a token for :func:`reset_binding`.

    Prefer the :func:`tenant_context` context manager. This exists for adapters
    whose entry and exit are genuinely separate calls -- Flask's
    ``before_request`` / ``teardown_request`` pair, for instance.
    """
    return _active.set(binding)


def reset_binding(token: Token) -> None:
    """Restore the binding a :func:`set_binding` call replaced."""
    _active.reset(token)


@contextlib.contextmanager
def bind(binding: Binding | None) -> Iterator[Binding | None]:
    """Activate a prepared :class:`Binding` for the duration of the block.

    Restoring via the token (rather than setting back to ``None``) is what makes
    nesting correct: a shared-table lookup inside a tenant request leaves the
    outer tenant intact on exit.
    """
    token = _active.set(binding)
    try:
        yield binding
    finally:
        _active.reset(token)


@contextlib.contextmanager
def public_schema(schema: str = "public") -> Iterator[Binding]:
    """Operate on shared tables with no tenant active.

    The explicit escape hatch that makes the hard failure in
    :func:`require_binding` reasonable. Saying "I mean the shared schema" is
    cheap; silently defaulting to it is how cross-tenant bugs start.
    """
    binding = Binding(key=None, schema=schema, tenant=None, is_public=True)
    with bind(binding):
        yield binding


def capture() -> Any | None:
    """Return the active tenant key, for carrying across a process boundary.

    Pair with :func:`restore` on the far side. See the Celery and RQ recipes in
    the documentation -- the library deliberately depends on neither.
    """
    return current_tenant_key()


@contextlib.contextmanager
def tenant_context(tenant: TenantProtocol) -> Iterator[Binding]:
    """Activate a tenant for the duration of the block.

    Takes a tenant *object* -- anything satisfying
    :class:`~flask_tenants.models.TenantProtocol`. No database lookup happens
    here, which is what keeps this module free of any ORM import and makes the
    primitive usable absolutely anywhere.

    To activate from a bare key, use
    :meth:`~flask_tenants.manager.TenantManager.tenant_context`, which consults
    the registry first.

    .. code-block:: python

        with tenant_context(acme):
            session.add(Patient(name="..."))
            session.commit()
    """
    binding = Binding(
        key=tenant.tenant_key,
        schema=tenant.schema_name,
        tenant=tenant,
        is_public=False,
    )
    with bind(binding):
        yield binding
