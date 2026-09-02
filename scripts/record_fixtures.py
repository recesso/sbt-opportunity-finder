#!/usr/bin/env python3
"""Record real provider responses so the suite can run offline forever after.

This is the one script in the repository that deliberately hits the network. It
is never run by CI and never by the test suite — a fixture is recorded once, by
a person, on purpose, and then lives in git.

    python scripts/record_fixtures.py --url https://gsae.org/speaker-interest-form
    python scripts/record_fixtures.py --seeds        # the three verified routes
    python scripts/record_fixtures.py --list         # what is already recorded

Needs ``FIRECRAWL_API_KEY`` (and ``EXA_API_KEY`` for ``--search``). Requests are
recorded WITHOUT headers, so a key can never reach the repository.

Existing recordings are not overwritten without ``--force``. A fixture is the
ground truth a dozen assertions rest on, and re-recording it silently is how a
test suite starts agreeing with whatever the web says this morning.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from finder.acquire import replay  # noqa: E402
from finder.acquire.providers.firecrawl import DEFAULT_BASE_URL  # noqa: E402
from finder.acquire.providers.search import DEFAULT_BASE_URL as EXA_URL  # noqa: E402
from finder.secrets import load_secrets, require  # noqa: E402

# The three routes verified by hand on 2026-09-01. Each exercises a different
# failure mode, which is why these are the seeds and not three arbitrary pages.
SEED_ROUTES: list[tuple[str, str]] = [
    (
        "https://joinaiweek.com/apply-to-speak",
        "ETA AI Week — open rolling call. The one Art submitted to.",
    ),
    (
        "https://gsae.org/speaker-interest-form",
        "GSAE — the form is an OFF-DOMAIN SurveyMonkey link in body text.",
    ),
    (
        "https://gamep.org/services/workforce-development",
        "GaMEP — the CHANNEL side: instructor/provider path, no published intake.",
    ),
]


def scrape(client: httpx.Client, api_key: str, url: str) -> tuple[httpx.Request, httpx.Response]:
    request = client.build_request(
        "POST",
        f"{DEFAULT_BASE_URL}/scrape",
        json={"url": url, "formats": ["markdown", "links"], "onlyMainContent": True},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    return request, client.send(request)


def search(client: httpx.Client, api_key: str, query: str) -> tuple[httpx.Request, httpx.Response]:
    request = client.build_request(
        "POST",
        f"{EXA_URL}/search",
        json={"query": query, "numResults": 25, "type": "neural"},
        headers={"x-api-key": api_key},
    )
    return request, client.send(request)


def record(
    directory: Path, request: httpx.Request, response: httpx.Response, note: str, *, force: bool
) -> bool:
    key = replay.key_for(request)
    if not force and replay.load(directory, key) is not None:
        print(f"  skip (already recorded)  {request.url}")
        return False
    if response.status_code >= 400:
        print(f"  FAILED {response.status_code}      {request.url}", file=sys.stderr)
        return False
    path = replay.save(directory, request, response, note=note)
    print(f"  recorded {path.name}  {request.url}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", action="append", default=[], help="page to scrape and record")
    ap.add_argument("--search", action="append", default=[], help="search query to record")
    ap.add_argument("--seeds", action="store_true", help="record the three verified seed routes")
    ap.add_argument("--list", action="store_true", help="show what is already recorded")
    ap.add_argument("--force", action="store_true", help="re-record even if present")
    ap.add_argument("--dir", default=str(ROOT / replay.DEFAULT_DIR))
    args = ap.parse_args()

    directory = Path(args.dir)

    if args.list:
        entries = replay.describe(directory)
        if not entries:
            print(f"No fixtures in {directory}.")
            return 0
        print(f"{len(entries)} fixture(s) in {directory}:")
        for what, note in entries:
            print(f"  {what}\n      {note}" if note else f"  {what}")
        return 0

    targets = [(u, "") for u in args.url] + (SEED_ROUTES if args.seeds else [])
    if not targets and not args.search:
        ap.error("nothing to record: pass --url, --search, --seeds or --list")

    secrets = load_secrets()
    written = 0
    with httpx.Client(timeout=60.0) as client:
        if targets:
            require(secrets, "FIRECRAWL_API_KEY")
            key = secrets.FIRECRAWL_API_KEY or ""
            print(f"Recording {len(targets)} page(s) through Firecrawl:")
            for url, note in targets:
                request, response = scrape(client, key, url)
                written += record(directory, request, response, note, force=args.force)
        if args.search:
            require(secrets, "EXA_API_KEY")
            key = secrets.EXA_API_KEY or ""
            print(f"Recording {len(args.search)} search(es) through Exa:")
            for query in args.search:
                request, response = search(client, key, query)
                note = f"search: {query}"
                written += record(directory, request, response, note, force=args.force)

    print(f"\n{written} new fixture(s). Commit them — they are the suite's ground truth.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
