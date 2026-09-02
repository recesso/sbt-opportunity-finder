"""The founder-owned write guard (E1.S3).

Founder-owned data is his. A worker attempting to write it is a P0 defect and
must fail loudly rather than succeed quietly.

The predecessor enforced this by convention, and the write path actually in use
bypassed it — he had to redo dispositions more than once. So here it is enforced
three ways, each of which would have to be defeated independently:

1. **Separate tables.** ``founder_mark`` and ``person_founder`` are not columns
   on ``person``; there is no worker write path that happens to include them.
2. **No generic write method.** :class:`~finder.store.repos.MarkRepo` exposes
   ``ingest`` and read methods. There is no update and no delete.
3. **A SQLite authorizer**, installed on every connection, which refuses INSERT,
   UPDATE and DELETE on those tables unless the code is inside
   :func:`founder_write_allowed`. This is the one that cannot be bypassed by a
   refactor, because it sits below the repository layer entirely — raw SQL run
   by a future worker is refused just the same.

Every attempt is recorded, allowed or not. A log holding only violations cannot
answer "when was this mark written, and by what", which is the question anyone
debugging a lost decision actually asks.
"""

from __future__ import annotations

import sqlite3
import traceback
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

import structlog

FOUNDER_OWNED_TABLES: frozenset[str] = frozenset({"founder_mark", "person_founder"})

_WRITE_ACTIONS = {
    sqlite3.SQLITE_INSERT: "INSERT",
    sqlite3.SQLITE_UPDATE: "UPDATE",
    sqlite3.SQLITE_DELETE: "DELETE",
}

# Set only inside founder_write_allowed(). A ContextVar rather than a module
# global so concurrent work cannot lift the guard for anyone else.
_ALLOWED: ContextVar[bool] = ContextVar("founder_write_allowed", default=False)

_log = structlog.get_logger("finder.store.guard")


class FounderFieldViolation(Exception):
    """A non-founder path tried to write founder-owned data.

    Names the table, because the first question is always "which one".
    """

    def __init__(self, table: str, operation: str, caller: str = "") -> None:
        super().__init__(
            f"{operation} on founder-owned table {table!r} refused: this data is the "
            "founder's and is written only through MarkRepo.ingest inside "
            f"founder_write_allowed(). Attempted from {caller or 'unknown'}."
        )
        self.table = table
        self.operation = operation
        self.caller = caller


@contextmanager
def founder_write_allowed() -> Iterator[None]:
    """Permit founder-owned writes for the duration of this block.

    Deliberately awkward to reach for. Anything using it is claiming to be the
    sanctioned ingest path, and a reviewer should treat a new call site as a
    change to who owns that data.
    """
    token = _ALLOWED.set(True)
    try:
        yield
    finally:
        _ALLOWED.reset(token)


def is_allowed() -> bool:
    return _ALLOWED.get()


def caller_of_record(skip: int = 0) -> str:
    """The first frame outside the store package. That is who to blame.

    Reporting the repository's own line would name the guard rail rather than
    the code that drove into it.
    """
    for frame in reversed(traceback.extract_stack()[: -1 - skip]):
        if "finder\\store" not in frame.filename and "finder/store" not in frame.filename:
            return f"{frame.filename}:{frame.lineno} in {frame.name}"
    return "unknown"


def install(conn: sqlite3.Connection) -> None:
    """Refuse founder-owned writes on this connection unless sanctioned.

    Below the repository layer on purpose: raw SQL a future worker runs against
    the same connection is refused identically.
    """

    def authorizer(action: int, arg1: str | None, arg2: str | None, *_: object) -> int:
        operation = _WRITE_ACTIONS.get(action)
        if operation is None or arg1 not in FOUNDER_OWNED_TABLES:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_OK if is_allowed() else sqlite3.SQLITE_DENY

    conn.set_authorizer(authorizer)


def uninstall(conn: sqlite3.Connection) -> None:
    """Remove the authorizer. Migrations need this; nothing else should."""
    conn.set_authorizer(None)


def record_attempt(
    conn: sqlite3.Connection,
    table: str,
    operation: str,
    *,
    allowed: bool,
    caller: str = "",
    detail: str = "",
) -> None:
    """Write the audit row. Never raises — an audit failure must not mask the
    violation it is recording."""
    from finder.store.db import utcnow

    try:
        conn.execute(
            "INSERT INTO founder_write_attempt"
            " (attempt_id, at, table_name, operation, allowed, caller, detail)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                f"fwa-{uuid.uuid4().hex[:16]}",
                utcnow(),
                table,
                operation,
                1 if allowed else 0,
                caller[:500],
                detail[:1000],
            ),
        )
    except sqlite3.Error as exc:  # pragma: no cover - the table exists after 004
        _log.error("founder_audit_write_failed", table=table, error=str(exc))


def refused(conn: sqlite3.Connection, table: str, operation: str, detail: str = "") -> None:
    """Log the violation at ERROR, audit it, and raise."""
    caller = caller_of_record(skip=1)
    _log.error(
        "founder_write_refused", table=table, operation=operation, caller=caller, detail=detail
    )
    record_attempt(conn, table, operation, allowed=False, caller=caller, detail=detail)
    raise FounderFieldViolation(table, operation, caller)


def table_in_statement(sql: str) -> str | None:
    """Which founder-owned table a statement targets, if any.

    Used to turn SQLite's bare "not authorized" into an error that says what was
    refused. Substring matching is adequate here precisely because the
    authorizer has already made the decision — this only labels it.
    """
    lowered = sql.lower()
    for table in sorted(FOUNDER_OWNED_TABLES):
        if table in lowered:
            return table
    return None


def operation_in_statement(sql: str) -> str:
    first = sql.strip().split(None, 1)[0].upper() if sql.strip() else ""
    return first if first in {"INSERT", "UPDATE", "DELETE"} else "WRITE"
