"""A local model through Ollama, with the same ``parse`` contract as :class:`cinemind.ai.llm.LLM`.

Free, offline and private: nothing leaves the machine. The answer's JSON schema goes to
Ollama's structured-output ``format`` option, and the reply is validated against the same
pydantic model, so callers can't tell the backends apart. Sampling is greedy (temperature 0),
so the same request gets the same answer.

Errors map like the Anthropic wrapper's: Ollama not running, or the model not pulled yet, is
:class:`LLMUnavailable` (callers fall back to the deterministic path); a reply that is cut off
or doesn't fit the schema is :class:`LLMError`. Calls are logged to ``llm_calls`` as
``ollama/<model>``.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from cinemind.ai.llm import CallRecord, LLMError, LLMUnavailable
from cinemind.core.store import Store


class OllamaLLM:
    def __init__(self, url: str, model: str, store: Store | None = None, timeout: float = 120.0,
                 transport: httpx.AsyncBaseTransport | None = None):  # fmt: skip
        self.url = url.rstrip("/")
        self.model = model
        self.store = store
        self.timeout = timeout
        self._transport = transport  # tests

    @property
    def label(self) -> str:
        return f"ollama/{self.model}"

    async def parse[T: BaseModel](
        self, purpose: str, system: str, user: str, output: type[T], max_tokens: int = 2048,
        *, calls: list[CallRecord] | None = None,
    ) -> T:  # fmt: skip
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "format": output.model_json_schema(),
            "options": {"temperature": 0, "num_predict": max_tokens},
        }
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as http:
                response = await http.post(f"{self.url}/api/chat", json=body)
        except httpx.TimeoutException as exc:
            err: LLMError = LLMUnavailable(f"Ollama at {self.url} didn't answer in time.")
            raise self._failed(purpose, started, "timeout", err) from exc
        except httpx.TransportError as exc:
            err = LLMUnavailable(f"Ollama isn't running at {self.url}. Start it, or change "
                                 "judge_provider.")  # fmt: skip
            raise self._failed(purpose, started, "connection_error", err) from exc
        if response.status_code == 404:
            err = LLMUnavailable(f"Ollama has no model {self.model!r}: run "
                                 f"`ollama pull {self.model}`.")  # fmt: skip
            raise self._failed(purpose, started, "not_found", err)
        if response.status_code >= 400:
            err = LLMError(f"Ollama returned HTTP {response.status_code}: {response.text[:200]}")
            raise self._failed(purpose, started, f"http_{response.status_code}", err)

        data: dict[str, Any] = response.json()
        text = str((data.get("message") or {}).get("content") or "")
        stop = str(data.get("done_reason") or "stop")
        self._log(purpose, started, stop, data)
        if calls is not None:
            served = f"ollama/{data['model']}" if data.get("model") else self.label
            calls.append(CallRecord(
                model=served, system=system, user=user, text=text, stop_reason=stop,
                input_tokens=int(data.get("prompt_eval_count") or 0),
                output_tokens=int(data.get("eval_count") or 0),
                latency_s=round(time.monotonic() - started, 3)))  # fmt: skip
        if stop == "length":
            raise LLMError("The local model's answer was cut off.")
        try:
            return output.model_validate_json(text)
        except ValidationError as exc:
            raise LLMError("The local model didn't answer in the expected shape.") from exc

    def _failed(self, purpose: str, started: float, outcome: str, error: LLMError) -> LLMError:
        self._log(purpose, started, outcome, {})
        return error

    def _log(self, purpose: str, started: float, outcome: str, data: dict[str, Any]) -> None:
        if self.store is None:
            return
        self.store.log_llm_call(
            purpose=purpose, model=self.label, outcome=outcome,
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
            latency_ms=int((time.monotonic() - started) * 1000),
        )  # fmt: skip
