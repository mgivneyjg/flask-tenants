"""The Flask integration -- deliberately thin (decisions Q3, Q5, Q6).

Everything of substance lives in :class:`~flask_tenants.manager.TenantManager`.
This module resolves the tenant in ``before_request``, enters the context, and
unwinds it in ``teardown_request``. If logic starts accumulating here it
belongs in the core instead, or you end up with two code paths that drift.
"""

from __future__ import annotations

import logging
from functools import wraps
from typing import TYPE_CHECKING, Any, Callable

from flask import Flask, current_app, g, request

from .context import Binding, current_tenant, reset_binding, set_binding
from .errors import NoActiveTenantError, TenantError
from .models import TenantProtocol

if TYPE_CHECKING:  # pragma: no cover
    from .manager import TenantManager

log = logging.getLogger("flask_tenants.flask")

_EXT_KEY = "flask_tenants"
_TOKEN_KEY = "_flask_tenants_token"


class FlaskTenants:
    """Binds a :class:`~flask_tenants.manager.TenantManager` to a Flask app.

    .. code-block:: python

        tenants = FlaskTenants(manager)
        tenants.init_app(app)

    With Flask-SQLAlchemy, build the manager from ``db.engine`` and
    ``db.session`` and pass it in the same way -- the adapter stays a shim.

    :param manager: The configured manager.
    :param strict_routes: Reject requests resolving to no tenant. Off by
        default: a health check and a signup page must work before any tenant
        exists. Guard individual routes with :func:`tenant_required` instead.
    """

    def __init__(
        self,
        manager: "TenantManager | None" = None,
        app: Flask | None = None,
        *,
        strict_routes: bool = False,
    ) -> None:
        self.manager = manager
        self.strict_routes = strict_routes
        if app is not None:
            self.init_app(app)

    def init_app(self, app: Flask, manager: "TenantManager | None" = None) -> None:
        """Register the request hooks."""
        manager = manager or self.manager
        if manager is None:
            raise TenantError("FlaskTenants needs a TenantManager")
        self.manager = manager

        app.config.setdefault("TENANTS_STRICT_ROUTES", self.strict_routes)
        app.extensions[_EXT_KEY] = self

        app.before_request(self._before_request)
        app.teardown_request(self._teardown_request)

    # -- request lifecycle ------------------------------------------------

    def _before_request(self) -> None:
        """Resolve the tenant and enter its context.

        A thin adapter over ``manager.tenant_context``. The context itself
        lives in a ``ContextVar`` owned by this package, not in ``g``, so the
        same primitive works in a worker with no application context.
        """
        assert self.manager is not None
        tenant = self.manager.resolve(request)

        if tenant is None:
            if current_app.config.get("TENANTS_STRICT_ROUTES"):
                raise NoActiveTenantError(
                    f"No tenant resolved for {request.host}{request.path} "
                    "and TENANTS_STRICT_ROUTES is on."
                )
            g.tenant = None
            return

        binding = Binding(
            key=tenant.tenant_key,
            schema=tenant.schema_name,
            tenant=tenant,
            is_public=False,
        )
        g.tenant = tenant
        g.__dict__[_TOKEN_KEY] = set_binding(binding)

    def _teardown_request(self, exc: BaseException | None = None) -> None:
        """Unwind the context, including when the request raised."""
        token = g.__dict__.pop(_TOKEN_KEY, None)
        if token is not None:
            reset_binding(token)


def tenant_required(view: Callable[..., Any]) -> Callable[..., Any]:
    """Reject requests that resolved to no tenant.

    The default is permissive -- a request with no tenant runs in the shared
    schema -- so strictness is one decorator away rather than a global setting
    that breaks the signup flow.

    .. code-block:: python

        @app.get("/patients")
        @tenant_required
        def list_patients():
            ...
    """

    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if current_tenant() is None:
            raise NoActiveTenantError(
                f"{request.path} requires a tenant, but none was resolved "
                f"for host {request.host!r}."
            )
        return view(*args, **kwargs)

    return wrapper


def current_tenant_or_404() -> TenantProtocol:
    """The active tenant, aborting with 404 if there is none.

    404 rather than 400: an unresolvable hostname should not confirm whether
    a given tenant exists.
    """
    from flask import abort

    tenant = current_tenant()
    if tenant is None:
        abort(404)
    return tenant


__all__ = ["FlaskTenants", "current_tenant_or_404", "tenant_required"]
