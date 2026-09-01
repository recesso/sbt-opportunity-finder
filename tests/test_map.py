"""E2.S5 — URL inventory.

The acceptance criterion has two halves and both are asserted here: a domain
with no sitemap still returns an inventory (the provider path needs none), and
every hit records the term that matched it.

The term is the point. A URL found by "call for speakers" is a different animal
from the same URL found by "blog", and losing that distinction would leave the
reranker with nothing to work from.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import httpx
import pytest
import respx
import yaml

from finder.acquire.map import (
    DEFAULT_LIMIT,
    FirecrawlMap,
    MapHit,
    MapProvider,
    SitemapMap,
    UrlInventory,
    hits_by_term,
    is_fetchable,
    match_term,
    normalize_url,
    select,
)
from finder.acquire.providers.base import FetchError
from finder.context import start_run
from finder.store.db import open_db
from finder.store.repos import Store

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "gamep.org"
MAP_URL = "https://api.firecrawl.dev/v2/map"

TERMS = ("speak", "call for speakers", "committees", "council", "blog", "instructor")


def programming_paths() -> list[str]:
    data = yaml.safe_load((ROOT / "config" / "paths.yaml").read_text(encoding="utf-8"))
    return data["PROGRAMMING_PATHS"]


class FakeMap:
    """A provider with no transport, and an optional failure."""

    def __init__(self, name: str, *pairs, error: Exception | None = None) -> None:
        self.name = name
        self.pairs = list(pairs)
        self.error = error
        self.calls: list[tuple[str, int]] = []

    def map(self, domain: str, *, limit: int = DEFAULT_LIMIT):
        self.calls.append((domain, limit))
        if self.error is not None:
            raise self.error
        return self.pairs


def sitemap_xml(*urls: str) -> str:
    body = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{body}</urlset>"
    )


def sitemap_index(*urls: str) -> str:
    body = "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in urls)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{body}</sitemapindex>"
    )


# --- normalisation ---------------------------------------------------------


def test_fragments_and_tracking_parameters_are_dropped() -> None:
    assert (
        normalize_url("https://gamep.org/events?utm_source=x&id=7&fbclid=z#agenda")
        == "https://gamep.org/events?id=7"
    )


def test_a_meaningful_query_string_survives() -> None:
    """On an AMS host the query string IS the address of the event page."""
    url = "https://business.cobbchamber.com/events/details?eid=1182"
    assert normalize_url(url) == url


def test_trailing_slashes_collapse_so_one_page_is_one_url() -> None:
    assert normalize_url("https://gamep.org/events/") == normalize_url("https://gamep.org/events")


def test_a_bare_root_keeps_its_slash() -> None:
    assert normalize_url("https://gamep.org/") == "https://gamep.org/"


@pytest.mark.parametrize(
    "bad", ["", "   ", "mailto:zack@joineta.org", "/relative/path", "ftp://x.org/f", "javascript:0"]
)
def test_non_http_urls_are_rejected(bad: str) -> None:
    assert normalize_url(bad) == ""
    assert is_fetchable(normalize_url(bad)) is False


@pytest.mark.parametrize(
    "asset",
    [
        "https://gamep.org/logo.PNG",
        "https://gamep.org/app.js",
        "https://gamep.org/style.css?v=2",
    ],
)
def test_assets_are_not_worth_fetching(asset: str) -> None:
    assert is_fetchable(asset) is False


def test_a_pdf_is_worth_fetching() -> None:
    """Past agendas are PDFs and are among the richest evidence there is."""
    assert is_fetchable("https://gamep.org/2025-agenda.pdf") is True


# --- term matching ---------------------------------------------------------


def test_a_hyphenated_path_matches_a_spaced_term() -> None:
    assert match_term("https://gamep.org/call-for-speakers", None, TERMS) == (
        "call for speakers",
        "path",
    )


def test_the_longest_term_wins() -> None:
    """Reporting 'speak' for a page found by 'call for speakers' would understate
    it to the reranker."""
    matched = match_term("https://x.org/events/call-for-speakers-2026", None, TERMS)
    assert matched == ("call for speakers", "path")


def test_a_term_must_match_a_whole_word() -> None:
    """'council' must not fire on 'councilman', or every civic page becomes a
    council seat."""
    assert match_term("https://x.org/councilman-smith", None, ("council",)) is None
    assert match_term("https://x.org/manufacturing-council", None, ("council",)) == (
        "council",
        "path",
    )


def test_a_title_can_match_when_the_path_says_nothing() -> None:
    matched = match_term("https://x.org/node/4821", "Call for Speakers", TERMS)
    assert matched == ("call for speakers", "title")


def test_the_path_beats_the_title() -> None:
    """A term in the URL is a structural claim about the page; a term in a link
    title is someone's wording."""
    matched = match_term("https://x.org/committees/list", "Read our blog", ("committees", "blog"))
    assert matched == ("committees", "path")


