"""URL inventory — the highest-leverage call in the system.

One map call per organization returns what pages a domain has, without a crawl.
Validated 2026-09-01: a single map of gamep.org surfaced an entire statewide
lunch-and-learn series in seconds, which no amount of reading the home page
would have found.

Two things this module is careful about:

* **Which term matched is recorded, not just that something did.** A URL found
  by "call for speakers" is a different animal from one found by "blog", and
  ``matched_term`` becomes a reranker feature downstream. This is the reason
  matching happens locally rather than being delegated to the provider's own
  search: a provider that returns a ranked list cannot tell you *why*.
* **A domain with no sitemap still yields an inventory.** The provider path
  needs no sitemap at all. The sitemap parser is the fallback for when the
  provider is down or refuses the domain — not the primary route.

Deviation from the backlog's step 2, stated rather than buried: the Firecrawl
adapter does **not** pass the term set as the provider's ``search`` parameter.
That API takes one query per call, so forty terms would be forty calls against
a step that is specified as one call per organization. Instead the adapter takes
the inventory once and this module matches the terms against it, which also
makes ``matched_term`` knowable and the whole step deterministic.
"""

from __future__ import annotations

import gzip
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

import httpx

from finder.acquire.providers.base import FetchError
from finder.context import RunContext

DEFAULT_LIMIT = 500
DEFAULT_TIMEOUT_S = 30.0
MAX_CHILD_SITEMAPS = 20

SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml", "/sitemap1.xml")

# Assets and feeds. Cheap to drop here; expensive to fetch and extract nothing.
_SKIP_SUFFIXES = (
    ".css",
    ".js",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".mp4",
    ".mp3",
    ".zip",
)
_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "_hs")
# Query strings carry real signal on AMS hosts (`?section=committees`), so
# `=` and `&` are separators too, not opaque characters.
_SEPARATORS = re.compile(r"[-_/+.,=&?:;~%#|]+")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class MapHit:
    """One URL worth fetching, and the reason it is worth fetching.

    ``matched_term`` is the reason. Losing it would leave the reranker unable to
    tell a call for speakers from a blog index.
    """

    url: str
    matched_term: str
    matched_in: str  # "path" | "title"
    title: str | None = None
    source: str = ""


@runtime_checkable
class MapProvider(Protocol):
    """Returns a domain's URL inventory. One call, no crawl."""

    name: str

    def map(self, domain: str, *, limit: int = DEFAULT_LIMIT) -> list[tuple[str, str | None]]:
        """``(url, title)`` pairs. Raises :class:`FetchError` when unavailable."""
        ...


# --- normalisation and matching --------------------------------------------


