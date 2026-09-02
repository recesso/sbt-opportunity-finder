"""Recorded provider responses, and the transport that replays them.

CI must never hit a live provider, and neither should a developer running the
suite. Recorded fixtures make every later story testable, deterministic and
free — and, for this system in particular, *stable*: every number it produces
comes from a page it read, so a test against a live page asserts whatever that
page says today.

A fixture is keyed by the request, not by a name someone chose:
``sha1(METHOD + url + body)``. Two consequences worth having:

* Changing the request changes the key, so a stale fixture surfaces as a MISS
  rather than as a silently wrong answer.
* Nobody has to invent names, and two people recording the same call produce
  the same file.

A miss raises with the exact command to record it. A replay transport that
returns 404 for a miss would let a test assert against a "page not found" it
never meant to exercise.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx

DEFAULT_DIR = Path("tests/fixtures/http")
SUFFIX = ".json"
_MAX_BODY_BYTES = 2_000_000


class FixtureMissing(Exception):
    """No recording for this request. Says how to make one."""

    def __init__(self, key: str, request: httpx.Request, directory: Path) -> None:
        super().__init__(
            f"No recorded response for {request.method} {request.url}\n"
            f"  expected: {directory / (key + SUFFIX)}\n"
            f"  record it: python scripts/record_fixtures.py --url {request.url}\n"
            "A recorded fixture is the ground truth for every assertion downstream, "
            "so a miss is a missing recording, never a 404 to assert against."
        )
        self.key = key
        self.request = request


def fixture_key(method: str, url: str, body: bytes = b"") -> str:
    """Keyed by the request itself, so a changed request cannot reuse a stale
    recording."""
    digest = hashlib.sha1(f"{method.upper()} {url}".encode() + b"\x1f" + body)
    return digest.hexdigest()


def key_for(request: httpx.Request) -> str:
    return fixture_key(request.method, str(request.url), request.content)


def save(
    directory: Path,
    request: httpx.Request,
    response: httpx.Response,
    *,
    note: str = "",
) -> Path:
    """Write one recording. Pretty-printed so a diff is readable in review."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (key_for(request) + SUFFIX)
    payload: dict[str, Any] = {
        "request": {
            "method": request.method,
            "url": str(request.url),
            # Headers are deliberately NOT recorded: they carry API keys.
            "body": request.content.decode("utf-8", "replace")[:_MAX_BODY_BYTES],
        },
        "response": {
            "status": response.status_code,
            "headers": {
                k: v
                for k, v in response.headers.items()
                if k.lower() in {"content-type", "content-encoding"}
            },
            "body": response.text[:_MAX_BODY_BYTES],
        },
        "note": note,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def load(directory: Path, key: str) -> dict[str, Any] | None:
    path = directory / (key + SUFFIX)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def replay_transport(directory: Path | str = DEFAULT_DIR) -> httpx.MockTransport:
    """An httpx transport that answers from recordings and refuses on a miss."""
    root = Path(directory)

    def handler(request: httpx.Request) -> httpx.Response:
        key = key_for(request)
        recorded = load(root, key)
        if recorded is None:
            raise FixtureMissing(key, request, root)
        response = recorded["response"]
        return httpx.Response(
            status_code=response["status"],
            headers=response.get("headers") or {},
            text=response.get("body", ""),
            request=request,
        )

    return httpx.MockTransport(handler)


def replay_client(directory: Path | str = DEFAULT_DIR, **kwargs: Any) -> httpx.Client:
    """A client that can only ever answer from recordings."""
    return httpx.Client(transport=replay_transport(directory), **kwargs)


def recorded_keys(directory: Path | str = DEFAULT_DIR) -> list[str]:
    root = Path(directory)
    if not root.exists():
        return []
    return sorted(p.name[: -len(SUFFIX)] for p in root.glob(f"*{SUFFIX}"))


def describe(directory: Path | str = DEFAULT_DIR) -> list[tuple[str, str]]:
    """``(method url, note)`` for every recording. For `make fixtures`."""
    root = Path(directory)
    out: list[tuple[str, str]] = []
    for key in recorded_keys(root):
        data = load(root, key) or {}
        req = data.get("request", {})
        out.append((f"{req.get('method', '?')} {req.get('url', '?')}", data.get("note", "")))
    return out