def test_a_page_matching_nothing_is_not_a_hit() -> None:
    assert match_term("https://x.org/about/history", None, TERMS) is None


def test_the_query_string_is_searched_too() -> None:
    assert match_term("https://x.org/p?section=committees", None, TERMS) == ("committees", "path")


def test_empty_terms_match_nothing() -> None:
    assert match_term("https://x.org/speak", None, ("", "   ")) is None


def test_the_real_programming_paths_find_the_gamep_pages() -> None:
    """Against the actual config, not a toy list: these are the page shapes the
    single validated map call surfaced."""
    terms = programming_paths()
    for url, expected in [
        ("https://gamep.org/call-for-speakers", "call for speakers"),
        ("https://gamep.org/events/lunch-and-learn", None),
        ("https://gamep.org/about/committees", "committees"),
        ("https://gamep.org/get-involved", "get involved"),
    ]:
        matched = match_term(url, None, terms)
        if expected is None:
            continue
        assert matched is not None and matched[0] == expected, f"{url} -> {matched}"


# --- selection -------------------------------------------------------------


def test_selection_records_the_term_source_and_title() -> None:
    """The acceptance criterion: each hit records the term that matched it."""
    hits = select(
        [("https://gamep.org/call-for-speakers", "Speak at a GaMEP event")],
        TERMS,
        source="firecrawl",
    )
    assert hits == [
        MapHit(
            url="https://gamep.org/call-for-speakers",
            matched_term="call for speakers",
            matched_in="path",
            title="Speak at a GaMEP event",
            source="firecrawl",
        )
    ]


def test_duplicates_collapse_to_one_hit() -> None:
    hits = select(
        [
            ("https://gamep.org/committees", None),
            ("https://gamep.org/committees/", None),
            ("https://gamep.org/committees?utm_source=news", None),
        ],
        TERMS,
        source="firecrawl",
    )
    assert len(hits) == 1


def test_pages_matching_nothing_are_left_out() -> None:
    hits = select(
        [("https://gamep.org/about", None), ("https://gamep.org/speak", None)],
        TERMS,
        source="firecrawl",
    )
    assert [h.url for h in hits] == ["https://gamep.org/speak"]


def test_the_limit_is_respected() -> None:
    pairs = [(f"https://x.org/speak/{i}", None) for i in range(50)]
    assert len(select(pairs, TERMS, source="firecrawl", limit=10)) == 10


def test_hits_by_term_shows_which_terms_earn_their_place() -> None:
    hits = select(
        [
            ("https://x.org/speak", None),
            ("https://x.org/a/speak", None),
            ("https://x.org/committees", None),
        ],
        TERMS,
        source="firecrawl",
    )
    assert hits_by_term(hits) == {"speak": 2, "committees": 1}


# --- the firecrawl adapter -------------------------------------------------


def test_the_adapters_satisfy_the_protocol() -> None:
    assert isinstance(FirecrawlMap(api_key="k"), MapProvider)
    assert isinstance(SitemapMap(), MapProvider)
    assert isinstance(FakeMap("fake"), MapProvider)


def test_a_map_provider_needs_a_key() -> None:
    with pytest.raises(ValueError, match="requires an API key"):
        FirecrawlMap(api_key="")


@respx.mock
def test_one_domain_is_one_call() -> None:
    """The step is specified as one map call per organization; forty terms must
    not become forty calls."""
    route = respx.post(MAP_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "links": [
                    {"url": "https://gamep.org/call-for-speakers", "title": "Speak"},
                    {"url": "https://gamep.org/about"},
                ],
            },
        )
    )
    provider = FirecrawlMap(api_key="k")
    pairs = provider.map(DOMAIN)

    assert route.call_count == 1
    assert provider.calls == 1
    assert pairs == [
        ("https://gamep.org/call-for-speakers", "Speak"),
        ("https://gamep.org/about", None),
    ]

    import json

    payload = json.loads(route.calls[0].request.content)
    assert payload["url"] == "https://gamep.org"
    assert "search" not in payload, "matching is local so the matched term is knowable"


