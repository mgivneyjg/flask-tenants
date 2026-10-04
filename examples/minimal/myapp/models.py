"""Shared and tenant-scoped models."""

from __future__ import annotations

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from flask_tenants import DomainMixin, TenantMixin, make_bases

bases = make_bases()
SharedBase = bases.shared
TenantBase = bases.tenant


# -- the registry, in `public` -------------------------------------------


class Tenant(SharedBase, TenantMixin):
    __tablename__ = "tenants"

    # Resolvers yield the subdomain ("acme"), not the primary key, so the
    # routing key is the slug. The schema name still derives from the id.
    __tenant_key__ = "slug"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    billing_plan: Mapped[str] = mapped_column(String(40), default="trial")


class Domain(SharedBase, DomainMixin):
    __tablename__ = "domains"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("public.tenants.id"))


# -- shared reference data ------------------------------------------------


class Speciality(SharedBase):
    """Reference data every tenant reads from the same rows."""

    __tablename__ = "specialities"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)


# -- tenant-scoped ---------------------------------------------------------


class Patient(TenantBase):
    """One copy of this table exists in every tenant's schema."""

    __tablename__ = "patients"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(120))

    # Cross-schema FK: reference the Column, not a string path. ForeignKey
    # string targets resolve within a single MetaData, and Speciality is in
    # the other one.
    speciality_id: Mapped[int | None] = mapped_column(
        ForeignKey(Speciality.__table__.c.id), nullable=True
    )
    # relationship() by string DOES work -- the two bases share a registry.
    speciality: Mapped["Speciality | None"] = relationship()
