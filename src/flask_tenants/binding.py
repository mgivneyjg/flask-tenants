"""The chokepoint: binding a transaction to a tenant's schema (decisions Q4, Q13).

This is the one module the entire isolation guarantee rests on. Everything else
in the package assumes it is correct.

Two mechanisms, set from one place
----------------------------------

``schema_translate_map`` is the **source of truth**. Tenant tables are declared
against the symbolic schema ``tenant``; SQLAlchemy rewrites that token at
execution time. Because it is a per-execution option rather than connection
state, there is nothing to reset and therefore nothing to forget to reset.

``SET LOCAL search_path`` is a **backstop**, for raw SQL the translate map
cannot see. Crucially it is ``SET LOCAL``, not ``SET``: PostgreSQL reverts it at
``COMMIT`` or ``ROLLBACK``, including the rollback from an unhandled exception.
That removes the pooled-connection leak -- the canonical multi-tenancy
catastrophe -- structurally rather than procedurally, and makes the whole thing
correct under PgBouncer transaction pooling.

They are set from a single binder so they can never disagree.

One tenant per transaction
--------------------------

``after_begin`` fires once per transaction, so ``search_path`` is set once,
while the translate map is applied per statement. If the active tenant were
allowed to change mid-transaction the two would silently diverge. So it is not
allowed: switching tenants inside an open transaction raises.

Known gap
---------

``SET LOCAL`` outside a transaction is inert -- PostgreSQL warns and moves on.
A raw ``engine.connect()`` in autocommit therefore receives the translate map
and no ``search_path``. Use :func:`tenant_connection` for Core work outside a
Session.
"""

from __future__ import annotations

import logging
import warnings
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator

from sqlalchemy import Table, event, text
from sqlalchemy.orm import ORMExecuteState, Session, sessionmaker
from sqlalchemy.sql import visitors

from .context import Binding, current_binding
from .errors import NoActiveTenantError, TenantError
from .schema import TENANT_SCHEMA_TOKEN, quote_identifier

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.engine import Connection, Engine

log = logging.getLogger("flask_tenants.binding")

#: Key under which the transaction's bound tenant is recorded on ``session.info``.
_TXN_KEY = "_flask_tenants_txn_binding"

# NOTE: `Connection.info` is deliberately NOT used as the idempotence marker.
# It is proxied to the pooled DBAPI connection and therefore *survives* the
# checkout, so a connection returned to the pool and handed out again for the
# same tenant would look "already bound" -- skipping both the translate map
# (gone, because the Connection facade is new) and the SET LOCAL (gone, because
# the previous transaction committed). The marker is keyed to the transaction.

#: Counter for the condition decision Q15 says should never happen. If this is
#: ever non-zero in production you have found an untested code path.
_no_tenant_attempts = 0


def no_tenant_attempt_count() -> int:
    """How many times a tenant-scoped query ran with no tenant active.

    Should be zero. Exposed so it can be wired to a metrics backend without this
    package depending on one.
    """
    return _no_tenant_attempts


