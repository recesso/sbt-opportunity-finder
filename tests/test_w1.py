"""E3.S1 — W1 NetworkRegistrar.

The acceptance criterion is two things: three networks enumerated to real node
lists, and a second run against an unchanged directory producing zero new
organizations. Both are asserted here, the second with the cache forced off so
it proves idempotent *writes* rather than an idempotent cache.

The rule the rest of this file protects is that ``node_count_est`` never becomes
data. It is a planning figure. The only thing it is allowed to do is set off a
smoke alarm when a directory yields far less than expected, because that means
the extraction broke — and a broken extraction that reports success is how a
whole network silently disappears from the harvest.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from finder.acquire.fetch import Fetcher
from finder.acquire.providers.base import FetchError, Snapshot
from finder.acquire.providers.search import SearchProvider, SearchResult
from finder.acquire.snapshot import SnapshotStore, content_hash
from finder.config import NetworkDef
from finder.context import start_run
from finder.harvest.w1_registry import (
    DEFAULT_DISCOVERY_LIMIT,
    IMPLAUSIBLE_YIELD_RATIO,
    LinkNodeExtractor,
    NetworkRegistrar,
    Node,
    NodeExtractor,
    discovery_queries,
    looks_like_a_node,
    seed_nodes,
    to_nodes,
)
from finder.store.db import open_db, utcnow
from finder.store.ids import rejection_id
from finder.store.models import Network, Rejection
from finder.store.repos import Store

ROOT = Path(__file__).resolve().parents[1]

MEP_DIRECTORY = "https://www.nist.gov/mep/centers"

# Shaped like a real directory page: chrome, social links, a self-link, a
# duplicate member, and the members themselves.
MEP_MARKDOWN = """
# MEP National Network Centers

