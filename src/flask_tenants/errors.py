"""Exception hierarchy for flask-tenants.

Every error raised by this package derives from :class:`TenantError`, so an
application can catch the whole family in one place.
"""

from __future__ import annotations


class TenantError(Exception):
    """Base class for every error raised by flask-tenants."""


class NoActiveTenantError(TenantError):
    """A tenant-scoped operation ran with no tenant active.

    This is deliberately an error rather than a silent fall back to the shared
    ``public`` schema (decision Q5). A fallback would turn "forgot to set the
    tenant" into wrong data instead of a stack trace.

    Use :func:`flask_tenants.public_schema` when operating on shared tables is
    what you actually mean.
    """


class UnknownTenantError(TenantError):
    """A resolver produced a tenant key that is not in the registry."""

    def __init__(self, key: object) -> None:
        super().__init__(f"No tenant registered with key {key!r}")
        self.key = key


class TenantNotReadyError(TenantError):
    """The tenant exists but its lifecycle state forbids serving requests.

    Raised for tenants that are still provisioning, have failed provisioning, or
    have been soft-deleted.
    """

    def __init__(self, key: object, state: object) -> None:
        super().__init__(f"Tenant {key!r} is not available (state: {state})")
        self.key = key
        self.state = state


class InvalidSchemaNameError(TenantError):
    """A schema name failed validation.

    Schema names are SQL *identifiers* and cannot be passed as bind parameters,
    so they are interpolated into DDL as text. Validation is therefore a
    security control, not a convenience (decision Q8).
    """


class ProvisioningError(TenantError):
    """Creating or migrating a tenant's schema failed."""


class ModelLayoutError(TenantError):
    """The declarative model layout violates a rule this package relies on.

    Currently raised for foreign keys pointing from a shared table into a
    tenant-scoped table, which is incoherent -- there is no way to say *which*
    tenant's row is referenced (decision Q9).
    """


class TenantLeakError(TenantError):
    """A test observed data belonging to a tenant other than the active one.

    Raised only by the testing helpers. If you ever see this outside a test
    suite, something is very wrong.
    """
