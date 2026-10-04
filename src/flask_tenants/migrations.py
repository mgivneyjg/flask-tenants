"""Running Alembic across the shared schema and every tenant (decision Q10).

Two separate Alembic environments, not branch labels::

    migrations/
      shared/    env.py, versions/   -> target_metadata = bases.shared_metadata
      tenant/    env.py, versions/   -> target_metadata = bases.tenant_metadata

Each ``env.py`` points at exactly one ``MetaData``, so ``--autogenerate`` just
works with no filtering. Branch labels are technically the right model for two
histories in one graph, and are the part of Alembic people get wrong most
often -- exporting that confusion to every consumer is not worth the saved
directory.

Tenants may legitimately sit at different revisions. One provisioned this
morning starts at head; one suspended through the last three releases does not.
So ``alembic_version`` lives **inside each tenant's own schema**, which also
makes a half-finished run resumable: re-running it is a no-op for the tenants
that already completed.

Writing tenant migrations
-------------------------

**Tenant migrations must not pass a ``schema=`` argument.** Write unqualified
DDL and let the ``search_path`` place it.

This is not a style preference. ``schema_translate_map`` is a SQLAlchemy
mechanism, and Alembic does not route every operation through it: ``op.add_column``,
``op.drop_column`` and the other ``ALTER TABLE`` operations format the schema
name into the statement themselves, so the symbolic ``tenant`` token survives
into the SQL and PostgreSQL rejects it with ``schema "tenant" does not exist``.
``op.create_table`` happens to work, which makes the failure arrive later and
look stranger than it is.

So the tenant ``env.py`` sets ``SET LOCAL search_path`` to the schema being
migrated, and migrations stay schema-agnostic -- which has the pleasant side
effect of making ``op.execute`` raw SQL work identically to everything else.
:func:`strip_tenant_schema` keeps ``--autogenerate`` from reintroducing the
argument.

Failure semantics: one transaction per tenant, stopping at the first failure.
Tenant #212 failing usually means a class of problem that will also hit #213,
and finding out after one failure beats finding out after 189. Pass
``continue_on_error=True`` for the 3am case where one tenant has bad data and
the other 399 need to ship tonight.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text

from .errors import ProvisioningError
from .models import TenantProtocol
from .schema import quote_identifier, validate_tenant_schema_name

if TYPE_CHECKING:  # pragma: no cover
    from .manager import TenantManager

log = logging.getLogger("flask_tenants.migrations")

#: Version table name used in both the shared and tenant environments.
VERSION_TABLE = "alembic_version"


@dataclass(slots=True)
class TenantMigrationResult:
    """Outcome for a single tenant in a migration run."""

    tenant_key: Any
    schema_name: str
    ok: bool
    revision: str | None = None
    error: str | None = None
    skipped: bool = False


@dataclass(slots=True)
class MigrationRun:
    """Outcome of a whole run."""

    results: list[TenantMigrationResult] = field(default_factory=list)
    stopped_early: bool = False

    @property
    def succeeded(self) -> list[TenantMigrationResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed(self) -> list[TenantMigrationResult]:
        return [r for r in self.results if not r.ok and not r.skipped]

    @property
    def skipped(self) -> list[TenantMigrationResult]:
        return [r for r in self.results if r.skipped]

    @property
    def all_ok(self) -> bool:
        return not self.failed and not self.stopped_early

    def summary(self) -> str:
        parts = [f"{len(self.succeeded)} upgraded"]
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        if self.skipped:
            parts.append(f"{len(self.skipped)} not attempted")
        return ", ".join(parts)


@dataclass(frozen=True, slots=True)
class TenantStatus:
    """One row of the ``status`` report."""

    tenant_key: Any
    schema_name: str
    current: str | None
    head: str | None

    @property
    def is_current(self) -> bool:
        return self.current == self.head

    @property
    def state(self) -> str:
        if self.current is None:
            return "unmigrated"
        return "current" if self.is_current else "behind"


class MigrationRunner:
    """Drives Alembic over the shared schema and each tenant schema.

    :param manager: The tenant manager, for the engine and registry.
    :param shared_config: Path to the shared environment's ``alembic.ini``, or
        a prepared :class:`~alembic.config.Config`.
    :param tenant_config: Same, for the tenant environment.
    """

    def __init__(
        self,
        manager: TenantManager,
        *,
        shared_config: str | Config | None = None,
        tenant_config: str | Config | None = None,
    ) -> None:
        self.manager = manager
        self.shared_config = _as_config(shared_config)
        self.tenant_config = _as_config(tenant_config)

    # -- shared schema ----------------------------------------------------

    def upgrade_shared(self, revision: str = "head") -> None:
        """Upgrade the shared schema."""
        if self.shared_config is None:
            raise ProvisioningError("No shared Alembic config was configured")
        cfg = self.shared_config
        cfg.attributes["connectable"] = self.manager.engine
        cfg.attributes["flask_tenants_manager"] = self.manager
        cfg.attributes["schema"] = self.manager.shared_schema
        command.upgrade(cfg, revision)
        log.info("Shared schema upgraded to %s", revision)

    # -- tenant schemas ---------------------------------------------------

    def upgrade_tenant(self, tenant: TenantProtocol, revision: str = "head") -> str | None:
        """Upgrade one tenant, in its own transaction.

        Returns the revision the schema sits at afterwards.
        """
        if self.tenant_config is None:
            raise ProvisioningError("No tenant Alembic config was configured")

        schema_name = validate_tenant_schema_name(tenant.schema_name)

        cfg = self.tenant_config
        cfg.attributes["connectable"] = self.manager.engine
        cfg.attributes["flask_tenants_manager"] = self.manager
        cfg.attributes["schema"] = schema_name
        cfg.attributes["tenant"] = tenant

        command.upgrade(cfg, revision)
        current = self.current_revision(schema_name)
        log.info("Tenant %s upgraded to %s", tenant.tenant_key, current)
        return current

    def upgrade_all(
        self,
        tenants: Iterable[TenantProtocol] | None = None,
        *,
        revision: str = "head",
        continue_on_error: bool = False,
    ) -> MigrationRun:
        """Upgrade every tenant.

        One transaction per tenant. Stops at the first failure unless
        ``continue_on_error`` is set; tenants not reached are reported as
        skipped rather than silently omitted, because a run that quietly
        covered 211 of 400 reads exactly like one that covered all of them.
        """
        targets = list(tenants) if tenants is not None else list(self.manager.registry.all())
        run = MigrationRun()

        for index, tenant in enumerate(targets):
            try:
                current = self.upgrade_tenant(tenant, revision)
                run.results.append(
                    TenantMigrationResult(
                        tenant_key=tenant.tenant_key,
                        schema_name=tenant.schema_name,
                        ok=True,
                        revision=current,
                    )
                )
            except Exception as exc:
                log.exception("Migration failed for tenant %s", tenant.tenant_key)
                run.results.append(
                    TenantMigrationResult(
                        tenant_key=tenant.tenant_key,
                        schema_name=tenant.schema_name,
                        ok=False,
                        error=str(exc),
                    )
                )
                if not continue_on_error:
                    run.stopped_early = True
                    for remaining in targets[index + 1 :]:
                        run.results.append(
                            TenantMigrationResult(
                                tenant_key=remaining.tenant_key,
                                schema_name=remaining.schema_name,
                                ok=False,
                                skipped=True,
                            )
                        )
                    break

        log.info("Migration run: %s", run.summary())
        return run

    # -- status -----------------------------------------------------------

    def current_revision(self, schema_name: str) -> str | None:
        """Read ``alembic_version`` out of one schema."""
        validate_schema = (
            validate_tenant_schema_name
            if schema_name != self.manager.shared_schema
            else (lambda name: name)
        )
        validate_schema(schema_name)

        with self.manager.engine.connect() as conn:
            exists = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = :schema AND table_name = :table"
                ),
                {"schema": schema_name, "table": VERSION_TABLE},
            ).first()
            if exists is None:
                return None
            conn.exec_driver_sql(f"SET LOCAL search_path TO {quote_identifier(schema_name)}")
            context = MigrationContext.configure(conn, opts={"version_table_schema": schema_name})
            return context.get_current_revision()

    def head_revision(self, *, tenant: bool = True) -> str | None:
        """The latest revision in an environment's history."""
        cfg = self.tenant_config if tenant else self.shared_config
        if cfg is None:
            return None
        script = ScriptDirectory.from_config(cfg)
        return script.get_current_head()

    def status(self, tenants: Sequence[TenantProtocol] | None = None) -> list[TenantStatus]:
        """Every tenant's revision against head.

        Drift is possible by design here, so it has to be observable --
        otherwise "are we migrated?" becomes a question nobody can answer.
        """
        targets = list(tenants) if tenants is not None else list(self.manager.registry.all())
        head = self.head_revision(tenant=True)
        return [
            TenantStatus(
                tenant_key=t.tenant_key,
                schema_name=t.schema_name,
                current=self.current_revision(t.schema_name),
                head=head,
            )
            for t in targets
        ]


