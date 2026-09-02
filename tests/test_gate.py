"""E4.S1 — the marker co-occurrence gate.

Acceptance: the EMS conference page is rejected with a recorded reason, and the
ETA call-for-speakers page passes with an employer-audience combination.

Those two pages came out of the same broad "call for speakers" search run by
hand on 2026-09-01. Both contain a call for speakers. One is an opportunity and
one is a conference for paramedics, and no keyword tells them apart — only the
co-occurrence of an employer audience with a published way in does. That is the
whole thesis of this file.

The gate is tuned for RECALL: a page wrongly kept costs one extraction, a page
wrongly dropped is gone from the week. Several tests below assert that it keeps
things a stricter filter would lose.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from finder.config import LexiconConfig
from finder.precision.lexicon import (
    REASON_INSUFFICIENT,
    REASON_NEGATIVE,
    REASON_NO_RELEVANCE,
    GateResult,
    MarkerGate,
    class_letter,
    count_matches,
    drop_report,
    normalize,
)

ROOT = Path(__file__).resolve().parents[1]
LABELLED = ROOT / "tests" / "fixtures" / "gate_labelled.json"


def lexicon() -> LexiconConfig:
    raw = yaml.safe_load((ROOT / "config" / "lexicon.yaml").read_text(encoding="utf-8"))
    return LexiconConfig(**raw)


def gate() -> MarkerGate:
    return MarkerGate.from_config(lexicon())


def labelled() -> list[dict[str, Any]]:
    return json.loads(LABELLED.read_text(encoding="utf-8"))["pages"]


def labelled_page(page_id: str) -> dict[str, Any]:
    return next(p for p in labelled() if p["id"] == page_id)


# --- the two pages the gate exists for -------------------------------------


def test_the_ems_conference_is_rejected_with_a_recorded_reason() -> None:
    """The false positive observed by hand. It hits class E — there really is a
    call for speakers on it — and nothing in class C, because its audience is
    paramedics. One class is not evidence."""
    result = gate().evaluate(labelled_page("ems_conference")["text"])

    assert result.passed is False
    assert result.reason == REASON_INSUFFICIENT
    assert "E_access" in result.classes_hit
    assert not any(c.startswith("C_") for c in result.classes_hit)
    assert "one class is not evidence" in result.explain()


def test_the_eta_call_passes_with_an_employer_audience_and_a_way_in() -> None:
    """The true positive from the same search — the one Art submitted to."""
    result = gate().evaluate(labelled_page("eta_call")["text"])

    assert result.passed is True
    assert "C" in result.combo and "E" in result.combo
    assert result.reason == ""


def test_the_two_pages_are_separated_by_co_occurrence_alone() -> None:
    """Both contain a call for speakers. No keyword tells them apart."""
    ems = labelled_page("ems_conference")["text"]
    eta = labelled_page("eta_call")["text"]
    access = lexicon().classes["E_access"]

    assert count_matches(ems, access), "the EMS page really does have a call for speakers"
    assert count_matches(eta, access)
    assert gate().evaluate(ems).passed is not gate().evaluate(eta).passed


# --- the whole labelled set ------------------------------------------------


@pytest.mark.parametrize("spec", labelled(), ids=lambda s: s["id"])
def test_every_labelled_page_lands_where_it_should(spec: dict[str, Any]) -> None:
    result = gate().evaluate(spec["text"])
    expect = spec["expect"]

    assert result.passed is expect["passed"], f"{spec['id']}: {result.explain()}"
    if "reason" in expect:
        assert result.reason == expect["reason"]
    for letter in expect.get("combo_contains", []):
        assert letter in result.combo, f"{spec['id']} lost class {letter}"


def test_the_labelled_set_carries_the_cases_that_actually_happened() -> None:
    ids = {p["id"] for p in labelled()}
    assert {"ems_conference", "eta_call", "woodworking_expo"} <= ids
    assert all(p["note"] for p in labelled()), "each page says what it is for"


def test_the_gate_keeps_the_channel_page() -> None:
    """The family Art named as the goal. A gate that dropped provider pages
    would remove the CHANNEL family from the system entirely."""
    assert gate().evaluate(labelled_page("gamep_channel")["text"]).passed


def test_the_gate_keeps_a_council_with_no_call_for_speakers() -> None:
    """Tuned for recall: an employer audience plus a real format is enough. A
    council seat is a way in even when nobody published a form."""
    result = gate().evaluate(labelled_page("chamber_council")["text"])
    assert result.passed
    assert "E" not in result.combo, "and it got there without a published call"


# --- normalisation ---------------------------------------------------------


def test_punctuation_and_case_do_not_hide_a_marker() -> None:
    """Real markup is full of hyphens, asterisks and capitals."""
    assert gate().evaluate(labelled_page("punctuated_markers")["text"]).passed


@pytest.mark.parametrize(
    ("text", "term"),
    [
        ("Call-for-Speakers!", "call for speakers"),
        ("*Plant Managers*", "plant manager"),
        ("VP-of-Operations", "vp of operations"),
        ("submit an RFP.", "rfp"),
        ("Time-to-Competency", "time-to-competency"),
        ("“lunch and learn”", "lunch and learn"),
    ],
)
def test_a_marker_survives_the_punctuation_around_it(text: str, term: str) -> None:
    assert count_matches(text, [term]) == (term,)


def test_normalize_pads_so_phrases_have_boundaries() -> None:
    assert normalize("  A-B  c ") == " a b c "


def test_a_marker_does_not_fire_inside_an_unrelated_word() -> None:
    """A plural is the same marker; a different word that merely contains it is
    not. 'council' matching 'councilman' would make every civic page a council
    seat — the same bug the URL matcher had."""
    assert count_matches("councilman smith spoke", ["council"]) == ()
    assert count_matches("the manufacturing council meets", ["council"]) == ("council",)
    assert count_matches("our councils meet monthly", ["council"]) == ("council",)


def test_markers_are_found_across_several_texts() -> None:
    """URL, title and body all carry markers, and a title-only match is a match."""
    result = gate().evaluate(
        "https://x.org/call-for-speakers", "Speak at our event", "For plant managers."
    )
    assert result.passed
    assert "C" in result.combo and "E" in result.combo


# --- the rules -------------------------------------------------------------


def test_one_class_is_never_enough() -> None:
    result = gate().evaluate(labelled_page("subject_only")["text"])
    assert result.reason == REASON_INSUFFICIENT
    assert result.classes_hit == ("A_subject_core",)


def test_the_minimum_is_configurable_and_enforced() -> None:
    classes = {"A_x": ["alpha"], "B_y": ["beta"], "C_z": ["gamma"]}
    text = "alpha and beta"
    assert MarkerGate(classes, min_classes=2).evaluate(text).passed is True
    assert MarkerGate(classes, min_classes=3).evaluate(text).passed is False


def test_a_minimum_below_one_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        MarkerGate({"A_x": ["alpha"]}, min_classes=0)


def test_a_gate_with_no_positive_classes_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="at least one positive"):
        MarkerGate({"N_negative": ["job seeker"]})


def test_negatives_are_weighed_by_density_not_presence() -> None:
    """A page about upskilling that mentions a career fair once is still about
    upskilling. A career-fair page that mentions upskilling once is not."""
    classes = {
        "A_x": ["upskilling", "time to competence", "skills gap"],
        "C_y": ["plant manager"],
        "N_negative": ["career fair"],
    }
    mostly_good = "upskilling for plant manager audiences, skills gap work, one career fair booth"
    mostly_bad = "career fair. career fair. career fair. plant manager. upskilling."

    assert MarkerGate(classes).evaluate(mostly_good).passed is True
    result = MarkerGate(classes).evaluate(mostly_bad)
    assert result.passed is False
    assert result.reason == REASON_NEGATIVE
    assert "different audience" in result.explain()


def test_coverage_is_checked_before_negatives() -> None:
    """The reported reason should be the first thing wrong, so a drop report
    points at the real problem rather than the last check to run."""
    classes = {"A_x": ["alpha"], "C_y": ["gamma"], "N_negative": ["bad"]}
    result = MarkerGate(classes).evaluate("alpha bad bad bad")
    assert result.reason == REASON_INSUFFICIENT


def test_a_page_with_no_markers_at_all_is_dropped_cleanly() -> None:
    result = gate().evaluate("")
    assert (result.passed, result.reason, result.classes_hit) == (
        False,
        REASON_INSUFFICIENT,
        (),
    )
    assert result.combo == ""


# --- what the gate hands downstream ----------------------------------------


def test_the_combo_is_a_sorted_letter_code() -> None:
    classes = {"E_access": ["alpha"], "C_audience": ["gamma"], "A_subject": ["beta"]}
    assert MarkerGate(classes).evaluate("alpha beta gamma").combo == "ACE"


def test_class_letter_is_the_first_character() -> None:
    assert class_letter("A_subject_core") == "A"
    assert class_letter("e_access") == "E"


def test_a_strong_combination_is_recognised() -> None:
    """A reranker feature, not a second gate: CE is the strongest thing this
    cheap layer can say, and downstream weighs it rather than obeying it."""
    g = gate()
    assert g.is_strong("CE") is True
    assert g.is_strong("ce") is True
    assert g.is_strong("AB") is False


def test_the_strong_combinations_come_from_config() -> None:
    assert gate().strong_combinations == tuple(lexicon().strong_combinations)


def test_the_result_records_which_terms_matched() -> None:
    """A drop that cannot be argued with cannot be tuned."""
    result = gate().evaluate(labelled_page("eta_call")["text"])
    assert "call for speakers" in result.matched["E_access"]
    assert result.positives >= len(result.classes_hit)


def test_the_result_summarises_itself() -> None:
    summary = gate().evaluate(labelled_page("ems_conference")["text"]).as_dict()
    assert summary["passed"] is False
    assert summary["reason"] == REASON_INSUFFICIENT
    assert "E_access" in summary["classes_hit"]


def test_shape_alone_is_not_relevance() -> None:
    """The woodworking expo. A format plus a published way in describes every
    conference on earth; something has to say it is about the right subject or
    the right people."""
    result = gate().evaluate(labelled_page("woodworking_expo")["text"])

    assert result.passed is False
    assert result.reason == REASON_NO_RELEVANCE
    assert set(result.combo) <= {"D", "E"}, "shape classes only"
    assert "shape without relevance" in result.explain()


def test_the_required_classes_come_from_config() -> None:
    assert gate().require_classes == tuple(lexicon().require_classes)
    assert set(gate().require_classes) == {"A", "B", "C"}


def test_relevance_is_checked_after_coverage() -> None:
    """A page hitting only one class should say so, not blame relevance."""
    classes = {"D_x": ["clinic"], "E_y": ["call for papers"], "A_z": ["upskilling"]}
    g = MarkerGate(classes, require_classes=["A"])
    assert g.evaluate("clinic").reason == REASON_INSUFFICIENT
    assert g.evaluate("clinic and call for papers").reason == REASON_NO_RELEVANCE
    assert g.evaluate("clinic and upskilling").passed is True


def test_no_required_classes_means_the_rule_is_off() -> None:
    """Configurable, and off by default, so a caller with a different lexicon is
    not silently held to this one's shape."""
    classes = {"D_x": ["clinic"], "E_y": ["call for papers"]}
    assert MarkerGate(classes).evaluate("clinic and call for papers").passed is True


