"""The marker co-occurrence gate — where precision actually comes from.

Precision comes from CO-OCCURRENCE, not from keywords. A page must hit at least
``min_classes`` distinct marker classes to survive.

That single rule is the difference between the two searches run by hand on
2026-09-01. A broad "call for speakers" query returned an EMS conference and a
woodworking expo; the EMS page hits class E (there is a call for speakers on it)
and nothing in class C (its audience is paramedics, not operators). One class is
not evidence. Two classes co-occurring is.

The gate is deliberately cheap and deliberately dumb. It runs before any model
call, on every candidate, and its whole job is to stop the expensive stages
being pointed at pages that were never going to work. It is tuned for **recall**
at this stage: a page it wrongly keeps costs one extraction, while a page it
wrongly drops is gone from the week entirely. When in doubt it keeps.

Every rejection records WHY, and which classes did hit. A gate that drops pages
without saying why cannot be tuned, and a recall problem in it would be
invisible — which is exactly how the predecessor's filtering became folklore.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

# Punctuation stripped at token boundaries so "call-for-speakers," and
# "call for speakers" are the same phrase.
_SEPARATORS = re.compile(r"[-_/\\+.,;:!?()\[\]{}'\"“”‘’|*#><=&~`@]+")
_WHITESPACE = re.compile(r"\s+")

REASON_INSUFFICIENT = "insufficient_class_coverage"
REASON_NO_RELEVANCE = "shape_without_relevance"
REASON_NEGATIVE = "negative_dominant"


def normalize(text: str) -> str:
    """Casefold, collapse whitespace, strip punctuation at token boundaries.

    Padded with spaces so a phrase can be matched with boundaries on both sides:
    "rfp" must not fire on "rfps-are-closed" reading as one token, and it must
    fire on "submit an RFP."
    """
    flattened = _SEPARATORS.sub(" ", text.casefold())
    return f" {_WHITESPACE.sub(' ', flattened).strip()} "


def class_letter(name: str) -> str:
    """``A_subject_core`` -> ``A``. The combo code is built from these."""
    return name[0].upper()


@dataclass(frozen=True, slots=True)
class GateResult:
    """Whether a page survives, and everything needed to argue with that."""

    passed: bool
    classes_hit: tuple[str, ...] = ()
    combo: str = ""
    reason: str = ""
    positives: int = 0
    negatives: int = 0
    matched: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "combo": self.combo,
            "classes_hit": list(self.classes_hit),
            "reason": self.reason,
            "positives": self.positives,
            "negatives": self.negatives,
        }

    def explain(self) -> str:
        """One line a human can read in a drop report."""
        if self.passed:
            return f"kept ({self.combo or 'no combo'}; {self.positives} markers)"
        if self.reason == REASON_INSUFFICIENT:
            hit = ", ".join(self.classes_hit) or "nothing"
            return f"dropped: only {hit} matched, and one class is not evidence"
        if self.reason == REASON_NO_RELEVANCE:
            return (
                f"dropped: {self.combo} is shape without relevance — a format and a way "
                "in, but nothing about the right subject or the right people"
            )
        if self.reason == REASON_NEGATIVE:
            return (
                f"dropped: {self.negatives} negative markers against {self.positives} "
                "positive — this page is about a different audience"
            )
        return f"dropped: {self.reason}"


def count_matches(text: str, terms: Sequence[str]) -> tuple[str, ...]:
    """Which of ``terms`` appear in ``text``, as whole phrases.

    Bounded phrase containment, so "rfp" does not fire inside "rfps-are-closed".
    A trailing "s" IS accepted, because real pages say "Plant Managers" and a
    gate that misses the plural of its own audience marker is a recall bug in
    the class that matters most.
    """
    normalized = normalize(text)
    return tuple(term for term in terms if _present(normalize(term).strip(), normalized))


def _present(term: str, normalized_text: str) -> bool:
    return f" {term} " in normalized_text or f" {term}s " in normalized_text


def occurrences(text: str, terms: Sequence[str]) -> int:
    """How many times the terms appear, counting repeats.

    Density, not presence: a page saying "career fair" three times is more about
    career fairs than one saying it once, and counting distinct terms would
    treat those as the same page.
    """
    normalized = normalize(text)
    total = 0
    for term in terms:
        needle = re.escape(normalize(term).strip())
        # Lookahead so adjacent repeats both count: str.count would consume the
        # shared space between "career fair career fair" and see one, not two.
        total += len(re.findall(rf"(?=\s{needle}s?\s)", normalized))
    return total


class MarkerGate:
    """Cheap co-occurrence filter, run before anything expensive."""

    def __init__(
        self,
        classes: dict[str, list[str]],
        *,
        min_classes: int = 2,
        require_classes: Sequence[str] = (),
        negative_prefix: str = "N_",
        strong_combinations: Sequence[str] = (),
    ) -> None:
        if min_classes < 1:
            raise ValueError("min_classes must be at least 1")
        self.positive = {k: v for k, v in classes.items() if not k.startswith(negative_prefix)}
        self.negative: list[str] = []
        for name, terms in classes.items():
            if name.startswith(negative_prefix):
                self.negative.extend(terms)
        if not self.positive:
            raise ValueError("the gate needs at least one positive marker class")
        self.min_classes = min_classes
        self.require_classes = tuple(r.upper() for r in require_classes)
        self.strong_combinations = tuple(c.upper() for c in strong_combinations)

    @classmethod
    def from_config(cls, lexicon) -> MarkerGate:
        return cls(
            dict(lexicon.classes),
            min_classes=lexicon.min_classes,
            require_classes=lexicon.require_classes,
            strong_combinations=lexicon.strong_combinations,
        )

    def evaluate(self, *texts: str) -> GateResult:
        """Judge one page. Several texts are joined — url, title and body all
        carry markers, and a title-only match is still a match."""
        text = "\n".join(t for t in texts if t)

        matched: dict[str, tuple[str, ...]] = {}
        for name, terms in self.positive.items():
            hits = count_matches(text, terms)
            if hits:
                matched[name] = hits

        negatives = occurrences(text, self.negative)
        positives = sum(occurrences(text, terms) for terms in matched.values())
        classes_hit = tuple(sorted(matched))
        combo = "".join(sorted({class_letter(n) for n in classes_hit}))

        if len(classes_hit) < self.min_classes:
            return GateResult(
                passed=False,
                classes_hit=classes_hit,
                combo=combo,
                reason=REASON_INSUFFICIENT,
                positives=positives,
                negatives=negatives,
                matched=matched,
            )

        # Shape is not relevance. D (format) and E (access) describe what KIND
        # of page this is; A, B and C establish it is about the right subject or
        # the right people. "Call for presentations" plus "hands-on clinic" is
        # every conference on earth, and it is how the woodworking expo survived
        # a broad search.
        if self.require_classes and not set(combo) & set(self.require_classes):
            return GateResult(
                passed=False,
                classes_hit=classes_hit,
                combo=combo,
                reason=REASON_NO_RELEVANCE,
                positives=positives,
                negatives=negatives,
                matched=matched,
            )

        # Density, not presence. A page about upskilling that mentions a career
        # fair once is still about upskilling; a career-fair page that mentions
        # upskilling once is not.
        if negatives > positives:
            return GateResult(
                passed=False,
                classes_hit=classes_hit,
                combo=combo,
                reason=REASON_NEGATIVE,
                positives=positives,
                negatives=negatives,
                matched=matched,
            )

        return GateResult(
            passed=True,
            classes_hit=classes_hit,
            combo=combo,
            positives=positives,
            negatives=negatives,
            matched=matched,
        )

    def is_strong(self, combo: str) -> bool:
        """Whether this combination is in the observed precision order.

        A reranker feature, not a second gate. CE — an employer audience plus a
        published way in — is the strongest thing this cheap layer can say.
        """
        return combo.upper() in self.strong_combinations


def drop_report(results: dict[str, GateResult]) -> dict[str, int]:
    """How many candidates each reason accounted for.

    The number that matters when tuning: a gate dropping ninety per cent of a
    week for one reason is either doing its job or broken, and only this tells
    you which to go and look at.
    """
    counts: dict[str, int] = {}
    for result in results.values():
        key = "kept" if result.passed else result.reason
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
