"""E1.S3 — the founder-owned write guard.

Acceptance: a worker attempting the write raises, the exception names the table,
and an audit row is written.

The predecessor enforced this by convention and the write path actually in use
bypassed it — Art had to redo his dispositions more than once. So the tests here
are not about a helper being polite. They are about three independent defences
holding: separate tables, no generic write method, and an authorizer sitting
BELOW the repository layer so that raw SQL is refused identically.
"""

from __future__ import annotations

import sqlite3

import pytest

from finder.store import guard
from finder.store.db import open_db, utcnow
from finder.store.guard import (
    FOUNDER_OWNED_TABLES,
    FounderFieldViolation,
    founder_write_allowed,
)
from finder.store.ids import mark_id, org_id, route_id
from finder.store.models import FounderMark, Organization, Route
from finder.store.repos import Store


@pytest.fixture
def store() -> Store:
    s = Store(open_db(":memory:"))
    org = s.organizations.upsert(
        Organization(
            org_id=org_id("gamep.org"),
            canonical_domain="gamep.org",
            name="GaMEP",
            name_normalized="gamep",
            first_seen=utcnow(),
        )
    )
    s.routes.upsert(
        Route(
            route_id=route_id("k1"),
            family="ROOM",
            org_id=org.org_id,
            mechanism_name="Lunch and learn",
            route_type="PARTNER_DELIVERY",
            series_key="k1",
            created_at=utcnow(),
        )
    )
    return s


def a_mark(when: str = "2026-09-01T00:00:00+00:00", verdict: str = "PURSUE") -> FounderMark:
    return FounderMark(
        mark_id=mark_id(route_id("k1"), when),
        route_id=route_id("k1"),
        marked_at=when,
        verdict=verdict,
    )


