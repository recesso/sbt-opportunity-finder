"""W3 MechanismExtractor — the highest-risk story in the plan.

Every downstream number inherits this step's quality and no plumbing fixes a bad
extractor. Reads only stored snapshot text and emits a route draft with
field-level provenance.

Four rules, each a failure the predecessor actually made:

1. **Snapshot text only.** The model has no browsing capability in this call and
   is told so. It cannot "remember" the page; it can only read what it is given,
   which is what makes an invented span detectable.
2. **Every span must actually appear in the snapshot.** A field whose span is
   not in the text is not weakly supported — it is fabricated, and it is
   dropped. This is the check the whole extraction contract exists to enable.
3. **`not_stated` is the preferred answer.** A thin page must produce a thin
   record. Padding it with plausible detail is the exact behaviour that filled
   the predecessor with fiction.
4. **The prompt is versioned and stored on every extraction.** A regression six
   weeks from now has to be attributable to a prompt, a model, and a snapshot.

Span checking is deliberately layered rather than binary. Providers re-wrap
markdown, and a span that differs from the page only by whitespace is a real
quote; one that differs by a word is not. ``exact`` and ``normalized`` are kept;
``absent`` is dropped and counted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from finder.acquire.providers.base import FetchError, Snapshot
from finder.acquire.providers.llm import DEFAULT_MAX_TOKENS, LLMProvider
from finder.context import RunContext
from finder.extract.schemas import (
    NOT_STATED,
    PROMPT_CLAUSES,
    CommonExtraction,
    Field,
    Quarantined,
    extract_with_retry,
    json_schema,
)

# Bumped whenever the prompt text changes. Stored on every extraction so a
# regression is attributable rather than mysterious.
PROMPT_VERSION = "w3-2026-09-02"

EXTRACTOR = f"w3/{PROMPT_VERSION}"

# Snapshots run long. This is a page budget, not a judgement about what matters:
# the head of a page carries the mechanism, and the tail carries the footer.
MAX_SNAPSHOT_CHARS = 60_000

_WHITESPACE = re.compile(r"\s+")

SYSTEM_PROMPT = """\
You extract structured facts from a single stored web page for Skill Bridge \
Talent, which sells Capability Engineering to employers.

You are reading STORED TEXT. You have no browsing capability in this call, no \
memory of this page, and no other source. If something is not in the text in \
front of you, it is not available to you.

Your job is to record what the page SAYS, with the exact words that say it. It \
is not to decide whether the opportunity is good. Something else does that, and \
it can only do it if what you record is true.

Non-negotiable rules:
{clauses}

For every field you state a value for, `span` must be text copied VERBATIM from \
the page — the shortest passage that supports the value. If you cannot copy \
such a passage, the value is not_stated. A field with a value and no span will \
be thrown away, and so will a field whose span is not found in the page.\
"""

USER_TEMPLATE = """\
Today's date is {today}. Compare every deadline and date to it.

You are extracting a {family} record.
{family_note}

SOURCE URL: {url}

--- BEGIN STORED PAGE TEXT ---
{snapshot}
--- END STORED PAGE TEXT ---

