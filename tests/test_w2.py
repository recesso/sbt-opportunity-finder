"""E3.S2 — W2 RouteMapper.

Acceptance: gamep.org returns the events series and the service pages in one
call, and re-running mid-run does not re-map completed organizations.

The distinction this file exists to protect is candidate vs route. A candidate
is "this URL is worth reading". A route is "here is how you get in", and only
extraction can say that. The predecessor collapsed the two, which is how a page
about a past event became an opportunity.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from finder.acquire.map import DEFAULT_LIMIT, UrlInventory
from finder.acquire.providers.base import FetchError
from finder.context import resume_run, start_run
from finder.harvest.w2_routes import (
    CADENCE_DAYS,
    PURSUE_VERDICTS,
    TIER_A_FIT,
    TIER_B_FIT,
    Candidate,
    RouteMapper,
    is_due,
    tier_for,
)
from finder.store.db import open_db, utcnow
from finder.store.ids import mark_id, org_id, route_id, score_id
from finder.store.models import FounderMark, Network, Organization, Route, Score
from finder.store.repos import Store

ROOT = Path(__file__).resolve().parents[1]

# The real gamep.org inventory shape: a statewide event series AND the service
# pages, which is the thing one map call was validated to surface together.
GAMEP_INVENTORY = [
    "https://gamep.org/",
    "https://gamep.org/about",
    "https://gamep.org/events/",
    "https://gamep.org/events/lunch-and-learn-series",
    "https://gamep.org/events/your-workforce-as-a-competitive-advantage",
    "https://gamep.org/services/",
    "https://gamep.org/services/workforce-development",
    "https://gamep.org/call-for-speakers",
    "https://gamep.org/about/committees",
    "https://gamep.org/get-involved",
    "https://gamep.org/news/2025-annual-report.pdf",
    "https://gamep.org/logo.png",
]


def paths() -> dict[str, list[str]]:
    return yaml.safe_load((ROOT / "config" / "paths.yaml").read_text(encoding="utf-8"))


class FakeMapProvider:
    name = "fake-map"

    def __init__(self, per_domain: dict[str, list[str]] | None = None, *, broken=()) -> None:
        self.per_domain = per_domain or {"gamep.org": GAMEP_INVENTORY}
        self.broken = set(broken)
        self.calls: list[str] = []

    def map(self, domain: str, *, limit: int = DEFAULT_LIMIT):
        self.calls.append(domain)
        if domain in self.broken:
            raise FetchError(f"503 mapping {domain}")
        return [(u, None) for u in self.per_domain.get(domain, [])]


@pytest.fixture
def store() -> Store:
    return Store(open_db(":memory:"))


def mapper(store: Store, provider=None, **kw) -> RouteMapper:
    return RouteMapper(
        store,
        UrlInventory(provider or FakeMapProvider()),
        programming_paths=paths()["PROGRAMMING_PATHS"],
        partner_paths=paths()["PARTNER_PATHS"],
        **kw,
    )


def org(
    store: Store,
    domain: str = "gamep.org",
    *,
    tier: str = "A",
    last_mapped: str | None = None,
    network_id: str | None = None,
) -> Organization:
    return store.organizations.upsert(
        Organization(
            org_id=org_id(domain),
            canonical_domain=domain,
            name=domain,
            name_normalized=domain,
            first_seen=utcnow(),
            tier=tier,
            last_mapped=last_mapped,
            network_id=network_id,
        )
    )


def route_at(store: Store, organization: Organization, key: str = "k1") -> Route:
    return store.routes.upsert(
        Route(
            route_id=route_id(key),
            family="ROOM",
            org_id=organization.org_id,
            mechanism_name="Lunch and learn",
            route_type="PARTNER_DELIVERY",
            series_key=key,
            created_at=utcnow(),
        )
    )


def days_ago(n: int) -> str:
    return (datetime.now(UTC) - timedelta(days=n)).isoformat()


# --- the acceptance criterion ----------------------------------------------


def test_one_map_call_returns_the_events_series_and_the_service_pages(store: Store) -> None:
    """The validated behaviour: both in a single call, not two passes."""
    o = org(store)
    provider = FakeMapProvider()

    result = mapper(store, provider).map_organizations([o])

    urls = {c.url for c in result.candidates}
    assert "https://gamep.org/events/lunch-and-learn-series" in urls, "the events series"
    assert "https://gamep.org/services/workforce-development" in urls, "the service pages"
    assert "https://gamep.org/call-for-speakers" in urls
    assert provider.calls == ["gamep.org"], "one map call per organization, no crawl"


def test_every_candidate_records_why_it_was_kept(store: Store) -> None:
    result = mapper(store).map_organizations([org(store)])
    speakers = next(c for c in result.candidates if c.url.endswith("call-for-speakers"))

    assert speakers.matched_term == "call for speakers"
    assert speakers.matched_in == "path"
    assert speakers.org_id == org_id("gamep.org")
    assert speakers.domain == "gamep.org"
    assert speakers.source == "fake-map"


def test_pages_matching_nothing_are_not_candidates(store: Store) -> None:
    result = mapper(store).map_organizations([org(store)])
    urls = {c.url for c in result.candidates}
    assert "https://gamep.org/about" not in urls
    assert "https://gamep.org/logo.png" not in urls, "assets are never candidates"


def test_a_past_event_page_is_still_only_a_candidate(store: Store) -> None:
    """W2 emits candidates, never routes. Whether a page is a live opportunity
    or a write-up of one that happened is extraction's call, and collapsing the
    two is exactly how the predecessor produced a dead GaMEP event as a way in."""
    result = mapper(store).map_organizations([org(store)])

    assert store.routes.count() == 0
    assert all(isinstance(c, Candidate) for c in result.candidates)


# --- checkpointing: the second half of the acceptance criterion -------------


def test_a_resumed_run_does_not_re_map_completed_organizations(store: Store) -> None:
    a, b = org(store, "gamep.org"), org(store, "scmep.org")
    provider = FakeMapProvider({"gamep.org": GAMEP_INVENTORY, "scmep.org": GAMEP_INVENTORY})
    w2 = mapper(store, provider)

    with start_run(store, "weekly", run_id="r-1") as run:
        w2.map_organizations([a], run=run)
    assert provider.calls == ["gamep.org"]

    with resume_run(store, "r-1") as run:
        result = w2.map_organizations([a, b], run=run)

    assert provider.calls == ["gamep.org", "scmep.org"], "gamep was not mapped twice"
    assert result.organizations == 1, "only the unfinished organization was worked"


def test_each_organization_is_its_own_checkpoint(store: Store) -> None:
    orgs = [org(store, f"o{i}.org") for i in range(3)]
    provider = FakeMapProvider(dict.fromkeys([f"o{i}.org" for i in range(3)], GAMEP_INVENTORY))

    with start_run(store, "weekly", run_id="r-1") as run:
        mapper(store, provider).map_organizations(orgs, run=run)
        assert run.stage_summary("map_routes") == {"done": 3}


def test_mapping_without_a_run_works(store: Store) -> None:
    assert mapper(store).map_organizations([org(store)]).organizations == 1


# --- deduplication ---------------------------------------------------------


def test_a_url_offered_by_two_organizations_is_one_candidate(store: Store) -> None:
    """Two chambers on the same AMS host can surface the same page. Reading it
    twice costs two fetches and produces two candidate routes for one page."""
    a, b = org(store, "a.org"), org(store, "b.org")
    shared = ["https://ams.example/events/committees"]
    provider = FakeMapProvider({"a.org": shared, "b.org": shared})

    result = mapper(store, provider).map_organizations([a, b])

    assert len(result.candidates) == 1
    assert result.duplicates == 1


def test_deduplication_is_per_pass_not_forever(store: Store) -> None:
    """Next week the same page is a candidate again — it may have changed."""
    o = org(store)
    w2 = mapper(store)
    first = w2.map_organizations([o])
    second = w2.map_organizations([o])
    assert len(second.candidates) == len(first.candidates)


# --- tiering ---------------------------------------------------------------


def test_a_founder_pursue_outranks_everything(store: Store) -> None:
    """His judgment is the ground truth the scores are approximating."""
    assert tier_for(best_fit=10, verdicts=["PURSUE"], network_tier="C") == "A"


@pytest.mark.parametrize("verdict", sorted(PURSUE_VERDICTS))
def test_every_pursue_verdict_promotes(verdict: str) -> None:
    assert tier_for(best_fit=None, verdicts=[verdict.lower()], network_tier="C") == "A"


def test_an_unrelated_verdict_does_not_promote() -> None:
    assert tier_for(best_fit=None, verdicts=["SKIP", "NOT NOW"], network_tier="C") == "C"


@pytest.mark.parametrize(
    ("fit", "expected"),
    [(100, "A"), (TIER_A_FIT, "A"), (TIER_A_FIT - 1, "B"), (TIER_B_FIT, "B"), (49, "C"), (0, "C")],
)
def test_tier_follows_the_configured_fit_bands(fit: int, expected: str) -> None:
    assert tier_for(best_fit=fit, verdicts=[], network_tier=None) == expected


def test_a_score_outranks_the_network_prior() -> None:
    """Belonging to a strong network is a prior, not a result. It must not hold
    an organization at tier A once its own routes have scored badly."""
    assert tier_for(best_fit=20, verdicts=[], network_tier="A") == "C"


def test_the_network_tier_is_used_only_when_nothing_is_scored() -> None:
    assert tier_for(best_fit=None, verdicts=[], network_tier="A") == "A"
    assert tier_for(best_fit=None, verdicts=[], network_tier=None) == "C"
    assert tier_for(best_fit=None, verdicts=[], network_tier="nonsense") == "C"


def test_retier_reads_the_best_route_not_the_average(store: Store) -> None:
    """One strong route is a reason to look weekly; averaging it against four
    weak ones buries it."""
    o = org(store, tier="C")
    for i, fit in enumerate((20, 80, 30)):
        r = route_at(store, o, f"k{i}")
        store.scores.add(
            Score(
                score_id=score_id(r.route_id, "cfg", utcnow()),
                route_id=r.route_id,
                scored_at=utcnow(),
                config_hash="cfg",
                fit=fit,
                route_score=50,
                confidence=80,
                components={},
            )
        )
    assert mapper(store).retier(o) == "A"


def test_retier_uses_the_latest_score_for_each_route(store: Store) -> None:
    """A route that scored 90 last month and 20 today is a 20."""
    o = org(store, tier="A")
    r = route_at(store, o)
    for fit, when in ((90, days_ago(30)), (20, utcnow())):
        store.scores.add(
            Score(
                score_id=score_id(r.route_id, "cfg", when),
                route_id=r.route_id,
                scored_at=when,
                config_hash="cfg",
                fit=fit,
                route_score=50,
                confidence=80,
                components={},
            )
        )
    assert mapper(store).retier(o) == "C"


def test_a_founder_mark_at_an_organization_promotes_it(store: Store) -> None:
    o = org(store, tier="C")
    r = route_at(store, o)
    store.marks.ingest(
        FounderMark(
            mark_id=mark_id(r.route_id, "2026-09-01T00:00:00+00:00"),
            route_id=r.route_id,
            marked_at="2026-09-01T00:00:00+00:00",
            verdict="PURSUE",
        )
    )
    assert mapper(store).retier(o) == "A"


def test_retier_all_writes_only_what_changed(store: Store) -> None:
    store.networks.upsert(Network(network_id="n", name="N", tier="A"))
    promoted = org(store, "a.org", tier="C", network_id="n")
    unchanged = org(store, "b.org", tier="C")

    changed = mapper(store).retier_all()

    assert changed == {promoted.org_id: "A"}
    assert store.organizations.get_by_domain("a.org").tier == "A"
    assert store.organizations.get_by_domain("b.org").tier == "C"


# --- cadence ---------------------------------------------------------------


def test_the_cadences_match_the_configured_tiering() -> None:
    tiering = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text(encoding="utf-8"))[
        "tiering"
    ]
    assert tiering["A"]["cadence"] == "weekly" and CADENCE_DAYS["A"] == 7
    assert tiering["B"]["cadence"] == "biweekly" and CADENCE_DAYS["B"] == 14
    assert tiering["C"]["cadence"] == "monthly" and CADENCE_DAYS["C"] == 30


def test_never_mapped_is_always_due() -> None:
    assert is_due(None, "C") is True


@pytest.mark.parametrize(
    ("tier", "age", "due"),
    [("A", 8, True), ("A", 3, False), ("B", 15, True), ("B", 9, False), ("C", 31, True)],
)
def test_the_cadence_decides_when_a_tier_comes_round(tier: str, age: int, due: bool) -> None:
    assert is_due(days_ago(age), tier) is due


def test_an_unknown_tier_falls_back_to_the_slowest_cadence() -> None:
    """Failing to the slowest cadence spends nothing on a tier nobody defined."""
    assert is_due(days_ago(8), "Z") is False
    assert is_due(days_ago(31), "Z") is True


def test_due_selects_across_tiers_with_the_never_mapped_first(store: Store) -> None:
    org(store, "fresh.org", tier="A", last_mapped=days_ago(1))
    org(store, "stale.org", tier="A", last_mapped=days_ago(20))
    never = org(store, "never.org", tier="C")
    org(store, "monthly-fresh.org", tier="C", last_mapped=days_ago(2))

    due = mapper(store).due()

    assert [o.canonical_domain for o in due] == ["stale.org", "never.org"]
    assert never.last_mapped is None


def test_due_can_be_limited_to_one_tier(store: Store) -> None:
    org(store, "a.org", tier="A")
    org(store, "c.org", tier="C")
    assert [o.canonical_domain for o in mapper(store).due(tiers=("A",))] == ["a.org"]


def test_mapping_records_that_the_organization_was_looked_at(store: Store) -> None:
    o = org(store)
    mapper(store).map_organizations([o])
    assert store.organizations.get_by_domain("gamep.org").last_mapped is not None


def test_a_domain_that_matched_nothing_is_still_marked_looked_at(store: Store) -> None:
    """'Looked at, found nothing' is an answer. Treating it as never-looked-at
    re-maps the same barren domain every single run, forever."""
    o = org(store, "quiet.org")
    provider = FakeMapProvider({"quiet.org": ["https://quiet.org/about"]})

    result = mapper(store, provider).map_organizations([o])

    assert result.candidates == []
    assert result.no_candidates == ["quiet.org"]
    assert store.organizations.get_by_domain("quiet.org").last_mapped is not None


def test_a_domain_nobody_could_map_stays_due(store: Store) -> None:
    """The opposite case, and it must not look the same. Nobody looked at this
    domain, so marking it mapped would skip it for a month over a 503."""
    o = org(store, "broken.org")
    provider = FakeMapProvider({"broken.org": []}, broken={"broken.org"})

    with start_run(store, "weekly", run_id="r-1") as run:
        result = mapper(store, provider).map_organizations([o], run=run)

    assert result.unmappable == ["broken.org"]
    assert result.no_candidates == []
    assert store.organizations.get_by_domain("broken.org").last_mapped is None
    assert store.runs.get("r-1").not_reached[0]["reason"] == "map_failed"


def test_run_due_retiers_then_maps_what_is_due(store: Store) -> None:
    store.networks.upsert(Network(network_id="n", name="N", tier="A"))
    org(store, "gamep.org", tier="C", network_id="n", last_mapped=days_ago(10))
    provider = FakeMapProvider()

    result = mapper(store, provider).run_due()

    assert store.organizations.get_by_domain("gamep.org").tier == "A", "promoted first"
    assert provider.calls == ["gamep.org"], "then mapped, because tier A is due at 10 days"
    assert result.candidates


def test_run_due_maps_nothing_when_nothing_is_due(store: Store) -> None:
    org(store, "gamep.org", tier="A", last_mapped=days_ago(1))
    provider = FakeMapProvider()
    assert mapper(store, provider).run_due().organizations == 0
    assert provider.calls == []


# --- reporting -------------------------------------------------------------


def test_the_pass_is_counted_on_the_run(store: Store) -> None:
    with start_run(store, "weekly", run_id="r-1") as run:
        result = mapper(store).map_organizations([org(store)], run=run)

    counters = store.runs.get("r-1").counters
    assert counters["orgs_mapped"] == 1
    assert counters["candidates"] == len(result.candidates)


def test_the_result_summarises_itself(store: Store) -> None:
    summary = mapper(store).map_organizations([org(store)]).as_dict()
    assert summary["organizations"] == 1
    assert summary["candidates"] > 0
    assert summary["unmappable"] == 0


def test_candidates_can_be_read_back_per_organization(store: Store) -> None:
    a, b = org(store, "gamep.org"), org(store, "scmep.org")
    provider = FakeMapProvider(
        {"gamep.org": GAMEP_INVENTORY, "scmep.org": ["https://scmep.org/speak"]}
    )
    result = mapper(store, provider).map_organizations([a, b])

    assert [c.url for c in result.for_organization(b.org_id)] == ["https://scmep.org/speak"]
    assert len(result.for_organization(a.org_id)) > 1


def test_the_per_organization_limit_reaches_the_inventory(store: Store) -> None:
    result = mapper(store, limit_per_org=2).map_organizations([org(store)])
    assert len(result.candidates) == 2