@respx.mock
def test_a_url_shaped_domain_is_not_double_schemed() -> None:
    route = respx.post(MAP_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "links": []})
    )
    FirecrawlMap(api_key="k").map("https://gamep.org")

    import json

    assert json.loads(route.calls[0].request.content)["url"] == "https://gamep.org"


def test_a_map_response_with_no_links_key_is_empty_not_a_crash() -> None:
    """Firecrawl omits the key for a domain it mapped to nothing. An empty
    inventory is the right answer; an exception would lose the domain."""
    from finder.acquire.map import _links_from

    assert _links_from(None) == []
    assert _links_from("https://not-a-list") == []
    assert _links_from([]) == []


@respx.mock
def test_bare_string_links_are_accepted() -> None:
    """Both response shapes are real; neither may crash the map."""
    respx.post(MAP_URL).mock(
        return_value=httpx.Response(
            200, json={"success": True, "links": ["https://gamep.org/speak", 42, None]}
        )
    )
    assert FirecrawlMap(api_key="k").map(DOMAIN) == [("https://gamep.org/speak", None)]


@respx.mock
@pytest.mark.parametrize(
    ("status", "retryable"),
    [(429, True), (503, True), (404, False), (403, False)],
)
def test_map_failures_say_whether_retrying_helps(status: int, retryable: bool) -> None:
    respx.post(MAP_URL).mock(return_value=httpx.Response(status))
    with pytest.raises(FetchError) as exc:
        FirecrawlMap(api_key="k").map(DOMAIN)
    assert exc.value.retryable is retryable


@respx.mock
def test_an_unsuccessful_map_is_an_error() -> None:
    respx.post(MAP_URL).mock(return_value=httpx.Response(200, json={"success": False}))
    with pytest.raises(FetchError, match="could not map"):
        FirecrawlMap(api_key="k").map(DOMAIN)


@respx.mock
def test_a_non_json_map_response_is_an_error() -> None:
    respx.post(MAP_URL).mock(return_value=httpx.Response(200, text="<html>502</html>"))
    with pytest.raises(FetchError, match="not JSON"):
        FirecrawlMap(api_key="k").map(DOMAIN)


