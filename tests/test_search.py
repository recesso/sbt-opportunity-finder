"""The search provider — the way in to organizations no directory lists.

Search is trusted for *existence*, not for ranking. Its idea of relevance is not
this system's, and the marker gate and reranker downstream are where relevance is
actually decided. What these tests protect is that a result carries the query
that surfaced it, that failures say whether retrying helps, and that an empty
answer is an empty answer rather than a crash.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from finder.acquire.providers.base import FetchError
from finder.acquire.providers.search import (
    DEFAULT_BASE_URL,
    MAX_RESULTS,
    ExaSearch,
    SearchProvider,
    SearchResult,
)

SEARCH = f"{DEFAULT_BASE_URL}/search"
QUERY = "manufacturing: an organization that holds direct relationships with many employers"


def body(*results: dict) -> dict:
    return {"results": list(results)}


def hit(url: str, title: str = "Georgia MEP", **kw) -> dict:
    return {"url": url, "title": title, **kw}


# --- the protocol ----------------------------------------------------------


def test_the_adapter_satisfies_the_protocol() -> None:
    assert isinstance(ExaSearch(api_key="k"), SearchProvider)


def test_a_search_provider_needs_a_key() -> None:
    with pytest.raises(ValueError, match="requires an API key"):
        ExaSearch(api_key="")


def test_an_empty_query_is_a_programming_error() -> None:
    """An empty query returns the whole internet, ranked by nothing."""
    with pytest.raises(ValueError, match="needs a query"):
        ExaSearch(api_key="k").search("   ")


# --- results ---------------------------------------------------------------


@respx.mock
def test_a_result_carries_the_query_that_surfaced_it() -> None:
    """A domain found by 'state manufacturers association' is a different
    candidate from one found by 'workforce consultant'."""
    respx.post(SEARCH).mock(
        return_value=httpx.Response(
            200,
            json=body(
                hit(
                    "https://gamep.org/",
                    "Georgia MEP",
                    text="Statewide lunch-and-learn series",
                    publishedDate="2026-08-01",
                )
            ),
        )
    )
    results = ExaSearch(api_key="k").search(QUERY)

    assert results == [
        SearchResult(
            url="https://gamep.org/",
            title="Georgia MEP",
            query=QUERY,
            snippet="Statewide lunch-and-learn series",
            published="2026-08-01",
            provider="exa",
        )
    ]


@respx.mock
def test_the_request_asks_for_neural_search() -> None:
    """The queries are descriptions of a kind of organization, not keyword bags,
    and keyword search on those returns exactly the noise this system fights."""
    route = respx.post(SEARCH).mock(return_value=httpx.Response(200, json=body()))
    ExaSearch(api_key="secret").search(QUERY, limit=10, include_domains=["gamep.org"])

    import json

    payload = json.loads(route.calls[0].request.content)
    assert payload["type"] == "neural"
    assert payload["query"] == QUERY
    assert payload["numResults"] == 10
    assert payload["includeDomains"] == ["gamep.org"]
    assert route.calls[0].request.headers["x-api-key"] == "secret"


@respx.mock
@pytest.mark.parametrize(("asked", "sent"), [(0, 1), (-5, 1), (500, MAX_RESULTS), (25, 25)])
def test_the_result_count_is_clamped(asked: int, sent: int) -> None:
    route = respx.post(SEARCH).mock(return_value=httpx.Response(200, json=body()))
    ExaSearch(api_key="k").search(QUERY, limit=asked)

    import json

    assert json.loads(route.calls[0].request.content)["numResults"] == sent


@respx.mock
def test_domains_are_only_sent_when_asked_for() -> None:
    route = respx.post(SEARCH).mock(return_value=httpx.Response(200, json=body()))
    ExaSearch(api_key="k").search(QUERY)

    import json

    assert "includeDomains" not in json.loads(route.calls[0].request.content)


@respx.mock
def test_a_long_snippet_is_truncated() -> None:
    """Snippets ride along into logs and prompts; an unbounded one is a page."""
    respx.post(SEARCH).mock(
        return_value=httpx.Response(200, json=body(hit("https://x.org/", text="y" * 5000)))
    )
    assert len(ExaSearch(api_key="k").search(QUERY)[0].snippet) == 1000


@respx.mock
def test_malformed_entries_are_skipped_not_fatal() -> None:
    """One bad row in a result set must not cost the other twenty-four."""
    respx.post(SEARCH).mock(
        return_value=httpx.Response(
            200,
            json=body(
                {"title": "no url at all"},
                "not an object",
                {"url": 42},
                hit("https://gamep.org/"),
            ),
        )
    )
    assert [r.url for r in ExaSearch(api_key="k").search(QUERY)] == ["https://gamep.org/"]


@respx.mock
@pytest.mark.parametrize("payload", [{"results": None}, {}, {"results": "nope"}])
def test_no_results_is_an_empty_answer_not_a_crash(payload: dict) -> None:
    """Search legitimately returns nothing for a narrow query. Losing the whole
    discovery pass over that would be a poor trade."""
    respx.post(SEARCH).mock(return_value=httpx.Response(200, json=payload))
    assert ExaSearch(api_key="k").search(QUERY) == []


# --- failures --------------------------------------------------------------


@respx.mock
@pytest.mark.parametrize(("status", "retryable"), [(429, True), (503, True), (401, False)])
def test_failures_say_whether_retrying_helps(status: int, retryable: bool) -> None:
    respx.post(SEARCH).mock(return_value=httpx.Response(status))
    with pytest.raises(FetchError) as exc:
        ExaSearch(api_key="k").search(QUERY)
    assert exc.value.retryable is retryable
    assert exc.value.status == status


@respx.mock
def test_a_search_timeout_is_retryable() -> None:
    respx.post(SEARCH).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(FetchError, match="timed out") as exc:
        ExaSearch(api_key="k").search(QUERY)
    assert exc.value.retryable is True


@respx.mock
def test_a_search_transport_error_is_not_retryable() -> None:
    respx.post(SEARCH).mock(side_effect=httpx.ConnectError("no route"))
    with pytest.raises(FetchError, match="transport error"):
        ExaSearch(api_key="k").search(QUERY)


@respx.mock
def test_a_non_json_search_response_is_a_clear_error() -> None:
    respx.post(SEARCH).mock(return_value=httpx.Response(200, text="<html>502</html>"))
    with pytest.raises(FetchError, match="not JSON"):
        ExaSearch(api_key="k").search(QUERY)


@respx.mock
def test_a_json_array_response_is_a_clear_error() -> None:
    respx.post(SEARCH).mock(return_value=httpx.Response(200, json=["nope"]))
    with pytest.raises(FetchError, match="no results object"):
        ExaSearch(api_key="k").search(QUERY)


@respx.mock
def test_calls_are_counted() -> None:
    respx.post(SEARCH).mock(return_value=httpx.Response(200, json=body()))
    provider = ExaSearch(api_key="k")
    provider.search(QUERY)
    provider.search(QUERY)
    assert provider.calls == 2


def test_the_search_client_can_be_closed() -> None:
    client = httpx.Client()
    ExaSearch(api_key="k", client=client).close()
    assert client.is_closed
