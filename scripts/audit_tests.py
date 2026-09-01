#!/usr/bin/env python3
"""Mutation audit: break the code on purpose and check the tests notice.

Passing tests prove nothing on their own. A test that cannot fail is decoration,
and a suite of them is worse than no suite because it manufactures confidence.

This script applies a set of *specific, realistic* mutations — each one a bug a
competent engineer could plausibly introduce — runs the suite against each, and
reports any mutation that SURVIVES. A survivor is a real gap: the behaviour is
unprotected and either a test is missing or the code is untested dead weight.

This is targeted rather than exhaustive mutation testing. Exhaustive tools
(mutmut, cosmic-ray) generate thousands of mutants, most of them equivalent or
uninteresting, and the signal drowns. Twelve mutations that map to twelve real
failure modes tell you more, in thirty seconds, than a four-hour run does.

    python scripts/audit_tests.py            # audit
    python scripts/audit_tests.py --verbose  # show which tests caught each

Exit code 0 only if every mutation is caught.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Mutation:
    """One deliberate bug.

    ``why`` states the real-world failure this mutation simulates, so a survivor
    report explains what is actually unprotected rather than just naming a line.
    """

    name: str
    path: str
    old: str
    new: str
    why: str


MUTATIONS: list[Mutation] = [
    # --- provenance and dedupe -------------------------------------------
    Mutation(
        name="ids-truncated",
        path="src/finder/store/ids.py",
        old="_HASH_LEN = 12",
        new="_HASH_LEN = 1",
        why="Truncated ids collide, so unrelated routes silently become one record.",
    ),
    Mutation(
        name="ids-nondeterministic",
        path="src/finder/store/ids.py",
        old='joined = "\\x1f".join(p.strip().lower() for p in parts)',
        new="import random; joined = str(random.random())",
        why="Non-deterministic ids break replay idempotency: every re-run duplicates.",
    ),
    Mutation(
        name="empty-key-accepted",
        path="src/finder/store/ids.py",
        old='        raise ValueError("series_key is required to derive a route_id")',
        new="        pass",
        why="An empty natural key produces a shared id that swallows unrelated routes.",
    ),
    # --- upsert semantics -------------------------------------------------
    Mutation(
        name="first-seen-overwritten",
        path="src/finder/store/repos.py",
        old="                geo_scope = COALESCE(excluded.geo_scope, organization.geo_scope),",
        new="                first_seen = excluded.first_seen,\n"
        "                geo_scope = COALESCE(excluded.geo_scope, organization.geo_scope),",
        why="Overwriting first_seen loses when an organization was discovered.",
    ),
    Mutation(
        name="null-erases-known-value",
        path="src/finder/store/repos.py",
        old="                org_type = COALESCE(excluded.org_type, organization.org_type),",
        new="                org_type = excluded.org_type,",
        why="A thinner later extraction blanks out what an earlier one established.",
    ),
    Mutation(
        name="route-url-erased",
        path="src/finder/store/repos.py",
        old="                route_url = COALESCE(excluded.route_url, route.route_url),",
        new="                route_url = excluded.route_url,",
        why="A re-extraction that missed the form erases the only way in.",
    ),
    # --- founder-owned data ----------------------------------------------
    Mutation(
        name="founder-mark-overwritten",
        path="src/finder/store/repos.py",
        old=" ON CONFLICT(route_id, marked_at) DO NOTHING",
        new=" ON CONFLICT(route_id, marked_at) DO UPDATE SET verdict = excluded.verdict,"
        " note_freetext = excluded.note_freetext",
        why="The predecessor destroyed the founder's decisions exactly this way; "
        "he had to redo dispositions more than once.",
    ),
    # --- standing rejections ---------------------------------------------
    Mutation(
        name="rejection-ignores-family-scope",
        path="src/finder/store/repos.py",
        old="(family_scope = 'ALL' OR family_scope = ?)",
        new="(family_scope = 'ALL' OR family_scope <> ?)",
        why="Rejecting a room would also block the channel at the same organization.",
    ),
    Mutation(
        name="rejection-name-only",
        path="src/finder/store/repos.py",
        old='            "   OR (match_domain IS NOT NULL AND match_domain = ?))",',
        new="            \"   OR (match_domain IS NOT NULL AND match_domain = ''))\",",
        why="Name-only matching is why twelve rows of a permanently rejected "
        "organization survived in the predecessor under a variant name.",
    ),
    # --- evidence ---------------------------------------------------------
    Mutation(
        name="span-audit-inverted",
        path="src/finder/store/repos.py",
        old="            \" AND (span_text IS NULL OR span_match = 'absent')\",",
        new="            \" AND (span_text IS NOT NULL AND span_match <> 'absent')\",",
        why="The unsupported-field audit would report the opposite set, hiding "
        "exactly the fabricated fields it exists to surface.",
    ),
    # --- triggers ---------------------------------------------------------
    Mutation(
        name="trigger-strength-min",
        path="src/finder/store/repos.py",
        old='            "SELECT MAX(COALESCE(decayed_strength, 0.0)) s FROM trigger'
        ' WHERE employer_id = ?",',
        new='            "SELECT MIN(COALESCE(decayed_strength, 0.0)) s FROM trigger'
        ' WHERE employer_id = ?",',
        why="Taking the weakest trigger buries an employer with a live, strong one.",
    ),
    # --- transactions -----------------------------------------------------
    Mutation(
        name="transaction-commits-on-error",
        path="src/finder/store/db.py",
        old='        conn.execute("ROLLBACK")',
        new='        conn.execute("COMMIT")',
        why="A failed multi-table write leaves half a record behind.",
    ),
    Mutation(
        name="foreign-keys-off",
        path="src/finder/store/db.py",
        old='    conn.execute("PRAGMA foreign_keys = ON")',
        new='    conn.execute("PRAGMA foreign_keys = OFF")',
        why="SQLite disables FKs by default; orphan rows accumulate silently.",
    ),
    Mutation(
        name="migrations-not-idempotent",
        path="src/finder/store/db.py",
        old="        if version in already:\n            continue",
        new="        if False:\n            continue",
        why="Re-running migrations would fail or duplicate schema objects.",
    ),
    # --- config guard rails ----------------------------------------------
    Mutation(
        name="geography-guard-removed",
        path="src/finder/config.py",
        old='    {"geo_rank", "geography", "geo", "distance", "proximity", "travel", "drive_time"}',
        new="    set()",
        why="Geography could quietly become a scored dimension again, which is "
        "the exact ADR this project reversed.",
    ),
    Mutation(
        name="weights-need-not-sum",
        path="src/finder/config.py",
        old="            if total != 100:",
        new="            if total != -1:",
        why="Weights that do not sum to 100 silently distort every FIT score.",
    ),
    # --- dedupe keys ------------------------------------------------------
    Mutation(
        name="platform-host-ignored",
        path="src/finder/store/keys.py",
        old="    if domain in PLATFORM_HOSTS:",
        new="    if False:",
        why="Two unrelated organizations both cite glueup.com in the real data. "
        "Keying identity on a rented platform host merges strangers.",
    ),
    Mutation(
        name="chapter-qualifier-disabled",
        path="src/finder/store/keys.py",
        old='_CHAPTER_MARKERS = {"post", "chapter",',
        new='_CHAPTER_MARKERS = set() or {"nothing",',
        why="Nine SAME posts share same.org and twelve HFMA chapters share hfma.org. "
        "Without the place qualifier they collapse into one organization.",
    ),
    Mutation(
        name="council-treated-as-chapter-marker",
        path="src/finder/store/keys.py",
        old='"roundtable", "section", "branch", "affiliate"}',
        new='"roundtable", "section", "branch", "affiliate", "council"}',
        why="Splits 'SC Manufacturers Council' from 'SC Manufacturers & Commerce' — "
        "the exact bug that let a permanently rejected organization survive.",
    ),
    Mutation(
        name="entity-marker-disabled",
        path="src/finder/store/keys.py",
        old='_DISTINCT_ENTITY_MARKERS = {"foundation", "fund", "pac", "institute", "trust"}',
        new="_DISTINCT_ENTITY_MARKERS = set()",
        why="A chamber and its foundation are separate legal entities sharing one "
        "domain; without this marker they merge.",
    ),
    Mutation(
        name="generic-pages-not-collapsed",
        path="src/finder/store/keys.py",
        old="    if is_generic_program_page(mechanism):",
        new="    if False:",
        why="A 'Forums' page and an 'Events' page at one body become two routes.",
    ),
    Mutation(
        name="years-split-recurring-series",
        path="src/finder/store/keys.py",
        old='    without_years = _ORDINALS.sub(" ", _YEARS.sub(" ", text or ""))',
        new='    without_years = text or ""',
        why="'2nd Annual Summit' and '3rd Annual Summit' become different series "
        "instead of one recurring mechanism with two occurrences.",
    ),
    # --- secrets ----------------------------------------------------------
    Mutation(
        name="redaction-disabled",
        path="src/finder/logging.py",
        old="    for value in _SECRET_VALUES:",
        new="    for value in []:",
        why="API keys and the sheet bridge token would appear in log output.",
    ),
    Mutation(
        name="missing-keys-not-reported",
        path="src/finder/secrets.py",
        old="    missing = secrets.missing(*names)",
        new="    missing = []",
        why="A run starts, spends money, and dies halfway on a missing key.",
    ),
    # --- run harness: checkpointing, isolation and honest reporting -------
    Mutation(
        name="failed-item-retried",
        path="src/finder/store/repos.py",
        old='TERMINAL_ITEM_STATES: frozenset[str] = frozenset({"done", "failed", "skipped"})',
        new='TERMINAL_ITEM_STATES: frozenset[str] = frozenset({"done"})',
        why="A page that 404s is retried forever inside the same run, burning budget.",
    ),
    Mutation(
        name="mid-flight-item-stranded",
        path="src/finder/store/repos.py",
        old='TERMINAL_ITEM_STATES: frozenset[str] = frozenset({"done", "failed", "skipped"})',
        new="TERMINAL_ITEM_STATES: frozenset[str] = frozenset("
        '{"done", "failed", "skipped", "running"})',
        why="Treating 'running' as terminal strands whatever the crash interrupted: the "
        "item the process died on is never retried and the loss is invisible.",
    ),
    Mutation(
        name="checkpoint-not-consulted",
        path="src/finder/context.py",
        old="        if status in TERMINAL_STATES:",
        new="        if False:",
        why="Ignoring the checkpoint makes a resumed run redo every completed item, "
        "which is the cost blow-up checkpointing exists to prevent.",
    ),
    Mutation(
        name="unclaimed-completion-silent",
        path="src/finder/context.py",
        old="        if not updated:",
        new="        if False:",
        why="Bookkeeping that silently does nothing turns the run report into fiction.",
    ),
    Mutation(
        name="finish-item-always-claims-success",
        path="src/finder/store/repos.py",
        old="        return cur.rowcount > 0",
        new="        return True",
        why="A repo that reports success for an item it never touched hides the bug "
        "the caller's guard exists to catch.",
    ),
    Mutation(
        name="reclaim-keeps-stale-outcome",
        path="src/finder/store/repos.py",
        old='            " finished_at = NULL, error = NULL",',
        new='            " finished_at = finished_at, error = error",',
        why="A retried item carrying the previous attempt's error and finish stamp "
        "reads as failed-then-succeeded, and the report cannot be trusted.",
    ),
    Mutation(
        name="failure-recorded-as-success",
        path="src/finder/context.py",
        old='            self.fail(stage, item_key, f"{type(exc).__name__}: {exc}")',
        new="            self.complete(stage, item_key)",
        why="An item that crashed but is marked done is never retried and never "
        "reported: silent data loss dressed up as a clean run.",
    ),
    Mutation(
        name="one-bad-page-kills-the-run",
        path="src/finder/context.py",
        old="        except Exception as exc:",
        new="        except KeyboardInterrupt as exc:",
        why="Without per-item isolation one malformed page ends a harvest of four hundred.",
    ),
    Mutation(
        name="not-reached-dropped",
        path="src/finder/context.py",
        old="        self.store.runs.append_not_reached(self.run_id, entry.as_dict())",
        new="        pass",
        why="Dropping truncation lets silence read as completeness, which is how the "
        "predecessor reported success on runs that produced nothing.",
    ),
    Mutation(
        name="counters-lost-on-crash",
        path="src/finder/context.py",
        old="        self.store.runs.bump(self.run_id, name, n)",
        new="        pass",
        why="Counters totalled only at close vanish with the process that dies, and "
        "that is exactly the run whose numbers matter.",
    ),
    Mutation(
        name="unknown-counter-ignored",
        path="src/finder/context.py",
        old="        if name not in COUNTERS:",
        new="        if False:",
        why="A typo'd counter that silently no-ops reports real work as zero.",
    ),
    Mutation(
        name="counter-column-not-whitelisted",
        path="src/finder/store/repos.py",
        old="        if counter not in RUN_COUNTERS:",
        new="        if False:",
        why="The counter name is interpolated into SQL. Without the whitelist that is "
        "an injection point, not a convenience.",
    ),
    Mutation(
        name="cost-not-persisted",
        path="src/finder/store/repos.py",
        old='            "INSERT INTO cost_event (cost_id, run_id, provider, operation,'
        ' units, usd,"\n            " recorded_at) VALUES (?,?,?,?,?,?,?)",',
        new='            "SELECT ?,?,?,?,?,?,?",',
        why="Unpersisted spend makes cost-per-good-route uncomputable and hides a price spike.",
    ),
    Mutation(
        name="cost-not-scoped-to-the-run",
        path="src/finder/store/repos.py",
        old='            "SELECT COALESCE(SUM(usd), 0.0) s FROM cost_event WHERE run_id = ?",'
        " (run_id,)",
        new='            "SELECT COALESCE(SUM(usd), 0.0) s FROM cost_event WHERE ? IS NOT NULL",'
        " (run_id,)",
        why="Billing every run for every other run's spend makes the number meaningless.",
    ),
    Mutation(
        name="aborted-run-reported-ok",
        path="src/finder/context.py",
        old='        ctx.finish("failed", f"{type(exc).__name__}: {exc}")',
        new='        ctx.finish("ok")',
        why="A run that died reporting 'ok' is the most dangerous lie the system can "
        "tell, because nobody goes looking for the missing half.",
    ),
    Mutation(
        name="error-text-untruncated",
        path="src/finder/context.py",
        old="MAX_LOGGED_ERROR = 500",
        new="MAX_LOGGED_ERROR = 5_000_000",
        why="A two-megabyte HTML body in a log line is how log files become unreadable.",
    ),
    Mutation(
        name="raw-sql-guard-blind",
        path="tests/test_repos.py",
        old='RAW_SQL = re.compile(r"import sqlite3|\\bconn\\.execute(many)?\\(")',
        new='RAW_SQL = re.compile(r"this-will-never-match")',
        why="The architectural boundary is only real while its scanner can see a "
        "violation. A blind guard is worse than none: it reports compliance.",
    ),
]


def run_suite() -> tuple[bool, str]:
    """Run the suite against whatever is currently on disk. Returns (passed, output).

    Deliberately runs in ROOT rather than a copied tree. The package is
    installed editable (PEP 660), which resolves imports through a meta-path
    finder pointing at the real ``src/finder`` — so a copied tree's tests would
    import the ORIGINAL modules and every mutation would appear to survive.
    That was the first version of this script, and it reported a perfect score
    while testing nothing.
    """
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-x", "-q", "--no-header"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0, proc.stdout + proc.stderr


@contextmanager
def mutated(mut: Mutation) -> Iterator[bool]:
    """Apply a mutation in place, then restore it unconditionally.

    Yields False if the target text was not found, meaning the mutation is
    stale and is testing nothing.
    """
    target = ROOT / mut.path

    # Byte-level I/O throughout. read_text/write_text apply universal-newline
    # translation, which on Windows rewrites every LF as CRLF — restoring a
    # file byte-for-different but content-identical, silently rewriting the
    # whole file and destroying git blame. The first version of this script did
    # exactly that to six files.
    original = target.read_bytes()

    # Match against the file's actual line endings. Git on Windows checks out
    # CRLF by default, so a multi-line mutation written with \n would silently
    # never match and be reported as stale.
    crlf = b"\r\n" in original

    def encode(text: str) -> bytes:
        raw = text.encode("utf-8")
        return raw.replace(b"\n", b"\r\n") if crlf else raw

    old = encode(mut.old)
    if old not in original:
        yield False
        return

    try:
        target.write_bytes(original.replace(old, encode(mut.new), 1))
        yield True
    finally:
        target.write_bytes(original)
        if target.read_bytes() != original:  # pragma: no cover - filesystem failure
            raise RuntimeError(
                f"FAILED TO RESTORE {mut.path}. Run `git checkout -- {mut.path}` now."
            )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--only", help="run one mutation by name")
    args = ap.parse_args()

    mutations = [m for m in MUTATIONS if not args.only or m.name == args.only]
    if not mutations:
        sys.stderr.write(f"no mutation named {args.only!r}\n")
        return 2

    if subprocess.run(["git", "diff", "--quiet"], cwd=ROOT).returncode != 0:
        sys.stderr.write(
            "Working tree is dirty. This script edits source files in place and "
            "restores them; commit or stash first so a crash cannot lose work.\n"
        )
        return 2

    # Baseline: the suite must be green before any of this means anything.
    ok, out = run_suite()
    if not ok:
        sys.stderr.write("BASELINE FAILS — fix the suite before auditing it.\n")
        sys.stderr.write(out[-3000:])
        return 2
    print(f"baseline green · auditing {len(mutations)} mutations\n")

    survivors: list[Mutation] = []
    not_applied: list[Mutation] = []

    for i, mut in enumerate(mutations, 1):
        with mutated(mut) as applied:
            if not applied:
                not_applied.append(mut)
                print(f"  {i:2}. {mut.name:34} STALE — target text not found")
                continue

            passed, output = run_suite()
            caught = not passed  # the suite failing means the mutation was caught
            print(f"  {i:2}. {mut.name:34} {'caught ' if caught else 'SURVIVED'}")
            if not caught:
                survivors.append(mut)
            elif args.verbose:
                failing = [ln for ln in output.splitlines() if ln.startswith("FAILED")]
                for ln in failing[:3]:
                    print(f"        {ln}")

    print()
    if not_applied:
        print("STALE MUTATIONS — the code moved and these no longer apply:")
        for m in not_applied:
            print(f"  - {m.name} ({m.path})")
        print("  Update scripts/audit_tests.py so the audit keeps testing something real.\n")

    if survivors:
        print(f"{len(survivors)} MUTATION(S) SURVIVED — these behaviours are unprotected:\n")
        for m in survivors:
            print(f"  {m.name}")
            print(f"    {m.path}")
            print(f"    {m.why}\n")
        return 1

    if not_applied:
        return 1

    print(f"All {len(mutations)} mutations caught. The suite can fail.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
