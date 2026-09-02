"""Test-suite guarantees that hold for every test in this repository.

**The suite never touches the network.** Not "should not" — cannot. An autouse
fixture replaces socket connection with a raising stub, so a test that reaches
for a live provider fails loudly instead of passing slowly, costing money, and
depending on a page somebody else can change.

That matters more here than in most projects. Every number this system produces
comes from a page it read, so a test that quietly hit the real gsae.org would be
asserting against whatever gsae.org says today. The fixtures are the ground
truth, and they only stay ground truth if nothing can bypass them.

A test that genuinely needs the network marks itself ``@pytest.mark.network``.
Those are excluded from CI by the Makefile; the marker is for a human running
one deliberately, not a way to opt out of the rule.

Recording a fixture is a separate, explicit act::

    python scripts/record_fixtures.py --url https://gsae.org/speaker-interest-form
"""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest

HTTP_FIXTURES = "tests/fixtures/http"


class NetworkAccessDenied(RuntimeError):
    """A test tried to open a socket.

    The message carries the fix, because the person seeing this is usually
    surprised and the useful reply is 'here is how to record it'.
    """

    def __init__(self, address: object) -> None:
        super().__init__(
            f"This test tried to open a network connection to {address!r}.\n"
            "The suite runs offline so it is deterministic and free.\n"
            "  • Mock the provider (see tests/test_fetch.py for the pattern), or\n"
            f"  • record a fixture:  python scripts/record_fixtures.py --url <URL>\n"
            f"    (fixtures live in {HTTP_FIXTURES}/), or\n"
            "  • mark the test @pytest.mark.network if it must hit a live service.\n"
            "Do not reach for the marker to make a failure go away."
        )


@pytest.fixture(autouse=True)
def no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Refuse outbound connections unless the test is marked ``network``.

    Patches ``socket.socket.connect`` rather than the whole socket module:
    creating a socket is harmless, and libraries create them for reasons that
    never reach the wire. Connecting is the act worth refusing.

    Loopback is left alone. A test that starts a local server and talks to it is
    not reaching the internet, and blocking that would only push people towards
    marking tests ``network`` for the wrong reason.
    """
    if request.node.get_closest_marker("network"):
        yield
        return

    real_connect = socket.socket.connect

    def guarded_connect(self: socket.socket, address, *args, **kwargs):  # type: ignore[no-untyped-def]
        host = address[0] if isinstance(address, tuple) and address else address
        if host in ("127.0.0.1", "::1", "localhost"):
            return real_connect(self, address, *args, **kwargs)
        raise NetworkAccessDenied(address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    yield
