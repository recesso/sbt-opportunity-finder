"""Repositories — the only place raw SQL lives.

Enforced by a CI check: ``import sqlite3`` outside ``src/finder/store/`` fails
the build. Everything else in the system works with the dataclasses in
``models.py``.

Only the entities on the M1 path plus the founder-owned ones are implemented.
Repositories for occurrence and signal are deliberately deferred until
a worker actually writes them — an unused abstraction is a liability, not a head
start. The tables exist; the accessors arrive with the code that needs them.

Founder-owned tables (``founder_mark``, ``person_founder``) are reachable only
through :class:`MarkRepo`, which has no generic update path. E1.S3 adds the
runtime guard on top of that structural separation.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from finder.store import guard
from finder.store.db import transaction, utcnow
from finder.store.models import (
    Employer,
    Evidence,
    FetchRecord,
    FounderMark,
    Network,
    Organization,
    Person,
    Rejection,
    Route,
    Run,
    Score,
    Trigger,
)

# Re-exported from `guard`, which is where the enforcement lives. Kept here
# because callers and tests have always looked for it at this name.
FOUNDER_OWNED_TABLES = guard.FOUNDER_OWNED_TABLES


class RepoError(Exception):
    """A write that the schema or the repository layer refused."""


def _json_list(value: str | None) -> list[str]:
    if not value:
        return []
    parsed = json.loads(value)
    return list(parsed) if isinstance(parsed, list) else []


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class _Repo:
    """Shared plumbing. Not an abstraction — just the two lines every repo needs."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def _exec(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        try:
            return self.conn.execute(sql, tuple(params))
        except sqlite3.IntegrityError as exc:
            raise RepoError(str(exc)) from exc
        except sqlite3.DatabaseError as exc:
            # The authorizer refuses with a bare "not authorized". Turn it into
            # an error that names the table and records the attempt.
            table = guard.table_in_statement(sql) if "not authorized" in str(exc) else None
            if table is None:
                raise
            guard.refused(self.conn, table, guard.operation_in_statement(sql), detail=str(exc))
            raise  # pragma: no cover - guard.refused always raises

    def _one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def _all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def count(self) -> int:
        raise NotImplementedError


# --------------------------------------------------------------------------


class OrganizationRepo(_Repo):
    TABLE = "organization"

    def upsert(self, org: Organization) -> Organization:
        """Insert, or update on canonical_domain conflict.

        ``first_seen`` is preserved on update: when an organization was first
        discovered is a fact about history, not about this run.
        """
        self._exec(
            """
            INSERT INTO organization (
                org_id, canonical_domain, name, name_normalized, aliases, org_type,
                network_id, member_unit, employer_reach_est, sectors, geo_city,
                geo_state, geo_scope, tier, first_seen, last_mapped, discovered_from
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(canonical_domain) DO UPDATE SET
                name = excluded.name,
                name_normalized = excluded.name_normalized,
                aliases = excluded.aliases,
                org_type = COALESCE(excluded.org_type, organization.org_type),
                network_id = COALESCE(excluded.network_id, organization.network_id),
                member_unit = COALESCE(excluded.member_unit, organization.member_unit),
                employer_reach_est = COALESCE(
                    excluded.employer_reach_est, organization.employer_reach_est),
                sectors = excluded.sectors,
                geo_city = COALESCE(excluded.geo_city, organization.geo_city),
                geo_state = COALESCE(excluded.geo_state, organization.geo_state),
                geo_scope = COALESCE(excluded.geo_scope, organization.geo_scope),
                tier = excluded.tier,
                last_mapped = COALESCE(excluded.last_mapped, organization.last_mapped),
                discovered_from = COALESCE(
                    excluded.discovered_from, organization.discovered_from)
            """,
            (
                org.org_id,
                org.canonical_domain,
                org.name,
                org.name_normalized,
                _dump(org.aliases),
                org.org_type,
                org.network_id,
                org.member_unit,
                org.employer_reach_est,
                _dump(org.sectors),
                org.geo_city,
                org.geo_state,
                org.geo_scope,
                org.tier,
                org.first_seen,
                org.last_mapped,
                org.discovered_from,
            ),
        )
        got = self.get(org.org_id) or self.get_by_domain(org.canonical_domain)
        if got is None:  # pragma: no cover - would mean the write vanished
            raise RepoError(f"upsert of {org.canonical_domain} did not persist")
        return got

    def get(self, org_id: str) -> Organization | None:
        row = self._one("SELECT * FROM organization WHERE org_id = ?", (org_id,))
        return self._row(row) if row else None

    def get_by_domain(self, domain: str) -> Organization | None:
        row = self._one("SELECT * FROM organization WHERE canonical_domain = ?", (domain,))
        return self._row(row) if row else None

    def find_by_normalized_name(self, name_normalized: str) -> list[Organization]:
        rows = self._all("SELECT * FROM organization WHERE name_normalized = ?", (name_normalized,))
        return [self._row(r) for r in rows]

    def all(self) -> list[Organization]:
        """Every organization, in a stable order."""
        return [
            self._row(r) for r in self._all("SELECT * FROM organization ORDER BY canonical_domain")
        ]

    def set_tier(self, org_id: str, tier: str) -> None:
        """Tier is earned from evidence and recomputed; it is not a fact about
        the row that only its discoverer may set."""
        self._exec("UPDATE organization SET tier = ? WHERE org_id = ?", (tier, org_id))

    def due_for_mapping(self, tier: str, before: str) -> list[Organization]:
        """Tier A weekly, B biweekly, C monthly — the caller supplies the cutoff."""
        rows = self._all(
            "SELECT * FROM organization WHERE tier = ?"
            " AND (last_mapped IS NULL OR last_mapped < ?) ORDER BY last_mapped IS NOT NULL,"
            " last_mapped",
            (tier, before),
        )
        return [self._row(r) for r in rows]

    def mark_mapped(self, org_id: str, when: str | None = None) -> None:
        self._exec(
            "UPDATE organization SET last_mapped = ? WHERE org_id = ?",
            (when or utcnow(), org_id),
        )

    def count(self) -> int:
        return self._one("SELECT COUNT(*) c FROM organization")["c"]  # type: ignore[index]

    @staticmethod
    def _row(row: sqlite3.Row) -> Organization:
        return Organization(
            org_id=row["org_id"],
            canonical_domain=row["canonical_domain"],
            name=row["name"],
            name_normalized=row["name_normalized"],
            first_seen=row["first_seen"],
            aliases=_json_list(row["aliases"]),
            org_type=row["org_type"],
            network_id=row["network_id"],
            member_unit=row["member_unit"],
            employer_reach_est=row["employer_reach_est"],
            sectors=_json_list(row["sectors"]),
            geo_city=row["geo_city"],
            geo_state=row["geo_state"],
            geo_scope=row["geo_scope"],
            tier=row["tier"],
            last_mapped=row["last_mapped"],
            discovered_from=row["discovered_from"],
        )


