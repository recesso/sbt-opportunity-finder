"""E0.S4 + E0.S5 — run harness, checkpointing, cost accounting.

The predecessor died without writing anything on four consecutive firings. The
tests that matter here are the ones proving that cannot happen again — in
particular ``test_a_real_crash_resumes_without_reprocessing``, which kills an
actual subprocess with ``os._exit`` rather than simulating it.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from finder.context import (
    COUNTERS,
    CostLedger,
    RunError,
    last_run,
    new_run_id,
    resume_run,
    start_run,
    unfinished_runs,
)
from finder.store.db import open_db

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def conn() -> sqlite3.Connection:
    return open_db(":memory:")


def run_row(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    return conn.execute("SELECT * FROM run WHERE run_id=?", (run_id,)).fetchone()


# --- identifiers and lifecycle ---------------------------------------------


def test_run_id_is_sortable_and_legible() -> None:
    rid = new_run_id("weekly")
    assert rid.startswith("weekly-")
    assert len(rid.split("-")) == 3


def test_run_is_recorded_and_closed(conn: sqlite3.Connection) -> None:
    with start_run(conn, "weekly", config_hash="cfg-1") as ctx:
        rid = ctx.run_id
        assert run_row(conn, rid)["status"] == "running"
    row = run_row(conn, rid)
    assert row["status"] == "ok"
    assert row["finished_at"] is not None
    assert row["config_hash"] == "cfg-1"


def test_a_crashing_run_is_marked_failed_not_left_running(conn: sqlite3.Connection) -> None:
    """An abandoned run must be distinguishable from one still in flight."""
    with pytest.raises(ValueError), start_run(conn, "weekly") as ctx:
        rid = ctx.run_id
        raise ValueError("provider exploded")

    row = run_row(conn, rid)
    assert row["status"] == "failed"
    assert "provider exploded" in row["error"]
    assert json.loads(row["not_reached"]), "an aborted run must say it was aborted"


def test_unfinished_runs_finds_a_stuck_run(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO run (run_id, workflow, started_at, status) VALUES (?,?,?,'running')",
        ("stuck-1", "weekly", "2026-09-01T00:00:00+00:00"),
    )
    assert [r["run_id"] for r in unfinished_runs(conn)] == ["stuck-1"]


def test_last_run_filters_by_workflow(conn: sqlite3.Connection) -> None:
    with start_run(conn, "daily"):
        pass
    with start_run(conn, "weekly") as ctx:
        weekly_id = ctx.run_id
    assert last_run(conn, "weekly")["run_id"] == weekly_id
    assert last_run(conn)["run_id"] == weekly_id


# --- checkpointing ---------------------------------------------------------


def test_claim_is_exclusive_once_an_item_is_done(conn: sqlite3.Connection) -> None:
    with start_run(conn, "weekly") as ctx:
        assert ctx.claim("map", "org-1") is True
        ctx.complete("map", "org-1")
        assert ctx.claim("map", "org-1") is False


def test_a_stale_running_item_is_reclaimable(conn: sqlite3.Connection) -> None:
    """The resume case: a crashed process leaves items mid-flight and they must
    be retried, not stranded."""
    with start_run(conn, "weekly") as ctx:
        assert ctx.claim("map", "org-1") is True
        assert ctx.claim("map", "org-1") is True, "a running item is not terminal"


def test_failed_and_skipped_items_are_not_retried_within_a_run(conn: sqlite3.Connection) -> None:
    with start_run(conn, "weekly") as ctx:
        ctx.claim("map", "org-1")
        ctx.fail("map", "org-1", "404")
        assert ctx.claim("map", "org-1") is False

        ctx.claim("map", "org-2")
        ctx.skip("map", "org-2", "robots disallowed")
        assert ctx.claim("map", "org-2") is False


def test_completing_an_unclaimed_item_is_an_error(conn: sqlite3.Connection) -> None:
    """Bookkeeping that silently does nothing is how reporting becomes fiction."""
    with start_run(conn, "weekly") as ctx, pytest.raises(RunError, match="never claimed"):
        ctx.complete("map", "never-claimed")


def test_item_context_records_success(conn: sqlite3.Connection) -> None:
    with start_run(conn, "weekly") as ctx:
        with ctx.item("map", "org-1") as claimed:
            assert claimed
        assert ctx.stage_summary("map") == {"done": 1}


def test_item_context_isolates_a_failure(conn: sqlite3.Connection) -> None:
    """One bad page must not take down a harvest of four hundred."""
    with start_run(conn, "weekly") as ctx:
        for i in range(5):
            with ctx.item("map", f"org-{i}") as claimed:
                assert claimed
                if i == 2:
                    raise RuntimeError("that page is malformed")

        summary = ctx.stage_summary("map")
        assert summary == {"done": 4, "failed": 1}

    assert run_row(conn, ctx.run_id)["status"] == "ok", "item failures do not fail the run"


def test_item_context_yields_false_for_finished_work(conn: sqlite3.Connection) -> None:
    with start_run(conn, "weekly") as ctx:
        with ctx.item("map", "org-1"):
            pass
        seen = []
        with ctx.item("map", "org-1") as claimed:
            seen.append(claimed)
        assert seen == [False]


def test_pending_returns_only_unfinished_items(conn: sqlite3.Connection) -> None:
    with start_run(conn, "weekly") as ctx:
        keys = [f"org-{i}" for i in range(5)]
        with ctx.item("map", "org-1"):
            pass
        ctx.claim("map", "org-3")
        ctx.fail("map", "org-3", "boom")
        assert ctx.pending("map", keys) == ["org-0", "org-2", "org-4"]


# --- the real crash test ---------------------------------------------------


def parse_result(stdout: str) -> dict[str, str]:
    """Pull the worker's RESULT line out of stdout; structlog shares the stream."""
    for line in stdout.splitlines():
        if line.startswith("RESULT "):
            return dict(pair.split("=", 1) for pair in line.split()[1:])
    raise AssertionError(f"no RESULT line in worker output:\n{stdout}")


