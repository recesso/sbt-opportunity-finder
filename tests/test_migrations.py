"""E1.S1 — schema and migrations.

The tests that matter here are the ones proving the schema refuses bad data:
STRICT typing, foreign keys actually on, the family/target invariant, and the
uniqueness of the dedupe keys that the predecessor system had and never checked.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from finder.store.db import (
    MigrationError,
    applied_versions,
    connect,
    discover_migrations,
    migrate,
    open_db,
    table_names,
    utcnow,
)

EXPECTED_TABLES = {
    "network",
    "organization",
    "employer",
    "person",
    "person_founder",
    "route",
    "route_room",
    "route_channel",
    "occurrence",
    "trigger",
    "evidence",
    "score",
    "signal",
    "founder_mark",
    "rejection",
    "run",
    "config_version",
    "stage_run",
    "cost_event",
    "schema_version",
}


@pytest.fixture
def db() -> sqlite3.Connection:
    return open_db(":memory:")


def _org(conn: sqlite3.Connection, org_id: str = "org-1", domain: str = "example.org") -> str:
    conn.execute(
        "INSERT INTO organization (org_id, canonical_domain, name, name_normalized, first_seen)"
        " VALUES (?,?,?,?,?)",
        (org_id, domain, "Example Org", "example org", utcnow()),
    )
    return org_id


def _route(conn: sqlite3.Connection, **kw) -> str:
    row = {
        "route_id": "rt-1",
        "family": "ROOM",
        "org_id": None,
        "employer_id": None,
        "person_id": None,
        "mechanism_name": "Industry Council",
        "route_type": "OPEN_CALL",
        "series_key": "sk-1",
        "created_at": utcnow(),
    }
    row.update(kw)
    conn.execute(
        "INSERT INTO route (route_id, family, org_id, employer_id, person_id,"
        " mechanism_name, route_type, series_key, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        tuple(
            row[k]
            for k in (
                "route_id",
                "family",
                "org_id",
                "employer_id",
                "person_id",
                "mechanism_name",
                "route_type",
                "series_key",
                "created_at",
            )
        ),
    )
    return row["route_id"]


# --- migration mechanics ---------------------------------------------------


def test_fresh_database_builds() -> None:
    conn = open_db(":memory:")
    assert table_names(conn) >= EXPECTED_TABLES


def test_migrations_are_idempotent() -> None:
    conn = connect(":memory:")
    first = migrate(conn)
    second = migrate(conn)
    assert first, "first run should apply migrations"
    assert second == [], "re-running must be a no-op"
    assert applied_versions(conn) == set(first)


def test_migrations_apply_in_order() -> None:
    versions = [v for v, _, _ in discover_migrations()]
    assert versions == sorted(versions)
    assert versions[0] == 1


def test_bad_migration_filename_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "oops.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match="does not match"):
        discover_migrations(tmp_path)


def test_duplicate_migration_version_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "001_b.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match="duplicate migration version"):
        discover_migrations(tmp_path)


def test_failed_migration_rolls_back_whole(tmp_path: Path) -> None:
    (tmp_path / "001_ok.sql").write_text("CREATE TABLE a (x TEXT) STRICT;", encoding="utf-8")
    (tmp_path / "002_bad.sql").write_text(
        "CREATE TABLE b (y TEXT) STRICT; THIS IS NOT SQL;", encoding="utf-8"
    )
    conn = connect(":memory:")
    with pytest.raises(MigrationError, match="rolled back"):
        migrate(conn, tmp_path)
    assert "b" not in table_names(conn), "a partially applied migration must leave nothing behind"
    assert applied_versions(conn) == {1}


def test_file_backed_database_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "finder.db"
    conn = open_db(path)
    _org(conn)
    conn.close()

    reopened = open_db(path)
    assert reopened.execute("SELECT COUNT(*) c FROM organization").fetchone()["c"] == 1


# --- the schema actually refuses bad data ----------------------------------


def test_foreign_keys_are_enforced(db: sqlite3.Connection) -> None:
    """SQLite disables FKs per connection by default; this proves ours are on."""
    with pytest.raises(sqlite3.IntegrityError):
        _route(db, org_id="org-does-not-exist")


def test_strict_typing_rejects_wrong_types(db: sqlite3.Connection) -> None:
    """Without STRICT, SQLite stores 'lots' in an INTEGER column without complaint."""
    _org(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE organization SET employer_reach_est = 'lots' WHERE org_id='org-1'")


def test_family_must_match_its_target(db: sqlite3.Connection) -> None:
    """A ROOM route without an organization is meaningless and must not exist."""
    with pytest.raises(sqlite3.IntegrityError):
        _route(db, family="ROOM", org_id=None)


def test_employer_route_requires_an_employer(db: sqlite3.Connection) -> None:
    _org(db)
    with pytest.raises(sqlite3.IntegrityError):
        _route(db, family="EMPLOYER", org_id="org-1", employer_id=None)


def test_unknown_family_is_rejected(db: sqlite3.Connection) -> None:
    _org(db)
    with pytest.raises(sqlite3.IntegrityError):
        _route(db, family="WAREHOUSE", org_id="org-1")


def test_series_key_is_unique(db: sqlite3.Connection) -> None:
    """Dedupe by content. The predecessor had these keys and never checked them:
    880 of its 976 keyed rows were inside duplicate clusters."""
    _org(db)
    _route(db, route_id="rt-1", org_id="org-1", series_key="same")
    with pytest.raises(sqlite3.IntegrityError):
        _route(db, route_id="rt-2", org_id="org-1", series_key="same")


def test_occurrence_key_is_unique(db: sqlite3.Connection) -> None:
    _org(db)
    _route(db, org_id="org-1")
    db.execute(
        "INSERT INTO occurrence (occ_id, route_id, occurrence_key) VALUES (?,?,?)",
        ("occ-1", "rt-1", "ok-1"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO occurrence (occ_id, route_id, occurrence_key) VALUES (?,?,?)",
            ("occ-2", "rt-1", "ok-1"),
        )


def test_canonical_domain_is_unique(db: sqlite3.Connection) -> None:
    _org(db, "org-1", "example.org")
    with pytest.raises(sqlite3.IntegrityError):
        _org(db, "org-2", "example.org")


def test_score_bounds_are_enforced(db: sqlite3.Connection) -> None:
    _org(db)
    _route(db, org_id="org-1")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO score (score_id, route_id, scored_at, config_hash,"
            " fit, route_score, confidence, components) VALUES (?,?,?,?,?,?,?,?)",
            ("s-1", "rt-1", utcnow(), "h", 140, 50, 50, "{}"),
        )


def test_rejection_needs_something_to_match_on(db: sqlite3.Connection) -> None:
    """A rejection with no name, domain or pattern would block nothing."""
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO rejection (rejection_id, created_at) VALUES (?,?)",
            ("rj-1", utcnow()),
        )


def test_founder_mark_cannot_be_duplicated(db: sqlite3.Connection) -> None:
    _org(db)
    _route(db, org_id="org-1")
    stamp = utcnow()
    db.execute(
        "INSERT INTO founder_mark (mark_id, route_id, marked_at, verdict) VALUES (?,?,?,?)",
        ("m-1", "rt-1", stamp, "GOOD"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO founder_mark (mark_id, route_id, marked_at, verdict) VALUES (?,?,?,?)",
            ("m-2", "rt-1", stamp, "BAD"),
        )


def test_founder_data_lives_in_its_own_tables(db: sqlite3.Connection) -> None:
    """Structural, not conventional: no worker table carries founder-owned fields.

    The predecessor enforced this by convention and the write path actually in
    use bypassed it.
    """
    person_cols = {r["name"] for r in db.execute("PRAGMA table_info(person)")}
    founder_only = {"known_to_art", "how_known", "last_contact", "connector_person_id"}
    assert not (person_cols & founder_only), (
        "founder-entered fields must live in person_founder, not person"
    )
    assert "person_founder" in table_names(db)


def test_route_url_and_evidence_url_are_separate_columns(db: sqlite3.Connection) -> None:
    """route_url is the page you act on; evidence_url is the page that proves it.

    Conflating them is how a past event page gets presented as a way in.
    """
    cols = {r["name"] for r in db.execute("PRAGMA table_info(route)")}
    assert {"route_url", "evidence_url", "route_url_is_offdomain"} <= cols


def test_cascade_delete_cleans_extensions(db: sqlite3.Connection) -> None:
    _org(db)
    _route(db, org_id="org-1")
    db.execute("INSERT INTO route_room (route_id) VALUES ('rt-1')")
    db.execute("DELETE FROM route WHERE route_id='rt-1'")
    assert db.execute("SELECT COUNT(*) c FROM route_room").fetchone()["c"] == 0


def test_stage_run_primary_key_prevents_double_processing(db: sqlite3.Connection) -> None:
    """ADR-010: claim() relies on this to make a resumed run skip finished items."""
    db.execute(
        "INSERT INTO stage_run (run_id, stage, item_key, status, started_at) VALUES (?,?,?,?,?)",
        ("run-1", "extract", "url-1", "done", utcnow()),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO stage_run (run_id, stage, item_key, status, started_at)"
            " VALUES (?,?,?,?,?)",
            ("run-1", "extract", "url-1", "running", utcnow()),
        )
