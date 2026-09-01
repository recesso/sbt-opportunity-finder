"""E1.S4 — normalisation and content dedupe keys.

Two evaluation sets, deliberately separate:

* ``dedupe_labelled.json`` — 500 auto-generated pairs from the predecessor
  export, labelled by signals this code does not use.
* ``dedupe_hard_cases.json`` — hand-curated pairs where the judgement is the
  whole point: a name variant versus a sibling sub-body, a shared platform host
  versus a shared organization. An automated labeller mislabels these; a first
  attempt called JAX Chamber Health Council and JAX Chamber IT Council the same
  organization.

The headline precision/recall numbers come from the 500. The hard cases are
asserted individually, because an average hides exactly the cases that broke the
predecessor.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from finder.store.keys import (
    PLATFORM_HOSTS,
    chapter_qualifier,
    is_generic_program_page,
    is_named_subbody,
    is_platform_host,
    normalize_mechanism,
    normalize_org,
    occurrence_key,
    org_identity,
    registrable_domain,
    same_organization,
    series_key,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --- domains ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://gamep.org/events", "gamep.org"),
        ("https://www.gamep.org/", "gamep.org"),
        ("https://members.tagonline.org/calendar", "tagonline.org"),
        ("https://business.athensga.com/events/details/x", "athensga.com"),
        ("http://GAMEP.ORG:8080/x", "gamep.org"),
        ("gamep.org", "gamep.org"),
        ("https://scmc.glueup.com/events/", "glueup.com"),
        ("", ""),
        ("not a url", "not a url"),
    ],
)
def test_registrable_domain(url: str, expected: str) -> None:
    assert registrable_domain(url) == expected


def test_hosting_subdomains_are_stripped_but_real_ones_are_not() -> None:
    """dekalb.chambermaster.com keeps its subdomain — it is a platform tenant,
    and the tenant is the only thing distinguishing it."""
    assert registrable_domain("https://members.tagonline.org") == "tagonline.org"
    assert registrable_domain("https://dekalb.chambermaster.com") == "chambermaster.com"


def test_platform_hosts_are_recognised() -> None:
    assert is_platform_host("https://scmc.glueup.com/events/")
    assert is_platform_host("https://www.surveymonkey.com/r/NKSQCY6")
    assert not is_platform_host("https://gamep.org/events")


# --- organization names ----------------------------------------------------


def test_body_words_do_not_carry_identity() -> None:
    """'Georgia Association of Manufacturers' and 'Georgia Manufacturers
    Association' are one body."""
    assert normalize_org("Georgia Association of Manufacturers") == normalize_org(
        "Georgia Manufacturers Association"
    )


def test_normalisation_handles_case_accents_and_punctuation() -> None:
    assert normalize_org("  Café  Manufacturers, Inc. ") == normalize_org("cafe manufacturers")


def test_a_name_of_only_body_words_still_normalises_to_something() -> None:
    """Never normalise a name out of existence."""
    assert normalize_org("The Association") != ""


@given(st.text())
def test_normalize_org_never_raises(text: str) -> None:
    normalize_org(text)


# --- chapter qualifiers ----------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected_tokens"),
    [
        ("SAME Charleston Post (Society of American Military Engineers)", {"charleston"}),
        ("CSCMP Atlanta Roundtable", {"atlanta"}),
        ("HFMA Alabama Chapter", {"alabama"}),
        ("Georgia HFMA", {"georgia"}),
        ("South Carolina Manufacturers Council", {"south", "carolina"}),
        ("South Carolina Manufacturers & Commerce", {"south", "carolina"}),
        ("Georgia Manufacturing Alliance", {"georgia"}),
    ],
)
def test_chapter_qualifier(name: str, expected_tokens: set[str]) -> None:
    assert set(chapter_qualifier(name).split()) == expected_tokens


def test_council_is_not_a_chapter_marker() -> None:
    """Including 'council' as a marker splits the two South Carolina names
    apart — which is the exact failure this must prevent."""
    assert chapter_qualifier("South Carolina Manufacturers Council") == chapter_qualifier(
        "South Carolina Manufacturers & Commerce"
    )


# --- organization identity -------------------------------------------------


def test_identity_separates_chapters_on_a_shared_domain() -> None:
    """Nine SAME posts share same.org. A domain-only key merges all of them."""
    charleston = org_identity("SAME Charleston Post", "https://www.same.org/charleston")
    atlanta = org_identity("SAME Atlanta Post", "https://www.same.org/atlanta")
    assert charleston != atlanta


def test_identity_merges_name_variants_on_a_shared_domain() -> None:
    """The case the predecessor got wrong: twelve rows of a permanently
    rejected organization survived under a variant name."""
    a = org_identity("South Carolina Manufacturers & Commerce", "https://www.myscmc.org/events")
    b = org_identity("South Carolina Manufacturers Council", "https://www.myscmc.org/councils")
    assert a == b


def test_identity_ignores_a_platform_host() -> None:
    """Two unrelated organizations both cite glueup.com in the real data."""
    a = org_identity("Maritime Association of South Carolina", "https://x.glueup.com/e")
    b = org_identity("South Carolina Manufacturers Council", "https://y.glueup.com/e")
    assert a != b
    assert "glueup" not in a and "glueup" not in b


def test_identity_needs_something_to_work_with() -> None:
    with pytest.raises(ValueError):
        org_identity("", "")


# --- mechanisms ------------------------------------------------------------


def test_years_and_ordinals_do_not_split_a_recurring_mechanism() -> None:
    """The occurrence key carries the date; the series must not."""
    assert normalize_mechanism("2nd Annual DeKalb Manufacturers Summit") == normalize_mechanism(
        "3rd Annual DeKalb Manufacturers Summit 2027"
    )


