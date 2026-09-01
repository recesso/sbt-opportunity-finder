"""Extraction contract — schema-forced structured output.

Build spec section 5. Three rules govern everything here:

1. **Every decision-bearing field carries the verbatim span that supports it.**
   A stated value with no span is a validation error, not a warning. That single
   rule is what makes a fabricated field impossible to write rather than merely
   discouraged.
2. **``not_stated`` is a correct and preferred answer.** The schema says so
   structurally: every field's type is ``T | "not_stated"``. Nothing is coerced
   into existence to satisfy a shape.
3. **Schema-invalid output is rejected and retried, never repaired.** There is
   no coercion path in this module. A second failure quarantines.

Prose is composed from fields downstream; the model is never asked for a
paragraph. ``route_url`` (the page you act on) and ``evidence_url`` (the page
that proves the claim) are separate fields and are never conflated.

``known_to_art`` is deliberately absent from :class:`PersonExtraction`. It is
founder-owned, and a schema that asks a model for it is an invitation to guess.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError, model_validator

NOT_STATED = "not_stated"
NotStated = Literal["not_stated"]

Family = Literal["ROOM", "CHANNEL", "EMPLOYER", "PERSON"]

# Route types per family. Mirrors config/families.yaml, which stays the source
# of truth for the *scores*; these literals exist so the JSON Schema handed to
# the model can constrain the answer. `test_schemas.py` fails if they drift.
ROUTE_TYPES: dict[str, tuple[str, ...]] = {
    "ROOM": (
        "OPEN_CALL",
        "EVERGREEN_SUBMISSION",
        "PROGRAM_COMMITTEE",
        "PARTNER_DELIVERY",
        "NAMED_OWNER",
        "COUNCIL_SEAT",
        "MEMBER_PROGRAMMING",
        "OPEN_REGISTRATION",
        "SPONSOR_CONTENT",
        "UNKNOWN",
    ),
    "CHANNEL": (
        "PROVIDER_NETWORK",
        "PARTNERSHIP_PROGRAM",
        "CO_APPLICANT",
        "CLIENT_PROGRAM",
        "BOARD_ADVISORY",
        "MEMBER_CHANNEL",
        "NAMED_OWNER",
        "UNKNOWN",
    ),
    "EMPLOYER": (
        "WARM_PATH",
        "CHANNEL_INTRO",
        "OPEN_SOLICITATION",
        "NAMED_TARGET",
        "UNKNOWN",
    ),
    "PERSON": (
        "EXISTING_RELATIONSHIP",
        "ONE_HOP",
        "ROLE_CHANGE",
        "PUBLIC_OWNER",
    ),
}

RoomRouteType = Literal[ROUTE_TYPES["ROOM"]]  # type: ignore[valid-type]
ChannelRouteType = Literal[ROUTE_TYPES["CHANNEL"]]  # type: ignore[valid-type]
EmployerRouteType = Literal[ROUTE_TYPES["EMPLOYER"]]  # type: ignore[valid-type]
PersonRouteType = Literal[ROUTE_TYPES["PERSON"]]  # type: ignore[valid-type]

TRIGGER_KINDS: tuple[str, ...] = (
    "contract_award",
    "grant_award",
    "automation_investment",
    "new_site",
    "expansion",
    "ai_initiative",
    "hiring_surge",
    "leadership_change",
    "merger",
    "open_solicitation",
)

MemberUnit = Literal["company", "individual", "mixed", "not_stated"]
RelationshipNature = Literal["members", "clients", "contracted", "funded", "convened", "not_stated"]
DeliveryModel = Literal["staff", "instructors", "partners", "providers", "mixed", "not_stated"]

# ISO dates only. "October 3" and "next fall" are schema violations on purpose:
# a deadline the system cannot compare to today is not a deadline.
IsoDate = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]
NonEmpty = Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]

# Clauses that must appear verbatim in every extraction prompt. Kept here rather
# than in the prompt template so a test can assert none was dropped — each one
# corresponds to an observed failure of the predecessor system.
PROMPT_CLAUSES: tuple[str, ...] = (
    "not_stated is a correct and preferred answer. Prefer it to a guess.",
    "Never infer the audience from a title, an organization name, or a sponsor list.",
    "Never treat a past cycle as a current open one.",
    "Return the verbatim span for every value. If you cannot quote it, answer not_stated.",
    "Separate what the page says from what you conclude. Only what it says may be written.",
    "Today's date is supplied. Compare every deadline to it.",
)


class SchemaViolation(Exception):
    """Model output that does not satisfy the contract. Never repaired."""

    def __init__(self, message: str, errors: list[str]) -> None:
        super().__init__(message)
        self.errors = errors


class Field[T](BaseModel):
    """One extracted value and the quoted text that supports it.

    The span is the whole point. A value with no span is unsupported, and the
    contract's answer to unsupported is ``not_stated`` — enforced here rather
    than left to a downstream check that someone forgets to run.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: T | NotStated
    span: str | None = None
    source_url: str

    @model_validator(mode="after")
    def _span_matches_the_claim(self) -> Field[T]:
        stated = self.value != NOT_STATED
        has_span = bool((self.span or "").strip())
        if stated and not has_span:
            raise ValueError(
                "a stated value requires the verbatim span supporting it; "
                "with no span the correct answer is not_stated"
            )
        if not stated and has_span:
            raise ValueError("not_stated carries no span; drop the span or state the value")
        return self

    @property
    def stated(self) -> bool:
        return self.value != NOT_STATED

    def or_none(self) -> T | None:
        """The value, or None when not stated. For writing to nullable columns."""
        return None if self.value == NOT_STATED else self.value  # type: ignore[return-value]


