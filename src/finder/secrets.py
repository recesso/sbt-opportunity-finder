"""Secrets loading and validation.

API keys come from the environment, never from files in the repo. A missing key
must fail at startup — not halfway through a run that has already spent money on
half a harvest.

    from finder.secrets import load_secrets, require
    sec = load_secrets()
    require(sec, "FIRECRAWL_API_KEY", "LLM_API_KEY")   # raises listing ALL missing

Every value loaded here is registered for log redaction (see finder.logging), so
a key can never appear in a log line even if something logs a whole request.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Values shorter than this are never redacted — redacting "1" or "" would blank
# out unrelated text and make logs useless.
MIN_REDACTABLE_LEN = 8


class MissingSecretError(Exception):
    """Raised at startup when a required key is absent. Names every missing key."""


@dataclass(frozen=True)
class Secrets:
    """Every external credential, loaded from the environment.

    Fields are optional at load time and checked by :func:`require` at the point
    of use, so a workflow that needs only Firecrawl does not fail because a
    reranker key is absent.
    """

    FIRECRAWL_API_KEY: str | None = None
    EXA_API_KEY: str | None = None
    LLM_API_KEY: str | None = None
    RERANK_API_KEY: str | None = None

    SHEET_BRIDGE_URL: str | None = None
    SHEET_BRIDGE_TOKEN: str | None = None
    SHEET_ID: str | None = None

    BRAVE_API_KEY: str | None = None
    APIFY_API_TOKEN: str | None = None

    def present(self) -> list[str]:
        return [f.name for f in fields(self) if getattr(self, f.name)]

    def missing(self, *names: str) -> list[str]:
        known = {f.name for f in fields(self)}
        unknown = [n for n in names if n not in known]
        if unknown:
            raise ValueError(f"unknown secret name(s): {unknown}; expected one of {sorted(known)}")
        return [n for n in names if not getattr(self, n)]

    def redactable_values(self) -> list[str]:
        """Values long enough to be worth redacting from log output."""
        return [
            v for f in fields(self) if (v := getattr(self, f.name)) and len(v) >= MIN_REDACTABLE_LEN
        ]


def _load_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env reader for local development.

    Deliberately not python-dotenv: this is a dozen lines, has no failure modes
    worth debugging, and keeps the dependency list honest.
    """
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if value:
            out[key.strip()] = value
    return out


def load_secrets(*, env_file: Path | None = None, environ: dict[str, str] | None = None) -> Secrets:
    """Read secrets from the environment, falling back to a local .env file.

    Real environment variables always win over the .env file, so a deployment
    never picks up a stale local value.
    """
    env = dict(environ if environ is not None else os.environ)
    dotenv = _load_dotenv(env_file if env_file is not None else REPO_ROOT / ".env")

    values = {}
    for f in fields(Secrets):
        values[f.name] = env.get(f.name) or dotenv.get(f.name) or None

    return Secrets(**values)


def require(secrets: Secrets, *names: str) -> None:
    """Fail at startup if any named secret is absent.

    Reports *every* missing key at once — discovering them one run at a time is
    how a simple setup problem becomes an afternoon.
    """
    missing = secrets.missing(*names)
    if missing:
        raise MissingSecretError(
            "missing required environment variable(s): "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill them in, or set them in the "
            "deployment environment."
        )