class EmployerRepo(_Repo):
    def upsert(self, emp: Employer) -> Employer:
        self._exec(
            """
            INSERT INTO employer (
                employer_id, name, name_normalized, domain, naics, site_city,
                site_state, employee_count, sectors, reached_via_route_id, first_seen
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(employer_id) DO UPDATE SET
                name = excluded.name,
                name_normalized = excluded.name_normalized,
                domain = COALESCE(excluded.domain, employer.domain),
                naics = COALESCE(excluded.naics, employer.naics),
                site_city = COALESCE(excluded.site_city, employer.site_city),
                site_state = COALESCE(excluded.site_state, employer.site_state),
                employee_count = COALESCE(excluded.employee_count, employer.employee_count),
                sectors = excluded.sectors,
                reached_via_route_id = COALESCE(
                    excluded.reached_via_route_id, employer.reached_via_route_id)
            """,
            (
                emp.employer_id,
                emp.name,
                emp.name_normalized,
                emp.domain,
                emp.naics,
                emp.site_city,
                emp.site_state,
                emp.employee_count,
                _dump(emp.sectors),
                emp.reached_via_route_id,
                emp.first_seen,
            ),
        )
        got = self.get(emp.employer_id)
        if got is None:  # pragma: no cover
            raise RepoError(f"upsert of employer {emp.employer_id} did not persist")
        return got

    def get(self, employer_id: str) -> Employer | None:
        row = self._one("SELECT * FROM employer WHERE employer_id = ?", (employer_id,))
        if not row:
            return None
        return Employer(
            employer_id=row["employer_id"],
            name=row["name"],
            name_normalized=row["name_normalized"],
            first_seen=row["first_seen"],
            domain=row["domain"],
            naics=row["naics"],
            site_city=row["site_city"],
            site_state=row["site_state"],
            employee_count=row["employee_count"],
            sectors=_json_list(row["sectors"]),
            reached_via_route_id=row["reached_via_route_id"],
        )

    def link_to_channel(self, employer_id: str, route_id: str) -> None:
        """Cross-family link: this employer is reachable through a CHANNEL route.

        When a trigger later fires here, the EMPLOYER route becomes
        CHANNEL_INTRO because a way in already exists.
        """
        self._exec(
            "UPDATE employer SET reached_via_route_id = ? WHERE employer_id = ?",
            (route_id, employer_id),
        )

    def count(self) -> int:
        return self._one("SELECT COUNT(*) c FROM employer")["c"]  # type: ignore[index]


class PersonRepo(_Repo):
    def upsert(self, person: Person) -> Person:
        self._exec(
            """
            INSERT INTO person (
                person_id, org_id, employer_id, name, title, email, phone, role,
                controls, source_url, verified_at, previous_title, leverage_change,
                change_detected_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(person_id) DO UPDATE SET
                title = COALESCE(excluded.title, person.title),
                email = COALESCE(excluded.email, person.email),
                phone = COALESCE(excluded.phone, person.phone),
                role = COALESCE(excluded.role, person.role),
                controls = COALESCE(excluded.controls, person.controls),
                source_url = COALESCE(excluded.source_url, person.source_url),
                verified_at = COALESCE(excluded.verified_at, person.verified_at),
                previous_title = COALESCE(excluded.previous_title, person.previous_title),
                leverage_change = COALESCE(excluded.leverage_change, person.leverage_change),
                change_detected_at = COALESCE(
                    excluded.change_detected_at, person.change_detected_at)
            """,
            (
                person.person_id,
                person.org_id,
                person.employer_id,
                person.name,
                person.title,
                person.email,
                person.phone,
                person.role,
                person.controls,
                person.source_url,
                person.verified_at,
                person.previous_title,
                person.leverage_change,
                person.change_detected_at,
            ),
        )
        got = self.get(person.person_id)
        if got is None:  # pragma: no cover
            raise RepoError(f"upsert of person {person.person_id} did not persist")
        return got

    def get(self, person_id: str) -> Person | None:
        row = self._one("SELECT * FROM person WHERE person_id = ?", (person_id,))
        if not row:
            return None
        return Person(
            person_id=row["person_id"],
            name=row["name"],
            org_id=row["org_id"],
            employer_id=row["employer_id"],
            title=row["title"],
            email=row["email"],
            phone=row["phone"],
            role=row["role"],
            controls=row["controls"],
            source_url=row["source_url"],
            verified_at=row["verified_at"],
            previous_title=row["previous_title"],
            leverage_change=row["leverage_change"],
            change_detected_at=row["change_detected_at"],
        )

    def for_organization(self, org_id: str) -> list[Person]:
        rows = self._all("SELECT person_id FROM person WHERE org_id = ?", (org_id,))
        return [p for r in rows if (p := self.get(r["person_id"]))]

    def count(self) -> int:
        return self._one("SELECT COUNT(*) c FROM person")["c"]  # type: ignore[index]


