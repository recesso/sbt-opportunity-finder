"""LLM provider — the boundary the extractor speaks through.

Same shape as fetch, map and search: one Protocol, one adapter per vendor, the
suite offline against a fake. Nothing above this line names a model.

Two things this boundary enforces rather than requests:

* **Schema-forced output.** The caller supplies a JSON Schema and the adapter
  makes it a hard constraint on the response, not a suggestion in a prompt.
* **Determinism where it is available.** Temperature 0 by default. The
  extraction step is trying to read what a page says, and creativity there is
  indistinguishable from fabrication.

The response carries ``model`` and ``prompt_version`` back out, because every
extraction records which model and which prompt produced it. A regression six
weeks from now has to be attributable to something.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx

from finder.acquire.providers.base import FetchError

DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_TIMEOUT_S = 120.0
DEFAULT_MAX_TOKENS = 8000
ANTHROPIC_VERSION = "2023-06-01"
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class Completion:
    """One model response, and enough provenance to attribute it later."""

    text: str
    model: str
    provider: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def truncated(self) -> bool:
        """A response cut off at the token limit is not a short answer.

        It is half an answer, and half a JSON object will fail schema validation
        for a reason that has nothing to do with the page.
        """
        return self.stop_reason == "max_tokens"


@runtime_checkable
class LLMProvider(Protocol):
    """Structured completion. The only thing the extractor asks for."""

    name: str

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> Completion:
        """Return the model's answer, or raise :class:`FetchError`."""
        ...


class AnthropicLLM:
    """Anthropic adapter. The only file that names this vendor.

    Uses a tool definition to force the JSON Schema. A schema stated in the
    prompt is a request; a tool schema is a constraint, and the difference shows
    up as the extraction retry rate.
    """

    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        client: httpx.Client | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        temperature: float = 0.0,
        cost_per_call_usd: float = 0.0,
    ) -> None:
        if not api_key:
            raise ValueError("AnthropicLLM requires an API key; see finder.secrets")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.temperature = temperature
        self.cost_per_call_usd = cost_per_call_usd
        self._client = client or httpx.Client(timeout=timeout_s)
        self.calls = 0

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> Completion:
        if not prompt.strip():
            raise ValueError("the model needs a prompt")

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": self.temperature,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        if schema is not None:
            # A tool schema is a constraint. A schema pasted into the prompt is
            # a request, and the difference shows up as the retry rate.
            payload["tools"] = [
                {
                    "name": "record_extraction",
                    "description": "Record the extraction. Every field is required.",
                    "input_schema": schema,
                }
            ]
            payload["tool_choice"] = {"type": "tool", "name": "record_extraction"}

        body = self._post(payload)
        return self._to_completion(body, forced_tool=schema is not None)

    # --- transport --------------------------------------------------------

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        try:
            response = self._client.post(
                f"{self.base_url}/messages",
                json=payload,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
                timeout=self.timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise FetchError("the model timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise FetchError(f"transport error calling the model: {exc}") from exc

        if response.status_code >= 400:
            raise FetchError(
                f"the model returned {response.status_code}",
                status=response.status_code,
                retryable=response.status_code in RETRYABLE_STATUS,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise FetchError("the model's response is not JSON") from exc
        if not isinstance(body, dict):
            raise FetchError("the model returned no response object")
        return body

    def _to_completion(self, body: dict[str, Any], *, forced_tool: bool) -> Completion:
        text = _first_content(body.get("content"), forced_tool=forced_tool)
        if not text:
            raise FetchError(
                "the model returned an empty response. An empty extraction would read "
                "as 'the page states nothing', which is a lie about a call that failed."
            )
        usage = body.get("usage") or {}
        return Completion(
            text=text,
            model=str(body.get("model") or self.model),
            provider=self.name,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            stop_reason=str(body.get("stop_reason") or ""),
            raw=body,
        )

    def close(self) -> None:
        self._client.close()


def _first_content(content: Any, *, forced_tool: bool) -> str:
    """The answer, from whichever block carries it.

    With a forced tool the answer is the tool input, serialised. Without one it
    is the text block. Falling back to text when a tool was forced would quietly
    accept prose where a record was required.
    """
    import json

    if not isinstance(content, list):
        return ""
    for block in content:
        if not isinstance(block, dict):
            continue
        if forced_tool and block.get("type") == "tool_use":
            return json.dumps(block.get("input") or {})
        if not forced_tool and block.get("type") == "text":
            return str(block.get("text") or "")
    return ""
