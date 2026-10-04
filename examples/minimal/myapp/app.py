"""The Flask application."""

from __future__ import annotations

from flask import Flask, g, jsonify, request
from sqlalchemy import select

from flask_tenants import FlaskTenants, TenantLogFilter, tenant_required

from myapp.models import Patient, Speciality
from myapp.tenancy import Session, manager


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "example-only-change-me"

    FlaskTenants(manager, app)

    import logging

    handler = logging.StreamHandler()
    handler.addFilter(TenantLogFilter())
    handler.setFormatter(logging.Formatter("%(levelname)s [%(tenant)s] %(message)s"))
    app.logger.addHandler(handler)

    @app.get("/healthz")
    def healthz():
        """Resolves to no tenant, and must still work."""
        return jsonify(ok=True)

    @app.get("/specialities")
    def specialities():
        """Shared reference data -- same rows for everyone, no tenant needed."""
        with Session() as session:
            return jsonify(session.execute(select(Speciality.name)).scalars().all())

    @app.get("/patients")
    @tenant_required
    def list_patients():
        with Session() as session:
            rows = session.execute(
                select(Patient.label, Speciality.name).outerjoin(Patient.speciality)
            ).all()
        return jsonify(
            tenant=str(g.tenant.tenant_key),
            schema=g.tenant.schema_name,
            patients=[{"label": label, "speciality": spec} for label, spec in rows],
        )

    @app.post("/patients")
    @tenant_required
    def add_patient():
        with Session() as session:
            patient = Patient(label=request.json["label"])
            session.add(patient)
            session.commit()
            return jsonify(id=patient.id), 201

    return app
