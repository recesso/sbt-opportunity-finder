"""W16 — the precision pipeline: gate, then similarity, then cross-encoder.

Three stages in strict cost order, and each one only sees what survived the last.
That ordering is the whole design:

* **the gate is free** and runs on everything,
* **similarity is cheap** and runs on what the gate kept,
* **the cross-encoder is paid** and runs only on what similarity kept.

Spending the expensive stage on candidates the free stage could have rejected is
how a precision layer becomes the most expensive part of the system while adding
the least.

**Every candidate gets a decision row, including the drops.** That is the point
of this module as much as the filtering is. The predecessor's filtering became
folklore because drops left no trace, and nobody could answer "why did we never
see the GSAE form?" except by guessing. Here, every drop carries a
machine-readable reason and the features that produced it, so a recall problem
is a query rather than an archaeology project.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from finder.acquire.providers.base import FetchError
from finder.acquire.providers.rerank import RerankProvider
from finder.context import RunContext
from finder.precision.lexicon import GateResult, MarkerGate
from finder.store import ids
from finder.store.db import utcnow
from finder.store.repos import Store

STAGE_GATE = "gate"
STAGE_SIMILARITY = "similarity"
STAGE_RERANK = "rerank"
STAGE_KEPT = "kept"

REASON_LOW_SIMILARITY = "below_similarity_floor"
REASON_LOW_RERANK = "below_rerank_floor"
REASON_RERANK_UNAVAILABLE = "rerank_unavailable"

# Deliberately low. This stage is protecting the extraction budget, not making
# the final call — scoring does that, on extracted fields, deterministically.
# A candidate wrongly dropped here never reaches a human at all.
DEFAULT_SIMILARITY_FLOOR = 0.15
DEFAULT_RERANK_FLOOR = 0.20


class Similarity(Protocol):
    """Cheap relevance between the thesis and a candidate, 0..1."""

    def score(self, query: str, doc: str) -> float: ...


@dataclass(frozen=True, slots=True)
class Candidate:
    """What the precision layer is asked to judge."""

    url: str
    text: str
    org_id: str | None = None
    title: str = ""
    matched_term: str = ""
    links: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Decision:
    """Why one candidate was kept or dropped, and what produced that."""

    url: str
    kept: bool
    stage: str
    reason: str = ""
    combo: str = ""
    similarity: float | None = None
    rerank_score: float | None = None
    features: dict[str, Any] = field(default_factory=dict)
    org_id: str | None = None

    def explain(self) -> str:
        if self.kept:
            return f"kept ({self.combo}; rerank {self.rerank_score:.2f})".replace("None", "n/a")
        return f"dropped at {self.stage}: {self.reason}"


def build_features(
    candidate: Candidate,
    gate: GateResult,
    *,
    submission_hosts: Sequence[str] = (),
) -> dict[str, Any]:
    """The reranker's inputs, and the drop report's evidence.

    ``has_offdomain_submission_link`` earns its place here specifically: the
    GSAE form is a SurveyMonkey link in body text, and a page that links out to
    a submission host is far more likely to be a real way in than one that does
    not — regardless of what its own prose says.
    """
    text = f"{candidate.title}\n{candidate.text}".lower()
    return {
        "classes_hit": list(gate.classes_hit),
        "combo": gate.combo,
        "matched_term": candidate.matched_term,
        "positives": gate.positives,
        "negatives": gate.negatives,
        "has_offdomain_submission_link": _links_out_to_submission(candidate, submission_hosts),
        "names_programming_owner": any(
            phrase in text
            for phrase in (
                "program chair",
                "program committee",
                "education committee",
                "program director",
                "director of programs",
                "member programs",
            )
        ),
        "names_employers": any(
            phrase in text
            for phrase in ("member companies", "employer members", "our members include")
        ),
        "page_type": _page_type(candidate.url),
    }


def _links_out_to_submission(candidate: Candidate, hosts: Sequence[str]) -> bool:
    own = candidate.url.lower()
    return any(
        host.lower() in link.lower() and host.lower() not in own
        for link in candidate.links
        for host in hosts
    )


def _page_type(url: str) -> str:
    """A coarse shape from the URL. A feature, never a decision."""
    lowered = url.lower()
    for marker, kind in (
        ("/event", "event"),
        ("/calendar", "event"),
        ("committee", "committee"),
        ("council", "committee"),
        ("speak", "submission"),
        ("call-for", "submission"),
        ("proposal", "submission"),
        ("provider", "provider"),
        ("partner", "provider"),
        ("service", "provider"),
        ("member", "membership"),
    ):
        if marker in lowered:
            return kind
    return "other"


@dataclass(slots=True)
class PrecisionResult:
    """One pass of the precision layer. Read the drop counts, not just the keeps."""

    decisions: list[Decision] = field(default_factory=list)
    ranked: list[Candidate] = field(default_factory=list)

    @property
    def kept(self) -> list[Decision]:
        return [d for d in self.decisions if d.kept]

    def dropped_by_reason(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for d in self.decisions:
            if not d.kept:
                counts[d.reason] = counts.get(d.reason, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidates": len(self.decisions),
            "kept": len(self.kept),
            "dropped": self.dropped_by_reason(),
        }


class PrecisionPipeline:
    """Gate, similarity, cross-encoder — in that order, for that reason."""

    def __init__(
        self,
        store: Store,
        gate: MarkerGate,
        *,
        thesis: str,
        similarity: Similarity | None = None,
        reranker: RerankProvider | None = None,
        similarity_floor: float = DEFAULT_SIMILARITY_FLOOR,
        rerank_floor: float = DEFAULT_RERANK_FLOOR,
        submission_hosts: Sequence[str] = (),
    ) -> None:
        if not thesis.strip():
            raise ValueError("the precision layer needs thesis text to compare against")
        self.store = store
        self.gate = gate
        self.thesis = thesis
        self.similarity = similarity
        self.reranker = reranker
        self.similarity_floor = similarity_floor
        self.rerank_floor = rerank_floor
        self.submission_hosts = list(submission_hosts)

    def run(
        self,
        candidates: Sequence[Candidate],
        *,
        run: RunContext | None = None,
        top_k: int | None = None,
    ) -> PrecisionResult:
        result = PrecisionResult()
        survivors: list[tuple[Candidate, dict[str, Any], GateResult, float | None]] = []

        # Stage 1 — the gate. Free, so it runs on everything.
        for candidate in candidates:
            gate = self.gate.evaluate(candidate.url, candidate.title, candidate.text)
            features = build_features(candidate, gate, submission_hosts=self.submission_hosts)
            if not gate.passed:
                result.decisions.append(
                    Decision(
                        url=candidate.url,
                        org_id=candidate.org_id,
                        kept=False,
                        stage=STAGE_GATE,
                        reason=gate.reason,
                        combo=gate.combo,
                        features=features,
                    )
                )
                continue
            survivors.append((candidate, features, gate, None))

        if run is not None:
            run.count("candidates", len(candidates))
            run.count("survived_gate", len(survivors))

        # Stage 2 — similarity. Cheap, so it runs on what the gate kept.
        scored: list[tuple[Candidate, dict[str, Any], GateResult, float | None]] = []
        for candidate, features, gate, _ in survivors:
            score = (
                None
                if self.similarity is None
                else self.similarity.score(self.thesis, candidate.text)
            )
            if score is not None and score < self.similarity_floor:
                result.decisions.append(
                    Decision(
                        url=candidate.url,
                        org_id=candidate.org_id,
                        kept=False,
                        stage=STAGE_SIMILARITY,
                        reason=REASON_LOW_SIMILARITY,
                        combo=gate.combo,
                        similarity=score,
                        features=features,
                    )
                )
                continue
            scored.append((candidate, features, gate, score))

        # Stage 3 — the cross-encoder. Paid, so it runs only on the rest.
        self._rerank(scored, result, run=run, top_k=top_k)

        if run is not None:
            run.count("survived_rerank", len(result.kept))
            run.log.info("precision_pass", **result.as_dict())
        self._persist(result, run)
        return result

    # --- stage 3 ----------------------------------------------------------

    def _rerank(
        self,
        scored: list[tuple[Candidate, dict[str, Any], GateResult, float | None]],
        result: PrecisionResult,
        *,
        run: RunContext | None,
        top_k: int | None,
    ) -> None:
        if not scored:
            return

        if self.reranker is None:
            self._keep_all(scored, result, rerank_score=None)
            return

        markers = sorted({t for _, f, _, _ in scored for t in f.get("classes_hit", [])})
        try:
            hits = self.reranker.rerank(self.thesis, [c.text for c, _, _, _ in scored], top_k=top_k)
        except FetchError as exc:
            # Keeping everything is the right failure mode. A candidate dropped
            # because a vendor was down is invisible; one wrongly kept costs one
            # extraction and is visible in the report.
            if run is not None:
                run.record_not_reached(
                    REASON_RERANK_UNAVAILABLE,
                    f"{exc} — {len(scored)} candidates kept unranked rather than dropped",
                    count=len(scored),
                )
            self._keep_all(scored, result, rerank_score=None, reason=REASON_RERANK_UNAVAILABLE)
            return

        if run is not None:
            run.cost.record(
                getattr(self.reranker, "name", "rerank"),
                "rerank",
                units=len(scored),
                usd=getattr(self.reranker, "cost_per_call_usd", 0.0),
            )

        by_index = {hit.index: hit.score for hit in hits}
        for order, (candidate, features, gate, similarity) in enumerate(scored):
            score = by_index.get(order)
            if score is None or score < self.rerank_floor:
                result.decisions.append(
                    Decision(
                        url=candidate.url,
                        org_id=candidate.org_id,
                        kept=False,
                        stage=STAGE_RERANK,
                        reason=REASON_LOW_RERANK,
                        combo=gate.combo,
                        similarity=similarity,
                        rerank_score=score,
                        features=features,
                    )
                )
                continue
            result.decisions.append(
                Decision(
                    url=candidate.url,
                    org_id=candidate.org_id,
                    kept=True,
                    stage=STAGE_KEPT,
                    combo=gate.combo,
                    similarity=similarity,
                    rerank_score=score,
                    features=features,
                )
            )
            result.ranked.append(candidate)

        # Best first, so the caller spends its extraction budget top down.
        order_by_url = {d.url: (d.rerank_score or 0.0) for d in result.kept}
        result.ranked.sort(key=lambda c: -order_by_url.get(c.url, 0.0))
        _ = markers  # reserved for provider-side windowing

    def _keep_all(
        self,
        scored: list[tuple[Candidate, dict[str, Any], GateResult, float | None]],
        result: PrecisionResult,
        *,
        rerank_score: float | None,
        reason: str = "",
    ) -> None:
        for candidate, features, gate, similarity in scored:
            result.decisions.append(
                Decision(
                    url=candidate.url,
                    org_id=candidate.org_id,
                    kept=True,
                    stage=STAGE_KEPT,
                    reason=reason,
                    combo=gate.combo,
                    similarity=similarity,
                    rerank_score=rerank_score,
                    features=features,
                )
            )
            result.ranked.append(candidate)

    # --- persistence ------------------------------------------------------

    def _persist(self, result: PrecisionResult, run: RunContext | None) -> None:
        """Write every decision. A drop with no row is a drop nobody can find."""
        if run is None:
            return
        for decision in result.decisions:
            self.store.decisions.record(
                decision_id=ids.evidence_id(run.run_id, "precision", decision.url),
                run_id=run.run_id,
                url=decision.url,
                org_id=decision.org_id,
                kept=decision.kept,
                stage=decision.stage,
                reason=decision.reason or ("" if decision.kept else "unspecified"),
                combo=decision.combo,
                similarity=decision.similarity,
                rerank_score=decision.rerank_score,
                features=json.dumps(decision.features, sort_keys=True),
                decided_at=utcnow(),
            )
