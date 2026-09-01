# Delivery Plan — how it gets built

**Status:** v1, 2026-09-01. Companion to `00-build-spec.md` (what the system is).

Work breakdown lives in `../plan/backlog.yaml` and is loaded into Beads by
`../scripts/load_backlog.py`. **14 epics, 66 stories.** Run `bd ready` to see what is startable.

---

## 1. Scope and non-goals

**In scope:** weekly unattended discovery, extraction, scoring and ranking across four families;
daily change and trigger detection; a review surface where the founder marks rows; a learning loop
that acts on those marks; full provenance on every claim.

**Explicit non-goals:**

- **No outreach automation.** The system drafts; Art sends. It never emails, registers, submits or
  contacts anyone.
- **No calendar writes.** Ever.
- **No volume, time, travel or budget limits** imposed on the founder. Thresholds gate quality,
  never quantity.
- **No CRM.** Pipeline management is out of scope for v1.
- **No multi-user.** Single operator — this assumption justifies half the simplifications below.
- **No real-time.** Batch only.

### Constraints that drive everything

| Constraint | Consequence |
|---|---|
| One user, weekly batch, 10⁴–10⁵ rows | Embedded database. No server, no orchestrator, no queue. |
| A missed opportunity is invisible forever; a false positive costs cents | Recall is over-inclusive by design; precision is a separate later stage |
| Fabricated content destroyed the predecessor | Fetch and write are separate. No span, no field. Independent audit before publish. |
| The founder's judgment is ground truth | Deterministic, decomposable scoring. Interpretable rule induction, never opaque training. |
| Runs unattended and must survive failure | Every stage checkpoints. `not_reached[]` is mandatory. |

---

## 2. Architecture decisions

Each records the decision, what else was considered, why, and what would make us revisit.
**ADR-001, 002 and 003 reverse choices inherited from documents later shown to be
machine-generated and never actually justified.** Do not re-litigate them from those files.

### ADR-001 · SQLite as the system of record *(reverses a prior choice)*

Single file at `data/finder.db`, WAL mode, backed up by copying.

- **Alternatives:** PostgreSQL (self-hosted or managed), DuckDB, a document store.
- **Why:** at one user, weekly batch and under ~10⁵ rows, SQLite gives full SQL, ACID, foreign
  keys and indices with **zero operational surface** — no server, no pooling, no managed-instance
  cost, no network failure mode. Postgres was in the earlier documents because it is the reflex
  answer to "real database," not because this workload requires it.
- **Revisit if:** concurrent writers appear, rows exceed ~10⁶, or the run moves to horizontally
  scaled compute. Migration is mechanical; the schema is standard SQL.
- **Risk:** durability in ephemeral cloud storage. Mitigated by E1.S5 — post-run copy to object
  storage, 30 dated copies, restore tested.

### ADR-002 · No vector database *(reverses a prior choice)*

Relevance → cross-encoder reranker API. Dedupe → normalized keys plus fuzzy matching.

- **Alternatives:** pgvector, sqlite-vec, a hosted vector DB.
- **Why:** both jobs are better served otherwise. A cross-encoder scores *(thesis, candidate)*
  jointly and is materially more accurate than cosine between independent embeddings. Dedupe is
  dominated by exact keys with `rapidfuzz` for the residue. A vector store adds an index to
  maintain, a model version to pin and a re-embedding migration, for marginal benefit.
- **Revisit if:** dedupe recall on the labelled set drops below 0.95. Then embed to a `numpy`
  array in memory (20k × 1024 floats is 80 MB) before considering any vector database.

### ADR-003 · Plain Python plus cron. No workflow orchestrator. *(reverses a prior choice)*

`python -m finder.run weekly|daily|monthly|replay`, scheduled by cron, GitHub Actions, or Cloud
Scheduler → Cloud Run Job.

- **Alternatives:** n8n, Airflow, Prefect, Dagster, Temporal.
- **Why:** an orchestrator here provides scheduling, retries and a credential vault — a cron
  entry, a `tenacity` decorator and environment variables. What it costs is a runtime to operate
  and logic split between a GUI and a repository, which makes the system harder to version, test
  and reason about. n8n appeared in the earlier documents because a licence existed, which is not
  a technical reason.
- **Revisit if:** human-in-the-loop approval becomes part of the pipeline itself, or fan-out
  exceeds a single process's window.

### ADR-004 · Snapshot-first: fetch and write are different processes

Acquisition writes an immutable content-addressed snapshot. Extraction reads only snapshots.

- **Why:** the measured root cause of 231 fabricated descriptions was an agent that browsed and
  wrote in one step, filling thin pages with category-typical detail. Separating them makes
  fabrication structurally harder and every extraction reproducible offline.

