# CONTINUE HERE

**Last updated:** 2026-09-01 · E0.S1–S5, E1.S1, E1.S2, E1.S4, E5.S1 done. 298 tests, 96% branch
coverage, 56/56 mutations caught.

If you are a new session or a new engineer, read this file, then `CLAUDE.md`, then run `bd ready`.
That is the whole orientation.

---

## Where the project is right now

| | |
|---|---|
| **Done** | E0.S1 tooling and CI · E0.S2 config · E0.S3 secrets and redaction · E0.S4 run harness · E0.S5 cost ledger · E1.S1 schema · E1.S2 repositories · E1.S4 dedupe keys · E5.S1 extraction contract |
| **Next** | `bd ready` → **E5.S2** (mechanism extractor — highest risk in the plan), **E2.S1** (fetch provider), **E1.S3** (founder write guard), **E4.S1** (marker gate) |
| **Nothing is running** | No scheduled jobs, no data collected, no live database yet |

```bash
make install
make check      # lint + 298 tests, offline, ~12s
make cov        # branch coverage, fails under 88%
make audit      # breaks the code on purpose; every mutation must be caught
bd ready
```

## How this project judges its own tests

`make audit` is not optional decoration. It applies 56 specific mutations — each one a real bug a
competent engineer could introduce — and fails if the suite does not notice. **A passing suite
proves nothing; a suite that catches deliberate sabotage proves something.** CI runs it on every
push alongside a branch-coverage floor.

If you add behaviour worth protecting, add a mutation for it. If a mutation reports `STALE`, the
code moved out from under it — fix the mutation, do not delete it.

## What exists

- `docs/` — build spec and delivery plan. Read `docs/00-build-spec.md` before touching scoring or
  extraction.
- `config/` — eight YAML files carrying every tunable value, now **loaded and validated** by
  `src/finder/config.py`. Cross-file invariants are enforced at startup; a typo fails loudly.
- `src/finder/config.py` — `load_config()` returns a frozen `Config` with a `hash` recorded on
  every future score row.
- `src/finder/secrets.py` + `src/finder/logging.py` — env-only secrets, `require()` reporting all
  missing keys at once, and a structlog processor that makes it impossible for a key to appear in
  a log line.
- `src/finder/store/` — schema and migrations (19 STRICT tables), twelve repositories,
  deterministic ids, and the dedupe keys. All database access lives here; a CI test scans the
  working tree for raw SQL anywhere else.
- `src/finder/extract/schemas.py` — the extraction contract: the `Field` wrapper (value, span,
  source_url), the common schema, four family extensions, per-family `route_type` literals, and
  `extract_with_retry`, which retries once with the specific violations and then quarantines.
- `src/finder/context.py` — the run harness. `start_run` / `resume_run`, per-item `claim()`
  checkpoints, `item()` for failure isolation, write-through counters, `not_reached`, and the cost
  ledger. Every worker loop is built on this.
- `plan/backlog.yaml` — 14 epics, 66 stories, decomposed to atomic steps. Source of truth.
- `scripts/load_backlog.py` — idempotent loader into Beads.
- `scripts/audit_tests.py` — the mutation audit.
- `scripts/build_dedupe_fixture.py` — regenerates the 500-pair labelled set from the predecessor
  export. The hard cases beside it are hand-curated and are NOT regenerated.

## What does not exist yet

No providers, no workers, no live database. Everything under `acquire/`, `harvest/`,
`precision/`, `extract/`, `resolve/`, `score/`, `ask/`, `output/`, `learn/` and `eval/` is still
an empty package.

## The next three things, in order

1. **E5.S2 — the mechanism extractor. The highest-risk story in the plan.** Every downstream
   number inherits its quality and no plumbing fixes a bad extractor. The contract it must satisfy
   already exists in `src/finder/extract/schemas.py`; what is missing is the prompt, the snapshot
   handling, and the check that every span it returns actually appears in the snapshot. Build it
   against the three hand-verified routes below before anything else.
2. **E2.S1** — the FetchProvider protocol and the Firecrawl adapter. First real network code, and
   the first place the run harness carries live work.
3. **E1.S3** — the founder-owned write guard. The schema already separates the tables; this adds
   the runtime assertion and the audit trail.

Milestone M1 is the thinnest slice that produces a real ranked list:
`E0 → E1 → E2 → E3.S2 → E5.S2 → E6.S1 → E7 → E9.S1`.

## Decisions made while building, worth knowing

- **`networks.yaml` gained `discovery_method`.** Config validation caught that
  `ga_adjacent_chambers` had no `directory_url` and no `seed_members`, so W1 had nothing to
  enumerate from. Rather than relax the check, the entry now declares `ams_host_patterns` — it is
  enumerated by walking GrowthZone/MemberClicks host patterns per state, not by search.
- **Config errors are flattened to one line** carrying file, field and reason. Pydantic's default
  multi-line output buried the useful part.
- **A test forbids geography from ever becoming a scored dimension** (`test_config.py`). The rule
  is an ADR; the test is the guard rail.
