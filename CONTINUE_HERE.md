# CONTINUE HERE

**Last updated:** 2026-09-02 · **25% of the plan by atomic step (84/340).** E0 complete ·
E1.S1/S2/S4 · E2.S1/S2/S5 · E3.S1/S2/S8 · E5.S1. 587 tests, 98% branch coverage,
140/140 mutations caught.

If you are a new session or a new engineer, read this file, then `CLAUDE.md`, then run `bd ready`.
That is the whole orientation.

---

## Where the project is right now

| | |
|---|---|
| **Done** | E0.S1 tooling and CI · E0.S2 config · E0.S3 secrets and redaction · E0.S4 run harness · E0.S5 cost ledger · E1.S1 schema · E1.S2 repositories · E1.S4 dedupe keys · E2.S1 fetch · E2.S2 snapshots · E2.S5 URL inventory · E3.S1 network registrar · E3.S2 route mapper · E3.S8 graph expansion · E5.S1 extraction contract |
| **Next** | `bd ready` → **E4.S1** (marker gate — the precision half), **E5.S2** (mechanism extractor — highest risk in the plan), **E1.S3** (founder write guard), **E3.S3** (W13 ChannelProspector) |
| **Nothing is running** | No scheduled jobs, no data collected, no live database yet |

```bash
make install
make check      # lint + 587 tests, offline
make cov        # branch coverage, fails under 88%
make audit      # breaks the code on purpose; every mutation must be caught
bd ready
```

## How this project judges its own tests

`make audit` is not optional decoration. It runs in about four minutes and applies 140 specific
mutations — each one a real bug a competent engineer could introduce — and fails if the suite does not notice. **A passing suite
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
- `src/finder/acquire/` — the fetch boundary. `providers/base.py` is the Protocol every
  external service sits behind; `providers/firecrawl.py` is the only file that knows the
  vendor's name; `fetch.py` is the cache; `snapshot.py` is the write-once store every
  extraction reads from; `map.py` decides WHICH urls are worth fetching and records the term
  that matched each one.
- `src/finder/harvest/w1_registry.py` — W1. Turns each network in `networks.yaml` into real
  organization rows from that network's own directory, plus `discover()` for the bodies that
  belong to no network. The recall backbone.
- `src/finder/harvest/expand.py` — follows partner, provider and member pages outward two hops,
  writing the span that named each organization. Indirect evidence of ACCESS.
- `src/finder/harvest/w2_routes.py` — W2. One map call per due organization, emitting CANDIDATE
  urls with the term that matched. Owns the tier arithmetic and the A/B/C cadence.
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

No live database, nothing scheduled, and **no route has ever been written**. The system can now
enumerate organizations, map them, and emit candidate URLs with reasons — but a candidate becomes
a route only in extraction (E5.S2), which does not exist yet. `harvest/`, `precision/`, `resolve/`, `score/`, `ask/`,
`output/`, `learn/` and `eval/` are still empty packages, and `extract/` holds the contract but
not the extractor.

## The next three things, in order

1. **E5.S2 — the mechanism extractor. The highest-risk story in the plan.** Every downstream
   number inherits its quality and no plumbing fixes a bad extractor. The contract it must satisfy
   already exists in `src/finder/extract/schemas.py`; what is missing is the prompt, the snapshot
   handling, and the check that every span it returns actually appears in the snapshot. Build it
   against the three hand-verified routes below before anything else.
2. **E4.S1 — the marker co-occurrence gate.** W2 now produces candidates in volume; nothing yet
   decides which are worth an expensive read. This is the precision half of the recall/precision
   pair, and without it the extractor is pointed at everything.
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

- **An empty page is a fetch failure, not a thin page.** A blank snapshot extracts cleanly as
  "the page states nothing", which is indistinguishable from a genuinely sparse page and is a lie
  about a call that failed. The adapter raises instead.
- **A cache hit requires the bytes, not just the index row.** If a snapshot goes missing the page
  is re-fetched rather than erroring, because the alternative is a page that can never be read
  again. And the snapshot is written *before* the fetch is logged, in that order, for the same
  reason.
- **Failed calls are billed.** A ledger that counts only successes understates the bill exactly
  when things are going wrong.
- **Snapshots are sharded by hash prefix.** A deviation from the backlog's one-line spec, recorded
  here rather than left to be discovered: they are retained forever, and a flat directory reaches
  five figures within months.

- **The map records WHICH term matched, not just that something did.** A URL found by "call for
  speakers" is a different animal from the same URL found by "blog", and `matched_term` is a
  reranker feature. This is why matching is local rather than delegated to the provider's own
  search: a provider that returns a ranked list cannot tell you why it ranked anything.
- **Matching is word-bounded.** "council" firing on "councilman" would turn every civic page into
  a council seat — substring matching is precisely how the predecessor's recall filled with noise.
- **A domain nobody could map is not a domain with nothing on it.** The first records
  `not_reached`; the second does not. A run report that let those look the same would be lying.

- **`node_count_est` is never written anywhere.** It is a planning figure, and the config says so.
  Its one job is setting off a smoke alarm: a directory yielding three nodes against an estimate of
  fifty-one did not find three, it broke, and that is recorded as `not_reached` rather than
  reported as success. `network.node_count_actual` holds what was actually counted.
- **A foreign key caught W1 writing organizations for a network that was never registered.** The
  constraint did its job; `NetworkRepo` is the fix.
- **`https://intranet/members` is not an organization.** A host label with no dot resolved to a
  "domain" that nothing could ever fetch. Found by a test, not in production.

- **`service`/`services` were missing from every path list.** The W2 acceptance criterion caught
  it: nothing matched `gamep.org/services/workforce-development`, which is exactly where the GaMEP
  CHANNEL route lives. W2 also matches PROGRAMMING_PATHS *and* PARTNER_PATHS against one map call
  — the call is the cost, matching is free, and narrowing the terms only loses candidates.
- **Tier is earned, not declared.** Founder PURSUE first, then the organization's BEST route FIT
  from each route's latest score, then the network's tier last. Best rather than average: one
  strong route is a reason to look weekly, and averaging it against four weak ones buries it.
- **"Mapped, found nothing" and "could not be mapped" are different findings.** `map_detailed`
  keeps them apart, so a 503 does not mark a domain looked-at and skip it for a month.

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
