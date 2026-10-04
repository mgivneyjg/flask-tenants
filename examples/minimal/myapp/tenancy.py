"""Engine, session factory and the tenant manager."""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from flask_tenants import (
    DomainResolver,
    autoassign_schema_names,
    SQLAlchemyRegistry,
    SubdomainResolver,
    TenantManager,
)

from myapp.models import Domain, Tenant, bases

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://app:app@localhost:5432/app"
)

# One shared pool for every tenant. Size it against max_connections across
# every web node and worker -- see docs/deployment.md.
engine = create_engine(DATABASE_URL, pool_size=5, max_overflow=5, pool_pre_ping=True)
Session = sessionmaker(engine)

# Fills in Tenant.schema_name right after the insert that assigns the id.
autoassign_schema_names(Session)

registry = SQLAlchemyRegistry(Session, Tenant, domain_model=Domain)

manager = TenantManager(
    engine=engine,
    session_factory=Session,
    bases=bases,
    registry=registry,
    # Ordered chain: the subdomain check is free, so it goes first; the
    # domain-table lookup catches vanity domains.
    resolvers=[
        SubdomainResolver("example.test", ignore=("www",)),
        DomainResolver(registry),
    ],
)
