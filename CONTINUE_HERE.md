# CONTINUE HERE

**Last updated:** 2026-09-01 · scaffold complete, no implementation yet.

If you are a new session or a new engineer, read this file, then `CLAUDE.md`, then run `bd ready`.
That is the whole orientation.

---

## Where the project is right now

| | |
|---|---|
| **State** | Scaffold complete. Config written. Backlog loaded. **Zero implementation code.** |
| **Next work** | `bd ready` — should surface E0.S1 (repo skeleton and CI) with nothing blocking it. |
| **Nothing is running** | No scheduled jobs, no data collected, no database created yet. |

## What exists

- `docs/` — the build spec, the delivery plan and the architecture decisions. Read
  `docs/00-build-spec.md` before touching scoring or extraction.
- `config/` — nine YAML files carrying every tunable value. These are complete and reviewed.
  They are the contract that `E0.S2` implements loading and validation for.
- `plan/backlog.yaml` — 14 epics, 52 stories, decomposed to atomic steps. Source of truth.
- `scripts/load_backlog.py` — idempotent loader from the YAML into Beads.
- Empty package skeleton under `src/finder/`.

## What does not exist yet

Everything in `src/finder/` beyond empty `__init__.py` files. No database, no schema, no
providers, no workers, no tests. That is the work.

## The first three things to build, in order

1. **E0** — config loading and validation, run harness with checkpointing, structured logging.
   Nothing else can be built or tested without these.
2. **E1** — SQLite schema, repositories, normalisation and dedupe keys.
3. **E5.S2** — the mechanism extractor. **This is the highest-risk story in the plan.** Every
   downstream number inherits its quality and no amount of good plumbing fixes a bad extractor.
   Build it against the ten labelled pages before building anything on top of it.

Milestone M1 is the thinnest slice that produces a real ranked list:
`E0 → E1 → E2 → E3.S2 → E5.S2 → E6.S1 → E7 → E9.S1`.

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
