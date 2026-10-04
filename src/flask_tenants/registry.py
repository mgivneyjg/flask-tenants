"""Looking tenants up by key or hostname (decision Q8).

The application owns the tenant table, so this package needs an indirection to
read it. :class:`TenantRegistry` is that indirection -- implement it against
whatever holds your tenants.

:class:`SQLAlchemyRegistry` is the implementation almost everyone wants: a
model using :class:`~flask_tenants.models.TenantMixin` in the shared schema.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from .errors import UnknownTenantError
from .models import TenantProtocol, TenantState

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session, sessionmaker


class TenantRegistry(ABC):
    """Where tenants are looked up.

    Implementations must be safe to call with no tenant active -- the registry
    lives in the shared schema and reading it must never require one.
    """

    @abstractmethod
    def get(self, key: Any) -> TenantProtocol | None:
        """Return the tenant with this key, or ``None``."""

    @abstractmethod
    def get_by_domain(self, hostname: str) -> TenantProtocol | None:
        """Return the tenant serving this hostname, or ``None``."""

    @abstractmethod
    def all(self, *, states: Sequence[TenantState] | None = None) -> list[TenantProtocol]:
        """Every tenant, optionally filtered by lifecycle state.

        Ordering must be stable, so that a migration run interrupted partway
        resumes over the same sequence.
        """

    def require(self, key: Any) -> TenantProtocol:
        """Return the tenant with this key, raising if absent."""
        tenant = self.get(key)
        if tenant is None:
            raise UnknownTenantError(key)
        return tenant

    def active(self) -> list[TenantProtocol]:
        """Every tenant currently servable."""
        return self.all(states=[TenantState.ACTIVE])

    def save(self, tenant: TenantProtocol) -> bool:
        """Persist changes to a tenant row. Returns True if anything was written.

        Lifecycle transitions are driven by this package but stored in *your*
        table, so provisioning and deactivation need a way to write them back.
        Registries that cannot persist return ``False`` and the caller is
        expected to handle it.
        """
        return False


class StaticRegistry(TenantRegistry):
    """An in-memory registry.

    For tests, single-tenant deployments, and applications whose tenant list is
    configuration rather than data.
    """

    def __init__(
        self, tenants: Iterable[TenantProtocol] = (), domains: dict[str, Any] | None = None
    ):
        self._tenants: dict[Any, TenantProtocol] = {t.tenant_key: t for t in tenants}
        self._domains: dict[str, Any] = dict(domains or {})

    def add(self, tenant: TenantProtocol, *domains: str) -> TenantProtocol:
        self._tenants[tenant.tenant_key] = tenant
        for domain in domains:
            self._domains[domain.lower()] = tenant.tenant_key
        return tenant

    def get(self, key: Any) -> TenantProtocol | None:
        return self._tenants.get(key)

    def get_by_domain(self, hostname: str) -> TenantProtocol | None:
        key = self._domains.get(hostname.lower())
        return self._tenants.get(key) if key is not None else None

    def save(self, tenant: TenantProtocol) -> bool:
        self._tenants[tenant.tenant_key] = tenant
        return True

    def all(self, *, states: Sequence[TenantState] | None = None) -> list[TenantProtocol]:
        tenants = sorted(self._tenants.values(), key=lambda t: str(t.tenant_key))
        if states is None:
            return tenants
        wanted = set(states)
        return [t for t in tenants if t.state in wanted]


class SQLAlchemyRegistry(TenantRegistry):
    """Reads tenants from a model in the shared schema.

    :param session_factory: A ``sessionmaker`` (or any zero-argument callable
        returning a ``Session``). A fresh short-lived session is used per
        lookup so registry reads never entangle with the caller's transaction
        -- which matters because the caller's transaction may be bound to a
        tenant, and the registry is shared.
    :param tenant_model: Your tenant model.
    :param domain_model: Your domain model, if you route on hostname.
    :param key_column: Column holding the routing key. Defaults to the primary key.
    """

    def __init__(
        self,
        session_factory: sessionmaker | Any,
        tenant_model: type,
        *,
        domain_model: type | None = None,
        key_column: Any = None,
    ) -> None:
        self.session_factory = session_factory
        self.tenant_model = tenant_model
        self.domain_model = domain_model
        self.key_column = key_column

    def _key_column(self) -> Any:
        """Which column holds the routing key.

        Derived from the model's ``__tenant_key__`` when present, so the
        registry and :attr:`TenantMixin.tenant_key` cannot disagree. A
        mismatch would surface as a type error on the very first request --
        ``operator does not exist: integer = character varying`` -- rather
        than as anything resembling a configuration problem.
        """
        if self.key_column is not None:
            return self.key_column
        attr = getattr(self.tenant_model, "__tenant_key__", None)
        if attr:
            column = getattr(self.tenant_model, attr, None)
            if column is not None:
                return column
        return next(iter(self.tenant_model.__table__.primary_key.columns))

    def _join_column(self) -> Any:
        """The column a domain row's ``tenant_id`` points at: the primary key."""
        return next(iter(self.tenant_model.__table__.primary_key.columns))

    def get(self, key: Any) -> TenantProtocol | None:
        with self._session() as session:
            stmt = select(self.tenant_model).where(self._key_column() == key)
            tenant = session.execute(stmt).scalar_one_or_none()
            if tenant is not None:
                session.expunge(tenant)
            return tenant

    def get_by_domain(self, hostname: str) -> TenantProtocol | None:
        if self.domain_model is None:
            return None
        with self._session() as session:
            stmt = (
                select(self.tenant_model)
                .join(
                    self.domain_model,
                    self.domain_model.tenant_id == self._join_column(),
                )
                .where(self.domain_model.domain == hostname.lower())
            )
            tenant = session.execute(stmt).scalar_one_or_none()
            if tenant is not None:
                session.expunge(tenant)
            return tenant

    def all(self, *, states: Sequence[TenantState] | None = None) -> list[TenantProtocol]:
        with self._session() as session:
            stmt = select(self.tenant_model).order_by(self._key_column())
            if states is not None:
                stmt = stmt.where(self.tenant_model.state.in_(list(states)))
            tenants = list(session.execute(stmt).scalars())
            for tenant in tenants:
                session.expunge(tenant)
            return tenants

    def save(self, tenant: TenantProtocol) -> bool:
        """Merge the (usually detached) tenant back and commit.

        Lookups expunge their results so a registry read never entangles with
        the caller's transaction -- which matters, because the caller's
        transaction may be bound to a tenant while the registry is shared.
        The cost is that mutating a returned tenant does nothing until it is
        merged back here.
        """
        with self._session() as session:
            session.merge(tenant)
            session.commit()
        return True

    def _session(self) -> Session:
        return self.session_factory()


__all__ = ["SQLAlchemyRegistry", "StaticRegistry", "TenantRegistry"]