class RouteRepo(_Repo):
    def upsert(self, route: Route) -> Route:
        self._exec(
            """
            INSERT INTO route (
                route_id, family, org_id, employer_id, person_id, mechanism_name,
                route_type, route_url, route_url_is_offdomain, evidence_url,
                eligibility, owner_person_id, series_key, status, surface,
                excluded_by_rule_id, unresolved, created_at, last_verified
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(series_key) DO UPDATE SET
                mechanism_name = excluded.mechanism_name,
                route_type = excluded.route_type,
                route_url = COALESCE(excluded.route_url, route.route_url),
                route_url_is_offdomain = excluded.route_url_is_offdomain,
                evidence_url = COALESCE(excluded.evidence_url, route.evidence_url),
                eligibility = COALESCE(excluded.eligibility, route.eligibility),
                owner_person_id = COALESCE(excluded.owner_person_id, route.owner_person_id),
                status = excluded.status,
                surface = excluded.surface,
                unresolved = excluded.unresolved,
                last_verified = COALESCE(excluded.last_verified, route.last_verified)
            """,
            (
                route.route_id,
                route.family,
                route.org_id,
                route.employer_id,
                route.person_id,
                route.mechanism_name,
                route.route_type,
                route.route_url,
                int(route.route_url_is_offdomain),
                route.evidence_url,
                route.eligibility,
                route.owner_person_id,
                route.series_key,
                route.status,
                route.surface,
                route.excluded_by_rule_id,
                _dump(route.unresolved),
                route.created_at,
                route.last_verified,
            ),
        )
        got = self.get_by_series_key(route.series_key)
        if got is None:  # pragma: no cover
            raise RepoError(f"upsert of route {route.series_key} did not persist")
        return got

    def get(self, route_id: str) -> Route | None:
        row = self._one("SELECT * FROM route WHERE route_id = ?", (route_id,))
        return self._row(row) if row else None

    def get_by_series_key(self, series_key: str) -> Route | None:
        row = self._one("SELECT * FROM route WHERE series_key = ?", (series_key,))
        return self._row(row) if row else None

    def by_surface(self, surface: str, family: str | None = None) -> list[Route]:
        if family:
            rows = self._all(
                "SELECT * FROM route WHERE surface = ? AND family = ?", (surface, family)
            )
        else:
            rows = self._all("SELECT * FROM route WHERE surface = ?", (surface,))
        return [self._row(r) for r in rows]

    def exclude(self, route_id: str, rule_id: str) -> None:
        """Rejected routes are retained with the reason. Nothing is ever deleted."""
        self._exec(
            "UPDATE route SET status = 'excluded', surface = 'LIBRARY',"
            " excluded_by_rule_id = ? WHERE route_id = ?",
            (rule_id, route_id),
        )

    def set_surface(self, route_id: str, surface: str) -> None:
        self._exec("UPDATE route SET surface = ? WHERE route_id = ?", (surface, route_id))

    def count(self) -> int:
        return self._one("SELECT COUNT(*) c FROM route")["c"]  # type: ignore[index]

    @staticmethod
    def _row(row: sqlite3.Row) -> Route:
        return Route(
            route_id=row["route_id"],
            family=row["family"],
            mechanism_name=row["mechanism_name"],
            route_type=row["route_type"],
            series_key=row["series_key"],
            created_at=row["created_at"],
            org_id=row["org_id"],
            employer_id=row["employer_id"],
            person_id=row["person_id"],
            route_url=row["route_url"],
            route_url_is_offdomain=bool(row["route_url_is_offdomain"]),
            evidence_url=row["evidence_url"],
            eligibility=row["eligibility"],
            owner_person_id=row["owner_person_id"],
            status=row["status"],
            surface=row["surface"],
            excluded_by_rule_id=row["excluded_by_rule_id"],
            unresolved=_json_list(row["unresolved"]),
            last_verified=row["last_verified"],
        )


class EvidenceRepo(_Repo):
    def add(self, ev: Evidence) -> Evidence:
        """Re-extracting the same field from the same snapshot updates in place."""
        self._exec(
            """
            INSERT INTO evidence (
                ev_id, route_id, org_id, field_name, value, span_text, span_match,
                source_url, content_hash, snapshot_uri, extractor, prompt_version, fetched_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(ev_id) DO UPDATE SET
                value = excluded.value,
                span_text = excluded.span_text,
                span_match = excluded.span_match,
                snapshot_uri = excluded.snapshot_uri,
                extractor = excluded.extractor,
                prompt_version = excluded.prompt_version,
                fetched_at = excluded.fetched_at
            """,
            (
                ev.ev_id,
                ev.route_id,
                ev.org_id,
                ev.field_name,
                ev.value,
                ev.span_text,
                ev.span_match,
                ev.source_url,
                ev.content_hash,
                ev.snapshot_uri,
                ev.extractor,
                ev.prompt_version,
                ev.fetched_at,
            ),
        )
        got = self.get(ev.ev_id)
        if got is None:  # pragma: no cover
            raise RepoError(f"evidence {ev.ev_id} did not persist")
        return got

    def get(self, ev_id: str) -> Evidence | None:
        row = self._one("SELECT * FROM evidence WHERE ev_id = ?", (ev_id,))
        if not row:
            return None
        return Evidence(
            ev_id=row["ev_id"],
            field_name=row["field_name"],
            source_url=row["source_url"],
            content_hash=row["content_hash"],
            extractor=row["extractor"],
            fetched_at=row["fetched_at"],
            route_id=row["route_id"],
            org_id=row["org_id"],
            value=row["value"],
            span_text=row["span_text"],
            span_match=row["span_match"],
            snapshot_uri=row["snapshot_uri"],
            prompt_version=row["prompt_version"],
        )

    def for_organization(self, org_id: str) -> list[Evidence]:
        """Claims recorded against an organization rather than a route.

        Graph expansion writes here: "GaMEP names this body as an approved
        provider" is a fact about the organization, established before any route
        at it exists.
        """
        rows = self._all(
            "SELECT ev_id FROM evidence WHERE org_id = ? ORDER BY field_name, source_url",
            (org_id,),
        )
        return [e for r in rows if (e := self.get(r["ev_id"]))]

    def for_route(self, route_id: str) -> list[Evidence]:
        rows = self._all("SELECT ev_id FROM evidence WHERE route_id = ?", (route_id,))
        return [e for r in rows if (e := self.get(r["ev_id"]))]

    def fields_without_span(self, route_id: str) -> list[str]:
        """Fields whose span is missing or unfindable.

        A field with no supporting span cannot be written; this is the query
        that proves it after the fact.
        """
        rows = self._all(
            "SELECT field_name FROM evidence WHERE route_id = ?"
            " AND (span_text IS NULL OR span_match = 'absent')",
            (route_id,),
        )
        return [r["field_name"] for r in rows]

    def count(self) -> int:
        return self._one("SELECT COUNT(*) c FROM evidence")["c"]  # type: ignore[index]


