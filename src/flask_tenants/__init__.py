"""Schema-per-tenant multi-tenancy for Flask and SQLAlchemy, on PostgreSQL."""

from .binding import SchemaBinder, no_tenant_attempt_count, warn_if_untracked
from .context import (
    Binding,
    bind,
    capture,
    current_binding,
    current_schema,
    current_tenant,
    current_tenant_key,
    public_schema,
    require_binding,
    tenant_context,
)
from .errors import (
    InvalidSchemaNameError,
    ModelLayoutError,
    NoActiveTenantError,
    ProvisioningError,
    TenantError,
    TenantLeakError,
    TenantNotReadyError,
    UnknownTenantError,
)
from .manager import MembershipCheck, TenantManager
from .migrations import MigrationRun, MigrationRunner, TenantStatus, strip_tenant_schema
from .models import (
    Bases,
    DomainMixin,
    SharedBase,
    SimpleTenant,
    TenantBase,
    TenantMixin,
    TenantProtocol,
    TenantState,
    autoassign_schema_names,
    make_bases,
    validate_model_layout,
)
from .observability import TenantLogFilter, counters
from .operations import BulkResult, TenantOutcome, for_each_tenant
from .propagation import (
    TENANT_HEADER,
    capture_headers,
    celery_signal_handlers,
    restore_into,
    with_tenant,
)
from .provisioning import ProvisionResult, create_schema, deactivate_tenant, provision_tenant
from .registry import SQLAlchemyRegistry, StaticRegistry, TenantRegistry
from .resolvers import (
    CallableResolver,
    DomainResolver,
    HeaderResolver,
    PathPrefixResolver,
    ResolverChain,
    SessionResolver,
    SubdomainResolver,
    TenantResolver,
)
from .schema import (
    derive_schema_name,
    quote_identifier,
    validate_schema_name,
    validate_tenant_schema_name,
)

__version__ = "0.1.0"

__all__ = [
    "TENANT_HEADER",
    "Bases",
    "Binding",
    "BulkResult",
    "CallableResolver",
    "DomainMixin",
    "DomainResolver",
    "HeaderResolver",
    "InvalidSchemaNameError",
    "MembershipCheck",
    "MigrationRun",
    "MigrationRunner",
    "ModelLayoutError",
    "NoActiveTenantError",
    "PathPrefixResolver",
    "ProvisionResult",
    "ProvisioningError",
    "ResolverChain",
    "SQLAlchemyRegistry",
    "SchemaBinder",
    "SessionResolver",
    "SharedBase",
    "SimpleTenant",
    "StaticRegistry",
    "SubdomainResolver",
    "TenantBase",
    "TenantError",
    "TenantLeakError",
    "TenantLogFilter",
    "TenantManager",
    "TenantMixin",
    "TenantNotReadyError",
    "TenantOutcome",
    "TenantProtocol",
    "TenantRegistry",
    "TenantResolver",
    "TenantState",
    "TenantStatus",
    "UnknownTenantError",
    "__version__",
    "autoassign_schema_names",
    "bind",
    "capture",
    "capture_headers",
    "celery_signal_handlers",
    "counters",
    "create_schema",
    "current_binding",
    "current_schema",
    "current_tenant",
    "current_tenant_key",
    "deactivate_tenant",
    "derive_schema_name",
    "for_each_tenant",
    "make_bases",
    "no_tenant_attempt_count",
    "provision_tenant",
    "public_schema",
    "quote_identifier",
    "require_binding",
    "restore_into",
    "strip_tenant_schema",
    "tenant_context",
    "validate_model_layout",
    "validate_schema_name",
    "validate_tenant_schema_name",
    "warn_if_untracked",
    "with_tenant",
]


def __getattr__(name: str):
    """Expose the Flask layer lazily, so importing this package never needs Flask."""
    if name in {"FlaskTenants", "tenant_required", "current_tenant_or_404"}:
        from . import flask_ext

        return getattr(flask_ext, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
