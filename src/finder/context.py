"""Run context: checkpointing, cost accounting and honest truncation reporting.

ADR-010. The predecessor system died without writing anything on four
consecutive firings. The fix is not "do less per run" — it is that every stage
is keyed and idempotent, so a killed run resumes from its last checkpoint
instead of starting over or losing what it had.

Three guarantees:

* **Resume.** ``claim()`` refuses an item that already reached a terminal state
  in this run. A process killed at item 47 restarts and continues at 47.
* **Isolation.** One bad page cannot take down a harvest of four hundred. A
  failure is recorded against the item and the loop continues.
* **Honesty.** Whenever work is truncated — by a budget, a timeout, a provider
  outage — ``not_reached`` records it, and the run report leads with it.

Typical use::

    store = Store(open_db(path))
    with start_run(store, "weekly", config_hash=cfg.hash) as run:
        for org in organizations:
            with run.item("map", org.org_id) as claimed:
                if not claimed:
                    continue
                ...
        run.count("orgs_mapped", len(organizations))

No SQL lives here. Every read and write goes through ``finder.store.repos``,
which is the only place raw SQL is allowed and the only place the founder-owned
tables are reachable.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import structlog

from finder.store.models import Run
from finder.store.repos import RUN_COUNTERS, TERMINAL_ITEM_STATES, Store

TERMINAL_STATES = TERMINAL_ITEM_STATES
COUNTERS = frozenset(RUN_COUNTERS)

# A stage_run.error column holds enough to diagnose; a log line holds less. A
# two-megabyte HTML body in either is how a log file becomes unreadable.
MAX_STORED_ERROR = 2000
MAX_LOGGED_ERROR = 500


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

    def as_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, "detail": self.detail, "count": self.count}


@dataclass(slots=True)
class CostLedger:
    """Per-run spend, so cost-per-good-route is computable and a spike is visible.

    Each charge is written when it is incurred rather than totalled at the end:
    a run that dies still leaves an accurate bill. Totals are read back from
    those rows, so a resumed run reports what the whole run cost — not what this
    process cost.
    """

    store: Store
    run_id: str

    def record(
        self, provider: str, operation: str, *, units: float = 1.0, usd: float = 0.0
    ) -> None:
        self.store.costs.record(
            f"cost-{uuid.uuid4().hex[:16]}",
            self.run_id,
            provider,
            operation,
            units=units,
            usd=usd,
        )

    @property
    def total_usd(self) -> float:
        return self.store.costs.total(self.run_id)

    def by_provider(self) -> dict[str, float]:
        return self.store.costs.by_provider(self.run_id)


@dataclass(slots=True)
class RunContext:
    """One execution of one workflow."""

    run_id: str
    workflow: str
    store: Store
    config_hash: str | None = None
    not_reached: list[NotReached] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)
    log: Any = field(init=False)
    cost: CostLedger = field(init=False)

    def __post_init__(self) -> None:
        self.cost = CostLedger(self.store, self.run_id)
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
        status = self.store.stage_runs.status(self.run_id, stage, item_key)
        if status in TERMINAL_STATES:
            return False
        self.store.stage_runs.start_item(self.run_id, stage, item_key)
        return True

    def complete(self, stage: str, item_key: str) -> None:
        self._finish_item(stage, item_key, "done", None)

    def fail(self, stage: str, item_key: str, error: str) -> None:
        """Record a failure against the item. Never raises past the item."""
        self._finish_item(stage, item_key, "failed", error[:MAX_STORED_ERROR])
        self.log.warning("item_failed", stage=stage, item=item_key, error=error[:MAX_LOGGED_ERROR])

    def skip(self, stage: str, item_key: str, reason: str) -> None:
        self._finish_item(stage, item_key, "skipped", reason[:MAX_STORED_ERROR])

    def _finish_item(self, stage: str, item_key: str, status: str, error: str | None) -> None:
        updated = self.store.stage_runs.finish_item(self.run_id, stage, item_key, status, error)
        if not updated:
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
        """The subset of items this run has not yet finished, in the given order."""
        done = self.store.stage_runs.finished_keys(self.run_id, stage)
        return [k for k in item_keys if k not in done]

    def stage_summary(self, stage: str) -> dict[str, int]:
        return self.store.stage_runs.summary(self.run_id, stage)

    # --- reporting --------------------------------------------------------

    def record_not_reached(self, reason: str, detail: str, count: int = 1) -> None:
        """Say what was left undone. Silence must not read as completeness.

        Written through to the run row, so a process that dies still leaves the
        record of what it never got to.
        """
        entry = NotReached(reason, detail, count)
        self.not_reached.append(entry)
        self.store.runs.append_not_reached(self.run_id, entry.as_dict())
        self.log.info("not_reached", reason=reason, detail=detail, count=count)

    def count(self, name: str, n: int = 1) -> None:
        """Record work done. Written through for the same reason as above."""
        if name not in COUNTERS:
            raise RunError(f"unknown counter {name!r}; expected one of {sorted(COUNTERS)}")
        self.counters[name] = self.counters.get(name, 0) + n
        self.store.runs.bump(self.run_id, name, n)

    def report(self) -> dict[str, Any]:
        """The run report. Truncation and failure come first, deliberately."""
        return {
            "run_id": self.run_id,
            "workflow": self.workflow,
            "not_reached": [n.as_dict() for n in self.not_reached],
            "counters": dict(self.counters),
            "cost_usd": self.cost.total_usd,
            "cost_by_provider": self.cost.by_provider(),
            "config_hash": self.config_hash,
        }

    def finish(self, status: str = "ok", error: str | None = None) -> None:
        self.store.runs.finish(
            self.run_id, status=status, error=error, cost_usd=self.cost.total_usd
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


@contextmanager
def _closing_run(ctx: RunContext) -> Iterator[RunContext]:
    """Close the run book however the block ends.

    A crash marks the run ``failed`` with the error attached rather than leaving
    it ``running`` forever — an abandoned run must be distinguishable from one
    still in flight, and a run that died reporting ``ok`` is the most dangerous
    lie the system can tell.
    """
    try:
        yield ctx
    except BaseException as exc:
        ctx.record_not_reached("run_aborted", f"{type(exc).__name__}: {exc}")
        ctx.finish("failed", f"{type(exc).__name__}: {exc}")
        raise
    else:
        ctx.finish("ok")


@contextmanager
def start_run(
    store: Store,
    workflow: str,
    *,
    config_hash: str | None = None,
    run_id: str | None = None,
) -> Iterator[RunContext]:
    """Open a new run."""
    rid = run_id or new_run_id(workflow)
    store.runs.start(rid, workflow, config_hash=config_hash)
    ctx = RunContext(run_id=rid, workflow=workflow, store=store, config_hash=config_hash)
    ctx.log.info("run_started", config_hash=config_hash)
    with _closing_run(ctx) as running:
        yield running


@contextmanager
def resume_run(store: Store, run_id: str) -> Iterator[RunContext]:
    """Re-open an existing run and continue where it stopped.

    Items already finished are refused by ``claim()``, so the caller loops over
    the whole work list again without reprocessing anything. Counters and
    ``not_reached`` are carried forward from the interrupted process, so the
    closing report covers the run rather than only its last attempt.
    """
    run = store.runs.get(run_id)
    if run is None:
        raise RunError(f"no such run: {run_id}")

    ctx = RunContext(
        run_id=run.run_id,
        workflow=run.workflow,
        store=store,
        config_hash=run.config_hash,
        counters={k: v for k, v in run.counters.items() if v},
        not_reached=[
            NotReached(n.get("reason", ""), n.get("detail", ""), n.get("count", 1))
            for n in run.not_reached
        ],
    )
    store.runs.reopen(run_id)
    ctx.log.info("run_resumed", originally_started=run.started_at)
    with _closing_run(ctx) as running:
        yield running


def last_run(store: Store, workflow: str | None = None) -> Run | None:
    return store.runs.last(workflow)


def unfinished_runs(store: Store) -> list[Run]:
    """Runs still marked ``running`` — a crashed process leaves one of these."""
    return store.runs.unfinished()
