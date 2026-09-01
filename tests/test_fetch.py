"""E2.S1 — the fetch boundary and its cache.

The acceptance criterion is a request count: the same URL fetched twice inside
``max_age_s`` must make exactly one HTTP request. Everything else here guards
the failure modes that would quietly poison the audit trail — an empty body
recorded as a page that said nothing, a snapshot indexed before its bytes are on
disk, a failed call that never reaches the bill.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from finder.acquire.fetch import DEFAULT_MAX_AGE_S, Fetcher, age_seconds
from finder.acquire.providers.base import FetchError, FetchProvider, Snapshot
from finder.acquire.providers.firecrawl import DEFAULT_BASE_URL, FirecrawlFetch
from finder.acquire.snapshot import SnapshotStore, content_hash
from finder.context import start_run
from finder.store.db import open_db
from finder.store.repos import Store

URL = "https://gsae.org/speaker-interest-form"
SCRAPE = f"{DEFAULT_BASE_URL}/scrape"

MARKDOWN = """# Speaker Interest

GSAE members submit topics through the speaker interest form.
The education committee sets themes in early October.
"""


def scrape_body(
    markdown: str = MARKDOWN,
    *,
    links: list[str] | None = None,
    source_url: str = URL,
    status: int = 200,
    content_type: str = "text/html",
) -> dict:
    return {
        "success": True,
        "data": {
            "markdown": markdown,
            "links": links if links is not None else ["https://www.surveymonkey.com/r/NKSQCY6"],
            "metadata": {
                "sourceURL": source_url,
                "statusCode": status,
                "contentType": content_type,
            },
        },
    }


@pytest.fixture
def store() -> Store:
    return Store(open_db(":memory:"))


@pytest.fixture
def snapshots(tmp_path: Path) -> SnapshotStore:
    return SnapshotStore(tmp_path / "snapshots")


class FakeProvider:
    """A provider that records what it was asked for. No transport at all."""

    name = "fake"
    cost_per_call_usd = 0.01

    def __init__(self, *pages: Snapshot | Exception) -> None:
        self.queue = list(pages)
        self.requested: list[tuple[str, int]] = []

    def fetch(self, url: str, *, max_age_s: int = 0) -> Snapshot:
        self.requested.append((url, max_age_s))
        item = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        if isinstance(item, Exception):
            raise item
        return item


def make_snapshot(markdown: str = MARKDOWN, url: str = URL, **kw) -> Snapshot:
    return Snapshot(
        content_hash=content_hash(markdown),
        url=url,
        canonical_url=kw.pop("canonical_url", url),
        markdown=markdown,
        links=kw.pop("links", ("https://www.surveymonkey.com/r/NKSQCY6",)),
        status=kw.pop("status", 200),
        fetched_at=kw.pop("fetched_at", datetime.now(UTC).isoformat()),
        provider="fake",
        **kw,
    )


# --- the protocol ----------------------------------------------------------


def test_the_firecrawl_adapter_satisfies_the_protocol() -> None:
    """ADR-012: nothing above this line may know the vendor's name."""
    assert isinstance(FirecrawlFetch(api_key="k"), FetchProvider)
    assert isinstance(FakeProvider(make_snapshot()), FetchProvider)


def test_a_provider_needs_a_key() -> None:
    with pytest.raises(ValueError, match="requires an API key"):
        FirecrawlFetch(api_key="")


# --- the adapter -----------------------------------------------------------


@respx.mock
def test_a_scrape_becomes_a_snapshot() -> None:
    route = respx.post(SCRAPE).mock(return_value=httpx.Response(200, json=scrape_body()))
    snap = FirecrawlFetch(api_key="k").fetch(URL)

    assert snap.markdown == MARKDOWN
    assert snap.content_hash == content_hash(MARKDOWN)
    assert snap.url == URL
    assert snap.provider == "firecrawl"
    assert snap.links == ("https://www.surveymonkey.com/r/NKSQCY6",)
    assert snap.fetched_at
    assert route.called


