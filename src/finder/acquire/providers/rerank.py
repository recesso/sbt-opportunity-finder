"""Reranking — the single biggest precision lever.

A cross-encoder scores ``(thesis, candidate)`` jointly. That is materially more
accurate than comparing two independently produced embeddings, because it can
weigh how the candidate relates to *this* query rather than how each looks on
its own. It is also roughly one fiftieth the cost of extraction, which is what
makes it worth running on everything the gate lets through.

The one subtlety worth stating is truncation. Documents run longer than the
provider's limit, and the naive answer — keep the first N characters — throws
away the region that made the page a candidate at all. A page's call for
speakers is usually two thirds of the way down. So the window is chosen around
the densest marker hits, and the head is only the fallback when nothing matched.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx

from finder.acquire.providers.base import FetchError

DEFAULT_BASE_URL = "https://api.cohere.com/v2"
DEFAULT_MODEL = "rerank-v3.5"
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_MAX_DOC_CHARS = 4000
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


@dataclass(frozen=True, slots=True)
class RerankHit:
    """One candidate's relevance to the query, by its position in the input.

    Indexed rather than named so the caller keeps ownership of what a document
    *is* — a page, a route draft, a snippet. The reranker only ranks.
    """

    index: int
    score: float


@runtime_checkable
class RerankProvider(Protocol):
    """Score documents against one query, jointly."""

    name: str

    def rerank(
        self, query: str, docs: Sequence[str], *, top_k: int | None = None
    ) -> list[RerankHit]:
        """Hits ordered best first. Raises :class:`FetchError` when unavailable."""
        ...


def marker_window(text: str, markers: Sequence[str], *, limit: int) -> str:
    """The ``limit`` characters most worth sending, ending on a sentence.

    Keeping the head of a page is the obvious choice and the wrong one: a call
    for speakers is usually two thirds of the way down, under the fold, and
    truncating to the head sends the reranker a masthead and a navigation menu.
    """
    if len(text) <= limit:
        return text
    if not markers:
        return _trim_to_sentence(text[:limit])

    lowered = text.lower()
    positions = [pos for marker in markers if (pos := lowered.find(marker.lower())) >= 0]
    if not positions:
        return _trim_to_sentence(text[:limit])

    # Centre the window on the densest run of markers rather than on the first.
    # One stray mention near the top should not drag the window away from the
    # part of the page where several markers cluster.
    best_start, best_hits = 0, -1
    for candidate in positions:
        start = max(0, candidate - limit // 3)
        hits = sum(1 for p in positions if start <= p < start + limit)
        if hits > best_hits:
            best_start, best_hits = start, hits

    return _trim_to_sentence(text[best_start : best_start + limit])


# Below this share of the window, trimming has cost more than it bought.
_MIN_TRIM_RATIO = 0.5


def _trim_to_sentence(chunk: str) -> str:
    """Cut back to the last sentence boundary, so the model is not handed half
    a sentence and asked to judge it.

    Unless that would throw most of the window away. A page whose only boundary
    is a stray full stop near the start would otherwise be reduced to "." and
    ranked against nothing at all — worse than a slightly ragged tail.
    """
    parts = _SENTENCE_END.split(chunk)
    if len(parts) > 1:
        trimmed = " ".join(parts[:-1]).strip()
        if len(trimmed) >= len(chunk) * _MIN_TRIM_RATIO:
            return trimmed
    return chunk.strip()


class CohereRerank:
    """Cohere adapter. Narrow enough that a second reranker is a drop-in."""

    name = "cohere"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        client: httpx.Client | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_doc_chars: int = DEFAULT_MAX_DOC_CHARS,
        cost_per_call_usd: float = 0.0,
    ) -> None:
        if not api_key:
            raise ValueError("CohereRerank requires an API key; see finder.secrets")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.max_doc_chars = max_doc_chars
        self.cost_per_call_usd = cost_per_call_usd
        self._client = client or httpx.Client(timeout=timeout_s)
        self.calls = 0

    def rerank(
        self,
        query: str,
        docs: Sequence[str],
        *,
        top_k: int | None = None,
        markers: Sequence[str] = (),
    ) -> list[RerankHit]:
        if not query.strip():
            raise ValueError("reranking needs a query")
        if not docs:
            return []  # nothing to rank is not an error; it is an empty week

        windows = [marker_window(d, markers, limit=self.max_doc_chars) for d in docs]
        payload: dict[str, Any] = {
            "model": self.model,
            "query": query,
            "documents": windows,
        }
        if top_k is not None:
            payload["top_n"] = min(top_k, len(windows))

        body = self._post(payload)
        return self._to_hits(body.get("results"), len(docs))

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        try:
            response = self._client.post(
                f"{self.base_url}/rerank",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "content-type": "application/json",
                },
                timeout=self.timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise FetchError("the reranker timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise FetchError(f"transport error calling the reranker: {exc}") from exc

        if response.status_code >= 400:
            raise FetchError(
                f"the reranker returned {response.status_code}",
                status=response.status_code,
                retryable=response.status_code in RETRYABLE_STATUS,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise FetchError("the reranker's response is not JSON") from exc
        if not isinstance(body, dict):
            raise FetchError("the reranker returned no results object")
        return body

    def _to_hits(self, raw: Any, doc_count: int) -> list[RerankHit]:
        """Ordered best first, with ties broken by input position.

        Deterministic ordering matters more than it looks: the same candidate
        set must rank the same way twice, or a week's list reshuffles for no
        reason and nobody can tell whether the system changed its mind.
        """
        if not isinstance(raw, list):
            raise FetchError("the reranker returned no results")
        hits: list[RerankHit] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            index = item.get("index")
            if not isinstance(index, int) or not 0 <= index < doc_count:
                continue
            hits.append(RerankHit(index=index, score=float(item.get("relevance_score") or 0.0)))
        return sorted(hits, key=lambda h: (-h.score, h.index))

    def close(self) -> None:
        self._client.close()
