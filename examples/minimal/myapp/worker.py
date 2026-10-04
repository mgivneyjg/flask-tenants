"""Celery worker, showing both propagation styles side by side.

The tenant does NOT cross a process boundary on its own -- see docs/workers.md.
"""

from __future__ import annotations

from celery import Celery
from celery.signals import before_task_publish, task_postrun, task_prerun
from sqlalchemy import func, select

from flask_tenants import celery_signal_handlers, current_tenant_key, with_tenant

from myapp.models import Patient
from myapp.tenancy import Session, manager

celery = Celery("myapp", broker="redis://localhost:6379/0", backend="redis://localhost:6379/0")

# --- Option 2: header propagation (the safety net) -----------------------
# Stamps the active tenant onto every published message and re-enters it on
# the worker. Returned rather than registered, so flask-tenants never imports
# Celery.
_handlers = celery_signal_handlers(manager)
before_task_publish.connect(_handlers["before_task_publish"])
task_prerun.connect(_handlers["task_prerun"])
task_postrun.connect(_handlers["task_postrun"])


# --- Option 1: explicit argument (preferred for new code) ----------------


@celery.task
@with_tenant(manager)
def count_patients_explicit(tenant_key: str) -> dict:
    """The tenant is part of the signature, so the dependency is visible."""
    with Session() as session:
        total = session.execute(select(func.count()).select_from(Patient)).scalar_one()
    return {"tenant": str(current_tenant_key()), "patients": total}


@celery.task
def count_patients_implicit() -> dict:
    """No tenant argument -- the signal handlers carried it in the headers."""
    with Session() as session:
        total = session.execute(select(func.count()).select_from(Patient)).scalar_one()
    return {"tenant": str(current_tenant_key()), "patients": total}


@celery.task
def nightly_rollup_all() -> int:
    """Beat calls this; it fans out one task per tenant.

    One task per tenant rather than one task looping over all of them: a
    failure then affects one customer instead of aborting the run, and retries
    are per-tenant.
    """
    tenants = manager.registry.active()
    for tenant in tenants:
        count_patients_explicit.delay(tenant_key=tenant.tenant_key)
    return len(tenants)
