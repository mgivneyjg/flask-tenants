"""Create the shared schema and two provisioned tenants.

Uses `create_all` for the shared schema and the example's tenant tables
because this is a demo with no migration history. A real deployment runs
Alembic -- see docs/migrations.md and docs/provisioning.md.
"""

from __future__ import annotations

from sqlalchemy import select, text

from flask_tenants import TenantState, tenant_context
from flask_tenants.schema import quote_identifier

from myapp.models import Domain, Patient, Speciality, Tenant, bases
from myapp.tenancy import Session, engine

SPECIALITIES = ["Cardiology", "Dermatology", "Paediatrics"]

TENANTS = [
    ("Acme Health", "acme", "acme.example.test", ["A. Patient", "B. Patient"]),
    ("Globex Clinics", "globex", "globex.example.test", ["C. Patient"]),
]


def main() -> None:
    bases.shared_metadata.create_all(engine)

    with Session() as session:
        for name in SPECIALITIES:
            if not session.execute(
                select(Speciality).where(Speciality.name == name)
            ).scalar_one_or_none():
                session.add(Speciality(name=name))
        session.commit()

    for name, slug, hostname, patients in TENANTS:
        tenant = _ensure_tenant(name, slug, hostname)
        _build_schema(tenant)
        _seed(tenant, patients)
        print(f"  {slug:8} -> {tenant.schema_name}  ({len(patients)} patients)")

    print("\nTry it:")
    for _, _, hostname, _ in TENANTS:
        print(f'  curl -H "Host: {hostname}" localhost:5000/patients')


def _ensure_tenant(name: str, slug: str, hostname: str) -> Tenant:
    with Session() as session:
        tenant = session.execute(select(Tenant).where(Tenant.slug == slug)).scalar_one_or_none()
        if tenant is None:
            tenant = Tenant(name=name, slug=slug, state=TenantState.PENDING)
            session.add(tenant)
            # The schema name derives from the primary key, so it is assigned
            # just after this flush -- by autoassign_schema_names(Session).
            session.flush()
            session.add(Domain(domain=hostname, tenant_id=tenant.id, is_primary=True))
        tenant.state = TenantState.ACTIVE
        session.commit()
        session.refresh(tenant)
        session.expunge(tenant)
        return tenant


def _build_schema(tenant: Tenant) -> None:
    token = bases.tenant_metadata.schema
    with engine.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {quote_identifier(tenant.schema_name)}"))
        scoped = conn.execution_options(schema_translate_map={token: tenant.schema_name})
        bases.tenant_metadata.create_all(scoped)


def _seed(tenant: Tenant, labels: list[str]) -> None:
    with tenant_context(tenant), Session() as session:
        if session.execute(select(Patient.id)).first():
            return
        speciality = session.execute(select(Speciality)).scalars().first()
        for label in labels:
            session.add(Patient(label=label, speciality_id=speciality.id))
        session.commit()


if __name__ == "__main__":
    main()
