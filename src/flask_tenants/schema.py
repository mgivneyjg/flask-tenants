"""Schema naming: derivation, validation and quoting.

A tenant's PostgreSQL schema name is a SQL *identifier*. Identifiers cannot be
supplied as bind parameters, so every name this package emits is interpolated
into DDL as text. That makes this module a security boundary rather than a
formatting helper.

Two rules follow from decision Q8:

1.  Schema names are derived from an **immutable surrogate id**, never from a
    human-facing slug. A customer rebranding should change a display label, not
    require renaming a schema underneath live connections.
2.  Every name is validated against a strict allowlist before use, and quoted
    on every use -- even though derivation already makes it safe. The validator
    is what keeps it safe when someone later adds a code path that sets the name
    directly.
"""

from __future__ import annotations

import re

from .errors import InvalidSchemaNameError

#: PostgreSQL truncates identifiers at ``NAMEDATALEN - 1``, which is 63 by default.
MAX_IDENTIFIER_LENGTH = 63

#: Lowercase, starts with a letter, then letters/digits/underscores.
_SCHEMA_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

#: Reserved by PostgreSQL itself. ``pg_`` is reserved by prefix, handled separately.
_RESERVED_SCHEMA_NAMES = frozenset(
    {
        "public",
        "information_schema",
        "pg_catalog",
        "pg_toast",
        "pg_temp",
    }
)

#: The symbolic schema token that tenant-scoped tables are declared with. It is
#: never a real schema -- ``schema_translate_map`` rewrites it at execution time.
TENANT_SCHEMA_TOKEN = "tenant"

#: The symbolic token for shared tables. Maps to a real schema, ``public`` by default.
SHARED_SCHEMA_TOKEN = "shared"


def validate_schema_name(name: str, *, reserved_ok: bool = False) -> str:
    """Validate a PostgreSQL schema name, returning it unchanged.

    :param reserved_ok: Permit names PostgreSQL reserves for itself, such as
        ``public``. Safe to quote and reference; never valid as a *tenant's*
        schema, which is why the default is to reject them.

    :raises InvalidSchemaNameError: if the name could not be used safely.
    """
    if not isinstance(name, str):
        raise InvalidSchemaNameError(f"Schema name must be a string, got {type(name).__name__}")
    if not name:
        raise InvalidSchemaNameError("Schema name must not be empty")
    if len(name) > MAX_IDENTIFIER_LENGTH:
        raise InvalidSchemaNameError(
            f"Schema name {name!r} is {len(name)} characters; "
            f"PostgreSQL truncates at {MAX_IDENTIFIER_LENGTH}"
        )
    if not _SCHEMA_NAME_RE.match(name):
        raise InvalidSchemaNameError(
            f"Schema name {name!r} is not a safe identifier. "
            "Use lowercase letters, digits and underscores, starting with a letter."
        )
    if name.startswith("pg_"):
        raise InvalidSchemaNameError(f"Schema name {name!r} uses the reserved 'pg_' prefix")
    if not reserved_ok and name in _RESERVED_SCHEMA_NAMES:
        raise InvalidSchemaNameError(
            f"Schema name {name!r} is reserved by PostgreSQL and cannot belong to a tenant"
        )
    return name


def validate_tenant_schema_name(name: str) -> str:
    """Validate a name that is about to become (or address) a tenant's schema.

    Stricter than :func:`validate_schema_name`: ``public`` and friends are
    rejected outright. Use this at every point that creates, migrates or drops
    a tenant schema -- it is what stops a mis-derived name from pointing DDL at
    the shared schema.
    """
    return validate_schema_name(name, reserved_ok=False)


def quote_identifier(name: str) -> str:
    """Quote an identifier for interpolation into DDL or ``SET`` statements.

    Validates first, so this can never emit something unsafe. Reserved names
    are permitted here because quoting ``public`` for a ``search_path`` is
    entirely legitimate -- it is *tenant* names that must never be reserved.
    Doubling embedded quotes is belt and braces; validation already rejects
    them.
    """
    validate_schema_name(name, reserved_ok=True)
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def derive_schema_name(tenant_id: object, *, prefix: str = "tenant_") -> str:
    """Build a schema name from a tenant's immutable surrogate id.

    UUIDs are the common case; their hyphens are not valid in an unquoted
    identifier, so they are converted to underscores.

    >>> derive_schema_name(42)
    'tenant_42'
    >>> derive_schema_name("3f2a-9c11")
    'tenant_3f2a_9c11'
    """
    token = str(tenant_id).strip().lower().replace("-", "_")
    if not token:
        raise InvalidSchemaNameError("Cannot derive a schema name from an empty tenant id")
    return validate_tenant_schema_name(f"{prefix}{token}")
