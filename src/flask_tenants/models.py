"""Model layer: the tenant contract and the shared/tenant declarative split.

Two decisions live here.

**Q8 -- the application owns the tenant table.** This package defines a
:class:`Protocol` describing the handful of attributes it needs, and ships
:class:`TenantMixin` as the convenient way to satisfy it. Your ``Tenant`` is
your model; add whatever columns you like. A library-owned table would make
every application-specific column somebody else's migration problem.

**Q9 -- one registry, two MetaData.** Alembic autogenerates against a
``MetaData``, so shared and tenant tables must live in separate ones or
per-schema migrations are unworkable. But two independent ``declarative_base()``
calls also create two *registries*, and string-based ``relationship()`` cannot
resolve across registries::

    sqlalchemy.exc.InvalidRequestError: expression 'Organization' failed to
    locate a name

:func:`make_bases` decouples the two concerns: a single ``registry`` with two
``MetaData`` objects hung off two abstract bases. Alembic sees two clean table
sets; ``relationship("Organization")`` resolves across the boundary normally.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol, runtime_checkable

from sqlalchemy import Enum as SAEnum, MetaData, String, inspect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, registry as sa_registry

from .errors import ModelLayoutError
from .schema import TENANT_SCHEMA_TOKEN, derive_schema_name


class TenantState(str, enum.Enum):
    """Lifecycle states a tenant moves through.

    The resolver only serves :attr:`ACTIVE` tenants, which is what stops a
    half-provisioned schema from ever answering a request (decision Q11).

    There is no ``PURGED``. Offboarding is a soft delete and this package never
    drops a schema (decision Q12); archival and purge are an external runbook.
    """

    PENDING = "pending"
    """Registered, schema not yet built. Not servable."""

    ACTIVE = "active"
    """Provisioned and serving."""

    FAILED = "failed"
    """Provisioning failed. Terminal until a human intervenes. Not servable."""

    INACTIVE = "inactive"
    """Soft-deleted or suspended. Data intact, not servable."""

    @property
    def is_servable(self) -> bool:
        return self is TenantState.ACTIVE


@runtime_checkable
class TenantProtocol(Protocol):
    """The entire contract this package needs from your tenant model.

    Anything structurally matching this works -- a SQLAlchemy model, a dataclass
    read from config, an object fronting a remote service.
    """

    @property
    def tenant_key(self) -> Any:
        """A stable identifier. What resolvers return and workers carry."""
        ...

    @property
    def schema_name(self) -> str:
        """The real PostgreSQL schema holding this tenant's tables."""
        ...

    @property
    def state(self) -> TenantState:
        """Current lifecycle state."""
        ...


@dataclass(frozen=True, slots=True)
class SimpleTenant:
    """A tenant with no database behind it.

    Useful for tests, for single-tenant deployments, and for applications whose
    tenant registry lives somewhere other than this database.
    """

    tenant_key: Any
    schema_name: str
    state: TenantState = TenantState.ACTIVE

    @classmethod
    def for_id(cls, tenant_id: Any, *, prefix: str = "tenant_") -> "SimpleTenant":
        return cls(tenant_key=tenant_id, schema_name=derive_schema_name(tenant_id, prefix=prefix))


@dataclass(frozen=True, slots=True)
class Bases:
    """What :func:`make_bases` returns.

    ``shared`` and ``tenant`` are abstract declarative bases sharing one
    registry; ``shared_metadata`` and ``tenant_metadata`` are the two
    ``MetaData`` objects Alembic points at.
    """

    registry: sa_registry
    shared: type[DeclarativeBase]
    tenant: type[DeclarativeBase]
    shared_metadata: MetaData
    tenant_metadata: MetaData