@respx.mock
def test_a_map_timeout_is_retryable() -> None:
    respx.post(MAP_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(FetchError, match="timed out") as exc:
        FirecrawlMap(api_key="k").map(DOMAIN)
    assert exc.value.retryable is True


@respx.mock
def test_a_map_transport_error_is_not_retryable() -> None:
    respx.post(MAP_URL).mock(side_effect=httpx.ConnectError("no route"))
    with pytest.raises(FetchError, match="transport error"):
        FirecrawlMap(api_key="k").map(DOMAIN)


def test_the_map_client_can_be_closed() -> None:
    client = httpx.Client()
    FirecrawlMap(api_key="k", client=client).close()
    assert client.is_closed


# --- the sitemap fallback --------------------------------------------------


@respx.mock
def test_a_plain_sitemap_is_parsed() -> None:
    respx.get("https://gamep.org/sitemap.xml").mock(
        return_value=httpx.Response(
            200, text=sitemap_xml("https://gamep.org/speak", "https://gamep.org/about")
        )
    )
    assert SitemapMap().map(DOMAIN) == [
        ("https://gamep.org/speak", None),
        ("https://gamep.org/about", None),
    ]


@respx.mock
def test_the_next_candidate_is_tried_when_the_first_is_missing() -> None:
    respx.get("https://gamep.org/sitemap.xml").mock(return_value=httpx.Response(404))
    respx.get("https://gamep.org/sitemap_index.xml").mock(
        return_value=httpx.Response(200, text=sitemap_xml("https://gamep.org/speak"))
    )
    assert SitemapMap().map(DOMAIN) == [("https://gamep.org/speak", None)]


@respx.mock
def test_a_sitemap_index_is_followed_one_level() -> None:
    respx.get("https://gamep.org/sitemap.xml").mock(
        return_value=httpx.Response(
            200, text=sitemap_index("https://gamep.org/sm-1.xml", "https://gamep.org/sm-2.xml")
        )
    )
    respx.get("https://gamep.org/sm-1.xml").mock(
        return_value=httpx.Response(200, text=sitemap_xml("https://gamep.org/speak"))
    )
    respx.get("https://gamep.org/sm-2.xml").mock(
        return_value=httpx.Response(200, text=sitemap_xml("https://gamep.org/committees"))
    )
    assert [u for u, _ in SitemapMap().map(DOMAIN)] == [
        "https://gamep.org/speak",
        "https://gamep.org/committees",
    ]


@respx.mock
def test_a_self_referencing_index_does_not_loop_forever() -> None:
    respx.get("https://gamep.org/sitemap.xml").mock(
        return_value=httpx.Response(200, text=sitemap_index("https://gamep.org/sitemap.xml"))
    )
    with pytest.raises(FetchError, match="no sitemap"):
        SitemapMap(candidates=("/sitemap.xml",)).map(DOMAIN)


@respx.mock
def test_a_gzipped_sitemap_is_decompressed() -> None:
    respx.get("https://gamep.org/sitemap.xml").mock(
        return_value=httpx.Response(
            200, content=gzip.compress(sitemap_xml("https://gamep.org/speak").encode())
        )
    )
    assert SitemapMap().map(DOMAIN) == [("https://gamep.org/speak", None)]


@respx.mock
def test_malformed_xml_served_as_a_sitemap_is_not_a_sitemap() -> None:
    """Plenty of hosts answer /sitemap.xml with a 200 and their 404 page.

    The body here is deliberately NOT well-formed: a tidy `<html><body>...`
    parses fine as XML and would exercise the wrong branch entirely.
    """
    respx.get(url__regex=r"https://gamep\.org/sitemap.*").mock(
        return_value=httpx.Response(200, text="<html><br>Page not found<p>Sorry</html>")
    )
    with pytest.raises(FetchError, match="no sitemap"):
        SitemapMap().map(DOMAIN)


@respx.mock
def test_well_formed_xml_that_is_not_a_sitemap_yields_nothing() -> None:
    """An RSS feed or an XML error document is valid XML with no <loc> in it."""
    respx.get(url__regex=r"https://gamep\.org/sitemap.*").mock(
        return_value=httpx.Response(200, text="<rss><channel><title>News</title></channel></rss>")
    )
    with pytest.raises(FetchError, match="no sitemap"):
        SitemapMap().map(DOMAIN)


@respx.mock
def test_a_sitemap_index_stops_once_the_limit_is_reached() -> None:
    """A large site's index can point at fifty child sitemaps. Reading all of
    them to then throw the surplus away is wasted time on every domain."""
    respx.get("https://gamep.org/sitemap.xml").mock(
        return_value=httpx.Response(
            200, text=sitemap_index("https://gamep.org/sm-1.xml", "https://gamep.org/sm-2.xml")
        )
    )
    first = respx.get("https://gamep.org/sm-1.xml").mock(
        return_value=httpx.Response(
            200, text=sitemap_xml("https://gamep.org/speak", "https://gamep.org/committees")
        )
    )
    second = respx.get("https://gamep.org/sm-2.xml").mock(
        return_value=httpx.Response(200, text=sitemap_xml("https://gamep.org/council"))
    )

    pairs = SitemapMap().map(DOMAIN, limit=2)

    assert len(pairs) == 2
    assert first.called
    assert not second.called, "the second child sitemap was read for nothing"


@respx.mock
def test_no_sitemap_anywhere_is_a_clear_error() -> None:
    respx.get(url__regex=r"https://gamep\.org/sitemap.*").mock(return_value=httpx.Response(404))
    with pytest.raises(FetchError, match="no sitemap"):
        SitemapMap().map(DOMAIN)


@respx.mock
def test_a_sitemap_host_that_refuses_connections_is_not_a_crash() -> None:
    respx.get(url__regex=r"https://gamep\.org/sitemap.*").mock(
        side_effect=httpx.ConnectError("refused")
    )
    with pytest.raises(FetchError, match="no sitemap"):
        SitemapMap().map(DOMAIN)


@respx.mock
def test_corrupt_gzip_is_not_a_crash() -> None:
    respx.get(url__regex=r"https://gamep\.org/sitemap.*").mock(
        return_value=httpx.Response(200, content=b"\x1f\x8bnot really gzip")
    )
    with pytest.raises(FetchError, match="no sitemap"):
        SitemapMap().map(DOMAIN)


def test_the_sitemap_client_can_be_closed() -> None:
    client = httpx.Client()
    SitemapMap(client=client).close()
    assert client.is_closed


# --- orchestration: the acceptance criterion -------------------------------


@pytest.fixture
def store() -> Store:
    return Store(open_db(":memory:"))


@respx.mock
def test_a_domain_with_no_sitemap_still_returns_an_inventory(store: Store) -> None:
    """The acceptance criterion. The provider path needs no sitemap at all."""
    respx.post(MAP_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "links": [
                    {"url": "https://gamep.org/call-for-speakers"},
                    {"url": "https://gamep.org/about/committees"},
                    {"url": "https://gamep.org/about/history"},
                ],
            },
        )
    )
    sitemap = respx.get(url__regex=r"https://gamep\.org/sitemap.*").mock(
        return_value=httpx.Response(404)
    )

    hits = UrlInventory(FirecrawlMap(api_key="k"), fallback=SitemapMap()).map(DOMAIN, TERMS)

    assert [h.url for h in hits] == [
        "https://gamep.org/call-for-speakers",
        "https://gamep.org/about/committees",
    ]
    assert all(h.matched_term for h in hits), "every hit records why it was kept"
    assert not sitemap.called, "the fallback must not run when the provider worked"


