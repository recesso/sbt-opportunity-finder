"""W1 NetworkRegistrar — the recall backbone.

Each network in ``config/networks.yaml`` is a template that replicates across N
nodes. Solve the programming pattern once at one node and the same extraction
applies to every peer. Getting the node list right is therefore the highest-
leverage thing in the harvest: a network enumerated to forty real organizations
is forty domains to map; the same network guessed at is nothing.

**``node_count_est`` is never used as data.** It is an order-of-magnitude
planning figure, and the config says so. What this module writes is what the
network's own directory actually lists. The estimate is used for exactly one
thing — noticing when a directory yields far fewer nodes than expected, which
almost always means the extraction failed rather than that the network shrank.
That is recorded as ``not_reached``, not swallowed.

Node extraction sits behind a Protocol. The shipped implementation is
deterministic: a directory page lists its nodes as links whose anchor text is
the node's name, and reading those is exact, free and replayable. An LLM
extractor can be dropped in behind the same Protocol when a directory needs one.

**Not in this module, deliberately:**

* *Semantic discovery* of organizations outside every network (step 5) needs a
  search provider that does not exist yet. Nothing here pretends to do it.
* *Graph expansion* from partner and member lists is E3.S8, which is a story of
  its own.

Both are named in ``CONTINUE_HERE.md`` rather than left to be discovered.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from finder.acquire.fetch import Fetcher
from finder.acquire.providers.base import FetchError, Snapshot
from finder.config import NetworkDef
from finder.context import RunContext
from finder.store import ids
from finder.store.db import utcnow
from finder.store.keys import normalize_org, registrable_domain
from finder.store.models import Network, Organization
from finder.store.repos import Store

# A directory that yields less than this share of its planning estimate has
# almost certainly been mis-parsed. Kept low deliberately: the estimates are
# order-of-magnitude, so this fires on failure, not on inaccuracy.
IMPLAUSIBLE_YIELD_RATIO = 0.25

# Anchor text that names no organization.
_GENERIC_ANCHORS = frozenset(
    {
        "here",
        "click here",
        "read more",
        "more",
        "learn more",
        "details",
        "website",
        "visit",
        "visit website",
        "link",
        "home",
        "back",
        "next",
        "previous",
        "contact",
        "contact us",
        "email",
        "map",
        "directions",
        "apply",
        "join",
        "login",
        "log in",
        "sign in",
        "register",
        "search",
        "menu",
        "skip to content",
        "privacy policy",
        "terms of use",
        "terms",
        "cookie policy",
        "accessibility",
        "sitemap",
        "careers",
        "donate",
        "subscribe",
        "newsletter",
    }
)

# Hosts that are never a node of anything.
_CHROME_DOMAINS = frozenset(
    {
        "facebook.com",
        "twitter.com",
        "x.com",
        "linkedin.com",
        "instagram.com",
        "youtube.com",
        "tiktok.com",
        "flickr.com",
        "google.com",
        "goo.gl",
        "bit.ly",
        "apple.com",
        "microsoft.com",
        "adobe.com",
        "wordpress.org",
        "usa.gov",
        "whitehouse.gov",
    }
)

_MD_LINK = re.compile(r"\[([^\]\n]{1,200})\]\((https?://[^\s)]+)\)")
_MIN_NAME_LEN = 3

# Directory listings pad names with dashes, pipes and dots. The en and em dashes
# are deliberate — real pages use them, and stripping only ASCII hyphens would
# leave "— Georgia Quick Start" with its dash attached.
_NAME_EDGE_CHARS = " .-–—|"


@dataclass(frozen=True, slots=True)
class Node:
    """One organization belonging to a network, and where it came from."""

    name: str
    domain: str
    network_id: str
    url: str = ""
    discovered_from: str = ""


@dataclass(slots=True)
class RegistrationResult:
    """What one network actually produced. Read the skipped lists, not just the
    counts — a network that 'succeeded' with two nodes did not succeed."""

    network_id: str
    source: str  # "directory" | "seed" | "none"
    nodes: list[Node] = field(default_factory=list)
    created: int = 0
    updated: int = 0
    skipped_parent: int = 0
    skipped_no_domain: list[str] = field(default_factory=list)
    estimate: int | None = None

    @property
    def actual(self) -> int:
        return len(self.nodes)

    def as_dict(self) -> dict[str, object]:
        return {
            "network_id": self.network_id,
            "source": self.source,
            "found": self.actual,
            "created": self.created,
            "updated": self.updated,
            "skipped_parent": self.skipped_parent,
            "skipped_no_domain": len(self.skipped_no_domain),
            "estimate": self.estimate,
        }


@runtime_checkable
class NodeExtractor(Protocol):
    """Turns a directory snapshot into ``(name, url)`` pairs."""

    name: str

    def extract(self, snapshot: Snapshot) -> list[tuple[str, str]]: ...


class LinkNodeExtractor:
    """Read the directory's links. Exact, free, and replayable.

    A network directory lists its nodes as links whose anchor text is the node's
    name — that is what a directory *is*. Reading them beats asking a model to
    re-read what the markup already states unambiguously, and it cannot invent a
    member that is not on the page.
    """

    name = "links"

    def extract(self, snapshot: Snapshot) -> list[tuple[str, str]]:
        return [(text.strip(), url.strip()) for text, url in _MD_LINK.findall(snapshot.markdown)]


def looks_like_a_node(name: str, url: str, *, parent_domain: str) -> bool:
    """Is this link plausibly an organization rather than page furniture?

    Deliberately permissive about *what* an organization is and strict about
    what is obviously not one. A wrongly dropped node is a silently missing
    branch of the whole harvest; a wrongly kept one costs a single map call and
    is filtered downstream.
    """
    cleaned = " ".join(name.split()).strip(_NAME_EDGE_CHARS)
    if len(cleaned) < _MIN_NAME_LEN or cleaned.lower() in _GENERIC_ANCHORS:
        return False
    if not any(ch.isalpha() for ch in cleaned):
        return False
    domain = registrable_domain(url)
    # A label with no dot is not a domain. `https://intranet/members` resolves to
    # "intranet", which would otherwise become an organization.
    if not domain or "." not in domain or domain in _CHROME_DOMAINS:
        return False
    # The directory linking to itself is not a member of itself.
    return domain != parent_domain


def to_nodes(pairs: Iterable[tuple[str, str]], network: NetworkDef) -> tuple[list[Node], int]:
    """Filter, resolve and deduplicate. Returns the nodes and the parent-link count.

    Deduplicated by registrable domain, first occurrence winning: directories
    routinely link the same member twice, once by name and once by logo, and the
    named one comes first.
    """
    parent = registrable_domain(network.directory_url or "")
    nodes: list[Node] = []
    seen: set[str] = set()
    parent_links = 0

    for raw_name, url in pairs:
        domain = registrable_domain(url)
        if domain and domain == parent:
            parent_links += 1
            continue
        if not looks_like_a_node(raw_name, url, parent_domain=parent):
            continue
        if domain in seen:
            continue
        seen.add(domain)
        nodes.append(
            Node(
                name=" ".join(raw_name.split()).strip(_NAME_EDGE_CHARS),
                domain=domain,
                network_id=network.id,
                url=url,
                discovered_from=f"directory:{network.directory_url}",
            )
        )
    return nodes, parent_links


def seed_nodes(network: NetworkDef) -> tuple[list[Node], list[str]]:
    """Nodes from ``seed_members``, and the names of seeds with no domain.

    A seed with no domain cannot become an organization row — identity is the
    registrable domain. Those names are returned rather than dropped, because
    "Align Wisconsin, domain unknown" is a research task, not a non-entity.
    """
    nodes: list[Node] = []
    unresolved: list[str] = []
    for seed in network.seed_members or []:
        domain = registrable_domain(seed.domain or "")
        if not domain:
            unresolved.append(seed.name)
            continue
        nodes.append(
            Node(
                name=seed.name,
                domain=domain,
                network_id=network.id,
                url=f"https://{domain}",
                discovered_from=f"seed:{network.id}",
            )
        )
    return nodes, unresolved


class NetworkRegistrar:
    """Enumerate networks into organization rows."""

    def __init__(
        self,
        store: Store,
        fetcher: Fetcher,
        *,
        extractor: NodeExtractor | None = None,
    ) -> None:
        self.store = store
        self.fetcher = fetcher
        self.extractor = extractor or LinkNodeExtractor()

    def register(
        self,
        network: NetworkDef,
        *,
        run: RunContext | None = None,
        max_age_s: int | None = None,
    ) -> RegistrationResult:
        """Enumerate one network and write its nodes."""
        result = RegistrationResult(
            network_id=network.id, source="none", estimate=network.node_count_est
        )

        if network.directory_url:
            result.source = "directory"
            try:
                snapshot = self.fetcher.fetch(network.directory_url, max_age_s=max_age_s, run=run)
            except FetchError as exc:
                self._not_reached(run, "directory_unreachable", f"{network.id}: {exc}")
                return result
            nodes, result.skipped_parent = to_nodes(self.extractor.extract(snapshot), network)
            result.nodes = nodes
        elif network.seed_members:
            result.source = "seed"
            result.nodes, result.skipped_no_domain = seed_nodes(network)
            if result.skipped_no_domain:
                self._not_reached(
                    run,
                    "seed_without_domain",
                    f"{network.id}: no domain for "
                    + ", ".join(result.skipped_no_domain)
                    + " — cannot be registered until someone finds it",
                    count=len(result.skipped_no_domain),
                )
        else:
            self._not_reached(
                run,
                "no_enumeration_path",
                f"{network.id}: declares discovery_method="
                f"{network.discovery_method!r}, which W1 does not implement",
            )
            return result

        self._write(result, network, run)
        self._check_yield(result, network, run)

        if run is not None:
            run.log.info("network_registered", **result.as_dict())
        return result

    def register_all(
        self,
        networks: Sequence[NetworkDef],
        *,
        run: RunContext | None = None,
        max_age_s: int | None = None,
    ) -> dict[str, RegistrationResult]:
        """Every network, each isolated: one bad directory cannot end the run."""
        results: dict[str, RegistrationResult] = {}
        for network in networks:
            if run is None:
                results[network.id] = self.register(network, max_age_s=max_age_s)
                continue
            with run.item("register", network.id) as claimed:
                if not claimed:
                    continue
                results[network.id] = self.register(network, run=run, max_age_s=max_age_s)
        return results

    # --- internals --------------------------------------------------------

    def _write(
        self, result: RegistrationResult, network: NetworkDef, run: RunContext | None
    ) -> None:
        # The network row first: organizations reference it. node_count_actual is
        # what was counted here, never networks.yaml's planning estimate — the
        # DDL reserves this column for exactly that distinction.
        self.store.networks.upsert(
            Network(
                network_id=network.id,
                name=network.name,
                tier=network.tier,
                sectors=list(network.sectors),
                directory_url=network.directory_url,
                discovery_method=network.discovery_method,
                node_count_actual=result.actual,
                last_refreshed=utcnow(),
            )
        )
        for node in result.nodes:
            existed = self.store.organizations.get_by_domain(node.domain) is not None
            self.store.organizations.upsert(
                Organization(
                    org_id=ids.org_id(node.domain),
                    canonical_domain=node.domain,
                    name=node.name,
                    name_normalized=normalize_org(node.name),
                    first_seen=utcnow(),
                    network_id=network.id,
                    sectors=list(network.sectors),
                    tier=network.tier,
                    discovered_from=node.discovered_from,
                )
            )
            if existed:
                result.updated += 1
            else:
                result.created += 1
        if run is not None and result.created:
            run.count("orgs_mapped", result.created)

    def _check_yield(
        self, result: RegistrationResult, network: NetworkDef, run: RunContext | None
    ) -> None:
        """A directory yielding three of an estimated fifty did not find three.

        The estimate is never written anywhere. It is used here and only here,
        as a smoke alarm on the extraction.
        """
        estimate = network.node_count_est
        if not estimate or result.source != "directory":
            return
        if result.actual >= estimate * IMPLAUSIBLE_YIELD_RATIO:
            return
        self._not_reached(
            run,
            "implausible_yield",
            f"{network.id}: directory yielded {result.actual} nodes against a planning "
            f"estimate of {estimate}. Extraction probably failed; the nodes found were "
            "kept, the rest were never seen.",
            count=max(0, estimate - result.actual),
        )

    @staticmethod
    def _not_reached(run: RunContext | None, reason: str, detail: str, count: int = 1) -> None:
        if run is not None:
            run.record_not_reached(reason, detail, count)
