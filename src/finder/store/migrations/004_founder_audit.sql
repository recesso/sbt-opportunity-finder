-- 004_founder_audit — every attempt to write founder-owned data.
--
-- The predecessor enforced "workers do not touch the founder's decisions" by
-- convention, and the write path actually in use bypassed it. He had to redo
-- dispositions more than once. Convention is not a control.
--
-- Both outcomes are recorded, not just the refusals. A log that only holds
-- violations cannot answer "when was this mark written and by what", which is
-- the question anyone debugging a lost decision actually asks.

CREATE TABLE founder_write_attempt (
    attempt_id TEXT PRIMARY KEY,
    at         TEXT NOT NULL,
    table_name TEXT NOT NULL,
    operation  TEXT NOT NULL,
    allowed    INTEGER NOT NULL CHECK (allowed IN (0,1)),
    caller     TEXT,                 -- module:function:line of the code that tried
    detail     TEXT
) STRICT;

CREATE INDEX ix_founder_attempt_at      ON founder_write_attempt(at);
CREATE INDEX ix_founder_attempt_allowed ON founder_write_attempt(allowed, at);
