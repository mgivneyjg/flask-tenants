"""Alembic env.py for the SHARED schema.

Copy to ``migrations/shared/env.py`` and point ``target_metadata`` at your own
``bases.shared_metadata``.

This environment owns one linear history for the tables that exist once, in
``public``. The tenant environment next door owns the other.
"""

from alembic import context
from myapp.models import bases  # <-- your make_bases() result

target_metadata = bases.shared_metadata
config = context.config


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        include_schemas=True,
        version_table_schema=target_metadata.schema,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connectable")
    if connectable is None:
        from sqlalchemy import engine_from_config, pool

        connectable = engine_from_config(
            config.get_section(config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Shared tables live in one known schema, so the version table does
            # too. Only the tenant environment needs per-schema version tables.
            version_table_schema=target_metadata.schema,
            include_schemas=False,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
