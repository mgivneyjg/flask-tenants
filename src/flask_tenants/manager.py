"""The core, framework-agnostic entry point.

:class:`TenantManager` ties the registry, the resolver chain and the schema
binder together. It has no Flask dependency -- decision Q3 -- so the same
object serves a web request, a Celery task, a CLI command and a test.

.. code-block:: python

    manager = TenantManager(
        engine=engine,
        session_factory=Session,
        bases=bases,
        registry=SQLAlchemyRegistry(Session, Tenant, domain_model=Domain),
        resolvers=[DomainResolver(registry), SessionResolver()],
    )
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Callable, Iterator, Sequence

from .binding import SchemaBinder
from .context import Binding, bind, current_binding, public_schema, tenant_context
from .errors import TenantNotReadyError, UnknownTenantError
from .models import Bases, TenantProtocol, TenantState, validate_model_layout
from .registry import TenantRegistry
from .resolvers import ResolverChain, TenantResolver

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session, sessionmaker

log = logging.getLogger("flask_tenants")

#: Signature of a membership check. Returns True if this request may use this
#: tenant. See `membership_check` on TenantManager.
MembershipCheck = Callable[[TenantProtocol, Any], bool]


class TenantManager:
    """Wires the pieces together.

    :param engine: The SQLAlchemy engine. One shared pool for all tenants
        (decision Q13) -- per-tenant pools do not scale.
    :param session_factory: A ``sessionmaker``, or a ``scoped_session``. The
        binder installs its listeners here.
    :param bases: What :func:`~flask_tenants.models.make_bases` returned.
    :param registry: Where tenants are looked up.
    :param resolvers: Ordered resolver chain. First non-``None`` wins.
    :param shared_schema: Real schema holding shared tables.
    :param schema_prefix: Prefix for derived schema names.
    :param strict: Raise when a tenant-scoped query runs with no tenant active.
        Leave on.
    :param membership_check: Called after a resolver produces a tenant, before
        it is activated. **Required if you use**
        :class:`~flask_tenants.resolvers.SessionResolver`, because the session
        caches a choice rather than granting access -- see decision Q7.
    :param validate_layout: Assert at startup that no shared table holds a
        foreign key into a tenant table.
    """

    def __init__(
        self,
        *,
        engine: "Engine",
        session_factory: "sessionmaker | Any",
        bases: Bases,
        registry: TenantRegistry,
        resolvers: Sequence[TenantResolver | Callable[[Any], Any | None]] = (),
        shared_schema: str = "public",
        schema_prefix: str = "tenant_",
        strict: bool = True,
        set_search_path: bool = True,
        membership_check: MembershipCheck | None = None,
        validate_layout: bool = True,
    ) -> None:
        self.engine = engine
        self.session_factory = session_factory
        self.bases = bases
        self.registry = registry
        self.resolvers = ResolverChain(list(resolvers))
        self.shared_schema = shared_schema
        self.schema_prefix = schema_prefix
        self.membership_check = membership_check

        self.binder = SchemaBinder(
            shared_schema=shared_schema,
            tenant_token=bases.tenant_metadata.schema or "tenant",
            set_search_path=set_search_path,
            strict=strict,
        )
        self.binder.install(session_factory)

        if validate_layout:
            validate_model_layout(bases)

    # -- activation -------------------------------------------------------

    @contextmanager
    def tenant_context(self, tenant_or_key: Any) -> Iterator[Binding]:
        """Activate a tenant, by object or by key.

        Accepting a bare key is what makes this the method a background worker
        calls: it carries a key across the process boundary and the registry
        turns it back into a tenant here.

        .. code-block:: python

            with manager.tenant_context("acme"):
                ...
        """
        tenant = self._coerce(tenant_or_key)
        self._assert_servable(tenant)
        with tenant_context(tenant) as binding:
            yield binding

    @contextmanager
    def public_schema(self) -> Iterator[Binding]:
        """Operate on shared tables, with no tenant active."""
        with public_schema(self.shared_schema) as binding:
            yield binding

    def restore(self, key: Any) -> Any:
        """Re-enter the context a :func:`~flask_tenants.capture` call recorded.

        The far half of cross-process propagation (decision Q17). ``None``
        restores the public context, so a task enqueued outside any tenant
        behaves the same on the worker.
        """
        if key is None:
            return self.public_schema()
        return self.tenant_context(key)

    # -- resolution -------------------------------------------------------

    def resolve(self, request: Any) -> TenantProtocol | None:
        """Run the resolver chain, returning a servable tenant or ``None``.

        ``None`` means "this request belongs to no tenant" -- a health check, a
        marketing page, the signup flow -- which runs in the shared schema.
        Guard the routes that must have one with ``@tenant_required``.

        :raises UnknownTenantError: a resolver produced a key with no tenant.
        :raises TenantNotReadyError: the tenant exists but is not servable.
        """
        key = self.resolvers.resolve(request)
        if key is None:
            return None

        tenant = self.registry.get(key)
        if tenant is None:
            raise UnknownTenantError(key)

        self._assert_servable(tenant)

        if self.membership_check is not None and not self.membership_check(tenant, request):
            raise TenantNotReadyError(tenant.tenant_key, "membership denied")

        return tenant

    # -- helpers ----------------------------------------------------------

    def _coerce(self, tenant_or_key: Any) -> TenantProtocol:
        if hasattr(tenant_or_key, "schema_name") and hasattr(tenant_or_key, "tenant_key"):
            return tenant_or_key
        return self.registry.require(tenant_or_key)

    def _assert_servable(self, tenant: TenantProtocol) -> None:
        """Only ACTIVE tenants are served.

        This is what stops a half-provisioned schema from answering a request,
        and it is why provisioning can safely run anywhere (decision Q11).
        """
        state = tenant.state
        if isinstance(state, str) and not isinstance(state, TenantState):
            state = TenantState(state)
        if not state.is_servable:
            raise TenantNotReadyError(tenant.tenant_key, state.value)

    def session(self) -> "Session":
        """A new session from the configured factory."""
        return self.session_factory()

    @contextmanager
    def connection(self, tenant_or_key: Any = None) -> Iterator[Any]:
        """A Core connection bound to a tenant, for work outside a Session."""
        if tenant_or_key is None:
            binding = current_binding()
            with self.binder.connection(self.engine, binding) as conn:
                yield conn
            return
        tenant = self._coerce(tenant_or_key)
        with tenant_context(tenant) as binding, self.binder.connection(self.engine, binding) as conn:
            yield conn

    def __repr__(self) -> str:  # pragma: no cover
        return f"<TenantManager shared={self.shared_schema!r} resolvers={self.resolvers!r}>"


__all__ = ["MembershipCheck", "TenantManager"]
