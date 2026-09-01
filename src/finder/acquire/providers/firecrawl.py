"""Firecrawl adapter.

The only file in the system that knows this vendor exists. It converts one
scrape call into a :class:`Snapshot` and converts every failure mode into a
:class:`FetchError` that says whether retrying is worth anything.

Two decisions worth stating:

* **An empty page is an error, not an empty Snapshot.** A blank markdown body
  would flow downstream and extract cleanly as "the page states nothing", which
  is indistinguishable from a real thin page and is a lie about a fetch that
  failed.
* **Retries are bounded and only for retryable failures.** Retrying a 404 burns
  budget and hides the answer. ``sleep`` is injectable so the suite runs offline
  and instantly.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from finder.acquire.providers.base import FetchError, Snapshot
from finder.acquire.snapshot import content_hash
from finder.store.db import utcnow

DEFAULT_BASE_URL = "https://api.firecrawl.dev/v2"
DEFAULT_TIMEOUT_S = 60.0
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

PDF_CONTENT_TYPES = ("application/pdf",)


class FirecrawlFetch:
    """Scrape one URL to markdown plus links."""

    name = "firecrawl"

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_attempts: int = 3,
        backoff_s: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        # Deliberately left at zero, and not an open item. Firecrawl's own
        # billing page answers "what did I spend" better than a hardcoded price
        # that goes stale. What billing CANNOT answer is attribution — which
        # stage of which run burned the calls — and that comes from the unit
        # counts, which are always exact. Set this only if you want dollars
        # attributed per stage as well as calls.
        cost_per_call_usd: float = 0.0,
    ) -> None:
        if not api_key:
            raise ValueError("FirecrawlFetch requires an API key; see finder.secrets")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.max_attempts = max_attempts
        self.backoff_s = backoff_s
        self.cost_per_call_usd = cost_per_call_usd
        self._sleep = sleep
        self._client = client or httpx.Client(timeout=timeout_s)
        self.calls = 0

    def fetch(self, url: str, *, max_age_s: int = 0) -> Snapshot:
        payload = {
            "url": url,
            "formats": ["markdown", "links"],
            "onlyMainContent": True,
        }
        if max_age_s > 0:
            payload["maxAge"] = max_age_s * 1000  # the API takes milliseconds

        data = self._scrape(url, payload)
        return self._to_snapshot(url, data)

    # --- transport --------------------------------------------------------

    def _scrape(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        last: FetchError | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self._attempt(url, payload)
            except FetchError as exc:
                if not exc.retryable:
                    raise
                last = exc
                if attempt < self.max_attempts:
                    self._sleep(self.backoff_s * (2 ** (attempt - 1)))
        assert last is not None  # only reachable after a retryable failure
        raise last

    def _attempt(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        try:
            response = self._client.post(
                f"{self.base_url}/scrape",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise FetchError(f"timed out fetching {url}", url=url, retryable=True) from exc
        except httpx.HTTPError as exc:
            raise FetchError(f"transport error fetching {url}: {exc}", url=url) from exc

        if response.status_code >= 400:
            raise FetchError(
                f"firecrawl returned {response.status_code} for {url}",
                url=url,
                status=response.status_code,
                retryable=response.status_code in RETRYABLE_STATUS,
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise FetchError(f"firecrawl response for {url} is not JSON", url=url) from exc

        if not isinstance(body, dict) or not body.get("success", False):
            reason = (body or {}).get("error", "no reason given") if isinstance(body, dict) else ""
            raise FetchError(f"firecrawl could not scrape {url}: {reason}", url=url)

        data = body.get("data")
        if not isinstance(data, dict):
            raise FetchError(f"firecrawl returned no data for {url}", url=url)
        return data

    # --- shaping ----------------------------------------------------------

    def _to_snapshot(self, url: str, data: dict[str, Any]) -> Snapshot:
        markdown = data.get("markdown") or ""
        if not markdown.strip():
            raise FetchError(
                f"firecrawl returned an empty body for {url}. An empty snapshot would "
                "extract cleanly as 'the page states nothing', which is a lie about a "
                "fetch that failed.",
                url=url,
                retryable=True,
            )

        metadata = data.get("metadata") or {}
        status = metadata.get("statusCode")
        content_type = str(metadata.get("contentType") or "").lower()

        return Snapshot(
            content_hash=content_hash(markdown),
            url=url,
            canonical_url=str(metadata.get("sourceURL") or url),
            markdown=markdown,
            links=_clean_links(data.get("links")),
            status=int(status) if isinstance(status, int | str) and str(status).isdigit() else 200,
            fetched_at=utcnow(),
            is_pdf=_looks_like_pdf(url, content_type),
            provider=self.name,
        )

    def close(self) -> None:
        self._client.close()


def _looks_like_pdf(url: str, content_type: str) -> bool:
    """A past agenda is usually a PDF, and PDFs are among the richest evidence
    the system reads — a program listing every outside presenter."""
    return any(t in content_type for t in PDF_CONTENT_TYPES) or url.lower().endswith(".pdf")


def _clean_links(raw: Any) -> tuple[str, ...]:
    """Absolute http(s) links, deduplicated, order preserved.

    Order matters: the first submission-looking link on a page is usually the
    real one, and sorting would throw that signal away.
    """
    if not isinstance(raw, list):
        return ()
    seen: dict[str, None] = {}
    for item in raw:
        if isinstance(item, str) and item.startswith(("http://", "https://")):
            seen.setdefault(item.strip(), None)
    return tuple(seen)