[Skip to content](https://www.nist.gov/#main)
[MEP home](https://www.nist.gov/mep)

Find the center serving your state.

- [Alabama Technology Network](https://atn.org/)
- [Georgia Manufacturing Extension Partnership](https://gamep.org/about)
- [GaMEP](https://www.gamep.org/)
- [Impact Washington](https://impactwashington.org/centers)
- [Michigan Manufacturing Technology Center](https://www.the-center.org/)
- [Learn more](https://www.nist.gov/mep/about)
- [here](https://example-vendor.com/)
- [—](https://dash-only.org/)

[Follow us on LinkedIn](https://www.linkedin.com/company/nist)
[Privacy Policy](https://www.nist.gov/privacy-policy)
"""


def networks_yaml() -> dict[str, NetworkDef]:
    """The real config, not a toy copy of it."""
    raw = yaml.safe_load((ROOT / "config" / "networks.yaml").read_text(encoding="utf-8"))
    return {n["id"]: NetworkDef(**n) for n in raw["networks"]}


def network(**overrides) -> NetworkDef:
    base = {
        "id": "nist_mep",
        "name": "NIST MEP centers",
        "directory_url": MEP_DIRECTORY,
        "sectors": ["manufacturing"],
        "node_count_est": 5,
        "tier": "A",
    }
    return NetworkDef(**(base | overrides))


NAM_MARKDOWN = """
# State Manufacturers Associations

[About NAM](https://www.nam.org/about/)

- [Georgia Association of Manufacturers](https://georgiamanufacturers.org/)
- [Michigan Manufacturers Association](https://www.mimfg.org/)
- [Texas Association of Manufacturers](https://www.aptexas.org/)
- [South Carolina Manufacturers Alliance](https://myscma.com/)
"""

CSCMP_MARKDOWN = """
# CSCMP Local Roundtables

[CSCMP membership](https://cscmp.org/membership)

- [Atlanta Roundtable](https://cscmpatlanta.org/)
- [Carolinas Roundtable](https://cscmpcarolinas.org/)
- [Chicago Roundtable](https://www.cscmpchicago.org/)
- [Houston Roundtable](https://cscmphouston.org/)
"""

DIRECTORY_PAGES = {
    "https://www.nist.gov/mep/centers": MEP_MARKDOWN,
    "https://www.nam.org/state-associations/": NAM_MARKDOWN,
    "https://cscmp.org/CSCMP/Connect/Local-Roundtables/": CSCMP_MARKDOWN,
}


class FakeFetcher:
    """A Fetcher stand-in serving canned markdown per URL, counting calls.

    Each directory gets its own page. Serving one fixture for several networks
    would make each network's host a *member* of the others, which is the filter
    behaving correctly against a fixture that does not resemble reality.
    """

    def __init__(
        self,
        markdown: str | None = None,
        *,
        pages: dict[str, str] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.markdown = markdown
        self.pages = pages or {}
        self.error = error
        self.calls: list[str] = []

    def fetch(self, url: str, *, max_age_s=None, run=None, cost_usd=None) -> Snapshot:
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        body = self.markdown if self.markdown is not None else self.pages.get(url, MEP_MARKDOWN)
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


def registrar(store: Store, fetcher=None) -> NetworkRegistrar:
    return NetworkRegistrar(store, fetcher or FakeFetcher())


# --- reading a directory ---------------------------------------------------


def test_the_link_extractor_reads_names_and_urls() -> None:
    pairs = LinkNodeExtractor().extract(
        Snapshot(content_hash="x", url="u", canonical_url="u", markdown=MEP_MARKDOWN)
    )
    assert ("Alabama Technology Network", "https://atn.org/") in pairs
    assert isinstance(LinkNodeExtractor(), NodeExtractor)


def test_a_directory_becomes_a_node_list() -> None:
    nodes, parent_links = to_nodes(
        LinkNodeExtractor().extract(
            Snapshot(content_hash="x", url="u", canonical_url="u", markdown=MEP_MARKDOWN)
        ),
        network(),
    )
    assert [n.domain for n in nodes] == [
        "atn.org",
        "gamep.org",
        "impactwashington.org",
        "the-center.org",
    ], "a link whose anchor text is 'here' names no organization"
    assert parent_links == 4, "self-links are counted, not silently dropped"


def test_the_network_does_not_become_its_own_member() -> None:
    """A directory linking to itself is not a member of itself."""
    nodes, parent_links = to_nodes([("MEP home", "https://www.nist.gov/mep")], network())
    assert nodes == []
    assert parent_links == 1


def test_one_member_linked_twice_is_one_node() -> None:
    """Directories link the same member by name and again by logo. The named
    one comes first, which is why first occurrence wins."""
    nodes, _ = to_nodes(
        [
            ("Georgia Manufacturing Extension Partnership", "https://gamep.org/about"),
            ("GaMEP", "https://www.gamep.org/"),
        ],
        network(),
    )
    assert len(nodes) == 1
    assert nodes[0].name == "Georgia Manufacturing Extension Partnership"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/company/nist",
        "https://youtube.com/c/x",
        "https://twitter.com/nist",
    ],
)
def test_social_links_are_not_organizations(url: str) -> None:
    assert looks_like_a_node("Alabama Technology Network", url, parent_domain="nist.gov") is False


def test_a_real_member_link_survives_every_filter() -> None:
    """The filters must not be so eager that they drop actual members."""
    assert (
        looks_like_a_node(
            "Alabama Technology Network", "https://atn.org/centers", parent_domain="nist.gov"
        )
        is True
    )


@pytest.mark.parametrize(
    "anchor",
    [
        "Learn more",
        "here",
        "  Read More  ",
        "Privacy Policy",
        "»",
        "--",
        "AB",
        "2024",
        "12 / 34",
        "»»»",
    ],
)
def test_generic_anchor_text_names_no_organization(anchor: str) -> None:
    """Page numbers and separator glyphs are links too. An organization has a
    name with letters in it."""
    assert looks_like_a_node(anchor, "https://somewhere.org/", parent_domain="nist.gov") is False


def test_a_padded_name_is_trimmed() -> None:
    nodes, _ = to_nodes([("— Georgia Quick Start |", "https://georgiaquickstart.org")], network())
    assert nodes[0].name == "Georgia Quick Start"


@pytest.mark.parametrize("url", ["not-a-url", "https://intranet/members", "https://localhost/x"])
def test_a_host_with_no_dot_is_not_a_domain(url: str) -> None:
    """`https://intranet/members` would otherwise become an organization called
    "intranet" that nothing can ever fetch."""
    nodes, _ = to_nodes([("Somewhere", url)], network())
    assert nodes == []


def test_provenance_records_the_directory_it_came_from() -> None:
    nodes, _ = to_nodes([("Impact Washington", "https://impactwashington.org")], network())
    assert nodes[0].discovered_from == f"directory:{MEP_DIRECTORY}"


# --- writing organizations -------------------------------------------------


def test_registering_writes_organizations_with_network_tier_and_sectors(store: Store) -> None:
    result = registrar(store).register(network())

    assert result.source == "directory"
    assert result.created == 4
    assert result.updated == 0

    org = store.organizations.get_by_domain("gamep.org")
    assert org.name == "Georgia Manufacturing Extension Partnership"
    assert org.network_id == "nist_mep"
    assert org.tier == "A"
    assert org.sectors == ["manufacturing"]
    assert org.discovered_from == f"directory:{MEP_DIRECTORY}"
    assert org.name_normalized


def test_the_network_itself_is_registered_with_its_real_count(store: Store) -> None:
    """Organizations reference the network, and the count written is the one
    counted — never the planning estimate."""
    registrar(store).register(network(node_count_est=51))

    net = store.networks.get("nist_mep")
    assert net.name == "NIST MEP centers"
    assert net.tier == "A"
    assert net.directory_url == MEP_DIRECTORY
    assert net.node_count_actual == 4, "what the directory listed, not the estimate of 51"
    assert net.last_refreshed


def test_a_network_that_yielded_nothing_still_gets_a_row(store: Store) -> None:
    """Zero is a finding. A missing row would read as 'never attempted'."""
    seeded = network(id="empty", directory_url=None, seed_members=[{"name": "X", "domain": None}])
    registrar(store).register(seeded)
    assert store.networks.get("empty").node_count_actual == 0


def test_a_later_pass_that_did_not_count_keeps_the_count(store: Store) -> None:
    """Something other than W1 touching the network row must not blank what W1
    established. A count of None means "did not count", not "counted zero"."""
    store.networks.upsert(
        Network(
            network_id="n",
            name="Original",
            tier="A",
            node_count_actual=51,
            last_refreshed="2026-01-01T00:00:00+00:00",
        )
    )
    store.networks.upsert(Network(network_id="n", name="Renamed", tier="B"))

    net = store.networks.get("n")
    assert net.node_count_actual == 51, "the count was blanked by a pass that did not count"
    assert net.last_refreshed == "2026-01-01T00:00:00+00:00"
    assert (net.name, net.tier) == ("Renamed", "B"), "what WAS supplied still updates"


def test_a_second_run_creates_nothing_new(store: Store) -> None:
    """The acceptance criterion. Cache forced off, so this proves the WRITES are
    idempotent rather than proving the fetch cache works."""
    fetcher = FakeFetcher()
    reg = NetworkRegistrar(store, fetcher)

    first = reg.register(network(), max_age_s=0)
    before = store.organizations.count()

    second = reg.register(network(), max_age_s=0)

    assert len(fetcher.calls) == 2, "the directory really was re-read"
    assert first.created == 4
    assert second.created == 0
    assert second.updated == 4
    assert store.organizations.count() == before


def test_a_directory_that_grew_adds_only_the_new_node(store: Store) -> None:
    reg = NetworkRegistrar(store, FakeFetcher())
    reg.register(network(), max_age_s=0)

    grown = NetworkRegistrar(
        store, FakeFetcher(MEP_MARKDOWN + "\n- [New England MEP](https://nemep.org/)\n")
    )
    result = grown.register(network(), max_age_s=0)

    assert result.created == 1
    assert store.organizations.get_by_domain("nemep.org") is not None


def test_first_seen_survives_a_re_registration(store: Store) -> None:
    """When an organization was first discovered is a fact about history."""
    reg = NetworkRegistrar(store, FakeFetcher())
    reg.register(network(), max_age_s=0)
    first_seen = store.organizations.get_by_domain("gamep.org").first_seen

    reg.register(network(), max_age_s=0)
    assert store.organizations.get_by_domain("gamep.org").first_seen == first_seen


# --- seeded networks -------------------------------------------------------


def test_a_seeded_network_registers_from_config(store: Store) -> None:
    seeded = network(
        id="employer_intermediaries",
        directory_url=None,
        node_count_est=None,
        seed_members=[
            {"name": "GPS Education Partners", "domain": "gpsed.org"},
            {"name": "Enterprise Technology Association", "domain": "joineta.org"},
        ],
    )
    result = registrar(store).register(seeded)

    assert result.source == "seed"
    assert result.created == 2
    assert store.organizations.get_by_domain("joineta.org").discovered_from == (
        "seed:employer_intermediaries"
    )


def test_a_seed_with_no_domain_is_named_not_dropped(store: Store) -> None:
    """'Align Wisconsin, domain unknown' is a research task, not a non-entity.
    Identity is the registrable domain, so it cannot be written — but it must
    not disappear either."""
    seeded = network(
        id="employer_intermediaries",
        directory_url=None,
        node_count_est=None,
        seed_members=[
            {"name": "America Achieves", "domain": "americaachieves.org"},
            {"name": "Align Wisconsin", "domain": None},
        ],
    )

    with start_run(store, "weekly", run_id="r-1") as run:
        result = registrar(store).register(seeded, run=run)

    assert result.created == 1
    assert result.skipped_no_domain == ["Align Wisconsin"]

    not_reached = store.runs.get("r-1").not_reached
    assert not_reached[0]["reason"] == "seed_without_domain"
    assert "Align Wisconsin" in not_reached[0]["detail"]


def test_seed_nodes_resolves_hosts_to_registrable_domains() -> None:
    nodes, unresolved = seed_nodes(
        network(
            directory_url=None,
            seed_members=[{"name": "AIDT", "domain": "www.aidt.edu"}],
        )
    )
    assert nodes == [
        Node(
            name="AIDT",
            domain="aidt.edu",
            network_id="nist_mep",
            url="https://aidt.edu",
            discovered_from="seed:nist_mep",
        )
    ]
    assert unresolved == []


# --- the planning estimate is not data -------------------------------------


def test_a_directory_yielding_far_less_than_expected_says_so(store: Store) -> None:
    """Three nodes against an estimate of fifty-one means the extraction broke.
    Reporting success there is how a whole network silently disappears."""
    thin = FakeFetcher("[Alabama Technology Network](https://atn.org/)")

    with start_run(store, "weekly", run_id="r-1") as run:
        result = NetworkRegistrar(store, thin).register(network(node_count_est=51), run=run)

    assert result.created == 1, "what was found is still kept"
    not_reached = store.runs.get("r-1").not_reached
    assert not_reached[0]["reason"] == "implausible_yield"
    assert not_reached[0]["count"] == 50


def test_a_plausible_yield_is_not_flagged(store: Store) -> None:
    with start_run(store, "weekly", run_id="r-1") as run:
        NetworkRegistrar(store, FakeFetcher()).register(network(node_count_est=5), run=run)
    assert store.runs.get("r-1").not_reached == []


def test_a_network_with_no_estimate_is_never_flagged(store: Store) -> None:
    with start_run(store, "weekly", run_id="r-1") as run:
        NetworkRegistrar(store, FakeFetcher()).register(network(node_count_est=None), run=run)
    assert store.runs.get("r-1").not_reached == []


def test_the_estimate_is_never_written_to_an_organization(store: Store) -> None:
    """It is a planning figure. Nothing downstream may mistake it for a count."""
    registrar(store).register(network(node_count_est=51))
    for domain in ("atn.org", "gamep.org", "impactwashington.org", "the-center.org"):
        assert store.organizations.get_by_domain(domain).employer_reach_est is None


def test_the_ratio_is_a_documented_constant() -> None:
    assert 0 < IMPLAUSIBLE_YIELD_RATIO < 1


# --- failure and reporting -------------------------------------------------


def test_an_unreachable_directory_is_reported_not_silent(store: Store) -> None:
    broken = FakeFetcher(error=FetchError("503 unavailable", url=MEP_DIRECTORY))

    with start_run(store, "weekly", run_id="r-1") as run:
        result = NetworkRegistrar(store, broken).register(network(), run=run)

    assert result.nodes == []
    assert store.organizations.count() == 0
    assert store.runs.get("r-1").not_reached[0]["reason"] == "directory_unreachable"


def test_a_network_with_no_enumeration_path_says_which_one(store: Store) -> None:
    """`ga_adjacent_chambers` is enumerated by AMS host patterns, which W1 does
    not implement. It must not look like a network with no members."""
    chambers = network(
        id="ga_adjacent_chambers",
        directory_url=None,
        node_count_est=200,
        discovery_method="ams_host_patterns",
    )

    with start_run(store, "weekly", run_id="r-1") as run:
        result = NetworkRegistrar(store, FakeFetcher()).register(chambers, run=run)

    assert result.source == "none"
    detail = store.runs.get("r-1").not_reached[0]
    assert detail["reason"] == "no_enumeration_path"
    assert "ams_host_patterns" in detail["detail"]


def test_created_organizations_are_counted_on_the_run(store: Store) -> None:
    with start_run(store, "weekly", run_id="r-1") as run:
        registrar(store).register(network(), run=run)
    assert store.runs.get("r-1").counters["orgs_mapped"] == 4


def test_registering_without_a_run_works(store: Store) -> None:
    assert registrar(store).register(network()).created == 4


# --- many networks ---------------------------------------------------------


def test_one_broken_directory_does_not_end_the_harvest(store: Store) -> None:
    """The isolation guarantee, applied where it matters most: a single bad
    directory must not cost the other fourteen networks."""

    class SelectivelyBroken(FakeFetcher):
        def fetch(self, url, *, max_age_s=None, run=None, cost_usd=None):
            if "broken" in url:
                raise FetchError("500", url=url)
            return super().fetch(url, max_age_s=max_age_s, run=run, cost_usd=cost_usd)

    nets = [
        network(id="a", directory_url="https://a.example/dir"),
        network(id="broken", directory_url="https://broken.example/dir"),
        network(id="c", directory_url="https://c.example/dir"),
    ]
    pages = {
        "https://a.example/dir": NAM_MARKDOWN,
        "https://c.example/dir": CSCMP_MARKDOWN,
    }

    with start_run(store, "weekly", run_id="r-1") as run:
        results = NetworkRegistrar(store, SelectivelyBroken(pages=pages)).register_all(
            nets, run=run
        )

    assert results["a"].actual >= 4 and results["a"].created == results["a"].actual
    assert results["broken"].nodes == [], "the broken directory yielded nothing"
    assert results["c"].actual >= 4
    assert run.stage_summary("register") == {"done": 3}, (
        "the failed network is still a completed item; it failed loudly, not silently"
    )


def test_register_all_works_without_a_run(store: Store) -> None:
    results = registrar(store).register_all([network(id="a"), network(id="b")])
    assert set(results) == {"a", "b"}


def test_a_resumed_harvest_does_not_redo_finished_networks(store: Store) -> None:
    fetcher = FakeFetcher()
    reg = NetworkRegistrar(store, fetcher)
    nets = [network(id="a"), network(id="b")]

    with start_run(store, "weekly", run_id="r-1") as run:
        reg.register_all(nets[:1], run=run)
    calls_after_first = len(fetcher.calls)

    from finder.context import resume_run

    with resume_run(store, "r-1") as run:
        results = reg.register_all(nets, run=run, max_age_s=0)

    assert "a" not in results, "network a was already done"
    assert "b" in results
    assert len(fetcher.calls) == calls_after_first + 1


# --- against the real config -----------------------------------------------


def test_every_configured_network_has_a_path_w1_can_take() -> None:
    """Config validation already enforces this; the point here is that W1's
    three branches actually cover what the config declares."""
    for net in networks_yaml().values():
        assert net.directory_url or net.seed_members or net.discovery_method, net.id


def test_the_three_tier_a_directories_enumerate(store: Store) -> None:
    """Three networks to real node lists, per the acceptance criterion. The
    markup is a fixture; what is being tested is that the real config entries
    drive the real code path end to end."""
    configured = networks_yaml()
    reg = NetworkRegistrar(store, FakeFetcher(pages=DIRECTORY_PAGES))

    counts = {}
    for network_id in ("nist_mep", "state_mfg_assns", "cscmp_roundtables"):
        result = reg.register(configured[network_id], max_age_s=0)
        assert result.source == "directory"
        assert result.actual >= 4, f"{network_id} enumerated {result.actual}"
        assert all(n.domain and n.network_id == network_id for n in result.nodes)
        counts[network_id] = result.actual
        assert store.networks.get(network_id).node_count_actual == result.actual

    assert store.organizations.count() == sum(counts.values())
    assert store.networks.count() == 3
    assert [n.network_id for n in store.networks.all()] == [
        "cscmp_roundtables",
        "nist_mep",
        "state_mfg_assns",
    ], "the registry is enumerable, and in a stable order"


def test_the_seeded_networks_in_the_real_config_register(store: Store) -> None:
    configured = networks_yaml()
    reg = NetworkRegistrar(store, FakeFetcher())

    with start_run(store, "weekly", run_id="r-1") as run:
        training = reg.register(configured["state_training_systems"], run=run)
        intermediaries = reg.register(configured["employer_intermediaries"], run=run)

    assert training.source == "seed"
    assert training.skipped_no_domain == ["NCEdge"]
    assert intermediaries.skipped_no_domain == ["Align Wisconsin"]
    assert store.organizations.get_by_domain("gpsed.org") is not None

    reasons = [n["reason"] for n in store.runs.get("r-1").not_reached]
    assert reasons == ["seed_without_domain", "seed_without_domain"]


def test_the_fetcher_protocol_is_the_real_one(store: Store, tmp_path: Path) -> None:
    """FakeFetcher stands in for Fetcher everywhere above. This is the one test
    that wires the real class in, so the fake cannot drift from it."""
    from tests.test_fetch import FakeProvider, make_snapshot

    real = Fetcher(
        FakeProvider(make_snapshot(MEP_MARKDOWN, "https://www.nist.gov/mep/centers")),
        store,
        SnapshotStore(tmp_path / "snapshots"),
    )
    assert NetworkRegistrar(store, real).register(network()).created == 4


# --- semantic discovery (step 5) -------------------------------------------

THESIS = """
  An organization that holds direct relationships with many employers and can
  reach them without an event.
"""


class FakeSearch:
    """A search provider with no transport."""

    name = "fake-search"

    def __init__(self, *results: SearchResult, error: Exception | None = None) -> None:
        self.results = list(results)
        self.error = error
        self.queries: list[tuple[str, int]] = []

    def search(self, query, *, limit=25, include_domains=None):
        self.queries.append((query, limit))
        if self.error is not None:
            raise self.error
        return [replace(r, query=query) for r in self.results]


def found(url: str, title: str) -> SearchResult:
    return SearchResult(url=url, title=title, query="", provider="fake-search")


def test_the_fake_search_matches_the_protocol() -> None:
    assert isinstance(FakeSearch(), SearchProvider)


def test_a_query_is_built_per_sector_carrying_the_thesis() -> None:
    """The thesis is what makes this a search for a kind of organization rather
    than a keyword hunt."""
    queries = discovery_queries(THESIS, ["manufacturing", "supply_chain"])

    assert len(queries) == 2
    assert queries[0].startswith("manufacturing: ")
    assert queries[1].startswith("supply chain: "), "underscores are not words"
    assert "holds direct relationships with many employers" in queries[0]
    assert "\n" not in queries[0], "the thesis is condensed to one line"


def test_an_empty_thesis_is_refused() -> None:
    with pytest.raises(ValueError, match="needs thesis text"):
        discovery_queries("   ", ["manufacturing"])


def test_a_blank_sector_produces_no_query() -> None:
    assert discovery_queries(THESIS, ["manufacturing", "", "  "]) == discovery_queries(
        THESIS, ["manufacturing"]
    )


def test_discovery_registers_organizations_no_directory_lists(store: Store) -> None:
    search = FakeSearch(
        found("https://myscma.com/", "South Carolina Manufacturers Alliance"),
        found("https://gpsed.org/about", "GPS Education Partners"),
    )

    result = registrar(store).discover(THESIS, ["manufacturing"], search=search)

    assert result.created == 2
    assert result.found == 2
    org = store.organizations.get_by_domain("myscma.com")
    assert org.name == "South Carolina Manufacturers Alliance"
    assert org.tier == "C", "an unaffiliated find is not a tier A network node"
    assert org.discovered_from.startswith("search:manufacturing: ")


def test_a_discovered_organization_belongs_to_no_network(store: Store) -> None:
    """It carries no network_id, because it belongs to no network — which is
    exactly why a directory could never have produced it."""
    registrar(store).discover(
        THESIS, ["manufacturing"], search=FakeSearch(found("https://myscma.com/", "SCMA"))
    )
    assert store.organizations.get_by_domain("myscma.com").network_id is None
    assert store.networks.count() == 0


def test_an_organization_already_registered_is_counted_not_rewritten(store: Store) -> None:
    """A discovery pass that finds nothing new because everything was already
    registered is a GOOD outcome, and must not look like a failed pass."""
    reg = registrar(store)
    reg.register(network())
    before = store.organizations.get_by_domain("gamep.org")

    result = reg.discover(
        THESIS, ["manufacturing"], search=FakeSearch(found("https://gamep.org/x", "GaMEP again"))
    )

    assert (result.created, result.already_known, result.found) == (0, 1, 0)
    after = store.organizations.get_by_domain("gamep.org")
    assert (after.name, after.network_id, after.tier) == (before.name, "nist_mep", "A")


def test_a_permanently_rejected_organization_stays_rejected(store: Store) -> None:
    """Search liking an organization does not overturn the founder's decision."""
    store.rejections.add(
        Rejection(
            rejection_id=rejection_id("", "myscma.com", "ALL"),
            created_at=utcnow(),
            match_name=None,
            match_domain="myscma.com",
            family_scope="ALL",
            scope="organization",
            reason="permanently rejected",
        )
    )

    result = registrar(store).discover(
        THESIS, ["manufacturing"], search=FakeSearch(found("https://myscma.com/", "SCMA"))
    )

    assert (result.created, result.rejected) == (0, 1)
    assert store.organizations.get_by_domain("myscma.com") is None


def test_the_same_domain_across_two_sectors_is_registered_once(store: Store) -> None:
    search = FakeSearch(found("https://myscma.com/", "SCMA"))
    result = registrar(store).discover(THESIS, ["manufacturing", "supply_chain"], search=search)

    assert len(search.queries) == 2, "both sectors were searched"
    assert result.created == 1
    assert result.found == 1
    assert result.already_known == 0, (
        "already_known means 'was in the registry before this pass', not "
        "'we wrote it two lines ago' — conflating them would report every "
        "discovery as half-redundant"
    )


def test_search_junk_is_not_registered(store: Store) -> None:
    search = FakeSearch(
        found("https://www.linkedin.com/company/x", "Some Association"),
        found("https://myscma.com/", "Learn more"),
        found("https://intranet/x", "Internal"),
        found("https://myscma.com/real", "South Carolina Manufacturers Alliance"),
    )
    result = registrar(store).discover(THESIS, ["manufacturing"], search=search)

    assert result.created == 1
    assert store.organizations.get_by_domain("myscma.com") is not None


def test_a_failed_query_is_reported_and_the_rest_continue(store: Store) -> None:
    class OneBadSector(FakeSearch):
        def search(self, query, *, limit=25, include_domains=None):
            self.queries.append((query, limit))
            if query.startswith("supply chain"):
                raise FetchError("429 rate limited")
            return [replace(r, query=query) for r in self.results]

    search = OneBadSector(found("https://myscma.com/", "SCMA"))

    # The failing sector goes FIRST on purpose. With it last, abandoning the loop
    # and continuing past it look identical, and the test proves nothing.
    with start_run(store, "weekly", run_id="r-1") as run:
        result = registrar(store).discover(
            THESIS, ["supply_chain", "manufacturing"], search=search, run=run
        )

    assert result.created == 1, "the sector AFTER the failure still produced its find"
    assert len(search.queries) == 2, "discovery stopped at the first failure"
    not_reached = store.runs.get("r-1").not_reached
    assert not_reached[0]["reason"] == "discovery_failed"
    assert "supply chain" in not_reached[0]["detail"]


def test_discovery_is_charged_and_counted(store: Store) -> None:
    search = FakeSearch(found("https://myscma.com/", "SCMA"))

    with start_run(store, "weekly", run_id="r-1") as run:
        registrar(store).discover(THESIS, ["manufacturing", "health"], search=search, run=run)

    assert store.costs.by_provider("r-1") == {"fake-search": 0.0}
    assert store.runs.get("r-1").counters["orgs_mapped"] == 1


def test_the_result_limit_reaches_the_provider(store: Store) -> None:
    search = FakeSearch()
    registrar(store).discover(THESIS, ["manufacturing"], search=search, limit=7)
    assert search.queries == [(search.queries[0][0], 7)]


def test_the_default_limit_is_a_ceiling_per_query_not_a_verdict() -> None:
    assert DEFAULT_DISCOVERY_LIMIT > 0


def test_discovery_without_a_run_works(store: Store) -> None:
    result = registrar(store).discover(
        THESIS, ["manufacturing"], search=FakeSearch(found("https://myscma.com/", "SCMA"))
    )
    assert result.created == 1


def test_a_discovery_pass_that_finds_nothing_is_not_an_error(store: Store) -> None:
    with start_run(store, "weekly", run_id="r-1") as run:
        result = registrar(store).discover(THESIS, ["manufacturing"], search=FakeSearch(), run=run)

    assert result.as_dict() == {
        "queries": 1,
        "found": 0,
        "created": 0,
        "already_known": 0,
        "rejected": 0,
    }
    assert store.runs.get("r-1").not_reached == []


def test_discovery_uses_the_real_thesis_text(store: Store) -> None:
    """Against config/thesis.yaml, not a paraphrase of it."""
    thesis = yaml.safe_load((ROOT / "config" / "thesis.yaml").read_text(encoding="utf-8"))
    search = FakeSearch()

    registrar(store).discover(thesis["thesis"]["CHANNEL"], ["fintech"], search=search)

    query = search.queries[0][0]
    assert query.startswith("fintech: ")
    assert "reach them without an event" in query