def strip_tenant_schema(context: Any, revision: Any, directives: list[Any]) -> None:
    """Remove ``schema=`` from autogenerated tenant operations.

    Wire into the tenant ``env.py``::

        context.configure(..., process_revision_directives=strip_tenant_schema)

    Autogenerate compares against ``tenant_metadata``, whose schema is the
    symbolic ``tenant`` token, so it emits ``schema="tenant"`` on every
    operation. That token is correct for the ORM and wrong for Alembic -- see
    the module docstring. Stripping it at generation time means the reviewer
    sees the migration that will actually run.
    """
    for directive in directives:
        for upgrade_ops in getattr(directive, "upgrade_ops_list", []) or [
            getattr(directive, "upgrade_ops", None)
        ]:
            if upgrade_ops is not None:
                _strip_ops(upgrade_ops)
        downgrade_ops = getattr(directive, "downgrade_ops", None)
        if downgrade_ops is not None:
            _strip_ops(downgrade_ops)


def _strip_ops(container: Any) -> None:
    """Recursively null the ``schema`` attribute on an operation tree."""
    for op in getattr(container, "ops", []):
        if hasattr(op, "schema"):
            op.schema = None
        if hasattr(op, "ops"):
            _strip_ops(op)


def _as_config(value: str | Config | None) -> Config | None:
    if value is None or isinstance(value, Config):
        return value
    return Config(value)


