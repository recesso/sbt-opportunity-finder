"""E0.S3 — secrets loading and log redaction.

Two requirements, both tested here:
  1. A missing key fails at startup naming EVERY missing key at once.
  2. No secret value can appear in a log line, even nested inside a dict or a
     URL, and even if something logs an entire request object.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import structlog

from finder.logging import REDACTED, redaction_processor, register_secrets
from finder.secrets import (
    MissingSecretError,
    Secrets,
    load_secrets,
    require,
)

FAKE_KEY = "fc-live-abcdef0123456789deadbeef"
FAKE_TOKEN = "AKfycbSUPERSECRETtokenvalue1234"


@pytest.fixture
def secrets() -> Secrets:
    s = Secrets(FIRECRAWL_API_KEY=FAKE_KEY, SHEET_BRIDGE_TOKEN=FAKE_TOKEN)
    register_secrets(s)
    return s


@pytest.fixture(autouse=True)
def _clear_registry():
    yield
    register_secrets(Secrets())


# --- loading ---------------------------------------------------------------


def test_loads_from_environment() -> None:
    s = load_secrets(environ={"EXA_API_KEY": "exa-123"}, env_file=Path("/nonexistent"))
    assert s.EXA_API_KEY == "exa-123"
    assert s.LLM_API_KEY is None


def test_loads_from_dotenv(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        '# a comment\n\nLLM_API_KEY=sk-from-dotenv\nEMPTY=\nQUOTED="q-value"\n',
        encoding="utf-8",
    )
    s = load_secrets(environ={}, env_file=env)
    assert s.LLM_API_KEY == "sk-from-dotenv"


def test_real_environment_beats_dotenv(tmp_path: Path) -> None:
    """A deployment must never pick up a stale local value."""
    env = tmp_path / ".env"
    env.write_text("LLM_API_KEY=stale-local\n", encoding="utf-8")
    s = load_secrets(environ={"LLM_API_KEY": "real-deployed"}, env_file=env)
    assert s.LLM_API_KEY == "real-deployed"


def test_present_lists_only_populated() -> None:
    s = Secrets(FIRECRAWL_API_KEY="a", EXA_API_KEY="b")
    assert set(s.present()) == {"FIRECRAWL_API_KEY", "EXA_API_KEY"}


# --- require ---------------------------------------------------------------


def test_require_passes_when_present() -> None:
    require(Secrets(FIRECRAWL_API_KEY="x", LLM_API_KEY="y"), "FIRECRAWL_API_KEY", "LLM_API_KEY")


def test_require_reports_every_missing_key_at_once() -> None:
    """Discovering missing keys one run at a time is how a setup problem
    becomes an afternoon."""
    s = Secrets(FIRECRAWL_API_KEY="x")
    with pytest.raises(MissingSecretError) as exc:
        require(s, "FIRECRAWL_API_KEY", "EXA_API_KEY", "LLM_API_KEY", "RERANK_API_KEY")
    message = str(exc.value)
    assert "EXA_API_KEY" in message
    assert "LLM_API_KEY" in message
    assert "RERANK_API_KEY" in message
    assert "FIRECRAWL_API_KEY" not in message


def test_require_rejects_unknown_secret_name() -> None:
    with pytest.raises(ValueError, match="unknown secret name"):
        require(Secrets(), "NOT_A_REAL_KEY")


# --- redaction -------------------------------------------------------------


def test_redacts_a_bare_value(secrets: Secrets) -> None:
    out = redaction_processor(None, "info", {"event": "call", "key": FAKE_KEY})
    assert FAKE_KEY not in json.dumps(out)
    assert out["key"] == REDACTED


def test_redacts_inside_a_url(secrets: Secrets) -> None:
    """The bridge takes its token as a query parameter, so a logged URL is the
    most likely way a secret escapes."""
    url = f"https://script.google.com/macros/s/x/exec?token={FAKE_TOKEN}&action=meta"
    out = redaction_processor(None, "info", {"event": "fetch", "url": url})
    assert FAKE_TOKEN not in json.dumps(out)
    assert REDACTED in out["url"]
    assert "action=meta" in out["url"], "redaction must not destroy the rest of the line"


def test_redacts_nested_structures(secrets: Secrets) -> None:
    payload = {
        "event": "request",
        "headers": {"Authorization": f"Bearer {FAKE_KEY}"},
        "items": [{"token": FAKE_TOKEN}, "clean"],
        "tup": (FAKE_KEY, "clean"),
    }
    out = redaction_processor(None, "info", payload)
    blob = json.dumps(out, default=str)
    assert FAKE_KEY not in blob
    assert FAKE_TOKEN not in blob
    assert "clean" in blob


def test_short_values_are_not_redacted() -> None:
    """Redacting a 1-character value would blank unrelated text."""
    register_secrets(Secrets(FIRECRAWL_API_KEY="ab"))
    out = redaction_processor(None, "info", {"event": "abracadabra"})
    assert out["event"] == "abracadabra"


def test_no_registered_secrets_is_a_passthrough() -> None:
    register_secrets(Secrets())
    payload = {"event": "hello", "n": 1}
    assert redaction_processor(None, "info", payload) == payload


def test_end_to_end_through_structlog(secrets: Secrets, capsys) -> None:
    """The real path: a bound logger writing JSON must not leak."""
    structlog.configure(
        processors=[redaction_processor, structlog.processors.JSONRenderer()],
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
    log = structlog.get_logger("test").bind(run_id="run-123")
    log.info("fetched", url=f"https://x/exec?token={FAKE_TOKEN}", key=FAKE_KEY)

    captured = capsys.readouterr().out
    assert FAKE_TOKEN not in captured
    assert FAKE_KEY not in captured
    assert REDACTED in captured
    assert "run-123" in captured, "run_id must survive redaction"
