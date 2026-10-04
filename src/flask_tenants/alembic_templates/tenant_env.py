"""Alembic env.py for TENANT schemas.

Copy to ``migrations/tenant/env.py`` and point ``target_metadata`` at your own
``bases.tenant_metadata``.

Two things make this different from an ordinary env.py:

``version_table_schema`` pins ``alembic_version`` **inside each tenant's own
schema**. Tenants legitimately sit at different revisions -- one provisioned
this morning, one suspended through the last three releases -- so a single
global version table would assert something false. It also makes an
interrupted run resumable: re-running is a no-op for tenants already done.

``SET LOCAL search_path`` is what actually places the DDL. **Tenant migrations
must not pass a ``schema=`` argument** -- write unqualified DDL. Alembic formats
the schema into ``ALTER TABLE`` statements itself rather than routing them
through ``schema_translate_map``, so a symbolic ``tenant`` token survives into
the SQL and PostgreSQL rejects it with ``schema "tenant" does not exist``.
``op.create_table`` happens to work, which only makes the failure arrive later.

The translate map is still set, because autogenerate's reflection uses it, and
``strip_tenant_schema`` keeps ``--autogenerate`` from writing the ``schema=``
argument back in.

The runner supplies both through ``config.attributes``; it is never invoked
bare.
"""

from alembic import context

from flask_tenants.migrations import strip_tenant_schema
from flask_tenants.schema import quote_identifier

from myapp.models import bases  # <-- your make_bases() result

target_metadata = bases.tenant_metadata
config = context.config

TENANT_TOKEN = target_metadata.schema or "tenant"


def run_migrations_online() -> None:
    connectable = config.attributes.get("connectable")
    schema = config.attributes.get("schema")

    if connectable is None or schema is None:
        raise RuntimeError(
            "This environment migrates one tenant schema at a time and must be "
            "driven by MigrationRunner or the flask-tenants CLI, which supply "
            "the connectable and target schema. Running `alembic upgrade` "
            "directly here would not know which tenant to migrate."
        )

    with connectable.connect() as connection:
        connection = connection.execution_options(
            schema_translate_map={TENANT_TOKEN: schema}
        )
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table="alembic_version",
            version_table_schema=schema,
            include_schemas=False,
            compare_type=True,
            process_revision_directives=strip_tenant_schema,
        )
        with context.begin_transaction():
            # This is what places the DDL. SET LOCAL, so it reverts with the
            # transaction whether that commits or rolls back.
            connection.exec_driver_sql(
                f"SET LOCAL search_path TO {quote_identifier(schema)}"
            )
            context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("Offline mode is not supported for per-tenant migrations.")

run_migrations_online()
