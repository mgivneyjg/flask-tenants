"""Maintenance callables, for `flask-tenants run`.

    flask-tenants run myapp.maintenance:relabel --workers 2
"""

from __future__ import annotations

from sqlalchemy import func, select

from myapp.models import Patient
from myapp.tenancy import Session


def count(tenant) -> int:
    """Report how many patients each tenant has."""
    with Session() as session:
        return session.execute(select(func.count()).select_from(Patient)).scalar_one()


def relabel(tenant) -> int:
    """A backfill. Commits per batch so an interrupted run resumes cleanly."""
    changed = 0
    with Session() as session:
        while True:
            rows = session.execute(
                select(Patient).where(~Patient.label.startswith("[")).limit(500)
            ).scalars().all()
            if not rows:
                break
            for row in rows:
                row.label = f"[{tenant.slug}] {row.label}"
                changed += 1
            session.commit()
    return changed
