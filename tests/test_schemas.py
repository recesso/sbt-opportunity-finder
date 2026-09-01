"""E5.S1 — the extraction contract.

Two properties matter more than the rest, and most of this file exists to prove
them:

* **A stated value with no span cannot be constructed.** That is what makes a
  fabricated field impossible rather than merely discouraged.
* **Invalid output is never repaired.** It is rejected, retried once with the
  errors, and quarantined. There is no coercion path, and these tests would fail
  if one appeared.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from finder.extract.schemas import (
    FOUNDER_OWNED_FIELDS,
    NOT_STATED,
    PROMPT_CLAUSES,
    ROUTE_TYPES,
    SCHEMAS,
    TRIGGER_KINDS,
    Field,
    Quarantined,
    SchemaViolation,
    extract_with_retry,
    json_schema,
    validate,
)

ROOT = Path(__file__).resolve().parents[1]
URL = "https://gsae.org/speaker-interest-form"


def families_yaml() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "config" / "families.yaml").read_text(encoding="utf-8"))


def ddl() -> str:
    return (ROOT / "src" / "finder" / "store" / "migrations" / "001_initial.sql").read_text(
        encoding="utf-8"
    )


def stated(value: Any, span: str = "quoted from the page") -> dict[str, Any]:
    return {"value": value, "span": span, "source_url": URL}


def blank() -> dict[str, Any]:
    return {"value": NOT_STATED, "span": None, "source_url": URL}


def common(family: str, route_type: str) -> dict[str, Any]:
    """A minimal record: everything not_stated except what identifies the route."""
    return {
        "family": family,
        "target_name": stated("Georgia Society of Association Executives"),
        "route_type": stated(route_type),
        "mechanism_name": stated("Speaker interest form"),
        "route_url": stated("https://www.surveymonkey.com/r/NKSQCY6"),
        "owner": blank(),
        "subject_signals": blank(),
        "sector": blank(),
        "geography": blank(),
        "eligibility": blank(),
        "evidence_url": URL,
        "fetched_at": "2026-09-01T12:00:00+00:00",
        "content_hash": "sha256:abc123",
    }


def room(**overrides: Any) -> dict[str, Any]:
    payload = common("ROOM", "EVERGREEN_SUBMISSION") | {
        "deadline": blank(),
        "next_occurrence": blank(),
        "formats_accepted": blank(),
        "session_length": blank(),
        "cost": blank(),
        "precedent": blank(),
        "audience": {
            "stated_roles": blank(),
            "member_unit": blank(),
            "named_employers": blank(),
            "expected_size": blank(),
        },
    }
    return payload | overrides


def channel(**overrides: Any) -> dict[str, Any]:
    payload = common("CHANNEL", "PROVIDER_NETWORK") | {
        "employer_relationship": {
            "nature": blank(),
            "count": blank(),
            "named_employers": blank(),
        },
        "delivery_model": blank(),
        "intake": {
            "url": blank(),
            "criteria": blank(),
            "approver": blank(),
            "scope_contracted": blank(),
        },
        "replication": {"network_id": blank(), "peer_node_count": blank()},
        "existing_providers": blank(),
    }
    return payload | overrides


def trigger(**overrides: Any) -> dict[str, Any]:
    return {
        "kind": "contract_award",
        "what": "won a $40M Navy sustainment contract",
        "occurred_on": "2026-08-14",
        "source_url": "https://example.com/press",
        "span": "awarded a $40 million contract",
        "capability_implication": blank(),
    } | overrides


def employer(**overrides: Any) -> dict[str, Any]:
    payload = common("EMPLOYER", "NAMED_TARGET") | {
        "company": stated("Austal USA"),
        "triggers": [trigger()],
        "problem_owner": blank(),
        "reachable_via": {"channel_route_id": blank(), "person_id": blank()},
    }
    return payload | overrides


def person(**overrides: Any) -> dict[str, Any]:
    payload = common("PERSON", "PUBLIC_OWNER") | {
        "person": stated("Zack Huhn"),
        "controls": blank(),
        "connector": blank(),
        "role_change": {
            "current_role": blank(),
            "previous_role": blank(),
            "effective_date": blank(),
        },
    }
    return payload | overrides


BUILDERS = {"ROOM": room, "CHANNEL": channel, "EMPLOYER": employer, "PERSON": person}


# --- the span rule ---------------------------------------------------------


def test_a_stated_value_requires_its_span() -> None:
    """The rule that makes fabrication impossible instead of discouraged."""
    with pytest.raises(ValueError, match="requires the verbatim span"):
        Field[str](value="Chief Operating Officers", span=None, source_url=URL)


def test_a_whitespace_span_is_no_span():
    with pytest.raises(ValueError, match="requires the verbatim span"):
        Field[str](value="Chief Operating Officers", span="   \n ", source_url=URL)


def test_not_stated_carries_no_span() -> None:
    """A quote attached to 'not stated' is a contradiction, and contradictions
    in an evidence trail are worse than gaps."""
    with pytest.raises(ValueError, match="carries no span"):
        Field[str](value=NOT_STATED, span="something", source_url=URL)


def test_not_stated_is_a_first_class_answer() -> None:
    field = Field[str](value=NOT_STATED, source_url=URL)
    assert field.stated is False
    assert field.or_none() is None


def test_a_stated_value_reads_back_whole() -> None:
    field = Field[str](value="Operations directors", span="operations directors", source_url=URL)
    assert field.stated is True
    assert field.or_none() == "Operations directors"


def test_a_fully_not_stated_record_is_valid() -> None:
    """A page that says almost nothing must produce a record that says almost
    nothing, not a record padded with guesses."""
    record = validate("ROOM", room())
    assert record.deadline.stated is False
    assert record.audience.member_unit.or_none() is None


# --- the schema is a hard constraint ---------------------------------------


@pytest.mark.parametrize("family", sorted(SCHEMAS))
def test_route_types_match_the_families_config(family: str) -> None:
    """Drift between the schema literals and families.yaml would silently let
    the model answer with a route type that has no score."""
    configured = tuple(families_yaml()["families"][family]["route_types"])
    assert ROUTE_TYPES[family] == configured


def test_trigger_kinds_match_the_families_config() -> None:
    assert tuple(families_yaml()["triggers"]) == TRIGGER_KINDS


def test_the_four_families_are_the_configured_families() -> None:
    assert set(SCHEMAS) == set(families_yaml()["families"])


@pytest.mark.parametrize("family", sorted(SCHEMAS))
def test_the_generated_schema_enumerates_that_familys_route_types(family: str) -> None:
    """The constraint has to be in the schema the model receives, not only in a
    validator that runs after it has already answered."""
    schema = json_schema(family)
    text = str(schema)
    for route_type in ROUTE_TYPES[family]:
        assert f"'{route_type}'" in text, f"{route_type} missing from the {family} schema"

    other = "PROVIDER_NETWORK" if family != "CHANNEL" else "OPEN_CALL"
    assert f"'{other}'" not in text, f"{family} schema admits a foreign route type"


def test_a_route_type_from_another_family_is_rejected() -> None:
    with pytest.raises(SchemaViolation) as exc:
        validate("ROOM", room(route_type=stated("PROVIDER_NETWORK")))
    assert any("route_type" in e for e in exc.value.errors)


@pytest.mark.parametrize("family", sorted(SCHEMAS))
def test_unknown_keys_are_refused(family: str) -> None:
    """An invented field is an invented claim."""
    payload = BUILDERS[family]() | {"vibe": stated("good")}
    with pytest.raises(SchemaViolation) as exc:
        validate(family, payload)
    assert any("forbids inventing keys" in e for e in exc.value.errors)


@pytest.mark.parametrize("family", sorted(SCHEMAS))
def test_every_object_in_the_schema_forbids_extra_properties(family: str) -> None:
    schema = json_schema(family)
    objects = [schema, *schema.get("$defs", {}).values()]
    permissive = [
        o.get("title", "?")
        for o in objects
        if o.get("type") == "object" and o.get("additionalProperties") is not False
    ]
    assert not permissive, f"{family} schema allows extra keys in: {permissive}"


def test_an_unknown_family_is_an_error() -> None:
    with pytest.raises(KeyError, match="unknown family"):
        json_schema("SOCIETY")
    with pytest.raises(KeyError, match="unknown family"):
        validate("SOCIETY", {})


# --- founder-owned inputs --------------------------------------------------


def property_names(schema: dict[str, Any]) -> set[str]:
    """Every property name at every depth of a generated JSON Schema."""
    names: set[str] = set()
    stack: list[Any] = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            names |= set(node.get("properties", {}))
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return names


@pytest.mark.parametrize("family", sorted(SCHEMAS))
def test_no_schema_asks_the_model_for_a_founder_owned_field(family: str) -> None:
    """`known_to_art` is the founder's answer, and the reliable way to stop a
    model guessing it is to give it nowhere to put the guess.

    Checks property names at every depth, not the rendered text — prose in a
    docstring mentioning the field is fine; a slot to put an answer in is not.
    """
    exposed = property_names(json_schema(family)) & FOUNDER_OWNED_FIELDS
    assert not exposed, f"{family} schema exposes founder-owned field(s) {sorted(exposed)}"


def test_the_founder_owned_check_can_actually_fail() -> None:
    """A guard that cannot fail is decoration."""
    fake = {"properties": {"person": {"properties": {"known_to_art": {"type": "string"}}}}}
    assert property_names(fake) & FOUNDER_OWNED_FIELDS == {"known_to_art"}


def test_a_payload_carrying_known_to_art_is_rejected_not_ignored() -> None:
    with pytest.raises(SchemaViolation):
        validate("PERSON", person(known_to_art=stated("yes")))


# --- dates -----------------------------------------------------------------


def test_a_non_iso_date_is_a_violation() -> None:
    """A deadline the system cannot compare to today is not a deadline."""
    with pytest.raises(SchemaViolation) as exc:
        validate("ROOM", room(deadline=stated("October 3, 2026")))
    assert any("deadline" in e for e in exc.value.errors)


def test_a_date_that_is_not_a_real_day_is_a_violation() -> None:
    """2026-02-31 passes the pattern and fails the calendar. It looks usable,
    which is worse than missing."""
    with pytest.raises(SchemaViolation, match="impossible date"):
        from finder.extract.schemas import validate_dates

        validate_dates(validate("ROOM", room(deadline=stated("2026-02-31"))))


def test_a_real_date_survives() -> None:
    from finder.extract.schemas import validate_dates

    record = validate("ROOM", room(deadline=stated("2026-10-03")))
    validate_dates(record)
    assert record.deadline.or_none() == "2026-10-03"


def test_an_impossible_trigger_date_is_caught() -> None:
    from finder.extract.schemas import validate_dates

    record = validate("EMPLOYER", employer(triggers=[trigger(occurred_on="2026-13-01")]))
    with pytest.raises(SchemaViolation, match="impossible date"):
        validate_dates(record)


def test_an_impossible_role_change_date_is_caught() -> None:
    """A role change is a trigger for the PERSON family; a date that is not a
    real day makes its recency unusable."""
    from finder.extract.schemas import validate_dates

    payload = person()
    payload["role_change"] = payload["role_change"] | {
        "effective_date": stated("2026-04-31", "effective April 31")
    }
    with pytest.raises(SchemaViolation, match="impossible date"):
        validate_dates(validate("PERSON", payload))


def test_a_valid_role_change_date_passes() -> None:
    from finder.extract.schemas import validate_dates

    payload = person()
    payload["role_change"] = payload["role_change"] | {
        "effective_date": stated("2026-04-30", "effective April 30")
    }
    record = validate("PERSON", payload)
    validate_dates(record)
    assert record.role_change.effective_date.or_none() == "2026-04-30"


# --- family rules ----------------------------------------------------------


def test_an_employer_route_without_a_trigger_is_refused() -> None:
    """With nothing that changed there is no reason to call. The family's whole
    premise is recency."""
    with pytest.raises(SchemaViolation) as exc:
        validate("EMPLOYER", employer(triggers=[]))
    assert any("at least one trigger" in e for e in exc.value.errors)


@pytest.mark.parametrize("missing", ["source_url", "span"])
def test_a_trigger_without_provenance_is_refused(missing: str) -> None:
    """A trigger nobody can point at is a rumour."""
    bad = trigger()
    del bad[missing]
    with pytest.raises(SchemaViolation) as exc:
        validate("EMPLOYER", employer(triggers=[bad]))
    assert any(missing in e for e in exc.value.errors)


def test_an_unknown_trigger_kind_is_refused() -> None:
    with pytest.raises(SchemaViolation):
        validate("EMPLOYER", employer(triggers=[trigger(kind="vibe_shift")]))


def test_a_channel_with_no_published_intake_is_valid() -> None:
    """The GaMEP case: a real channel with a null route_url. It must survive
    extraction and reach WORTH A LOOK, not be filtered out here."""
    record = validate("CHANNEL", channel(route_url=blank(), route_type=stated("UNKNOWN")))
    assert record.route_url.or_none() is None
    assert record.route_type.or_none() == "UNKNOWN"


def test_route_url_and_evidence_url_stay_separate() -> None:
    """The GSAE case: the form is on surveymonkey.com, the proof is on gsae.org."""
    record = validate("ROOM", room())
    assert record.route_url.or_none() == "https://www.surveymonkey.com/r/NKSQCY6"
    assert record.evidence_url == URL
    assert record.route_url.or_none() != record.evidence_url


# --- JSON handling ---------------------------------------------------------


def test_a_json_string_response_is_parsed() -> None:
    import json

    record = validate("ROOM", json.dumps(room()))
    assert record.family == "ROOM"


def test_a_non_json_response_is_a_violation_not_a_crash() -> None:
    with pytest.raises(SchemaViolation, match="not JSON"):
        validate("ROOM", "I could not find a speaker form on this page.")


# --- retry and quarantine --------------------------------------------------


def test_a_malformed_response_is_retried_with_the_errors() -> None:
    attempts: list[str | None] = []

    def call(feedback: str | None) -> Any:
        attempts.append(feedback)
        return room(deadline=stated("October 3")) if feedback is None else room()

    result = extract_with_retry("ROOM", call)

    assert not isinstance(result, Quarantined)
    assert attempts[0] is None
    assert "rejected" in attempts[1] and "deadline" in attempts[1], (
        "the retry must carry the specific violation, not a generic nudge"
    )


def test_two_failures_quarantine_rather_than_repair() -> None:
    """The acceptance criterion: schema-invalid output is never coerced."""
    calls = 0

    def call(feedback: str | None) -> Any:
        nonlocal calls
        calls += 1
        return room(deadline=stated("whenever"))

    result = extract_with_retry("ROOM", call)

    assert isinstance(result, Quarantined)
    assert calls == 2
    assert result.attempts == 2
    assert any("deadline" in e for e in result.errors)
    assert result.raw["deadline"]["value"] == "whenever", "the raw payload is kept for diagnosis"
    assert "quarantined after 2 attempts" in result.summary()


def test_a_quarantined_result_is_not_a_record() -> None:
    """Nothing downstream can mistake a failure for an extraction."""
    result = extract_with_retry("ROOM", lambda _: {"family": "ROOM"})
    assert isinstance(result, Quarantined)
    assert not hasattr(result, "target_name")


def test_a_first_attempt_that_validates_costs_one_call() -> None:
    calls = 0

    def call(feedback: str | None) -> Any:
        nonlocal calls
        calls += 1
        return room()

    assert not isinstance(extract_with_retry("ROOM", call), Quarantined)
    assert calls == 1


def test_the_impossible_date_check_runs_inside_the_retry_loop() -> None:
    """A calendar-impossible date must trigger the retry like any other
    violation, not slip through because it passed the regex."""
    seen: list[str | None] = []

    def call(feedback: str | None) -> Any:
        seen.append(feedback)
        return room(deadline=stated("2026-02-31")) if feedback is None else room()

    result = extract_with_retry("ROOM", call)
    assert not isinstance(result, Quarantined)
    assert "2026-02-31" in seen[1]


def test_more_attempts_can_be_requested() -> None:
    calls = 0

    def call(feedback: str | None) -> Any:
        nonlocal calls
        calls += 1
        return room() if calls == 3 else room(deadline=stated("soon"))

    assert not isinstance(extract_with_retry("ROOM", call, max_attempts=3), Quarantined)
    assert calls == 3


def test_zero_attempts_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        extract_with_retry("ROOM", lambda _: room(), max_attempts=0)


# --- the prompt clauses ----------------------------------------------------


def test_the_non_negotiable_prompt_clauses_are_present() -> None:
    """Each clause is a failure the predecessor actually made. Dropping one is
    how it comes back."""
    joined = " ".join(PROMPT_CLAUSES).lower()
    for required in ("not_stated", "never infer", "past cycle", "verbatim span", "today's date"):
        assert required in joined, f"prompt clause about {required!r} is missing"
    assert len(PROMPT_CLAUSES) == len(set(PROMPT_CLAUSES))


# --- the enums match the database ------------------------------------------


@pytest.mark.parametrize(
    ("column", "alias"),
    [
        ("member_unit", "MemberUnit"),
        ("relationship_nature", "RelationshipNature"),
        ("delivery_model", "DeliveryModel"),
    ],
)
def test_extraction_enums_match_the_schema_check_constraints(column: str, alias: str) -> None:
    """A value the extractor can produce but the column refuses is a write that
    fails at 2am on live data."""
    from finder.extract import schemas

    match = re.search(rf"{column}\s+TEXT\s+CHECK\s*\({column}\s+IN\s*\(([^)]*)\)", ddl(), re.S)
    assert match, f"could not find the {column} CHECK constraint in the DDL"
    allowed = set(re.findall(r"'([^']+)'", match.group(1)))

    from typing import get_args

    assert set(get_args(getattr(schemas, alias))) == allowed
