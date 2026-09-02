"""E5.S2 — W3 MechanismExtractor.

The highest-risk story in the plan: every downstream number inherits this step's
quality and no plumbing fixes a bad extractor.

What is tested here is everything the extractor CONTROLS, offline and
deterministically: that spans are checked against the stored page, that a stated
value with no support is dropped rather than kept, that a thin page yields a
thin record, that the prompt says what it must, and that provenance the harness
knows is never left to the model.

What is NOT tested here is how well a real model reads a real page. That is a
live measurement against recorded fixtures — `scripts/eval_extraction.py`, run
deliberately. Pretending a scripted fake answers that question would be exactly
the testing theatre this project forbids, so the split is explicit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from finder.acquire.providers.base import FetchError, Snapshot
from finder.acquire.providers.llm import Completion, LLMProvider
from finder.acquire.snapshot import content_hash
from finder.context import start_run
from finder.extract.schemas import NOT_STATED, PROMPT_CLAUSES
from finder.extract.w3_mechanism import (
    MAX_SNAPSHOT_CHARS,
    PROMPT_VERSION,
    MechanismExtractor,
    build_system_prompt,
    build_user_prompt,
    span_match,
    strip_unsupported,
)
from finder.store.db import open_db, utcnow
from finder.store.repos import Store

ROOT = Path(__file__).resolve().parents[1]
LABELLED = ROOT / "tests" / "fixtures" / "extraction_labelled" / "pages.json"
TODAY = "2026-09-02"


def labelled_pages() -> list[dict[str, Any]]:
    return json.loads(LABELLED.read_text(encoding="utf-8"))["pages"]


def page(page_id: str) -> dict[str, Any]:
    return next(p for p in labelled_pages() if p["id"] == page_id)


def snapshot_of(spec: dict[str, Any]) -> Snapshot:
    return Snapshot(
        content_hash=content_hash(spec["markdown"]),
        url=spec["url"],
        canonical_url=spec["url"],
        markdown=spec["markdown"],
        fetched_at=utcnow(),
        provider="fixture",
    )


class ScriptedLLM:
    """Returns answers a test wrote, and records what it was asked.

    A fake, not a mock of a model: it cannot tell us how well a model reads. It
    lets us drive the extractor's own logic — including the failure modes a real
    model would produce occasionally and a test needs to produce on demand.
    """

    name = "scripted"
    cost_per_call_usd = 0.0

    def __init__(self, *answers: Any, stop_reason: str = "tool_use") -> None:
        self.answers = list(answers)
        self.stop_reason = stop_reason
        self.calls: list[dict[str, Any]] = []

    def complete(self, *, system, prompt, schema=None, max_tokens=8000) -> Completion:
        self.calls.append(
            {"system": system, "prompt": prompt, "schema": schema, "max_tokens": max_tokens}
        )
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        text = answer if isinstance(answer, str) else json.dumps(answer)
        return Completion(
            text=text,
            model="scripted-1",
            provider=self.name,
            output_tokens=100,
            stop_reason=self.stop_reason,
        )


def field(value: Any, span: str, url: str = "https://x/") -> dict[str, Any]:
    return {"value": value, "span": span, "source_url": url}


def blank(url: str = "https://x/") -> dict[str, Any]:
    return {"value": NOT_STATED, "span": None, "source_url": url}


def room_answer(markdown: str, url: str, **overrides: Any) -> dict[str, Any]:
    """A ROOM record whose spans are genuinely quoted from `markdown`."""
    first_line = markdown.strip().splitlines()[0].lstrip("# ").strip()
    base = {
        "family": "ROOM",
        "target_name": field(first_line, first_line, url),
        "route_type": field("EVERGREEN_SUBMISSION", first_line, url),
        "mechanism_name": field(first_line, first_line, url),
        "route_url": blank(url),
        "owner": blank(url),
        "subject_signals": blank(url),
        "sector": blank(url),
        "geography": blank(url),
        "eligibility": blank(url),
        "deadline": blank(url),
        "next_occurrence": blank(url),
        "formats_accepted": blank(url),
        "session_length": blank(url),
        "cost": blank(url),
        "precedent": blank(url),
        "audience": {
            "stated_roles": blank(url),
            "member_unit": blank(url),
            "named_employers": blank(url),
            "expected_size": blank(url),
        },
        "evidence_url": url,
        "fetched_at": "2026-09-02T00:00:00+00:00",
        "content_hash": "sha256:x",
    }
    return base | overrides


@pytest.fixture
def store() -> Store:
    return Store(open_db(":memory:"))


def extractor(llm: LLMProvider, **kw) -> MechanismExtractor:
    return MechanismExtractor(llm, today=TODAY, **kw)


# --- the labelled set ------------------------------------------------------


def test_the_labelled_set_has_ten_pages_covering_the_known_failure_modes() -> None:
    pages = labelled_pages()
    ids = {p["id"] for p in pages}
    assert len(pages) == 10
    for required in (
        "offdomain_form",
        "past_event",
        "service_channel",
        "thin_page",
        "looks_relevant_no_route",
    ):
        assert required in ids, f"the {required} shape is one that actually broke things"
    assert {p["family"] for p in pages} >= {"ROOM", "CHANNEL", "EMPLOYER"}


def test_every_labelled_page_carries_its_expected_answer() -> None:
    for spec in labelled_pages():
        assert spec["markdown"].strip(), spec["id"]
        assert spec["expect"], spec["id"]
        assert spec["note"], "each page says which failure mode it reproduces"


# --- the prompt ------------------------------------------------------------


def test_the_prompt_carries_every_non_negotiable_clause() -> None:
    """Each clause is a failure the predecessor made. Dropping one is how it
    comes back."""
    system = build_system_prompt()
    for clause in PROMPT_CLAUSES:
        assert clause in system


def test_the_prompt_says_there_is_no_browsing() -> None:
    """The model reads stored text and nothing else. Saying so is what makes an
    invented span a detectable error rather than an expected one."""
    system = build_system_prompt().lower()
    assert "no browsing capability" in system
    assert "stored" in system


def test_the_prompt_supplies_todays_date_and_the_page() -> None:
    spec = page("open_call")
    prompt = build_user_prompt("ROOM", snapshot_of(spec), today=TODAY)
    assert TODAY in prompt
    assert "Compare every deadline" in prompt
    assert spec["markdown"].strip() in prompt
    assert spec["url"] in prompt


def test_the_prompt_tells_the_channel_family_that_no_intake_is_normal() -> None:
    """The GaMEP case. A model that invents a route_url here destroys the
    family Art named as the goal."""
    prompt = build_user_prompt("CHANNEL", snapshot_of(page("service_channel")), today=TODAY)
    assert "no published intake is normal" in prompt
    assert "rather than inventing one" in prompt


def test_the_prompt_tells_the_room_family_the_form_may_be_off_domain() -> None:
    """The GSAE case, which could not be found by hand precisely because the
    form is not on the organization's own domain."""
    prompt = build_user_prompt("ROOM", snapshot_of(page("offdomain_form")), today=TODAY)
    assert "DIFFERENT domain" in prompt


