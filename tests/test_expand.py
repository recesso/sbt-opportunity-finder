"""E3.S8 — graph expansion from partner, sponsor and member lists.

The acceptance criterion: a fixture partner page yields new organizations with
correct provenance, and the traversal terminates at depth 2.

The property worth stating plainly is why this exists at all. *Indirect evidence
of ACCESS beats direct evidence of EXISTENCE.* An organization on somebody's
approved-providers page has been vouched for by a body that works with
employers; no amount of reading its own website establishes that. So the span
that named it is not decoration — it is the evidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from finder.acquire.map import DEFAULT_LIMIT, UrlInventory
from finder.acquire.providers.base import FetchError, Snapshot
from finder.acquire.snapshot import content_hash
from finder.context import start_run
from finder.harvest.expand import (
    MAX_DEPTH,
    Edge,
    ExpansionResult,
    GraphExpander,
)
from finder.store.db import open_db, utcnow
from finder.store.ids import org_id, rejection_id
from finder.store.models import Organization, Rejection
from finder.store.repos import Store

ROOT = Path(__file__).resolve().parents[1]

GAMEP_PARTNERS = """
# Approved Providers

GaMEP works with these approved third-party providers.

- [Georgia Quick Start](https://georgiaquickstart.org/about)
- [Technical College System of Georgia](https://tcsg.edu/)
- [Learn more](https://gamep.org/about)
- [Follow us](https://www.linkedin.com/company/gamep)
"""

QUICKSTART_PARTNERS = """
# Our Partners

- [Georgia Association of Manufacturers](https://georgiamanufacturers.org/)
- [Savannah Economic Development Authority](https://seda.org/)
"""

# Depth 3 would come from here. It must never be read.
GAM_PARTNERS = """
# Members

- [Never Reached Industries](https://never-reached.example/)
"""

PAGES = {
    "https://gamep.org/partners": GAMEP_PARTNERS,
    "https://georgiaquickstart.org/partners": QUICKSTART_PARTNERS,
    "https://georgiamanufacturers.org/partners": GAM_PARTNERS,
}


def partner_paths() -> list[str]:
    return yaml.safe_load((ROOT / "config" / "paths.yaml").read_text(encoding="utf-8"))[
        "PARTNER_PATHS"
    ]


class FakeMapProvider:
    """Returns one partner page per domain, or none."""

    name = "fake-map"

    def __init__(self, per_domain: dict[str, list[str]] | None = None) -> None:
        self.per_domain = per_domain if per_domain is not None else _default_map()
        self.calls: list[str] = []

    def map(self, domain: str, *, limit: int = DEFAULT_LIMIT):
        self.calls.append(domain)
        return [(u, None) for u in self.per_domain.get(domain, [])]


def _default_map() -> dict[str, list[str]]:
    return {
        "gamep.org": ["https://gamep.org/partners"],
        "georgiaquickstart.org": ["https://georgiaquickstart.org/partners"],
        "georgiamanufacturers.org": ["https://georgiamanufacturers.org/partners"],
    }


class FakeFetcher:
    def __init__(self, pages: dict[str, str] | None = None, *, broken: set[str] | None = None):
        self.pages = pages if pages is not None else dict(PAGES)
        self.broken = broken or set()
        self.calls: list[str] = []

    def fetch(self, url: str, *, max_age_s=None, run=None, cost_usd=None) -> Snapshot:
        self.calls.append(url)
        if url in self.broken:
            raise FetchError(f"503 for {url}", url=url)
        body = self.pages.get(url, "")
        return Snapshot(
            content_hash=content_hash(body),
            url=url,
            canonical_url=url,
            markdown=body,
            fetched_at=utcnow(),
            provider="fake",
        )


@pytest.fixture
def store() -> Store:
    return Store(open_db(":memory:"))


def expander(store: Store, *, fetcher=None, mapper=None, **kw) -> GraphExpander:
    return GraphExpander(
        store,
        fetcher or FakeFetcher(),
        UrlInventory(mapper or FakeMapProvider()),
        partner_paths=partner_paths(),
        **kw,
    )


def seed(store: Store, domain: str = "gamep.org", name: str = "GaMEP") -> None:
    store.organizations.upsert(
        Organization(
            org_id=org_id(domain),
            canonical_domain=domain,
            name=name,
            name_normalized=name.lower(),
            first_seen=utcnow(),
            tier="A",
        )
    )


# --- the acceptance criterion ----------------------------------------------


def test_a_partner_page_yields_new_organizations(store: Store) -> None:
    seed(store)
    result = expander(store).expand(["gamep.org"], max_depth=1)

    assert {e.to_domain for e in result.edges} == {"georgiaquickstart.org", "tcsg.edu"}
    assert result.created == 2

    org = store.organizations.get_by_domain("georgiaquickstart.org")
    assert org.name == "Georgia Quick Start"
    assert org.discovered_from == "partner:https://gamep.org/partners"
    assert org.network_id is None
    assert org.tier == "C", "being named on a partner page is not a network tier"


def test_the_span_that_named_the_organization_is_written(store: Store) -> None:
    """Indirect evidence of ACCESS is the whole point. An organization with no
    span supporting its discovery is indistinguishable from one somebody typed
    in — the exact failure this system exists to prevent."""
    seed(store)
    expander(store).expand(["gamep.org"], max_depth=1)

    rows = store.evidence.for_organization(org_id("georgiaquickstart.org"))
    assert len(rows) == 1
    ev = rows[0]
    assert ev.field_name == "named_on_partner_page"
    assert ev.span_text == "Georgia Quick Start"
    assert ev.source_url == "https://gamep.org/partners"
    assert ev.value == "gamep.org", "the evidence records WHO vouched for it"
    assert ev.span_match == "exact"


def test_traversal_terminates_at_depth_two(store: Store) -> None:
    """The acceptance criterion's second half. Depth 3 exists in the fixtures
    precisely so its absence proves something."""
    seed(store)
    fetcher = FakeFetcher()
    result = expander(store, fetcher=fetcher).expand(["gamep.org"], max_depth=2)

    assert result.depth_reached == 2
    assert {e.to_domain for e in result.at_depth(1)} == {"georgiaquickstart.org", "tcsg.edu"}
    assert {e.to_domain for e in result.at_depth(2)} == {
        "georgiamanufacturers.org",
        "seda.org",
    }
    assert not any(e.depth > 2 for e in result.edges)

    # The depth-3 page exists in the fixtures and names an organization nothing
    # else does. Never fetching it is what proves the cap holds.
    assert "https://georgiamanufacturers.org/partners" not in fetcher.calls
    assert store.organizations.get_by_domain("never-reached.example") is None


def test_depth_three_is_refused_outright(store: Store) -> None:
    """Beyond two hops this is a crawl of the open web wearing a different name."""
    with pytest.raises(ValueError, match="exceeds the cap"):
        expander(store).expand(["gamep.org"], max_depth=3)


def test_depth_zero_is_a_programming_error(store: Store) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        expander(store).expand(["gamep.org"], max_depth=0)


def test_the_cap_is_the_documented_constant() -> None:
    assert MAX_DEPTH == 2


# --- cycles ----------------------------------------------------------------


def test_a_cycle_does_not_loop_forever(store: Store) -> None:
    """Partner pages link back. Without a visited set this never terminates."""
    seed(store, "a.org", "A")
    pages = {
        "https://a.org/partners": "- [B Org](https://b.org/)",
        "https://b.org/partners": "- [A Org](https://a.org/)",
    }
    mapper = FakeMapProvider(
        {"a.org": ["https://a.org/partners"], "b.org": ["https://b.org/partners"]}
    )
    fetcher = FakeFetcher(pages)

    result = expander(store, fetcher=fetcher, mapper=mapper).expand(["a.org"], max_depth=2)

    assert result.created == 1
    assert fetcher.calls.count("https://a.org/partners") == 1
    assert "a.org" in result.visited, "the seed is visited before the walk starts"
    assert result.already_known == 0, (
        "B's page links back to A, but A is where we STARTED. Counting the seed as "
        "an organization the expansion rediscovered would overstate the overlap and "
        "understate what the hop actually produced"
    )


def test_a_self_link_is_not_an_edge(store: Store) -> None:
    seed(store)
    pages = {"https://gamep.org/partners": "- [GaMEP home](https://gamep.org/about)"}
    result = expander(store, fetcher=FakeFetcher(pages)).expand(["gamep.org"], max_depth=1)
    assert result.edges == []


def test_the_same_organization_named_twice_on_a_page_is_one_edge(store: Store) -> None:
    seed(store)
    pages = {
        "https://gamep.org/partners": (
            "- [Georgia Quick Start](https://georgiaquickstart.org/a)\n"
            "- [Quick Start](https://georgiaquickstart.org/b)\n"
        )
    }
    result = expander(store, fetcher=FakeFetcher(pages)).expand(["gamep.org"], max_depth=1)
    assert len(result.edges) == 1
    assert result.edges[0].name == "Georgia Quick Start", "the fuller name comes first"


# --- what is not an organization -------------------------------------------


def test_chrome_and_generic_links_are_not_organizations(store: Store) -> None:
    seed(store)
    result = expander(store).expand(["gamep.org"], max_depth=1)
    domains = {e.to_domain for e in result.edges}
    assert "linkedin.com" not in domains
    assert not any(e.name == "Learn more" for e in result.edges)


def test_an_organization_already_known_is_not_re_registered(store: Store) -> None:
    seed(store)
    seed(store, "georgiaquickstart.org", "Georgia Quick Start")
    before = store.organizations.get_by_domain("georgiaquickstart.org")

    result = expander(store).expand(["gamep.org"], max_depth=1)

    assert result.already_known == 1
    after = store.organizations.get_by_domain("georgiaquickstart.org")
    assert (after.tier, after.first_seen) == (before.tier, before.first_seen)


def test_evidence_is_recorded_even_for_an_organization_already_known(store: Store) -> None:
    """'GaMEP lists this body as an approved provider' is a fact about GaMEP,
    worth having whether or not the body was already in the registry."""
    seed(store)
    seed(store, "georgiaquickstart.org", "Georgia Quick Start")

    expander(store).expand(["gamep.org"], max_depth=1)

    rows = store.evidence.for_organization(org_id("georgiaquickstart.org"))
    assert [r.value for r in rows] == ["gamep.org"]


def test_a_permanently_rejected_organization_is_not_registered(store: Store) -> None:
    seed(store)
    store.rejections.add(
        Rejection(
            rejection_id=rejection_id("", "tcsg.edu", "ALL"),
            created_at=utcnow(),
            match_name=None,
            match_domain="tcsg.edu",
            family_scope="ALL",
            scope="organization",
            reason="permanently rejected",
        )
    )

    result = expander(store).expand(["gamep.org"], max_depth=1)

    assert result.rejected == 1
    assert store.organizations.get_by_domain("tcsg.edu") is None


def test_a_rejected_organization_is_not_walked_further(store: Store) -> None:
    """It must not seed the next hop either, or a rejection buys nothing."""
    seed(store, "a.org", "A")
    store.rejections.add(
        Rejection(
            rejection_id=rejection_id("", "b.org", "ALL"),
            created_at=utcnow(),
            match_name=None,
            match_domain="b.org",
            family_scope="ALL",
            scope="organization",
            reason="rejected",
        )
    )
    pages = {
        "https://a.org/partners": "- [B Org](https://b.org/)",
        "https://b.org/partners": "- [C Org](https://c.org/)",
    }
    mapper = FakeMapProvider(
        {"a.org": ["https://a.org/partners"], "b.org": ["https://b.org/partners"]}
    )
    fetcher = FakeFetcher(pages)

    result = expander(store, fetcher=fetcher, mapper=mapper).expand(["a.org"], max_depth=2)

    assert result.created == 0
    assert "https://b.org/partners" not in fetcher.calls
    assert store.organizations.get_by_domain("c.org") is None


# --- failure and reporting -------------------------------------------------


def test_an_unreachable_partner_page_is_reported_not_silent(store: Store) -> None:
    seed(store)
    fetcher = FakeFetcher(broken={"https://gamep.org/partners"})

    with start_run(store, "weekly", run_id="r-1") as run:
        result = expander(store, fetcher=fetcher).expand(["gamep.org"], max_depth=1, run=run)

    assert result.edges == []
    assert result.pages_read == 0
    detail = store.runs.get("r-1").not_reached[0]
    assert detail["reason"] == "partner_page_unreachable"
    assert "gamep.org" in detail["detail"]


def test_an_unreachable_page_without_a_run_is_still_survivable(store: Store) -> None:
    """One-off scripts and the eval harness have no run to report to. The page
    is still skipped rather than taking down the expansion."""
    seed(store)
    fetcher = FakeFetcher(broken={"https://gamep.org/partners"})

    result = expander(store, fetcher=fetcher).expand(["gamep.org"], max_depth=1)

    assert result.edges == []
    assert result.created == 0


def test_a_domain_with_no_partner_pages_is_not_a_failure(store: Store) -> None:
    """Plenty of organizations simply have no partner page. That is an answer."""
    seed(store, "quiet.org", "Quiet Org")

    with start_run(store, "weekly", run_id="r-1") as run:
        result = expander(store, mapper=FakeMapProvider({})).expand(
            ["quiet.org"], max_depth=2, run=run
        )

    assert result.edges == []
    assert store.runs.get("r-1").not_reached == []


def test_expansion_counts_created_organizations_on_the_run(store: Store) -> None:
    seed(store)
    with start_run(store, "weekly", run_id="r-1") as run:
        expander(store).expand(["gamep.org"], max_depth=2, run=run)
    assert store.runs.get("r-1").counters["orgs_mapped"] == 4


def test_expansion_without_a_run_works(store: Store) -> None:
    seed(store)
    assert expander(store).expand(["gamep.org"], max_depth=1).created == 2


def test_pages_per_organization_is_bounded(store: Store) -> None:
    """Forty matching pages on one domain is a site map, not forty partner lists."""
    seed(store)
    many = [f"https://gamep.org/partners-{i}" for i in range(20)]
    mapper = FakeMapProvider({"gamep.org": many})
    fetcher = FakeFetcher(dict.fromkeys(many, GAMEP_PARTNERS))

    expander(store, fetcher=fetcher, mapper=mapper, pages_per_org=3).expand(
        ["gamep.org"], max_depth=1
    )

    assert len(fetcher.calls) == 3


def test_a_seed_given_as_a_url_is_resolved_to_a_domain(store: Store) -> None:
    seed(store)
    result = expander(store).expand(["https://www.gamep.org/about"], max_depth=1)
    assert result.created == 2


def test_the_result_summarises_itself(store: Store) -> None:
    seed(store)
    summary = expander(store).expand(["gamep.org"], max_depth=2).as_dict()
    assert summary["created"] == 4
    assert summary["depth_reached"] == 2
    assert summary["pages_read"] == 2, (
        "gamep.org and georgiaquickstart.org only: tcsg.edu has no partner page, and "
        "the depth-2 finds are registered but never walked"
    )
    assert summary["edges"] == 4


def test_an_empty_result_reports_zero_not_nothing() -> None:
    assert ExpansionResult().as_dict()["depth_reached"] == 0


def test_edges_carry_the_term_that_found_the_page(store: Store) -> None:
    """Which partner-path term matched flows through, the same signal MapHit
    carries, so a page found by 'approved provider' outranks one found by
    'blog' downstream."""
    seed(store)
    result = expander(store).expand(["gamep.org"], max_depth=1)
    assert all(isinstance(e, Edge) and e.matched_term for e in result.edges)
    assert {e.matched_term for e in result.edges} == {"partners"}
