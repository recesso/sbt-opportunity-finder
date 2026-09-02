"""E2.S7 — recorded fixtures and the network guard.

Acceptance: the suite cannot reach a live provider, and a fixture miss says how
to record one.

Two properties matter more than the rest. **A recorded request never carries its
headers**, because that is where the API key lives and a fixture goes into git.
And **a miss raises rather than 404s**: a replay transport that invents a "not
found" would let a test assert against a page it never meant to exercise, which
is precisely the class of quiet wrongness this project keeps hunting.
"""

from __future__ import annotations

import socket
from pathlib import Path

import httpx
import pytest

from finder.acquire import replay
from tests.conftest import NetworkAccessDenied


@pytest.fixture
def fixtures(tmp_path: Path) -> Path:
    return tmp_path / "http"


def a_request(url: str = "https://api.firecrawl.dev/v2/scrape", **kw) -> httpx.Request:
    return httpx.Request(kw.pop("method", "POST"), url, **kw)


# --- the network guard -----------------------------------------------------


def test_an_outbound_connection_is_refused() -> None:
    """Not 'should not' — cannot. Every number this system produces comes from a
    page it read, so a test that quietly hit the real gsae.org would assert
    against whatever gsae.org says today."""
    with pytest.raises(NetworkAccessDenied) as exc:
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("example.com", 80))

    message = str(exc.value)
    assert "record_fixtures.py" in message, "the error carries the fix"
    assert "@pytest.mark.network" in message
    assert "Do not reach for the marker" in message


def test_an_http_client_cannot_reach_out() -> None:
    """The guard is at the socket, so it holds however the caller gets there."""
    with pytest.raises(Exception) as exc:
        httpx.Client(timeout=1.0).get("https://example.com/")
    assert "NetworkAccessDenied" in repr(exc.value) or isinstance(
        exc.value, NetworkAccessDenied | httpx.ConnectError
    )