def test_an_unknown_family_is_refused() -> None:
    with pytest.raises(KeyError, match="unknown family"):
        build_user_prompt("SOCIETY", snapshot_of(page("thin_page")), today=TODAY)


def test_a_very_long_page_is_truncated_not_refused() -> None:
    spec = dict(page("thin_page"))
    spec["markdown"] = "x" * (MAX_SNAPSHOT_CHARS + 5_000)
    prompt = build_user_prompt("ROOM", snapshot_of(spec), today=TODAY)
    assert len(prompt) < MAX_SNAPSHOT_CHARS + 5_000


def test_the_prompt_version_is_stamped_on_every_result() -> None:
    """A regression six weeks from now has to be attributable to a prompt.

    Asserting only `result.prompt_version == PROMPT_VERSION` would be a
    tautology — it compares the value to the constant it came from and passes
    happily when that constant is blank. So the version itself is checked.
    """
    assert PROMPT_VERSION.strip(), "an unversioned prompt is attributable to nothing"
    assert PROMPT_VERSION.startswith("w3-"), "the version names the worker it belongs to"
    assert len(PROMPT_VERSION) >= 8, "and carries a date, so two prompts can be ordered"

    spec = page("open_call")
    llm = ScriptedLLM(room_answer(spec["markdown"], spec["url"]))
    result = extractor(llm).extract("ROOM", snapshot_of(spec))
    assert result.prompt_version == PROMPT_VERSION
    assert result.as_dict()["prompt_version"] == PROMPT_VERSION
    assert result.model == "scripted-1"


# --- span checking: the rule the contract exists for -----------------------


def test_a_span_quoted_exactly_is_exact() -> None:
    assert span_match("accepting proposals", "AI Week is accepting proposals now") == "exact"


def test_a_reflowed_span_is_still_a_real_quote() -> None:
    """Providers re-wrap markdown. Treating that as fabrication would throw away
    true fields over a formatting difference."""
    assert span_match("accepting   PROPOSALS", "is accepting proposals now") == "normalized"


@pytest.mark.parametrize("span", ["deadline is March 1", "", "   ", None])
def test_a_span_not_in_the_page_is_absent(span: str | None) -> None:
    """Not 'weakly supported'. Fabricated."""
    assert span_match(span, "AI Week is accepting proposals now") == "absent"