def make_bases(
    registry: sa_registry | None = None,
    *,
    shared_schema: str = "public",
    naming_convention: dict[str, str] | None = None,
) -> Bases:
    """Build the shared and tenant declarative bases.

    This is the real API (decision Q18). ``flask_tenants.SharedBase`` and
    ``flask_tenants.TenantBase`` are a prebuilt pair for applications that do
    not need their own registry.

    Passing your own ``registry`` is what keeps this package a *participant* in
    your model layer rather than its owner -- which matters as soon as a second
    extension, a shared internal package of common tables, or a second app in a
    monorepo wants to join the same registry.

    :param shared_schema: Real schema for shared tables. Almost always
        ``public``; configurable for deployments that keep shared tables
        elsewhere.
    :param naming_convention: Passed to both ``MetaData`` objects. Strongly
        recommended -- Alembic autogenerate produces far better migrations when
        constraints have deterministic names.
    """
    reg = registry if registry is not None else sa_registry()

    convention = naming_convention or DEFAULT_NAMING_CONVENTION
    shared_metadata = MetaData(schema=shared_schema, naming_convention=dict(convention))
    tenant_metadata = MetaData(schema=TENANT_SCHEMA_TOKEN, naming_convention=dict(convention))

    class SharedBase(DeclarativeBase):
        """Base for tables that exist once, in the shared schema."""

        __abstract__ = True
        registry = reg
        metadata = shared_metadata

    class TenantBase(DeclarativeBase):
        """Base for tables cloned into every tenant's schema.

        Declared against the symbolic schema ``tenant``, which
        ``schema_translate_map`` rewrites to the active tenant's real schema at
        execution time. The token is never a real schema.
        """

        __abstract__ = True
        registry = reg
        metadata = tenant_metadata

    return Bases(
        registry=reg,
        shared=SharedBase,
        tenant=TenantBase,
        shared_metadata=shared_metadata,
        tenant_metadata=tenant_metadata,
    )


