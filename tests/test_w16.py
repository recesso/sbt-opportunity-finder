"""E4.S4 — the precision pipeline.

Acceptance: every dropped candidate has a non-null machine-readable reason.

That is the acceptance criterion because it is the thing the predecessor could
not do. Its filtering became folklore — nobody could answer "why did we never
see the GSAE form?" except by guessing. A drop with no recorded reason is
invisible, and an invisible drop cannot be argued with, tuned, or learned from.

The other property under test is cost order: free gate, cheap similarity, paid
cross-encoder, each seeing only what survived the last. Spending the expensive
stage on candidates the free stage would have rejected is how a precision layer
becomes the most expensive part of the system while adding the least.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from finder.acquire.providers.base import FetchError
from finder.acquire.providers.rerank import RerankHit
from finder.config import LexiconConfig
from finder.context import start_run
from finder.precision.lexicon import REASON_INSUFFICIENT, MarkerGate
from finder.precision.w16_rerank import (
    REASON_LOW_RERANK,
    REASON_LOW_SIMILARITY,
    REASON_RERANK_UNAVAILABLE,
    STAGE_GATE,
    STAGE_KEPT,
    STAGE_RERANK,
    STAGE_SIMILARITY,
    Candidate,
    PrecisionPipeline,
    _page_type,
    build_features,
)
from finder.store.db import open_db
from finder.store.repos import RepoError, Store

ROOT = Path(__file__).resolve().parents[1]
THESIS = "an organization that reaches many employers and offers a way to be in the room"

GOOD = (
    "Call for speakers is open. Sessions are for plant managers and business owners "
    "working on workforce capability and automation readiness. Formats include workshop."
)
EMS = (
    "Annual EMS Conference. Call for speakers is now open. Continuing education credit "
    "for nurses and paramedics available."
)
CHANNEL = (
    "We work with manufacturers to close the skills gap. Become a provider to deliver "
    "inside member companies, reaching plant managers across the state."
)


def gate() -> MarkerGate:
    raw = yaml.safe_load((ROOT / "config" / "lexicon.yaml").read_text(encoding="utf-8"))
    return MarkerGate.from_config(LexiconConfig(**raw))


class FakeSimilarity:
    def __init__(self, scores: dict[str, float], default: float = 0.9) -> None:
        self.scores = scores
        self.default = default
        self.calls = 0

    def score(self, query: str, doc: str) -> float:
        self.calls += 1
        for needle, value in self.scores.items():
            if needle in doc:
                return value
        return self.default


class FakeReranker:
    name = "fake-rerank"
    cost_per_call_usd = 0.001

    def __init__(self, scores: list[float] | None = None, *, error: Exception | None = None):
        self.scores = scores
        self.error = error
        self.calls: list[list[str]] = []

    def rerank(self, query, docs, *, top_k=None):
        self.calls.append(list(docs))
        if self.error is not None:
            raise self.error
        scores = self.scores or [0.9] * len(docs)
        return [RerankHit(i, scores[i % len(scores)]) for i in range(len(docs))]


@pytest.fixture
def store() -> Store:
    return Store(open_db(":memory:"))


def pipeline(store: Store, **kw) -> PrecisionPipeline:
    kw.setdefault("thesis", THESIS)
    return PrecisionPipeline(store, gate(), **kw)


def candidate(url: str, text: str, **kw) -> Candidate:
    return Candidate(url=url, text=text, **kw)


# --- the acceptance criterion ----------------------------------------------


def test_every_dropped_candidate_has_a_machine_readable_reason(store: Store) -> None:
    """The criterion, and the thing the predecessor could not do."""
    candidates = [
        candidate("https://x/good", GOOD),
        candidate("https://x/ems", EMS),
        candidate("https://x/nothing", "A page about nothing in particular."),
    ]

    with start_run(store, "weekly", run_id="r-1") as run:
        result = pipeline(store, reranker=FakeReranker([0.9]), similarity=FakeSimilarity({})).run(
            candidates, run=run
        )

    for decision in result.decisions:
        if not decision.kept:
            assert decision.reason, f"{decision.url} was dropped with no reason"
            assert " " not in decision.reason, "machine-readable, not a sentence"

    rows = store.decisions.for_run("r-1")
    assert len(rows) == 3, "every candidate got a row, including the drops"
    assert all(r["reason"] for r in rows if not r["kept"])


def test_a_drop_can_be_traced_back_by_url(store: Store) -> None:
    """'Why did we never see the GSAE form?' should be a query, not archaeology."""
    with start_run(store, "weekly", run_id="r-1") as run:
        pipeline(store, reranker=FakeReranker()).run([candidate("https://x/ems", EMS)], run=run)

    history = store.decisions.why_dropped("https://x/ems")
    assert len(history) == 1
    assert history[0]["reason"] == REASON_INSUFFICIENT
    assert history[0]["stage"] == STAGE_GATE
    assert json.loads(history[0]["features"])["combo"]


def test_the_repository_refuses_a_drop_with_no_reason(store: Store) -> None:
    """Enforced at the boundary, not left to callers to remember."""
    with pytest.raises(RepoError, match="needs a reason"):
        store.decisions.record(
            decision_id="d1",
            run_id="r-1",
            url="https://x/",
            kept=False,
            stage=STAGE_GATE,
            reason="",
            decided_at="t",
        )


def test_drop_reasons_are_countable_for_a_run(store: Store) -> None:
    candidates = [
        candidate("https://x/ems", EMS),
        candidate("https://x/nothing", "nothing at all"),
        candidate("https://x/good", GOOD),
    ]
    with start_run(store, "weekly", run_id="r-1") as run:
        pipeline(store, reranker=FakeReranker([0.9])).run(candidates, run=run)

    assert store.decisions.drop_reasons("r-1")[REASON_INSUFFICIENT] == 2


# --- cost order ------------------------------------------------------------


def test_the_expensive_stage_never_sees_what_the_free_one_rejected(store: Store) -> None:
    """Spending the cross-encoder on candidates the gate would have rejected is
    how a precision layer becomes the most expensive part of the system."""
    reranker = FakeReranker()
    similarity = FakeSimilarity({})
    candidates = [candidate("https://x/good", GOOD)] + [
        candidate(f"https://x/junk{i}", "nothing relevant") for i in range(5)
    ]

    pipeline(store, similarity=similarity, reranker=reranker).run(candidates)

    assert similarity.calls == 1, "similarity saw only the gate's survivor"
    assert len(reranker.calls[0]) == 1, "and the reranker saw only similarity's"


def test_a_low_similarity_candidate_never_reaches_the_reranker(store: Store) -> None:
    reranker = FakeReranker()
    result = pipeline(
        store,
        similarity=FakeSimilarity({"skills gap": 0.01}),
        reranker=reranker,
        similarity_floor=0.5,
    ).run([candidate("https://x/ch", CHANNEL), candidate("https://x/good", GOOD)])

    dropped = next(d for d in result.decisions if not d.kept)
    assert dropped.stage == STAGE_SIMILARITY
    assert dropped.reason == REASON_LOW_SIMILARITY
    assert dropped.similarity == 0.01
    assert len(reranker.calls[0]) == 1


def test_a_low_rerank_score_is_dropped_with_its_score(store: Store) -> None:
    result = pipeline(store, reranker=FakeReranker([0.05]), rerank_floor=0.5).run(
        [candidate("https://x/good", GOOD)]
    )
    dropped = result.decisions[0]
    assert (dropped.kept, dropped.stage, dropped.reason) == (
        False,
        STAGE_RERANK,
        REASON_LOW_RERANK,
    )
    assert dropped.rerank_score == 0.05


def test_nothing_surviving_the_gate_costs_no_rerank_call(store: Store) -> None:
    reranker = FakeReranker()
    pipeline(store, reranker=reranker).run([candidate("https://x/ems", EMS)])
    assert reranker.calls == []


# --- failure modes ---------------------------------------------------------


def test_a_reranker_outage_keeps_candidates_rather_than_dropping_them(store: Store) -> None:
    """The right failure mode. A candidate dropped because a vendor was down is
    invisible; one wrongly kept costs a single extraction and shows up in the
    report."""
    with start_run(store, "weekly", run_id="r-1") as run:
        result = pipeline(store, reranker=FakeReranker(error=FetchError("503"))).run(
            [candidate("https://x/good", GOOD)], run=run
        )

    assert result.kept, "kept, not dropped"
    assert result.kept[0].reason == REASON_RERANK_UNAVAILABLE
    not_reached = store.runs.get("r-1").not_reached
    assert not_reached[0]["reason"] == REASON_RERANK_UNAVAILABLE
    assert "kept unranked rather than dropped" in not_reached[0]["detail"]


def test_a_reranker_outage_without_a_run_still_keeps_the_candidates(store: Store) -> None:
    """A one-off script has no run to report to, and must not lose the week's
    candidates because there was nowhere to write the warning."""
    result = pipeline(store, reranker=FakeReranker(error=FetchError("503"))).run(
        [candidate("https://x/good", GOOD)]
    )
    assert len(result.kept) == 1
    assert result.kept[0].reason == REASON_RERANK_UNAVAILABLE


def test_kept_and_dropped_can_be_queried_separately(store: Store) -> None:
    """The two questions a review asks: what survived, and what did not."""
    with start_run(store, "weekly", run_id="r-1") as run:
        pipeline(store, reranker=FakeReranker([0.9])).run(
            [candidate("https://x/good", GOOD), candidate("https://x/ems", EMS)], run=run
        )

    assert [r["url"] for r in store.decisions.for_run("r-1", kept=True)] == ["https://x/good"]
    assert [r["url"] for r in store.decisions.for_run("r-1", kept=False)] == ["https://x/ems"]
    assert len(store.decisions.for_run("r-1")) == 2


def test_with_no_reranker_configured_the_gate_survivors_are_kept(store: Store) -> None:
    """So the pipeline is usable before the paid stage is wired up."""
    result = pipeline(store).run([candidate("https://x/good", GOOD)])
    assert len(result.kept) == 1
    assert result.kept[0].rerank_score is None


def test_a_missing_rerank_score_is_a_drop_not_a_pass(store: Store) -> None:
    """A reranker returning fewer rows than it was sent must not silently keep
    the ones it forgot."""

    class Partial(FakeReranker):
        def rerank(self, query, docs, *, top_k=None):
            self.calls.append(list(docs))
            return [RerankHit(0, 0.9)]

    result = pipeline(store, reranker=Partial()).run(
        [candidate("https://x/good", GOOD), candidate("https://x/ch", CHANNEL)]
    )
    assert len(result.kept) == 1
    assert [d.reason for d in result.decisions if not d.kept] == [REASON_LOW_RERANK]


def test_an_empty_thesis_is_a_programming_error(store: Store) -> None:
    with pytest.raises(ValueError, match="needs thesis text"):
        PrecisionPipeline(store, gate(), thesis="   ")


def test_no_candidates_is_a_quiet_empty_pass(store: Store) -> None:
    with start_run(store, "weekly", run_id="r-1") as run:
        result = pipeline(store, reranker=FakeReranker()).run([], run=run)
    assert result.as_dict()["candidates"] == 0
    assert store.runs.get("r-1").not_reached == []


# --- features --------------------------------------------------------------


def test_an_offdomain_submission_link_is_a_feature(store: Store) -> None:
    """The GSAE case: the form is a SurveyMonkey link in body text. A page that
    links out to a submission host is far more likely to be a real way in than
    one that does not, whatever its own prose says."""
    hosts = ["surveymonkey.com", "sessionize.com"]
    with_link = candidate(
        "https://gsae.org/speaker-interest-form",
        GOOD,
        links=("https://www.surveymonkey.com/r/NKSQCY6",),
    )
    without = candidate("https://gsae.org/speaker-interest-form", GOOD)

    g = gate().evaluate(GOOD)
    assert build_features(with_link, g, submission_hosts=hosts)["has_offdomain_submission_link"]
    assert not build_features(without, g, submission_hosts=hosts)["has_offdomain_submission_link"]


def test_a_link_to_its_own_host_is_not_an_offdomain_link(store: Store) -> None:
    hosts = ["eventbrite.com"]
    on_platform = candidate(
        "https://eventbrite.com/e/123", GOOD, links=("https://eventbrite.com/e/123/register",)
    )
    g = gate().evaluate(GOOD)
    assert not build_features(on_platform, g, submission_hosts=hosts)[
        "has_offdomain_submission_link"
    ]


def test_the_feature_vector_carries_what_the_reranker_needs(store: Store) -> None:
    c = candidate(
        "https://x.org/about/committees",
        "The education committee sets programming. Our member companies attend.",
        matched_term="committees",
    )
    features = build_features(c, gate().evaluate(c.text))

    assert features["names_programming_owner"] is True
    assert features["names_employers"] is True
    assert features["matched_term"] == "committees"
    assert features["page_type"] == "committee"
    assert "combo" in features and "classes_hit" in features


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("https://x/events/spring", "event"),
        ("https://x/about/committees", "committee"),
        ("https://x/call-for-speakers", "submission"),
        ("https://x/approved-providers", "provider"),
        ("https://x/membership", "membership"),
        ("https://x/about/history", "other"),
    ],
)
def test_the_page_type_is_a_coarse_shape_from_the_url(url: str, kind: str) -> None:
    assert _page_type(url) == kind


# --- reporting -------------------------------------------------------------


def test_the_run_counters_tell_the_funnel_story(store: Store) -> None:
    candidates = [
        candidate("https://x/good", GOOD),
        candidate("https://x/ch", CHANNEL),
        candidate("https://x/ems", EMS),
    ]
    with start_run(store, "weekly", run_id="r-1") as run:
        pipeline(store, reranker=FakeReranker([0.9, 0.05])).run(candidates, run=run)

    counters = store.runs.get("r-1").counters
    assert counters["candidates"] == 3
    assert counters["survived_gate"] == 2
    assert counters["survived_rerank"] == 1


def test_reranking_is_charged_to_the_run(store: Store) -> None:
    with start_run(store, "weekly", run_id="r-1") as run:
        pipeline(store, reranker=FakeReranker()).run([candidate("https://x/good", GOOD)], run=run)
    assert store.costs.by_provider("r-1")["fake-rerank"] > 0


def test_survivors_come_back_best_first(store: Store) -> None:
    """So the caller spends its extraction budget from the top down."""
    result = pipeline(store, reranker=FakeReranker([0.3, 0.95])).run(
        [candidate("https://x/good", GOOD), candidate("https://x/ch", CHANNEL)]
    )
    assert [c.url for c in result.ranked] == ["https://x/ch", "https://x/good"]


def test_the_result_summarises_itself(store: Store) -> None:
    result = pipeline(store, reranker=FakeReranker([0.9])).run(
        [candidate("https://x/good", GOOD), candidate("https://x/ems", EMS)]
    )
    summary = result.as_dict()
    assert summary == {"candidates": 2, "kept": 1, "dropped": {REASON_INSUFFICIENT: 1}}


def test_a_decision_explains_itself(store: Store) -> None:
    result = pipeline(store, reranker=FakeReranker([0.9])).run(
        [candidate("https://x/good", GOOD), candidate("https://x/ems", EMS)]
    )
    kept = next(d for d in result.decisions if d.kept)
    dropped = next(d for d in result.decisions if not d.kept)
    assert kept.explain().startswith("kept")
    assert dropped.explain() == f"dropped at {STAGE_GATE}: {REASON_INSUFFICIENT}"
    assert kept.stage == STAGE_KEPT


def test_re_running_a_run_updates_rather_than_duplicates(store: Store) -> None:
    """A resumed run must not write two verdicts for one URL."""
    candidates = [candidate("https://x/good", GOOD)]
    with start_run(store, "weekly", run_id="r-1") as run:
        pipeline(store, reranker=FakeReranker([0.9])).run(candidates, run=run)
        pipeline(store, reranker=FakeReranker([0.9])).run(candidates, run=run)

    assert len(store.decisions.for_run("r-1")) == 1


def test_decisions_are_not_persisted_without_a_run(store: Store) -> None:
    """A one-off script has no run to attribute rows to, and an unattributed
    decision row cannot be compared against anything."""
    pipeline(store, reranker=FakeReranker()).run([candidate("https://x/good", GOOD)])
    assert store.decisions.count() == 0
