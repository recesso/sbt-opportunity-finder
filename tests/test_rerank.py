"""E4.S3 — the reranker boundary.

Acceptance: a fixed input set produces a stable ordering across runs.

That matters more than it sounds. If the same candidates rank differently on two
runs, a week's list reshuffles for no reason and nobody can tell whether the
system changed its mind or the provider did. Ties break on input position, which
is arbitrary but *fixed*.

The other property under test is truncation. Keeping the head of a page is the
obvious choice and the wrong one: a call for speakers is usually two thirds of
the way down, and a head-truncated document sends the reranker a masthead.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from finder.acquire.providers.base import FetchError
from finder.acquire.providers.rerank import (
    DEFAULT_BASE_URL,
    CohereRerank,
    RerankHit,
    RerankProvider,
    marker_window,
)

RERANK = f"{DEFAULT_BASE_URL}/rerank"
THESIS = "an organization that holds direct relationships with many employers"


def results(*pairs: tuple[int, float]) -> dict:
    return {"results": [{"index": i, "relevance_score": s} for i, s in pairs]}


# --- the protocol ----------------------------------------------------------


def test_the_adapter_satisfies_the_protocol() -> None:
    assert isinstance(CohereRerank(api_key="k"), RerankProvider)


def test_a_reranker_needs_a_key() -> None:
    with pytest.raises(ValueError, match="requires an API key"):
        CohereRerank(api_key="")


def test_an_empty_query_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="needs a query"):
        CohereRerank(api_key="k").rerank("  ", ["doc"])


@respx.mock
def test_nothing_to_rank_is_an_empty_week_not_an_error() -> None:
    route = respx.post(RERANK).mock(return_value=httpx.Response(200, json=results()))
    assert CohereRerank(api_key="k").rerank(THESIS, []) == []
    assert not route.called, "and it costs nothing"


# --- ordering: the acceptance criterion ------------------------------------


@respx.mock
def test_the_same_input_ranks_the_same_way_twice() -> None:
    """The acceptance criterion. A list that reshuffles between runs cannot be
    reasoned about."""
    respx.post(RERANK).mock(
        return_value=httpx.Response(200, json=results((2, 0.91), (0, 0.44), (1, 0.72)))
    )
    provider = CohereRerank(api_key="k")
    docs = ["a", "b", "c"]

    first = provider.rerank(THESIS, docs)
    second = provider.rerank(THESIS, docs)

    assert first == second
    assert [h.index for h in first] == [2, 1, 0], "best first"


@respx.mock
def test_ties_break_on_input_position() -> None:
    """Arbitrary, but FIXED. Anything else means two equally good candidates
    swap places between runs for no reason."""
    respx.post(RERANK).mock(
        return_value=httpx.Response(200, json=results((3, 0.5), (1, 0.5), (2, 0.9)))
    )
    hits = CohereRerank(api_key="k").rerank(THESIS, ["a", "b", "c", "d"])
    assert [h.index for h in hits] == [2, 1, 3]


@respx.mock
def test_an_out_of_range_index_is_discarded() -> None:
    """An index pointing at a document that was not sent would silently rank
    some other candidate."""
    respx.post(RERANK).mock(
        return_value=httpx.Response(200, json=results((0, 0.9), (99, 0.99), (-1, 0.98)))
    )
    assert [h.index for h in CohereRerank(api_key="k").rerank(THESIS, ["a", "b"])] == [0]


@respx.mock
def test_a_malformed_result_row_is_skipped_not_fatal() -> None:
    respx.post(RERANK).mock(
        return_value=httpx.Response(
            200, json={"results": ["nope", {"index": 1, "relevance_score": 0.8}]}
        )
    )
    assert CohereRerank(api_key="k").rerank(THESIS, ["a", "b"]) == [RerankHit(1, 0.8)]


@respx.mock
def test_top_k_is_passed_through_and_bounded() -> None:
    route = respx.post(RERANK).mock(return_value=httpx.Response(200, json=results((0, 0.5))))
    CohereRerank(api_key="k").rerank(THESIS, ["a", "b"], top_k=50)
    assert json.loads(route.calls[0].request.content)["top_n"] == 2, "never more than sent"


@respx.mock
def test_without_top_k_everything_is_ranked() -> None:
    route = respx.post(RERANK).mock(return_value=httpx.Response(200, json=results((0, 0.5))))
    CohereRerank(api_key="k").rerank(THESIS, ["a"])
    assert "top_n" not in json.loads(route.calls[0].request.content)


# --- truncation ------------------------------------------------------------


def test_a_short_document_is_sent_whole() -> None:
    assert marker_window("short page", ["call for speakers"], limit=100) == "short page"


def test_the_window_follows_the_markers_not_the_masthead() -> None:
    """A call for speakers is usually two thirds of the way down. Head
    truncation sends the reranker a navigation menu."""
    masthead = "Home. About. Contact. " * 40
    body = "We are accepting a call for speakers from operators. "
    text = masthead + body + "Footer. " * 40

    window = marker_window(text, ["call for speakers"], limit=200)

    assert "call for speakers" in window
    assert len(window) <= 200


def test_the_window_prefers_where_markers_cluster() -> None:
    """One stray mention near the top should not drag the window away from the
    part of the page where several markers cluster."""
    text = (
        "workshop mentioned once. "
        + "filler. " * 200
        + "Our call for speakers is open to plant managers for a workshop. "
        + "filler. " * 200
    )
    window = marker_window(text, ["workshop", "call for speakers", "plant managers"], limit=150)
    assert "call for speakers" in window


def test_the_window_ends_on_a_sentence() -> None:
    """The model should not be handed half a sentence and asked to judge it."""
    text = "First sentence here. Second sentence here. Third sentence runs on and on and on."
    window = marker_window(text, [], limit=45)
    assert window.endswith(".")
    assert "Third sentence runs on" not in window


def test_a_document_with_no_markers_falls_back_to_the_head() -> None:
    text = "nothing relevant here at all. " * 50
    window = marker_window(text, ["call for speakers"], limit=100)
    assert window.startswith("nothing relevant")
    assert len(window) <= 100


def test_trimming_never_throws_most_of_the_window_away() -> None:
    """A page whose only sentence boundary is a stray full stop near the start
    would be reduced to "." and ranked against nothing. A ragged tail beats an
    empty document."""
    window = marker_window(". " + "x" * 200, [], limit=50)
    assert len(window) > 40, f"trimmed to {window!r}"


def test_a_single_run_on_sentence_is_still_returned() -> None:
    """A page with no sentence boundary at all must not truncate to nothing."""
    text = "x" * 500
    assert marker_window(text, [], limit=100) == "x" * 100


@respx.mock
def test_documents_are_truncated_before_they_are_sent() -> None:
    route = respx.post(RERANK).mock(return_value=httpx.Response(200, json=results((0, 0.5))))
    CohereRerank(api_key="k", max_doc_chars=50).rerank(THESIS, ["word. " * 200])

    sent = json.loads(route.calls[0].request.content)["documents"][0]
    assert len(sent) <= 50


# --- the request -----------------------------------------------------------


@respx.mock
def test_the_request_carries_the_model_and_the_key() -> None:
    route = respx.post(RERANK).mock(return_value=httpx.Response(200, json=results((0, 0.5))))
    CohereRerank(api_key="secret", model="rerank-test").rerank(THESIS, ["a"])

    request = route.calls[0].request
    payload = json.loads(request.content)
    assert payload["model"] == "rerank-test"
    assert payload["query"] == THESIS
    assert payload["documents"] == ["a"]
    assert request.headers["authorization"] == "Bearer secret"


# --- failures --------------------------------------------------------------


@respx.mock
@pytest.mark.parametrize(("status", "retryable"), [(429, True), (503, True), (401, False)])
def test_failures_say_whether_retrying_helps(status: int, retryable: bool) -> None:
    respx.post(RERANK).mock(return_value=httpx.Response(status))
    with pytest.raises(FetchError) as exc:
        CohereRerank(api_key="k").rerank(THESIS, ["a"])
    assert exc.value.retryable is retryable


@respx.mock
def test_a_rerank_timeout_is_retryable() -> None:
    respx.post(RERANK).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(FetchError, match="timed out") as exc:
        CohereRerank(api_key="k").rerank(THESIS, ["a"])
    assert exc.value.retryable is True


@respx.mock
def test_a_transport_error_is_not_retryable() -> None:
    respx.post(RERANK).mock(side_effect=httpx.ConnectError("no route"))
    with pytest.raises(FetchError, match="transport error"):
        CohereRerank(api_key="k").rerank(THESIS, ["a"])


@respx.mock
def test_a_non_json_response_is_a_clear_error() -> None:
    respx.post(RERANK).mock(return_value=httpx.Response(200, text="<html>502</html>"))
    with pytest.raises(FetchError, match="not JSON"):
        CohereRerank(api_key="k").rerank(THESIS, ["a"])


@respx.mock
def test_a_json_array_response_is_a_clear_error() -> None:
    respx.post(RERANK).mock(return_value=httpx.Response(200, json=["nope"]))
    with pytest.raises(FetchError, match="no results object"):
        CohereRerank(api_key="k").rerank(THESIS, ["a"])


@respx.mock
def test_missing_results_is_an_error_not_an_empty_ranking() -> None:
    """An empty ranking would silently drop every candidate that survived the
    gate — the whole week, reported as nothing worth looking at."""
    respx.post(RERANK).mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(FetchError, match="no results"):
        CohereRerank(api_key="k").rerank(THESIS, ["a"])


@respx.mock
def test_calls_are_counted() -> None:
    respx.post(RERANK).mock(return_value=httpx.Response(200, json=results((0, 0.5))))
    provider = CohereRerank(api_key="k")
    provider.rerank(THESIS, ["a"])
    provider.rerank(THESIS, ["a"])
    assert provider.calls == 2


def test_the_rerank_client_can_be_closed() -> None:
    client = httpx.Client()
    CohereRerank(api_key="k", client=client).close()
    assert client.is_closed