def test_a_fabricated_field_is_dropped_and_the_rest_kept(store: Store) -> None:
    """A page that yielded nine true fields and one invented one is worth nine.
    Throwing the record away would lose real evidence to punish one slip. What
    is not acceptable is keeping the tenth."""
    spec = page("open_call")
    answer = room_answer(
        spec["markdown"],
        spec["url"],
        owner=field("Zack Huhn", "Contact Zack Huhn, Program Director", spec["url"]),
        deadline=field("2026-12-01", "proposals are due December 1", spec["url"]),
    )
    result = extractor(ScriptedLLM(answer)).extract("ROOM", snapshot_of(spec))

    assert result.ok
    assert result.dropped == ["deadline"], "the invented deadline, and only it"
    assert result.record.deadline.value == NOT_STATED
    assert result.record.deadline.span is None
    assert result.record.owner.value == "Zack Huhn", "the supported field survives"
    assert result.span_matches["owner"] == "exact"
    assert result.fabricated == 1


def test_a_nested_fabricated_field_is_dropped_too(store: Store) -> None:
    """audience.member_unit is nested. A checker that only walked the top level
    would leave the most decision-bearing fields unguarded."""
    spec = page("thin_page")
    answer = room_answer(spec["markdown"], spec["url"])
    answer["audience"] = dict(answer["audience"])
    answer["audience"]["member_unit"] = field("company", "member companies attend", spec["url"])

    result = extractor(ScriptedLLM(answer)).extract("ROOM", snapshot_of(spec))

    assert result.dropped == ["audience.member_unit"]
    assert result.record.audience.member_unit.value == NOT_STATED


def test_a_clean_extraction_drops_nothing(store: Store) -> None:
    spec = page("open_call")
    result = extractor(ScriptedLLM(room_answer(spec["markdown"], spec["url"]))).extract(
        "ROOM", snapshot_of(spec)
    )
    assert result.dropped == []
    assert result.fabricated == 0


def test_strip_unsupported_leaves_a_clean_record_untouched() -> None:
    from finder.extract.schemas import validate

    spec = page("open_call")
    record = validate("ROOM", room_answer(spec["markdown"], spec["url"]))
    stripped, dropped, _ = strip_unsupported(record, spec["markdown"])
    assert stripped is record, "no rebuild when there is nothing to blank"
    assert dropped == []


# --- the thin page: the named acceptance criterion -------------------------


def test_the_thin_page_yields_not_stated_rather_than_invention(store: Store) -> None:
    """Named in the acceptance criterion. A page that says almost nothing must
    produce a record that says almost nothing."""
    spec = page("thin_page")
    invented = room_answer(
        spec["markdown"],
        spec["url"],
        deadline=field("2026-10-15", "proposals close October 15", spec["url"]),
        owner=field("Dana Whitfield", "contact Dana Whitfield", spec["url"]),
        eligibility=field("open to members", "open to all members", spec["url"]),
    )

    result = extractor(ScriptedLLM(invented)).extract("ROOM", snapshot_of(spec))

    assert sorted(result.dropped) == ["deadline", "eligibility", "owner"]
    assert result.record.deadline.value == NOT_STATED
    assert result.record.owner.value == NOT_STATED
    assert result.record.eligibility.value == NOT_STATED

    # Against the labels: every field the page does not state must come back
    # not_stated, checked field by field rather than asserted in the abstract.
    for name, expected in spec["expect"].items():
        if expected != "not_stated" or name.startswith("_"):
            continue
        value = getattr(result.record, name, None)
        if value is None:
            value = getattr(result.record.audience, name, None)
        assert value is not None, f"{name} is not a field on the record"
        assert value.value == NOT_STATED, f"{name} was invented"


# --- provenance ------------------------------------------------------------


def test_the_harness_supplies_provenance_the_model_must_not_guess(store: Store) -> None:
    """evidence_url, fetched_at and content_hash are facts the harness knows.
    Letting a model state them invites it to state them wrongly."""
    spec = page("open_call")
    answer = room_answer(spec["markdown"], spec["url"])
    answer["evidence_url"] = "https://wrong.example/"
    answer["content_hash"] = "sha256:wrong"

    snapshot = snapshot_of(spec)
    result = extractor(ScriptedLLM(answer)).extract("ROOM", snapshot)

    assert result.record.evidence_url == snapshot.url
    assert result.record.content_hash == snapshot.content_hash
    assert result.content_hash == snapshot.content_hash


def test_the_schema_reaches_the_model_as_a_constraint(store: Store) -> None:
    spec = page("open_call")
    llm = ScriptedLLM(room_answer(spec["markdown"], spec["url"]))
    extractor(llm).extract("ROOM", snapshot_of(spec))

    schema = llm.calls[0]["schema"]
    assert schema is not None
    assert "route_type" in schema["properties"]


