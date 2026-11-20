"""The MCP server, driven through an in-process MCP client, with its guardrails."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from pathlib import Path

import pytest
import respx
from mcp import Client

from cinemind import service
from cinemind.core.store import Store
from cinemind.mcp.guardrails import Audit, PlanBook
from cinemind.mcp.server import CinemindApp, create_server
from cinemind.planning.streams import StreamResolver
from cinemind.sources.stremio.client import AddonClient

from .conftest import FIXTURES, load
from .fakes import FakeLLM, FakeRunner, FixedSource

CINEMETA = "https://v3-cinemeta.strem.io"
FRIENDS = "tt0108778"
SNAPSHOT = FIXTURES / "mcp_tools.json"


@pytest.fixture
def cinemeta():
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{CINEMETA}/manifest.json").respond(json=load("cinemeta_manifest.json"))
        router.get(f"{CINEMETA}/catalog/series/top/search=Friends.json").respond(
            json=load("cinemeta_search_series_friends.json"))  # fmt: skip
        router.get(f"{CINEMETA}/catalog/movie/top/search=Friends.json").respond(
            json=load("cinemeta_search_movie_friends.json"))  # fmt: skip
        router.get(f"{CINEMETA}/meta/series/{FRIENDS}.json").respond(
            json=load("cinemeta_meta_friends.json"))  # fmt: skip
        yield router


@pytest.fixture
async def app(cfg, tmp_path, cinemeta):
    FakeRunner.instances.clear()
    store = Store(tmp_path / "state.db")
    client = AddonClient(cfg, retry_wait=0.0)
    session = service.Session(cfg, client, await client.load_all(cfg.meta_addons),
                              StreamResolver([FixedSource()], cfg))  # fmt: skip
    clock = [0.0]
    app = CinemindApp(cfg, store, session, service.Downloads(cfg, store, FakeRunner),
                      PlanBook(cfg.mcp_plan_ttl_minutes * 60, clock=lambda: clock[0]),
                      Audit(store), llm=None)  # fmt: skip
    app.clock = clock  # type: ignore[attr-defined]  # lets tests move time on
    yield app
    await app.downloads.aclose()
    await client.aclose()
    store.close()


async def call(client: Client, tool: str, **arguments):
    result = await client.call_tool(tool, arguments)
    text = " ".join(getattr(block, "text", "") for block in result.content)
    return result, text


async def ok(client: Client, tool: str, **arguments) -> dict:
    result, text = await call(client, tool, **arguments)
    assert not result.is_error, text
    return result.structured_content


async def refused(client: Client, tool: str, **arguments) -> str:
    result, text = await call(client, tool, **arguments)
    assert result.is_error, text
    return text


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


# -- the tool list ------------------------------------------------------------------------------


async def test_tools_their_annotations_and_the_read_only_mode(app):
    async with Client(create_server(app=app)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"search_titles", "title_info", "plan_download", "plan_request",
                          "start_download", "job_status", "list_jobs", "pause_job",
                          "resume_job", "list_reviews", "resolve_review"}  # fmt: skip
    assert tools["plan_download"].annotations.read_only_hint is True  # planning changes nothing
    assert tools["start_download"].annotations.read_only_hint is False
    assert tools["start_download"].annotations.destructive_hint is False
    assert tools["list_reviews"].annotations.read_only_hint is True
    assert tools["resolve_review"].annotations.read_only_hint is False
    assert all(t.output_schema for t in tools.values())  # typed results for the agent

    async with Client(create_server(app=app, read_only=True)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert "start_download" not in names and "pause_job" not in names and "plan_download" in names
    assert "list_reviews" in names and "resolve_review" not in names


async def test_tool_schemas_match_the_snapshot(app):
    """Changing a tool's inputs changes what agents send: make it a deliberate decision."""
    async with Client(create_server(app=app)) as client:
        tools = (await client.list_tools()).tools
    # Python 3.13 strips docstring indentation at compile time, 3.12 keeps it: compare cleaned.
    current = {
        t.name: {"description": inspect.cleandoc(t.description or ""), "input": t.input_schema}
        for t in sorted(tools, key=lambda t: t.name)
    }
    if os.environ.get("CINEMIND_UPDATE_SNAPSHOTS"):
        text = json.dumps(current, indent=1, sort_keys=True) + "\n"
        SNAPSHOT.write_text(text, encoding="utf-8", newline="\n")  # LF, as .gitattributes wants
    assert current == json.loads(SNAPSHOT.read_text(encoding="utf-8")), (
        "MCP tool schemas changed; rerun with CINEMIND_UPDATE_SNAPSHOTS=1 if that's intended"
    )


