# Build Spec — what the system is

**Status:** v3, 2026-09-01. Authoritative. Where this conflicts with anything else, this wins.

Companion: `01-delivery-plan.md` (how it gets built). Rules of engagement: `../CLAUDE.md`.

---

## 1. Purpose

Find, rank and surface the rooms, organizations, employers and people where Skill Bridge Talent
can engage employers consultatively — and get better every week from what the founder decides.

The goal is **clients and revenue**, reached by being present with employers: delivering a
workshop, presenting, sitting in a room, or just talking to people. All four are equally valid.
Never hard selling.

Priority sectors: **fintech and payments · defense and the DIB · supply chain and logistics ·
manufacturing · healthcare.**

The leverage that matters most is **one-to-many**: SBT → a convening organization → its many
employer members, or SBT → an employer-serving organization → its board, members and client
employers.

---

## 2. Four families. The unit of work is a ROUTE, not an event.

Every prior version stored events, which is why none could answer *"how do I get on this
circuit?"* — the answer is not an event, it is a way in.

A **route** is `(target, mechanism, how you get in)`.

| family | The opportunity is | Event? |
|---|---|---|
| **ROOM** | A gathering with employers in it — present, workshop, council seat, or just talk | Yes |
| **CHANNEL** | An organization relationship reaching its employers **with no event required** | No |
| **EMPLOYER** | A specific company with a live trigger | No |
| **PERSON** | A named individual who is themself the path | No |

**CHANNEL is the family the founder named as the goal**, and it has the fewest events in it.
GPS Education Partners has no call for speakers. At GaMEP the highest-value route is probably not
the lunch-and-learn but becoming an instructor or delivery partner, because that puts Capability
Engineering inside 300+ plant engagements a year. An event-shaped system ranks the luncheon and
never sees the partnership.

Route types and their base scores are in **`config/families.yaml`**.

### Two rules that prevent the two observed failures

1. **`route_url` and `evidence_url` are different fields.** `route_url` is the form, application,
   intake or registration you act on. `evidence_url` proves the claims. **A route with a null
   `route_url` cannot enter BEST** — it goes to WORTH A LOOK with a generated question.

2. **The mechanism is frequently off-domain.** GSAE's speaker form is a SurveyMonkey link in body
   text; it is not on gsae.org, which is why it could not be found by hand. Extraction must
   capture outbound links to the hosts in `config/hosts.yaml` and treat them as `route_url`.

---

## 3. Source classes — the recall engine

Fourteen classes. Classes 1–10 are **direct evidence** (what an organization says about itself);
11–14 are **indirect evidence** (traces other people leave). Full table with harvest methods and
cadence: **`config/sources.yaml`**.

**Governing principle: indirect evidence of ACCESS beats direct evidence of EXISTENCE.** One post
reading *"great session with the Georgia Manufacturing Alliance on getting your workforce ready
for automation"* establishes in a sentence that GMA runs sessions, programs outside practitioners,
on this subject, in this format — and names someone who got in. No amount of reading their website
produces that.

### The highest-yield techniques

- **Network templates (class 1).** MEP centers, chapter networks, state associations. Solve the
  pattern at one node, apply it to all N. Seeds in `config/networks.yaml`.
- **Domain mapping (classes 2, 3, 8).** One `map` call per organization with
  `PROGRAMMING_PATHS` or `PARTNER_PATHS` from `config/paths.yaml`. No crawl.
  *Validated 2026-09-01: one map of gamep.org surfaced an entire statewide series.*
- **Peer-trace mining (class 11).** Harvest 20–40 adjacent practitioners' speaking histories.
  Every venue on those lists is a proven outside-expert venue on this subject. It inverts
  discovery: instead of asking 800 organizations whether they take speakers, start from people
  who already got in.
- **Provider directories (class 8).** A published third-party provider directory names the slot,
  the criteria, the approver and the incumbents — in one page. One per state for MEP.

### How a trace becomes a route