class _Strict(BaseModel):
    """Unknown keys are a violation: an invented field is an invented claim."""

    model_config = ConfigDict(frozen=True, extra="forbid")


# --- common ----------------------------------------------------------------


class CommonExtraction(_Strict):
    """Fields every family carries.

    ``evidence_url``, ``fetched_at`` and ``content_hash`` are provenance the
    harness already knows; they are plain values, not Fields, because there is
    nothing for the model to claim about them.
    """

    family: Family
    target_name: Field[NonEmpty]
    route_type: Field[Any]
    mechanism_name: Field[NonEmpty]
    route_url: Field[NonEmpty]
    owner: Field[NonEmpty]
    subject_signals: Field[list[str]]
    sector: Field[list[str]]
    geography: Field[NonEmpty]
    eligibility: Field[NonEmpty]

    evidence_url: NonEmpty
    fetched_at: NonEmpty
    content_hash: NonEmpty
    unresolved: list[str] = []


# --- ROOM ------------------------------------------------------------------


class Audience(_Strict):
    stated_roles: Field[list[str]]
    member_unit: Field[MemberUnit]
    named_employers: Field[list[str]]
    expected_size: Field[int]


class RoomExtraction(CommonExtraction):
    """A gathering with employers in it."""

    family: Literal["ROOM"]
    route_type: Field[RoomRouteType]
    deadline: Field[IsoDate]
    next_occurrence: Field[IsoDate]
    formats_accepted: Field[list[str]]
    session_length: Field[NonEmpty]
    cost: Field[NonEmpty]
    precedent: Field[NonEmpty]
    audience: Audience


# --- CHANNEL ---------------------------------------------------------------


class EmployerRelationship(_Strict):
    nature: Field[RelationshipNature]
    count: Field[int]
    named_employers: Field[list[str]]


class Intake(_Strict):
    url: Field[NonEmpty]
    criteria: Field[NonEmpty]
    approver: Field[NonEmpty]
    scope_contracted: Field[NonEmpty]


class Replication(_Strict):
    network_id: Field[NonEmpty]
    peer_node_count: Field[int]


class ChannelExtraction(CommonExtraction):
    """An organization relationship that reaches employers with no event required.

    ``route_url`` is routinely ``not_stated`` here and that is not a defect —
    a channel with no published intake goes to WORTH A LOOK with a question.
    """

    family: Literal["CHANNEL"]
    route_type: Field[ChannelRouteType]
    employer_relationship: EmployerRelationship
    delivery_model: Field[DeliveryModel]
    intake: Intake
    replication: Replication
    existing_providers: Field[list[str]]


# --- EMPLOYER --------------------------------------------------------------


class TriggerExtraction(_Strict):
    """Something that changed at a company, with the page that says so.

    Span and source_url are required, not optional: a trigger nobody can point
    at is a rumour, and the whole family is built on recency being checkable.
    """

    kind: Literal[TRIGGER_KINDS]  # type: ignore[valid-type]
    what: NonEmpty
    occurred_on: IsoDate
    source_url: NonEmpty
    span: NonEmpty
    capability_implication: Field[NonEmpty]


class ReachableVia(_Strict):
    channel_route_id: Field[NonEmpty]
    person_id: Field[NonEmpty]


class EmployerExtraction(CommonExtraction):
    """A company with a live trigger that creates a capability problem now."""

    family: Literal["EMPLOYER"]
    route_type: Field[EmployerRouteType]
    company: Field[NonEmpty]
    triggers: list[TriggerExtraction]
    problem_owner: Field[NonEmpty]
    reachable_via: ReachableVia

    @model_validator(mode="after")
    def _employer_routes_need_a_trigger(self) -> EmployerExtraction:
        if not self.triggers:
            raise ValueError(
                "an EMPLOYER route requires at least one trigger with a source_url; "
                "with no trigger there is nothing that changed and no reason to call"
            )
        return self


# --- PERSON ----------------------------------------------------------------


class RoleChange(_Strict):
    current_role: Field[NonEmpty]
    previous_role: Field[NonEmpty]
    effective_date: Field[IsoDate]


