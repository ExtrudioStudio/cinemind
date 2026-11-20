"""Structured logs: one JSON object per line in ``cinemind.jsonl``, read back by ``cinemind logs``.

Every line has ``ts`` (ISO 8601, with the offset), ``level``, ``logger`` and ``msg``, the keys
MediaPipeline v1's ingest logs used. Lines about a download also carry what they're about:
``job_id``, ``item`` (an episode code, or ``movie``), ``stage`` (a delivery stage),
``info_hash`` and ``plan_id`` (the MCP plan a job came from). They come from
:func:`log_context`, which covers everything logged inside it (also in the tasks it starts),
or from ``extra=`` on one call. A failure's traceback is in ``error``.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

FIELDS = ("job_id", "item", "stage", "info_hash", "plan_id")
LOG_FILE = "cinemind.jsonl"
MAX_BYTES = 2_000_000
BACKUPS = 3
LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40, "critical": 50}

_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "cinemind_log", default=None
)


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Add these fields to every line logged inside the block (None values are left out)."""
    current = _context.get() or {}
    token = _context.set({**current, **{k: v for k, v in fields.items() if v is not None}})
    try:
        yield
    finally:
        _context.reset(token)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).astimezone().isoformat(
                timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }  # fmt: skip
        entry.update(_context.get() or {})
        for name in FIELDS:
            value = getattr(record, name, None)
            if value is not None:
                entry[name] = value
        if record.exc_info:
            entry["error"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def setup_logging(folder: Path, verbose: bool = False) -> None:
    """JSON lines to ``folder/cinemind.jsonl`` (rotated); with ``verbose``, text on stderr."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(folder / LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUPS,
                                      encoding="utf-8")  # fmt: skip
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    except OSError:
        pass  # a read-only home: run without a log file
    if verbose:
        err = logging.StreamHandler(sys.stderr)
        err.setLevel(logging.DEBUG)
        err.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(err)
    for noisy in ("httpx", "httpcore", "rebulk", "guessit"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_files(folder: Path) -> list[Path]:
    """The log and its rotated copies, oldest first."""
    rotated = [folder / f"{LOG_FILE}.{n}" for n in range(BACKUPS, 0, -1)]
    return [p for p in [*rotated, folder / LOG_FILE] if p.is_file()]


def read_logs(
    folder: Path,
    job: int | None = None,
    item: str | None = None,
    level: str = "debug",
    last: int | None = None,
) -> list[dict[str, Any]]:
    """The matching lines, oldest first (the newest ``last`` of them). Lines that aren't JSON,
    such as a half-written last line, are skipped."""
    floor = LEVELS.get(level.lower(), 10)
    out: list[dict[str, Any]] = []
    for path in log_files(folder):
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if job is not None and entry.get("job_id") != job:
                    continue
                if item is not None and str(entry.get("item", "")).lower() != item.lower():
                    continue
                if LEVELS.get(str(entry.get("level", "")).lower(), 10) < floor:
                    continue
                out.append(entry)
    return out[-last:] if last else out