### ADR-005 · Independent audit before publish, using a different model

- **Why:** self-consistency checks with the same model correlate their own errors. Model diversity
  is the point.
- **Cost:** roughly doubles extraction spend. Justified — extraction quality caps every downstream
  number, and a fabricated row costs the founder's trust, which is the scarce resource.

### ADR-006 · Scoring is deterministic; no model output enters a score

- **Why:** same inputs must always give the same rank, and every rank must decompose to fields and
  spans the founder can inspect and dispute. A black-box ranker is unacceptable in a system whose
  premise is that his judgment is ground truth.
- **Test:** `score(fields, config)` is pure, with property-based tests.

### ADR-007 · Two-stage retrieval: cheap recall, expensive precision

- **Why:** asymmetric costs. The gate is free (string matching); the reranker is ~1/50th the cost
  of extraction. Filtering before extraction is where the cost model works.

### ADR-008 · Config as data, versioned, hashed onto every score

- **Why:** tuning must never require a deploy, and a ranking change must always be attributable to
  a config version. This is what makes weight re-fitting safe to propose and trivial to roll back.

### ADR-009 · Learning is rule induction plus constrained weight fitting, not model training

- **Alternatives:** fine-tuned ranker, gradient-boosted LTR, preference-learning net.
- **Why:** the training set is ~66 labels growing by tens per month. Six parameters can be fitted
  from that; a model cannot be trained from it, and pretending otherwise produces overfitting
  dressed as intelligence. Rule induction is also the only form the founder can read, argue with
  and veto.
- **Revisit if:** labelled marks exceed ~500 with real outcome data attached.

### ADR-010 · Idempotent stages with checkpointing and replay

- **Why:** the predecessor died without writing on four consecutive firings. The fix is
  architectural, not a shorter task list.
- **Mechanism:** `stage_run(run_id, stage, item_key)`. Before processing, check for a completed
  record. `--replay <run_id>` re-executes from snapshots without refetching.

### ADR-011 · Google Sheets as the review surface

Written through the existing Apps Script bridge.

- **Why:** it exists, it works, the founder already uses it, it is natively filterable, and it
  costs zero build. A web UI is v2 and must not block delivery of the thing that finds
  opportunities.
- **Fixes required (E9.S2–S4):** the current sheet has no freeze panes, no autofilter, and
  validation covering 35 of 251 rows. Its feedback columns sat at the far right, outside the
  header block, 8–13 characters wide — and were used zero times out of 251 rows.

### ADR-012 · Provider abstraction over external APIs

`SearchProvider`, `FetchProvider`, `MapProvider`, `RerankProvider`, `ExtractProvider`.

- **Why:** vendor capabilities and pricing change. Swapping a provider must be a config change
  plus one adapter, not a refactor. It also makes offline testing with recorded fixtures possible.

---

## 3. Total infrastructure

One Python process. One file. One cron entry. Four external APIs — Firecrawl (map, scrape,
change-tracking), Exa (semantic search), an LLM (extraction, audit, classification), a reranker
(precision gate). Everything else is a library.

**No server, no queue, no orchestrator, no vector store to operate.**

Model tiering matters and is deliberate:

| Role | Tier | Note |
|---|---|---|
| Extraction | capable | Caps everything downstream |
| Audit | **a different model** | Same model twice correlates its own errors |
| Page classification, marker scoring | small/fast | High volume, low stakes per item |
| Embeddings | pinned by version | Changing it requires re-embedding the corpus |
| Reranking | cross-encoder | Biggest precision lever, cheap relative to extraction |

---

## 4. Component interfaces

```python
# acquire — ADR-004
@dataclass(frozen=True)
class Snapshot:
    content_hash: str          # sha256 of normalized text
    url: str; canonical_url: str
    markdown: str; links: list[str]
    status: int; fetched_at: datetime
    is_pdf: bool; provider: str

class FetchProvider(Protocol):
    def fetch(self, url: str, *, max_age_s: int = 0) -> Snapshot: ...
class MapProvider(Protocol):
    def map(self, domain: str, terms: list[str], limit: int) -> list[MapHit]: ...
class SearchProvider(Protocol):
    def search(self, q: str, *, semantic: bool, limit: int) -> list[SearchHit]: ...
class RerankProvider(Protocol):
    def rerank(self, query: str, docs: list[str], top_k: int) -> list[RerankHit]: ...

# precision — ADR-007
def marker_gate(text: str, lexicon: Lexicon) -> GateResult
    # GateResult(passed, classes_hit, combo, reason)

# extract — ADR-004, ADR-005
def extract_mechanism(snap: Snapshot, family: Family, prompt_version: str) -> ExtractionResult
def audit(snap: Snapshot, written: ExtractionResult) -> AuditResult

# resolve
def normalize_org(name: str) -> str
def series_key(org: str, mechanism: str) -> str
def occurrence_key(org: str, mechanism: str, date: date | None) -> str
def resolve(draft: RouteDraft, repo: RouteRepo) -> Resolution   # INSERT | MERGE | REJECT_DUP

# score — ADR-006, pure function
def score(fields: ExtractedFields, cfg: Config) -> Score
    # Score(fit, route, confidence, components, config_hash)

# learn — ADR-009
def induce_rules(marks, min_support=3, max_counterexamples=1) -> list[ProposedRule]
def fit_weights(marks, family, current, max_delta_pct=20) -> WeightProposal
def knn_veto(cand, negatives, positives, k=5) -> VetoResult
```