# --- retry and quarantine --------------------------------------------------


def test_a_malformed_answer_is_retried_with_the_violation(store: Store) -> None:
    spec = page("open_call")
    good = room_answer(spec["markdown"], spec["url"])
    bad = dict(good)
    bad["deadline"] = field("October 15", "reviewed monthly", spec["url"])

    llm = ScriptedLLM(bad, good)
    result = extractor(llm).extract("ROOM", snapshot_of(spec))

    assert result.ok
    assert result.attempts == 2
    assert "rejected" in llm.calls[1]["prompt"]
    assert "deadline" in llm.calls[1]["prompt"], "the retry names the actual violation"


def test_two_bad_answers_quarantine_rather_than_coerce(store: Store) -> None:
    spec = page("open_call")
    bad = room_answer(spec["markdown"], spec["url"])
    bad["deadline"] = field("whenever", "reviewed monthly", spec["url"])

    with start_run(store, "weekly", run_id="r-1") as run:
        result = extractor(ScriptedLLM(bad)).extract("ROOM", snapshot_of(spec), run=run)

    assert not result.ok
    assert result.quarantined is not None
    assert result.record is None, "nothing downstream can mistake this for an extraction"
    assert store.runs.get("r-1").counters["quarantined"] == 1
    assert store.runs.get("r-1").not_reached[0]["reason"] == "extraction_quarantined"


def test_a_truncated_answer_fails_loudly_rather_than_validating(store: Store) -> None:
    """Half a JSON object fails validation for a reason that has nothing to do
    with the page. Reported as a schema violation it would look like a bad page
    and get quarantined; reported as truncation it says raise the token budget."""
    spec = page("open_call")
    llm = ScriptedLLM(room_answer(spec["markdown"], spec["url"]), stop_reason="max_tokens")

    with pytest.raises(FetchError) as exc:
        extractor(llm).extract("ROOM", snapshot_of(spec))

    assert "cut off" in str(exc.value)
    assert "not the page" in str(exc.value), "the message says where to look"


def test_prose_instead_of_a_record_is_a_violation_not_a_crash(store: Store) -> None:
    result = extractor(ScriptedLLM("I could not find a speaker form.")).extract(
        "ROOM", snapshot_of(page("thin_page"))
    )
    assert result.quarantined is not None
    assert any("JSON" in e for e in result.quarantined.errors)


# --- cost and reporting ----------------------------------------------------


def test_extraction_is_charged_to_the_run(store: Store) -> None:
    spec = page("open_call")
    with start_run(store, "weekly", run_id="r-1") as run:
        extractor(ScriptedLLM(room_answer(spec["markdown"], spec["url"]))).extract(
            "ROOM", snapshot_of(spec), run=run
        )
    assert "scripted" in store.costs.by_provider("r-1")


def test_the_result_summarises_itself(store: Store) -> None:
    spec = page("open_call")
    summary = (
        extractor(ScriptedLLM(room_answer(spec["markdown"], spec["url"])))
        .extract("ROOM", snapshot_of(spec))
        .as_dict()
    )
    assert summary["ok"] is True
    assert summary["prompt_version"] == PROMPT_VERSION
    assert summary["url"] == spec["url"]


def test_dropped_fields_are_logged_with_the_page_that_lost_them(store: Store) -> None:
    """A silent drop is a field that vanishes between the model and the record.
    The log line is how anyone notices the extractor is inventing."""
    from structlog.testing import capture_logs

    spec = page("thin_page")
    invented = room_answer(
        spec["markdown"], spec["url"], owner=field("Dana", "contact Dana", spec["url"])
    )

    with capture_logs() as logs, start_run(store, "weekly", run_id="r-1") as run:
        extractor(ScriptedLLM(invented)).extract("ROOM", snapshot_of(spec), run=run)

    warning = next(e for e in logs if e["event"] == "spans_not_found")
    assert warning["fields"] == ["owner"]
    assert warning["url"] == spec["url"]


def test_a_non_dict_json_answer_reaches_the_schema_layer(store: Store) -> None:
    """A JSON array is valid JSON and not a record. It must arrive at the schema
    as a violation rather than crashing while provenance is filled in."""
    result = extractor(ScriptedLLM("[1, 2, 3]")).extract("ROOM", snapshot_of(page("thin_page")))
    assert result.quarantined is not None


def test_extraction_without_a_run_works(store: Store) -> None:
    spec = page("open_call")
    assert (
        extractor(ScriptedLLM(room_answer(spec["markdown"], spec["url"])))
        .extract("ROOM", snapshot_of(spec))
        .ok
    )