#: Deterministic constraint names. Without these, Alembic autogenerate emits
#: migrations that cannot drop the constraints they created on some backends.
DEFAULT_NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class TenantMixin:
    """Columns this package needs on your tenant model.

    Mix into a model on your *shared* base -- the tenant registry lives in the
    shared schema, by definition.

    .. code-block:: python

        class Tenant(SharedBase, TenantMixin):
            __tablename__ = "tenants"

            id: Mapped[int] = mapped_column(primary_key=True)
            name: Mapped[str] = mapped_column(String(120))
            billing_plan: Mapped[str] = mapped_column(String(40), default="trial")

    ``schema_name`` is populated by :func:`~flask_tenants.schema.derive_schema_name`
    from the primary key, so it never tracks a mutable slug.
    """

    slug: Mapped[str] = mapped_column(String(63), unique=True, index=True)
    """Human-facing label. Safe to change -- it is not the schema name."""

    schema_name: Mapped[str | None] = mapped_column(
        String(63), unique=True, index=True, nullable=True
    )
    """The real schema. Derived from the surrogate key; treat as immutable.

    Nullable for one unavoidable reason: the name derives from the primary
    key, and the primary key does not exist until the row is inserted. A
    ``NOT NULL`` column could therefore never be satisfied on the first flush.

    It is populated immediately afterwards, either by calling
    :meth:`assign_schema_name` yourself or by installing
    :func:`autoassign_schema_names`. Provisioning refuses a tenant without
    one, and the resolver never serves an unprovisioned tenant, so the window
    where this is ``None`` is a single flush.
    """

    state: Mapped[TenantState] = mapped_column(
        SAEnum(
            TenantState,
            name="tenant_state",
            native_enum=False,
            length=16,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=TenantState.PENDING,
        nullable=False,
        index=True,
    )
    """Lifecycle state. Only ``active`` tenants are served.

    Stored as a ``VARCHAR`` with a check constraint rather than a native
    PostgreSQL enum -- adding a value to a native enum is a migration that
    cannot run inside a transaction, which would conflict with the per-tenant
    transaction model in decision Q10.
    """

    __tenant_key__: ClassVar[str] = "id"
    """Which attribute resolvers return and workers carry.

    Defaults to the primary key. Set it to ``"slug"`` if you route on
    subdomains, because :class:`~flask_tenants.resolvers.SubdomainResolver`
    yields ``acme``, not ``7``::

        class Tenant(SharedBase, TenantMixin):
            __tenant_key__ = "slug"

    One declaration, used by both :attr:`tenant_key` and
    :class:`~flask_tenants.registry.SQLAlchemyRegistry`, so the two cannot
    disagree -- a mismatch otherwise surfaces as
    ``operator does not exist: integer = character varying`` on the first
    request.

    This is the **routing** key, deliberately distinct from the surrogate key
    the schema name derives from. Routing keys may be human-facing and may
    change; schema names must not.
    """

    @property
    def tenant_key(self) -> Any:
        """The routing key named by :attr:`__tenant_key__`."""
        return getattr(self, self.__tenant_key__, None)

    @property
    def surrogate_key(self) -> Any:
        """The primary key. What the schema name derives from."""
        state = inspect(self)
        if state.identity:
            return state.identity[0]
        pk_name = next(iter(type(self).__table__.primary_key.columns)).name
        return getattr(self, pk_name, None)

    def assign_schema_name(self, *, prefix: str = "tenant_") -> str:
        """Derive and store this tenant's schema name from its **primary key**.

        Not from :attr:`tenant_key`, which may be a mutable slug. A rebrand
        should change a label, never require renaming a schema underneath live
        connections.

        Call once the row has an id -- after ``session.flush()``, or let
        :func:`autoassign_schema_names` do it.
        """
        key = self.surrogate_key
        if key is None:
            raise ModelLayoutError(
                "Cannot derive a schema name before the tenant has a primary key. "
                "Flush the session first, or install autoassign_schema_names()."
            )
        self.schema_name = derive_schema_name(key, prefix=prefix)
        return self.schema_name


class DomainMixin:
    """Columns for hostname-based routing (the default resolver, decision Q6).

    A separate table rather than a column on the tenant, because the point of
    the domain table is that one tenant may answer to several hostnames --
    ``acme.app.com``, ``app.acme.com``, and a vanity domain.
    """

    domain: Mapped[str] = mapped_column(String(253), unique=True, index=True)
    """Fully-qualified hostname, lowercase, no port."""

    is_primary: Mapped[bool] = mapped_column(default=False, nullable=False)
    """Which hostname to use when generating absolute URLs for this tenant."""


def validate_model_layout(bases: Bases) -> None:
    """Assert no shared table holds a foreign key into a tenant table.

    PostgreSQL happily enforces cross-schema foreign keys, so
    ``tenant_x.patients.org_id -> public.organizations.id`` is a real,
    useful constraint. The reverse is incoherent: a row in ``public`` cannot
    say *which* tenant's row it references.

    Without this check the mistake surfaces at ``CREATE TABLE`` time while
    provisioning tenant #47, not in development. Call it once at startup --
    :class:`~flask_tenants.manager.TenantManager` does so automatically.

    :raises ModelLayoutError: on the first offending foreign key.
    """
    tenant_tables = {table.name for table in bases.tenant_metadata.tables.values()}

    for table in bases.shared_metadata.tables.values():
        for fk in table.foreign_keys:
            target_schema = fk.column.table.schema
            target_name = fk.column.table.name
            if target_schema == TENANT_SCHEMA_TOKEN or target_name in tenant_tables:
                raise ModelLayoutError(
                    f"Shared table {table.name!r} has a foreign key "
                    f"{fk.parent.name!r} -> {target_name}.{fk.column.name}, "
                    "which points into a tenant-scoped table. A shared row cannot "
                    "reference a tenant row -- there is no way to say which tenant. "
                    "Either move the shared table onto the tenant base, or drop the "
                    "foreign key and carry the tenant key alongside it."
                )


def autoassign_schema_names(session_factory: Any, *, prefix: str = "tenant_") -> None:
    """Populate ``schema_name`` automatically after a tenant is inserted.

    Saves remembering :meth:`TenantMixin.assign_schema_name` at every call
    site. Install once at startup::

        autoassign_schema_names(Session)

    Uses ``after_flush_postexec``, the first point at which the row has its
    primary key, and writes in the same transaction as the insert -- so a
    tenant row never reaches a committed state without a schema name.
    """
    from sqlalchemy import event

    def _assign(session: Any, flush_context: Any) -> None:
        pending = [
            obj
            for obj in session.identity_map.values()
            if isinstance(obj, TenantMixin) and getattr(obj, "schema_name", None) is None
        ]
        for tenant in pending:
            tenant.assign_schema_name(prefix=prefix)

    event.listen(session_factory, "after_flush_postexec", _assign)


# Convenience pair for applications that do not need their own registry.
_default_bases = make_bases()
SharedBase = _default_bases.shared
TenantBase = _default_bases.tenant
default_bases = _default_bases

__all__ = [
    "Bases",
    "DEFAULT_NAMING_CONVENTION",
    "DomainMixin",
    "SharedBase",
    "SimpleTenant",
    "TenantBase",
    "TenantMixin",
    "TenantProtocol",
    "TenantState",
    "default_bases",
    "make_bases",
    "validate_model_layout",
]
