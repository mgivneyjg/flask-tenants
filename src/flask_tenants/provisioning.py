"""Creating a tenant's schema (decision Q11).

A new tenant's schema is built by **replaying the migration history**, not by
``create_all()``.

``create_all()`` is faster and constant-time, and it is a trap. It builds from
the current model definitions, which is not the same thing as replaying the
migrations: anything a migration did outside SQLAlchemy metadata is silently
absent -- ``op.execute`` raw SQL, partial and functional indexes, triggers,
check constraints added by hand, seed data, backfilled defaults. Tenants
provisioned before and after such a migration end up structurally different,
and nothing surfaces it until one customer's query behaves oddly months later.
Replay is linear in migration count; that is a bounded, visible cost.

This module ships no task queue. :func:`provision_tenant` is idempotent and
callable from anywhere -- inline, Celery, RQ, a CLI command, a script -- and
drives the tenant's lifecycle state so a half-built schema can never serve a
request. Where it runs is an application decision.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from .errors import ProvisioningError
from .models import TenantProtocol, TenantState
from .schema import quote_identifier, validate_tenant_schema_name

if TYPE_CHECKING:  # pragma: no cover
    from .manager import TenantManager
    from .migrations import MigrationRunner

log = logging.getLogger("flask_tenants.provisioning")


@dataclass(frozen=True, slots=True)
class ProvisionResult:
    """What :func:`provision_tenant` did."""

    tenant_key: Any
    schema_name: str
    created: bool
    """False when the schema already existed -- the idempotent path."""
    revision: str | None = None
    """Alembic revision the schema now sits at, when known."""


def schema_exists(connection: Any, schema_name: str) -> bool:
    """Is this schema present?"""
    validate_tenant_schema_name(schema_name)
    result = connection.execute(
        text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :name"),
        {"name": schema_name},
    ).first()
    return result is not None


def create_schema(connection: Any, schema_name: str) -> bool:
    """Create a tenant schema if absent. Returns True if it was created.

    The name is validated as a *tenant* schema name, which rejects ``public``
    and the ``pg_`` family outright -- so a mis-derived name cannot point DDL
    at the shared schema.
    """
    validate_tenant_schema_name(schema_name)
    if schema_exists(connection, schema_name):
        return False
    connection.execute(text(f"CREATE SCHEMA {quote_identifier(schema_name)}"))
    log.info("Created schema %s", schema_name)
    return True


def provision_tenant(
    manager: TenantManager,
    tenant: TenantProtocol,
    *,
    runner: MigrationRunner | None = None,
    on_state_change: Callable[[TenantProtocol, TenantState], None] | None = None,
) -> ProvisionResult:
    """Build a tenant's schema and bring it to the latest revision.

    Idempotent: safe to call again after a failure, and safe for a retried
    background job. An existing schema is migrated rather than recreated.

    The tenant moves ``PENDING -> ACTIVE`` on success and ``PENDING -> FAILED``
    on error. Persisting that transition is the application's job -- pass
    ``on_state_change`` to hook your own ``session.commit()`` in, since this
    package does not own your tenant table.

    :param runner: Migration runner. Without one the schema is created empty,
        which is only useful if you intend to migrate it separately.
    :raises ProvisioningError: wrapping whatever went wrong.
    """
    schema_name = tenant.schema_name
    if not schema_name:
        raise ProvisioningError(
            f"Tenant {tenant.tenant_key!r} has no schema_name. It derives from the "
            "primary key, so assign it once the row has been flushed -- call "
            "tenant.assign_schema_name(), or install autoassign_schema_names(Session) "
            "at startup."
        )
    validate_tenant_schema_name(schema_name)

    def _set_state(state: TenantState) -> None:
        try:
            tenant.state = state  # type: ignore[misc]
        except AttributeError:  # frozen or read-only tenant objects
            return
        if on_state_change is not None:
            on_state_change(tenant, state)
        else:
            # Registry lookups return detached objects, so without this the
            # transition would be set in memory and silently never written.
            manager.registry.save(tenant)

    try:
        with manager.engine.begin() as conn:
            created = create_schema(conn, schema_name)

        revision = None
        if runner is not None:
            revision = runner.upgrade_tenant(tenant)

        _set_state(TenantState.ACTIVE)
        log.info("Provisioned tenant %s (schema %s)", tenant.tenant_key, schema_name)
        return ProvisionResult(
            tenant_key=tenant.tenant_key,
            schema_name=schema_name,
            created=created,
            revision=revision,
        )

    except Exception as exc:
        _set_state(TenantState.FAILED)
        log.exception("Provisioning failed for tenant %s", tenant.tenant_key)
        raise ProvisioningError(
            f"Provisioning tenant {tenant.tenant_key!r} (schema {schema_name!r}) failed: {exc}"
        ) from exc


def deactivate_tenant(
    tenant: TenantProtocol,
    *,
    registry: Any = None,
    on_state_change: Callable[[TenantProtocol, TenantState], None] | None = None,
) -> None:
    """Soft-delete a tenant (decision Q12).

    Flips lifecycle state to ``INACTIVE``. The resolver stops serving it; the
    schema and every row in it are left untouched.

    **This package never drops a schema.** There is no ``purge_tenant``, no
    guarded CLI command, no ``--force``. A healthcare contract ending usually
    means a retention obligation measured in years, and keeping the most
    destructive statement this code could emit out of it entirely is a
    deliberate choice.

    The consequences are real and belong in your runbook: schemas accumulate
    indefinitely, and at a few thousand dead ones you will see it in
    ``pg_dump`` duration, autovacuum scheduling and catalog scans. Archival and
    purge are a human operation performed outside this library.
    """
    try:
        tenant.state = TenantState.INACTIVE  # type: ignore[misc]
    except AttributeError:
        return
    if on_state_change is not None:
        on_state_change(tenant, TenantState.INACTIVE)
    elif registry is not None:
        registry.save(tenant)
    else:
        raise ProvisioningError(
            "deactivate_tenant() needs somewhere to write the state change: pass "
            "registry=manager.registry, or an on_state_change callback. Without "
            "one the tenant would be marked inactive in memory only."
        )
    log.info("Deactivated tenant %s (schema %s retained)", tenant.tenant_key, tenant.schema_name)


__all__ = [
    "ProvisionResult",
    "create_schema",
    "deactivate_tenant",
    "provision_tenant",
    "schema_exists",
]