@respx.mock
def test_the_request_asks_for_markdown_and_links_only() -> None:
    route = respx.post(SCRAPE).mock(return_value=httpx.Response(200, json=scrape_body()))
    FirecrawlFetch(api_key="secret-key").fetch(URL, max_age_s=3600)

    request = route.calls[0].request
    import json

    payload = json.loads(request.content)
    assert payload["formats"] == ["markdown", "links"]
    assert payload["onlyMainContent"] is True
    assert payload["maxAge"] == 3_600_000, "the API takes milliseconds"
    assert request.headers["authorization"] == "Bearer secret-key"


@respx.mock
def test_max_age_zero_omits_the_parameter() -> None:
    route = respx.post(SCRAPE).mock(return_value=httpx.Response(200, json=scrape_body()))
    FirecrawlFetch(api_key="k").fetch(URL, max_age_s=0)

    import json

    assert "maxAge" not in json.loads(route.calls[0].request.content)


@respx.mock
def test_the_resolved_url_is_kept_separately_from_the_requested_one() -> None:
    """A redirect to an AMS host is how one page becomes three organizations."""
    respx.post(SCRAPE).mock(
        return_value=httpx.Response(
            200, json=scrape_body(source_url="https://gsae.growthzoneapp.com/speakers")
        )
    )
    snap = FirecrawlFetch(api_key="k").fetch(URL)
    assert snap.url == URL
    assert snap.canonical_url == "https://gsae.growthzoneapp.com/speakers"


@respx.mock
def test_a_pdf_is_flagged() -> None:
    """Past agendas are usually PDFs and are among the richest evidence there is."""
    respx.post(SCRAPE).mock(
        return_value=httpx.Response(200, json=scrape_body(content_type="application/pdf"))
    )
    assert FirecrawlFetch(api_key="k").fetch(URL).is_pdf is True


@respx.mock
def test_a_pdf_url_is_flagged_even_without_a_content_type() -> None:
    respx.post(SCRAPE).mock(return_value=httpx.Response(200, json=scrape_body(content_type="")))
    assert FirecrawlFetch(api_key="k").fetch("https://x.org/2025-agenda.PDF").is_pdf is True


@respx.mock
def test_relative_and_junk_links_are_dropped_and_order_is_kept() -> None:
    """The first submission-looking link on a page is usually the real one, so
    order carries signal that sorting would throw away."""
    respx.post(SCRAPE).mock(
        return_value=httpx.Response(
            200,
            json=scrape_body(
                links=[
                    "https://b.example/second",
                    "/relative",
                    "mailto:zack@joineta.org",
                    "https://a.example/first",
                    "https://b.example/second",
                    None,
                ]
            ),
        )
    )
    assert FirecrawlFetch(api_key="k").fetch(URL).links == (
        "https://b.example/second",
        "https://a.example/first",
    )


# --- adapter failure modes -------------------------------------------------


@respx.mock
def test_an_empty_body_is_an_error_not_an_empty_snapshot() -> None:
    """A blank snapshot extracts cleanly as 'the page states nothing', which is
    indistinguishable from a real thin page and is a lie about a failed fetch."""
    respx.post(SCRAPE).mock(return_value=httpx.Response(200, json=scrape_body("   \n  ")))
    with pytest.raises(FetchError, match="empty body"):
        FirecrawlFetch(api_key="k", max_attempts=1).fetch(URL)


@respx.mock
def test_a_404_is_not_retried() -> None:
    """Retrying a permanent failure burns budget and hides the answer."""
    route = respx.post(SCRAPE).mock(return_value=httpx.Response(404, json={"error": "not found"}))
    provider = FirecrawlFetch(api_key="k", max_attempts=3, sleep=lambda _: None)

    with pytest.raises(FetchError) as exc:
        provider.fetch(URL)

    assert exc.value.status == 404
    assert exc.value.retryable is False
    assert route.call_count == 1