class ScoreRepo(_Repo):
    def add(self, score: Score) -> Score:
        self._exec(
            "INSERT INTO score (score_id, route_id, scored_at, config_hash, fit,"
            " route_score, confidence, components) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(score_id) DO UPDATE SET fit = excluded.fit,"
            " route_score = excluded.route_score, confidence = excluded.confidence,"
            " components = excluded.components",
            (
                score.score_id,
                score.route_id,
                score.scored_at,
                score.config_hash,
                score.fit,
                score.route_score,
                score.confidence,
                _dump(score.components),
            ),
        )
        got = self.latest_for_route(score.route_id)
        if got is None:  # pragma: no cover
            raise RepoError(f"score for {score.route_id} did not persist")
        return got

    def latest_for_route(self, route_id: str) -> Score | None:
        row = self._one(
            "SELECT * FROM score WHERE route_id = ? ORDER BY scored_at DESC, rowid DESC LIMIT 1",
            (route_id,),
        )
        if not row:
            return None
        return Score(
            score_id=row["score_id"],
            route_id=row["route_id"],
            scored_at=row["scored_at"],
            config_hash=row["config_hash"],
            fit=row["fit"],
            route_score=row["route_score"],
            confidence=row["confidence"],
            components=json.loads(row["components"]),
        )

    def best_fit_for_organization(self, org_id: str) -> int | None:
        """The organization's best FIT, taking each route's LATEST score.

        Best, not average: one strong route is a reason to look at an
        organization weekly, and averaging it against four weak ones buries it.
        The same reasoning as ``access_warmth`` rolling up as the best known
        path rather than the mean.
        """
        row = self._one(
            "SELECT MAX(s.fit) AS best FROM score s"
            " JOIN route r ON r.route_id = s.route_id"
            " WHERE r.org_id = ?"
            "   AND s.scored_at = ("
            "     SELECT MAX(s2.scored_at) FROM score s2 WHERE s2.route_id = s.route_id)",
            (org_id,),
        )
        return row["best"] if row and row["best"] is not None else None

    def history(self, route_id: str) -> list[Score]:
        rows = self._all(
            "SELECT score_id FROM score WHERE route_id = ? ORDER BY scored_at", (route_id,)
        )
        out: list[Score] = []
        for r in rows:
            row = self._one("SELECT * FROM score WHERE score_id = ?", (r["score_id"],))
            if row:
                out.append(
                    Score(
                        score_id=row["score_id"],
                        route_id=row["route_id"],
                        scored_at=row["scored_at"],
                        config_hash=row["config_hash"],
                        fit=row["fit"],
                        route_score=row["route_score"],
                        confidence=row["confidence"],
                        components=json.loads(row["components"]),
                    )
                )
        return out

    def count(self) -> int:
        return self._one("SELECT COUNT(*) c FROM score")["c"]  # type: ignore[index]


class TriggerRepo(_Repo):
    def add(self, trig: Trigger) -> Trigger:
        self._exec(
            """
            INSERT INTO trigger (
                trigger_id, employer_id, kind, what, occurred_on, source_url, span_text,
                capability_implication, detected_at, decayed_strength, decay_computed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(trigger_id) DO UPDATE SET
                decayed_strength = excluded.decayed_strength,
                decay_computed_at = excluded.decay_computed_at
            """,
            (
                trig.trigger_id,
                trig.employer_id,
                trig.kind,
                trig.what,
                trig.occurred_on,
                trig.source_url,
                trig.span_text,
                trig.capability_implication,
                trig.detected_at,
                trig.decayed_strength,
                trig.decay_computed_at,
            ),
        )
        got = self.get(trig.trigger_id)
        if got is None:  # pragma: no cover
            raise RepoError(f"trigger {trig.trigger_id} did not persist")
        return got

    def get(self, trigger_id: str) -> Trigger | None:
        row = self._one("SELECT * FROM trigger WHERE trigger_id = ?", (trigger_id,))
        if not row:
            return None
        return Trigger(
            trigger_id=row["trigger_id"],
            employer_id=row["employer_id"],
            kind=row["kind"],
            what=row["what"],
            occurred_on=row["occurred_on"],
            source_url=row["source_url"],
            span_text=row["span_text"],
            detected_at=row["detected_at"],
            capability_implication=row["capability_implication"],
            decayed_strength=row["decayed_strength"],
            decay_computed_at=row["decay_computed_at"],
        )

    def for_employer(self, employer_id: str) -> list[Trigger]:
        rows = self._all(
            "SELECT trigger_id FROM trigger WHERE employer_id = ? ORDER BY occurred_on DESC",
            (employer_id,),
        )
        return [t for r in rows if (t := self.get(r["trigger_id"]))]

    def strongest_for_employer(self, employer_id: str) -> float:
        """Max decayed strength. A trigger below 1.0 drops its route to LIBRARY."""
        row = self._one(
            "SELECT MAX(COALESCE(decayed_strength, 0.0)) s FROM trigger WHERE employer_id = ?",
            (employer_id,),
        )
        return float(row["s"] or 0.0) if row else 0.0

    def set_decay(self, trigger_id: str, strength: float, computed_at: str) -> None:
        self._exec(
            "UPDATE trigger SET decayed_strength = ?, decay_computed_at = ? WHERE trigger_id = ?",
            (strength, computed_at, trigger_id),
        )

    def count(self) -> int:
        return self._one("SELECT COUNT(*) c FROM trigger")["c"]  # type: ignore[index]


