-- 005_precision_decision — one row per candidate, kept or dropped.
--
-- "A wrongly dropped candidate must be diagnosable rather than invisible."
--
-- The predecessor's filtering became folklore precisely because drops left no
-- trace: nobody could answer "why did we never see the GSAE form?" except by
-- guessing. This table is the answer to that question, for every candidate,
-- including — especially — the ones that did not survive.
--
-- Keyed by (run_id, url) so a run can be replayed and compared against the last
-- one. Features are stored as JSON rather than columns because they are the
-- reranker's inputs and will change shape as the learning loop tunes them; the
-- decision and its reason will not.

CREATE TABLE precision_decision (
    decision_id TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    url         TEXT NOT NULL,
    org_id      TEXT REFERENCES organization(org_id),
    kept        INTEGER NOT NULL CHECK (kept IN (0,1)),
    stage       TEXT NOT NULL CHECK (stage IN ('gate','similarity','rerank','kept')),
    reason      TEXT NOT NULL,          -- machine-readable; never empty on a drop
    combo       TEXT,
    similarity  REAL,
    rerank_score REAL,
    features    TEXT NOT NULL DEFAULT '{}',
    decided_at  TEXT NOT NULL,
    UNIQUE (run_id, url)
) STRICT;

CREATE INDEX ix_decision_run    ON precision_decision(run_id, kept);
CREATE INDEX ix_decision_reason ON precision_decision(reason);
CREATE INDEX ix_decision_url    ON precision_decision(url);
