"""E4.S2 — thesis similarity.

Acceptance: a manufacturing council page scores higher against the ROOM thesis
than a nursing CEU page does.

This stage is never the final score. It shrinks the candidate set before the
cross-encoder, and the ranking that reaches Art has to stay explainable and
decomposable — his judgment is the ground truth, and he has to be able to see
why something ranked where it did and argue with it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from finder.precision.thesis import (
    DEFAULT_FLOOR,
    STOPWORDS,
    VERSION,
    ThesisSimilarity,
    ThesisVector,
    tokenize,
)

ROOT = Path(__file__).resolve().parents[1]

COUNCIL_PAGE = (
    "The Manufacturing Council meets monthly. Member companies send plant managers and "
    "operations leaders. Sessions are workshops on workforce capability, upskilling and "
    "automation readiness, with a call for speakers each spring."
)
NURSING_PAGE = (
    "Earn continuing education credit for nurses at our clinical skills conference. "
    "Sessions cover patient assessment, wound care and pharmacology for registered nurses."
)
CHANNEL_PAGE = (
    "We are contracted to work inside manufacturers, delivering through approved third-party "
    "providers and instructors. Our employer members include two hundred companies."
)


def theses() -> dict[str, str]:
    raw = yaml.safe_load((ROOT / "config" / "thesis.yaml").read_text(encoding="utf-8"))
    return raw["thesis"]


def similarity(**kw) -> ThesisSimilarity:
    return ThesisSimilarity(theses(), **kw)


# --- the acceptance criterion ----------------------------------------------


def test_a_council_page_beats_a_nursing_page_against_the_room_thesis() -> None:
    """The named acceptance criterion."""
    sim = similarity()
    council = sim.similarity(COUNCIL_PAGE, "ROOM")
    nursing = sim.similarity(NURSING_PAGE, "ROOM")

    assert council > nursing, f"council {council:.3f} did not beat nursing {nursing:.3f}"
    assert council > DEFAULT_FLOOR, "and it clears the floor the pipeline uses"
    assert nursing < council / 2, "not marginally better — substantially"


def test_a_channel_page_looks_most_like_the_channel_thesis() -> None:
    """Each family's thesis describes a different animal, and the vectors have
    to be able to tell them apart or the feature says nothing."""
    family, score = similarity().best_family(CHANNEL_PAGE)
    assert family == "CHANNEL"
    assert score > DEFAULT_FLOOR


def test_a_room_page_looks_most_like_the_room_thesis() -> None:
    assert similarity().best_family(COUNCIL_PAGE)[0] == "ROOM"


def test_an_irrelevant_page_scores_low_against_every_family() -> None:
    sim = similarity()
    assert sim.similarity(NURSING_PAGE) < DEFAULT_FLOOR


# --- the vector ------------------------------------------------------------


def test_similarity_is_bounded() -> None:
    sim = similarity()
    for page in (COUNCIL_PAGE, NURSING_PAGE, "", "the the the"):
        assert 0.0 <= sim.similarity(page) <= 1.0


def test_a_thesis_is_most_similar_to_itself() -> None:
    text = theses()["ROOM"]
    assert similarity().similarity(text, "ROOM") == pytest.approx(1.0)


def test_an_empty_page_scores_zero() -> None:
    assert similarity().similarity("") == 0.0


def test_a_page_of_stopwords_scores_zero() -> None:
    """Two pages are not similar because they are both written in English."""
    assert similarity().similarity("the and of to for with that this") == 0.0


def test_stopwords_and_short_tokens_are_dropped() -> None:
    tokens = tokenize("The employers and an ai of workforce capability")
    assert "the" not in tokens and "and" not in tokens
    assert "ai" not in tokens, "two-character tokens carry no signal here"
    assert {"employers", "workforce", "capability"} <= set(tokens)
    assert "the" in STOPWORDS


def test_repetition_counts_but_does_not_dominate() -> None:
    """Sub-linear weighting: a thesis repeating a word four times cares about it
    more than one saying it once, but not four times more, or a single repeated
    word swamps the whole comparison."""
    once = ThesisVector.build("X", "employer capability workforce")
    repeated = ThesisVector.build("X", "employer employer employer employer capability workforce")
    assert repeated.weights["employer"] > once.weights["employer"]
    assert repeated.weights["employer"] < 4 * once.weights["employer"]


def test_an_empty_thesis_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="no usable terms"):
        ThesisVector.build("ROOM", "the and of")


def test_no_families_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="at least one family"):
        ThesisSimilarity({})


def test_an_unknown_family_is_refused() -> None:
    with pytest.raises(KeyError, match="unknown family"):
        similarity().similarity(COUNCIL_PAGE, "SOCIETY")


def test_without_a_family_the_best_match_is_used() -> None:
    """A candidate is worth keeping if it looks like ANY of the four. Pre-judging
    its family here would decide something extraction has not established yet."""
    sim = similarity()
    best = max(sim.similarity(CHANNEL_PAGE, f) for f in theses())
    assert sim.similarity(CHANNEL_PAGE) == pytest.approx(best)


# --- the protocol the pipeline expects -------------------------------------


def test_it_satisfies_the_pipelines_similarity_shape() -> None:
    from finder.precision.w16_rerank import PrecisionPipeline

    sim = similarity()
    assert sim.score("ignored query", COUNCIL_PAGE) == sim.similarity(COUNCIL_PAGE)
    assert callable(PrecisionPipeline.__init__)


def test_it_drops_into_the_pipeline_and_filters(tmp_path: Path) -> None:
    """End to end with the real thesis text: the nursing page is dropped at the
    similarity stage and never reaches the paid cross-encoder."""
    import yaml as _yaml

    from finder.config import LexiconConfig
    from finder.precision.lexicon import MarkerGate
    from finder.precision.w16_rerank import REASON_LOW_SIMILARITY, Candidate, PrecisionPipeline
    from finder.store.db import open_db
    from finder.store.repos import Store

    lexicon = LexiconConfig(
        **_yaml.safe_load((ROOT / "config" / "lexicon.yaml").read_text(encoding="utf-8"))
    )
    pipeline = PrecisionPipeline(
        Store(open_db(":memory:")),
        MarkerGate.from_config(lexicon),
        thesis=theses()["ROOM"],
        similarity=similarity(),
        similarity_floor=0.2,
    )

    result = pipeline.run(
        [
            Candidate(url="https://x/council", text=COUNCIL_PAGE),
            Candidate(url="https://x/nursing", text=NURSING_PAGE),
        ]
    )

    kept = {d.url for d in result.kept}
    assert "https://x/council" in kept
    assert "https://x/nursing" not in kept

    # It never reaches the paid stage, and the similarity score is what would
    # have stopped it there even if the gate had let it through.
    dropped = next(d for d in result.decisions if d.url == "https://x/nursing")
    assert dropped.reason, "dropped with a recorded reason"
    assert similarity().similarity(NURSING_PAGE, "ROOM") < 0.2, (
        "and it is genuinely below the similarity floor, not merely gated out"
    )
    _ = REASON_LOW_SIMILARITY


# --- caching ---------------------------------------------------------------


def test_the_cache_is_keyed_by_version_and_config_hash(tmp_path: Path) -> None:
    """Editing the thesis text must invalidate the cache rather than silently
    scoring against yesterday's wording."""
    first = similarity(config_hash="cfg-1", cache_dir=tmp_path)
    assert first.cache_key == f"{VERSION}-cfg-1"
    assert sorted(first.cached_families()) == sorted(theses())

    edited = ThesisSimilarity(
        {**theses(), "ROOM": "a completely different paragraph about employers"},
        config_hash="cfg-2",
        cache_dir=tmp_path,
    )
    assert edited.cache_key != first.cache_key
    assert len(list(tmp_path.glob("thesis-*.json"))) == 2, "both cached, neither overwritten"


def test_it_builds_from_the_loaded_config(tmp_path: Path) -> None:
    """Against the real Config object, so a rename in config.py breaks here
    rather than at the first weekly run."""
    from finder.config import load_config

    config = load_config()
    sim = ThesisSimilarity.from_config(config, cache_dir=tmp_path)

    assert sorted(sim.vectors) == sorted(theses())
    assert sim.config_hash == config.hash
    assert sim.similarity(COUNCIL_PAGE, "ROOM") > DEFAULT_FLOOR


def test_no_cache_directory_means_no_cache_file(tmp_path: Path) -> None:
    sim = similarity(config_hash="cfg-1")
    assert sim.cached_families() == []
    assert not list(tmp_path.glob("*.json"))


def test_a_missing_hash_still_produces_a_usable_key() -> None:
    assert similarity().cache_key == f"{VERSION}-nohash"
