# CONTINUE HERE

**Last updated:** 2026-09-01 · E0.S1–S3, E1.S1, E1.S2 done. 129 tests, 95% branch coverage,
18/18 mutations caught.

If you are a new session or a new engineer, read this file, then `CLAUDE.md`, then run `bd ready`.
That is the whole orientation.

---

## Where the project is right now

| | |
|---|---|
| **Done** | E0.S1 tooling and CI · E0.S2 config · E0.S3 secrets and redaction · E1.S1 schema · E1.S2 repositories |
| **Next** | `bd ready` → **E1.S3** (founder write guard), **E1.S4** (dedupe keys), **E0.S4** (run harness), **E0.S5** (cost ledger) |
| **Nothing is running** | No scheduled jobs, no data collected, no live database yet |

```bash
make install
make check      # lint + 129 tests, offline, ~3s
make cov        # branch coverage, fails under 88%
make audit      # breaks the code on purpose; every mutation must be caught
bd ready
```

## How this project judges its own tests

`make audit` is not optional decoration. It applies 18 specific mutations — each one a real bug a
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
- `plan/backlog.yaml` — 14 epics, 66 stories, decomposed to atomic steps. Source of truth.
- `scripts/load_backlog.py` — idempotent loader into Beads.

## What does not exist yet

No database, no schema, no providers, no workers. `src/finder/` beyond the three modules above is
empty packages.

## The next three things, in order

1. **E1.S1** — SQLite schema and migrations. Unblocks E0.S4 (run harness) and all of E1.
2. **E1.S4** — normalisation and dedupe keys, with the 500-row labelled set. In the predecessor
   database 880 of the 976 keyed rows were duplicates because the keys were never checked.
3. **E5.S2** — the mechanism extractor. **Highest-risk story in the plan.** Every downstream
   number inherits its quality and no plumbing fixes a bad extractor. Build it against the ten
   labelled pages before building anything on top of it.

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
