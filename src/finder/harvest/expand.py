"""E3.S8 — graph expansion from partner, sponsor and member lists.

Pages the system has already fetched name other organizations. Following those
edges finds bodies no query would have produced — the training system that
appears only on a manufacturer's partner page, the intermediary listed only as a
funder — and it is how the registry gets smarter instead of re-running the same
searches forever.

The governing principle from the spec applies with full force here: *indirect
evidence of ACCESS beats direct evidence of EXISTENCE*. An organization that
appears on somebody's "approved providers" page has been vouched for by a body
that works with employers. No amount of reading its own website establishes
that.

Three properties this module is built around:

* **Every edge carries its span.** The anchor text that named the organization
  is written to ``evidence`` alongside the page it came from. An organization
  with no span supporting its discovery is indistinguishable from one somebody
  typed in, which is the failure this whole system exists to prevent.
* **Depth is bounded and cycles cannot happen.** Partner pages link back. A
  visited set plus a hard depth cap is the difference between an expansion and
  a run that never ends.
* **Breadth-first, so depth 1 is complete before depth 2 starts.** A budget that
  runs out mid-expansion should cost the furthest hops, not a random half of the
  nearest ones.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from finder.acquire.fetch import Fetcher
from finder.acquire.map import MapHit, UrlInventory
from finder.acquire.providers.base import FetchError
from finder.context import RunContext
from finder.harvest.w1_registry import (
    LinkNodeExtractor,
    NodeExtractor,
    looks_like_a_node,
)
from finder.store import ids
from finder.store.db import utcnow
from finder.store.keys import normalize_org, registrable_domain
from finder.store.models import Evidence, Organization
from finder.store.repos import Store

# The spec's number. Depth 3 is a crawl of the open web wearing a different name.
MAX_DEPTH = 2

# Pages worth reading per organization. An organization with forty matching
# pages has a site map, not forty partner lists.
DEFAULT_PAGES_PER_ORG = 5

EXTRACTOR = "w-expand/links"
FIELD_NAME = "named_on_partner_page"


@dataclass(frozen=True, slots=True)
class Edge:
    """One organization naming another, and the text that named it."""

    from_domain: str
    to_domain: str
    name: str
    depth: int
    source_url: str
    span: str
    matched_term: str = ""


@dataclass(slots=True)
class ExpansionResult:
    """What the traversal found, and how far it actually got."""

    edges: list[Edge] = field(default_factory=list)
    created: int = 0
    already_known: int = 0
    rejected: int = 0
    pages_read: int = 0
    visited: set[str] = field(default_factory=set)
    depth_reached: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "edges": len(self.edges),
            "created": self.created,
            "already_known": self.already_known,
            "rejected": self.rejected,
            "pages_read": self.pages_read,
            "visited": len(self.visited),
            "depth_reached": self.depth_reached,
        }

    def at_depth(self, depth: int) -> list[Edge]:
        return [e for e in self.edges if e.depth == depth]


class GraphExpander:
    """Walk partner and member pages outward from organizations already known."""

    def __init__(
        self,
        store: Store,
        fetcher: Fetcher,
        inventory: UrlInventory,
        *,
        partner_paths: Sequence[str],
        extractor: NodeExtractor | None = None,
        pages_per_org: int = DEFAULT_PAGES_PER_ORG,
    ) -> None:
        self.store = store
        self.fetcher = fetcher
        self.inventory = inventory
        self.partner_paths = list(partner_paths)
        self.extractor = extractor or LinkNodeExtractor()
        self.pages_per_org = pages_per_org

    def expand(
        self,
        seeds: Sequence[str],
        *,
        max_depth: int = MAX_DEPTH,
        run: RunContext | None = None,
        max_age_s: int | None = None,
    ) -> ExpansionResult:
        """Breadth-first from ``seeds``, at most ``max_depth`` hops out."""
        if max_depth < 1:
            raise ValueError("max_depth must be at least 1")
        if max_depth > MAX_DEPTH:
            raise ValueError(
                f"max_depth {max_depth} exceeds the cap of {MAX_DEPTH}; beyond two hops "
                "this is a crawl of the open web wearing a different name"
            )

        result = ExpansionResult()
        frontier = [registrable_domain(s) or s for s in seeds]
        result.visited.update(frontier)

        for depth in range(1, max_depth + 1):
            if not frontier:
                break
            result.depth_reached = depth
            next_frontier: list[str] = []
            for domain in frontier:
                for edge in self._edges_from(domain, depth, result, run, max_age_s):
                    result.edges.append(edge)
                    if edge.to_domain in result.visited:
                        continue
                    result.visited.add(edge.to_domain)
                    if self._register(edge, result, run):
                        next_frontier.append(edge.to_domain)
            frontier = next_frontier

        if run is not None:
            if result.created:
                run.count("orgs_mapped", result.created)
            run.log.info("graph_expanded", **result.as_dict())
        return result

    # --- one organization -------------------------------------------------

    def _edges_from(
        self,
        domain: str,
        depth: int,
        result: ExpansionResult,
        run: RunContext | None,
        max_age_s: int | None,
    ) -> list[Edge]:
        """Partner-shaped pages on one domain, and who they name."""
        hits = self.inventory.map(domain, self.partner_paths, run=run)[: self.pages_per_org]
        edges: list[Edge] = []
        for hit in hits:
            page = self._read(hit, domain, run, max_age_s)
            if page is None:
                continue
            result.pages_read += 1
            edges.extend(self._edges_on_page(page, hit, domain, depth))
        return edges

    def _read(
        self, hit: MapHit, domain: str, run: RunContext | None, max_age_s: int | None
    ) -> object | None:
        try:
            return self.fetcher.fetch(hit.url, max_age_s=max_age_s, run=run)
        except FetchError as exc:
            if run is not None:
                run.record_not_reached("partner_page_unreachable", f"{domain} {hit.url}: {exc}")
            return None

    def _edges_on_page(self, page, hit: MapHit, from_domain: str, depth: int) -> list[Edge]:
        seen_here: set[str] = set()
        edges: list[Edge] = []
        for raw_name, url in self.extractor.extract(page):
            to_domain = registrable_domain(url)
            if to_domain == from_domain:
                continue  # a site linking to itself is not an edge
            if not looks_like_a_node(raw_name, url, parent_domain=from_domain):
                continue
            if to_domain in seen_here:
                continue
            seen_here.add(to_domain)
            edges.append(
                Edge(
                    from_domain=from_domain,
                    to_domain=to_domain,
                    name=" ".join(raw_name.split()),
                    depth=depth,
                    source_url=page.url,
                    span=" ".join(raw_name.split()),
                    matched_term=hit.matched_term,
                )
            )
        return edges

    # --- writing ----------------------------------------------------------

    def _register(self, edge: Edge, result: ExpansionResult, run: RunContext | None) -> bool:
        """Write the organization and the evidence for it. False if not written.

        Returning False keeps the domain out of the next frontier, so a rejected
        or already-known organization is not re-walked.
        """
        if self.store.organizations.get_by_domain(edge.to_domain) is not None:
            result.already_known += 1
            self._write_evidence(edge)
            return False

        if self.store.rejections.blocks(
            name_normalized=normalize_org(edge.name), domain=edge.to_domain, family="CHANNEL"
        ):
            result.rejected += 1
            return False

        org_id = ids.org_id(edge.to_domain)
        self.store.organizations.upsert(
            Organization(
                org_id=org_id,
                canonical_domain=edge.to_domain,
                name=edge.name,
                name_normalized=normalize_org(edge.name),
                first_seen=utcnow(),
                network_id=None,
                tier="C",
                discovered_from=f"partner:{edge.source_url}",
            )
        )
        self._write_evidence(edge)
        result.created += 1
        return True

    def _write_evidence(self, edge: Edge) -> None:
        """The span that named this organization, on the page that named it.

        Written for already-known organizations too: "GaMEP lists this body as an
        approved provider" is a fact about GaMEP worth having even when the body
        was already in the registry.
        """
        org_id = ids.org_id(edge.to_domain)
        self.store.evidence.add(
            Evidence(
                ev_id=ids.evidence_id(org_id, FIELD_NAME, edge.source_url),
                field_name=FIELD_NAME,
                source_url=edge.source_url,
                content_hash="",
                extractor=EXTRACTOR,
                fetched_at=utcnow(),
                org_id=org_id,
                value=edge.from_domain,
                span_text=edge.span,
                span_match="exact",
            )
        )
