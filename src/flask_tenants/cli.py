"""Command line interface.

Install the ``cli`` extra and point ``--manager`` at an import path resolving
to a :class:`~flask_tenants.manager.TenantManager` (or a zero-argument factory
returning one)::

    flask-tenants --manager myapp.tenancy:manager status

Set ``FLASK_TENANTS_MANAGER`` to avoid repeating it.

There is no ``purge`` command, and no ``--force`` that would add one. Decision
Q12 keeps ``DROP SCHEMA`` out of this package entirely; ``deactivate`` is a
soft delete that leaves every row in place.
"""

from __future__ import annotations

import importlib
import os
import sys
from typing import Any

try:
    import click
except ImportError as exc:  # pragma: no cover
    raise SystemExit("The flask-tenants CLI needs the 'cli' extra: pip install flask-tenants[cli]") from exc

from .migrations import MigrationRunner
from .models import TenantState
from .operations import for_each_tenant
from .provisioning import deactivate_tenant, provision_tenant


def _import(path: str) -> Any:
    """Resolve ``module:attribute`` to the object itself."""
    if ":" not in path:
        raise click.BadParameter(f"Expected 'module:attribute', got {path!r}")
    module_name, attr = path.split(":", 1)
    sys.path.insert(0, os.getcwd())
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise click.ClickException(f"Could not import {module_name!r}: {exc}") from exc
    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise click.ClickException(f"{module_name!r} has no attribute {attr!r}") from exc


def _load_manager(path: str) -> Any:
    """Resolve a manager, calling it if it turns out to be a factory."""
    obj = _import(path)
    if hasattr(obj, "registry"):
        return obj
    if callable(obj):
        return obj()
    raise click.ClickException(f"{path!r} is not a TenantManager or a factory for one")


@click.group()
@click.option(
    "--manager",
    envvar="FLASK_TENANTS_MANAGER",
    required=True,
    help="Import path to your TenantManager, as module:attribute.",
)
@click.pass_context
def cli(ctx: click.Context, manager: str) -> None:
    """Manage tenant schemas and migrations."""
    ctx.ensure_object(dict)
    ctx.obj["manager"] = _load_manager(manager)


def _runner(ctx: click.Context) -> MigrationRunner:
    mgr = ctx.obj["manager"]
    runner = getattr(mgr, "migration_runner", None)
    if runner is None:
        raise click.ClickException(
            "No migration runner is attached. Set `manager.migration_runner = "
            "MigrationRunner(manager, shared_config=..., tenant_config=...)`."
        )
    return runner