- **Organization identity is NOT the domain alone.** `same.org` hosts nine SAME posts, `cscmp.org`
  hosts Atlanta and Charlotte roundtables, `hfma.org` twelve state chapters — all distinct
  organizations. And `glueup.com` is an event platform shared by unrelated bodies. Identity is
  `(own domain, chapter place, distinct-entity marker)`. This corrected the `org_id(domain)` design
  from E1.S2; see `src/finder/store/keys.py`.
- **`same_org` and `same_route` are different questions.** A named council belongs to its parent
  organization and separates at the route level, not the organization level.
- **The 500-pair dedupe score is 1.0/1.0 and that is not impressive.** Those pairs are easy by
  construction. The real signal is `tests/fixtures/dedupe_hard_cases.json` — 26 hand-curated pairs,
  four of which failed on the first run and drove real design changes.
- **A killed process is now a tested case, not a hope.** `tests/test_context.py` starts a real
  subprocess, kills it with `os._exit(137)` at item 47, and restarts it against the same SQLite
  file. The 46 completed items are not redone, the mid-flight 47th is, and the counters and the
  spend both survive the kill. Anything less than a real kill would only prove the mock works.
- **Counters and `not_reached` write through to the run row.** They were totalled in memory and
  written at close — which loses them exactly when a run dies, the case whose report matters most.
- **`context.py` had to give up its cursors.** `test_no_raw_sql_outside_the_store_package` caught
  it and the code moved into `RunRepo`, `StageRunRepo` and `CostRepo`; `RunContext` holds a
  `Store`. The guard itself had a hole: it ran `git grep`, which sees only **tracked** files, so a
  brand-new module was invisible to it until commit. It now walks the working tree, and a second
  test runs the same scanner over a deliberately broken tree so it can be shown to fail.

- **The span rule is enforced in the type, not in a downstream check.** A stated value with no
  verbatim span cannot be constructed at all, and `not_stated` carrying a span is refused just as
  firmly — a contradictory evidence trail is worse than a gap. Two mutations guard it.
- **`known_to_art` has no slot in any extraction schema.** Founder-owned inputs are kept out of the
  shape entirely, because a model given a field will fill it, and its guess would outrank the
  truth. A test walks every generated schema at every depth to prove no slot exists.
- **Dates must be ISO *and* real days.** `2026-02-31` passes a regex and fails a calendar, and a
  deadline that looks comparable but is not is worse than a missing one. The calendar check runs
  inside the retry loop, so an impossible date gets the same second chance as any other violation.

## Three routes already verified by hand

These were fetched and read on 2026-09-01. Use them as the seed fixtures for extraction tests —
they are known-good ground truth, and each one exercises a different failure mode.

| Target | Family | route_type | The point |
|---|---|---|---|
| **Enterprise Technology Association — AI Week** | ROOM | `OPEN_CALL` | Open rolling call for speakers, trainers and sponsors. `route_url` = `joinaiweek.com/apply-to-speak`. Named owner Zack Huhn, `zack@joineta.org`. Tracks include manufacturing, healthcare, financial services and workforce. Art submitted to this one. |
| **GSAE — speaker interest** | ROOM | `EVERGREEN_SUBMISSION` | **Tests off-domain link resolution.** The form is `https://www.surveymonkey.com/r/NKSQCY6`, embedded in body text on `gsae.org/speaker-interest-form`. It is not on gsae.org, which is why it could not be found by hand. Committee sets topics in early October. |
| **GaMEP** | ROOM **and** CHANNEL | ROOM: `PARTNER_DELIVERY` · CHANNEL: `UNKNOWN` | **Tests the two-family split.** The lunch-and-learn circuit is a ROOM. The instructor/provider path is a CHANNEL with no published intake — so `route_url` is null and it must surface as WORTH A LOOK with the question *"who selects lunch-and-learn presenters, and does GaMEP use outside instructors?"* If the system produces only the ROOM route, Phase 1 has failed. |

## Inputs to migrate — and what NOT to migrate

Reference material lives in `C:\Users\artre\Documents\Claude\Projects\GTM-Event-Tracker`
(read-only; do not add it to this repo).

**Migrate exactly three things** from the predecessor Google Sheet:

1. The **66 distinct founder decisions** → `founder_mark` rows. These are the entire training set.
2. The **9 permanent organization rejections** → `rejection` rows, keyed on normalised name
   **and** domain.
3. The **distinct organization names and canonical domains** → W1 seed input.

**Do not migrate the 1,689 opportunity records.** Measured: 880 of the 976 rows carrying a dedupe
key are inside duplicate clusters, and roughly a third of descriptions were fabricated. Everything
worth keeping re-derives from source in one run, correctly, with evidence.

## Open questions only Art can answer

Do not guess at these. Ask.

1. Is there a real prototype — has he delivered anything, anywhere, through a host organisation,
   and what happened afterwards? Everything in the predecessor system was anchored on a CSCMP
   session that did not happen.
2. Cobb Chamber CEO Roundtable and the GAM Operational Excellence Council — did he reject these?
   Their rejection exists only as prose in a machine-generated document.
3. The practitioner roster in `config/thesis.yaml` is empty. Who are the twenty people doing
   adjacent work whose speaking histories should seed peer-trace mining?

## How to update this file

When the state of the project changes — a milestone lands, a decision is made, a blocking
question is answered — update the top three sections. This file is the handoff. Keep it true.
