"""SQLite connection factory and migration runner (ADR-001).

One file at ``data/finder.db``. No server, no pooling, no managed instance.
WAL mode and foreign keys are enabled on every connection — SQLite disables
foreign keys per-connection by default, which is a well-known way to get silent
orphan rows.

Migrations are numbered ``NNN_name.sql`` files applied in order inside a
transaction and recorded, so re-running is a no-op and a failed migration rolls
back whole.

    from finder.store.db import connect, migrate
    conn = connect(Path("data/finder.db"))
    migrate(conn)
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
MIGRATION_PATTERN = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


class MigrationError(Exception):
    """Raised when migrations are malformed or cannot be applied."""


def utcnow() -> str:
    """ISO8601 UTC. Every timestamp in the schema uses this format."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path | str = ":memory:") -> sqlite3.Connection:
    """Open a connection with the settings this schema assumes."""
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path), isolation_level=None)  # explicit transactions
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block in one transaction. Rolls back whole on any exception."""
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _ensure_schema_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TEXT NOT NULL
        ) STRICT
        """
    )


def discover_migrations(directory: Path | None = None) -> list[tuple[int, str, Path]]:
    """Return ``(version, name, path)`` sorted by version.

    Raises if a filename does not match the convention or a version repeats —
    both are the kind of mistake that silently skips a migration.
    """
    directory = directory or MIGRATIONS_DIR
    found: dict[int, tuple[str, Path]] = {}

    for path in sorted(directory.glob("*.sql")):
        match = MIGRATION_PATTERN.match(path.name)
        if not match:
            raise MigrationError(
                f"migration filename {path.name!r} does not match NNN_lowercase_name.sql"
            )
        version = int(match.group(1))
        if version in found:
            raise MigrationError(
                f"duplicate migration version {version:03d}: {found[version][0]} and {path.name}"
            )
        found[version] = (path.stem, path)

    return [(v, name, p) for v, (name, p) in sorted(found.items())]


def split_statements(sql: str) -> list[str]:
    """Split a migration script into complete statements.

    ``executescript`` cannot be used: it issues an implicit COMMIT before
    running, which silently defeats the all-or-nothing guarantee migrations are
    supposed to have. ``sqlite3.complete_statement`` gives correct statement
    boundaries without hand-rolling a SQL parser.
    """
    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        stripped = line.strip()
        is_noise = not stripped or stripped.startswith("--")
        if is_noise and not buffer.strip():
            continue  # leading blank lines and comment blocks between statements
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        raise MigrationError(f"trailing incomplete statement: {buffer.strip()[:80]!r}")
    return statements


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    _ensure_schema_version_table(conn)
    rows = conn.execute("SELECT version FROM schema_version").fetchall()
    return {row["version"] for row in rows}


def migrate(conn: sqlite3.Connection, directory: Path | None = None) -> list[int]:
    """Apply any unapplied migrations in order. Returns the versions applied.

    Idempotent: running twice against the same database applies nothing the
    second time and returns an empty list.
    """
    _ensure_schema_version_table(conn)
    already = applied_versions(conn)
    applied: list[int] = []

    for version, name, path in discover_migrations(directory):
        if version in already:
            continue
        statements = split_statements(path.read_text(encoding="utf-8"))
        try:
            with transaction(conn):
                for statement in statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_version (version, name, applied_at) VALUES (?,?,?)",
                    (version, name, utcnow()),
                )
        except sqlite3.Error as exc:
            raise MigrationError(f"migration {name} failed and was rolled back: {exc}") from exc
        applied.append(version)

    return applied


def open_db(path: Path | str = ":memory:", *, migrate_now: bool = True) -> sqlite3.Connection:
    """Connect and bring the schema up to date. The normal entry point."""
    conn = connect(path)
    if migrate_now:
        migrate(conn)
    return conn


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {row["name"] for row in rows}