# -- the happy path -------------------------------------------------------------------------------


async def test_search_plan_start_and_follow_a_download(app):
    async with Client(create_server(app=app)) as client:
        found = await ok(client, "search_titles", query="Friends", type="series")
        top = found["results"][0]
        assert (top["id"], top["name"], top["type"]) == (FRIENDS, "Friends", "series")
        assert top["year"].startswith("1994")
        info = await ok(client, "title_info", title_id=FRIENDS, type="series")
        assert len(info["seasons"]) == 10 and info["seasons"][0]["episodes"] == 24

        plan = await ok(client, "plan_download", title_id=FRIENDS, type="series",
                        selection="s1e1-2", quality="1080p")  # fmt: skip
        assert [f["episode"] for f in plan["files"]] == ["S01E01", "S01E02"]
        assert plan["files"][0]["kind"] == "direct" and plan["files"][0]["checksum_verified"]
        assert plan["total_size"] == "762.9 MB" and plan["plan_id"].startswith("plan-")
        assert plan["expires_in_seconds"] == 15 * 60 and "start_download" in plan["next_step"]

        started = await ok(client, "start_download", plan_id=plan["plan_id"])
        await _settle()
        status = await ok(client, "job_status", job_id=started["job_id"])
        assert status["running"] and status["status"] == "running"

        FakeRunner.instances[0].release.set()
        await _settle()
        status = await ok(client, "job_status", job_id=started["job_id"])
        assert status["status"] == "done" and not status["running"]
        assert [f["state"] for f in status["files"]] == ["done", "done"]
        jobs = await ok(client, "list_jobs")
        assert jobs["jobs"][0]["job_id"] == started["job_id"] and jobs["jobs"][0]["done"] == 2


async def test_pause_and_resume_through_the_tools(app):
    async with Client(create_server(app=app)) as client:
        plan = await ok(client, "plan_download", title_id=FRIENDS, selection="s1e1")
        job = (await ok(client, "start_download", plan_id=plan["plan_id"]))["job_id"]
        await _settle()
        assert (await ok(client, "pause_job", job_id=job))["status"] == "pausing"
        await _settle()
        assert app.store.job(job).status == "paused"
        assert "isn't running" in await refused(client, "pause_job", job_id=job)
        assert (await ok(client, "resume_job", job_id=job))["status"] == "queued"
        assert "No download job #404" in await refused(client, "job_status", job_id=404)


# -- guardrails ---------------------------------------------------------------------------------


async def test_downloads_need_a_fresh_plan_id_used_once(app):
    async with Client(create_server(app=app)) as client:
        assert "Unknown plan id" in await refused(client, "start_download", plan_id="plan-made-up")

        plan = await ok(client, "plan_download", title_id=FRIENDS, selection="s1e1")
        await ok(client, "start_download", plan_id=plan["plan_id"])
        assert "Unknown plan id" in await refused(client, "start_download",
                                                  plan_id=plan["plan_id"])  # fmt: skip

        stale = await ok(client, "plan_download", title_id=FRIENDS, selection="s1e2")
        app.clock[0] += 16 * 60
        assert "expired after 15 minutes" in await refused(client, "start_download",
                                                           plan_id=stale["plan_id"])  # fmt: skip
    outcomes = [(c["tool"], c["outcome"]) for c in reversed(app.store.tool_calls())]
    assert ("start_download", "refused") in outcomes and ("plan_download", "ok") in outcomes
    assert all(c["arguments"] for c in app.store.tool_calls())