def test_loopback_is_left_alone() -> None:
    """A test talking to a local server is not reaching the internet, and
    blocking it would only push people to mark tests `network` wrongly."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(server.getsockname())  # not refused


@pytest.mark.network
def test_a_marked_test_is_not_patched() -> None:
    """The marker exists for a human running one deliberately. It is excluded
    from CI, so it cannot be used to make a failure quietly go away."""
    assert socket.socket.connect.__qualname__ != "no_network.<locals>.guarded_connect"


def test_the_network_marker_is_registered() -> None:
    """An unregistered marker is a typo away from silently doing nothing, and
    --strict-markers is what makes that impossible."""
    import tomllib

    config = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    pytest_config = config["tool"]["pytest"]["ini_options"]
    assert any(m.startswith("network:") for m in pytest_config["markers"])
    assert "--strict-markers" in pytest_config["addopts"]


# --- fixture keys ----------------------------------------------------------


def test_the_key_is_the_request_itself() -> None:
    """Keyed by the request so nobody invents names and two people recording the
    same call produce the same file."""
    key = replay.fixture_key("POST", "https://x/scrape", b'{"url":"a"}')
    assert key == replay.fixture_key("post", "https://x/scrape", b'{"url":"a"}')
    assert len(key) == 40


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("GET", "https://x/scrape", b'{"url":"a"}'),
        ("POST", "https://x/map", b'{"url":"a"}'),
        ("POST", "https://x/scrape", b'{"url":"b"}'),
    ],
)
def test_a_changed_request_is_a_different_key(method: str, url: str, body: bytes) -> None:
    """A stale fixture surfaces as a MISS rather than as a silently wrong answer."""
    base = replay.fixture_key("POST", "https://x/scrape", b'{"url":"a"}')
    assert replay.fixture_key(method, url, body) != base


# --- recording -------------------------------------------------------------


def test_a_recording_round_trips(fixtures: Path) -> None:
    request = a_request(json={"url": "https://gsae.org/"})
    response = httpx.Response(200, json={"success": True}, request=request)

    path = replay.save(fixtures, request, response, note="GSAE")

    assert path.parent == fixtures
    data = replay.load(fixtures, replay.key_for(request))
    assert data["response"]["status"] == 200
    assert data["note"] == "GSAE"
    assert data["request"]["url"] == str(request.url)


def test_a_recording_never_carries_the_api_key(fixtures: Path) -> None:
    """Fixtures go into git. This is the assertion that keeps a key out of it."""
    request = a_request(
        json={"url": "https://gsae.org/"},
        headers={"Authorization": "Bearer sk-live-SECRET", "x-api-key": "SECRET"},
    )
    path = replay.save(fixtures, request, httpx.Response(200, text="ok", request=request))

    written = path.read_text(encoding="utf-8")
    assert "SECRET" not in written
    assert "Authorization" not in written
    assert "headers" not in replay.load(fixtures, replay.key_for(request))["request"]


def test_a_missing_recording_reads_as_none(fixtures: Path) -> None:
    assert replay.load(fixtures, "0" * 40) is None


def test_recordings_are_listable(fixtures: Path) -> None:
    for url in ("https://a/x", "https://b/y"):
        request = a_request(url)
        replay.save(fixtures, request, httpx.Response(200, request=request), note=f"note {url}")

    assert len(replay.recorded_keys(fixtures)) == 2
    described = replay.describe(fixtures)
    assert {note for _, note in described} == {"note https://a/x", "note https://b/y"}


def test_listing_an_empty_directory_is_not_an_error(tmp_path: Path) -> None:
    assert replay.recorded_keys(tmp_path / "never-written") == []
    assert replay.describe(tmp_path / "never-written") == []


# --- replay ----------------------------------------------------------------


def test_a_recorded_call_replays_offline(fixtures: Path) -> None:
    request = a_request(json={"url": "https://gsae.org/"})
    replay.save(
        fixtures,
        request,
        httpx.Response(
            200,
            json={"success": True, "data": {"markdown": "# Speaker Interest"}},
            request=request,
        ),
    )

    with replay.replay_client(fixtures) as client:
        response = client.post(str(request.url), json={"url": "https://gsae.org/"})

    assert response.status_code == 200
    assert response.json()["data"]["markdown"] == "# Speaker Interest"


def test_a_miss_raises_with_the_command_to_record_it(fixtures: Path) -> None:
    """A transport that returned 404 on a miss would let a test assert against a
    'page not found' it never meant to exercise."""
    with pytest.raises(replay.FixtureMissing) as exc, replay.replay_client(fixtures) as client:
        client.post("https://api.firecrawl.dev/v2/scrape", json={"url": "https://new.org/"})

    message = str(exc.value)
    assert "record_fixtures.py --url" in message
    assert "https://api.firecrawl.dev/v2/scrape" in message
    assert "never a 404 to assert against" in message


def test_a_different_request_body_does_not_reuse_a_recording(fixtures: Path) -> None:
    """Two scrapes of different URLs hit the same endpoint. If the body were not
    in the key they would share a recording, and the second would silently
    receive an answer to a question it never asked."""
    endpoint = "https://api.firecrawl.dev/v2/scrape"
    recorded = a_request(endpoint, json={"url": "https://gsae.org/"})
    replay.save(fixtures, recorded, httpx.Response(200, text="gsae", request=recorded))

    with replay.replay_client(fixtures) as client:
        assert client.post(endpoint, json={"url": "https://gsae.org/"}).text == "gsae"
        with pytest.raises(replay.FixtureMissing):
            client.post(endpoint, json={"url": "https://gamep.org/"})


def test_a_recorded_error_response_replays_as_that_error(fixtures: Path) -> None:
    """A recorded 429 is a real recording of how the provider behaves, and the
    retry path deserves to be tested against it."""
    request = a_request(json={"url": "https://blocked.org/"})
    replay.save(fixtures, request, httpx.Response(429, text="slow down", request=request))

    with replay.replay_client(fixtures) as client:
        response = client.post(str(request.url), json={"url": "https://blocked.org/"})
    assert response.status_code == 429


def test_the_firecrawl_adapter_works_against_a_recording(fixtures: Path) -> None:
    """End to end: the real adapter, a recorded response, no network at all."""
    from finder.acquire.providers.firecrawl import DEFAULT_BASE_URL, FirecrawlFetch

    payload = {
        "url": "https://gsae.org/speaker-interest-form",
        "formats": ["markdown", "links"],
        "onlyMainContent": True,
    }
    request = httpx.Request("POST", f"{DEFAULT_BASE_URL}/scrape", json=payload)
    replay.save(
        fixtures,
        request,
        httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "markdown": "# Speaker Interest\n\nSubmit through the form.",
                    "links": ["https://www.surveymonkey.com/r/NKSQCY6"],
                    "metadata": {"sourceURL": payload["url"], "statusCode": 200},
                },
            },
            request=request,
        ),
        note="GSAE — off-domain SurveyMonkey form",
    )

    provider = FirecrawlFetch(api_key="not-used", client=replay.replay_client(fixtures))
    snapshot = provider.fetch(payload["url"])

    assert "Speaker Interest" in snapshot.markdown
    assert snapshot.links == ("https://www.surveymonkey.com/r/NKSQCY6",)