---

## 5. The learning loop, in full

Five mechanisms, staged by the data each requires. None is model training.

### M1 · Exclusion memory — the "never ask me twice" guarantee

```
on mark(route_id, verdict) where verdict in {DROP_TARGET, DO_NOT_SURFACE}:
    rejection.insert(
        match_name   = normalize_org(route.org.name),
        match_domain = route.org.canonical_domain,     # BOTH, not either
        family_scope = route.family,
        reason       = mark.note or verdict )

enforced in W7 before every write:
if rejection.matches(name) or rejection.matches(domain):
    store in LIBRARY with status=excluded and the rule cited — never delete
```

**Matching on domain as well as name fixes the observed failure.** "South Carolina Manufacturers
Council" and "South Carolina Manufacturers & Commerce" are name variants of one rejected
organization; they share a domain. In the predecessor database twelve rows of that organization
survived a permanent rejection, three of them `DECISION-READY`.

Rules are **scoped by family** — rejecting a room says nothing about a channel at the same
organization.

### M2 · Pattern induction — three rejections become one rule

```
signature(route) = (family, route_type, member_unit, org_type,
                    format_class, sector, marker_combo, delivery_model)

for attr in signature_fields:
  for value in distinct(attr):
    neg = |marks where attr==value and verdict==BAD|
    pos = |marks where attr==value and verdict==GOOD|
    if neg >= 3 and pos <= 1:
        yield ProposedRule(
          action     = EXCLUDE if neg/(neg+pos) > 0.9 else DEMOTE,
          support    = neg, counterexamples = pos,
          english    = render(attr, value),
          would_have_caught = marks matching,     # shown to the founder
          would_affect_now  = live routes matching )
```

**This produces a real rule on day one from data that already exists.** The four
`DO NOT SURFACE AGAIN` decisions — PMI Atlanta, NCMA East Tennessee, USGIF, SAME Atlanta — all
share `member_unit = individual`, with zero acceptances carrying that value. M2 proposes:
*"Demote organizations whose membership unit is individuals rather than companies. Support: 4
rejections, 0 acceptances. This would currently affect 23 live routes, including 3 in your queue."*
He approves or rejects it in one click.

That is the difference between a blacklist and a system that learns.

### M3 · Weight fitting — six numbers, ~66 labels, constrained optimisation

```
P = { (a,b) : a marked GOOD, b marked BAD, same family, same week }

maximise  Σ σ( fit(a,w) − fit(b,w) )
subject to  Σ w = 100 ; 0 ≤ w_d ≤ 40 ; |w_d − current| ≤ 0.20 × current

method   scipy.optimize SLSQP — six parameters, seconds
report   Kendall τ before → after; row-by-row top-25 diff
stability  perturb each w ±20%, recompute top-25, mean Jaccard.
           < 0.70 → DO NOT SHIP, report instability instead.
gate     |P| < 20 for this family → skip and say so in the run report
```

Nothing applies without founder approval. Every score row carries its `config_hash`, so a bad fit
is a one-line rollback.

### M4 · Retrieval-augmented judging — negatives as few-shot examples

```
neighbours = top-5 most similar labelled examples (reranker similarity)
if ≥3 negative and 0 positive:
    demote — reason: "resembles {org} which you rejected: {his words}"
else:
    include neighbours as few-shot context in the judge prompt

near-misses weighted 3× when selecting neighbours —
right-org-wrong-room teaches the boundary; obvious junk does not
```

### M5 · Source reallocation — measured, floored, reported

```
per source_class, rolling 4 runs:  good_rate = marked_GOOD / routes_written
allocation ∝ good_rate, subject to a 5% floor per class
  ← never starve a source; that is how you lose the ability to find new things
good_rate < 0.05 for 4 consecutive runs → demote to monthly + flag in the report
  ← never silently disable
```

### Cold start