class MarkRepo(_Repo):
    """Founder-owned. Read freely; write only through :meth:`ingest`.

    There is deliberately no ``update`` and no ``delete``. A mark is a record of
    a decision the founder made; the predecessor destroyed his work repeatedly
    by overwriting these, and he had to redo dispositions more than once.
    """

    def ingest(self, mark: FounderMark) -> FounderMark:
        """Insert a mark. An existing (route_id, marked_at) is left untouched.

        The ONE sanctioned founder write path. Everything else — including raw
        SQL — is refused by the authorizer installed on the connection.
        """
        with guard.founder_write_allowed():
            return self._ingest(mark)

    def _ingest(self, mark: FounderMark) -> FounderMark:
        self._exec(
            "INSERT INTO founder_mark (mark_id, route_id, marked_at, verdict,"
            " target_verdict, note_freetext, outcome, knows_someone)"
            " VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(route_id, marked_at) DO NOTHING",
            (
                mark.mark_id,
                mark.route_id,
                mark.marked_at,
                mark.verdict,
                mark.target_verdict,
                mark.note_freetext,
                mark.outcome,
                mark.knows_someone,
            ),
        )
        row = self._one(
            "SELECT * FROM founder_mark WHERE route_id = ? AND marked_at = ?",
            (mark.route_id, mark.marked_at),
        )
        if row is None:  # pragma: no cover
            raise RepoError(f"mark for {mark.route_id} did not persist")
        guard.record_attempt(
            self.conn,
            "founder_mark",
            "INSERT",
            allowed=True,
            caller=guard.caller_of_record(),
            detail=f"route={mark.route_id} marked_at={mark.marked_at}",
        )
        return self._row(row)

    def for_route(self, route_id: str) -> list[FounderMark]:
        rows = self._all(
            "SELECT * FROM founder_mark WHERE route_id = ? ORDER BY marked_at", (route_id,)
        )
        return [self._row(r) for r in rows]

    def verdicts_for_organization(self, org_id: str) -> list[str]:
        """Every verdict the founder has recorded on any route at this organization.

        Read-only, like everything else here. Marks are appended, never changed.
        """
        rows = self._all(
            "SELECT m.verdict FROM founder_mark m"
            " JOIN route r ON r.route_id = m.route_id"
            " WHERE r.org_id = ? AND m.verdict IS NOT NULL"
            " ORDER BY m.marked_at",
            (org_id,),
        )
        return [r["verdict"] for r in rows]

    def all_marks(self) -> list[FounderMark]:
        """The entire training set. Every learning mechanism starts here."""
        return [self._row(r) for r in self._all("SELECT * FROM founder_mark ORDER BY marked_at")]

    def count(self) -> int:
        return self._one("SELECT COUNT(*) c FROM founder_mark")["c"]  # type: ignore[index]

    @staticmethod
    def _row(row: sqlite3.Row) -> FounderMark:
        return FounderMark(
            mark_id=row["mark_id"],
            route_id=row["route_id"],
            marked_at=row["marked_at"],
            verdict=row["verdict"],
            target_verdict=row["target_verdict"],
            note_freetext=row["note_freetext"],
            outcome=row["outcome"],
            knows_someone=row["knows_someone"],
        )


class RejectionRepo(_Repo):
    """Standing rejections, keyed by normalized name AND registrable domain.

    Matching on both is the fix for the observed failure: twelve rows of a
    permanently rejected organization survived in the predecessor because the
    check was by name only and the organization reappeared under a variant.
    """

    def add(self, rej: Rejection) -> Rejection:
        self._exec(
            "INSERT INTO rejection (rejection_id, match_name, match_domain, family_scope,"
            " scope, pattern_tag, reason, created_from_mark_id, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(rejection_id) DO UPDATE SET reason = excluded.reason",
            (
                rej.rejection_id,
                rej.match_name,
                rej.match_domain,
                rej.family_scope,
                rej.scope,
                rej.pattern_tag,
                rej.reason,
                rej.created_from_mark_id,
                rej.created_at,
            ),
        )
        got = self.get(rej.rejection_id)
        if got is None:  # pragma: no cover
            raise RepoError(f"rejection {rej.rejection_id} did not persist")
        return got

    def get(self, rejection_id: str) -> Rejection | None:
        row = self._one("SELECT * FROM rejection WHERE rejection_id = ?", (rejection_id,))
        return self._row(row) if row else None

    def matching(
        self, *, name_normalized: str | None, domain: str | None, family: str
    ) -> list[Rejection]:
        """Every rule that would block this candidate.

        A rule scoped to one family does not block another — rejecting a room
        says nothing about a channel at the same organization.
        """
        rows = self._all(
            "SELECT * FROM rejection WHERE (family_scope = 'ALL' OR family_scope = ?)"
            " AND ((match_name IS NOT NULL AND match_name = ?)"
            "   OR (match_domain IS NOT NULL AND match_domain = ?))",
            (family, name_normalized or "", domain or ""),
        )
        return [self._row(r) for r in rows]

    def blocks(self, *, name_normalized: str | None, domain: str | None, family: str) -> bool:
        return bool(self.matching(name_normalized=name_normalized, domain=domain, family=family))

    def count(self) -> int:
        return self._one("SELECT COUNT(*) c FROM rejection")["c"]  # type: ignore[index]

    @staticmethod
    def _row(row: sqlite3.Row) -> Rejection:
        return Rejection(
            rejection_id=row["rejection_id"],
            created_at=row["created_at"],
            match_name=row["match_name"],
            match_domain=row["match_domain"],
            family_scope=row["family_scope"],
            scope=row["scope"],
            pattern_tag=row["pattern_tag"],
            reason=row["reason"],
            created_from_mark_id=row["created_from_mark_id"],
        )