def test_a_real_crash_resumes_without_reprocessing(tmp_path: Path) -> None:
    """Kill a real process mid-item; restart; nothing completed is redone.

    ``os._exit`` in the child skips every finally block and buffer flush. This
    is the acceptance criterion for E0.S4 and the direct answer to four
    consecutive firings that died having written nothing.

    The precise invariant is narrower than "items 1-47 are not reprocessed":
    the 47th item was *mid-flight* when the process died, so it must be redone.
    Only completed work is protected. Both halves are asserted below.
    """
    db_path = tmp_path / "crash.db"
    worker = FIXTURES / "crashing_worker.py"
    run_id = "test-crash-001"

    crashed = subprocess.run(
        [sys.executable, str(worker), str(db_path), "crash", run_id, "47"],
        capture_output=True,
        text=True,
    )
    assert crashed.returncode == 137, f"child did not crash as expected: {crashed.stderr[-800:]}"

    conn = open_db(db_path)
    done_before = conn.execute(
        "SELECT COUNT(*) c FROM stage_run WHERE run_id=? AND status='done'", (run_id,)
    ).fetchone()["c"]
    assert done_before == 46, "46 completed before the crash; the 47th was mid-flight"
    assert run_row(conn, run_id)["status"] == "running", "a killed run stays marked running"
    conn.close()

    resumed = subprocess.run(
        [sys.executable, str(worker), str(db_path), "resume", run_id],
        capture_output=True,
        text=True,
    )
    assert resumed.returncode == 0, resumed.stderr[-800:]

    result = parse_result(resumed.stdout)
    assert int(result["processed"]) == 100 - done_before, (
        f"resume reprocessed work: did {result['processed']}, expected {100 - done_before}"
    )
    assert result["first"] == "item-046", (
        "resume must restart at the item that was mid-flight, not after it"
    )
    assert result["last"] == "item-099"

    conn = open_db(db_path)
    assert (
        conn.execute(
            "SELECT COUNT(*) c FROM stage_run WHERE run_id=? AND status='done'", (run_id,)
        ).fetchone()["c"]
        == 100
    )
    assert run_row(conn, run_id)["status"] == "ok"


# --- not_reached -----------------------------------------------------------


def test_not_reached_is_recorded_and_persisted(conn: sqlite3.Connection) -> None:
    """Silence must not be readable as completeness."""
    with start_run(conn, "weekly") as ctx:
        rid = ctx.run_id
        ctx.record_not_reached("budget", "stopped after 300 of 800 organizations", count=500)

    stored = json.loads(run_row(conn, rid)["not_reached"])
    assert stored == [
        {"reason": "budget", "detail": "stopped after 300 of 800 organizations", "count": 500}
    ]


def test_a_clean_run_reports_nothing_unreached(conn: sqlite3.Connection) -> None:
    with start_run(conn, "weekly") as ctx:
        rid = ctx.run_id
    assert json.loads(run_row(conn, rid)["not_reached"]) == []


# --- counters --------------------------------------------------------------


def test_counters_accumulate_and_persist(conn: sqlite3.Connection) -> None:
    with start_run(conn, "weekly") as ctx:
        rid = ctx.run_id
        ctx.count("pages_fetched", 12)
        ctx.count("pages_fetched", 3)
        ctx.count("routes_written")
    row = run_row(conn, rid)
    assert row["pages_fetched"] == 15
    assert row["routes_written"] == 1
    assert row["quarantined"] == 0


def test_an_unknown_counter_is_an_error(conn: sqlite3.Connection) -> None:
    """A typo that silently does nothing turns the run report into fiction."""
    with start_run(conn, "weekly") as ctx, pytest.raises(RunError, match="unknown counter"):
        ctx.count("pagez_fetched")


def test_counter_names_match_the_schema(conn: sqlite3.Connection) -> None:
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(run)")}
    assert columns >= COUNTERS, f"counters with no column: {COUNTERS - columns}"


# --- cost accounting -------------------------------------------------------


