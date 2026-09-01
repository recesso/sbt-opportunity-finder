-- 002_stage_run — checkpointing (ADR-010).
--
-- The predecessor system died without writing anything on four consecutive
-- firings. The fix is architectural: every stage is keyed and idempotent, so a
-- killed run resumes from its last checkpoint rather than starting over or
-- losing what it had.
--
-- claim() inserts RUNNING and returns False if a DONE row already exists.
-- Failures are recorded per item and never raised past the item, so one bad
-- page cannot take down a harvest of four hundred.

CREATE TABLE stage_run (
    run_id      TEXT NOT NULL,
    stage       TEXT NOT NULL,
    item_key    TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('running','done','failed','skipped')),
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    error       TEXT,
    PRIMARY KEY (run_id, stage, item_key)
) STRICT;

CREATE INDEX ix_stage_run_status ON stage_run(run_id, stage, status);

-- Per-provider cost, so cost-per-good-route is computable (E12.S3) and a spend
-- spike is detectable (E13.S2) rather than discovered on an invoice.
CREATE TABLE cost_event (
    cost_id    TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL,
    provider   TEXT NOT NULL,
    operation  TEXT NOT NULL,
    units      REAL NOT NULL DEFAULT 1.0,
    usd        REAL NOT NULL DEFAULT 0.0,
    recorded_at TEXT NOT NULL
) STRICT;

CREATE INDEX ix_cost_run      ON cost_event(run_id);
CREATE INDEX ix_cost_provider ON cost_event(run_id, provider);
