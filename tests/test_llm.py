"""The LLM boundary.

Same shape as fetch, map and search: one Protocol, one adapter, the suite
offline. What these tests protect is that the schema reaches the model as a
CONSTRAINT rather than a suggestion, that temperature is zero by default, and
that a truncated answer is recognisable as truncation rather than as a bad page.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from finder.acquire.providers.base import FetchError
from finder.acquire.providers.llm import (
    ANTHROPIC_VERSION,
    DEFAULT_BASE_URL,
    AnthropicLLM,
    Completion,
    LLMProvider,
)

MESSAGES = f"{DEFAULT_BASE_URL}/messages"
SCHEMA = {"type": "object", "properties": {"family": {"type": "string"}}, "required": ["family"]}


def body(*content: dict, stop_reason: str = "end_turn", **extra) -> dict:
    return {
        "model": "claude-sonnet-5",
        "content": list(content),
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 1200, "output_tokens": 340},
        **extra,
    }


def tool_use(payload: dict) -> dict:
    return {"type": "tool_use", "name": "record_extraction", "input": payload}


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


# --- the protocol ----------------------------------------------------------


def test_the_adapter_satisfies_the_protocol() -> None:
    assert isinstance(AnthropicLLM(api_key="k"), LLMProvider)


def test_an_llm_needs_a_key() -> None:
    with pytest.raises(ValueError, match="requires an API key"):
        AnthropicLLM(api_key="")


def test_an_empty_prompt_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="needs a prompt"):
        AnthropicLLM(api_key="k").complete(system="s", prompt="   ")


# --- schema as a constraint ------------------------------------------------


@respx.mock
def test_a_schema_is_forced_as_a_tool_not_suggested_in_the_prompt() -> None:
    """A schema pasted into a prompt is a request. A tool schema is a
    constraint, and the difference shows up as the extraction retry rate."""
    route = respx.post(MESSAGES).mock(
        return_value=httpx.Response(200, json=body(tool_use({"family": "ROOM"})))
    )
    result = AnthropicLLM(api_key="k").complete(system="sys", prompt="read this", schema=SCHEMA)

    payload = json.loads(route.calls[0].request.content)
    assert payload["tools"][0]["input_schema"] == SCHEMA
    assert payload["tool_choice"] == {"type": "tool", "name": "record_extraction"}
    assert json.loads(result.text) == {"family": "ROOM"}


@respx.mock
def test_without_a_schema_the_text_block_is_the_answer() -> None:
    respx.post(MESSAGES).mock(return_value=httpx.Response(200, json=body(text_block("hello"))))
    assert AnthropicLLM(api_key="k").complete(system="s", prompt="p").text == "hello"


@respx.mock
def test_prose_where_a_record_was_forced_is_not_accepted() -> None:
    """Falling back to the text block when a tool was forced would quietly take
    an explanation in place of the record that was required."""
    respx.post(MESSAGES).mock(
        return_value=httpx.Response(200, json=body(text_block("I could not find a form.")))
    )
    with pytest.raises(FetchError, match="empty response"):
        AnthropicLLM(api_key="k").complete(system="s", prompt="p", schema=SCHEMA)


@respx.mock
def test_the_request_is_deterministic_by_default() -> None:
    """This step reads what a page says. Creativity here is indistinguishable
    from fabrication."""
    route = respx.post(MESSAGES).mock(return_value=httpx.Response(200, json=body(text_block("x"))))
    AnthropicLLM(api_key="secret").complete(system="sys", prompt="p", max_tokens=500)

    request = route.calls[0].request
    payload = json.loads(request.content)
    assert payload["temperature"] == 0.0
    assert payload["max_tokens"] == 500
    assert payload["system"] == "sys"
    assert payload["messages"] == [{"role": "user", "content": "p"}]
    assert request.headers["x-api-key"] == "secret"
    assert request.headers["anthropic-version"] == ANTHROPIC_VERSION


# --- provenance ------------------------------------------------------------


@respx.mock
def test_the_completion_carries_what_produced_it() -> None:
    """Every extraction records which model answered. A regression six weeks
    from now has to be attributable to something."""
    respx.post(MESSAGES).mock(return_value=httpx.Response(200, json=body(text_block("x"))))
    result = AnthropicLLM(api_key="k").complete(system="s", prompt="p")

    assert result.model == "claude-sonnet-5"
    assert result.provider == "anthropic"
    assert (result.input_tokens, result.output_tokens) == (1200, 340)


@respx.mock
def test_a_truncated_answer_is_recognisable_as_truncation() -> None:
    """Half a JSON object fails validation for a reason that has nothing to do
    with the page. Without this flag it looks like a bad page."""
    respx.post(MESSAGES).mock(
        return_value=httpx.Response(
            200, json=body(text_block('{"family": "RO'), stop_reason="max_tokens")
        )
    )
    assert AnthropicLLM(api_key="k").complete(system="s", prompt="p").truncated is True


def test_a_complete_answer_is_not_flagged_as_truncated() -> None:
    assert Completion(text="x", model="m", stop_reason="end_turn").truncated is False


# --- failures --------------------------------------------------------------


@respx.mock
@pytest.mark.parametrize(("status", "retryable"), [(429, True), (529, False), (401, False)])
def test_failures_say_whether_retrying_helps(status: int, retryable: bool) -> None:
    respx.post(MESSAGES).mock(return_value=httpx.Response(status))
    with pytest.raises(FetchError) as exc:
        AnthropicLLM(api_key="k").complete(system="s", prompt="p")
    assert exc.value.retryable is retryable


@respx.mock
def test_a_model_timeout_is_retryable() -> None:
    respx.post(MESSAGES).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(FetchError, match="timed out") as exc:
        AnthropicLLM(api_key="k").complete(system="s", prompt="p")
    assert exc.value.retryable is True


@respx.mock
def test_a_transport_error_is_not_retryable() -> None:
    respx.post(MESSAGES).mock(side_effect=httpx.ConnectError("no route"))
    with pytest.raises(FetchError, match="transport error"):
        AnthropicLLM(api_key="k").complete(system="s", prompt="p")


@respx.mock
@pytest.mark.parametrize(
    "payload", [{"content": []}, {"content": "not a list"}, {"content": [{"type": "thinking"}]}]
)
def test_an_empty_answer_is_an_error_not_an_empty_record(payload: dict) -> None:
    """An empty extraction would read as 'the page states nothing', which is a
    lie about a call that failed — the same rule as an empty fetch."""
    respx.post(MESSAGES).mock(return_value=httpx.Response(200, json=payload))
    with pytest.raises(FetchError, match="empty response"):
        AnthropicLLM(api_key="k").complete(system="s", prompt="p")


@respx.mock
def test_a_malformed_content_block_is_skipped_not_fatal() -> None:
    """One odd block among several must not cost the answer sitting next to it."""
    respx.post(MESSAGES).mock(
        return_value=httpx.Response(200, json=body("not a dict", text_block("the answer")))
    )
    assert AnthropicLLM(api_key="k").complete(system="s", prompt="p").text == "the answer"


@respx.mock
def test_a_non_json_response_is_a_clear_error() -> None:
    respx.post(MESSAGES).mock(return_value=httpx.Response(200, text="<html>502</html>"))
    with pytest.raises(FetchError, match="not JSON"):
        AnthropicLLM(api_key="k").complete(system="s", prompt="p")


@respx.mock
def test_a_json_array_response_is_a_clear_error() -> None:
    respx.post(MESSAGES).mock(return_value=httpx.Response(200, json=["nope"]))
    with pytest.raises(FetchError, match="no response object"):
        AnthropicLLM(api_key="k").complete(system="s", prompt="p")


@respx.mock
def test_calls_are_counted() -> None:
    respx.post(MESSAGES).mock(return_value=httpx.Response(200, json=body(text_block("x"))))
    llm = AnthropicLLM(api_key="k")
    llm.complete(system="s", prompt="p")
    llm.complete(system="s", prompt="p")
    assert llm.calls == 2


def test_the_llm_client_can_be_closed() -> None:
    client = httpx.Client()
    AnthropicLLM(api_key="k", client=client).close()
    assert client.is_closed