```
trace found        "delivered a workshop on automation readiness at GMA"
      ↓
entity extraction  org / person / topic / format / date
      ↓
resolve            existing org? attach. new org? create + queue for mapping.
      ↓
emits three things 1. precedent evidence on the GMA route (score 5, with span)
                   2. a PERSON route for the practitioner
                   3. a discovery if GMA was not already known
```

A trace is **never itself an opportunity**. It is evidence that upgrades one, or a pointer to one
not yet found. Traces decay in *confidence*, never in *precedent* — a 2023 trace still proves the
organization has programmed outsiders.

---

## 4. Retrieval — recall first, then precision

Two stages, deliberately separated, with different cost profiles. Merging them is what produced
1,689 rows of noise in the predecessor system.

```
STAGE 1 — RECALL        cheap · broad · tolerant of noise
  objective: miss nothing. A false positive costs cents.
             A missed opportunity is invisible forever.
  output:    candidate pool, 10-50x the final list. That is correct.

STAGE 2 — PRECISION     expensive · narrow · intolerant of noise
  marker gate → thesis similarity → cross-encoder rerank → extraction → scoring
  typically drops 80-95% BEFORE anything expensive runs
```

**Precision comes from marker co-occurrence, not keywords.** Six classes in
`config/lexicon.yaml`; a page must hit **at least two**. This single rule is why a broad
"call for speakers" search returns EMS conferences and a gated one does not — the EMS page hits
class E and nothing in class C. Strong combinations in observed precision order: `CE, AE, CD, AC, BD`.

**A silently dropped good opportunity is a worse defect than a false positive.** Every drop
records a machine-readable reason, and sampled drops appear in the weekly report.

---

## 5. Extraction contract

Fetch and write are separate processes. The model that writes a record sees only stored snapshot
text, never a memory of having browsed. **Every decision-bearing field carries the quoted span
that supports it. A field with no span cannot be written.**

Field wrapper: `{value: T | NOT_STATED, span: str | None, source_url: str}`.

Common fields plus per-family extensions:

- **common** — family, target_name, route_type, route_url (+is_offdomain), mechanism_name, owner,
  subject_signals, sector, geography, eligibility, evidence_url, fetched_at, content_hash,
  unresolved[]
- **ROOM** — deadline, next_occurrence, formats_accepted, session_length, cost, precedent,
  audience{stated_roles, member_unit, named_employers, expected_size}
- **CHANNEL** — employer_relationship{nature, count, named_employers}, delivery_model,
  intake{url, criteria, approver, scope_contracted}, replication{network_id, peer_node_count},
  existing_providers[]
- **EMPLOYER** — company, **trigger**{kind, what, date, source_url, capability_implication},
  problem_owner, reachable_via{channel_route_id, person_id}
- **PERSON** — person, controls, known_to_art *(founder-entered only)*, connector, role_change

### Non-negotiable prompt clauses

- `not_stated` is a **correct and preferred** answer. Say so explicitly.
- Never infer audience from a title, an organization name, or a sponsor list.
- Never treat a past cycle as a current open one.
- Return the verbatim span for every value. No span → `not_stated`.
- Separate what the page *says* from what you *conclude*. Only what it says is written.
- Today's date is supplied; compare every deadline to it.

**Second-pass audit.** A different model, different prompt, re-extracts from the same snapshot.
Field-level diff on decision-bearing fields. Disagreement quarantines rather than publishes.
Same model twice correlates its own errors, which is the whole point of using a different one.

---

## 6. Scoring rubrics

Nine dimensions, each 0–5 against written anchors, computed from extracted fields only — never
from a model's overall impression. Full anchors are implemented in `src/finder/score/rubrics.py`
and specified here.

