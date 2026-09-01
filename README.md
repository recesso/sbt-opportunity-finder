# sbt-opportunity-finder

Finds, ranks and surfaces the rooms, organizations, employers and people where Skill Bridge Talent
can engage employers consultatively — and gets better every week from what the founder decides.

Runs weekly, unattended. Writes a ranked list. Art marks it. The marks train it.

---

## Status

**Scaffold complete. No implementation yet.** → **[`CONTINUE_HERE.md`](CONTINUE_HERE.md)**

```bash
bd ready          # what can be started right now
```

## Read in this order

1. **[`CONTINUE_HERE.md`](CONTINUE_HERE.md)** — where the work is, right now
2. **[`CLAUDE.md`](CLAUDE.md)** — how to work here; the eight ground rules
3. **[`docs/00-build-spec.md`](docs/00-build-spec.md)** — what the system is
4. **[`docs/01-delivery-plan.md`](docs/01-delivery-plan.md)** — architecture decisions, interfaces,
   the learning loop, test strategy, runbook

## Layout

```
config/     all tuning — weights, lexicon, paths, networks, thesis, sources, hosts
            nothing tunable lives anywhere else
docs/       the specs
plan/       backlog.yaml — source of truth for all work. Beads is derived from it.
scripts/    load_backlog.py, migrate_predecessor.py, record_fixtures.py
src/finder/ the system
tests/      runs offline against recorded fixtures
```

## Stack

Python 3.12 · SQLite · cron. Four external APIs: Firecrawl, Exa, an LLM, a reranker.

No server, no queue, no orchestrator, no vector store. Rationale in
[`docs/01-delivery-plan.md`](docs/01-delivery-plan.md) §2 (ADR-001 through ADR-003).

## Commands

```bash
make install       # editable install with dev extras
make check         # lint + tests, offline
make backlog       # reload plan/backlog.yaml into Beads (idempotent)
make ready         # bd ready

python -m finder.run weekly     # the main cycle
python -m finder.run daily      # change and trigger detection
python -m finder.run monthly    # registry refresh and deep re-verify
python -m finder.run replay --run-id X   # re-execute from snapshots, no refetching
```

## Four things this never does

No outreach. No calendar writes. No limits on the founder's volume, time, travel or spend.
No claim that is not on a page that was actually fetched.