def normalize_url(url: str) -> str:
    """Drop the fragment and tracking parameters, keep everything else.

    Not aggressive: a query string is often the whole address of an event page
    on an AMS host, so only known tracking keys are removed.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    query = "&".join(
        pair
        for pair in parts.query.split("&")
        if pair and not pair.lower().startswith(_TRACKING_PREFIXES)
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def is_fetchable(url: str) -> bool:
    return bool(url) and not url.lower().split("?")[0].endswith(_SKIP_SUFFIXES)


def _searchable(text: str) -> str:
    """Lowercase, with separators turned into spaces so 'call-for-speakers'
    and 'call for speakers' are the same string."""
    return f" {_WHITESPACE.sub(' ', _SEPARATORS.sub(' ', text.lower())).strip()} "


def match_term(url: str, title: str | None, terms: Sequence[str]) -> tuple[str, str] | None:
    """The best term matching this URL, and where it matched.

    Longest term wins: a page found by "call for speakers" is far more valuable
    than the same page found by "speak", and reporting the weaker match would
    understate it to the reranker. Ties break on the order in ``paths.yaml``,
    which is written most-important-first.

    The path is preferred over the title. A term in the URL is a structural
    claim about the page; a term in a link title is someone's wording.
    """
    parts = urlsplit(url)
    haystacks = (("path", _searchable(f"{parts.path} {parts.query}")),)
    if title:
        haystacks = (*haystacks, ("title", _searchable(title)))

    best: tuple[int, int, str, str] | None = None
    for order, term in enumerate(terms):
        needle = _searchable(term).strip()
        if not needle:
            continue
        for where, hay in haystacks:
            if f" {needle} " in hay:
                candidate = (-len(needle), order, term, where)
                if best is None or candidate < best:
                    best = candidate
                break  # path beats title for this term
    return (best[2], best[3]) if best else None


def select(
    pairs: Iterable[tuple[str, str | None]],
    terms: Sequence[str],
    *,
    source: str,
    limit: int = DEFAULT_LIMIT,
) -> list[MapHit]:
    """Turn an inventory into hits, deduplicated, first occurrence winning."""
    hits: list[MapHit] = []
    seen: set[str] = set()
    for raw, title in pairs:
        url = normalize_url(raw)
        if not is_fetchable(url) or url in seen:
            continue
        matched = match_term(url, title, terms)
        if matched is None:
            continue
        seen.add(url)
        hits.append(
            MapHit(
                url=url,
                matched_term=matched[0],
                matched_in=matched[1],
                title=title,
                source=source,
            )
        )
        if len(hits) >= limit:
            break
    return hits


# --- providers -------------------------------------------------------------


class FirecrawlMap:
    """One ``/map`` call per domain. No crawl, no per-term fan-out."""

    name = "firecrawl"

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        base_url: str = "https://api.firecrawl.dev/v2",
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        if not api_key:
            raise ValueError("FirecrawlMap requires an API key; see finder.secrets")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._client = client or httpx.Client(timeout=timeout_s)
        self.calls = 0

    def map(self, domain: str, *, limit: int = DEFAULT_LIMIT) -> list[tuple[str, str | None]]:
        self.calls += 1
        target = domain if domain.startswith(("http://", "https://")) else f"https://{domain}"
        try:
            response = self._client.post(
                f"{self.base_url}/map",
                json={"url": target, "limit": limit, "sitemap": "include"},
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise FetchError(f"timed out mapping {domain}", url=target, retryable=True) from exc
        except httpx.HTTPError as exc:
            raise FetchError(f"transport error mapping {domain}: {exc}", url=target) from exc

        if response.status_code >= 400:
            raise FetchError(
                f"firecrawl map returned {response.status_code} for {domain}",
                url=target,
                status=response.status_code,
                retryable=response.status_code in (408, 425, 429, 500, 502, 503, 504),
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise FetchError(
                f"firecrawl map response for {domain} is not JSON", url=target
            ) from exc

        if not isinstance(body, dict) or not body.get("success", False):
            raise FetchError(f"firecrawl could not map {domain}", url=target)
        return _links_from(body.get("links"))

    def close(self) -> None:
        self._client.close()


def _links_from(raw: Any) -> list[tuple[str, str | None]]:
    """Accept both response shapes: bare strings and ``{url, title}`` objects."""
    out: list[tuple[str, str | None]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, str):
            out.append((item, None))
        elif isinstance(item, dict) and isinstance(item.get("url"), str):
            title = item.get("title") or item.get("description")
            out.append((item["url"], title if isinstance(title, str) else None))
    return out


class SitemapMap:
    """Fallback inventory from ``/sitemap.xml``, for when the provider is out.

    Follows a sitemap index one level, bounded. Handles gzipped sitemaps, which
    are common enough on association hosts to be worth the four lines.
    """

    name = "sitemap"

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        candidates: Sequence[str] = SITEMAP_PATHS,
    ) -> None:
        self.timeout_s = timeout_s
        self.candidates = tuple(candidates)
        self._client = client or httpx.Client(timeout=timeout_s, follow_redirects=True)
        self.calls = 0

    def map(self, domain: str, *, limit: int = DEFAULT_LIMIT) -> list[tuple[str, str | None]]:
        root = domain if domain.startswith(("http://", "https://")) else f"https://{domain}"
        for candidate in self.candidates:
            urls = self._read(urljoin(root, candidate), limit, depth=0)
            if urls:
                return [(u, None) for u in urls[:limit]]
        raise FetchError(f"no sitemap found for {domain}", url=root)

    def _read(self, url: str, limit: int, *, depth: int) -> list[str]:
        body = self._get(url)
        if body is None:
            return []
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError:
            return []  # an HTML 404 page served with a 200; not a sitemap

        locs = [
            el.text.strip()
            for el in root.iter()
            if _tag(el) == "loc" and el.text and el.text.strip()
        ]
        if _tag(root) != "sitemapindex":
            return locs

        if depth > 0:
            return []  # one level of indirection is enough; a loop is not
        collected: list[str] = []
        for child in locs[:MAX_CHILD_SITEMAPS]:
            collected.extend(self._read(child, limit, depth=depth + 1))
            if len(collected) >= limit:
                break
        return collected

    def _get(self, url: str) -> bytes | None:
        self.calls += 1
        try:
            response = self._client.get(url, timeout=self.timeout_s)
        except httpx.HTTPError:
            return None
        if response.status_code >= 400:
            return None
        content = response.content
        if url.endswith(".gz") or content[:2] == b"\x1f\x8b":
            try:
                content = gzip.decompress(content)
            except (OSError, EOFError):
                return None
        return content

    def close(self) -> None:
        self._client.close()


def _tag(element: ElementTree.Element) -> str:
    """Local name without the namespace. Sitemaps declare several."""
    return element.tag.rsplit("}", 1)[-1]


# --- orchestration ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MapOutcome:
    """What mapping one domain produced, and whether it worked at all.

    ``mapped`` is the distinction that matters: a domain nobody could reach is
    not a domain with nothing on it, and a caller handed a bare empty list
    cannot tell the two apart.
    """

    hits: list[MapHit]
    source: str = ""
    failures: tuple[str, ...] = ()

    @property
    def mapped(self) -> bool:
        return bool(self.source)


class UrlInventory:
    """Map a domain, falling back to its sitemap, and say which was used."""

    def __init__(
        self,
        provider: MapProvider,
        *,
        fallback: MapProvider | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> None:
        self.provider = provider
        self.fallback = fallback
        self.limit = limit

    def map(
        self,
        domain: str,
        terms: Sequence[str],
        *,
        limit: int | None = None,
        run: RunContext | None = None,
    ) -> list[MapHit]:
        """Hits for one domain. Convenience over :meth:`map_detailed`.

        An empty list here is ambiguous by construction — use ``map_detailed``
        when the caller needs to tell "nothing matched" from "could not map".
        """
        return self.map_detailed(domain, terms, limit=limit, run=run).hits

    def map_detailed(
        self,
        domain: str,
        terms: Sequence[str],
        *,
        limit: int | None = None,
        run: RunContext | None = None,
    ) -> MapOutcome:
        """Hits plus whether the domain was mapped at all.

        A domain nobody could reach is recorded in ``not_reached`` and comes
        back with ``mapped`` False; a domain that mapped fine and matched
        nothing comes back mapped with no hits. Those are different findings.
        """
        cap = limit or self.limit
        attempts: list[tuple[MapProvider, str]] = [(self.provider, "provider")]
        if self.fallback is not None:
            attempts.append((self.fallback, "fallback"))

        failures: list[str] = []
        for source, _role in attempts:
            try:
                pairs = source.map(domain, limit=cap)
            except FetchError as exc:
                failures.append(f"{source.name}: {exc}")
                continue
            if run is not None:
                run.cost.record(source.name, "map")
            hits = select(pairs, terms, source=source.name, limit=cap)
            if run is not None:
                run.log.info(
                    "domain_mapped",
                    domain=domain,
                    source=source.name,
                    inventory=len(pairs),
                    hits=len(hits),
                )
            return MapOutcome(hits=hits, source=source.name, failures=tuple(failures))

        if run is not None:
            run.record_not_reached("map_failed", f"{domain}: " + "; ".join(failures))
        return MapOutcome(hits=[], source="", failures=tuple(failures))


def hits_by_term(hits: Iterable[MapHit]) -> dict[str, int]:
    """Which terms are actually earning their place in ``paths.yaml``.

    Feeds the source-reallocation loop: a term that never matches anything worth
    keeping is costing budget for nothing.
    """
    counts: dict[str, int] = {}
    for hit in hits:
        counts[hit.matched_term] = counts.get(hit.matched_term, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
