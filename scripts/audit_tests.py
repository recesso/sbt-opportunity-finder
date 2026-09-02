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
    # Test files that should notice this mutation. Only a speed hint: a
    # mutation that survives them is re-run against the whole suite before it
    # is reported, so a wrong guess costs time and never correctness.
    tests: tuple[str, ...] = ()


MUTATIONS: list[Mutation] = [
    # --- provenance and dedupe -------------------------------------------
    Mutation(
        name="ids-truncated",
        path="src/finder/store/ids.py",
        old="_HASH_LEN = 12",
        new="_HASH_LEN = 1",
        why="Truncated ids collide, so unrelated routes silently become one record.",
        tests=("tests/test_repos.py",),
    ),
    Mutation(
        name="ids-nondeterministic",
        path="src/finder/store/ids.py",
        old='joined = "\\x1f".join(p.strip().lower() for p in parts)',
        new="import random; joined = str(random.random())",
        why="Non-deterministic ids break replay idempotency: every re-run duplicates.",
        tests=("tests/test_repos.py",),
    ),
    Mutation(
        name="empty-key-accepted",
        path="src/finder/store/ids.py",
        old='        raise ValueError("series_key is required to derive a route_id")',
        new="        pass",
        why="An empty natural key produces a shared id that swallows unrelated routes.",
        tests=("tests/test_repos.py",),
    ),
    # --- upsert semantics -------------------------------------------------
    Mutation(
        name="first-seen-overwritten",
        path="src/finder/store/repos.py",
        old="                geo_scope = COALESCE(excluded.geo_scope, organization.geo_scope),",
        new="                first_seen = excluded.first_seen,\n"
        "                geo_scope = COALESCE(excluded.geo_scope, organization.geo_scope),",
        why="Overwriting first_seen loses when an organization was discovered.",
        tests=("tests/test_repos.py", "tests/test_fetch.py"),
    ),
    Mutation(
        name="null-erases-known-value",
        path="src/finder/store/repos.py",
        old="                org_type = COALESCE(excluded.org_type, organization.org_type),",
        new="                org_type = excluded.org_type,",
        why="A thinner later extraction blanks out what an earlier one established.",
        tests=("tests/test_repos.py", "tests/test_fetch.py"),
    ),
    Mutation(
        name="route-url-erased",
        path="src/finder/store/repos.py",
        old="                route_url = COALESCE(excluded.route_url, route.route_url),",
        new="                route_url = excluded.route_url,",
        why="A re-extraction that missed the form erases the only way in.",
        tests=("tests/test_repos.py", "tests/test_fetch.py"),
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
        tests=("tests/test_repos.py", "tests/test_fetch.py"),
    ),
    # --- standing rejections ---------------------------------------------
    Mutation(
        name="rejection-ignores-family-scope",
        path="src/finder/store/repos.py",
        old="(family_scope = 'ALL' OR family_scope = ?)",
        new="(family_scope = 'ALL' OR family_scope <> ?)",
        why="Rejecting a room would also block the channel at the same organization.",
        tests=("tests/test_repos.py", "tests/test_fetch.py"),
    ),
    Mutation(
        name="rejection-name-only",
        path="src/finder/store/repos.py",
        old='            "   OR (match_domain IS NOT NULL AND match_domain = ?))",',
        new="            \"   OR (match_domain IS NOT NULL AND match_domain = ''))\",",
        why="Name-only matching is why twelve rows of a permanently rejected "
        "organization survived in the predecessor under a variant name.",
        tests=("tests/test_repos.py", "tests/test_fetch.py"),
    ),
    # --- evidence ---------------------------------------------------------
    Mutation(
        name="span-audit-inverted",
        path="src/finder/store/repos.py",
        old="            \" AND (span_text IS NULL OR span_match = 'absent')\",",
        new="            \" AND (span_text IS NOT NULL AND span_match <> 'absent')\",",
        why="The unsupported-field audit would report the opposite set, hiding "
        "exactly the fabricated fields it exists to surface.",
        tests=("tests/test_repos.py", "tests/test_fetch.py"),
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
        tests=("tests/test_repos.py", "tests/test_fetch.py"),
    ),
    # --- transactions -----------------------------------------------------
    Mutation(
        name="transaction-commits-on-error",
        path="src/finder/store/db.py",
        old='        conn.execute("ROLLBACK")',
        new='        conn.execute("COMMIT")',
        why="A failed multi-table write leaves half a record behind.",
        tests=("tests/test_migrations.py", "tests/test_repos.py"),
    ),
    Mutation(
        name="foreign-keys-off",
        path="src/finder/store/db.py",
        old='    conn.execute("PRAGMA foreign_keys = ON")',
        new='    conn.execute("PRAGMA foreign_keys = OFF")',
        why="SQLite disables FKs by default; orphan rows accumulate silently.",
        tests=("tests/test_migrations.py", "tests/test_repos.py"),
    ),
    Mutation(
        name="migrations-not-idempotent",
        path="src/finder/store/db.py",
        old="        if version in already:\n            continue",
        new="        if False:\n            continue",
        why="Re-running migrations would fail or duplicate schema objects.",
        tests=("tests/test_migrations.py", "tests/test_repos.py"),
    ),
    # --- config guard rails ----------------------------------------------
    Mutation(
        name="geography-guard-removed",
        path="src/finder/config.py",
        old='    {"geo_rank", "geography", "geo", "distance", "proximity", "travel", "drive_time"}',
        new="    set()",
        why="Geography could quietly become a scored dimension again, which is "
        "the exact ADR this project reversed.",
        tests=("tests/test_config.py",),
    ),
    Mutation(
        name="weights-need-not-sum",
        path="src/finder/config.py",
        old="            if total != 100:",
        new="            if total != -1:",
        why="Weights that do not sum to 100 silently distort every FIT score.",
        tests=("tests/test_config.py",),
    ),
    # --- dedupe keys ------------------------------------------------------
    Mutation(
        name="platform-host-ignored",
        path="src/finder/store/keys.py",
        old="    if domain in PLATFORM_HOSTS:",
        new="    if False:",
        why="Two unrelated organizations both cite glueup.com in the real data. "
        "Keying identity on a rented platform host merges strangers.",
        tests=("tests/test_keys.py",),
    ),
    Mutation(
        name="chapter-qualifier-disabled",
        path="src/finder/store/keys.py",
        old='_CHAPTER_MARKERS = {"post", "chapter",',
        new='_CHAPTER_MARKERS = set() or {"nothing",',
        why="Nine SAME posts share same.org and twelve HFMA chapters share hfma.org. "
        "Without the place qualifier they collapse into one organization.",
        tests=("tests/test_keys.py",),
    ),
    Mutation(
        name="council-treated-as-chapter-marker",
        path="src/finder/store/keys.py",
        old='"roundtable", "section", "branch", "affiliate"}',
        new='"roundtable", "section", "branch", "affiliate", "council"}',
        why="Splits 'SC Manufacturers Council' from 'SC Manufacturers & Commerce' — "
        "the exact bug that let a permanently rejected organization survive.",
        tests=("tests/test_keys.py",),
    ),
    Mutation(
        name="entity-marker-disabled",
        path="src/finder/store/keys.py",
        old='_DISTINCT_ENTITY_MARKERS = {"foundation", "fund", "pac", "institute", "trust"}',
        new="_DISTINCT_ENTITY_MARKERS = set()",
        why="A chamber and its foundation are separate legal entities sharing one "
        "domain; without this marker they merge.",
        tests=("tests/test_keys.py",),
    ),
    Mutation(
        name="generic-pages-not-collapsed",
        path="src/finder/store/keys.py",
        old="    if is_generic_program_page(mechanism):",
        new="    if False:",
        why="A 'Forums' page and an 'Events' page at one body become two routes.",
        tests=("tests/test_keys.py",),
    ),
    Mutation(
        name="years-split-recurring-series",
        path="src/finder/store/keys.py",
        old='    without_years = _ORDINALS.sub(" ", _YEARS.sub(" ", text or ""))',
        new='    without_years = text or ""',
        why="'2nd Annual Summit' and '3rd Annual Summit' become different series "
        "instead of one recurring mechanism with two occurrences.",
        tests=("tests/test_keys.py",),
    ),
    # --- secrets ----------------------------------------------------------
    Mutation(
        name="redaction-disabled",
        path="src/finder/logging.py",
        old="    for value in _SECRET_VALUES:",
        new="    for value in []:",
        why="API keys and the sheet bridge token would appear in log output.",
        tests=("tests/test_secrets.py",),
    ),
    Mutation(
        name="missing-keys-not-reported",
        path="src/finder/secrets.py",
        old="    missing = secrets.missing(*names)",
        new="    missing = []",
        why="A run starts, spends money, and dies halfway on a missing key.",
        tests=("tests/test_secrets.py",),
    ),
    # --- run harness: checkpointing, isolation and honest reporting -------
    Mutation(
        name="failed-item-retried",
        path="src/finder/store/repos.py",
        old='TERMINAL_ITEM_STATES: frozenset[str] = frozenset({"done", "failed", "skipped"})',
        new='TERMINAL_ITEM_STATES: frozenset[str] = frozenset({"done"})',
        why="A page that 404s is retried forever inside the same run, burning budget.",
        tests=("tests/test_repos.py", "tests/test_fetch.py"),
    ),
    Mutation(
        name="mid-flight-item-stranded",
        path="src/finder/store/repos.py",
        old='TERMINAL_ITEM_STATES: frozenset[str] = frozenset({"done", "failed", "skipped"})',
        new="TERMINAL_ITEM_STATES: frozenset[str] = frozenset("
        '{"done", "failed", "skipped", "running"})',
        why="Treating 'running' as terminal strands whatever the crash interrupted: the "
        "item the process died on is never retried and the loss is invisible.",
        tests=("tests/test_repos.py", "tests/test_fetch.py"),
    ),
    Mutation(
        name="checkpoint-not-consulted",
        path="src/finder/context.py",
        old="        if status in TERMINAL_STATES:",
        new="        if False:",
        why="Ignoring the checkpoint makes a resumed run redo every completed item, "
        "which is the cost blow-up checkpointing exists to prevent.",
        tests=("tests/test_context.py", "tests/test_fetch.py"),
    ),
    Mutation(
        name="unclaimed-completion-silent",
        path="src/finder/context.py",
        old="        if not updated:",
        new="        if False:",
        why="Bookkeeping that silently does nothing turns the run report into fiction.",
        tests=("tests/test_context.py", "tests/test_fetch.py"),
    ),
    Mutation(
        name="finish-item-always-claims-success",
        path="src/finder/store/repos.py",
        old="        return cur.rowcount > 0",
        new="        return True",
        why="A repo that reports success for an item it never touched hides the bug "
        "the caller's guard exists to catch.",
        tests=("tests/test_repos.py", "tests/test_fetch.py"),
    ),
    Mutation(
        name="reclaim-keeps-stale-outcome",
        path="src/finder/store/repos.py",
        old='            " finished_at = NULL, error = NULL",',
        new='            " finished_at = finished_at, error = error",',
        why="A retried item carrying the previous attempt's error and finish stamp "
        "reads as failed-then-succeeded, and the report cannot be trusted.",
        tests=("tests/test_repos.py", "tests/test_fetch.py"),
    ),
    Mutation(
        name="failure-recorded-as-success",
        path="src/finder/context.py",
        old='            self.fail(stage, item_key, f"{type(exc).__name__}: {exc}")',
        new="            self.complete(stage, item_key)",
        why="An item that crashed but is marked done is never retried and never "
        "reported: silent data loss dressed up as a clean run.",
        tests=("tests/test_context.py", "tests/test_fetch.py"),
    ),
    Mutation(
        name="one-bad-page-kills-the-run",
        path="src/finder/context.py",
        old="        except Exception as exc:",
        new="        except KeyboardInterrupt as exc:",
        why="Without per-item isolation one malformed page ends a harvest of four hundred.",
        tests=("tests/test_context.py", "tests/test_fetch.py"),
    ),
    Mutation(
        name="not-reached-dropped",
        path="src/finder/context.py",
        old="        self.store.runs.append_not_reached(self.run_id, entry.as_dict())",
        new="        pass",
        why="Dropping truncation lets silence read as completeness, which is how the "
        "predecessor reported success on runs that produced nothing.",
        tests=("tests/test_context.py", "tests/test_fetch.py"),
    ),
    Mutation(
        name="counters-lost-on-crash",
        path="src/finder/context.py",
        old="        self.store.runs.bump(self.run_id, name, n)",
        new="        pass",
        why="Counters totalled only at close vanish with the process that dies, and "
        "that is exactly the run whose numbers matter.",
        tests=("tests/test_context.py", "tests/test_fetch.py"),
    ),
    Mutation(
        name="unknown-counter-ignored",
        path="src/finder/context.py",
        old="        if name not in COUNTERS:",
        new="        if False:",
        why="A typo'd counter that silently no-ops reports real work as zero.",
        tests=("tests/test_context.py", "tests/test_fetch.py"),
    ),
    Mutation(
        name="counter-column-not-whitelisted",
        path="src/finder/store/repos.py",
        old="        if counter not in RUN_COUNTERS:",
        new="        if False:",
        why="The counter name is interpolated into SQL. Without the whitelist that is "
        "an injection point, not a convenience.",
        tests=("tests/test_repos.py", "tests/test_fetch.py"),
    ),
    Mutation(
        name="cost-not-persisted",
        path="src/finder/store/repos.py",
        old='            "INSERT INTO cost_event (cost_id, run_id, provider, operation,'
        ' units, usd,"\n            " recorded_at) VALUES (?,?,?,?,?,?,?)",',
        new='            "SELECT ?,?,?,?,?,?,?",',
        why="Unpersisted spend makes cost-per-good-route uncomputable and hides a price spike.",
        tests=("tests/test_repos.py", "tests/test_fetch.py"),
    ),
    Mutation(
        name="cost-not-scoped-to-the-run",
        path="src/finder/store/repos.py",
        old='            "SELECT COALESCE(SUM(usd), 0.0) s FROM cost_event WHERE run_id = ?",'
        " (run_id,)",
        new='            "SELECT COALESCE(SUM(usd), 0.0) s FROM cost_event WHERE ? IS NOT NULL",'
        " (run_id,)",
        why="Billing every run for every other run's spend makes the number meaningless.",
        tests=("tests/test_repos.py", "tests/test_fetch.py"),
    ),
    Mutation(
        name="aborted-run-reported-ok",
        path="src/finder/context.py",
        old='        ctx.finish("failed", f"{type(exc).__name__}: {exc}")',
        new='        ctx.finish("ok")',
        why="A run that died reporting 'ok' is the most dangerous lie the system can "
        "tell, because nobody goes looking for the missing half.",
        tests=("tests/test_context.py", "tests/test_fetch.py"),
    ),
    Mutation(
        name="error-text-untruncated",
        path="src/finder/context.py",
        old="MAX_LOGGED_ERROR = 500",
        new="MAX_LOGGED_ERROR = 5_000_000",
        why="A two-megabyte HTML body in a log line is how log files become unreadable.",
        tests=("tests/test_context.py", "tests/test_fetch.py"),
    ),
    Mutation(
        name="raw-sql-guard-blind",
        path="tests/test_repos.py",
        old='RAW_SQL = re.compile(r"import sqlite3|\\bconn\\.execute(many)?\\(")',
        new='RAW_SQL = re.compile(r"this-will-never-match")',
        why="The architectural boundary is only real while its scanner can see a "
        "violation. A blind guard is worse than none: it reports compliance.",
        tests=("tests/test_repos.py",),
    ),
    # --- extraction contract ----------------------------------------------
    Mutation(
        name="span-not-required",
        path="src/finder/extract/schemas.py",
        old="        if stated and not has_span:",
        new="        if False:",
        why="Without the span rule a model can assert anything about a page and the "
        "record is indistinguishable from an extracted one. This is THE rule.",
        tests=("tests/test_schemas.py",),
    ),
    Mutation(
        name="whitespace-counts-as-a-span",
        path="src/finder/extract/schemas.py",
        old='        has_span = bool((self.span or "").strip())',
        new="        has_span = self.span is not None",
        why="A span of three spaces satisfies the letter of the rule and supports "
        "nothing, which is exactly how an evidence trail rots.",
        tests=("tests/test_schemas.py",),
    ),
    Mutation(
        name="not-stated-may-carry-a-span",
        path="src/finder/extract/schemas.py",
        old="        if not stated and has_span:",
        new="        if False:",
        why="A quote attached to 'not stated' is a contradiction, and a contradictory "
        "evidence trail is worse than a gap.",
        tests=("tests/test_schemas.py",),
    ),
    Mutation(
        name="extra-keys-allowed",
        path="src/finder/extract/schemas.py",
        old='    model_config = ConfigDict(frozen=True, extra="forbid")\n\n\n# --- common',
        new='    model_config = ConfigDict(frozen=True, extra="allow")\n\n\n# --- common',
        why="An invented field is an invented claim; allowing extras lets one through "
        "silently instead of failing the record.",
        tests=("tests/test_schemas.py",),
    ),
    Mutation(
        name="malformed-output-not-retried",
        path="src/finder/extract/schemas.py",
        old="            continue\n        return record",
        new="            break\n        return record",
        why="One bad answer quarantines a page that a single retry with the error "
        "would have fixed, and real yield drops for no reason.",
        tests=("tests/test_schemas.py",),
    ),
    Mutation(
        name="retry-feedback-is-generic",
        path="src/finder/extract/schemas.py",
        old='                "Your previous answer was rejected. Fix exactly these problems "\n'
        '                "and return the whole object again:\\n- " + "\\n- ".join(errors)',
        new='                "Your previous answer was rejected. Try again."',
        why="A retry that does not name the violation is a coin flip. The errors are "
        "the only thing that makes the second attempt better than the first.",
        tests=("tests/test_schemas.py",),
    ),
    Mutation(
        name="quarantine-becomes-repair",
        path="src/finder/extract/schemas.py",
        old="    return Quarantined(family=family, attempts=max_attempts, errors=errors, raw=raw)",
        new="    return SCHEMAS[family].model_construct(**(raw if isinstance(raw, dict) else {}))",
        why="model_construct skips validation. This is the exact shape of 'coerce it "
        "into a record so the pipeline keeps moving', which the contract forbids.",
        tests=("tests/test_schemas.py",),
    ),
    Mutation(
        name="quarantine-drops-the-evidence",
        path="src/finder/extract/schemas.py",
        old="errors=errors, raw=raw)",
        new="errors=[], raw=None)",
        why="A quarantine that throws away the payload and the reasons teaches nothing "
        "and cannot be debugged.",
        tests=("tests/test_schemas.py",),
    ),
    Mutation(
        name="impossible-dates-accepted",
        path="src/finder/extract/schemas.py",
        old="            date.fromisoformat(str(field.value))",
        new="            pass",
        why="2026-02-31 passes the pattern and fails the calendar. It looks usable, "
        "which is worse than missing.",
        tests=("tests/test_schemas.py",),
    ),
    Mutation(
        name="trigger-dates-unchecked",
        path="src/finder/extract/schemas.py",
        old="            date.fromisoformat(trigger.occurred_on)",
        new="            pass",
        why="The EMPLOYER family is built on recency; an uncheckable trigger date "
        "makes the decay meaningless.",
        tests=("tests/test_schemas.py",),
    ),
    Mutation(
        name="employer-route-without-a-trigger",
        path="src/finder/extract/schemas.py",
        old="        if not self.triggers:",
        new="        if False:",
        why="With nothing that changed there is no reason to call. The family's whole "
        "premise is that something just happened.",
        tests=("tests/test_schemas.py",),
    ),
    Mutation(
        name="route-type-not-constrained",
        path="src/finder/extract/schemas.py",
        old="    route_type: Field[RoomRouteType]",
        new="    route_type: Field[Any]",
        why="An unconstrained route_type lets the model answer with a type that has no "
        "score, and the route silently ranks at zero.",
        tests=("tests/test_schemas.py",),
    ),
    Mutation(
        name="founder-field-exposed",
        path="src/finder/extract/schemas.py",
        old="    connector: Field[NonEmpty]\n    role_change: RoleChange",
        new="    connector: Field[NonEmpty]\n    known_to_art: Field[NonEmpty]\n"
        "    role_change: RoleChange",
        why="known_to_art is the founder's answer. A slot in the schema is an "
        "invitation for a model to guess it, and the guess would outrank the truth.",
        tests=("tests/test_schemas.py",),
    ),
    Mutation(
        name="non-json-response-crashes",
        path="src/finder/extract/schemas.py",
        old="        except json.JSONDecodeError as exc:",
        new="        except KeyboardInterrupt as exc:",
        why="A model that answers in prose must be a violation the retry can handle, "
        "not an exception that ends the run.",
        tests=("tests/test_schemas.py",),
    ),
    Mutation(
        name="prompt-clause-dropped",
        path="src/finder/extract/schemas.py",
        old='    "Never treat a past cycle as a current open one.",',
        new="",
        why="Each clause is a failure the predecessor actually made. Dropping one is "
        "how last year's closed call comes back as this year's opportunity.",
        tests=("tests/test_schemas.py",),
    ),
    # --- acquisition: snapshots and the fetch cache ------------------------
    Mutation(
        name="snapshot-overwritten",
        path="src/finder/acquire/snapshot.py",
        old="        if path.exists():",
        new="        if False:",
        why="Rewriting a stored snapshot rewrites history, and history is the only "
        "thing the store exists for. Every past extraction becomes unverifiable.",
        tests=("tests/test_snapshot.py", "tests/test_fetch.py"),
    ),
    Mutation(
        name="snapshot-hash-ignores-whitespace-rule",
        path="src/finder/acquire/snapshot.py",
        old='    return _WHITESPACE.sub(" ", text).strip()',
        new="    return text",
        why="Providers re-wrap markdown between calls. Hashing raw bytes stores the "
        "same page a dozen times and defeats both the cache and the audit.",
        tests=("tests/test_snapshot.py", "tests/test_fetch.py"),
    ),
    Mutation(
        name="snapshot-key-unvalidated",
        path="src/finder/acquire/snapshot.py",
        old="        if not _HASH_RE.match(digest):",
        new="        if False:",
        why="A snapshot is addressed by content, never by a name someone chose. An "
        "unvalidated key is also a path-traversal write into the data directory.",
        tests=("tests/test_snapshot.py", "tests/test_fetch.py"),
    ),
    Mutation(
        name="missing-snapshot-reads-as-empty",
        path="src/finder/acquire/snapshot.py",
        old="        if not path.exists():",
        new="        if False:",
        why="An empty string reads like a page that said nothing, and the extractor "
        "would faithfully record not_stated for a page it never saw.",
        tests=("tests/test_snapshot.py", "tests/test_fetch.py"),
    ),
    Mutation(
        name="snapshot-write-not-atomic",
        path="src/finder/acquire/snapshot.py",
        old="            os.replace(tmp, path)",
        new="            path.write_bytes(tmp.read_bytes()[:-1])",
        why="A process killed mid-write must leave nothing or a whole file, never a "
        "truncated archive that reads as a valid but shorter page.",
        tests=("tests/test_snapshot.py", "tests/test_fetch.py"),
    ),
    Mutation(
        name="cache-never-hits",
        path="src/finder/acquire/fetch.py",
        old="        cached = self._from_cache(url, max_age)",
        new="        cached = None",
        why="Re-fetching every page every run is what the cache exists to prevent; "
        "the bill and the rate limit both notice.",
        tests=("tests/test_fetch.py",),
    ),
    Mutation(
        name="cache-ignores-age",
        path="src/finder/acquire/fetch.py",
        old="        if record is None or age_seconds(record.last_fetched_at) > max_age_s:",
        new="        if record is None:",
        why="A cache with no expiry serves last year's page forever, and a deadline "
        "that appeared on Tuesday is never seen.",
        tests=("tests/test_fetch.py",),
    ),
    Mutation(
        name="cache-hit-without-the-bytes",
        path="src/finder/acquire/fetch.py",
        old="        if not self.snapshots.has(record.content_hash):",
        new="        if False:",
        why="If the index outlives the bytes, a hit raises instead of re-fetching and "
        "the page becomes permanently unreadable.",
        tests=("tests/test_fetch.py",),
    ),
    Mutation(
        name="future-timestamp-reads-as-fresh",
        path="src/finder/acquire/fetch.py",
        old="    return max(0.0, (current - then).total_seconds())",
        new="    return (current - then).total_seconds()",
        why="Clock skew produces a negative age, which makes a stale page look "
        "permanently fresh and it is never re-read.",
        tests=("tests/test_fetch.py",),
    ),
    Mutation(
        name="failed-call-never-billed",
        path="src/finder/acquire/fetch.py",
        old="            self.stats.failures += 1\n            self._charge(run, cost_usd)",
        new="            self.stats.failures += 1",
        why="A ledger that counts only successes understates the bill exactly when "
        "things are going wrong and spend is spiking.",
        tests=("tests/test_fetch.py",),
    ),
    Mutation(
        name="snapshot-indexed-before-it-is-stored",
        path="src/finder/acquire/fetch.py",
        old="        self.snapshots.put(snapshot.markdown)\n        self.store.fetch_log.record(",
        new="        self.store.fetch_log.record(",
        why="Extraction reads only from the store, so an index entry pointing at bytes "
        "that were never written is a page that cannot be read.",
        tests=("tests/test_fetch.py",),
    ),
    Mutation(
        name="empty-page-accepted",
        path="src/finder/acquire/providers/firecrawl.py",
        old="        if not markdown.strip():",
        new="        if False:",
        why="A blank snapshot extracts cleanly as 'the page states nothing', which is "
        "indistinguishable from a real thin page and is a lie about a failed fetch.",
        tests=("tests/test_fetch.py",),
    ),
    Mutation(
        name="permanent-failures-retried",
        path="src/finder/acquire/providers/firecrawl.py",
        old="                if not exc.retryable:",
        new="                if False:",
        why="Retrying a 404 three times burns budget and hides the answer.",
        tests=("tests/test_fetch.py",),
    ),
    Mutation(
        name="retries-unbounded",
        path="src/finder/acquire/providers/firecrawl.py",
        old="                if attempt < self.max_attempts:",
        new="                if True:",
        why="An unbounded retry against a rate limiter turns one bad page into a stalled run.",
        tests=("tests/test_fetch.py",),
    ),
    Mutation(
        name="rate-limit-not-retryable",
        path="src/finder/acquire/providers/firecrawl.py",
        old="RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})",
        new="RETRYABLE_STATUS = frozenset({500})",
        why="A 429 is the single most common transient failure; not retrying it drops "
        "pages that were one short wait away.",
        tests=("tests/test_fetch.py",),
    ),
    Mutation(
        name="resolved-url-collapsed-into-requested",
        path="src/finder/acquire/providers/firecrawl.py",
        old='canonical_url=str(metadata.get("sourceURL") or url),',
        new="canonical_url=url,",
        why="Conflating where a page resolved with what was requested is how one page "
        "ends up in the database three times, under three organizations.",
        tests=("tests/test_fetch.py",),
    ),
    Mutation(
        name="unchanged-refetch-counted-as-change",
        path="src/finder/store/repos.py",
        old="     + (CASE WHEN fetch_log.content_hash = excluded.content_hash THEN 0 ELSE 1 END)",
        new="     + 1",
        why="Counting a re-read as a change makes every page look volatile and destroys "
        "the only stability signal the fetch log carries.",
        tests=("tests/test_repos.py", "tests/test_fetch.py"),
    ),
    Mutation(
        name="first-fetch-timestamp-moves",
        path="src/finder/store/repos.py",
        old='            "   last_fetched_at = excluded.last_fetched_at,"',
        new='            "   first_fetched_at = excluded.first_fetched_at,'
        ' last_fetched_at = excluded.last_fetched_at,"',
        why="first_fetched_at is the anchor for how long a page has been known; moving "
        "it makes every page look newly discovered.",
        tests=("tests/test_repos.py", "tests/test_fetch.py"),
    ),
    # --- URL inventory ------------------------------------------------------
    Mutation(
        name="matched-term-not-recorded",
        path="src/finder/acquire/map.py",
        old="                matched_term=matched[0],",
        new='                matched_term="",',
        why="Which term matched IS the signal. A URL found by 'call for speakers' is "
        "a different animal from one found by 'blog', and the reranker has nothing "
        "to work from without it.",
        tests=("tests/test_map.py",),
    ),
    Mutation(
        name="weakest-term-reported",
        path="src/finder/acquire/map.py",
        old="                candidate = (-len(needle), order, term, where)",
        new="                candidate = (len(needle), order, term, where)",
        why="Reporting 'speak' for a page found by 'call for speakers' understates "
        "the strongest evidence on the page.",
        tests=("tests/test_map.py",),
    ),
    Mutation(
        name="term-matches-inside-a-word",
        path="src/finder/acquire/map.py",
        old='            if f" {needle} " in hay:',
        new="            if needle in hay:",
        why="'council' would fire on 'councilman' and every civic page becomes a "
        "council seat. Substring matching is exactly how the predecessor's recall "
        "filled with noise.",
        tests=("tests/test_map.py",),
    ),
    Mutation(
        name="hyphens-not-normalised",
        path="src/finder/acquire/map.py",
        old='_SEPARATORS = re.compile(r"[-_/+.,=&?:;~%#|]+")',
        new='_SEPARATORS = re.compile(r"[/]+")',
        why="'call-for-speakers' would stop matching 'call for speakers', which is "
        "the single most valuable term in the list.",
        tests=("tests/test_map.py",),
    ),
    Mutation(
        name="query-string-not-searched",
        path="src/finder/acquire/map.py",
        old='    haystacks = (("path", _searchable(f"{parts.path} {parts.query}")),)',
        new='    haystacks = (("path", _searchable(parts.path)),)',
        why="On an AMS host the query string carries the section, so committee and "
        "event pages stop matching entirely.",
        tests=("tests/test_map.py",),
    ),
    Mutation(
        name="tracking-params-kept",
        path="src/finder/acquire/map.py",
        old="        if pair and not pair.lower().startswith(_TRACKING_PREFIXES)",
        new="        if pair",
        why="The same page arriving from a newsletter and from search becomes two "
        "URLs, two fetches and two routes.",
        tests=("tests/test_map.py",),
    ),
    Mutation(
        name="meaningful-query-discarded",
        path="src/finder/acquire/map.py",
        old='    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))',
        new='    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))',
        why="On an AMS host the query string IS the address of the event page; "
        "dropping it collapses every event onto one URL.",
        tests=("tests/test_map.py",),
    ),
    Mutation(
        name="duplicate-urls-kept",
        path="src/finder/acquire/map.py",
        old="        if not is_fetchable(url) or url in seen:",
        new="        if not is_fetchable(url):",
        why="One page under three URLs is three fetches and three candidate routes, "
        "which is how the predecessor accumulated 880 duplicates.",
        tests=("tests/test_map.py",),
    ),
    Mutation(
        name="fallback-never-runs",
        path="src/finder/acquire/map.py",
        old="            except FetchError as exc:",
        new="            except FetchError as exc:\n                raise exc",
        why="A provider outage would take out every domain in the run instead of "
        "falling back to the sitemap.",
        tests=("tests/test_map.py",),
    ),
    Mutation(
        name="unmappable-domain-reads-as-empty",
        path="src/finder/acquire/map.py",
        old='            run.record_not_reached("map_failed", f"{domain}: " + "; ".join(failures))',
        new="            pass",
        why="A domain nobody could map is not a domain with nothing on it, and the "
        "run report must not let the two look the same.",
        tests=("tests/test_map.py",),
    ),
    Mutation(
        name="sitemap-index-not-followed",
        path="src/finder/acquire/map.py",
        old='        if _tag(root) != "sitemapindex":',
        new="        if True:",
        why="Large association sites publish only an index; not following it means "
        "the fallback returns nothing exactly where it is most needed.",
        tests=("tests/test_map.py",),
    ),
    Mutation(
        name="sitemap-recursion-unbounded",
        path="src/finder/acquire/map.py",
        old="        if depth > 0:",
        new="        if False:",
        why="A self-referencing index is common and would loop until the run dies.",
        tests=("tests/test_map.py",),
    ),
    Mutation(
        name="gzipped-sitemap-not-decompressed",
        path="src/finder/acquire/map.py",
        old='        if url.endswith(".gz") or content[:2] == b"\\x1f\\x8b":',
        new="        if False:",
        why="Gzipped sitemaps are common on association hosts; unread, the fallback "
        "silently finds nothing there.",
        tests=("tests/test_map.py",),
    ),
    Mutation(
        name="assets-fetched",
        path="src/finder/acquire/map.py",
        old='    return bool(url) and not url.lower().split("?")[0].endswith(_SKIP_SUFFIXES)',
        new="    return bool(url)",
        why="Fetching CSS and images costs a call each and extracts nothing.",
        tests=("tests/test_map.py",),
    ),
    Mutation(
        name="map-limit-ignored",
        path="src/finder/acquire/map.py",
        old="        if len(hits) >= limit:",
        new="        if False:",
        why="An unbounded inventory on a large site turns one domain into thousands of fetches.",
        tests=("tests/test_map.py",),
    ),
    # --- W1 network registration -------------------------------------------
    Mutation(
        name="planning-estimate-written-as-fact",
        path="src/finder/harvest/w1_registry.py",
        old="                node_count_actual=result.actual,",
        new="                node_count_actual=network.node_count_est,",
        why="node_count_est is an order-of-magnitude planning figure. Written as data "
        "it stops being one the moment anything reads it back, and the config's own "
        "warning becomes decoration.",
        tests=("tests/test_w1.py",),
    ),
    Mutation(
        name="implausible-yield-not-reported",
        path="src/finder/harvest/w1_registry.py",
        old="        if result.actual >= estimate * IMPLAUSIBLE_YIELD_RATIO:",
        new="        if True:",
        why="Three nodes against an estimate of fifty-one means the extraction broke. "
        "Reporting success there is how a whole network silently disappears from the "
        "harvest with nobody noticing.",
        tests=("tests/test_w1.py",),
    ),
    Mutation(
        name="network-is-its-own-member",
        path="src/finder/harvest/w1_registry.py",
        old="        if domain and domain == parent:",
        new="        if False:",
        why="A directory links to itself constantly. Registering NIST as a member of "
        "the NIST network pollutes every downstream count.",
        tests=("tests/test_w1.py",),
    ),
    Mutation(
        name="duplicate-members-registered-twice",
        path="src/finder/harvest/w1_registry.py",
        old="        if domain in seen:\n            continue",
        new="        if False:\n            continue",
        why="Directories link the same member by name and again by logo. Two nodes for "
        "one organization is the duplicate problem this project exists to end.",
        tests=("tests/test_w1.py",),
    ),
    Mutation(
        name="generic-anchors-become-organizations",
        path="src/finder/harvest/w1_registry.py",
        old="    if len(cleaned) < _MIN_NAME_LEN or cleaned.lower() in _GENERIC_ANCHORS:",
        new="    if False:",
        why="'Learn more' and 'here' would become organizations, each costing a map "
        "call and appearing in the registry as a real body.",
        tests=("tests/test_w1.py",),
    ),
    Mutation(
        name="hostless-domain-accepted",
        path="src/finder/harvest/w1_registry.py",
        old='    if not domain or "." not in domain or domain in _CHROME_DOMAINS:',
        new="    if not domain:",
        why="`https://intranet/members` becomes an organization called 'intranet' that "
        "nothing can ever fetch, and social links become member bodies.",
        tests=("tests/test_w1.py",),
    ),
    Mutation(
        name="seed-without-domain-silently-dropped",
        path="src/finder/harvest/w1_registry.py",
        old="            unresolved.append(seed.name)",
        new="            pass",
        why="'Align Wisconsin, domain unknown' is a research task, not a non-entity. "
        "Dropping it silently loses a named target the founder himself picked out.",
        tests=("tests/test_w1.py",),
    ),
    Mutation(
        name="unreachable-directory-reads-as-empty",
        path="src/finder/harvest/w1_registry.py",
        old='self._not_reached(run, "directory_unreachable"',
        new='self._not_reached(None, "directory_unreachable"',
        why="A network whose directory 503'd is not a network with no members, and a "
        "run report that lets those look the same is lying.",
        tests=("tests/test_w1.py",),
    ),
    Mutation(
        name="unimplemented-discovery-looks-empty",
        path="src/finder/harvest/w1_registry.py",
        old='                "no_enumeration_path",',
        new='                "",',
        why="ga_adjacent_chambers is enumerated by AMS host patterns, which W1 does not "
        "implement. Two hundred chambers must not vanish under a blank reason.",
        tests=("tests/test_w1.py",),
    ),
    Mutation(
        name="provenance-lost",
        path="src/finder/harvest/w1_registry.py",
        old='                discovered_from=f"directory:{network.directory_url}",',
        new='                discovered_from="",',
        why="Where an organization came from is what makes a bad batch traceable to the "
        "directory that produced it.",
        tests=("tests/test_w1.py",),
    ),
    Mutation(
        name="harvest-not-isolated-per-network",
        path="src/finder/harvest/w1_registry.py",
        old='            with run.item("register", network.id) as claimed:',
        new='            with run.item("register", "all") as claimed:',
        why="Keying every network to one item means the first one done marks the rest "
        "complete, and fourteen networks are skipped on a resume.",
        tests=("tests/test_w1.py",),
    ),
    Mutation(
        name="network-count-overwritten-by-null",
        path="src/finder/store/repos.py",
        old='"     excluded.node_count_actual, network.node_count_actual),"',
        new='"     excluded.node_count_actual, NULL),"',
        why="A later pass that did not count would blank the count an earlier one established.",
        tests=("tests/test_w1.py", "tests/test_repos.py"),
    ),
    # --- search and semantic discovery -------------------------------------
    Mutation(
        name="search-result-loses-its-query",
        path="src/finder/acquire/providers/search.py",
        old="                    query=query,",
        new='                    query="",',
        why="A domain found by 'state manufacturers association' is a different "
        "candidate from one found by 'workforce consultant'. Losing which query "
        "surfaced it throws that away, exactly as losing matched_term would.",
        tests=("tests/test_search.py", "tests/test_w1.py"),
    ),
    Mutation(
        name="keyword-search-instead-of-neural",
        path="src/finder/acquire/providers/search.py",
        old='            "type": "neural",',
        new='            "type": "keyword",',
        why="The queries are descriptions of a kind of organization, not keyword bags. "
        "Keyword search on a thesis paragraph returns the EMS conferences and the "
        "woodworking expo all over again.",
        tests=("tests/test_search.py",),
    ),
    Mutation(
        name="result-count-unclamped",
        path="src/finder/acquire/providers/search.py",
        old='            "numResults": min(max(1, limit), MAX_RESULTS),',
        new='            "numResults": limit,',
        why="A zero or negative count is a silently empty pass; an enormous one is a "
        "bill nobody asked for.",
        tests=("tests/test_search.py",),
    ),
    Mutation(
        name="empty-search-results-crash",
        path="src/finder/acquire/providers/search.py",
        old="        if not isinstance(raw, list):\n            return []",
        new="        if False:\n            return []",
        why="Search legitimately returns nothing for a narrow query. Losing the whole "
        "discovery pass over that is a poor trade.",
        tests=("tests/test_search.py",),
    ),
    Mutation(
        name="malformed-result-row-is-fatal",
        path="src/finder/acquire/providers/search.py",
        old='            if not isinstance(item, dict) or not isinstance(item.get("url"), str):',
        new="            if False:",
        why="One bad row in a result set must not cost the other twenty-four.",
        tests=("tests/test_search.py",),
    ),
    Mutation(
        name="snippet-unbounded",
        path="src/finder/acquire/providers/search.py",
        old='snippet=str(item.get("text") or item.get("snippet") or "").strip()[:1000],',
        new='snippet=str(item.get("text") or item.get("snippet") or "").strip(),',
        why="Snippets ride into logs and prompts. An unbounded one is a whole page.",
        tests=("tests/test_search.py",),
    ),
    Mutation(
        name="discovery-query-drops-the-thesis",
        path="src/finder/harvest/w1_registry.py",
        old="    return [f\"{sector.replace('_', ' ')}: {condensed}\" for sector in sectors"
        " if sector.strip()]",
        new="    return [sector.replace('_', ' ') for sector in sectors if sector.strip()]",
        why="The thesis is what makes this a search for a KIND of organization. Without "
        "it, 'manufacturing' returns directories and press releases.",
        tests=("tests/test_w1.py",),
    ),
    Mutation(
        name="empty-thesis-accepted",
        path="src/finder/harvest/w1_registry.py",
        old="    if not condensed:",
        new="    if False:",
        why="An empty query returns the whole internet, ranked by nothing.",
        tests=("tests/test_w1.py",),
    ),
    Mutation(
        name="discovery-rewrites-known-organizations",
        path="src/finder/harvest/w1_registry.py",
        old="            if self.store.organizations.get_by_domain(domain) is not None:",
        new="            if False:",
        why="A search hit would overwrite a tier A network node's name, tier and "
        "network with a search engine's title and tier C.",
        tests=("tests/test_w1.py",),
    ),
    Mutation(
        name="discovery-ignores-standing-rejections",
        path="src/finder/harvest/w1_registry.py",
        old="            if self.store.rejections.blocks(",
        new="            if False and self.store.rejections.blocks(",
        why="Search liking an organization does not overturn the founder's permanent "
        "rejection of it. This is how the rejected ones kept coming back.",
        tests=("tests/test_w1.py",),
    ),
    Mutation(
        name="discovered-org-claims-a-network",
        path="src/finder/harvest/w1_registry.py",
        old='                    network_id=None,\n                    tier="C",',
        new='                    network_id="nist_mep",\n                    tier="A",',
        why="An organization found by search belongs to no network and has not earned "
        "a network's tier. Claiming both makes a guess look like a directory entry.",
        tests=("tests/test_w1.py",),
    ),
    Mutation(
        name="one-failed-query-ends-discovery",
        path="src/finder/harvest/w1_registry.py",
        old='                self._not_reached(run, "discovery_failed", f"{query[:80]}: {exc}")\n'
        "                continue",
        new='                self._not_reached(run, "discovery_failed", f"{query[:80]}: {exc}")\n'
        "                break",
        why="A rate limit on one sector must not silently cost the other four.",
        tests=("tests/test_w1.py",),
    ),
    Mutation(
        name="duplicate-discovery-hits-registered-twice",
        path="src/finder/harvest/w1_registry.py",
        old="            if domain in seen:\n                continue\n"
        "            seen.add(domain)",
        new="            if False:\n                continue\n            seen.add(domain)",
        why="The same organization surfacing under two sector queries would be written "
        "twice and counted twice.",
        tests=("tests/test_w1.py",),
    ),
    # --- graph expansion ----------------------------------------------------
    Mutation(
        name="expansion-loses-the-span",
        path="src/finder/harvest/expand.py",
        old="                span_text=edge.span,",
        new="                span_text=None,",
        why="Indirect evidence of ACCESS is the whole point of following these edges. "
        "An organization with no span supporting its discovery is indistinguishable "
        "from one somebody typed in.",
        tests=("tests/test_expand.py",),
    ),
    Mutation(
        name="expansion-loses-who-vouched",
        path="src/finder/harvest/expand.py",
        old="                value=edge.from_domain,",
        new="                value=None,",
        why="'GaMEP names this body as an approved provider' is the fact. Without the "
        "vouching organization the evidence row says nothing useful.",
        tests=("tests/test_expand.py",),
    ),
    Mutation(
        name="expansion-depth-unbounded",
        path="src/finder/harvest/expand.py",
        old="        if max_depth > MAX_DEPTH:",
        new="        if False:",
        why="Beyond two hops this is a crawl of the open web wearing a different name, "
        "and the run never ends.",
        tests=("tests/test_expand.py",),
    ),
    Mutation(
        name="expansion-cycles-forever",
        path="src/finder/harvest/expand.py",
        old="                    if edge.to_domain in result.visited:",
        new="                    if False:",
        why="Partner pages link back constantly. Without a visited set the traversal "
        "walks A to B to A until the process dies.",
        tests=("tests/test_expand.py",),
    ),
    Mutation(
        name="expansion-registers-duplicates-per-page",
        path="src/finder/harvest/expand.py",
        old="            if to_domain in seen_here:",
        new="            if False:",
        why="A partner listed by full name and again by abbreviation becomes two edges "
        "and two evidence rows for one relationship.",
        tests=("tests/test_expand.py",),
    ),
    Mutation(
        name="expansion-rewrites-known-organizations",
        path="src/finder/harvest/expand.py",
        old="        if self.store.organizations.get_by_domain(edge.to_domain) is not None:",
        new="        if False:",
        why="A partner-page mention would overwrite a tier A network node's tier and "
        "first_seen with a passing reference.",
        tests=("tests/test_expand.py",),
    ),
    Mutation(
        name="expansion-ignores-standing-rejections",
        path="src/finder/harvest/expand.py",
        old="        if self.store.rejections.blocks(",
        new="        if False and self.store.rejections.blocks(",
        why="A rejected organization would return through somebody else's partner page, "
        "which is exactly how the predecessor kept resurrecting them.",
        tests=("tests/test_expand.py",),
    ),
    Mutation(
        name="rejected-organization-still-walked",
        path="src/finder/harvest/expand.py",
        old="            result.rejected += 1\n            return False",
        new="            result.rejected += 1\n            return True",
        why="Walking outward from a rejected organization spends the budget on exactly "
        "the branch the founder said not to look at.",
        tests=("tests/test_expand.py",),
    ),
    Mutation(
        name="expansion-pages-per-org-unbounded",
        path="src/finder/harvest/expand.py",
        old="[: self.pages_per_org]",
        new="[:]",
        why="Forty matching pages on one domain is a site map, not forty partner lists, "
        "and fetching all of them multiplies the bill by the size of the site.",
        tests=("tests/test_expand.py",),
    ),
    Mutation(
        name="unreachable-partner-page-silent",
        path="src/finder/harvest/expand.py",
        old='                run.record_not_reached("partner_page_unreachable"',
        new='                run.log.debug("partner_page_unreachable"',
        why="A partner page that 503'd is not a page with no partners on it.",
        tests=("tests/test_expand.py",),
    ),
    Mutation(
        name="expanded-org-claims-a-tier",
        path="src/finder/harvest/expand.py",
        old='                tier="C",\n'
        '                discovered_from=f"partner:{edge.source_url}",',
        new='                tier="A",\n'
        '                discovered_from=f"partner:{edge.source_url}",',
        why="Being named on somebody's partner page is not a network tier. Claiming one "
        "makes a passing mention outrank a verified directory entry.",
        tests=("tests/test_expand.py",),
    ),
    # --- W2 route mapping ---------------------------------------------------
    Mutation(
        name="founder-verdict-outranked-by-score",
        path="src/finder/harvest/w2_routes.py",
        old="    if any(v.strip().upper() in PURSUE_VERDICTS for v in verdicts):",
        new="    if False:",
        why="His judgment is the ground truth the scores approximate. A PURSUE that "
        "does not promote means the system stops looking at the very organizations "
        "he told it to pursue.",
        tests=("tests/test_w2.py",),
    ),
    Mutation(
        name="network-prior-outranks-the-score",
        path="src/finder/harvest/w2_routes.py",
        old="    if best_fit is not None:",
        new="    if False:",
        why="Belonging to a strong network is a prior, not a result. Letting it hold an "
        "organization at tier A after its own routes score badly spends the weekly "
        "budget on the ones already known to be weak.",
        tests=("tests/test_w2.py",),
    ),
    Mutation(
        name="tier-bands-shifted",
        path="src/finder/harvest/w2_routes.py",
        old="TIER_A_FIT = 65",
        new="TIER_A_FIT = 50",
        why="The A band is the BEST list's bar on purpose. Widening it puts every "
        "middling organization on a weekly cadence and the budget goes to noise.",
        tests=("tests/test_w2.py",),
    ),
    Mutation(
        name="best-fit-becomes-worst-fit",
        path="src/finder/store/repos.py",
        old='            "SELECT MAX(s.fit) AS best FROM score s"',
        new='            "SELECT MIN(s.fit) AS best FROM score s"',
        why="One strong route is a reason to look at an organization weekly. Taking "
        "the weakest buries it behind four bad ones, the same error as averaging.",
        tests=("tests/test_w2.py", "tests/test_repos.py"),
    ),
    Mutation(
        name="stale-score-treated-as-current",
        path="src/finder/store/repos.py",
        old='"     SELECT MAX(s2.scored_at) FROM score s2 WHERE s2.route_id = s.route_id)"',
        new='"     SELECT MIN(s2.scored_at) FROM score s2 WHERE s2.route_id = s.route_id)"',
        why="A route that scored 90 last month and 20 today is a 20. Reading the "
        "oldest score keeps a dead opportunity on the weekly list forever.",
        tests=("tests/test_w2.py", "tests/test_repos.py"),
    ),
    Mutation(
        name="cadence-ignored",
        path="src/finder/harvest/w2_routes.py",
        old="    return datetime.fromisoformat(last_mapped) <= cutoff",
        new="    return True",
        why="Every organization becomes due every run, which multiplies the map bill by "
        "the size of the registry.",
        tests=("tests/test_w2.py",),
    ),
    Mutation(
        name="never-mapped-not-due",
        path="src/finder/harvest/w2_routes.py",
        old="    if not last_mapped:\n        return True",
        new="    if not last_mapped:\n        return False",
        why="An organization nobody has ever looked at would never be looked at.",
        tests=("tests/test_w2.py",),
    ),
    Mutation(
        name="unknown-tier-gets-the-fastest-cadence",
        path="src/finder/harvest/w2_routes.py",
        old='    days = CADENCE_DAYS.get(tier, CADENCE_DAYS["C"])',
        new='    days = CADENCE_DAYS.get(tier, CADENCE_DAYS["A"])',
        why="Failing OPEN on an undefined tier spends the weekly budget on rows nobody "
        "classified. Failing to the slowest cadence spends nothing.",
        tests=("tests/test_w2.py",),
    ),
    Mutation(
        name="barren-domain-never-marked-mapped",
        path="src/finder/harvest/w2_routes.py",
        old="        self.store.organizations.mark_mapped(org.org_id)",
        new="        pass",
        why="'Looked at, found nothing' is an answer. Treating it as never-looked-at "
        "re-maps the same barren domain every single run, forever.",
        tests=("tests/test_w2.py",),
    ),
    Mutation(
        name="unmappable-domain-marked-mapped",
        path="src/finder/harvest/w2_routes.py",
        old="        if not outcome.mapped:",
        new="        if False:",
        why="Marking a domain mapped after a 503 skips it for a month over a transient "
        "failure, and makes a dead provider look like a set of barren domains.",
        tests=("tests/test_w2.py",),
    ),
    Mutation(
        name="candidates-not-deduplicated",
        path="src/finder/harvest/w2_routes.py",
        old="            if hit.url in seen:",
        new="            if False:",
        why="Two chambers on one AMS host surface the same page; reading it twice costs "
        "two fetches and produces two candidate routes for one page.",
        tests=("tests/test_w2.py",),
    ),
    Mutation(
        name="organizations-not-checkpointed",
        path="src/finder/harvest/w2_routes.py",
        old='            with run.item("map_routes", org.org_id) as claimed:',
        new='            with run.item("map_routes", "all") as claimed:',
        why="One key for every organization means the first one done marks the rest "
        "complete, and a resumed run skips the whole registry.",
        tests=("tests/test_w2.py",),
    ),
    Mutation(
        name="partner-paths-dropped-from-the-map",
        path="src/finder/harvest/w2_routes.py",
        old="        self.terms = list(dict.fromkeys([*programming_paths, *partner_paths]))",
        new="        self.terms = list(programming_paths)",
        why="The service and provider pages disappear, and with them the CHANNEL "
        "routes — the family the founder named as the goal. The map call is the "
        "cost; matching more terms against it is free.",
        tests=("tests/test_w2.py",),
    ),
    Mutation(
        name="retier-writes-every-row",
        path="src/finder/harvest/w2_routes.py",
        old="            if earned != org.tier:",
        new="            if True:",
        why="Reporting every organization as changed makes the one that actually moved "
        "impossible to see.",
        tests=("tests/test_w2.py",),
    ),
    Mutation(
        name="map-outcome-hides-failure",
        path="src/finder/acquire/map.py",
        old='        return MapOutcome(hits=[], source="", failures=tuple(failures))',
        new='        return MapOutcome(hits=[], source="sitemap", failures=tuple(failures))',
        why="A domain nobody could reach is not a domain with nothing on it, and a "
        "caller that cannot tell them apart will mark the unreachable one done.",
        tests=("tests/test_w2.py", "tests/test_map.py"),
    ),
    # --- founder write guard ------------------------------------------------
    Mutation(
        name="guard-not-installed",
        path="src/finder/store/db.py",
        old="    guard.install(conn)",
        new="    pass",
        why="Every connection would be unguarded. This is the defence that survives a "
        "refactor, and the predecessor lost the founder's decisions without it.",
        tests=("tests/test_write_guard.py",),
    ),
    Mutation(
        name="statement-cache-defeats-the-guard",
        path="src/finder/store/db.py",
        old="    conn = sqlite3.connect(str(path), isolation_level=None, cached_statements=0)",
        new="    conn = sqlite3.connect(str(path), isolation_level=None)",
        why="SQLite runs the authorizer at PREPARE time and Python reuses prepared "
        "statements, so with the cache on the SECOND identical write to a founder "
        "table skips the guard entirely. A guard that protects only the first row.",
        tests=("tests/test_write_guard.py",),
    ),
    Mutation(
        name="guard-permits-everything",
        path="src/finder/store/guard.py",
        old="        return sqlite3.SQLITE_OK if is_allowed() else sqlite3.SQLITE_DENY",
        new="        return sqlite3.SQLITE_OK",
        why="The authorizer would wave through every worker write to the founder's "
        "decisions, which is precisely the predecessor's failure.",
        tests=("tests/test_write_guard.py",),
    ),
    Mutation(
        name="guard-blocks-reads-too",
        path="src/finder/store/guard.py",
        old="        if operation is None or arg1 not in FOUNDER_OWNED_TABLES:",
        new="        if arg1 not in FOUNDER_OWNED_TABLES:",
        why="His marks are the training set. A guard that blocks reading them breaks "
        "every downstream consumer while protecting nothing.",
        tests=("tests/test_write_guard.py",),
    ),
    Mutation(
        name="guard-blocks-every-table",
        path="src/finder/store/guard.py",
        old='frozenset({"founder_mark", "person_founder"})',
        new='frozenset({"founder_mark"})',
        why="person_founder holds who he knows. Dropping it from the registry leaves "
        "half the founder-owned data unguarded.",
        tests=("tests/test_write_guard.py", "tests/test_repos.py"),
    ),
    Mutation(
        name="permission-leaks-past-the-block",
        path="src/finder/store/guard.py",
        old="        _ALLOWED.reset(token)",
        new="        pass",
        why="Once any sanctioned ingest ran, every later worker write would be "
        "permitted for the rest of the process.",
        tests=("tests/test_write_guard.py",),
    ),
    Mutation(
        name="violation-not-audited",
        path="src/finder/store/guard.py",
        old="    record_attempt(conn, table, operation, allowed=False,"
        " caller=caller, detail=detail)",
        new="    pass",
        why="A P0 defect with no record of who did it or when cannot be traced back to "
        "the code that caused it.",
        tests=("tests/test_write_guard.py",),
    ),
    Mutation(
        name="sanctioned-write-not-audited",
        path="src/finder/store/repos.py",
        old='            "founder_mark",\n            "INSERT",\n            allowed=True,',
        new='            "founder_mark",\n            "INSERT",\n            allowed=False,',
        why="A trail that mislabels the sanctioned path as a violation is worse than "
        "none: it cries wolf on the one write that was supposed to happen.",
        tests=("tests/test_write_guard.py",),
    ),
    Mutation(
        name="violation-logged-below-error",
        path="src/finder/store/guard.py",
        old='    _log.error(\n        "founder_write_refused"',
        new='    _log.debug(\n        "founder_write_refused"',
        why="A P0 defect at debug level is invisible in production.",
        tests=("tests/test_write_guard.py",),
    ),
    Mutation(
        name="caller-of-record-blames-the-guard",
        path="src/finder/store/guard.py",
        old='if "finder\\\\store" not in frame.filename',
        new="if False",
        why="Naming the repository line reports the guard rail rather than the code "
        "that drove into it, which is useless for finding the offending worker.",
        tests=("tests/test_write_guard.py",),
    ),
    Mutation(
        name="audit-failure-masks-the-violation",
        path="src/finder/store/guard.py",
        old="    except sqlite3.Error as exc:  # pragma: no cover - the table exists after 004",
        new="    except KeyboardInterrupt as exc:",
        why="Losing the audit record is bad; letting a lost record turn the refusal "
        "into a different exception, so the write looks merely broken rather than "
        "forbidden, is worse.",
        tests=("tests/test_write_guard.py",),
    ),
    Mutation(
        name="non-refusal-errors-relabelled",
        path="src/finder/store/repos.py",
        old='if "not authorized" in str(exc) else None',
        new="else None",
        why="A real database fault reported as a permission problem sends whoever is "
        "debugging it to entirely the wrong place.",
        tests=("tests/test_write_guard.py",),
    ),
    # --- offline guarantee --------------------------------------------------
    Mutation(
        name="network-guard-disabled",
        path="tests/conftest.py",
        old='    monkeypatch.setattr(socket.socket, "connect", guarded_connect)',
        new="    pass",
        why="The suite could reach live providers again: slow, costly, and asserting "
        "against whatever the web says this morning rather than against a fixture.",
        tests=("tests/test_replay.py",),
    ),
    Mutation(
        name="network-guard-lets-everything-through",
        path="tests/conftest.py",
        old='        if host in ("127.0.0.1", "::1", "localhost"):',
        new="        if True:",
        why="Exempting every host is the same as no guard, while still looking like one.",
        tests=("tests/test_replay.py",),
    ),
    Mutation(
        name="fixture-records-the-api-key",
        path="src/finder/acquire/replay.py",
        old='        "request": {',
        new='        "request": {\n            "headers": dict(request.headers),',
        why="Fixtures go into git. Recording request headers commits the API key with "
        "them, and it stays in history after anyone notices.",
        tests=("tests/test_replay.py",),
    ),
    Mutation(
        name="fixture-miss-returns-404",
        path="src/finder/acquire/replay.py",
        old="            raise FixtureMissing(key, request, root)",
        new="            return httpx.Response(404, request=request)",
        why="A miss answered with 404 lets a test assert against a 'page not found' it "
        "never meant to exercise — quiet wrongness rather than a loud gap.",
        tests=("tests/test_replay.py",),
    ),
    Mutation(
        name="fixture-key-ignores-the-body",
        path="src/finder/acquire/replay.py",
        old="    return fixture_key(request.method, str(request.url), request.content)",
        new="    return fixture_key(request.method, str(request.url))",
        why="Two different requests to the same endpoint would share a recording, so a "
        "stale fixture answers a question it was never asked.",
        tests=("tests/test_replay.py",),
    ),
    # --- W3 extraction ------------------------------------------------------
    Mutation(
        name="fabricated-span-kept",
        path="src/finder/extract/w3_mechanism.py",
        old='        if verdict == "absent":',
        new="        if False:",
        why="THE rule. A field whose span is not in the page is not weakly supported, "
        "it is invented, and keeping it puts fiction into the ranked list with a "
        "citation attached.",
        tests=("tests/test_w3.py",),
    ),
    Mutation(
        name="span-check-only-top-level",
        path="src/finder/extract/w3_mechanism.py",
        old='        elif hasattr(type(value), "model_fields"):',
        new="        elif False:",
        why="audience.member_unit and intake.url are nested and are among the most "
        "decision-bearing fields there are. A checker that skips them guards the "
        "cheap fields and leaves the expensive ones open.",
        tests=("tests/test_w3.py",),
    ),
    Mutation(
        name="reflowed-span-called-fabricated",
        path="src/finder/extract/w3_mechanism.py",
        old='    return "normalized" if normalize(span) in normalize(snapshot) else "absent"',
        new='    return "absent"',
        why="Providers re-wrap markdown constantly. Treating a whitespace difference "
        "as fabrication throws away true fields and makes the extractor look broken.",
        tests=("tests/test_w3.py",),
    ),
    Mutation(
        name="empty-span-accepted",
        path="src/finder/extract/w3_mechanism.py",
        old="    if not span or not span.strip():",
        new="    if False:",
        why="An empty span passes a substring check against any page, so every field "
        "would validate as supported by nothing at all.",
        tests=("tests/test_w3.py",),
    ),
    Mutation(
        name="model-supplies-its-own-provenance",
        path="src/finder/extract/w3_mechanism.py",
        old='            payload["evidence_url"] = snapshot.url',
        new="            pass",
        why="evidence_url is a fact the harness knows. Letting the model state it "
        "invites it to state it wrongly, and every downstream audit follows that URL.",
        tests=("tests/test_w3.py",),
    ),
    Mutation(
        name="content-hash-not-pinned",
        path="src/finder/extract/w3_mechanism.py",
        old='            payload["content_hash"] = snapshot.content_hash',
        new="            pass",
        why="The content hash is what lets an extraction be replayed against the exact "
        "bytes it saw. A model-supplied one points at nothing.",
        tests=("tests/test_w3.py",),
    ),
    Mutation(
        name="truncation-looks-like-a-bad-page",
        path="src/finder/extract/w3_mechanism.py",
        old="            if completion.truncated:",
        new="            if False:",
        why="Half a JSON object fails validation for a reason that has nothing to do "
        "with the page, and gets quarantined as if the page were at fault.",
        tests=("tests/test_w3.py",),
    ),
    Mutation(
        name="prompt-loses-the-clauses",
        path="src/finder/extract/w3_mechanism.py",
        old='    return SYSTEM_PROMPT.format(clauses="\\n".join(f"- {c}" for c in PROMPT_CLAUSES))',
        new='    return SYSTEM_PROMPT.format(clauses="")',
        why="Each clause is a failure the predecessor made. Dropping them is how "
        "inferred audiences and last year's closed calls come back.",
        tests=("tests/test_w3.py",),
    ),
    Mutation(
        name="prompt-version-frozen",
        path="src/finder/extract/w3_mechanism.py",
        old='PROMPT_VERSION = "w3-2026-09-02"',
        new='PROMPT_VERSION = ""',
        why="Without a version stamped on every extraction, a regression six weeks "
        "from now is attributable to nothing.",
        tests=("tests/test_w3.py",),
    ),
    Mutation(
        name="channel-told-to-invent-an-intake",
        path="src/finder/extract/w3_mechanism.py",
        old='"and valuable — leave route_url not_stated rather than inventing one."',
        new='"and valuable."',
        why="The GaMEP case. A model that invents a route_url for a channel with no "
        "published intake destroys the family the founder named as the goal.",
        tests=("tests/test_w3.py",),
    ),
    Mutation(
        name="room-not-told-the-form-is-off-domain",
        path="src/finder/extract/w3_mechanism.py",
        old='"application); it is often on a DIFFERENT domain from this page, and if the page "',
        new='"application). "',
        why="The GSAE case: the form is a SurveyMonkey link in body text, which is why "
        "it could not be found by hand. Without this the extractor misses it too.",
        tests=("tests/test_w3.py",),
    ),
    Mutation(
        name="schema-not-sent-to-the-model",
        path="src/finder/extract/w3_mechanism.py",
        old="                system=system, prompt=text, schema=schema, max_tokens=self.max_tokens",
        new="                system=system, prompt=text, max_tokens=self.max_tokens",
        why="Without the schema as a hard constraint the model answers in whatever "
        "shape it likes and every page burns two attempts before quarantine.",
        tests=("tests/test_w3.py",),
    ),
    Mutation(
        name="quarantine-not-counted",
        path="src/finder/extract/w3_mechanism.py",
        old='                run.record_not_reached("extraction_quarantined", outcome.summary())',
        new="                pass",
        why="A page the extractor could not read is not a page with nothing on it, and "
        "the run report must not let the two look the same.",
        tests=("tests/test_w3.py",),
    ),
    Mutation(
        name="llm-temperature-freed",
        path="src/finder/acquire/providers/llm.py",
        old="        temperature: float = 0.0,",
        new="        temperature: float = 1.0,",
        why="This step reads what a page says. Creativity here is indistinguishable "
        "from fabrication, and it also makes every extraction unrepeatable.",
        tests=("tests/test_llm.py",),
    ),
    Mutation(
        name="schema-suggested-not-forced",
        path="src/finder/acquire/providers/llm.py",
        old='            payload["tool_choice"] = {"type": "tool", "name": "record_extraction"}',
        new="            pass",
        why="A tool the model MAY use is a suggestion. Forcing it is what makes the "
        "schema a constraint, and the difference is the retry rate.",
        tests=("tests/test_llm.py",),
    ),
    Mutation(
        name="prose-accepted-where-a-record-was-forced",
        path="src/finder/acquire/providers/llm.py",
        old='        if not forced_tool and block.get("type") == "text":',
        new='        if block.get("type") == "text":',
        why="Falling back to the text block when a tool was forced quietly takes an "
        "explanation in place of the record that was required.",
        tests=("tests/test_llm.py",),
    ),
    Mutation(
        name="empty-model-answer-accepted",
        path="src/finder/acquire/providers/llm.py",
        old="        if not text:",
        new="        if False:",
        why="An empty extraction reads as 'the page states nothing' — a lie about a "
        "call that failed, exactly like an empty fetch.",
        tests=("tests/test_llm.py",),
    ),
    # --- the marker gate ----------------------------------------------------
    Mutation(
        name="gate-accepts-one-class",
        path="src/finder/precision/lexicon.py",
        old="        if len(classes_hit) < self.min_classes:",
        new="        if False:",
        why="Co-occurrence IS the precision. One class is not evidence, and without "
        "this the EMS conference comes back — it has a real call for speakers on it.",
        tests=("tests/test_gate.py",),
    ),
    Mutation(
        name="gate-accepts-shape-without-relevance",
        path="src/finder/precision/lexicon.py",
        old="        if self.require_classes and not set(combo) & set(self.require_classes):",
        new="        if False:",
        why="A format plus a way in describes every conference on earth. This is how "
        "the woodworking expo survived a broad search.",
        tests=("tests/test_gate.py",),
    ),
    Mutation(
        name="gate-ignores-negatives",
        path="src/finder/precision/lexicon.py",
        old="        if negatives > positives:",
        new="        if False:",
        why="Career fairs and SHRM chapters flood back in. The predecessor filled up "
        "with exactly these.",
        tests=("tests/test_gate.py",),
    ),
    Mutation(
        name="negatives-counted-by-presence-not-density",
        path="src/finder/precision/lexicon.py",
        old="        negatives = occurrences(text, self.negative)",
        new="        negatives = len(count_matches(text, self.negative))",
        why="A page saying 'career fair' three times is more about career fairs than "
        "one saying it once. Distinct-term counting calls those the same page.",
        tests=("tests/test_gate.py",),
    ),
    Mutation(
        name="plural-markers-missed",
        path="src/finder/precision/lexicon.py",
        old='    return f" {term} " in normalized_text or f" {term}s " in normalized_text',
        new='    return f" {term} " in normalized_text',
        why="Real pages say 'Plant Managers'. Missing the plural of the audience marker "
        "is a recall bug in the class that decides everything.",
        tests=("tests/test_gate.py",),
    ),
    Mutation(
        name="marker-fires-inside-any-word",
        path="src/finder/precision/lexicon.py",
        old='    return f" {term} " in normalized_text or f" {term}s " in normalized_text',
        new="    return term in normalized_text",
        why="'council' would fire on 'councilman' and every civic page becomes a council "
        "seat — the same bug the URL matcher had.",
        tests=("tests/test_gate.py",),
    ),
    Mutation(
        name="gate-drops-without-a-reason",
        path="src/finder/precision/lexicon.py",
        old="                reason=REASON_INSUFFICIENT,",
        new='                reason="",',
        why="A gate that drops pages without saying why cannot be tuned, and a recall "
        "problem inside it would be invisible.",
        tests=("tests/test_gate.py",),
    ),
    Mutation(
        name="punctuation-hides-markers",
        path="src/finder/precision/lexicon.py",
        old='    flattened = _SEPARATORS.sub(" ", text.casefold())',
        new="    flattened = text.casefold()",
        why="Real markup is hyphens and asterisks. 'Call-for-Speakers' would stop "
        "matching the single most important access marker there is.",
        tests=("tests/test_gate.py",),
    ),
    Mutation(
        name="combo-not-reported",
        path="src/finder/precision/lexicon.py",
        old='        combo = "".join(sorted({class_letter(n) for n in classes_hit}))',
        new='        combo = ""',
        why="The combo is a reranker feature. CE — employer audience plus a published "
        "way in — is the strongest thing this cheap layer can say, and losing it "
        "throws that signal away.",
        tests=("tests/test_gate.py",),
    ),
]


def run_suite(paths: tuple[str, ...] = ()) -> tuple[bool, str]:
    """Run the suite against whatever is currently on disk. Returns (passed, output).

    ``paths`` narrows the run to specific test files. Used only as a speed hint:
    the caller re-runs everything before reporting a survivor.

    Deliberately runs in ROOT rather than a copied tree. The package is
    installed editable (PEP 660), which resolves imports through a meta-path
    finder pointing at the real ``src/finder`` — so a copied tree's tests would
    import the ORIGINAL modules and every mutation would appear to survive.
    That was the first version of this script, and it reported a perfect score
    while testing nothing.
    """
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-x", "-q", "--no-header", *(paths or ())],
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

            passed, output = run_suite(mut.tests)
            if passed and mut.tests:
                # The subset missed it. Before calling this a gap, run the whole
                # suite: the subset is a speed hint, never a verdict.
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