def attempts(store: Store, *, allowed: bool | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM founder_write_attempt"
    if allowed is not None:
        sql += f" WHERE allowed = {1 if allowed else 0}"
    return store.conn.execute(sql + " ORDER BY at").fetchall()


# --- the registry ----------------------------------------------------------


def test_the_founder_owned_tables_are_declared_in_one_place() -> None:
    from finder.store.repos import FOUNDER_OWNED_TABLES as re_exported

    assert {"founder_mark", "person_founder"} == FOUNDER_OWNED_TABLES
    assert re_exported is FOUNDER_OWNED_TABLES, "one registry, not two that can drift"


def test_founder_data_is_not_a_column_on_a_worker_table(store: Store) -> None:
    """Defence one: separation. There is no worker write path that happens to
    include the founder's fields."""
    person_cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(person)")}
    assert not (person_cols & {"known_to_art", "how_known", "last_contact"})


def test_the_mark_repository_has_no_generic_write_path(store: Store) -> None:
    """Defence two. A mark is a record of a decision he made; there is nothing
    to update and nothing to delete."""
    for forbidden in ("update", "delete", "upsert", "set", "remove", "clear"):
        assert not hasattr(store.marks, forbidden), f"MarkRepo grew a {forbidden}()"


# --- the authorizer: defence three -----------------------------------------


@pytest.mark.parametrize("table", sorted(FOUNDER_OWNED_TABLES))
@pytest.mark.parametrize("operation", ["INSERT INTO", "DELETE FROM"])
def test_raw_sql_against_a_founder_table_is_refused(
    store: Store, table: str, operation: str
) -> None:
    """The defence that survives a refactor: it sits BELOW the repository layer,
    so a future worker opening its own cursor is refused identically."""
    sql = (
        f"INSERT INTO {table} (mark_id, route_id, marked_at) VALUES ('m','r','t')"
        if operation.startswith("INSERT")
        else f"DELETE FROM {table}"
    )
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        store.conn.execute(sql)


def test_an_update_to_a_mark_is_refused(store: Store) -> None:
    store.marks.ingest(a_mark())
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        store.conn.execute("UPDATE founder_mark SET verdict = 'SKIP'")
    assert store.marks.for_route(route_id("k1"))[0].verdict == "PURSUE"


def test_reading_founder_data_is_always_allowed(store: Store) -> None:
    """The guard governs writes. His decisions are the training set and
    everything downstream has to be able to read them."""
    store.marks.ingest(a_mark())
    assert store.conn.execute("SELECT COUNT(*) c FROM founder_mark").fetchone()["c"] == 1
    assert len(store.marks.all_marks()) == 1


def test_writes_to_every_other_table_are_unaffected(store: Store) -> None:
    store.conn.execute("UPDATE organization SET tier = 'B'")
    assert store.organizations.get_by_domain("gamep.org").tier == "B"


# --- the repository path ---------------------------------------------------


def test_the_sanctioned_path_writes(store: Store) -> None:
    mark = store.marks.ingest(a_mark())
    assert mark.verdict == "PURSUE"
    assert store.marks.count() == 1


def test_a_worker_going_through_the_repository_is_still_refused(store: Store) -> None:
    """A worker cannot get in by calling `_ingest` past the guard wrapper."""
    with pytest.raises(FounderFieldViolation) as exc:
        store.marks._ingest(a_mark())

    assert exc.value.table == "founder_mark"
    assert exc.value.operation == "INSERT"
    assert "founder_mark" in str(exc.value), "the exception names the table"
    assert "MarkRepo.ingest" in str(exc.value), "and says what the sanctioned path is"


def test_the_permission_does_not_leak_past_the_block(store: Store) -> None:
    with founder_write_allowed():
        store.marks.ingest(a_mark())
    assert guard.is_allowed() is False
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        store.conn.execute("DELETE FROM founder_mark")


def test_the_permission_is_released_even_on_failure(store: Store) -> None:
    with pytest.raises(RuntimeError), founder_write_allowed():
        raise RuntimeError("boom")
    assert guard.is_allowed() is False


def test_ingest_releases_the_permission_afterwards(store: Store) -> None:
    store.marks.ingest(a_mark())
    assert guard.is_allowed() is False


# --- the audit trail -------------------------------------------------------


def test_a_refusal_is_audited(store: Store) -> None:
    """The acceptance criterion's third clause."""
    with pytest.raises(FounderFieldViolation):
        store.marks._ingest(a_mark())

    rows = attempts(store, allowed=False)
    assert len(rows) == 1
    assert rows[0]["table_name"] == "founder_mark"
    assert rows[0]["operation"] == "INSERT"
    assert rows[0]["at"]
    assert "test_write_guard.py" in rows[0]["caller"], (
        "the caller of record is the code that drove into the guard rail, not the guard rail itself"
    )


def test_a_sanctioned_write_is_audited_too(store: Store) -> None:
    """A log holding only violations cannot answer 'when was this mark written,
    and by what', which is what anyone debugging a lost decision asks."""
    store.marks.ingest(a_mark())

    rows = attempts(store, allowed=True)
    assert len(rows) == 1
    assert rows[0]["table_name"] == "founder_mark"
    assert route_id("k1") in rows[0]["detail"]
    assert "test_write_guard.py" in rows[0]["caller"]


def test_both_outcomes_land_in_one_trail(store: Store) -> None:
    """Also proves the guard survives a repeated identical statement: SQLite
    runs the authorizer at PREPARE time, and a statement cache would let the
    second write through."""
    store.marks.ingest(a_mark())
    with pytest.raises(FounderFieldViolation):
        store.marks._ingest(a_mark("2026-09-02T00:00:00+00:00"))

    assert [bool(r["allowed"]) for r in attempts(store)] == [True, False]


def test_a_repeated_identical_write_is_refused_every_time(store: Store) -> None:
    """The statement cache would prepare this once and skip the authorizer on
    every later execution, which is a guard that protects only the first row."""
    sql = "INSERT INTO founder_mark (mark_id, route_id, marked_at) VALUES (?,?,?)"
    with founder_write_allowed():
        store.conn.execute(sql, ("m-1", route_id("k1"), "2026-09-01T00:00:00+00:00"))

    for i in range(3):
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            store.conn.execute(sql, (f"m-{i + 2}", route_id("k1"), f"2026-09-0{i + 2}T00:00:00Z"))
    assert store.marks.count() == 1


def test_the_refusal_is_logged_at_error(store: Store) -> None:
    """A P0 defect must be visible in the log, not only in a table."""
    from structlog.testing import capture_logs

    with capture_logs() as logs, pytest.raises(FounderFieldViolation):
        store.marks._ingest(a_mark())

    refusal = next(e for e in logs if e["event"] == "founder_write_refused")
    assert refusal["log_level"] == "error"
    assert refusal["table"] == "founder_mark"


def test_an_audit_failure_does_not_mask_the_violation(store: Store) -> None:
    """If the audit table is gone the refusal must still raise. Losing the
    record is bad; letting the write look like it was considered is worse."""
    with founder_write_allowed():
        store.conn.execute("DROP TABLE founder_write_attempt")

    with pytest.raises(FounderFieldViolation):
        store.marks._ingest(a_mark())


# --- migrations ------------------------------------------------------------


def test_migrations_can_create_the_founder_tables(store: Store) -> None:
    """Schema changes are not founder writes. The guard governs rows, and a
    guard that blocked its own tables into existence would be self-defeating."""
    tables = {r["name"] for r in store.conn.execute("SELECT name FROM sqlite_master")}
    assert tables >= FOUNDER_OWNED_TABLES
    assert "founder_write_attempt" in tables


def test_a_fresh_database_starts_guarded() -> None:
    """The guard is installed by `connect`, so there is no window between
    opening a database and it being protected."""
    conn = open_db(":memory:")
    assert guard.is_allowed() is False
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        conn.execute("DELETE FROM founder_mark")


def test_the_guard_can_be_removed_only_deliberately() -> None:
    conn = open_db(":memory:")
    guard.uninstall(conn)
    conn.execute("DELETE FROM founder_mark")  # no longer refused
    guard.install(conn)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        conn.execute("DELETE FROM founder_mark")


def test_a_database_error_that_is_not_a_refusal_still_surfaces(store: Store) -> None:
    """The guard only relabels 'not authorized'. Every other database error must
    reach the caller unchanged, or a real fault gets reported as a permission
    problem and nobody looks at the actual cause."""
    with pytest.raises(sqlite3.DatabaseError) as exc:
        store.organizations._exec("SELECT * FROM no_such_table")
    assert "no such table" in str(exc.value)
    assert not isinstance(exc.value, FounderFieldViolation)


def test_the_caller_of_record_falls_back_when_the_stack_is_all_store_code() -> None:
    """A refusal raised from deep inside the store with no outside frame should
    say 'unknown' rather than crash while reporting a violation."""
    assert guard.caller_of_record(skip=10_000) == "unknown"


# --- labelling -------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "table"),
    [
        ("INSERT INTO founder_mark (a) VALUES (1)", "founder_mark"),
        ("update person_founder set x = 1", "person_founder"),
        ("INSERT INTO organization (a) VALUES (1)", None),
    ],
)
def test_a_refused_statement_is_labelled_with_its_table(sql: str, table: str | None) -> None:
    assert guard.table_in_statement(sql) == table


@pytest.mark.parametrize(
    ("sql", "operation"),
    [
        ("INSERT INTO x VALUES (1)", "INSERT"),
        ("  update x set a = 1", "UPDATE"),
        ("DELETE FROM x", "DELETE"),
        ("REPLACE INTO x VALUES (1)", "WRITE"),
        ("", "WRITE"),
    ],
)
def test_the_operation_is_named_or_falls_back(sql: str, operation: str) -> None:
    assert guard.operation_in_statement(sql) == operation
