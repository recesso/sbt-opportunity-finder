# sbt-opportunity-finder — working agreement

**Read `CONTINUE_HERE.md` first.** It tells you where the work is. This file tells you how to work.

## What this is

A system that finds, ranks and surfaces the rooms, organizations, employers and people where
Skill Bridge Talent can engage employers consultatively — and gets better every week from what
the founder decides. It runs weekly, unattended.

**Art Recesso is the operator and the ground truth.** He marks rows; those marks train the system.
He decides what to pursue, how much is enough, and what is worth travelling for.

## Ground rules, in priority order

1. **Never fabricate.** Every specific claim must appear in text on a page that was actually
   fetched, with the quoted span stored. `not_stated` is a correct and preferred answer. A field
   with no supporting span cannot be written. The predecessor system had 231 fabricated
   descriptions; that is why this project exists.

2. **Never impose limits on Art.** No volume caps, no time budgets, no travel or spend rules,
   no "top N". Thresholds gate *quality*, never *quantity*. If 400 routes clear the bar, 400
   routes appear. He decides how many is enough.

3. **Never write a founder-owned field.** Marks, dispositions and notes are his. Enforced at the
   repository layer; a violation is a P0 defect, not a style issue.

4. **`route_url` is the page you act on. `evidence_url` is the page that proves the claim.**
   They are different fields and are never conflated. A route with no `route_url` cannot enter
   the BEST list — it goes to WORTH A LOOK with a generated question.

5. **Fetch and write are separate steps.** Acquisition stores an immutable snapshot. Extraction
   reads only from snapshots. No component both browses and writes.

6. **Scoring is deterministic.** Models extract and classify. A pure function maps fields plus
   config to numbers. No model output enters a score. Every rank decomposes to fields and spans.

7. **Geography is a facet, never a score.** A national event with employers in the room and a
   workshop slot is a top opportunity. Sort and filter by distance; never rank by it.

8. **Say what failed, first.** A run that writes half and reports what it missed beats a run
   that dies silently. `not_reached[]` is mandatory output.

## Where things live

| | |
|---|---|
| Specs | `docs/` — build spec, delivery plan, ADRs. Versioned here, not in chat. |
| Tuning | `config/*.yaml` — weights, lexicon, paths, networks, thesis, sources, hosts. **All tuning lives here and nowhere else.** Changing a weight must never require a code change. |
| Backlog | `plan/backlog.yaml` is the source of truth. Beads is derived state, loaded by `scripts/load_backlog.py`. |
| Code | `src/finder/` |
| Tests | `tests/` — the suite runs offline against recorded fixtures. |

## Task tracking — Beads

Per SBT convention, all work is tracked in Beads.

```bash
bd ready              # what can be started right now
bd show <id>          # full context for one task — it should be self-contained
bd update <id> --status=in_progress
bd close <id>
```

**Every task must be self-contained.** If executing it requires reading a chat transcript, the
task is written wrong — fix the task. Include file paths, function signatures, the acceptance
test, and enough context that a stranger can do it.

When you discover work that is not in the backlog, add it to `plan/backlog.yaml` and re-run
`make backlog`. Do not create orphan beads that the plan does not know about.

## Definition of done

A story is done when:

- the acceptance criterion in the bead passes, demonstrably, with output shown;
- tests exist that would fail if the code were broken;
- the suite runs offline (no live network in CI);
- `make check` passes;
- `CONTINUE_HERE.md` is updated if the state of the project changed.

Do not mark work done on assertion. Show the command and its output.

## Conventions

- Python 3.12. `ruff` for lint and format. `pytest` for tests.
- No raw SQL outside `src/finder/store/`.
- Every external service sits behind a Protocol in `acquire/providers/` so it can be swapped and
  faked in tests.
- Structured logging; every line carries `run_id`. API keys are redacted by a log filter.
- Secrets come from environment variables. `.env` is never committed.

## What this system does not do

No outreach. No emails. No registrations. No submissions. No calendar writes. Ever.
It drafts; Art sends.


<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:7510c1e2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->