def test_cost_is_recorded_per_call_and_totalled(conn: sqlite3.Connection) -> None:
    with start_run(conn, "weekly") as ctx:
        rid = ctx.run_id
        ctx.cost.record("firecrawl", "map", usd=0.002)
        ctx.cost.record("firecrawl", "scrape", units=40, usd=0.04)
        ctx.cost.record("llm", "extract", usd=0.15)

        assert ctx.cost.total_usd == pytest.approx(0.192)
        assert ctx.cost.by_provider() == {
            "firecrawl": pytest.approx(0.042),
            "llm": pytest.approx(0.15),
        }

    assert run_row(conn, rid)["cost_usd"] == pytest.approx(0.192)
    assert conn.execute("SELECT COUNT(*) c FROM cost_event").fetchone()["c"] == 3


def test_cost_survives_a_run_that_dies(conn: sqlite3.Connection) -> None:
    """Spend is real whether or not the run finished; recording per call means a
    crashed run still leaves an accurate bill."""
    with pytest.raises(RuntimeError), start_run(conn, "weekly") as ctx:
        ctx.cost.record("firecrawl", "scrape", usd=0.5)
        raise RuntimeError("died mid-run")

    assert conn.execute("SELECT SUM(usd) s FROM cost_event").fetchone()["s"] == pytest.approx(0.5)
    assert run_row(conn, ctx.run_id)["cost_usd"] == pytest.approx(0.5)


def test_empty_ledger_totals_zero(conn: sqlite3.Connection) -> None:
    ledger = CostLedger(conn, "run-x")
    assert ledger.total_usd == 0.0
    assert ledger.by_provider() == {}


# --- the report ------------------------------------------------------------


def test_report_leads_with_what_failed(conn: sqlite3.Connection) -> None:
    """Standing rule: say what failed, first."""
    with start_run(conn, "weekly", config_hash="cfg-9") as ctx:
        ctx.count("routes_written", 7)
        ctx.cost.record("exa", "search", usd=0.01)
        ctx.record_not_reached("provider_outage", "Exa returned 503 for 40 queries", count=40)
        report = ctx.report()

    keys = list(report)
    assert keys.index("not_reached") < keys.index("counters")
    assert report["not_reached"][0]["reason"] == "provider_outage"
    assert report["counters"] == {"routes_written": 7}
    assert report["cost_usd"] == pytest.approx(0.01)
    assert report["config_hash"] == "cfg-9"


# --- resume ----------------------------------------------------------------


def test_resume_unknown_run_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(RunError, match="no such run"), resume_run(conn, "nope"):
        pass


def test_a_resumed_run_that_crashes_is_also_marked_failed(conn: sqlite3.Connection) -> None:
    """Resume is not a second-class path; it closes the run book the same way."""
    with start_run(conn, "weekly") as ctx:
        rid = ctx.run_id

    with pytest.raises(RuntimeError), resume_run(conn, rid):
        raise RuntimeError("died again")

    row = run_row(conn, rid)
    assert row["status"] == "failed"
    assert "died again" in row["error"]
    assert json.loads(row["not_reached"])[0]["reason"] == "run_aborted"


def test_resume_reopens_and_recloses(conn: sqlite3.Connection) -> None:
    with start_run(conn, "weekly") as ctx:
        rid = ctx.run_id
        ctx.claim("map", "org-1")
        ctx.complete("map", "org-1")

    with resume_run(conn, rid) as ctx2:
        assert ctx2.workflow == "weekly"
        assert ctx2.claim("map", "org-1") is False
        assert ctx2.claim("map", "org-2") is True
        ctx2.complete("map", "org-2")

    assert run_row(conn, rid)["status"] == "ok"


def test_every_log_line_carries_the_run_id(conn: sqlite3.Connection) -> None:
    """A run's log lines must be greppable by run_id or triage is guesswork."""
    with capture_logs() as logs, start_run(conn, "weekly") as ctx:
        rid = ctx.run_id
        ctx.claim("map", "org-1")
        ctx.fail("map", "org-1", "boom")
        ctx.record_not_reached("budget", "ran out")

    events = [e["event"] for e in logs]
    assert {"run_started", "item_failed", "not_reached", "run_finished"} <= set(events)
    for entry in logs:
        assert entry["run_id"] == rid, f"{entry['event']} has no run_id"
        assert entry["workflow"] == "weekly"


def test_failure_log_does_not_leak_a_whole_page_body(conn: sqlite3.Connection) -> None:
    """Errors get truncated; a 2 MB HTML body in a log line is how log files die."""
    with capture_logs() as logs, start_run(conn, "weekly") as ctx:
        ctx.claim("fetch", "page-1")
        ctx.fail("fetch", "page-1", "x" * 50_000)

    failure = next(e for e in logs if e["event"] == "item_failed")
    assert len(failure["error"]) == 500

    stored = conn.execute(
        "SELECT error FROM stage_run WHERE stage='fetch' AND item_key='page-1'"
    ).fetchone()["error"]
    assert len(stored) == 2000
