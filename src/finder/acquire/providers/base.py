"""The fetch boundary (ADR-012).

Every external service sits behind a Protocol so it can be swapped and faked.
Two consequences the project depends on:

* The whole suite runs offline. Tests use a fake provider, not a mocked
  transport wrapped around real client code.
* Firecrawl is replaceable. Nothing above this line knows the vendor's name.

``fetch`` returns a :class:`Snapshot` — stored text, not a live handle. The
model that writes a record sees only that text and has no browsing capability
in the call, which is what makes an invented span detectable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class FetchError(Exception):
    """A page could not be fetched.

    ``retryable`` separates "come back in a moment" (429, 5xx, timeout) from
    "this will never work" (404, 403, blocked by robots). Retrying the second
    kind burns budget and hides the real answer.
    """

    def __init__(
        self,
        message: str,
        *,
        url: str = "",
        status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.url = url
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One page as it was at one moment, addressed by its content.

    ``url`` is what was requested; ``canonical_url`` is where it resolved. They
    differ constantly — a redirect, a tracking parameter stripped, a chamber's
    event page served from an AMS host — and conflating them is how the same
    page ends up in the database three times.
    """

    content_hash: str
    url: str
    canonical_url: str
    markdown: str
    links: tuple[str, ...] = ()
    status: int = 200
    fetched_at: str = ""
    is_pdf: bool = False
    provider: str = ""
    # Excluded from equality: the same page is the same page whether it came
    # off the wire or off the disk.
    from_cache: bool = field(default=False, compare=False)


@runtime_checkable
class FetchProvider(Protocol):
    """What the rest of the system may assume about a fetcher."""

    name: str

    def fetch(self, url: str, *, max_age_s: int = 0) -> Snapshot:
        """Return the page, or raise :class:`FetchError`.

        ``max_age_s`` is passed to the provider so its own cache can serve the
        request. The local cache in ``finder.acquire.fetch`` sits above this and
        is the one that avoids the call entirely.
        """
        ...
