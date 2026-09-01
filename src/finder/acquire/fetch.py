"""The fetch cache — one page, one call, one snapshot.

Sits above any :class:`~finder.acquire.providers.base.FetchProvider` and turns a
URL into stored text. Three jobs:

* **Do not pay twice.** A page fetched inside ``max_age_s`` is served from the
  snapshot store with no network call at all. The provider's own ``maxAge`` is
  still passed through, but a cache that avoids the request entirely is the one
  that shows up on the bill.
* **Write the snapshot before anything reads it.** Extraction reads only from
  the store, so the store must be written first and the fetch logged second.
* **Report honestly.** Hits, misses, failures and spend all land on the
  :class:`~finder.context.RunContext` when one is supplied.

A cache hit is only a hit when the snapshot is actually still on disk. A logged
fetch whose snapshot has gone missing is a miss, not an error — the alternative
is a page that can never be re-read because the index says it was already done.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from finder.acquire.providers.base import FetchError, FetchProvider, Snapshot
from finder.acquire.snapshot import SnapshotStore
from finder.context import RunContext
from finder.store.db import utcnow
from finder.store.repos import Store

# A week. Long enough that a weekly run does not re-pay for pages it read last
# run; short enough that a deadline appearing on Tuesday is seen this week.
DEFAULT_MAX_AGE_S = 7 * 24 * 3600


@dataclass
class FetchStats:
    hits: int = 0
    misses: int = 0
    failures: int = 0

    @property
    def calls(self) -> int:
        return self.misses + self.failures

    def as_dict(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "failures": self.failures}


def age_seconds(fetched_at: str, *, now: str | None = None) -> float:
    """Seconds between a stored timestamp and now. Negative clamps to zero.

    Clock skew between machines writing to the same database would otherwise
    produce a negative age, which reads as 'fetched in the future' and would
    make a stale page look permanently fresh.
    """
    then = datetime.fromisoformat(fetched_at)
    current = datetime.fromisoformat(now or utcnow())
    return max(0.0, (current - then).total_seconds())


class Fetcher:
    """Fetch through the cache. The only way the rest of the system gets pages."""

    def __init__(
        self,
        provider: FetchProvider,
        store: Store,
        snapshots: SnapshotStore,
        *,
        default_max_age_s: int = DEFAULT_MAX_AGE_S,
    ) -> None:
        self.provider = provider
        self.store = store
        self.snapshots = snapshots
        self.default_max_age_s = default_max_age_s
        self.stats = FetchStats()

    def fetch(
        self,
        url: str,
        *,
        max_age_s: int | None = None,
        run: RunContext | None = None,
        cost_usd: float | None = None,
    ) -> Snapshot:
        """Return the page. Raises :class:`FetchError` when it cannot be had."""
        max_age = self.default_max_age_s if max_age_s is None else max_age_s

        cached = self._from_cache(url, max_age)
        if cached is not None:
            self.stats.hits += 1
            if run is not None:
                run.log.debug("fetch_cache_hit", url=url, content_hash=cached.content_hash)
            return cached

        try:
            snapshot = self.provider.fetch(url, max_age_s=max_age)
        except FetchError:
            self.stats.failures += 1
            self._charge(run, cost_usd)
            raise

        self.stats.misses += 1
        self._charge(run, cost_usd)

        # Snapshot first: extraction reads only from the store, so an index entry
        # pointing at bytes that are not there yet is a page that cannot be read.
        self.snapshots.put(snapshot.markdown)
        self.store.fetch_log.record(
            url,
            content_hash=snapshot.content_hash,
            status=snapshot.status,
            provider=snapshot.provider or getattr(self.provider, "name", "unknown"),
            canonical_url=snapshot.canonical_url,
            is_pdf=snapshot.is_pdf,
            links=snapshot.links,
            fetched_at=snapshot.fetched_at or utcnow(),
        )
        if run is not None:
            run.count("pages_fetched")
        return snapshot

    # --- internals --------------------------------------------------------

    def _from_cache(self, url: str, max_age_s: int) -> Snapshot | None:
        if max_age_s <= 0:
            return None
        record = self.store.fetch_log.get(url)
        if record is None or age_seconds(record.last_fetched_at) > max_age_s:
            return None
        if not self.snapshots.has(record.content_hash):
            return None  # the index outlived the bytes; re-fetch rather than fail

        return Snapshot(
            content_hash=record.content_hash,
            url=record.url,
            canonical_url=record.canonical_url or record.url,
            markdown=self.snapshots.get(record.content_hash),
            links=tuple(record.links),
            status=record.status,
            fetched_at=record.last_fetched_at,
            is_pdf=record.is_pdf,
            provider=record.provider,
            from_cache=True,
        )

    def _charge(self, run: RunContext | None, cost_usd: float | None) -> None:
        """Record the call whether or not it succeeded. A failed request is
        still a request, and a ledger that only counts successes understates
        the bill exactly when things are going wrong."""
        if run is None:
            return
        price = cost_usd
        if price is None:
            price = getattr(self.provider, "cost_per_call_usd", 0.0)
        run.cost.record(getattr(self.provider, "name", "fetch"), "scrape", usd=price)