# -- inspection -----------------------------------------------------------


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show every tenant's revision against head.

    Drift is possible by design (decision Q10), so it has to be observable --
    otherwise "are we migrated?" is a question nobody can answer.
    """
    rows = _runner(ctx).status()
    if not rows:
        click.echo("No tenants registered.")
        return

    head = rows[0].head or "-"
    click.echo(f"head: {head}\n")
    width = max(len(str(r.tenant_key)) for r in rows)
    behind = 0
    for row in rows:
        marker = {"current": "  ok", "behind": "BEHIND", "unmigrated": " NONE"}[row.state]
        if row.state != "current":
            behind += 1
        click.echo(f"{marker}  {str(row.tenant_key):<{width}}  {row.schema_name}  {row.current or '-'}")

    click.echo()
    click.echo(f"{len(rows)} tenants, {behind} not at head")
    if behind:
        ctx.exit(1)


@cli.command("list")
@click.option("--state", type=click.Choice([s.value for s in TenantState]), default=None)
@click.pass_context
def list_tenants(ctx: click.Context, state: str | None) -> None:
    """List registered tenants."""
    mgr = ctx.obj["manager"]
    states = [TenantState(state)] if state else None
    tenants = mgr.registry.all(states=states)
    for tenant in tenants:
        value = tenant.state.value if hasattr(tenant.state, "value") else tenant.state
        click.echo(f"{tenant.tenant_key}\t{tenant.schema_name}\t{value}")
    click.echo(f"\n{len(tenants)} tenants", err=True)


# -- migrations -----------------------------------------------------------


@cli.command()
@click.option("--shared", is_flag=True, help="Upgrade the shared schema.")
@click.option("--all", "all_tenants", is_flag=True, help="Upgrade every tenant.")
@click.option("--tenant", "tenant_key", default=None, help="Upgrade one tenant.")
@click.option("--revision", default="head", show_default=True)
@click.option(
    "--continue-on-error",
    is_flag=True,
    help="Keep going past a failing tenant. For the 3am case where one tenant "
    "has bad data and the rest need to ship tonight.",
)
@click.pass_context
def upgrade(
    ctx: click.Context,
    shared: bool,
    all_tenants: bool,
    tenant_key: str | None,
    revision: str,
    continue_on_error: bool,
) -> None:
    """Run migrations.

    One transaction per tenant, stopping at the first failure by default:
    tenant #212 failing usually means a class of problem that will also hit
    #213.
    """
    mgr = ctx.obj["manager"]
    runner = _runner(ctx)

    if shared:
        runner.upgrade_shared(revision)
        click.echo(f"Shared schema upgraded to {revision}.")

    if tenant_key:
        tenant = mgr.registry.require(tenant_key)
        at = runner.upgrade_tenant(tenant, revision)
        click.echo(f"Tenant {tenant_key} upgraded to {at}.")
        return

    if all_tenants:
        run = runner.upgrade_all(revision=revision, continue_on_error=continue_on_error)
        for result in run.failed:
            click.echo(f"FAILED {result.tenant_key}: {result.error}", err=True)
        if run.skipped:
            click.echo(f"{len(run.skipped)} tenants not attempted after the failure.", err=True)
        click.echo(run.summary())
        if not run.all_ok:
            ctx.exit(1)


# -- lifecycle ------------------------------------------------------------


@cli.command()
@click.argument("tenant_key")
@click.pass_context
def provision(ctx: click.Context, tenant_key: str) -> None:
    """Create a tenant's schema and migrate it to head.

    Idempotent -- safe to re-run after a failure.
    """
    mgr = ctx.obj["manager"]
    tenant = mgr.registry.require(tenant_key)
    result = provision_tenant(mgr, tenant, runner=getattr(mgr, "migration_runner", None))
    verb = "Created and migrated" if result.created else "Migrated existing"
    click.echo(f"{verb} schema {result.schema_name} (revision {result.revision or '-'}).")


@cli.command()
@click.argument("tenant_key")
@click.pass_context
def deactivate(ctx: click.Context, tenant_key: str) -> None:
    """Soft-delete a tenant. The schema and all its data are retained.

    This package never drops a schema (decision Q12). Archival and purge are a
    human operation with a runbook, performed outside this tool.
    """
    mgr = ctx.obj["manager"]
    tenant = mgr.registry.require(tenant_key)
    deactivate_tenant(tenant, registry=mgr.registry)
    click.echo(f"Tenant {tenant_key} is now inactive. Schema {tenant.schema_name} retained.")


@cli.command("run")
@click.argument("callable_path")
@click.option("--workers", default=1, show_default=True, help="Concurrent tenants.")
@click.option("--stop-on-error", is_flag=True)
@click.pass_context
def run_for_each(ctx: click.Context, callable_path: str, workers: int, stop_on_error: bool) -> None:
    """Run ``module:function`` once per active tenant.

    A maintenance primitive -- backfills, re-indexing, applying a fix
    everywhere. Not a reporting tool; see the docs on analytics.
    """
    mgr = ctx.obj["manager"]
    fn = _import(callable_path)   # imported, never called here
    if not callable(fn):
        raise click.ClickException(f"{callable_path!r} is not callable")
    result = for_each_tenant(
        mgr, fn, continue_on_error=not stop_on_error, max_workers=workers
    )
    for outcome in result.failed:
        click.echo(f"FAILED {outcome.tenant_key}: {outcome.error}", err=True)
    click.echo(result.summary())
    if not result.all_ok:
        ctx.exit(1)


def main() -> None:  # pragma: no cover
    cli(obj={})


if __name__ == "__main__":  # pragma: no cover
    main()