class SchemaBinder:
    """Applies the active tenant to every statement a Session executes.

    Install once per ``sessionmaker`` (or ``Session`` class) at startup.
    :class:`~flask_tenants.manager.TenantManager` does this for you; construct
    one directly only if you are wiring the pieces by hand.

    :param shared_schema: Real schema holding shared tables.
    :param tenant_token: Symbolic schema tenant tables are declared against.
    :param set_search_path: Emit the ``SET LOCAL search_path`` backstop. Leave
        on unless you have audited every caller for raw SQL.
    :param strict: Raise when a tenant-scoped statement runs with no tenant
        active. Turning this off is not recommended and exists only for
        migrating an existing codebase incrementally.
    """

    def __init__(
        self,
        *,
        shared_schema: str = "public",
        tenant_token: str = TENANT_SCHEMA_TOKEN,
        set_search_path: bool = True,
        strict: bool = True,
    ) -> None:
        self.shared_schema = shared_schema
        self.tenant_token = tenant_token
        self.set_search_path = set_search_path
        self.strict = strict
        self._installed: list[Any] = []

    # -- installation -----------------------------------------------------

    def install(self, target: sessionmaker | type[Session] | Session) -> None:
        """Register the event listeners on a sessionmaker or Session class."""
        event.listen(target, "do_orm_execute", self._on_orm_execute)
        event.listen(target, "after_begin", self._on_after_begin)
        event.listen(target, "after_transaction_end", self._on_transaction_end)
        self._installed.append(target)

    def uninstall(self, target: sessionmaker | type[Session] | Session) -> None:
        """Remove the listeners. Chiefly useful in tests."""
        event.remove(target, "do_orm_execute", self._on_orm_execute)
        event.remove(target, "after_begin", self._on_after_begin)
        event.remove(target, "after_transaction_end", self._on_transaction_end)
        if target in self._installed:
            self._installed.remove(target)

    # -- the chokepoint ---------------------------------------------------

    def translate_map(self, binding: Binding | None) -> dict[str, str]:
        """Build the ``schema_translate_map`` for a binding.

        With no binding the tenant token is deliberately left unmapped, so a
        tenant-scoped statement that slips past the strict check fails loudly
        against a schema that does not exist rather than reading ``public``.
        """
        if binding is None:
            return {}
        return {self.tenant_token: binding.schema}

    def _on_orm_execute(self, state: ORMExecuteState) -> None:
        """Check for the no-tenant case, then make sure the connection is bound.

        This fires for ``Session.execute()`` -- ORM queries *and* textual SQL --
        but **not** for unit-of-work flushes, which go through the persistence
        layer instead. That is precisely why the translate map is applied to the
        connection rather than to the statement: a connection-level option
        covers the INSERTs and UPDATEs a flush emits, and a statement-level one
        silently would not.

        Its job here is the strict check, which produces a far better error than
        the ``relation "tenant.x" does not exist`` a bare mechanism would give.
        """
        global _no_tenant_attempts

        binding = current_binding()

        if binding is None:
            if _statement_touches_tenant(state, self.tenant_token):
                _no_tenant_attempts += 1
                if self.strict:
                    raise NoActiveTenantError(
                        "A tenant-scoped query ran with no tenant active. "
                        "Wrap it in `with tenant_context(tenant):`, or use "
                        "`with public_schema():` if you meant the shared schema."
                    )
                log.warning("Tenant-scoped query with no active tenant (strict mode off)")
            return

        self._assert_same_tenant(state.session, binding)
        # Acquiring the connection fires `after_begin` if the transaction has not
        # started yet. If it started before the tenant was activated -- which is
        # exactly what a test fixture holding an outer transaction does -- then
        # `after_begin` has already run and this is where the binding lands.
        self._apply(state.session, state.session.connection(), binding)

    def _on_after_begin(self, session: Session, transaction: Any, connection: "Connection") -> None:
        """Bind the connection as soon as its transaction opens."""
        binding = current_binding()
        if binding is None:
            return
        self._apply(session, connection, binding)

    def _apply(self, session: Session, connection: "Connection", binding: Binding) -> None:
        """Bind a connection to a tenant. Idempotent within one transaction.

        Both mechanisms are set here and nowhere else. That is what "chokepoint"
        means: there is no second place either could be set from, so they cannot
        drift apart.

        Idempotence is keyed to the *transaction*, not the connection, for the
        reason noted at the top of this module -- and because ``SET LOCAL`` dies
        with the transaction, so a new transaction genuinely does need it again.
        """
        transaction = session.get_transaction()
        marker = session.info.get(_TXN_KEY)
        if marker is not None and marker[0] is transaction and marker[1] == binding.key:
            return

        session.info[_TXN_KEY] = (transaction, binding.key)

        # Connection.execution_options() updates in place and applies to
        # everything subsequently executed on this connection -- including the
        # statements a flush emits, which never pass through do_orm_execute.
        connection.execution_options(schema_translate_map=self.translate_map(binding))

        if not self.set_search_path:
            return

        # Identifiers cannot be bound parameters, so this is interpolated --
        # which is why every name passes through `quote_identifier` first.
        connection.exec_driver_sql(f"SET LOCAL search_path TO {self._search_path_for(binding)}")

    def _search_path_for(self, binding: Binding) -> str:
        """Build a validated, quoted search_path for a binding."""
        if binding.is_public:
            return quote_identifier(binding.schema)
        if binding.schema == self.shared_schema:
            return quote_identifier(self.shared_schema)
        return f"{quote_identifier(binding.schema)}, {quote_identifier(self.shared_schema)}"

    def _assert_same_tenant(self, session: Session, binding: Binding) -> None:
        """Refuse to switch tenants inside an open transaction.

        ``search_path`` is set once per transaction and the translate map per
        statement. Allowing the tenant to change would let the two disagree,
        so the second half of the transaction could read raw SQL from one
        tenant and ORM queries from another.
        """
        marker = session.info.get(_TXN_KEY)
        if marker is None or marker[0] is not session.get_transaction():
            return
        bound = marker[1]
        if bound != binding.key:
            raise TenantError(
                f"Transaction is bound to tenant {bound!r} but tenant {binding.key!r} "
                "is now active. A transaction belongs to exactly one tenant -- commit "
                "or roll back before switching."
            )

    def _on_transaction_end(self, session: Session, transaction: Any) -> None:
        """Drop the marker when its transaction ends, so the next one rebinds."""
        marker = session.info.get(_TXN_KEY)
        if marker is not None and marker[0] is transaction:
            del session.info[_TXN_KEY]

    # -- Core escape hatch ------------------------------------------------

    @contextmanager
    def connection(self, engine: "Engine", binding: Binding | None = None) -> Iterator["Connection"]:
        """Open a Core connection correctly bound to a tenant.

        For work outside a Session. Opens an explicit transaction, which is what
        makes ``SET LOCAL`` effective -- see the known gap in the module
        docstring.
        """
        binding = binding or current_binding()
        if binding is None:
            raise NoActiveTenantError("tenant_connection() requires an active tenant")

        with engine.connect() as conn:
            conn = conn.execution_options(schema_translate_map=self.translate_map(binding))
            with conn.begin():
                if self.set_search_path:
                    conn.exec_driver_sql(f"SET LOCAL search_path TO {self._search_path_for(binding)}")
                yield conn