@respx.mock
def test_a_429_is_retried_then_succeeds() -> None:
    slept: list[float] = []
    respx.post(SCRAPE).mock(
        side_effect=[
            httpx.Response(429, json={"error": "slow down"}),
            httpx.Response(200, json=scrape_body()),
        ]
    )
    provider = FirecrawlFetch(api_key="k", max_attempts=3, backoff_s=2.0, sleep=slept.append)

    assert provider.fetch(URL).markdown == MARKDOWN
    assert provider.calls == 2
    assert slept == [2.0], "backoff must actually wait before the second attempt"


@respx.mock
def test_retries_are_bounded_and_the_last_error_surfaces() -> None:
    route = respx.post(SCRAPE).mock(return_value=httpx.Response(503))
    provider = FirecrawlFetch(api_key="k", max_attempts=3, sleep=lambda _: None)

    with pytest.raises(FetchError) as exc:
        provider.fetch(URL)

    assert route.call_count == 3
    assert exc.value.status == 503


@respx.mock
def test_backoff_grows() -> None:
    slept: list[float] = []
    respx.post(SCRAPE).mock(return_value=httpx.Response(500))
    provider = FirecrawlFetch(api_key="k", max_attempts=4, backoff_s=1.0, sleep=slept.append)

    with pytest.raises(FetchError):
        provider.fetch(URL)
    assert slept == [1.0, 2.0, 4.0]


@respx.mock
def test_a_timeout_is_retryable() -> None:
    respx.post(SCRAPE).mock(side_effect=httpx.ReadTimeout("too slow"))
    provider = FirecrawlFetch(api_key="k", max_attempts=2, sleep=lambda _: None)

    with pytest.raises(FetchError, match="timed out") as exc:
        provider.fetch(URL)
    assert exc.value.retryable is True
    assert provider.calls == 2


@respx.mock
def test_a_connection_error_is_not_retried_forever() -> None:
    respx.post(SCRAPE).mock(side_effect=httpx.ConnectError("no route to host"))
    provider = FirecrawlFetch(api_key="k", max_attempts=3, sleep=lambda _: None)

    with pytest.raises(FetchError, match="transport error"):
        provider.fetch(URL)
    assert provider.calls == 1


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        {"success": False, "error": "blocked by robots.txt"},
        {"success": True},
        {"success": True, "data": "not an object"},
        ["not", "an", "object"],
    ],
)
def test_a_malformed_response_is_a_clear_error(body: object) -> None:
    respx.post(SCRAPE).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(FetchError):
        FirecrawlFetch(api_key="k", max_attempts=1).fetch(URL)


@respx.mock
def test_a_non_json_response_is_a_clear_error() -> None:
    respx.post(SCRAPE).mock(return_value=httpx.Response(200, text="<html>gateway</html>"))
    with pytest.raises(FetchError, match="not JSON"):
        FirecrawlFetch(api_key="k", max_attempts=1).fetch(URL)


def test_zero_attempts_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        FirecrawlFetch(api_key="k", max_attempts=0)


def test_links_absent_from_the_response_is_not_a_crash() -> None:
    """Firecrawl omits the key when a page has no links. An empty tuple is the
    right answer; an exception would lose an otherwise perfectly good page."""
    from finder.acquire.providers.firecrawl import _clean_links

    assert _clean_links(None) == ()
    assert _clean_links("https://not-a-list") == ()
    assert _clean_links([]) == ()


def test_the_client_can_be_closed() -> None:
    client = httpx.Client()
    FirecrawlFetch(api_key="k", client=client).close()
    assert client.is_closed


# --- the cache: the acceptance criterion -----------------------------------


@respx.mock
def test_the_same_url_twice_makes_one_request(store: Store, snapshots: SnapshotStore) -> None:
    """The acceptance criterion, proven on the request count."""
    route = respx.post(SCRAPE).mock(return_value=httpx.Response(200, json=scrape_body()))
    fetcher = Fetcher(FirecrawlFetch(api_key="k"), store, snapshots)

    first = fetcher.fetch(URL, max_age_s=3600)
    second = fetcher.fetch(URL, max_age_s=3600)

    assert route.call_count == 1, "the second fetch hit the network"
    assert second.content_hash == first.content_hash
    assert second.markdown == first.markdown
    assert second.from_cache is True and first.from_cache is False
    assert fetcher.stats.as_dict() == {"hits": 1, "misses": 1, "failures": 0}


