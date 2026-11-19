"""The one way cinemind calls Claude: structured output, mapped errors, logged usage.

Every LLM call goes through :meth:`LLM.parse`:

- **Structured output.** The answer is validated against a pydantic model, so callers get typed
  data or an error, never free text to scrape.
- **Cacheable prompt.** The fixed system prompt comes first and carries ``cache_control``; the
  variable request comes last.
- **Refusal fallback.** The server-side ``fallbacks`` option (beta) lets the API re-run a
  declined request on a fallback model inside the same call.
- **Mapped errors.** No credentials, rate limits, outages and lost connections become
  :class:`LLMUnavailable`, so a caller can fall back to the deterministic path. Refusals and
  cut-off answers have their own errors.
- **Audit.** Model, tokens (including cache reads and writes), latency, outcome and request id
  go to the store's ``llm_calls`` table.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from pydantic import BaseModel

from cinemind.core.config import Config
from cinemind.core.store import Store

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
_NO_CREDENTIALS = "Could not resolve authentication method"
SETUP_HINT = (
    "Install the AI extra (pip install 'cinemind[ai]') and set ANTHROPIC_API_KEY, "
    "or run `ant auth login`."
)


class LLMError(Exception):
    """The model call failed in a way worth reporting to the user."""


class LLMUnavailable(LLMError):
    """No usable model right now: no credentials, rate limited, offline or an outage."""


class LLMRefused(LLMError):
    """The model (and its fallback) declined the request."""


class LLM:
    def __init__(self, cfg: Config, client: Any, store: Store | None = None):
        self.cfg = cfg
        self.client = client
        self.store = store

    @classmethod
    def from_config(cls, cfg: Config, store: Store | None = None) -> LLM | None:
        """An LLM when the feature is on and the SDK is installed; None otherwise.

        Credentials are only checked by the first request (the SDK also reads `ant auth`
        profiles, so their absence can't be known up front).
        """
        if not cfg.llm_enabled:
            return None
        try:
            import anthropic
        except ImportError:
            return None
        try:
            client = anthropic.AsyncAnthropic(max_retries=2, timeout=60.0)
        except anthropic.AnthropicError as exc:  # e.g. a configured profile that doesn't exist
            log.warning("Anthropic client unavailable: %s", exc)
            return None
        return cls(cfg, client, store)

    async def parse[T: BaseModel](
        self, purpose: str, system: str, user: str, output: type[T], max_tokens: int = 2048
    ) -> T:
        """Ask for an answer shaped like ``output``. ``purpose`` labels the call in the log."""
        import anthropic

        started = time.monotonic()
        try:
            response = await self.client.beta.messages.parse(
                model=self.cfg.llm_model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_format=output,
                output_config={"effort": self.cfg.llm_effort},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        # Most specific first: the status-code subclasses before APIStatusError.
        except anthropic.AuthenticationError as exc:
            err: LLMError = LLMUnavailable(f"The API rejected the credentials. {SETUP_HINT}")
            raise self._failed(purpose, started, "auth_error", err) from exc
        except anthropic.PermissionDeniedError as exc:
            err = LLMUnavailable(f"These credentials can't use {self.cfg.llm_model}.")
            raise self._failed(purpose, started, "permission_denied", err) from exc
        except anthropic.NotFoundError as exc:
            err = LLMError(f"Unknown model {self.cfg.llm_model!r} (setting llm_model).")
            raise self._failed(purpose, started, "not_found", err) from exc
        except anthropic.RateLimitError as exc:
            err = LLMUnavailable("The Anthropic API is rate limiting requests; try again soon.")
            raise self._failed(purpose, started, "rate_limited", err) from exc
        except anthropic.BadRequestError as exc:
            err = LLMError(f"The request was rejected: {exc.message}")
            raise self._failed(purpose, started, "bad_request", err) from exc
        except anthropic.APIStatusError as exc:
            err = LLMUnavailable(f"The Anthropic API returned HTTP {exc.status_code}; try later.")
            raise self._failed(purpose, started, f"http_{exc.status_code}", err) from exc
        except anthropic.APIConnectionError as exc:
            err = LLMUnavailable("Could not reach the Anthropic API.")
            raise self._failed(purpose, started, "connection_error", err) from exc
        except TypeError as exc:
            if _NO_CREDENTIALS not in str(exc):
                raise
            err = LLMUnavailable(f"No Anthropic API credentials found. {SETUP_HINT}")
            raise self._failed(purpose, started, "no_credentials", err) from exc

        self._record(purpose, started, response.stop_reason or "unknown", response)
        if response.stop_reason == "refusal":
            raise LLMRefused("The model declined this request.")
        if response.stop_reason == "max_tokens":
            raise LLMError("The model's answer was cut off.")
        parsed: T | None = response.parsed_output
        if parsed is None:
            raise LLMError("The model didn't return an answer in the expected shape.")
        return parsed

    # -- bookkeeping -----------------------------------------------------------------------

    def _failed(self, purpose: str, started: float, outcome: str, error: LLMError) -> LLMError:
        self._record(purpose, started, outcome)
        log.warning("LLM call %s failed: %s", purpose, error)
        return error

    def _record(self, purpose: str, started: float, outcome: str, response: Any = None) -> None:
        if self.store is None:
            return
        usage = getattr(response, "usage", None)
        self.store.log_llm_call(
            purpose=purpose,
            model=str(getattr(response, "model", None) or self.cfg.llm_model),
            outcome=outcome,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            latency_ms=int((time.monotonic() - started) * 1000),
            request_id=getattr(response, "_request_id", None),
        )
