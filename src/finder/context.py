"""Run context: checkpointing, cost accounting and honest truncation reporting.

ADR-010. The predecessor system died without writing anything on four
consecutive firings. The fix is not "do less per run" — it is that every stage
is keyed and idempotent, so a killed run resumes from its last checkpoint
instead of starting over or losing what it had.

Three guarantees:

* **Resume.** ``claim()`` refuses an item that already reached a terminal state
  in this run. A process killed at item 47 restarts and continues at 48.
* **Isolation.** One bad page cannot take down a harvest of four hundred. A
  failure is recorded against the item and the loop continues.
* **Honesty.** Whenever work is truncated — by a budget, a timeout, a provider
  outage — ``not_reached`` records it, and the run report leads with it.

Typical use::

    with start_run(conn, "weekly", config_hash=cfg.hash) as run:
        for org in organizations:
            with run.item("map", org.org_id) as claimed:
                if not claimed:
                    continue
                ...
        run.count("orgs_mapped", len(organizations))
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import structlog

from finder.store.db import utcnow

TERMINAL_STATES = frozenset({"done", "failed", "skipped"})

# Counters on the run row. Anything not here is a typo, and a typo that
# silently does nothing is how reporting quietly becomes fiction.
COUNTERS = frozenset(
    {
        "orgs_mapped",
        "pages_fetched",
        "candidates",
        "survived_gate",
        "survived_rerank",
        "routes_written",
        "quarantined",
    }
)


class RunError(Exception):
    """Raised when the run harness is used incorrectly."""


@dataclass(frozen=True, slots=True)
class NotReached:
    """One piece of work the run did not get to, and why.

    Never inferred. Something has to say it explicitly, which is the point —
    silence must not be readable as completeness.
    """

    reason: str
    detail: str
    count: int = 1


@dataclass(frozen=True, slots=True)
class CostEntry:
    provider: str
    operation: str
    units: float
    usd: float


@dataclass
class CostLedger:
    """Per-run spend, so cost-per-good-route is computable and a spike is visible.

    Recorded per call rather than totalled at the end: a run that dies still
    leaves an accurate record of what it spent.
    """

    conn: sqlite3.Connection
    run_id: str
    entries: list[CostEntry] = field(default_factory=list)

    def record(
        self, provider: str, operation: str, *, units: float = 1.0, usd: float = 0.0
    ) -> None:
        entry = CostEntry(provider, operation, units, usd)
        self.entries.append(entry)
        self.conn.execute(
            "INSERT INTO cost_event (cost_id, run_id, provider, operation, units, usd,"
            " recorded_at) VALUES (?,?,?,?,?,?,?)",
            (
                f"cost-{uuid.uuid4().hex[:16]}",
                self.run_id,
                provider,
                operation,
                units,
                usd,
                utcnow(),
            ),
        )

    @property
    def total_usd(self) -> float:
        return round(sum(e.usd for e in self.entries), 6)

    def by_provider(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for e in self.entries:
            out[e.provider] = round(out.get(e.provider, 0.0) + e.usd, 6)
        return out


@dataclass
class RunContext:
    """One execution of one workflow."""

    run_id: str
    workflow: str
    conn: sqlite3.Connection
    config_hash: str | None = None
    started_at: str = field(default_factory=utcnow)
    not_reached: list[NotReached] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)
    log: Any = field(init=False)
    cost: CostLedger = field(init=False)

    def __post_init__(self) -> None:
        self.cost = CostLedger(self.conn, self.run_id)
        # Bound once, so run_id and workflow ride on every line this run emits.
        self.log = structlog.get_logger("finder.run").bind(
            run_id=self.run_id, workflow=self.workflow
        )

    # --- checkpointing ----------------------------------------------------

    def claim(self, stage: str, item_key: str) -> bool:
        """Take ownership of one item. False when it is already finished.

        A row left ``running`` by a crashed process IS reclaimable — that is
        precisely the resume case. Only terminal states block a retry.
        """
        row = self.conn.execute(
            "SELECT status FROM stage_run WHERE run_id=? AND stage=? AND item_key=?",
            (self.run_id, stage, item_key),
        ).fetchone()

        if row is not None and row["status"] in TERMINAL_STATES:
            return False

        self.conn.execute(
            "INSERT INTO stage_run (run_id, stage, item_key, status, started_at)"
            " VALUES (?,?,?,'running',?)"
            " ON CONFLICT(run_id, stage, item_key) DO UPDATE SET"
            " status='running', started_at=excluded.started_at, error=NULL",
            (self.run_id, stage, item_key, utcnow()),
        )
        return True

    def complete(self, stage: str, item_key: str) -> None:
        self._finish_item(stage, item_key, "done", None)

    def fail(self, stage: str, item_key: str, error: str) -> None:
        """Record a failure against the item. Never raises past the item."""
        self._finish_item(stage, item_key, "failed", error[:2000])
        self.log.warning("item_failed", stage=stage, item=item_key, error=error[:500])

    def skip(self, stage: str, item_key: str, reason: str) -> None:
        self._finish_item(stage, item_key, "skipped", reason[:2000])

    def _finish_item(self, stage: str, item_key: str, status: str, error: str | None) -> None:
        cur = self.conn.execute(
            "UPDATE stage_run SET status=?, finished_at=?, error=?"
            " WHERE run_id=? AND stage=? AND item_key=?",
            (status, utcnow(), error, self.run_id, stage, item_key),
        )
        if cur.rowcount == 0:
            raise RunError(
                f"cannot mark {stage}/{item_key} as {status}: it was never claimed. "
                "Every item must go through claim() first."
            )

    @contextmanager
    def item(self, stage: str, item_key: str) -> Iterator[bool]:
        """Claim, run, and record the outcome.

        Yields False when the item is already finished — the caller skips it.
        An exception inside the block is recorded as a failure and swallowed,
        so one bad page cannot end the run.
        """
        if not self.claim(stage, item_key):
            yield False
            return
        try:
            yield True
        except Exception as exc:
            self.fail(stage, item_key, f"{type(exc).__name__}: {exc}")
        else:
            self.complete(stage, item_key)

    def pending(self, stage: str, item_keys: list[str]) -> list[str]:
        """The subset of items this run has not yet finished."""
        done = {
            r["item_key"]
            for r in self.conn.execute(
                "SELECT item_key FROM stage_run WHERE run_id=? AND stage=?"
                " AND status IN ('done','failed','skipped')",
                (self.run_id, stage),
            )
        }
        return [k for k in item_keys if k not in done]

    def stage_summary(self, stage: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) c FROM stage_run WHERE run_id=? AND stage=? GROUP BY status",
            (self.run_id, stage),
        ).fetchall()
        return {r["status"]: r["c"] for r in rows}

    # --- reporting --------------------------------------------------------

    def record_not_reached(self, reason: str, detail: str, count: int = 1) -> None:
        """Say what was left undone. Silence must not read as completeness."""
        self.not_reached.append(NotReached(reason, detail, count))
        self.log.info("not_reached", reason=reason, detail=detail, count=count)

    def count(self, name: str, n: int = 1) -> None:
        if name not in COUNTERS:
            raise RunError(f"unknown counter {name!r}; expected one of {sorted(COUNTERS)}")
        self.counters[name] = self.counters.get(name, 0) + n

    def report(self) -> dict[str, Any]:
        """The run report. Failures and truncation come first, deliberately."""
        return {
            "run_id": self.run_id,
            "workflow": self.workflow,
            "not_reached": [
                {"reason": n.reason, "detail": n.detail, "count": n.count} for n in self.not_reached
            ],
            "counters": dict(self.counters),
            "cost_usd": self.cost.total_usd,
            "cost_by_provider": self.cost.by_provider(),
            "config_hash": self.config_hash,
        }

    def finish(self, status: str = "ok", error: str | None = None) -> None:
        import json

        self.conn.execute(
            "UPDATE run SET finished_at=?, status=?, error=?, cost_usd=?, not_reached=?,"
            " orgs_mapped=?, pages_fetched=?, candidates=?, survived_gate=?,"
            " survived_rerank=?, routes_written=?, quarantined=? WHERE run_id=?",
            (
                utcnow(),
                status,
                error,
                self.cost.total_usd,
                json.dumps(
                    [
                        {"reason": n.reason, "detail": n.detail, "count": n.count}
                        for n in self.not_reached
                    ]
                ),
                *[
                    self.counters.get(c, 0)
                    for c in (
                        "orgs_mapped",
                        "pages_fetched",
                        "candidates",
                        "survived_gate",
                        "survived_rerank",
                        "routes_written",
                        "quarantined",
                    )
                ],
                self.run_id,
            ),
        )
        self.log.info(
            "run_finished",
            status=status,
            cost_usd=self.cost.total_usd,
            not_reached=len(self.not_reached),
            **self.counters,
        )


# --- lifecycle -------------------------------------------------------------


def new_run_id(workflow: str) -> str:
    """Sortable and human-legible: ``weekly-20260901T174233-a1b2c3``."""
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    return f"{workflow}-{stamp}-{uuid.uuid4().hex[:6]}"


def _insert_run(
    conn: sqlite3.Connection, run_id: str, workflow: str, config_hash: str | None
) -> None:
    conn.execute(
        "INSERT INTO run (run_id, workflow, started_at, status, config_hash)"
        " VALUES (?,?,?,'running',?)",
        (run_id, workflow, utcnow(), config_hash),
    )


@contextmanager
def start_run(
    conn: sqlite3.Connection,
    workflow: str,
    *,
    config_hash: str | None = None,
    run_id: str | None = None,
) -> Iterator[RunContext]:
    """Open a run, and close it honestly however it ends.

    A crash marks the run ``failed`` with the error attached rather than leaving
    it ``running`` forever — an abandoned run must be distinguishable from one
    still in flight.
    """
    rid = run_id or new_run_id(workflow)
    _insert_run(conn, rid, workflow, config_hash)
    ctx = RunContext(run_id=rid, workflow=workflow, conn=conn, config_hash=config_hash)
    ctx.log.info("run_started", config_hash=config_hash)
    try:
        yield ctx
    except BaseException as exc:
        ctx.record_not_reached("run_aborted", f"{type(exc).__name__}: {exc}")
        ctx.finish("failed", f"{type(exc).__name__}: {exc}")
        raise
    else:
        ctx.finish("ok")


@contextmanager
def resume_run(conn: sqlite3.Connection, run_id: str) -> Iterator[RunContext]:
    """Re-open an existing run and continue where it stopped.

    Items already ``done`` are refused by ``claim()``, so the caller can loop
    over the whole work list again without reprocessing anything.
    """
    row = conn.execute("SELECT * FROM run WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise RunError(f"no such run: {run_id}")

    ctx = RunContext(
        run_id=run_id,
        workflow=row["workflow"],
        conn=conn,
        config_hash=row["config_hash"],
        started_at=row["started_at"],
    )
    conn.execute("UPDATE run SET status='running', finished_at=NULL WHERE run_id=?", (run_id,))
    ctx.log.info("run_resumed", originally_started=row["started_at"])
    try:
        yield ctx
    except BaseException as exc:
        ctx.record_not_reached("run_aborted", f"{type(exc).__name__}: {exc}")
        ctx.finish("failed", f"{type(exc).__name__}: {exc}")
        raise
    else:
        ctx.finish("ok")


def last_run(conn: sqlite3.Connection, workflow: str | None = None) -> sqlite3.Row | None:
    if workflow:
        return conn.execute(
            "SELECT * FROM run WHERE workflow=? ORDER BY started_at DESC LIMIT 1", (workflow,)
        ).fetchone()
    return conn.execute("SELECT * FROM run ORDER BY started_at DESC LIMIT 1").fetchone()


def unfinished_runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Runs still marked ``running`` — a crashed process leaves one of these."""
    return conn.execute("SELECT * FROM run WHERE status='running' ORDER BY started_at").fetchall()