| Dimension | Applies to | 5 | 3 | 0 |
|---|---|---|---|---|
| **employer_presence** | ROOM | Named member companies or a roster with operator/exec titles | `member_unit=company` and this is a member room | Generic assertion, or member_unit=individual, or students/HR |
| **relationship_depth** | CHANNEL | `contracted`/`funded` — paid to work *inside* employers | `members` where the unit is companies, with a count | Individual membership, or no employer relationship evidenced |
| **trigger_strength** | EMPLOYER | Recency-decayed; see `config/families.yaml` triggers | — | No trigger, or decayed below 1.0 |
| **reach** | all | Statewide/national body serving 500+ employers, or a replicable network node | Chapter or council with 25+ employer members | Unknown |
| **subject_proximity** | all | Their programming names workforce capability, time-to-competency, automation/AI readiness, onboarding, expertise retention | Business performance or growth themes where capability is implicit | Off-topic |
| **sector_match** | all | One of the five is the organization's primary focus | Cross-sector but employer/operator audience | Outside |
| **repeatability** | all | Recurring monthly/quarterly **and** replicates across a peer network | Annual, but with continuous machinery around it | One-off, no successor |
| **precedent** | ROOM, CHANNEL | A named outside expert delivered here, documented | Past programs list non-staff presenters | None found |
| **access_warmth** | all | Active relationship with someone who controls this | Credible one-hop introduction | No named human |

Two notes that matter:

- **`access_warmth` rolls up as the BEST known path, never an average.** One strong relationship
  opens a door and must not be diluted by the people you don't know.
- **`access_warmth` is the one dimension the system cannot research.** It is founder-entered. The
  system's job is to *ask* — every new CHANNEL or EMPLOYER route prompts "do you know anyone
  here?" — and to treat the answer as first-class evidence surviving every rebuild.

---

## 7. The algorithm

Deterministic. No model output enters a score. Same inputs always give the same numbers, and every
number decomposes back to fields and spans.

```
FIT = round( 100 × Σ( weight[family][d] × score[d] / 5 ) / Σ weight[family][d] )

ROUTE = round( 100 × (route_base / 5) × openness )

CONFIDENCE = round( 100 × ( 0.50 × evidence_level/5
                          + 0.30 × field_completeness
                          + 0.20 × recency ) )
```

Weights per family, openness multipliers, confidence components and thresholds: **`config/weights.yaml`**
and **`config/families.yaml`**.

**Weights differ by family** because the families are not the same kind of thing. A CHANNEL is
judged mostly on how firmly it holds employers and how many it reaches. An EMPLOYER is judged
mostly on whether something just changed. A PERSON is judged almost entirely on relationship
strength.

**`route_type` is deliberately absent from FIT in every family.** A superb channel with no known
way in is still a superb channel — it goes to WORTH A LOOK with a question, never down-ranked
into invisibility.

**Geography appears in no weight column.** It is a display facet and a sort key. A national event
with employers in the room, a workshop slot and the right sector is a top opportunity wherever it
is. What geography was smuggling in — *can I be here repeatedly* — is now `repeatability`, which
is about cadence and network replication, not distance.

### Routing to the surfaces

```
BEST          FIT ≥ 65  and  ROUTE ≥ 50  and  CONFIDENCE ≥ 70  and  route_url is not null
WORTH A LOOK  FIT ≥ 65  and ( ROUTE < 50 or CONFIDENCE < 70 or route_url is null )
              → the single blocking question is generated
LIBRARY       everything else — retained, searchable, never deleted

sort within family   FIT × ROUTE / 100, descending
pinned above sort    door closing within 21 days, or a trigger under 30 days old
```

**These are quality bars, not volume caps.** If 400 routes clear them, 400 routes appear. The
system never pre-selects a subset. The founder decides how many is enough.

---

## 8. Data model

Full DDL lives in `src/finder/store/migrations/`. Shape:

```
organization · network
route  ← the unit of work, all four families
  ├── route_room     (1:1 where family = ROOM)
  └── route_channel  (1:1 where family = CHANNEL)
occurrence · employer · trigger · person
evidence   ← one row per claim; this is what makes scores auditable
score      ← fit, route_score, confidence, components jsonb, config_hash
signal · founder_mark · rejection · config_version · run · stage_run
```

**Founder-owned fields are in their own table with no write path from any worker.**

**Dedupe keys are computed before every write and checked against the whole table:**
`series_key = normalize(target|mechanism)`, `occurrence_key = series_key|date`, plus canonical
domain and fuzzy match. In the predecessor database 880 of the 976 keyed rows were inside
duplicate clusters — the keys existed and were never checked.

---

## 9. The sixteen workers