# --------------------------------------------------------------------------

# Counter columns on the run row. Anything outside this tuple is a typo, and a
# typo that silently does nothing is how a run report becomes fiction.
RUN_COUNTERS: tuple[str, ...] = (
    "orgs_mapped",
    "pages_fetched",
    "candidates",
    "survived_gate",
    "survived_rerank",
    "routes_written",
    "quarantined",
)

TERMINAL_ITEM_STATES: frozenset[str] = frozenset({"done", "failed", "skipped"})


class RunRepo(_Repo):
    """The run ledger: one row per execution, opened and closed honestly.

    A run left ``running`` means the process died. That is a real state worth
    keeping — :meth:`unfinished` is how a resume finds its work.
    """

    def start(self, run_id: str, workflow: str, *, config_hash: str | None = None) -> Run:
        self._exec(
            "INSERT INTO run (run_id, workflow, started_at, status, config_hash)"
            " VALUES (?,?,?,'running',?)",
            (run_id, workflow, utcnow(), config_hash),
        )
        got = self.get(run_id)
        if got is None:  # pragma: no cover - insert succeeded or raised
            raise RepoError(f"run {run_id} did not persist")
        return got

    def get(self, run_id: str) -> Run | None:
        row = self._one("SELECT * FROM run WHERE run_id = ?", (run_id,))
        return self._row(row) if row else None

    def reopen(self, run_id: str) -> None:
        """Mark a run running again so a resume is visible while it is happening."""
        self._exec(
            "UPDATE run SET status = 'running', finished_at = NULL WHERE run_id = ?",
            (run_id,),
        )

    def bump(self, run_id: str, counter: str, n: int = 1) -> None:
        """Increment a counter in the row, not in memory.

        Counters totalled at close vanish with the process that dies — and that
        is precisely the run whose report matters. Written through, a crashed
        run still reports the work it actually did.
        """
        if counter not in RUN_COUNTERS:
            raise RepoError(f"unknown run counter {counter!r}; expected one of {RUN_COUNTERS}")
        # Interpolated, not bound: SQLite cannot parameterise a column name. Safe
        # only because the name was just checked against the fixed tuple above.
        self._exec(f"UPDATE run SET {counter} = {counter} + ? WHERE run_id = ?", (n, run_id))

    def append_not_reached(self, run_id: str, entry: dict[str, Any]) -> None:
        """Append one truncation record, durably.

        Read-modify-write is fine here: not_reached holds a handful of entries
        per run, and a run has one writer.
        """
        row = self._one("SELECT not_reached FROM run WHERE run_id = ?", (run_id,))
        if row is None:
            raise RepoError(f"no such run: {run_id}")
        parsed = json.loads(row["not_reached"] or "[]")
        current = list(parsed) if isinstance(parsed, list) else []
        current.append(entry)
        self._exec("UPDATE run SET not_reached = ? WHERE run_id = ?", (_dump(current), run_id))

    def finish(self, run_id: str, *, status: str, error: str | None, cost_usd: float) -> None:
        """Close the book. Counters and not_reached are already on the row."""
        self._exec(
            "UPDATE run SET finished_at = ?, status = ?, error = ?, cost_usd = ? WHERE run_id = ?",
            (utcnow(), status, error, cost_usd, run_id),
        )

    def last(self, workflow: str | None = None) -> Run | None:
        if workflow is None:
            row = self._one("SELECT * FROM run ORDER BY started_at DESC LIMIT 1")
        else:
            row = self._one(
                "SELECT * FROM run WHERE workflow = ? ORDER BY started_at DESC LIMIT 1",
                (workflow,),
            )
        return self._row(row) if row else None

    def unfinished(self) -> list[Run]:
        """Runs a process died inside. The starting point for every resume."""
        return [
            self._row(r)
            for r in self._all("SELECT * FROM run WHERE status = 'running' ORDER BY started_at")
        ]

    def count(self) -> int:
        return self._one("SELECT COUNT(*) c FROM run")["c"]  # type: ignore[index]

    @staticmethod
    def _row(row: sqlite3.Row) -> Run:
        parsed = json.loads(row["not_reached"] or "[]")
        return Run(
            run_id=row["run_id"],
            workflow=row["workflow"],
            started_at=row["started_at"],
            status=row["status"],
            finished_at=row["finished_at"],
            config_hash=row["config_hash"],
            counters={c: row[c] for c in RUN_COUNTERS},
            cost_usd=row["cost_usd"],
            not_reached=list(parsed) if isinstance(parsed, list) else [],
            error=row["error"],
        )