def test_the_fallback_runs_when_the_provider_fails() -> None:
    primary = FakeMap("firecrawl", error=FetchError("503 unavailable"))
    fallback = FakeMap("sitemap", ("https://gamep.org/speak", None))

    hits = UrlInventory(primary, fallback=fallback).map(DOMAIN, TERMS)

    assert [h.source for h in hits] == ["sitemap"]
    assert fallback.calls == [(DOMAIN, DEFAULT_LIMIT)]


def test_both_paths_failing_reports_it_rather_than_reading_as_empty(store: Store) -> None:
    """An unmappable domain is not a domain with nothing on it, and the run
    report must not let the two look the same."""
    primary = FakeMap("firecrawl", error=FetchError("503"))
    fallback = FakeMap("sitemap", error=FetchError("no sitemap"))

    with start_run(store, "weekly", run_id="r-1") as run:
        hits = UrlInventory(primary, fallback=fallback).map(DOMAIN, TERMS, run=run)

    assert hits == []
    not_reached = store.runs.get("r-1").not_reached
    assert not_reached[0]["reason"] == "map_failed"
    assert "firecrawl" in not_reached[0]["detail"] and "sitemap" in not_reached[0]["detail"]


def test_a_domain_that_maps_to_nothing_relevant_is_not_a_failure(store: Store) -> None:
    """Empty because nothing matched is a real answer, and must not be reported
    as an unreachable domain."""
    primary = FakeMap("firecrawl", ("https://gamep.org/about", None))

    with start_run(store, "weekly", run_id="r-1") as run:
        hits = UrlInventory(primary).map(DOMAIN, TERMS, run=run)

    assert hits == []
    assert store.runs.get("r-1").not_reached == []


def test_mapping_is_charged_to_the_run(store: Store) -> None:
    primary = FakeMap("firecrawl", ("https://gamep.org/speak", None))
    with start_run(store, "weekly", run_id="r-1") as run:
        UrlInventory(primary).map(DOMAIN, TERMS, run=run)
    assert store.costs.by_provider("r-1") == {"firecrawl": 0.0}


def test_a_per_call_limit_overrides_the_default() -> None:
    primary = FakeMap("firecrawl", *[(f"https://x.org/speak/{i}", None) for i in range(20)])
    hits = UrlInventory(primary, limit=50).map(DOMAIN, TERMS, limit=5)
    assert len(hits) == 5
    assert primary.calls == [(DOMAIN, 5)]


def test_no_fallback_configured_is_fine() -> None:
    primary = FakeMap("firecrawl", error=FetchError("503"))
    assert UrlInventory(primary).map(DOMAIN, TERMS) == []