| | | |
|---|---|---|
| **W1** NetworkRegistrar | monthly | Enumerate every network's real node list; semantic discovery; graph expansion |
| **W2** RouteMapper | weekly, tiered | One map call per org with `PROGRAMMING_PATHS` → ROOM candidates |
| **W3** MechanismExtractor | per candidate | **Highest-risk component.** Snapshot text → route draft with spans |
| **W4** RoomProfiler | per org | `member_unit`, named employers, council inventory, reach, precedent |
| **W5** PlatformSweeper | weekly/daily | Submission platforms, event platforms, Grants.gov, SAM.gov |
| **W6** ChangeWatcher | daily | Content diff on watched URLs → signals; never sets status directly |
| **W7** Resolver | before every write | Dedupe, entity resolution, **cross-family linking** |
| **W8** Scorer | per route | Deterministic; no LLM |
| **W9** Auditor | before publish | Independent re-extraction, different model, quarantine on diff |
| **W10** QuestionWriter | WORTH A LOOK | One blocking question, a named target, a draft message |
| **W11** BriefBuilder | weekly | BEST / WORTH A LOOK / NEW / CHANGED, grouped by family |
| **W12** Learner | on mark | Rules, weights, relationship capture |
| **W13** ChannelProspector | weekly | `PARTNER_PATHS` + provider directories → CHANNEL candidates |
| **W14** TriggerWatcher | daily | Award feeds and announcements → employers + dated triggers |
| **W15** TraceHunter | weekly/monthly | Peer histories, session reports, agenda archives, public minutes |
| **W16** Reranker | per candidate | Marker gate → similarity → cross-encoder, with drop reasons |

**Cross-family linking is where compounding happens.** A company on a channel's client roster
becomes an `employer` reached via that channel. A trigger firing there surfaces as an EMPLOYER
route typed `CHANNEL_INTRO` with base 5 — because a way in already exists. That link *is*
one-to-many, and it cannot exist without the CHANNEL family.

---

## 10. Workflows

```
WF-1  DAILY 06:00 ET       W6 changes · W14 triggers · nightly trigger decay
WF-2  WEEKLY SUN 18:00 ET  recall (W2, W13, W5, W15) → W16 precision gate
                           → W3/W4 extraction → W7 resolve → W8 score
                           → W9 audit → W10 questions → W11 brief → run report
WF-3  MONTHLY 1st SUN      W1 registry refresh · propagate solved provider patterns
                           across peer nodes · deep re-verify > 90 days · config drift
WF-4  ON FOUNDER MARK      W12 learner → rules, person rows, weight proposal, re-rank
```

Every stage checkpoints. A killed run resumes. `not_reached[]` is mandatory output.

---

## 11. What the founder sees

Four lists — **BEST**, **WORTH A LOOK**, **NEW THIS WEEK**, **CHANGED** — grouped by family so a
partnership never competes for a slot with a luncheon. Uncapped.

Every row: the target · the opportunity · **why it reaches employers** · how you get in ·
**act here** (`route_url`) · who to talk to and whether you know them · what they want · when ·
multiplier · fit/route/confidence (each expandable to components and spans) · the open question
if any · evidence links with fetch dates · **your mark**.

Plus **where** — city, state, drive time — as a sortable, filterable facet that changes no score.

---

## 12. Learning

Five mechanisms, staged by the data each needs. None is model training. All are inspectable and
reversible. Full design in `01-delivery-plan.md` §5.

| | Changes | Needs | Live from |
|---|---|---|---|
| **M1** Exclusion memory | What can be written at all | 1 mark | Day 1 |
| **M2** Pattern induction | Proposes rules demoting a class | ≥3 same signature | Day 1 |
| **M3** Weight fitting | The six FIT weights per family | ≥20 marks in family | ~Month 2 |
| **M4** kNN veto | Precision gate decisions | ≥10 negatives | ~Week 3 |
| **M5** Source reallocation | Crawl effort per source | 4 runs of yield | ~Month 2 |

**The measure that tells you the loop is working: the share of BEST rows the founder marks good,
week over week, per family.** Everything else is diagnostics.