class StageRunRepo(_Repo):
    """Per-item checkpoints (ADR-010).

    The one subtlety worth stating: a row left ``running`` by a crashed process
    is NOT terminal. Refusing to reclaim it would strand exactly the item the
    process died on, and the loss would be invisible.
    """

    def status(self, run_id: str, stage: str, item_key: str) -> str | None:
        row = self._one(
            "SELECT status FROM stage_run WHERE run_id = ? AND stage = ? AND item_key = ?",
            (run_id, stage, item_key),
        )
        return row["status"] if row else None

    def start_item(self, run_id: str, stage: str, item_key: str) -> None:
        self._exec(
            "INSERT INTO stage_run (run_id, stage, item_key, status, started_at)"
            " VALUES (?,?,?,'running',?)"
            " ON CONFLICT(run_id, stage, item_key) DO UPDATE SET"
            " status = 'running', started_at = excluded.started_at,"
            " finished_at = NULL, error = NULL",
            (run_id, stage, item_key, utcnow()),
        )

    def finish_item(
        self, run_id: str, stage: str, item_key: str, status: str, error: str | None
    ) -> bool:
        """False when no such claimed item exists — the caller must not ignore it."""
        cur = self._exec(
            "UPDATE stage_run SET status = ?, finished_at = ?, error = ?"
            " WHERE run_id = ? AND stage = ? AND item_key = ?",
            (status, utcnow(), error, run_id, stage, item_key),
        )
        return cur.rowcount > 0

    def finished_keys(self, run_id: str, stage: str) -> set[str]:
        placeholders = ",".join("?" for _ in TERMINAL_ITEM_STATES)
        rows = self._all(
            "SELECT item_key FROM stage_run WHERE run_id = ? AND stage = ?"
            f" AND status IN ({placeholders})",
            (run_id, stage, *sorted(TERMINAL_ITEM_STATES)),
        )
        return {r["item_key"] for r in rows}

    def summary(self, run_id: str, stage: str) -> dict[str, int]:
        rows = self._all(
            "SELECT status, COUNT(*) c FROM stage_run WHERE run_id = ? AND stage = ?"
            " GROUP BY status",
            (run_id, stage),
        )
        return {r["status"]: r["c"] for r in rows}

    def count(self) -> int:
        return self._one("SELECT COUNT(*) c FROM stage_run")["c"]  # type: ignore[index]


class CostRepo(_Repo):
    """Per-provider spend, written as it is incurred.

    Totals are read back from these rows rather than accumulated in memory, so a
    resumed run reports what the whole run cost, not what this process cost.
    """

    def record(
        self,
        cost_id: str,
        run_id: str,
        provider: str,
        operation: str,
        *,
        units: float = 1.0,
        usd: float = 0.0,
    ) -> None:
        self._exec(
            "INSERT INTO cost_event (cost_id, run_id, provider, operation, units, usd,"
            " recorded_at) VALUES (?,?,?,?,?,?,?)",
            (cost_id, run_id, provider, operation, units, usd, utcnow()),
        )

    def total(self, run_id: str) -> float:
        row = self._one(
            "SELECT COALESCE(SUM(usd), 0.0) s FROM cost_event WHERE run_id = ?", (run_id,)
        )
        return round(float(row["s"]), 6)  # type: ignore[index]

    def by_provider(self, run_id: str) -> dict[str, float]:
        rows = self._all(
            "SELECT provider, SUM(usd) s FROM cost_event WHERE run_id = ?"
            " GROUP BY provider ORDER BY provider",
            (run_id,),
        )
        return {r["provider"]: round(float(r["s"]), 6) for r in rows}

    def count(self) -> int:
        return self._one("SELECT COUNT(*) c FROM cost_event")["c"]  # type: ignore[index]


# --------------------------------------------------------------------------


class FetchLogRepo(_Repo):
    """One row per URL, pointing at the snapshot it produced.

    Snapshots are addressed by content, which is right for the audit trail and
    useless for answering "have I already fetched this page today?". This is the
    other half of that question.
    """

    def get(self, url: str) -> FetchRecord | None:
        row = self._one("SELECT * FROM fetch_log WHERE url = ?", (url,))
        return self._row(row) if row else None

    def record(
        self,
        url: str,
        *,
        content_hash: str,
        status: int,
        provider: str,
        canonical_url: str | None = None,
        is_pdf: bool = False,
        links: Sequence[str] = (),
        fetched_at: str | None = None,
    ) -> FetchRecord:
        """Log a live fetch.

        ``first_fetched_at`` is preserved on conflict and ``change_count`` rises
        only when the hash actually changed — a page re-fetched unchanged is not
        a change, and counting it as one would make every page look volatile.
        """
        now = fetched_at or utcnow()
        self._exec(
            "INSERT INTO fetch_log (url, content_hash, canonical_url, status, is_pdf,"
            " provider, links, first_fetched_at, last_fetched_at, fetch_count, change_count)"
            " VALUES (?,?,?,?,?,?,?,?,?,1,0)"
            " ON CONFLICT(url) DO UPDATE SET"
            "   content_hash = excluded.content_hash,"
            "   canonical_url = excluded.canonical_url,"
            "   status = excluded.status,"
            "   is_pdf = excluded.is_pdf,"
            "   provider = excluded.provider,"
            "   links = excluded.links,"
            "   last_fetched_at = excluded.last_fetched_at,"
            "   fetch_count = fetch_log.fetch_count + 1,"
            "   change_count = fetch_log.change_count"
            "     + (CASE WHEN fetch_log.content_hash = excluded.content_hash THEN 0 ELSE 1 END)",
            (
                url,
                content_hash,
                canonical_url,
                status,
                1 if is_pdf else 0,
                provider,
                _dump(list(links)),
                now,
                now,
            ),
        )
        got = self.get(url)
        if got is None:  # pragma: no cover - insert succeeded or raised
            raise RepoError(f"fetch_log row for {url} did not persist")
        return got

    def by_hash(self, content_hash: str) -> list[FetchRecord]:
        """Every URL that produced this snapshot. Mirrors and redirects show up
        here, which is how one page stops becoming three organizations."""
        return [
            self._row(r)
            for r in self._all(
                "SELECT * FROM fetch_log WHERE content_hash = ? ORDER BY url", (content_hash,)
            )
        ]

    def count(self) -> int:
        return self._one("SELECT COUNT(*) c FROM fetch_log")["c"]  # type: ignore[index]

    @staticmethod
    def _row(row: sqlite3.Row) -> FetchRecord:
        return FetchRecord(
            url=row["url"],
            content_hash=row["content_hash"],
            canonical_url=row["canonical_url"],
            status=row["status"],
            is_pdf=bool(row["is_pdf"]),
            provider=row["provider"],
            links=_json_list(row["links"]),
            first_fetched_at=row["first_fetched_at"],
            last_fetched_at=row["last_fetched_at"],
            fetch_count=row["fetch_count"],
            change_count=row["change_count"],
        )