def test_a_plural_audience_marker_still_matches() -> None:
    """Real pages say "Plant Managers". Missing the plural of the audience
    marker is a recall bug in the class that matters most."""
    assert count_matches("For Plant Managers and Business Owners", ["plant manager"]) == (
        "plant manager",
    )
    assert count_matches("workshops all day", ["workshop"]) == ("workshop",)


def test_repeats_are_counted_as_density() -> None:
    """A page saying "career fair" three times is more about career fairs than
    one saying it once, and distinct-term counting would call those the same."""
    from finder.precision.lexicon import occurrences

    assert occurrences("career fair. career fair. career fair.", ["career fair"]) == 3
    assert occurrences("nothing here", ["career fair"]) == 0


def test_a_drop_report_counts_the_reasons() -> None:
    """The number that matters when tuning: a gate dropping ninety per cent of a
    week for one reason is either working or broken, and only this says which to
    go and look at."""
    g = gate()
    results = {p["id"]: g.evaluate(p["text"]) for p in labelled()}

    report = drop_report(results)
    assert report["kept"] == sum(1 for r in results.values() if r.passed)
    assert report[REASON_INSUFFICIENT] >= 2
    assert report[REASON_NO_RELEVANCE] >= 1
    assert report[REASON_NEGATIVE] >= 1
    assert sum(report.values()) == len(labelled())


def test_a_passing_result_explains_itself_too() -> None:
    assert "kept" in gate().evaluate(labelled_page("eta_call")["text"]).explain()


def test_an_unknown_reason_still_explains() -> None:
    assert "dropped: something_else" in GateResult(passed=False, reason="something_else").explain()