def test_a_stale_entry_is_refetched(store: Store, snapshots: SnapshotStore) -> None:
    provider = FakeProvider(make_snapshot())
    fetcher = Fetcher(provider, store, snapshots)

    fetcher.fetch(URL, max_age_s=3600)
    stale = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    store.conn.execute("UPDATE fetch_log SET last_fetched_at = ?", (stale,))

    fetcher.fetch(URL, max_age_s=3600)
    assert len(provider.requested) == 2
    assert fetcher.stats.hits == 0


def test_max_age_zero_always_refetches(store: Store, snapshots: SnapshotStore) -> None:
    provider = FakeProvider(make_snapshot())
    fetcher = Fetcher(provider, store, snapshots)

    fetcher.fetch(URL, max_age_s=0)
    fetcher.fetch(URL, max_age_s=0)
    assert len(provider.requested) == 2


def test_different_urls_do_not_share_a_cache_entry(store: Store, snapshots: SnapshotStore) -> None:
    provider = FakeProvider(make_snapshot())
    fetcher = Fetcher(provider, store, snapshots)

    fetcher.fetch(URL)
    fetcher.fetch("https://gsae.org/committees")
    assert len(provider.requested) == 2
    assert store.fetch_log.count() == 2


def test_a_missing_snapshot_forces_a_refetch(store: Store, snapshots: SnapshotStore) -> None:
    """The index outliving the bytes must not make a page permanently unreadable."""
    provider = FakeProvider(make_snapshot())
    fetcher = Fetcher(provider, store, snapshots)

    snap = fetcher.fetch(URL)
    snapshots.path_for(snap.content_hash).unlink()

    fetcher.fetch(URL)
    assert len(provider.requested) == 2


def test_the_default_max_age_is_a_week() -> None:
    assert DEFAULT_MAX_AGE_S == 7 * 24 * 3600


def test_the_providers_own_max_age_is_passed_through(
    store: Store, snapshots: SnapshotStore
) -> None:
    provider = FakeProvider(make_snapshot())
    Fetcher(provider, store, snapshots).fetch(URL, max_age_s=1234)
    assert provider.requested == [(URL, 1234)]


# --- what the cache writes -------------------------------------------------


def test_the_snapshot_is_stored_and_indexed(store: Store, snapshots: SnapshotStore) -> None:
    snap = Fetcher(FakeProvider(make_snapshot()), store, snapshots).fetch(URL)

    assert snapshots.get(snap.content_hash) == MARKDOWN
    record = store.fetch_log.get(URL)
    assert record.content_hash == snap.content_hash
    assert record.provider == "fake"
    assert record.links == ["https://www.surveymonkey.com/r/NKSQCY6"]
    assert record.fetch_count == 1
    assert record.change_count == 0


def test_a_changed_page_is_counted_as_a_change(store: Store, snapshots: SnapshotStore) -> None:
    """Stability over time without a separate history table."""
    changed = MARKDOWN + "\nProposals close 2026-10-03.\n"
    provider = FakeProvider(make_snapshot(), make_snapshot(changed))
    fetcher = Fetcher(provider, store, snapshots)

    fetcher.fetch(URL, max_age_s=0)
    fetcher.fetch(URL, max_age_s=0)

    record = store.fetch_log.get(URL)
    assert (record.fetch_count, record.change_count) == (2, 1)
    assert record.first_fetched_at <= record.last_fetched_at
    assert snapshots.stats().count == 2, "both versions are kept; snapshots are never replaced"


def test_an_unchanged_refetch_is_not_a_change(store: Store, snapshots: SnapshotStore) -> None:
    """Counting a re-read as a change would make every page look volatile."""
    fetcher = Fetcher(FakeProvider(make_snapshot()), store, snapshots)
    fetcher.fetch(URL, max_age_s=0)
    fetcher.fetch(URL, max_age_s=0)

    record = store.fetch_log.get(URL)
    assert (record.fetch_count, record.change_count) == (2, 0)
    assert snapshots.stats().count == 1