def run_migrations_online(context: Any, target_metadata: Any, config: Any) -> None:
    """Helper for a tenant ``env.py``.

    Reads the connectable, schema and metadata the runner stashed on
    ``config.attributes`` and configures Alembic to work inside one schema:
    ``version_table_schema`` pins ``alembic_version`` into the tenant's own
    schema, and ``schema_translate_map`` rewrites the symbolic ``tenant``
    token the models are declared against.

    See ``flask_tenants/alembic_templates/`` for complete files.
    """
    connectable = config.attributes.get("connectable")
    schema = config.attributes.get("schema")
    if connectable is None or schema is None:
        raise ProvisioningError(
            "env.py was invoked without a connectable and schema. Run migrations "
            "through MigrationRunner or the flask-tenants CLI."
        )

    token = target_metadata.schema or "tenant"

    with connectable.connect() as connection:
        connection = connection.execution_options(schema_translate_map={token: schema})
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table=VERSION_TABLE,
            version_table_schema=schema,
            include_schemas=False,
            compare_type=True,
            process_revision_directives=strip_tenant_schema,
        )
        with context.begin_transaction():
            # This is what actually places the DDL, not the translate map --
            # see "Writing tenant migrations" above.
            connection.exec_driver_sql(f"SET LOCAL search_path TO {quote_identifier(schema)}")
            context.run_migrations()


__all__ = [
    "VERSION_TABLE",
    "MigrationRun",
    "MigrationRunner",
    "TenantMigrationResult",
    "TenantStatus",
    "run_migrations_online",
    "strip_tenant_schema",
]
