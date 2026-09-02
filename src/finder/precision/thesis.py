"""Thesis similarity — the cheap middle stage.

One canonical paragraph per family from ``config/thesis.yaml``, turned into a
weighted term vector once and cached. Its only job is to shrink the candidate
set before the cross-encoder, which is where the real judgement happens. It is
**never** the final score: the ranking has to stay explainable and decomposable,
because the founder's judgment is the ground truth and he has to be able to see
why something ranked where it did and argue with it.

**A deliberate deviation, stated rather than buried.** The backlog says "embedded
once and stored", which implies an embedding API. This uses a local, deterministic
term-overlap vector instead, and the reason is proportion: an embedding vendor
would add a fourth API, a fourth key and a fourth failure mode to a stage whose
entire purpose is to be *cheap and roughly right* ahead of the cross-encoder that
does the actual work. The :class:`~finder.precision.w16_rerank.Similarity`
Protocol is unchanged, so an embedding-backed implementation drops in later
without touching the pipeline.

What is kept from the spec regardless: the cache is keyed by ``(version,
config_hash)``, so changing the thesis text invalidates it rather than silently
scoring against yesterday's wording.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

VERSION = "lexical-1"

_TOKEN = re.compile(r"[a-z0-9][a-z0-9'-]*")

# Words that appear in every page ever written. Keeping them would make two
# unrelated pages look similar because they both use English.
STOPWORDS: frozenset[str] = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "can",
        "could",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "he",
        "her",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "more",
        "most",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "out",
        "over",
        "said",
        "she",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "to",
        "too",
        "under",
        "until",
        "up",
        "upon",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    ]
)

# Below this, a candidate shares almost nothing with the thesis but the language
# it is written in. Used as the pipeline's default floor.
DEFAULT_FLOOR = 0.15


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.casefold()) if t not in STOPWORDS and len(t) > 2]


@dataclass(frozen=True, slots=True)
class ThesisVector:
    """One family's thesis as a weighted term vector.

    Weights are sub-linear in frequency: a thesis repeating "employer" four times
    cares about employers more than one saying it once, but not four times more,
    and linear weights let a single repeated word dominate the whole comparison.
    """

    family: str
    weights: dict[str, float]
    norm: float

    @classmethod
    def build(cls, family: str, text: str) -> ThesisVector:
        counts = Counter(tokenize(text))
        if not counts:
            raise ValueError(f"the {family} thesis has no usable terms")
        weights = {term: 1.0 + math.log(n) for term, n in counts.items()}
        norm = math.sqrt(sum(w * w for w in weights.values()))
        return cls(family=family, weights=weights, norm=norm)

    def similarity(self, text: str) -> float:
        """Cosine of the candidate against this thesis, 0..1.

        Only terms the thesis actually contains contribute. A page can be long
        and full of other things; what is being asked is how much of THIS
        thesis it speaks to.
        """
        counts = Counter(tokenize(text))
        if not counts or self.norm == 0:
            return 0.0

        # Non-empty counts guarantee every weight is at least 1.0, so the norm
        # cannot be zero here and there is no divide-by-zero branch to guard.
        candidate = {term: 1.0 + math.log(n) for term, n in counts.items()}
        candidate_norm = math.sqrt(sum(w * w for w in candidate.values()))

        dot = sum(weight * candidate.get(term, 0.0) for term, weight in self.weights.items())
        return max(0.0, min(1.0, dot / (self.norm * candidate_norm)))


class ThesisSimilarity:
    """Similarity against each family's thesis, built once and cached.

    Satisfies the ``Similarity`` Protocol the precision pipeline expects, so it
    can be handed to :class:`~finder.precision.w16_rerank.PrecisionPipeline`
    directly.
    """

    def __init__(
        self,
        theses: dict[str, str],
        *,
        config_hash: str = "",
        cache_dir: Path | str | None = None,
    ) -> None:
        if not theses:
            raise ValueError("thesis similarity needs at least one family thesis")
        self.config_hash = config_hash
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.vectors = {family: ThesisVector.build(family, text) for family, text in theses.items()}
        self._default_family = next(iter(self.vectors))
        if self.cache_dir is not None:
            self._save_cache()

    @classmethod
    def from_config(cls, config, cache_dir: Path | str | None = None) -> ThesisSimilarity:
        return cls(
            dict(config.thesis.thesis),
            config_hash=getattr(config, "hash", ""),
            cache_dir=cache_dir,
        )

    def similarity(self, text: str, family: str | None = None) -> float:
        """How much of the family's thesis this text speaks to.

        With no family given, the best match across families is used: a
        candidate is worth keeping if it looks like ANY of the four, and
        pre-judging its family here would decide something extraction has not
        established yet.
        """
        if family is not None:
            vector = self.vectors.get(family)
            if vector is None:
                raise KeyError(f"unknown family {family!r}; expected {sorted(self.vectors)}")
            return vector.similarity(text)
        return max(v.similarity(text) for v in self.vectors.values())

    def best_family(self, text: str) -> tuple[str, float]:
        """The family this text most resembles, and by how much. A hint for the
        extractor, never a decision — the extractor reads the page."""
        scored = [(v.family, v.similarity(text)) for v in self.vectors.values()]
        return max(scored, key=lambda pair: (pair[1], pair[0]))

    # --- the Similarity Protocol -----------------------------------------

    def score(self, query: str, doc: str) -> float:
        """Protocol shape: ``query`` is ignored because the thesis IS the query.

        Kept in the signature so an embedding-backed implementation, which does
        need the query text, is a drop-in replacement.
        """
        return self.similarity(doc)

    # --- caching ----------------------------------------------------------

    @property
    def cache_key(self) -> str:
        """Keyed by version AND config hash, so editing the thesis text
        invalidates the cache rather than silently scoring against yesterday's
        wording."""
        return f"{VERSION}-{self.config_hash or 'nohash'}"

    def _cache_path(self) -> Path:
        return Path(self.cache_dir or ".") / f"thesis-{self.cache_key}.json"

    def _save_cache(self) -> None:
        path = self._cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": VERSION,
                    "config_hash": self.config_hash,
                    "families": {
                        f: {"weights": v.weights, "norm": v.norm} for f, v in self.vectors.items()
                    },
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def cached_families(self) -> list[str]:
        path = self._cache_path()
        if not path.exists():
            return []
        return sorted(json.loads(path.read_text(encoding="utf-8"))["families"])
