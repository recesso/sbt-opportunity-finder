"""Entity dataclasses.

Repositories return these, never raw rows or dicts. A typo in a field name
becomes an AttributeError at the call site instead of a silent ``None`` three
layers away.

Frozen and slotted: entities read out of the database are values, not mutable
state. To change one, build a new one and write it.

JSON-encoded columns (lists) are handled at the repository boundary so callers
work with real Python lists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

Family = str  # 'ROOM' | 'CHANNEL' | 'EMPLOYER' | 'PERSON'


@dataclass(frozen=True, slots=True)
class Organization:
    org_id: str
    canonical_domain: str
    name: str
    name_normalized: str
    first_seen: str
    aliases: list[str] = field(default_factory=list)
    org_type: str | None = None
    network_id: str | None = None
    member_unit: str | None = None
    employer_reach_est: int | None = None
    sectors: list[str] = field(default_factory=list)
    geo_city: str | None = None
    geo_state: str | None = None
    geo_scope: str | None = None
    tier: str = "C"
    last_mapped: str | None = None
    discovered_from: str | None = None


@dataclass(frozen=True, slots=True)
class Employer:
    employer_id: str
    name: str
    name_normalized: str
    first_seen: str
    domain: str | None = None
    naics: str | None = None
    site_city: str | None = None
    site_state: str | None = None
    employee_count: int | None = None
    sectors: list[str] = field(default_factory=list)
    reached_via_route_id: str | None = None


@dataclass(frozen=True, slots=True)
class Person:
    person_id: str
    name: str
    org_id: str | None = None
    employer_id: str | None = None
    title: str | None = None
    email: str | None = None
    phone: str | None = None
    role: str | None = None
    controls: str | None = None
    source_url: str | None = None
    verified_at: str | None = None
    previous_title: str | None = None
    leverage_change: str | None = None
    change_detected_at: str | None = None


@dataclass(frozen=True, slots=True)
class Route:
    """The unit of work: (target, mechanism, how you get in).

    ``route_url`` is the page you act on. ``evidence_url`` is the page that
    proves the claim. They are never the same field, and a route with no
    ``route_url`` cannot reach the BEST surface.
    """

    route_id: str
    family: Family
    mechanism_name: str
    route_type: str
    series_key: str
    created_at: str
    org_id: str | None = None
    employer_id: str | None = None
    person_id: str | None = None
    route_url: str | None = None
    route_url_is_offdomain: bool = False
    evidence_url: str | None = None
    eligibility: str | None = None
    owner_person_id: str | None = None
    status: str = "active"
    surface: str | None = None
    excluded_by_rule_id: str | None = None
    unresolved: list[str] = field(default_factory=list)
    last_verified: str | None = None


@dataclass(frozen=True, slots=True)
class Evidence:
    """One row per claim. A field with no supporting span cannot be written."""

    ev_id: str
    field_name: str
    source_url: str
    content_hash: str
    extractor: str
    fetched_at: str
    route_id: str | None = None
    org_id: str | None = None
    value: str | None = None
    span_text: str | None = None
    span_match: str | None = None
    snapshot_uri: str | None = None
    prompt_version: str | None = None


@dataclass(frozen=True, slots=True)
class Score:
    score_id: str
    route_id: str
    scored_at: str
    config_hash: str
    fit: int
    route_score: int
    confidence: int
    components: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Trigger:
    trigger_id: str
    employer_id: str
    kind: str
    what: str
    occurred_on: str
    source_url: str
    span_text: str
    detected_at: str
    capability_implication: str | None = None
    decayed_strength: float | None = None
    decay_computed_at: str | None = None


@dataclass(frozen=True, slots=True)
class FounderMark:
    """Founder-owned. Written only by the mark ingester, never by a worker."""

    mark_id: str
    route_id: str
    marked_at: str
    verdict: str | None = None
    target_verdict: str | None = None
    note_freetext: str | None = None
    outcome: str | None = None
    knows_someone: str | None = None


@dataclass(frozen=True, slots=True)
class Rejection:
    """Persists outside the row, keyed by normalized name AND domain.

    Matching on both is what stops a rejected organization returning under a
    name variant.
    """

    rejection_id: str
    created_at: str
    match_name: str | None = None
    match_domain: str | None = None
    family_scope: str = "ALL"
    scope: str = "organization"
    pattern_tag: str | None = None
    reason: str | None = None
    created_from_mark_id: str | None = None


@dataclass(frozen=True, slots=True)
class Run:
    """One execution of one workflow.

    ``not_reached`` is a list, never None: a run that reached everything says so
    with an empty list, and silence is never the same as completeness.
    """

    run_id: str
    workflow: str
    started_at: str
    status: str
    finished_at: str | None = None
    config_hash: str | None = None
    counters: dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0
    not_reached: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
