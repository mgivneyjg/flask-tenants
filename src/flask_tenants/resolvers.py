"""Resolving a request to a tenant (decision Q6).

Resolvers form an **ordered chain**; the first one returning a key wins. A
subdomain deployment short-circuits before the session is consulted; a
bare-domain deployment falls through to it.

.. code-block:: python

    resolvers = [SubdomainResolver(base_domain="app.example.com"), SessionResolver()]

The interface is deliberately trivial -- a callable taking the request and
returning a key or ``None``. The moment it grows lifecycle hooks it stops being
an interface and starts being a framework, so if the shipped resolvers do not
fit, writing your own should take about ten lines.

The first four resolvers are **stateless**: the tenant is derivable from the
request, and the answer does not depend on who is logged in.
:class:`SessionResolver` is not, which is why it carries an authorization
obligation the others do not -- see its docstring.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Sequence
from typing import Any


class TenantResolver(ABC):
    """Turns a request into a tenant key, or ``None`` to defer to the next."""

    @abstractmethod
    def resolve(self, request: Any) -> Any | None:
        """Return a tenant key, or ``None`` if this resolver has no opinion."""

    def __call__(self, request: Any) -> Any | None:
        return self.resolve(request)


def _hostname(request: Any) -> str:
    """Hostname without the port, lowercased.

    ``request.host`` is Flask/Werkzeug; ``request.headers['Host']`` is the
    fallback for anything else that looks request-shaped.
    """
    host = getattr(request, "host", None)
    if host is None:
        headers = getattr(request, "headers", {}) or {}
        host = headers.get("Host", "") if hasattr(headers, "get") else ""
    return str(host).split(":")[0].strip().lower()


class SubdomainResolver(TenantResolver):
    """``acme.app.example.com`` resolves to ``acme``.

    No database lookup, so it is the cheapest resolver -- but it cannot serve a
    tenant on its own vanity domain. Use :class:`DomainResolver` if that is ever
    on the roadmap; a subdomain is just a row in the domain table.

    :param base_domain: The suffix to strip. Required, so that
        ``app.example.com`` itself resolves to no tenant rather than ``app``.
    :param ignore: Subdomains that are never tenants.
    """

    def __init__(
        self,
        base_domain: str,
        *,
        ignore: Iterable[str] = ("www", "api", "admin", "static"),
    ) -> None:
        self.base_domain = base_domain.strip().lower().lstrip(".")
        self.ignore = {s.lower() for s in ignore}

    def resolve(self, request: Any) -> Any | None:
        host = _hostname(request)
        suffix = "." + self.base_domain
        if not host.endswith(suffix):
            return None
        label = host[: -len(suffix)]
        if not label or "." in label or label in self.ignore:
            return None
        return label


class DomainResolver(TenantResolver):
    """Looks the full hostname up in the domain table. **The default.**

    A strict superset of :class:`SubdomainResolver` -- a subdomain is simply a
    row -- which is why it is the shipped default despite costing a lookup.
    Retrofitting custom domains later would otherwise mean a migration plus a
    resolver swap in every deployment.

    :param registry: Where to look the hostname up.
    """

    def __init__(self, registry: Any) -> None:
        self.registry = registry

    def resolve(self, request: Any) -> Any | None:
        host = _hostname(request)
        if not host:
            return None
        tenant = self.registry.get_by_domain(host)
        return tenant.tenant_key if tenant is not None else None


class PathPrefixResolver(TenantResolver):
    """``/t/acme/patients`` resolves to ``acme``.

    The only resolver that costs nothing in DNS or TLS, which makes it a good
    fit for internal tools and for local development.

    :param prefix: Leading path segment marking a tenant-scoped URL.
    """

    def __init__(self, prefix: str = "t") -> None:
        self.prefix = prefix.strip("/")

    def resolve(self, request: Any) -> Any | None:
        path = str(getattr(request, "path", "") or "")
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] == self.prefix:
            return parts[1]
        return None


class HeaderResolver(TenantResolver):
    """Reads the tenant key from a request header.

    Natural for an API behind a separate frontend, where subdomains are pure
    overhead.

    A header is caller-supplied, so this grants nothing on its own: whether the
    caller may *use* that tenant is an authorization question your application
    must still answer. See :class:`SessionResolver` for the same point at
    greater length.
    """

    def __init__(self, header: str = "X-Tenant-ID") -> None:
        self.header = header

    def resolve(self, request: Any) -> Any | None:
        headers = getattr(request, "headers", None)
        if headers is None:
            return None
        value = headers.get(self.header)
        return value.strip() if value else None


class SessionResolver(TenantResolver):
    """Reads the tenant the user chose at login out of the session.

    For deployments with no subdomain: the user logs in, the application looks
    up which tenants they belong to, asks them to pick if there is more than
    one, and stores the choice. Logout or expiry clears it.

    **This resolver does not grant access.** It reports a stored preference.
    Membership must be re-validated on every request -- which is what
    ``membership_check`` on
    :class:`~flask_tenants.manager.TenantManager` is for. A session that
    outlives a revoked grant would otherwise keep serving that tenant's data
    until it expired.

    Two further things worth knowing:

    * Flask's default session is a **signed cookie**, so it needs no shared
      store and no sticky sessions -- every node verifies it with the same
      ``SECRET_KEY``. Swapping in a server-side store (Flask-Session with
      Redis) is an application-side config change; this resolver reads through
      the session interface either way and does not care.
    * The session is **shared across tabs**. A user in two tenants with two
      tabs open can have one act on the other's data. The stateless resolvers
      are immune to this by construction; if it matters, echo the active tenant
      in a header or hidden field and reject on mismatch.

    :param key: Session key holding the chosen tenant.
    :param session_getter: Where to read the session from. Defaults to
        ``flask.session``.
    """

    def __init__(self, key: str = "tenant_id", session_getter: Callable[[], Any] | None = None):
        self.key = key
        self._session_getter = session_getter

    def _session(self) -> Any:
        if self._session_getter is not None:
            return self._session_getter()
        from flask import session as flask_session  # imported lazily: Flask is optional

        return flask_session

    def resolve(self, request: Any) -> Any | None:
        try:
            session = self._session()
        except RuntimeError:  # outside a request context
            return None
        if session is None:
            return None
        value = session.get(self.key)
        return value if value not in (None, "") else None


class CallableResolver(TenantResolver):
    """Wraps a plain function, for when a class is more ceremony than it is worth."""

    def __init__(self, fn: Callable[[Any], Any | None]) -> None:
        self.fn = fn

    def resolve(self, request: Any) -> Any | None:
        return self.fn(request)


class ResolverChain(TenantResolver):
    """Tries each resolver in order; first non-``None`` wins."""

    def __init__(self, resolvers: Sequence[TenantResolver | Callable[[Any], Any | None]]) -> None:
        self.resolvers: list[TenantResolver] = [
            r if isinstance(r, TenantResolver) else CallableResolver(r) for r in resolvers
        ]

    def resolve(self, request: Any) -> Any | None:
        for resolver in self.resolvers:
            key = resolver.resolve(request)
            if key is not None:
                return key
        return None

    def __repr__(self) -> str:  # pragma: no cover
        names = ", ".join(type(r).__name__ for r in self.resolvers)
        return f"<ResolverChain [{names}]>"


__all__ = [
    "CallableResolver",
    "DomainResolver",
    "HeaderResolver",
    "PathPrefixResolver",
    "ResolverChain",
    "SessionResolver",
    "SubdomainResolver",
    "TenantResolver",
]