Record the {family} extraction. Use not_stated wherever the page does not say.\
"""

FAMILY_NOTES: dict[str, str] = {
    "ROOM": (
        "A ROOM is a gathering where employer decision-makers are present and there is a "
        "real role available beyond buying a ticket: a call for speakers, a program or "
        "education committee, a workshop or lunch-and-learn slot, a council seat. "
        "Informal formats count fully — receptions, breakfasts, tours and peer forums are "
        "rooms when employers are in them. route_url is the page you ACT on (the form, the "
        "application); it is often on a DIFFERENT domain from this page, and if the page "
        "links out to a form, that link is the route_url."
    ),
    "CHANNEL": (
        "A CHANNEL is an organization that reaches many employers WITHOUT an event: it is "
        "contracted, funded or paid to work inside employers, or holds them as clients or "
        "company members, and it delivers through instructors, partners or approved "
        "providers rather than staff alone. A channel with no published intake is normal "
        "and valuable — leave route_url not_stated rather than inventing one."
    ),
    "EMPLOYER": (
        "An EMPLOYER record needs at least one dated, citable trigger: an expansion, a new "
        "site, an automation or AI investment, a contract award, a grant, a hiring surge, "
        "or a new COO or VP of Operations. No trigger means no record."
    ),
    "PERSON": (
        "A PERSON record is about someone who controls programming, partnerships, a budget "
        "or an employer cohort. Never guess at whether anyone knows them; that is not in "
        "this page and there is nowhere to put it."
    ),
}


def normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip().lower()


def span_match(span: str | None, snapshot: str) -> str:
    """How well a quoted span is supported by the page.

    ``exact`` — the span is in the page as written.
    ``normalized`` — it differs only by whitespace or case. Providers re-wrap
    markdown constantly; treating that as fabrication would throw away true
    fields for a formatting difference.
    ``absent`` — not there. Not "weakly supported". Fabricated.
    """
    if not span or not span.strip():
        return "absent"
    if span in snapshot:
        return "exact"
    return "normalized" if normalize(span) in normalize(snapshot) else "absent"


@dataclass(slots=True)
class ExtractionResult:
    """One page, extracted. Read ``dropped`` before believing ``record``."""

    record: CommonExtraction | None = None
    quarantined: Quarantined | None = None
    dropped: list[str] = field(default_factory=list)
    span_matches: dict[str, str] = field(default_factory=dict)
    attempts: int = 0
    prompt_version: str = PROMPT_VERSION
    model: str = ""
    content_hash: str = ""
    url: str = ""

    @property
    def ok(self) -> bool:
        return self.record is not None

    @property
    def fabricated(self) -> int:
        """Fields the model stated but could not support. The number that
        matters most: the acceptance bar for this story is zero."""
        return len(self.dropped)

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "ok": self.ok,
            "quarantined": self.quarantined is not None,
            "dropped": self.dropped,
            "attempts": self.attempts,
            "prompt_version": self.prompt_version,
            "model": self.model,
        }


def build_system_prompt() -> str:
    return SYSTEM_PROMPT.format(clauses="\n".join(f"- {c}" for c in PROMPT_CLAUSES))


def build_user_prompt(family: str, snapshot: Snapshot, *, today: str) -> str:
    if family not in FAMILY_NOTES:
        raise KeyError(f"unknown family {family!r}; expected one of {sorted(FAMILY_NOTES)}")
    text = snapshot.markdown[:MAX_SNAPSHOT_CHARS]
    return USER_TEMPLATE.format(
        today=today,
        family=family,
        family_note=FAMILY_NOTES[family],
        url=snapshot.url,
        snapshot=text,
    )


def strip_unsupported(
    record: CommonExtraction, snapshot: str
) -> tuple[CommonExtraction, list[str], dict[str, str]]:
    """Replace every fabricated field with ``not_stated``.

    Dropping the field rather than the whole record is deliberate: a page that
    yielded nine true fields and one invented one is worth nine fields, and
    throwing the record away would lose real evidence to punish a single slip.
    What is NOT acceptable is keeping the tenth.
    """
    dropped: list[str] = []
    matches: dict[str, str] = {}
    updates: dict[str, Any] = {}

    for name, value in _iter_fields(record):
        if not isinstance(value, Field) or not value.stated:
            continue
        verdict = span_match(value.span, snapshot)
        matches[name] = verdict
        if verdict == "absent":
            dropped.append(name)
            updates[name] = _blank(value)

    if not updates:
        return record, dropped, matches
    return _replace_nested(record, updates), dropped, matches


def _blank(original: Field[Any]) -> Field[Any]:
    return type(original)(value=NOT_STATED, span=None, source_url=original.source_url)


def _iter_fields(record: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """Every Field on the record, including one level of nesting."""
    found: list[tuple[str, Any]] = []
    for name in type(record).model_fields:
        value = getattr(record, name, None)
        path = f"{prefix}{name}"
        if isinstance(value, Field):
            found.append((path, value))
        elif hasattr(type(value), "model_fields"):
            found.extend(_iter_fields(value, prefix=f"{path}."))
    return found


def _replace_nested(record: CommonExtraction, updates: dict[str, Any]) -> CommonExtraction:
    """Rebuild the record with the named paths blanked. Models are frozen."""
    data = record.model_dump()
    for path, blanked in updates.items():
        target = data
        parts = path.split(".")
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = blanked.model_dump()
    return type(record).model_validate(data)


class MechanismExtractor:
    """Turn one stored snapshot into a validated, span-checked record."""

    def __init__(
        self,
        llm: LLMProvider,
        *,
        today: str,
        max_attempts: int = 2,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self.llm = llm
        self.today = today
        self.max_attempts = max_attempts
        self.max_tokens = max_tokens

    def extract(
        self,
        family: str,
        snapshot: Snapshot,
        *,
        run: RunContext | None = None,
    ) -> ExtractionResult:
        """Extract one family's record from one snapshot."""
        result = ExtractionResult(url=snapshot.url, content_hash=snapshot.content_hash)
        system = build_system_prompt()
        prompt = build_user_prompt(family, snapshot, today=self.today)
        schema = json_schema(family)

        def call(feedback: str | None) -> Any:
            result.attempts += 1
            text = prompt if feedback is None else f"{prompt}\n\n{feedback}"
            completion = self.llm.complete(
                system=system, prompt=text, schema=schema, max_tokens=self.max_tokens
            )
            result.model = completion.model
            if run is not None:
                run.cost.record(
                    self.llm.name,
                    "extract",
                    units=completion.output_tokens or 1,
                    usd=getattr(self.llm, "cost_per_call_usd", 0.0),
                )
            if completion.truncated:
                # Half a JSON object fails validation for a reason that has
                # nothing to do with the page. Say so, so the retry is honest.
                raise FetchError(
                    f"the model's answer was cut off at {self.max_tokens} tokens; "
                    "the record is incomplete, not the page"
                )
            return self._as_payload(completion.text, snapshot)

        outcome = extract_with_retry(family, call, max_attempts=self.max_attempts)

        if isinstance(outcome, Quarantined):
            result.quarantined = outcome
            if run is not None:
                run.count("quarantined")
                run.record_not_reached("extraction_quarantined", outcome.summary())
            return result

        record, dropped, matches = strip_unsupported(outcome, snapshot.markdown)
        result.record = record
        result.dropped = dropped
        result.span_matches = matches

        if dropped and run is not None:
            run.log.warning(
                "spans_not_found",
                url=snapshot.url,
                fields=dropped,
                note="stated without support; blanked to not_stated",
            )
        return result

    @staticmethod
    def _as_payload(text: str, snapshot: Snapshot) -> Any:
        """Fill in the provenance the harness knows and the model must not guess."""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return text  # let the schema layer report it as a violation
        if isinstance(payload, dict):
            payload["evidence_url"] = snapshot.url
            payload["fetched_at"] = snapshot.fetched_at
            payload["content_hash"] = snapshot.content_hash
        return payload
