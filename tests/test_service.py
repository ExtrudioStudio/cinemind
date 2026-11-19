"""The background download manager used by long-lived processes (the MCP server)."""

from __future__ import annotations

import asyncio

import pytest

from cinemind.core.models import EpisodeRef
from cinemind.core.store import Store
from cinemind.planning.planner import build_plan
from cinemind.service import Downloads

from .fakes import FakeRunner, FixedSource


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "state.db")
    yield s
    s.close()


async def _plan(cfg, friends, episode):
    ref = EpisodeRef(1, episode)
    cands = await FixedSource().candidates(friends, ref)
    return build_plan(friends, [ref], {ref: cands}, cfg, "1080p")


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def _reset_runners():
    FakeRunner.instances.clear()


async def test_jobs_run_one_at_a_time_in_order(cfg, friends, store):
    downloads = Downloads(cfg, store, runner_factory=FakeRunner)
    first = downloads.start(await _plan(cfg, friends, 1))
    second = downloads.start(await _plan(cfg, friends, 2))
    await _settle()
    assert downloads.running_job == first and downloads.queued == [second]
    assert store.job(second).status == "queued"
    assert downloads.progress(first) == [] and downloads.progress(second) is None

    FakeRunner.instances[0].release.set()
    await _settle()
    assert store.job(first).status == "done" and downloads.running_job == second
    FakeRunner.instances[1].release.set()
    await _settle()
    assert store.job(second).status == "done" and downloads.running_job is None
    await downloads.aclose()


async def test_pausing_a_running_job_saves_it_and_resume_continues(cfg, friends, store):
    downloads = Downloads(cfg, store, runner_factory=FakeRunner)
    job = downloads.start(await _plan(cfg, friends, 1))
    await _settle()
    assert downloads.pause(job)
    await _settle()
    assert store.job(job).status == "paused" and downloads.running_job is None

    downloads.resume(job)
    await _settle()
    assert downloads.running_job == job
    FakeRunner.instances[-1].release.set()
    await _settle()
    assert store.job(job).status == "done"
    with pytest.raises(ValueError, match="only paused jobs"):
        downloads.resume(job)
    await downloads.aclose()


async def test_pausing_a_queued_job_takes_it_out_of_the_queue(cfg, friends, store):
    downloads = Downloads(cfg, store, runner_factory=FakeRunner)
    downloads.start(await _plan(cfg, friends, 1))
    queued = downloads.start(await _plan(cfg, friends, 2))
    await _settle()
    assert downloads.pause(queued) and downloads.queued == []
    assert store.job(queued).status == "paused"
    assert not downloads.pause(9999)
    await downloads.aclose()


async def test_a_job_that_cannot_run_is_failed_with_its_reason(cfg, friends, store):
    downloads = Downloads(cfg, store, runner_factory=FakeRunner)
    job = downloads.start(await _plan(cfg, friends, 1))
    await _settle()
    FakeRunner.instances[0].fail = RuntimeError("needs BitTorrent support")
    FakeRunner.instances[0].release.set()
    await _settle()
    row = store.job(job)
    assert row.status == "failed" and row.error == "needs BitTorrent support"
    await downloads.aclose()


async def test_closing_pauses_whatever_is_running_or_queued(cfg, friends, store):
    downloads = Downloads(cfg, store, runner_factory=FakeRunner)
    running = downloads.start(await _plan(cfg, friends, 1))
    queued = downloads.start(await _plan(cfg, friends, 2))
    await _settle()
    await downloads.aclose()
    assert store.job(running).status == "paused" and store.job(queued).status == "paused"
    assert store.jobs(include_finished=False)  # both still resumable with `cinemind resume`
