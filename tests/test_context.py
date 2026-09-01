"""E0.S4 + E0.S5 — run harness, checkpointing, cost accounting.

The predecessor died without writing anything on four consecutive firings. The
tests that matter here are the ones proving that cannot happen again — in
particular ``test_a_real_crash_resumes_without_reprocessing``, which kills an
actual subprocess with ``os._exit`` rather than simulating it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from finder.context import (
    COUNTERS,
    RunContext,
    RunError,
    last_run,
    new_run_id,
    resume_run,
    start_run,
    unfinished_runs,
)
from finder.store.db import open_db
from finder.store.repos import RepoError, Store

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def store() -> Store:
    return Store(open_db(":memory:"))


# --- identifiers and lifecycle ---------------------------------------------


def test_run_id_is_sortable_and_legible() -> None:
    rid = new_run_id("weekly")
    assert rid.startswith("weekly-")
    assert len(rid.split("-")) == 3


def test_run_is_recorded_and_closed(store: Store) -> None:
    with start_run(store, "weekly", config_hash="cfg-1") as ctx:
        rid = ctx.run_id
        assert store.runs.get(rid).status == "running"
    run = store.runs.get(rid)
    assert run.status == "ok"
    assert run.finished_at is not None
    assert run.config_hash == "cfg-1"
    assert run.not_reached == []


def test_a_crashing_run_is_marked_failed_not_left_running(store: Store) -> None:
    """An abandoned run must be distinguishable from one still in flight."""
    with pytest.raises(ValueError), start_run(store, "weekly") as ctx:
        rid = ctx.run_id
        raise ValueError("provider exploded")

    run = store.runs.get(rid)
    assert run.status == "failed"
    assert "provider exploded" in run.error
    assert run.not_reached[0]["reason"] == "run_aborted"


def test_unfinished_runs_finds_a_stuck_run(store: Store) -> None:
    store.runs.start("stuck-1", "weekly")
    assert [r.run_id for r in unfinished_runs(store)] == ["stuck-1"]

    with start_run(store, "weekly"):
        pass
    assert [r.run_id for r in unfinished_runs(store)] == ["stuck-1"], (
        "a run that closed cleanly is not unfinished"
    )


def test_last_run_filters_by_workflow(store: Store) -> None:
    with start_run(store, "daily"):
        pass
    with start_run(store, "weekly") as ctx:
        weekly_id = ctx.run_id
    assert last_run(store, "weekly").run_id == weekly_id
    assert last_run(store).run_id == weekly_id
    assert last_run(store, "monthly") is None


# --- checkpointing ---------------------------------------------------------


def test_claim_is_exclusive_once_an_item_is_done(store: Store) -> None:
    with start_run(store, "weekly") as ctx:
        assert ctx.claim("map", "org-1") is True
        ctx.complete("map", "org-1")
        assert ctx.claim("map", "org-1") is False


def test_a_stale_running_item_is_reclaimable(store: Store) -> None:
    """The resume case: a crashed process leaves items mid-flight and they must
    be retried, not stranded."""
    with start_run(store, "weekly") as ctx:
        assert ctx.claim("map", "org-1") is True
        assert ctx.claim("map", "org-1") is True, "a running item is not terminal"


def test_failed_and_skipped_items_are_not_retried_within_a_run(store: Store) -> None:
    with start_run(store, "weekly") as ctx:
        ctx.claim("map", "org-1")
        ctx.fail("map", "org-1", "404")
        assert ctx.claim("map", "org-1") is False

        ctx.claim("map", "org-2")
        ctx.skip("map", "org-2", "robots disallowed")
        assert ctx.claim("map", "org-2") is False


def test_checkpoints_are_scoped_to_their_run(store: Store) -> None:
    """Last week's completed work must not suppress this week's."""
    with start_run(store, "weekly", run_id="r-1") as first:
        first.claim("map", "org-1")
        first.complete("map", "org-1")

    with start_run(store, "weekly", run_id="r-2") as second:
        assert second.claim("map", "org-1") is True


def test_completing_an_unclaimed_item_is_an_error(store: Store) -> None:
    """Bookkeeping that silently does nothing is how reporting becomes fiction."""
    with start_run(store, "weekly") as ctx, pytest.raises(RunError, match="never claimed"):
        ctx.complete("map", "never-claimed")


def test_item_context_records_success(store: Store) -> None:
    with start_run(store, "weekly") as ctx:
        with ctx.item("map", "org-1") as claimed:
            assert claimed
        assert ctx.stage_summary("map") == {"done": 1}


def test_item_context_isolates_a_failure(store: Store) -> None:
    """One bad page must not take down a harvest of four hundred."""
    with start_run(store, "weekly") as ctx:
        for i in range(5):
            with ctx.item("map", f"org-{i}") as claimed:
                assert claimed
                if i == 2:
                    raise RuntimeError("that page is malformed")

        assert ctx.stage_summary("map") == {"done": 4, "failed": 1}
        rid = ctx.run_id

    assert store.runs.get(rid).status == "ok", "item failures do not fail the run"


def test_a_failed_item_records_why(store: Store) -> None:
    with start_run(store, "weekly") as ctx:
        with ctx.item("map", "org-1"):
            raise ValueError("no such element")
        assert store.stage_runs.status(ctx.run_id, "map", "org-1") == "failed"


def test_item_context_yields_false_for_finished_work(store: Store) -> None:
    with start_run(store, "weekly") as ctx:
        with ctx.item("map", "org-1"):
            pass
        seen = []
        with ctx.item("map", "org-1") as claimed:
            seen.append(claimed)
        assert seen == [False]


def test_pending_returns_only_unfinished_items_in_order(store: Store) -> None:
    with start_run(store, "weekly") as ctx:
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
    Only completed work is protected. Both halves are asserted here, along with
    the counters and the spend, which must also survive the kill.
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

    store = Store(open_db(db_path))
    done_before = store.stage_runs.summary(run_id, "process")["done"]
    assert done_before == 46, "46 completed before the crash; the 47th was mid-flight"

    crashed_run = store.runs.get(run_id)
    assert crashed_run.status == "running", "a killed run stays marked running"
    assert crashed_run.counters["pages_fetched"] == 47, (
        "counters must be written through, not totalled at a close that never came"
    )
    assert store.costs.total(run_id) == pytest.approx(0.047), "spend before the crash is real"
    store.conn.close()

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

    store = Store(open_db(db_path))
    final = store.runs.get(run_id)
    assert store.stage_runs.summary(run_id, "process") == {"done": 100}
    assert final.status == "ok"
    assert final.counters["pages_fetched"] == 101, (
        "47 before the crash plus 54 after: the mid-flight item is counted twice, "
        "which is the honest number for work actually performed"
    )
    assert final.cost_usd == pytest.approx(0.101)


# --- not_reached -----------------------------------------------------------


def test_not_reached_is_recorded_and_persisted(store: Store) -> None:
    """Silence must not be readable as completeness."""
    with start_run(store, "weekly") as ctx:
        rid = ctx.run_id
        ctx.record_not_reached("budget", "stopped after 300 of 800 organizations", count=500)

    assert store.runs.get(rid).not_reached == [
        {"reason": "budget", "detail": "stopped after 300 of 800 organizations", "count": 500}
    ]


def test_not_reached_is_on_the_row_before_the_run_closes(store: Store) -> None:
    """Recorded, then killed: the truncation record is already durable."""
    with start_run(store, "weekly", run_id="r-1") as ctx:
        ctx.record_not_reached("provider_outage", "Exa 503")
        assert store.runs.get("r-1").not_reached[0]["reason"] == "provider_outage"
        assert store.runs.get("r-1").status == "running", "not yet closed"


def test_a_clean_run_reports_nothing_unreached(store: Store) -> None:
    with start_run(store, "weekly") as ctx:
        rid = ctx.run_id
    assert store.runs.get(rid).not_reached == []


# --- counters --------------------------------------------------------------


def test_counters_accumulate_and_persist(store: Store) -> None:
    with start_run(store, "weekly") as ctx:
        rid = ctx.run_id
        ctx.count("pages_fetched", 12)
        ctx.count("pages_fetched", 3)
        ctx.count("routes_written")
    counters = store.runs.get(rid).counters
    assert counters["pages_fetched"] == 15
    assert counters["routes_written"] == 1
    assert counters["quarantined"] == 0


def test_an_unknown_counter_is_an_error(store: Store) -> None:
    """A typo that silently does nothing turns the run report into fiction."""
    with start_run(store, "weekly") as ctx, pytest.raises(RunError, match="unknown counter"):
        ctx.count("pagez_fetched")


def test_the_repo_refuses_an_unknown_counter_too(store: Store) -> None:
    """The guard belongs at the boundary as well: the SQL builds a column name."""
    store.runs.start("r-1", "weekly")
    with pytest.raises(RepoError, match="unknown run counter"):
        store.runs.bump("r-1", "pages_fetched = 0, workflow = 'x'", 1)
    assert store.runs.get("r-1").workflow == "weekly"


def test_counter_names_match_the_schema(store: Store) -> None:
    columns = {r["name"] for r in store.conn.execute("PRAGMA table_info(run)")}
    assert columns >= COUNTERS, f"counters with no column: {COUNTERS - columns}"


# --- cost accounting -------------------------------------------------------


def test_cost_is_recorded_per_call_and_totalled(store: Store) -> None:
    with start_run(store, "weekly") as ctx:
        rid = ctx.run_id
        ctx.cost.record("firecrawl", "map", usd=0.002)
        ctx.cost.record("firecrawl", "scrape", units=40, usd=0.04)
        ctx.cost.record("llm", "extract", usd=0.15)

        assert ctx.cost.total_usd == pytest.approx(0.192)
        assert ctx.cost.by_provider() == {
            "firecrawl": pytest.approx(0.042),
            "llm": pytest.approx(0.15),
        }

    assert store.runs.get(rid).cost_usd == pytest.approx(0.192)
    assert store.costs.count() == 3


def test_cost_survives_a_run_that_dies(store: Store) -> None:
    """Spend is real whether or not the run finished; recording per call means a
    crashed run still leaves an accurate bill."""
    with pytest.raises(RuntimeError), start_run(store, "weekly", run_id="r-1") as ctx:
        ctx.cost.record("firecrawl", "scrape", usd=0.5)
        raise RuntimeError("died mid-run")

    assert store.costs.total("r-1") == pytest.approx(0.5)
    assert store.runs.get("r-1").cost_usd == pytest.approx(0.5)


def test_cost_is_scoped_to_the_run(store: Store) -> None:
    with start_run(store, "weekly", run_id="r-1") as first:
        first.cost.record("exa", "search", usd=1.0)
    with start_run(store, "weekly", run_id="r-2") as second:
        second.cost.record("exa", "search", usd=0.25)
        assert second.cost.total_usd == pytest.approx(0.25)


def test_a_run_with_no_spend_totals_zero(store: Store) -> None:
    with start_run(store, "weekly") as ctx:
        assert ctx.cost.total_usd == 0.0
        assert ctx.cost.by_provider() == {}


# --- the report ------------------------------------------------------------


def test_report_leads_with_what_failed(store: Store) -> None:
    """Standing rule: say what failed, first."""
    with start_run(store, "weekly", config_hash="cfg-9") as ctx:
        ctx.count("routes_written", 7)
        ctx.cost.record("exa", "search", usd=0.01)
        ctx.record_not_reached("provider_outage", "Exa returned 503 for 40 queries", count=40)
        report = ctx.report()

    keys = list(report)
    assert keys.index("not_reached") < keys.index("counters")
    assert report["not_reached"][0]["reason"] == "provider_outage"
    assert report["counters"] == {"routes_written": 7}
    assert report["cost_usd"] == pytest.approx(0.01)
    assert report["cost_by_provider"] == {"exa": pytest.approx(0.01)}
    assert report["config_hash"] == "cfg-9"


# --- resume ----------------------------------------------------------------


def test_resume_unknown_run_raises(store: Store) -> None:
    with pytest.raises(RunError, match="no such run"), resume_run(store, "nope"):
        pass


def test_resume_reopens_and_recloses(store: Store) -> None:
    with start_run(store, "weekly") as ctx:
        rid = ctx.run_id
        ctx.claim("map", "org-1")
        ctx.complete("map", "org-1")

    with resume_run(store, rid) as ctx2:
        assert ctx2.workflow == "weekly"
        assert ctx2.claim("map", "org-1") is False
        assert ctx2.claim("map", "org-2") is True
        ctx2.complete("map", "org-2")

    assert store.runs.get(rid).status == "ok"


def test_resume_carries_forward_counters_and_truncation(store: Store) -> None:
    """The closing report covers the run, not just its last attempt."""
    with start_run(store, "weekly", config_hash="cfg-3", run_id="r-1") as ctx:
        ctx.count("pages_fetched", 40)
        ctx.record_not_reached("budget", "300 of 800")

    with resume_run(store, "r-1") as ctx2:
        assert ctx2.config_hash == "cfg-3"
        assert ctx2.counters == {"pages_fetched": 40}
        assert [n.reason for n in ctx2.not_reached] == ["budget"]
        ctx2.count("pages_fetched", 2)
        report = ctx2.report()

    assert report["counters"]["pages_fetched"] == 42
    assert store.runs.get("r-1").counters["pages_fetched"] == 42
    assert len(store.runs.get("r-1").not_reached) == 1, "no duplicate truncation record"


def test_a_resumed_run_that_crashes_is_also_marked_failed(store: Store) -> None:
    """Resume is not a second-class path; it closes the run book the same way."""
    with start_run(store, "weekly", run_id="r-1"):
        pass

    with pytest.raises(RuntimeError), resume_run(store, "r-1"):
        raise RuntimeError("died again")

    run = store.runs.get("r-1")
    assert run.status == "failed"
    assert "died again" in run.error
    assert run.not_reached[0]["reason"] == "run_aborted"


# --- logging ---------------------------------------------------------------


def test_every_log_line_carries_the_run_id(store: Store) -> None:
    """A run's log lines must be greppable by run_id or triage is guesswork."""
    with capture_logs() as logs, start_run(store, "weekly") as ctx:
        rid = ctx.run_id
        ctx.claim("map", "org-1")
        ctx.fail("map", "org-1", "boom")
        ctx.record_not_reached("budget", "ran out")

    events = [e["event"] for e in logs]
    assert {"run_started", "item_failed", "not_reached", "run_finished"} <= set(events)
    for entry in logs:
        assert entry["run_id"] == rid, f"{entry['event']} has no run_id"
        assert entry["workflow"] == "weekly"


def test_failure_log_does_not_leak_a_whole_page_body(store: Store) -> None:
    """Errors get truncated; a 2 MB HTML body in a log line is how log files die."""
    with capture_logs() as logs, start_run(store, "weekly") as ctx:
        ctx.claim("fetch", "page-1")
        ctx.fail("fetch", "page-1", "x" * 50_000)
        rid = ctx.run_id

    failure = next(e for e in logs if e["event"] == "item_failed")
    assert len(failure["error"]) == 500

    stored = store.conn.execute(
        "SELECT error FROM stage_run WHERE run_id=? AND stage='fetch' AND item_key='page-1'",
        (rid,),
    ).fetchone()["error"]
    assert len(stored) == 2000


def test_the_harness_holds_a_store_not_a_connection(store: Store) -> None:
    """The architecture rule: no worker holds a cursor. Repositories own the SQL."""
    ctx = RunContext(run_id="r-1", workflow="weekly", store=store)
    assert ctx.store is store
    assert not hasattr(ctx, "conn")
