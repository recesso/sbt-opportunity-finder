"""W2 RouteMapper — candidate URLs from one map call per organization.

No crawl. Validated 2026-09-01: a single map of gamep.org surfaced the statewide
lunch-and-learn series and the service pages together.

Deviation from the backlog's step 2, stated rather than buried: it says
``map(domain, PROGRAMMING_PATHS)``, and this matches PROGRAMMING_PATHS **and**
PARTNER_PATHS against the same inventory. The map CALL is the cost; matching is
local and free. Restricting the terms would buy nothing and lose exactly the
service and provider pages the acceptance criterion asks for — which is where
the GaMEP CHANNEL route lives, the family the founder named as the goal. Each
candidate carries the term that matched it, so a ROOM-shaped hit and a
CHANNEL-shaped one stay distinguishable downstream.

**This produces candidates, not routes.** A candidate is "this URL is worth
reading"; a route is "here is how you get in", and only extraction can say that.
Keeping the two apart is what stops a page about a past event from becoming an
opportunity, which is precisely what the predecessor did.

Two things worth stating about the tiering:

* **Tier is derived from evidence, not declared.** An organization is tier A
  because the founder marked a route there PURSUE, or because a route there
  scored FIT ≥ 65, or because it is a member of a tier A network. Nothing else
  promotes it. The rules live in ``config/sources.yaml``; the arithmetic lives
  here.
* **A founder verdict outranks a score.** If he said PURSUE, the organization is
  tier A whatever the numbers say. His judgment is the ground truth the scores
  are trying to approximate, not the other way round.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from finder.acquire.map import MapHit, UrlInventory
from finder.context import RunContext
from finder.store.db import utcnow
from finder.store.models import Organization
from finder.store.repos import Store

# config/sources.yaml tiering: A weekly, B biweekly, C monthly.
CADENCE_DAYS: dict[str, int] = {"A": 7, "B": 14, "C": 30}
TIERS: tuple[str, ...] = ("A", "B", "C")

# FIT thresholds from config/sources.yaml. Tier A is the BEST list's bar, on
# purpose: an organization that has produced a BEST route is worth a weekly look.
TIER_A_FIT = 65
TIER_B_FIT = 50

# Verdicts that mean "keep looking at this one".
PURSUE_VERDICTS = frozenset({"PURSUE", "PURSUED", "ATTEND", "SUBMIT", "SUBMITTED"})


@dataclass(frozen=True, slots=True)
class Candidate:
    """A URL worth reading, and why it was thought worth reading."""

    org_id: str
    domain: str
    url: str
    matched_term: str
    matched_in: str
    title: str | None = None
    source: str = ""

    @classmethod
    def from_hit(cls, org: Organization, hit: MapHit) -> Candidate:
        return cls(
            org_id=org.org_id,
            domain=org.canonical_domain,
            url=hit.url,
            matched_term=hit.matched_term,
            matched_in=hit.matched_in,
            title=hit.title,
            source=hit.source,
        )


@dataclass(slots=True)
class MappingResult:
    """What one W2 pass produced. Read ``unmappable`` before believing the count."""

    candidates: list[Candidate] = field(default_factory=list)
    organizations: int = 0
    duplicates: int = 0
    # Two different findings, deliberately kept apart: a domain that mapped and
    # matched nothing, and a domain nobody could map at all.
    no_candidates: list[str] = field(default_factory=list)
    unmappable: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "organizations": self.organizations,
            "candidates": len(self.candidates),
            "duplicates": self.duplicates,
            "no_candidates": len(self.no_candidates),
            "unmappable": len(self.unmappable),
        }

    def for_organization(self, org_id: str) -> list[Candidate]:
        return [c for c in self.candidates if c.org_id == org_id]


def tier_for(*, best_fit: int | None, verdicts: Sequence[str], network_tier: str | None) -> str:
    """The tier an organization has earned.

    Order matters and is not arbitrary. A founder verdict comes first because
    his judgment is the ground truth the scores approximate. A network's tier
    comes last because belonging to a strong network is a prior, not a result —
    it should not hold an organization at A once its own routes have scored
    badly.
    """
    if any(v.strip().upper() in PURSUE_VERDICTS for v in verdicts):
        return "A"
    if best_fit is not None:
        if best_fit >= TIER_A_FIT:
            return "A"
        return "B" if best_fit >= TIER_B_FIT else "C"
    return network_tier if network_tier in TIERS else "C"


def is_due(last_mapped: str | None, tier: str, *, now: str | None = None) -> bool:
    """Never mapped is always due. Otherwise the tier's cadence decides."""
    if not last_mapped:
        return True
    days = CADENCE_DAYS.get(tier, CADENCE_DAYS["C"])
    cutoff = datetime.fromisoformat(now or utcnow()) - timedelta(days=days)
    return datetime.fromisoformat(last_mapped) <= cutoff