def test_two_urls_serving_one_page_are_findable(store: Store, snapshots: SnapshotStore) -> None:
    """A mirror and its original share a hash. Seeing that is how one page stops
    becoming two organizations."""
    fetcher = Fetcher(FakeProvider(make_snapshot()), store, snapshots)
    snap = fetcher.fetch(URL)
    fetcher.fetch("https://www.gsae.org/speaker-interest-form")

    assert {r.url for r in store.fetch_log.by_hash(snap.content_hash)} == {
        URL,
        "https://www.gsae.org/speaker-interest-form",
    }


# --- reporting -------------------------------------------------------------


def test_a_live_fetch_is_counted_and_charged(store: Store, snapshots: SnapshotStore) -> None:
    fetcher = Fetcher(FakeProvider(make_snapshot()), store, snapshots)

    with start_run(store, "weekly", run_id="r-1") as run:
        fetcher.fetch(URL, max_age_s=0, run=run)
        fetcher.fetch(URL, max_age_s=0, run=run)

    assert store.runs.get("r-1").counters["pages_fetched"] == 2
    assert store.costs.total("r-1") == pytest.approx(0.02)
    assert store.costs.by_provider("r-1") == {"fake": pytest.approx(0.02)}


def test_a_cache_hit_costs_nothing(store: Store, snapshots: SnapshotStore) -> None:
    fetcher = Fetcher(FakeProvider(make_snapshot()), store, snapshots)

    with start_run(store, "weekly", run_id="r-1") as run:
        fetcher.fetch(URL, max_age_s=3600, run=run)
        fetcher.fetch(URL, max_age_s=3600, run=run)

    assert store.runs.get("r-1").counters["pages_fetched"] == 1
    assert store.costs.total("r-1") == pytest.approx(0.01)


def test_a_failed_call_still_reaches_the_bill(store: Store, snapshots: SnapshotStore) -> None:
    """A ledger that only counts successes understates the bill exactly when
    things are going wrong."""
    fetcher = Fetcher(FakeProvider(FetchError("410 gone", url=URL)), store, snapshots)

    with start_run(store, "weekly", run_id="r-1") as run, pytest.raises(FetchError):
        fetcher.fetch(URL, run=run)

    assert store.costs.total("r-1") == pytest.approx(0.01)
    assert store.runs.get("r-1").counters["pages_fetched"] == 0
    assert fetcher.stats.as_dict() == {"hits": 0, "misses": 0, "failures": 1}
    assert store.fetch_log.count() == 0, "a failed fetch must not be indexed as fetched"


def test_a_price_override_wins(store: Store, snapshots: SnapshotStore) -> None:
    fetcher = Fetcher(FakeProvider(make_snapshot()), store, snapshots)
    with start_run(store, "weekly", run_id="r-1") as run:
        fetcher.fetch(URL, run=run, cost_usd=0.25)
    assert store.costs.total("r-1") == pytest.approx(0.25)


def test_fetching_without_a_run_still_works(store: Store, snapshots: SnapshotStore) -> None:
    """One-off scripts and the eval harness have no run to report to."""
    snap = Fetcher(FakeProvider(make_snapshot()), store, snapshots).fetch(URL)
    assert snap.markdown == MARKDOWN


def test_stats_count_calls(store: Store, snapshots: SnapshotStore) -> None:
    fetcher = Fetcher(FakeProvider(make_snapshot()), store, snapshots)
    fetcher.fetch(URL, max_age_s=0)
    fetcher.fetch(URL, max_age_s=3600)
    assert fetcher.stats.calls == 1


# --- age arithmetic --------------------------------------------------------


def test_age_is_measured_in_seconds() -> None:
    then = "2026-09-01T12:00:00+00:00"
    assert age_seconds(then, now="2026-09-01T12:01:30+00:00") == 90.0


def test_a_future_timestamp_reads_as_age_zero_not_negative() -> None:
    """Clock skew between machines would otherwise make a stale page look
    permanently fresh."""
    assert age_seconds("2026-09-02T00:00:00+00:00", now="2026-09-01T00:00:00+00:00") == 0.0
