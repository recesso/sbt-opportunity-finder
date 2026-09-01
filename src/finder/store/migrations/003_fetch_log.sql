-- 003_fetch_log — the URL-to-snapshot index behind the fetch cache.
--
-- Snapshots are addressed by content, which is right for the audit trail and
-- useless for answering "have I already fetched this page today?". This table
-- is the other half: one row per URL, pointing at the snapshot it produced.
--
-- Why it matters beyond saving money: a re-fetch that returns identical content
-- must NOT create a second snapshot or a second extraction. The predecessor
-- re-derived the same pages every run and accumulated 880 duplicate rows.
--
-- first_fetched_at is never updated. last_fetched_at moves. The pair is how a
-- page's stability over time becomes visible without a separate history table.

CREATE TABLE fetch_log (
    url              TEXT PRIMARY KEY,
    content_hash     TEXT NOT NULL,
    canonical_url    TEXT,
    status           INTEGER NOT NULL,
    is_pdf           INTEGER NOT NULL DEFAULT 0 CHECK (is_pdf IN (0,1)),
    provider         TEXT NOT NULL,
    links            TEXT NOT NULL DEFAULT '[]',
    first_fetched_at TEXT NOT NULL,
    last_fetched_at  TEXT NOT NULL,
    fetch_count      INTEGER NOT NULL DEFAULT 1,
    change_count     INTEGER NOT NULL DEFAULT 0
) STRICT;

CREATE INDEX ix_fetch_log_hash    ON fetch_log(content_hash);
CREATE INDEX ix_fetch_log_fetched ON fetch_log(last_fetched_at);
