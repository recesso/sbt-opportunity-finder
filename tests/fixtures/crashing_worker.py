"""A worker that dies abruptly, for the resume test.

Run as a subprocess. ``os._exit`` skips every ``finally`` block, every atexit
hook and every buffer flush — it is as close to a real crash as a test can get
without a signal. Simulating this with a mock would prove that the mock works.

    python crashing_worker.py <db_path> crash <run_id> <crash_after>
    python crashing_worker.py <db_path> resume <run_id>

The ``RESULT`` line is the machine-readable contract with the test; structlog
also writes to stdout, so the marker matters.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from finder.context import RunContext, resume_run, start_run
from finder.store.db import open_db

TOTAL_ITEMS = 100
STAGE = "process"


def work(ctx: RunContext, crash_after: int | None) -> None:
    processed: list[str] = []
    for i in range(TOTAL_ITEMS):
        key = f"item-{i:03d}"
        with ctx.item(STAGE, key) as claimed:
            if not claimed:
                continue
            processed.append(key)
            if crash_after is not None and len(processed) >= crash_after:
                os._exit(137)  # abrupt: no finally, no flush, no cleanup

    first = processed[0] if processed else ""
    last = processed[-1] if processed else ""
    print(f"RESULT processed={len(processed)} first={first} last={last}", flush=True)


def main() -> int:
    db_path, mode, run_id = sys.argv[1], sys.argv[2], sys.argv[3]
    conn = open_db(db_path)

    if mode == "crash":
        with start_run(conn, "test", run_id=run_id) as ctx:
            work(ctx, int(sys.argv[4]))
    else:
        with resume_run(conn, run_id) as ctx:
            work(ctx, None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