class RouteMapper:
    """Map due organizations and emit candidate URLs."""

    def __init__(
        self,
        store: Store,
        inventory: UrlInventory,
        *,
        programming_paths: Sequence[str],
        partner_paths: Sequence[str] = (),
        limit_per_org: int = 40,
    ) -> None:
        self.store = store
        self.inventory = inventory
        # Ordered, deduplicated: `match_term` breaks ties on position, and
        # PROGRAMMING_PATHS is written most-important-first.
        self.terms = list(dict.fromkeys([*programming_paths, *partner_paths]))
        self.limit_per_org = limit_per_org

    # --- tiering ----------------------------------------------------------

    def retier(self, org: Organization) -> str:
        """Recompute an organization's tier from what is now known about it."""
        network = self.store.networks.get(org.network_id) if org.network_id else None
        return tier_for(
            best_fit=self.store.scores.best_fit_for_organization(org.org_id),
            verdicts=self.store.marks.verdicts_for_organization(org.org_id),
            network_tier=network.tier if network else None,
        )

    def retier_all(self) -> dict[str, str]:
        """Apply the earned tier to every organization. Returns what changed."""
        changed: dict[str, str] = {}
        for org in self.store.organizations.all():
            earned = self.retier(org)
            if earned != org.tier:
                self.store.organizations.set_tier(org.org_id, earned)
                changed[org.org_id] = earned
        return changed

    def due(self, *, now: str | None = None, tiers: Sequence[str] = TIERS) -> list[Organization]:
        """Organizations whose cadence has come round, most overdue first.

        Never-mapped organizations sort first: a body nobody has ever looked at
        is a bigger gap than one looked at eight days ago.
        """
        when = now or utcnow()
        out: list[Organization] = []
        for tier in tiers:
            cutoff = (
                datetime.fromisoformat(when) - timedelta(days=CADENCE_DAYS.get(tier, 30))
            ).isoformat()
            out.extend(self.store.organizations.due_for_mapping(tier, cutoff))
        return out

    # --- mapping ----------------------------------------------------------

    def map_organizations(
        self,
        organizations: Sequence[Organization],
        *,
        run: RunContext | None = None,
    ) -> MappingResult:
        """One map call each, checkpointed, deduplicated across the whole pass."""
        result = MappingResult()
        seen: set[str] = set()

        for org in organizations:
            if run is None:
                self._map_one(org, result, seen, run)
                continue
            with run.item("map_routes", org.org_id) as claimed:
                if not claimed:
                    continue
                self._map_one(org, result, seen, run)

        if run is not None:
            run.count("orgs_mapped", result.organizations)
            run.count("candidates", len(result.candidates))
            run.log.info("routes_mapped", **result.as_dict())
        return result

    def run_due(self, *, run: RunContext | None = None, now: str | None = None) -> MappingResult:
        """The weekly entry point: retier, select what is due, map it."""
        self.retier_all()
        return self.map_organizations(self.due(now=now), run=run)

    def _map_one(
        self,
        org: Organization,
        result: MappingResult,
        seen: set[str],
        run: RunContext | None,
    ) -> None:
        outcome = self.inventory.map_detailed(
            org.canonical_domain, self.terms, limit=self.limit_per_org, run=run
        )
        result.organizations += 1

        if not outcome.mapped:
            # NOT marked mapped: nobody looked at this domain, so it stays due.
            result.unmappable.append(org.canonical_domain)
            return

        # Marked mapped even when nothing matched. "Looked at, found nothing" is
        # an answer, and treating it as never-looked-at re-maps the same barren
        # domain every single run.
        self.store.organizations.mark_mapped(org.org_id)

        if not outcome.hits:
            result.no_candidates.append(org.canonical_domain)
            return

        for hit in outcome.hits:
            if hit.url in seen:
                result.duplicates += 1
                continue
            seen.add(hit.url)
            result.candidates.append(Candidate.from_hit(org, hit))
