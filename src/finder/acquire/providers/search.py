"""Search provider — the way in to organizations no directory lists.

W1 enumerates networks, and networks cover the bodies that belong to something.
They do not cover the state manufacturers association that left its national
affiliation, the employer collaborative funded by one foundation, or the regional
payments group that predates every directory it might have been in. Those exist
only as pages somebody wrote, and search is the only thing that reaches them.

The Protocol is deliberately narrow: a query in, ranked results out. Ranking is
not trusted — a search engine's idea of relevance is not this system's, and the
marker gate and reranker downstream are where relevance is actually decided.
What search is trusted for is *existence*: this domain is out there and nothing
in the registry knows about it.

``SearchResult.query`` records which query surfaced each result, for the same
reason ``MapHit.matched_term`` does: a domain found by "state manufacturers
association" is a different candidate from one found by "workforce consultant",
and the difference is worth keeping.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx

from finder.acquire.providers.base import FetchError

DEFAULT_BASE_URL = "https://api.exa.ai"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_RESULTS = 25
MAX_RESULTS = 100
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One page search says exists, and the query that surfaced it."""

    url: str
    title: str
    query: str
    snippet: str = ""
    published: str | None = None
    provider: str = ""


@runtime_checkable
class SearchProvider(Protocol):
    """Find pages by meaning, not by keyword match."""

    name: str

    def search(
        self, query: str, *, limit: int = DEFAULT_RESULTS, include_domains: list[str] | None = None
    ) -> list[SearchResult]:
        """Ranked results, or raise :class:`FetchError`."""
        ...


class ExaSearch:
    """Exa adapter. The only file that knows this vendor exists.

    Uses neural search: the queries this system asks are descriptions of a kind
    of organization ("state manufacturers association with an education
    committee"), not keyword bags, and keyword search on those returns exactly
    the noise the predecessor drowned in.
    """

    name = "exa"

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        cost_per_call_usd: float = 0.0,
    ) -> None:
        if not api_key:
            raise ValueError("ExaSearch requires an API key; see finder.secrets")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.cost_per_call_usd = cost_per_call_usd
        self._client = client or httpx.Client(timeout=timeout_s)
        self.calls = 0

    def search(
        self, query: str, *, limit: int = DEFAULT_RESULTS, include_domains: list[str] | None = None
    ) -> list[SearchResult]:
        if not query.strip():
            raise ValueError("search needs a query")
        payload: dict[str, Any] = {
            "query": query,
            "numResults": min(max(1, limit), MAX_RESULTS),
            "type": "neural",
        }
        if include_domains:
            payload["includeDomains"] = include_domains

        self.calls += 1
        try:
            response = self._client.post(
                f"{self.base_url}/search",
                json=payload,
                headers={"x-api-key": self.api_key},
                timeout=self.timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise FetchError(f"timed out searching for {query!r}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise FetchError(f"transport error searching for {query!r}: {exc}") from exc

        if response.status_code >= 400:
            raise FetchError(
                f"exa returned {response.status_code} for {query!r}",
                status=response.status_code,
                retryable=response.status_code in RETRYABLE_STATUS,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise FetchError(f"exa response for {query!r} is not JSON") from exc
        if not isinstance(body, dict):
            raise FetchError(f"exa returned no results object for {query!r}")

        return self._to_results(body.get("results"), query)

    def _to_results(self, raw: Any, query: str) -> list[SearchResult]:
        """A missing results list is an empty answer, not a crash.

        Search legitimately returns nothing for a narrow query, and losing the
        whole discovery pass over one empty answer would be a poor trade.
        """
        if not isinstance(raw, list):
            return []
        out: list[SearchResult] = []
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                continue
            out.append(
                SearchResult(
                    url=item["url"].strip(),
                    title=str(item.get("title") or "").strip(),
                    query=query,
                    snippet=str(item.get("text") or item.get("snippet") or "").strip()[:1000],
                    published=item.get("publishedDate") or None,
                    provider=self.name,
                )
            )
        return out

    def close(self) -> None:
        self._client.close()