async def test_big_plans_and_missing_selections_are_refused(app):
    app.cfg.mcp_max_plan_gb = 0.5
    async with Client(create_server(app=app)) as client:
        message = await refused(client, "plan_download", title_id=FRIENDS, selection="s1e1-2")
        assert "over the 0.5 GB limit" in message and "mcp_max_plan_gb" in message
        assert "Say which episodes" in await refused(client, "plan_download", title_id=FRIENDS)
        app.cfg.mcp_max_plan_gb, app.cfg.mcp_max_files = 60, 1
        assert "over the limit of 1" in await refused(client, "plan_download", title_id=FRIENDS,
                                                      selection="s1e1-2")  # fmt: skip


async def test_unexpected_errors_are_logged_not_leaked(app, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(service, "find_offers", broken)
    async with Client(create_server(app=app)) as client:
        message = await refused(client, "plan_download", title_id=FRIENDS, selection="s1e1")
    assert "Internal error in plan_download (RuntimeError)" in message
    assert "secret internal detail" not in message
    assert app.store.tool_calls()[0]["outcome"] == "error"


# -- plain language -------------------------------------------------------------------------------


async def test_plain_language_needs_claude_and_says_how_to_get_it(app):
    async with Client(create_server(app=app)) as client:
        message = await refused(client, "plan_request", request="first episode of Friends")
    assert "ANTHROPIC_API_KEY" in message and "plan_download" in message


async def test_plan_request_reads_the_request_and_shows_alternatives(app):
    app.llm = FakeLLM({"is_download_request": True, "title": "Friends", "media_type": "series",
                       "episodes": {"scope": "explicit", "selection": "s1e1-2"},
                       "quality": "1080p"})  # fmt: skip
    async with Client(create_server(app=app)) as client:
        plan = await ok(client, "plan_request", request="the first two episodes of Friends")
    assert plan["title"] == "Friends" and plan["read_as"] == "s1e1-2 /1080p"
    assert len(plan["files"]) == 2 and plan["plan_id"]
    assert all(alt["id"] != FRIENDS for alt in plan["alternatives"])


async def test_plan_download_accepts_plain_words_for_the_selection(app):
    app.llm = FakeLLM({"episodes": {"scope": "latest_season"}, "quality": None})
    async with Client(create_server(app=app)) as client:
        plan = await ok(client, "plan_download", title_id=FRIENDS, selection="the newest season")
    assert plan["read_as"] == "s10" and plan["files"][0]["episode"].startswith("S10E")


def test_snapshot_exists():
    assert Path(SNAPSHOT).is_file()


# -- reviews after a download ----------------------------------------------------------------------


async def test_reviews_are_listed_retried_and_dismissed(app, friends):
    from .test_delivery import _job

    store = app.store
    job_id, targets = _job(store, app.cfg, friends, episodes=(1, 2))
    for target, stage in zip(targets, ("needs_review", "quarantined"), strict=True):
        key = target.refs[0].code
        store.start_delivery(job_id, key, str(target.final_path), key)
        store.set_delivery(job_id, key, stage, review=f"{stage}: because")
    async with Client(create_server(app=app)) as client:
        listed = (await ok(client, "list_reviews"))["items"]
        assert [(i["item"], i["stage"]) for i in listed] == [("S01E01", "needs_review"),
                                                             ("S01E02", "quarantined")]  # fmt: skip
        assert listed[0]["file"] == targets[0].final_path.name  # a name, not the full path
        assert listed[0]["title"] == "Friends" and listed[0]["reason"] == "needs_review: because"

        off = await refused(client, "resolve_review", job_id=job_id, item="S01E01", action="retry")
        assert "Delivery is off" in off
        app.downloads.cfg = app.cfg.model_copy(update={"auto_subtitles": True})
        bad = await refused(client, "resolve_review", job_id=job_id, item="S01E02", action="retry")
        assert "another release" in bad

        acked = await ok(client, "resolve_review", job_id=job_id, item="s01e01", action="retry")
        assert acked["status"] == "retrying" and "S01E01" in acked["message"]
        await _settle()
        assert store.delivery(job_id, "S01E01").stage == "pending"
        assert FakeRunner.instances[-1].job_id == job_id  # queued: the runner delivers it

        gone = await ok(client, "resolve_review", job_id=job_id, item="S01E02", action="dismiss")
        assert gone["status"] == "dismissed"
        assert (await ok(client, "list_reviews"))["items"] == []
        status = await ok(client, "job_status", job_id=job_id)
        assert status["deliveries"] == {"pending": 1, "dismissed": 1}
