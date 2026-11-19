"""What stands between an agent and side effects.

- **Two-phase commit.** ``start_download`` only accepts a ``plan_id`` that ``plan_download``
  returned in this process. It works once, and only within ``mcp_plan_ttl_minutes``, so what
  starts is exactly what the user was shown, and sizes and sources are still fresh.
- **Limits.** A plan over ``mcp_max_plan_gb`` or ``mcp_max_files`` is refused when it's made.
- **Paths.** Every planned file must land inside the configured library roots.
- **Audit.** Every tool call is written to the store's ``tool_calls`` table, with its outcome.
- **Read-only mode.** ``cinemind mcp --read-only`` doesn't register the tools that change
  anything (see :mod:`cinemind.mcp.server`).
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cinemind.core.config import Config
from cinemind.core.store import Store
from cinemind.planning.planner import Plan

log = logging.getLogger(__name__)


class GuardrailError(Exception):
    """A request the agent may not make; the message tells it what to do instead."""


@dataclass
class _Entry:
    plan: Plan
    created: float


class PlanBook:
    """Plans waiting for the user's go-ahead, by id. Each one expires, and each works once."""

    def __init__(self, ttl_seconds: float, clock: Callable[[], float] = time.monotonic):
        self.ttl = ttl_seconds
        self.clock = clock
        self._plans: dict[str, _Entry] = {}

    def add(self, plan: Plan) -> str:
        self._expire()
        plan_id = "plan-" + secrets.token_urlsafe(9)
        self._plans[plan_id] = _Entry(plan, self.clock())
        return plan_id

    def expires_in(self, plan_id: str) -> int:
        entry = self._plans.get(plan_id)
        return 0 if entry is None else max(0, int(entry.created + self.ttl - self.clock()))

    def take(self, plan_id: str) -> Plan:
        """The plan, removed from the book. GuardrailError if it's unknown, used or expired."""
        entry = self._plans.pop(plan_id, None)
        if entry is None:
            raise GuardrailError(
                f"Unknown plan id {plan_id!r}. Plan ids come from plan_download (or "
                "plan_request) in this session and work once; make a new plan and show it to "
                "the user before starting it."
            )
        if self.clock() - entry.created > self.ttl:
            raise GuardrailError(
                f"Plan {plan_id} expired after {self.ttl / 60:g} minutes. Make a new plan so the "
                "user sees current sizes and sources, then start that one."
            )
        return entry.plan

    def _expire(self) -> None:
        now = self.clock()
        for plan_id in [k for k, e in self._plans.items() if now - e.created > self.ttl]:
            del self._plans[plan_id]


def check_plan(plan: Plan, cfg: Config) -> None:
    """Refuse plans that are too big or would write outside the library."""
    size_gb = plan.total_size / 1024**3
    if size_gb > cfg.mcp_max_plan_gb:
        raise GuardrailError(
            f"This plan is {size_gb:.1f} GB, over the {cfg.mcp_max_plan_gb:g} GB limit for plans "
            "made through the assistant (setting mcp_max_plan_gb). Ask for fewer episodes or a "
            "lower quality."
        )
    if plan.file_count > cfg.mcp_max_files:
        raise GuardrailError(
            f"This plan has {plan.file_count} files, over the limit of {cfg.mcp_max_files} "
            "(setting mcp_max_files). Plan fewer episodes at a time."
        )
    roots = [Path(cfg.tv_root).resolve(), Path(cfg.movie_root).resolve()]
    for job in plan.jobs:
        for target in job.targets:
            final = target.final_path.resolve()
            if not any(final.is_relative_to(root) for root in roots):
                raise GuardrailError(f"Refusing a file outside the library roots: {final}")


class Audit:
    """Records each tool call and turns failures into messages the agent can act on."""

    def __init__(self, store: Store):
        self.store = store

    @contextmanager
    def call(self, tool: str, arguments: dict[str, Any]) -> Iterator[None]:
        args = json.dumps(arguments, default=str, sort_keys=True)
        try:
            yield
        except GuardrailError as exc:
            self.store.log_tool_call(tool, args, "refused", str(exc))
            raise
        except Exception as exc:
            self.store.log_tool_call(tool, args, "error", f"{type(exc).__name__}: {exc}")
            raise
        else:
            self.store.log_tool_call(tool, args, "ok")
