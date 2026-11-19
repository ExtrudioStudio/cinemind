"""The Claude wrapper: what it sends, how failures map, and what it records."""

from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx
import pytest
from pydantic import BaseModel

from cinemind.ai.llm import FALLBACK_BETA, LLM, LLMError, LLMRefused, LLMUnavailable
from cinemind.core.config import Config
from cinemind.core.store import Store


class Answer(BaseModel):
    value: int


class FakeMessages:
    def __init__(self, outcome):
        self.outcome = outcome
        self.kwargs: dict = {}

    async def parse(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _client(outcome) -> SimpleNamespace:
    return SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages(outcome)))


SEVEN = Answer(value=7)


def _response(stop_reason="end_turn", parsed=SEVEN):
    usage = SimpleNamespace(input_tokens=1200, output_tokens=40, cache_read_input_tokens=1000,
                            cache_creation_input_tokens=0)  # fmt: skip
    resp = SimpleNamespace(stop_reason=stop_reason, parsed_output=parsed, usage=usage,
                           model="claude-opus-5-5")  # fmt: skip
    resp._request_id = "req_123"
    return resp


def _status_error(cls, code):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx.Response(code, request=request), body=None)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "state.db")
    yield s
    s.close()


async def test_a_good_answer_is_parsed_and_logged(store):
    client = _client(_response())
    llm = LLM(Config(llm_effort="low"), client, store)
    assert await llm.parse("test", "SYSTEM", "USER", Answer) == Answer(value=7)

    sent = client.beta.messages.kwargs
    assert sent["model"] == "claude-opus-5-5" and sent["output_format"] is Answer
    assert sent["output_config"] == {"effort": "low"}
    assert sent["system"] == [{"type": "text", "text": "SYSTEM",
                               "cache_control": {"type": "ephemeral"}}]  # fmt: skip
    assert sent["messages"] == [{"role": "user", "content": "USER"}]
    assert sent["betas"] == [FALLBACK_BETA] and sent["fallbacks"] == "default"
    (row,) = store.llm_calls()
    assert (row["purpose"], row["outcome"], row["input_tokens"], row["output_tokens"],
            row["cache_read_tokens"], row["request_id"]) == (
        "test", "end_turn", 1200, 40, 1000, "req_123")  # fmt: skip


@pytest.mark.parametrize(
    ("response", "error"),
    [(_response(stop_reason="refusal"), LLMRefused),
     (_response(stop_reason="max_tokens"), LLMError),
     (_response(parsed=None), LLMError)],
)  # fmt: skip
async def test_unusable_answers_raise(store, response, error):
    with pytest.raises(error):
        await LLM(Config(), _client(response), store).parse("t", "s", "u", Answer)
    assert store.llm_calls()[0]["outcome"] == response.stop_reason


@pytest.mark.parametrize(
    ("exc", "error", "outcome"),
    [(_status_error(anthropic.AuthenticationError, 401), LLMUnavailable, "auth_error"),
     (_status_error(anthropic.PermissionDeniedError, 403), LLMUnavailable, "permission_denied"),
     (_status_error(anthropic.NotFoundError, 404), LLMError, "not_found"),
     (_status_error(anthropic.RateLimitError, 429), LLMUnavailable, "rate_limited"),
     (_status_error(anthropic.BadRequestError, 400), LLMError, "bad_request"),
     (_status_error(anthropic.InternalServerError, 529), LLMUnavailable, "http_529"),
     (anthropic.APIConnectionError(request=httpx.Request("POST", "https://x")),
      LLMUnavailable, "connection_error"),
     (TypeError("Could not resolve authentication method. Expected one of api_key"),
      LLMUnavailable, "no_credentials")],
)  # fmt: skip
async def test_api_failures_map_to_errors_the_caller_can_handle(store, exc, error, outcome):
    with pytest.raises(error) as raised:
        await LLM(Config(), _client(exc), store).parse("t", "s", "u", Answer)
    assert type(raised.value) is error  # LLMUnavailable means "fall back", LLMError means "report"
    assert store.llm_calls()[0]["outcome"] == outcome


async def test_other_type_errors_are_bugs_and_propagate():
    with pytest.raises(TypeError, match="unexpected keyword"):
        await LLM(Config(), _client(TypeError("unexpected keyword"))).parse("t", "s", "u", Answer)


def test_from_config_respects_the_switch():
    assert LLM.from_config(Config(llm_enabled=False)) is None
    assert isinstance(LLM.from_config(Config()), LLM)  # credentials are checked on first use