class NetworkRepo(_Repo):
    """The network registry. Organizations reference it, so it is written first.

    ``node_count_actual`` holds what W1 counted. The planning estimate from
    ``networks.yaml`` never reaches this table — a figure written as data stops
    being a planning figure the moment something reads it back.
    """

    def upsert(self, net: Network) -> Network:
        self._exec(
            "INSERT INTO network (network_id, name, directory_url, discovery_method,"
            " sectors, tier, node_count_actual, last_refreshed) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(network_id) DO UPDATE SET"
            "   name = excluded.name,"
            "   directory_url = excluded.directory_url,"
            "   discovery_method = excluded.discovery_method,"
            "   sectors = excluded.sectors,"
            "   tier = excluded.tier,"
            "   node_count_actual = COALESCE("
            "     excluded.node_count_actual, network.node_count_actual),"
            "   last_refreshed = COALESCE(excluded.last_refreshed, network.last_refreshed)",
            (
                net.network_id,
                net.name,
                net.directory_url,
                net.discovery_method,
                _dump(net.sectors),
                net.tier,
                net.node_count_actual,
                net.last_refreshed,
            ),
        )
        got = self.get(net.network_id)
        if got is None:  # pragma: no cover - insert succeeded or raised
            raise RepoError(f"network {net.network_id} did not persist")
        return got

    def get(self, network_id: str) -> Network | None:
        row = self._one("SELECT * FROM network WHERE network_id = ?", (network_id,))
        return self._row(row) if row else None

    def all(self) -> list[Network]:
        return [self._row(r) for r in self._all("SELECT * FROM network ORDER BY network_id")]

    def count(self) -> int:
        return self._one("SELECT COUNT(*) c FROM network")["c"]  # type: ignore[index]

    @staticmethod
    def _row(row: sqlite3.Row) -> Network:
        return Network(
            network_id=row["network_id"],
            name=row["name"],
            tier=row["tier"],
            sectors=_json_list(row["sectors"]),
            directory_url=row["directory_url"],
            discovery_method=row["discovery_method"],
            node_count_actual=row["node_count_actual"],
            last_refreshed=row["last_refreshed"],
        )


class DecisionRepo(_Repo):
    """One row per candidate the precision layer judged — kept or dropped.

    The predecessor's filtering became folklore because drops left no trace.
    This is the table that answers "why did we never see the GSAE form?" without
    guessing, so a recall problem is a query rather than an archaeology project.
    """

    def record(
        self,
        *,
        decision_id: str,
        run_id: str,
        url: str,
        kept: bool,
        stage: str,
        reason: str,
        decided_at: str,
        org_id: str | None = None,
        combo: str | None = None,
        similarity: float | None = None,
        rerank_score: float | None = None,
        features: str = "{}",
    ) -> None:
        if not kept and not reason:
            raise RepoError(
                f"a dropped candidate needs a reason ({url}); a drop with no reason is "
                "exactly the invisibility this table exists to end"
            )
        self._exec(
            "INSERT INTO precision_decision (decision_id, run_id, url, org_id, kept, stage,"
            " reason, combo, similarity, rerank_score, features, decided_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(run_id, url) DO UPDATE SET"
            "   kept = excluded.kept, stage = excluded.stage, reason = excluded.reason,"
            "   combo = excluded.combo, similarity = excluded.similarity,"
            "   rerank_score = excluded.rerank_score, features = excluded.features,"
            "   decided_at = excluded.decided_at",
            (
                decision_id,
                run_id,
                url,
                org_id,
                1 if kept else 0,
                stage,
                reason,
                combo,
                similarity,
                rerank_score,
                features,
                decided_at,
            ),
        )

    def for_run(self, run_id: str, *, kept: bool | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM precision_decision WHERE run_id = ?"
        params: list[Any] = [run_id]
        if kept is not None:
            sql += " AND kept = ?"
            params.append(1 if kept else 0)
        return self._all(sql + " ORDER BY rerank_score DESC, url", params)

    def why_dropped(self, url: str) -> list[sqlite3.Row]:
        """Every run's verdict on one URL. The question a recall complaint asks."""
        return self._all(
            "SELECT * FROM precision_decision WHERE url = ? ORDER BY decided_at DESC", (url,)
        )

    def drop_reasons(self, run_id: str) -> dict[str, int]:
        rows = self._all(
            "SELECT reason, COUNT(*) c FROM precision_decision WHERE run_id = ? AND kept = 0"
            " GROUP BY reason ORDER BY c DESC, reason",
            (run_id,),
        )
        return {r["reason"]: r["c"] for r in rows}

    def count(self) -> int:
        return self._one("SELECT COUNT(*) c FROM precision_decision")["c"]  # type: ignore[index]


class Store:
    """All repositories over one connection, plus the unit of work.

    Not a service layer — just the handle a worker holds so it does not have to
    construct eight objects.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.networks = NetworkRepo(conn)
        self.organizations = OrganizationRepo(conn)
        self.employers = EmployerRepo(conn)
        self.people = PersonRepo(conn)
        self.routes = RouteRepo(conn)
        self.evidence = EvidenceRepo(conn)
        self.scores = ScoreRepo(conn)
        self.triggers = TriggerRepo(conn)
        self.marks = MarkRepo(conn)
        self.rejections = RejectionRepo(conn)
        self.runs = RunRepo(conn)
        self.stage_runs = StageRunRepo(conn)
        self.costs = CostRepo(conn)
        self.fetch_log = FetchLogRepo(conn)
        self.decisions = DecisionRepo(conn)

    @contextmanager
    def unit_of_work(self) -> Iterator[Store]:
        """Multi-table writes commit together or not at all."""
        with transaction(self.conn):
            yield self