| Available today | Count | Feeds |
|---|---|---|
| Real founder decisions | 66 distinct | M1 and M2 immediately; M3 once split by family |
| Permanent organization rejections | 9 | M1 seed, keyed on name *and* domain |
| Rejections sharing `member_unit=individual` | 4 | M2's first proposed rule, day one |
| Recorded outcomes | **0** | Nothing yet. The first real one outweighs all web evidence. |

---

## 6. Sequencing

```
E0 ──┬─→ E1 ──┬─→ E2 ──┬─→ E3 ──→ E4 ──→ E5 ──→ E6 ──→ E7 ──┬─→ E8 ──→ E9
     │        │        └──────────────────────→ E10 ────────┤
     │        └───────────────────────────────────────→ E12 │
     └────────────────────────────────────────────────→ E13 │
                                                E9 ──→ E11 (needs marks)

M1  first output   E0 → E1 → E2 → E3.S2 → E5.S2 → E6.S1 → E7 → E9.S1
M2  both families  + E3.S3 (W13) + E4 (precision gate)
M3  full recall    + E3.S4-S8 + E10
M4  it learns      + E11 + E12
M5  hands off      + E13
```

M1 deliberately reaches a real ranked list through the thinnest possible slice so the extraction
contract meets reality before anything is built on top of it. **E5.S2 is the highest-risk story in
the plan** — everything downstream inherits its quality and it is the one component no amount of
good plumbing can fix.

---

## 7. Test strategy

| Layer | Tested | Gate |
|---|---|---|
| Unit | Rubrics, normalisation, keys, decay, scoring | 100% of `score/` and `store/keys.py`; property tests for monotonicity and bounds |
| Contract | Every provider adapter | Recorded fixtures; **no network in CI** |
| Extraction eval | Field accuracy, span validity, `not_stated` discipline | ≥90% accuracy · **zero** invented spans |
| Retrieval eval | Gate and reranker | ≥90% of good survives · ≥80% of near-miss negatives dropped |
| Dedupe eval | 500 hand-labelled rows | ≥0.98 precision · ≥0.95 recall |
| Integration | Full weekly run on frozen fixtures | Deterministic given fixed config |
| Chaos | Kill mid-run; 429 storms; malformed output; empty maps | Partial results retained; `not_reached` accurate |
| **Founder acceptance** | Does the list contain what he wants | **Share of BEST marked good, rising week over week** |

The last row is the only one that finally matters. Every other gate is a proxy.

---

## 8. Operations runbook

| Symptom | Likely cause | Action |
|---|---|---|
| Run failed, nothing written | Failure before first checkpoint | Check logs by `run_id`; re-run — checkpointing makes it safe |
| Completed, zero new routes | Provider outage, expired key, or a gate mis-tuned to reject everything | Compare candidates-generated vs survived-gate; a healthy run drops 80–95%, not 100% |
| Quarantine spike | Prompt regression or model version change | Check `hallucinated_span_count` by `prompt_version`; roll back the prompt |
| Duplicates appearing | Normalisation drift or a new name pattern | Re-run the dedupe eval; add the pattern; backfill keys |
| Spend spike | A domain with a huge URL inventory, or a crawl loop | Per-run budget halts it; inspect cost-by-provider; cap that domain |
| A founder mark disappeared | Should be impossible — defect in E1.S3 | Restore from a dated backup; **P0**; the write guard failed |
| Rejected org reappeared | Name variant with a different domain | Add the domain to the rule; consider whether M2 should generalise the pattern |
| Ranking looks wrong | Config change or a weight fit | Every score carries `config_hash`; diff configs; rollback is one line |

---

## 9. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| **Extraction quality caps everything** | High | E5.S2 built first against 100 labelled pages; span validation; independent audit; CI regression gate |
| Peer-trace mining depends on public posts | Medium | Three independent modes. Agenda archives alone suffice if social sources are unavailable. |
| Provider terms or rate limits change | Medium | Provider abstraction; two providers per critical role; fixtures make the system testable without any |
| **Gate tuned too tight; good things silently dropped** | **High** | Every drop records a reason; sampled drop review in the weekly report. A silent drop is worse than a false positive. |
| Weight fitting overfits 66 labels | Medium | ±20% cap; stability check refuses unstable fits; founder approves; one-line rollback |
| SQLite durability in ephemeral compute | Medium | Post-run copy to object storage, 30 dated copies, restore tested in E1.S5 |
| **The founder stops marking rows** | **High** | The loop dies without marks. Mitigation is ergonomic: marks adjacent to row identity, real dropdowns, seconds per row — and the visible payoff that a mark changes next week's list. |
| Scope creep into CRM and outreach | Medium | Non-goals in §1 are binding. The system finds and ranks. Art acts. |