def _statement_touches_tenant(state: ORMExecuteState, tenant_token: str) -> bool:
    """Does this statement reference a tenant-scoped table?

    Used only to decide whether "no active tenant" is an error. ORM statements
    are answered from the mappers; Core statements by walking the expression
    tree for ``Table`` objects.

    Textual SQL is invisible to both -- the known gap. Such a statement runs
    without a ``search_path`` and will generally fail against the default one,
    which is the intended outcome, just with a less helpful message.
    """
    mappers = getattr(state, "all_mappers", None)
    if mappers:
        return any(
            m.local_table is not None and m.local_table.schema == tenant_token for m in mappers
        )

    statement = state.statement
    if statement is None:
        return False
    try:
        for element in visitors.iterate(statement, {"column_collections": False}):
            if isinstance(element, Table) and element.schema == tenant_token:
                return True
    except Exception:  # pragma: no cover - never let introspection break a query
        return False
    return False


def warn_if_untracked(engine: "Engine") -> None:
    """Warn when a raw connection runs in autocommit with a tenant active.

    The Q13 gap made audible. Attach in development; it costs a check per
    connection and is not meant for production.
    """

    @event.listens_for(engine, "begin")
    def _mark(conn):  # pragma: no cover - diagnostic only
        conn.info["_flask_tenants_in_txn"] = True

    @event.listens_for(engine, "commit")
    @event.listens_for(engine, "rollback")
    def _unmark(conn):  # pragma: no cover - diagnostic only
        conn.info.pop("_flask_tenants_in_txn", None)

    @event.listens_for(engine, "before_cursor_execute")
    def _check(conn, cursor, statement, parameters, context, executemany):  # pragma: no cover
        if current_binding() is None:
            return
        if conn.info.get("_flask_tenants_in_txn"):
            return
        warnings.warn(
            "A statement executed outside a transaction while a tenant was active. "
            "SET LOCAL does not apply, so raw SQL will resolve against the default "
            "search_path. Use SchemaBinder.connection() or a Session.",
            stacklevel=2,
        )


__all__ = ["SchemaBinder", "no_tenant_attempt_count", "warn_if_untracked"]