class PersonExtraction(CommonExtraction):
    """The individual is the path.

    ``known_to_art`` is absent by design. It is founder-owned, the one input the
    system must never author, and the reliable way to keep a model from guessing
    it is to give it nowhere to put the guess.
    """

    family: Literal["PERSON"]
    route_type: Field[PersonRouteType]
    person: Field[NonEmpty]
    controls: Field[NonEmpty]
    connector: Field[NonEmpty]
    role_change: RoleChange


SCHEMAS: dict[str, type[CommonExtraction]] = {
    "ROOM": RoomExtraction,
    "CHANNEL": ChannelExtraction,
    "EMPLOYER": EmployerExtraction,
    "PERSON": PersonExtraction,
}

# Founder-owned inputs. No extraction schema may contain one of these names at
# any depth; `test_schemas.py` walks the generated JSON Schema to prove it.
FOUNDER_OWNED_FIELDS: frozenset[str] = frozenset({"known_to_art", "relationship_strength"})


def json_schema(family: str) -> dict[str, Any]:
    """The JSON Schema handed to the model as a hard constraint, not a hint."""
    if family not in SCHEMAS:
        raise KeyError(f"unknown family {family!r}; expected one of {sorted(SCHEMAS)}")
    return SCHEMAS[family].model_json_schema()


def _flatten(exc: ValidationError) -> list[str]:
    """One line per problem, field first. Pydantic's default buries the useful part."""
    out = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        msg = err["msg"].removeprefix("Value error, ").strip()
        if err["type"] == "extra_forbidden":
            msg = "unexpected field — the schema forbids inventing keys"
        out.append(f"{loc}: {msg}")
    return out


def validate(family: str, payload: Any) -> CommonExtraction:
    """Parse model output against the family's schema.

    Raises :class:`SchemaViolation`. Never returns a partially valid record and
    never fills a missing field in to make one.
    """
    if family not in SCHEMAS:
        raise KeyError(f"unknown family {family!r}; expected one of {sorted(SCHEMAS)}")

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise SchemaViolation(
                f"response is not JSON: {exc}", [f"(root): not valid JSON — {exc}"]
            ) from exc

    try:
        return SCHEMAS[family].model_validate(payload)
    except ValidationError as exc:
        errors = _flatten(exc)
        raise SchemaViolation(
            f"{len(errors)} schema violation(s) in {family} extraction", errors
        ) from exc


def validate_dates(record: CommonExtraction) -> None:
    """Reject dates that match the ISO pattern but are not real days.

    ``2026-02-31`` passes a regex and fails a calendar. A deadline that cannot
    be compared to today is worse than a missing one, because it looks usable.
    """
    bad: list[str] = []
    for name, field in _iter_date_fields(record):
        if not field.stated:
            continue
        try:
            date.fromisoformat(str(field.value))
        except ValueError:
            bad.append(f"{name}: {field.value!r} is not a real calendar date")
    for i, trigger in enumerate(getattr(record, "triggers", [])):
        try:
            date.fromisoformat(trigger.occurred_on)
        except ValueError:
            bad.append(f"triggers.{i}.occurred_on: {trigger.occurred_on!r} is not a real date")
    if bad:
        raise SchemaViolation(f"{len(bad)} impossible date(s)", bad)


def _iter_date_fields(record: CommonExtraction) -> list[tuple[str, Field[Any]]]:
    found: list[tuple[str, Field[Any]]] = []
    for name in ("deadline", "next_occurrence"):
        field = getattr(record, name, None)
        if isinstance(field, Field):
            found.append((name, field))
    role_change = getattr(record, "role_change", None)
    if role_change is not None:
        found.append(("role_change.effective_date", role_change.effective_date))
    return found


# --- retry and quarantine --------------------------------------------------


@dataclass(frozen=True, slots=True)
class Quarantined:
    """Output that failed the contract twice. Held, reported, never written.

    Carries the raw payload so the failure can be read later — a quarantine that
    throws away the evidence teaches nothing.
    """

    family: str
    attempts: int
    errors: list[str]
    raw: Any

    def summary(self) -> str:
        return f"{self.family}: quarantined after {self.attempts} attempts — " + "; ".join(
            self.errors[:5]
        )


def extract_with_retry(
    family: str,
    call: Callable[[str | None], Any],
    *,
    max_attempts: int = 2,
) -> CommonExtraction | Quarantined:
    """Call the model, validate, and on violation retry once with the errors.

    ``call`` receives ``None`` on the first attempt and the flattened validation
    errors on each retry, to be appended to the prompt. On the final failure the
    result is quarantined — the one thing that never happens is a repair.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    feedback: str | None = None
    errors: list[str] = []
    raw: Any = None

    for _attempt in range(1, max_attempts + 1):
        raw = call(feedback)
        try:
            record = validate(family, raw)
            validate_dates(record)
        except SchemaViolation as exc:
            errors = exc.errors
            feedback = (
                "Your previous answer was rejected. Fix exactly these problems "
                "and return the whole object again:\n- " + "\n- ".join(errors)
            )
            continue
        return record

    return Quarantined(family=family, attempts=max_attempts, errors=errors, raw=raw)
