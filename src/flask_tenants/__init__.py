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
from .manager import MembershipCheck, TenantManager
from .migrations import MigrationRun, MigrationRunner, TenantStatus, strip_tenant_schema
from .observability import TenantLogFilter, counters
from .operations import BulkResult, TenantOutcome, for_each_tenant
from .propagation import TENANT_HEADER, capture_headers, celery_signal_handlers, restore_into, with_tenant
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
from .models import (
    autoassign_schema_names,
    Bases,
    DomainMixin,
    SharedBase,
    SimpleTenant,
    TenantBase,
    TenantMixin,
    TenantProtocol,
    TenantState,
    make_bases,
    validate_model_layout,
)
from .schema import (
    derive_schema_name,
    quote_identifier,
    validate_schema_name,
    validate_tenant_schema_name,
)

__version__ = "0.1.0"

__all__ = [
    "BulkResult",
    "CallableResolver",
    "DomainResolver",
    "HeaderResolver",
    "MembershipCheck",
    "MigrationRun",
    "MigrationRunner",
    "PathPrefixResolver",
    "ProvisionResult",
    "ResolverChain",
    "SQLAlchemyRegistry",
    "SessionResolver",
    "StaticRegistry",
    "SubdomainResolver",
    "TENANT_HEADER",
    "TenantLogFilter",
    "TenantManager",
    "TenantOutcome",
    "TenantRegistry",
    "TenantResolver",
    "TenantStatus",
    "strip_tenant_schema",
    "capture_headers",
    "celery_signal_handlers",
    "counters",
    "create_schema",
    "deactivate_tenant",
    "for_each_tenant",
    "provision_tenant",
    "restore_into",
    "with_tenant",
    "Bases",
    "Binding",
    "DomainMixin",
    "InvalidSchemaNameError",
    "ModelLayoutError",
    "NoActiveTenantError",
    "ProvisioningError",
    "SchemaBinder",
    "SharedBase",
    "SimpleTenant",
    "TenantBase",
    "TenantError",
    "TenantLeakError",
    "TenantMixin",
    "TenantNotReadyError",
    "TenantProtocol",
    "TenantState",
    "UnknownTenantError",
    "__version__",
    "bind",
    "capture",
    "current_binding",
    "current_schema",
    "current_tenant",
    "current_tenant_key",
    "derive_schema_name",
    "autoassign_schema_names",
    "make_bases",
    "no_tenant_attempt_count",
    "public_schema",
    "quote_identifier",
    "require_binding",
    "tenant_context",
    "validate_model_layout",
    "validate_schema_name",
    "validate_tenant_schema_name",
    "warn_if_untracked",
]


def __getattr__(name: str):
    """Expose the Flask layer lazily, so importing this package never needs Flask."""
    if name in {"FlaskTenants", "tenant_required", "current_tenant_or_404"}:
        from . import flask_ext

        return getattr(flask_ext, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
