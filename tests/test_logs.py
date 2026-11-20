"""Structured logs: what a line carries, how `cinemind logs` filters them."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from typer.testing import CliRunner

from cinemind.cli import app as cli
from cinemind.core.config import log_dir
from cinemind.core.logs import LOG_FILE, JsonFormatter, log_context, read_logs


@pytest.fixture
def captured(tmp_path):
    """A logger writing JSON lines to tmp_path/cinemind.jsonl, as the CLI sets up."""
    handler = logging.FileHandler(tmp_path / LOG_FILE, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("cinemind.test")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield logger
    logger.removeHandler(handler)
    handler.close()


def test_a_line_carries_its_context_its_extras_and_a_traceback(captured, tmp_path):
    with log_context(job_id=7, item=None):  # None fields are left out
        captured.info("Job #%s started", 7, extra={"plan_id": "p-1"})
        with log_context(item="S01E02", stage="ingesting"):
            try:
                raise ValueError("bad stream")
            except ValueError:
                captured.exception("Ingest failed")
    captured.warning("outside")

    first, failed, outside = read_logs(tmp_path)
    assert first["msg"] == "Job #7 started" and first["level"] == "INFO"
    assert first["job_id"] == 7 and first["plan_id"] == "p-1" and "item" not in first
    assert first["ts"][10] == "T" and first["ts"][-6] in "+-"  # ISO 8601 with the offset
    assert (failed["item"], failed["stage"]) == ("S01E02", "ingesting")
    assert "ValueError: bad stream" in failed["error"]
    assert "job_id" not in outside  # the context ended with its block


async def test_tasks_started_inside_a_context_keep_it(captured, tmp_path):
    async def work():
        await asyncio.sleep(0)
        captured.info("in a task")

    with log_context(job_id=3):
        task = asyncio.get_running_loop().create_task(work())
    await task
    assert read_logs(tmp_path)[0]["job_id"] == 3


def test_reading_filters_by_job_item_and_level_and_skips_broken_lines(captured, tmp_path):
    with log_context(job_id=1, item="S01E01"):
        captured.info("one")
        captured.warning("one, warning")
    with log_context(job_id=2, item="movie"):
        captured.error("two")
    with (tmp_path / LOG_FILE).open("a", encoding="utf-8") as fh:
        fh.write('{"ts": "half a li')  # the process died mid-write

    assert [e["msg"] for e in read_logs(tmp_path, job=1)] == ["one", "one, warning"]
    assert [e["msg"] for e in read_logs(tmp_path, item="s01e01", level="warning")] == [
        "one, warning"
    ]
    assert [e["msg"] for e in read_logs(tmp_path, last=1)] == ["two"]


def test_rotated_files_are_read_oldest_first(tmp_path):
    for name, msg in ((f"{LOG_FILE}.2", "oldest"), (f"{LOG_FILE}.1", "older"), (LOG_FILE, "new")):
        (tmp_path / name).write_text(json.dumps({"level": "INFO", "msg": msg}) + "\n", "utf-8")
    assert [e["msg"] for e in read_logs(tmp_path)] == ["oldest", "older", "new"]


def test_the_logs_command_shows_one_jobs_story(tmp_path):
    folder = log_dir()
    folder.mkdir(parents=True)
    lines = [
        {"ts": "2026-11-20T13:05:12.000+02:00", "level": "INFO", "msg": "Job #4: 1 file(s)",
         "job_id": 4},
        {"ts": "2026-11-20T13:09:40.000+02:00", "level": "WARNING", "msg": "S01E02: quarantined",
         "job_id": 4, "item": "S01E02", "stage": "quarantined"},
        {"ts": "2026-11-20T13:10:00.000+02:00", "level": "INFO", "msg": "another job",
         "job_id": 5},
    ]  # fmt: skip
    (folder / LOG_FILE).write_text("".join(json.dumps(x) + "\n" for x in lines), "utf-8")

    shown = CliRunner().invoke(cli.app, ["logs", "--job", "4"])
    assert shown.exit_code == 0
    assert "13:09:40" in shown.output and "S01E02 quarantined" in shown.output
    assert "another job" not in shown.output
    raw = CliRunner().invoke(cli.app, ["logs", "--job", "4", "--level", "warning", "--json"])
    assert [json.loads(x)["stage"] for x in raw.output.splitlines()] == ["quarantined"]
    none = CliRunner().invoke(cli.app, ["logs", "--job", "99"])
    assert "Nothing logged for that" in none.output