def test_named_subbodies_are_recognised() -> None:
    for text in (
        "Industry Council",
        "Education Committee",
        "Atlanta Roundtable",
        "Fintech Society",
        "Aerospace Task Force",
    ):
        assert is_named_subbody(text), text


def test_generic_program_pages_are_recognised() -> None:
    for text in ("Forums", "Speaker Bank", "Events", "Membership", "Calendar"):
        assert is_generic_program_page(text), text
    assert not is_generic_program_page("Industry Council")


# --- keys ------------------------------------------------------------------


def test_series_key_is_stable_and_content_based() -> None:
    a = series_key("gamep.org|georgia", "Lunch and Learn series")
    b = series_key("gamep.org|georgia", "  lunch  and  learn   SERIES ")
    assert a == b


def test_generic_pages_collapse_onto_the_organization() -> None:
    """A 'Forums' page and an 'Events' page at one body are not two routes."""
    assert series_key("x.org", "Forums") == series_key("x.org", "Events")


def test_named_subbodies_stay_separate() -> None:
    assert series_key("jaxchamber.com", "Health Council") != series_key(
        "jaxchamber.com", "IT Council"
    )


def test_occurrence_key_separates_dates_and_groups_recurring() -> None:
    series = series_key("gamep.org", "Lunch and Learn")
    assert occurrence_key(series, "2026-10-15") != occurrence_key(series, "2026-11-19")
    assert occurrence_key(series, None) == occurrence_key(series, "recurring")


def test_keys_require_their_inputs() -> None:
    with pytest.raises(ValueError):
        series_key("", "anything")
    with pytest.raises(ValueError):
        occurrence_key("", "2026-01-01")


# --- the hand-curated hard cases -------------------------------------------


def _hard_cases() -> list[dict]:
    return load("dedupe_hard_cases.json")["pairs"]


@pytest.mark.parametrize("case", _hard_cases(), ids=lambda c: c["a_org"][:38])
def test_hard_case(case: dict) -> None:
    """Each of these was read by hand. An average would hide them."""
    same_identity = org_identity(case["a_org"], case["a_url"]) == org_identity(
        case["b_org"], case["b_url"]
    )
    fuzzy = same_organization(case["a_org"], case["a_url"], case["b_org"], case["b_url"])
    verdict = same_identity or fuzzy

    assert verdict is case["same_org"], (
        f"\n  A: {case['a_org']}  <{case['a_url']}>"
        f"\n  B: {case['b_org']}  <{case['b_url']}>"
        f"\n  expected same_org={case['same_org']}, got {verdict}"
        f"\n  why: {case['why']}"
    )


# --- the 500-pair evaluation set -------------------------------------------


def test_labelled_set_is_balanced_and_large_enough() -> None:
    data = load("dedupe_labelled.json")
    counts = data["counts"]
    assert sum(counts.values()) >= 500
    assert min(counts.values()) >= 200, "neither class may dominate"


def test_dedupe_precision_and_recall_on_the_labelled_set() -> None:
    """The acceptance bar for E1.S4: >=0.98 precision, >=0.95 recall.

    Precision matters more than recall here. A false merge destroys a real
    opportunity and is invisible; a missed merge leaves a duplicate, which is
    annoying and obvious.
    """
    pairs = load("dedupe_labelled.json")["pairs"]

    tp = fp = tn = fn = 0
    misses: list[dict] = []

    for p in pairs:
        a, b = p["a"], p["b"]
        try:
            predicted = org_identity(a["org"], a["url"]) == org_identity(b["org"], b["url"])
        except ValueError:
            predicted = False
        if not predicted:
            predicted = same_organization(a["org"], a["url"], b["org"], b["url"])

        if p["label"] and predicted:
            tp += 1
        elif p["label"] and not predicted:
            fn += 1
            misses.append(p)
        elif not p["label"] and predicted:
            fp += 1
            misses.append(p)
        else:
            tn += 1

    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0

    report = (
        f"\n  tp={tp} fp={fp} tn={tn} fn={fn}"
        f"\n  precision={precision:.4f}  recall={recall:.4f}"
        + "".join(
            f"\n  MISS [{m['klass']}] {m['a']['org'][:44]} | {m['b']['org'][:44]}"
            for m in misses[:8]
        )
    )
    assert precision >= 0.98, f"precision below bar{report}"
    assert recall >= 0.95, f"recall below bar{report}"


def test_platform_hosts_list_is_not_empty() -> None:
    """Guard: emptying it silently merges every organization on a shared platform."""
    assert len(PLATFORM_HOSTS) > 20
    assert "glueup.com" in PLATFORM_HOSTS


# --- route-level separation ------------------------------------------------
# same_org and same_route are different questions. A named council belongs to
# its parent organization and separates at the route level. Conflating the two
# was the first mistake made while writing the hard-case fixture.


def _sub_body_routes() -> list[dict]:
    return load("dedupe_hard_cases.json")["sub_body_routes"]


@pytest.mark.parametrize("case", _sub_body_routes(), ids=lambda c: c["mech_a"][:34])
def test_sub_body_route_separation(case: dict) -> None:
    same = series_key(case["org_key"], case["mech_a"]) == series_key(
        case["org_key"], case["mech_b"]
    )
    assert same is case["same_route"], (
        f"\n  {case['org_key']}"
        f"\n    A: {case['mech_a']}"
        f"\n    B: {case['mech_b']}"
        f"\n  expected same_route={case['same_route']}, got {same}"
        f"\n  why: {case['why']}"
    )
