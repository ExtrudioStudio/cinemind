"""After a download: subtitle, ingest, review, and picking up where an interrupted run stopped."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cinemind.cli import app as cli
from cinemind.core.config import data_dir
from cinemind.core.models import Candidate, EpisodeRef, FileTarget, Title
from cinemind.core.store import Store
from cinemind.ingest.probe import ToolFailed
from cinemind.ingest.runner import Ingester
from cinemind.pipeline import runner as runner_module
from cinemind.pipeline.delivery import Delivery, dismiss, media_for, reopen
from cinemind.pipeline.runner import DownloadRunner
from cinemind.planning.planner import build_plan
from cinemind.subtitles.finder import FindResult

from .test_ingest_runner import COMPLIANT, REJECT, FakeTools


@pytest.fixture
def share(tmp_path) -> Path:
    root = tmp_path / "share"
    (root / "_incoming").mkdir(parents=True)
    return root


@pytest.fixture
def delivery_cfg(cfg, share):
    return cfg.model_copy(update={"auto_subtitles": True, "auto_ingest": True,
                                  "ingest_root": str(share), "subtitle_language": "he",
                                  "file_unlock_timeout": 1})  # fmt: skip


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "state.db")
    yield db
    db.close()


def _job(store: Store, cfg, friends: Title, episodes=(1, 2)) -> tuple[int, list[FileTarget]]:
    """A job whose files have finished downloading into the local library."""
    keys = [EpisodeRef(1, e) for e in episodes]
    cands = {k: [Candidate(f"{i:x}" * 40, 0, "1080p", 50, 10, filename=f"S01E0{k.episode}.mkv")]
             for i, k in enumerate(keys, start=1)}  # fmt: skip
    plan = build_plan(friends, keys, cands, cfg, "1080p")
    job_id = store.create_job(plan)
    targets = [t for job in plan.jobs for t in job.targets]
    for target in targets:
        target.final_path.parent.mkdir(parents=True, exist_ok=True)
        target.final_path.write_bytes(b"video")
        for ref in target.refs:
            store.set_item(job_id, ref, "done", path=str(target.final_path))
    for job in plan.jobs:
        store.update_torrent(job_id, job.info_hash, status="done")
    return job_id, targets


class Subtitles:
    """Stands in for find_for_video: saves a subtitle next to the video, or finds none."""

    def __init__(self, found: bool = True, error: Exception | None = None):
        self.found, self.error = found, error
        self.calls: list[tuple[str, str, int | None, int | None]] = []
        self.gate: asyncio.Event | None = None  # set to hold the search until released
        self.running = self.peak = 0

    async def __call__(self, video: Path, imdb: str, season, episode) -> FindResult:
        self.calls.append((video.name, imdb, season, episode))
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            if self.gate is not None:
                await self.gate.wait()
            await asyncio.sleep(0.01)
        finally:
            self.running -= 1
        if self.error:
            raise self.error
        if not self.found:
            return FindResult("none", reason="found 3 candidate(s), but none matched this "
                                             "file's audio timing")  # fmt: skip
        out = video.with_name(f"{video.stem}.he.srt")
        out.write_text("1\n00:00:01,000 --> 00:00:02,000\nשלום\n", encoding="utf-8-sig")
        return FindResult("found", path=out, source="wizdom", release="x")


def _tools(targets: list[FileTarget], probe=COMPLIANT) -> FakeTools:
    return FakeTools({t.final_path.name: probe for t in targets})


def _delivery(cfg, store, job_id, title, tools: FakeTools, subs: Subtitles, **kw) -> Delivery:
    ingester = Ingester(cfg, probe_fn=tools.probe, run_fn=tools.run)
    return Delivery(cfg, store, job_id, title, find_subtitle=subs, ingester=ingester, **kw)


# -- the stages ---------------------------------------------------------------------------------


async def test_a_file_gets_its_subtitle_then_is_filed_with_it(delivery_cfg, store, friends, share):
    job_id, targets = _job(store, delivery_cfg, friends, episodes=(1,))
    delivered: list[tuple[str, Path]] = []
    subs = Subtitles()
    d = _delivery(delivery_cfg, store, job_id, friends, _tools(targets), subs,
                  on_delivered=lambda key, path: delivered.append((key, path)))  # fmt: skip
    assert d.submit(targets[0]) == "S01E01"
    await d.wait()

    row = store.delivery(job_id, "S01E01")
    assert row.stage == "delivered" and row.attempts == 1 and row.review is None
    target = Path(row.target)
    assert target.is_file() and target.is_relative_to(share / "TV Shows")
    assert "Season 01" in str(target) and not targets[0].final_path.exists()
    assert row.subtitle == str(target.with_name(f"{target.stem}.he.srt"))  # moved with it
    assert Path(row.subtitle).is_file()
    assert subs.calls == [(targets[0].final_path.name, friends.id, 1, 1)]
    assert delivered == [("S01E01", target)]
    assert json.loads(row.detail)["ingest"]["status"] == "placed"
    assert store.jobs()[0].deliveries == {"delivered": 1}


async def test_no_matching_subtitle_needs_review_but_the_file_is_filed(
    delivery_cfg, store, friends
):
    job_id, targets = _job(store, delivery_cfg, friends, episodes=(1,))
    d = _delivery(delivery_cfg, store, job_id, friends, _tools(targets), Subtitles(found=False))
    d.submit(targets[0])
    await d.wait()
    row = store.delivery(job_id, "s01e01")  # keys are case-insensitive
    assert row.stage == "needs_review" and row.target and Path(row.target).is_file()
    assert "no he subtitle" in row.review and "audio timing" in row.review
    assert row.subtitle is None


async def test_a_failing_subtitle_search_doesnt_stop_the_ingest(delivery_cfg, store, friends):
    job_id, targets = _job(store, delivery_cfg, friends, episodes=(1,))
    subs = Subtitles(error=ConnectionError("wizdom.xyz is down"))
    d = _delivery(delivery_cfg, store, job_id, friends, _tools(targets), subs)
    d.submit(targets[0])
    await d.wait()
    row = store.delivery(job_id, "S01E01")
    assert row.stage == "needs_review" and "wizdom.xyz is down" in row.review and row.target


async def test_a_release_that_cant_play_is_quarantined_for_review(delivery_cfg, store, friends):
    job_id, targets = _job(store, delivery_cfg, friends, episodes=(1,))
    delivered: list = []
    d = _delivery(delivery_cfg, store, job_id, friends, _tools(targets, REJECT), Subtitles(),
                  on_delivered=lambda *a: delivered.append(a))  # fmt: skip
    d.submit(targets[0])
    await d.wait()
    row = store.delivery(job_id, "S01E01")
    assert row.stage == "quarantined" and "Find another release" in row.review
    assert "_quarantine" in row.review and delivered == []  # no hand-off for a rejected file
    with pytest.raises(ValueError, match="another release"):
        reopen(store, job_id, "S01E01")
    assert dismiss(store, job_id, "S01E01") == "S01E01"
    row = store.delivery(job_id, "S01E01")
    assert row.stage == "dismissed" and "Find another release" in row.review  # kept


async def test_each_stage_change_is_logged_with_the_job_and_the_file(
    delivery_cfg, store, friends, caplog
):
    job_id, targets = _job(store, delivery_cfg, friends, episodes=(1,))
    d = _delivery(delivery_cfg, store, job_id, friends, _tools(targets, REJECT), Subtitles())
    with caplog.at_level(logging.INFO, logger="cinemind.pipeline.delivery"):
        d.submit(targets[0])
        await d.wait()
    stages = [(r.job_id, r.item, r.stage, r.levelname) for r in caplog.records
              if hasattr(r, "stage")]  # fmt: skip
    assert stages == [(job_id, "S01E01", s, level) for s, level in (
        ("pending", "INFO"), ("subtitling", "INFO"), ("ingesting", "INFO"),
        ("quarantined", "WARNING"))]  # fmt: skip
    assert "won't play directly" in caplog.records[-1].getMessage()  # and why


async def test_a_failed_ingest_can_be_retried(delivery_cfg, store, friends):
    job_id, targets = _job(store, delivery_cfg, friends, episodes=(1,))
    tools = _tools(targets)
    real_probe = tools.probe
    calls = [0]

    async def flaky_probe(path):
        calls[0] += 1
        if calls[0] == 1:
            raise ToolFailed("ffprobe failed (exit 1): the share is offline")
        return await real_probe(path)

    tools.probe = flaky_probe  # type: ignore[method-assign]
    subs = Subtitles()
    d = _delivery(delivery_cfg, store, job_id, friends, tools, subs)
    d.submit(targets[0])
    await d.wait()
    row = store.delivery(job_id, "S01E01")
    assert row.stage == "failed" and "share is offline" in row.error
    assert row.subtitle and Path(row.subtitle).is_file()  # the subtitle stage had finished

    assert d.retry("s01e01") == "S01E01"
    await d.wait()
    row = store.delivery(job_id, "S01E01")
    assert row.stage == "delivered" and row.attempts == 2 and row.error is None
    assert len(subs.calls) == 1  # the subtitle it already had was kept
    assert Path(row.subtitle).parent == Path(row.target).parent


async def test_a_subtitle_already_next_to_the_video_is_kept(delivery_cfg, store, friends):
    cfg = delivery_cfg.model_copy(update={"auto_ingest": False})
    job_id, targets = _job(store, cfg, friends, episodes=(1,))
    video = targets[0].final_path
    video.with_name(f"{video.stem}.he.srt").write_text("1\n", encoding="utf-8")
    subs = Subtitles()
    d = _delivery(cfg, store, job_id, friends, _tools(targets), subs)
    d.submit(targets[0])
    await d.wait()
    row = store.delivery(job_id, "S01E01")
    assert row.stage == "delivered" and subs.calls == [] and row.target is None
    assert row.path == str(video)  # without the ingest, it stays where it was downloaded


async def test_files_are_delivered_a_few_at_a_time(delivery_cfg, store, friends):
    for limit, episodes in ((1, (1, 2, 3, 4)), (2, (5, 6, 7, 8))):
        cfg = delivery_cfg.model_copy(update={"auto_ingest": False, "max_delivery_jobs": limit})
        job_id, targets = _job(store, cfg, friends, episodes=episodes)
        subs = Subtitles()
        d = _delivery(cfg, store, job_id, friends, _tools(targets), subs)
        for target in targets:
            d.submit(target)
        await d.wait()
        assert subs.peak == limit and len(subs.calls) == 4
        assert store.job(job_id).deliveries == {"delivered": 4}


# -- interruptions -------------------------------------------------------------------------------


async def test_an_interrupted_delivery_resumes_in_the_next_run(delivery_cfg, store, friends):
    job_id, targets = _job(store, delivery_cfg, friends, episodes=(1,))
    subs = Subtitles()
    subs.gate = asyncio.Event()  # the search never ends: the run is stopped during it
    d = _delivery(delivery_cfg, store, job_id, friends, _tools(targets), subs)
    d.submit(targets[0])
    await asyncio.sleep(0.05)
    assert d.busy == 1 and store.delivery(job_id, "S01E01").stage == "subtitling"
    await d.aclose()
    assert store.delivery(job_id, "S01E01").stage == "subtitling"  # left for the next run
    assert store.finish_job(job_id) == "paused"  # so `cinemind resume` picks it up

    again = _delivery(delivery_cfg, store, job_id, friends, _tools(targets), Subtitles())
    assert again.resume() == 1
    await again.wait()
    assert store.delivery(job_id, "S01E01").stage == "delivered"
    assert store.finish_job(job_id) == "done"


async def test_a_file_that_moved_away_is_reported_not_guessed(delivery_cfg, store, friends):
    job_id, targets = _job(store, delivery_cfg, friends, episodes=(1,))
    store.start_delivery(job_id, "S01E01", str(targets[0].final_path), "S01E01")
    store.set_delivery(job_id, "S01E01", "ingesting")
    targets[0].final_path.unlink()  # e.g. the move finished just before the crash
    d = _delivery(delivery_cfg, store, job_id, friends, _tools(targets), Subtitles())
    assert d.resume() == 1
    await d.wait()
    row = store.delivery(job_id, "S01E01")
    assert row.stage == "failed" and "no longer at" in row.error
    assert f"cinemind review dismiss {job_id} S01E01" in row.error


def test_names_for_movies_episodes_and_double_episodes(friends):
    inception = Title("tt1375666", "movie", "Inception", 2010)
    assert media_for(inception, ()).kind == "movie"
    one = media_for(friends, (EpisodeRef(1, 1),))
    assert (one.kind, one.season, one.episode, one.end_episode) == ("episode", 1, 1, None)
    assert one.episode_title and one.imdb == friends.id
    double = media_for(friends, (EpisodeRef(1, 16), EpisodeRef(1, 17)))
    assert (double.episode, double.end_episode, double.episode_title) == (16, 17, None)


# -- with the download runner ---------------------------------------------------------------------


async def test_the_runner_delivers_finished_files_then_hands_them_off(
    delivery_cfg, store, friends, monkeypatch
):
    handed: list[Path] = []

    def fake_hand_off(cfg, title, target, *args, **kwargs):
        handed.append(target.final_path)

    monkeypatch.setattr(runner_module, "hand_off", fake_hand_off)
    job_id, targets = _job(store, delivery_cfg, friends, episodes=(1, 2))
    run = DownloadRunner(delivery_cfg, store, job_id, store.load_plan(job_id))
    run.delivery = _delivery(delivery_cfg, store, job_id, friends, _tools(targets), Subtitles(),
                             on_delivered=run._after_delivery)  # fmt: skip
    for job in store.finished_torrents(job_id):
        run.on_job_done(job)  # what the engine calls when a torrent is complete
    assert await run.run([], display=False) == "done"

    rows = store.deliveries(job_id)
    assert [r.stage for r in rows] == ["delivered", "delivered"]
    assert sorted(handed) == sorted(Path(r.target) for r in rows)  # the new paths, after filing


async def test_a_paused_run_leaves_the_job_resumable(delivery_cfg, store, friends):
    job_id, targets = _job(store, delivery_cfg, friends, episodes=(1,))
    run = DownloadRunner(delivery_cfg, store, job_id, store.load_plan(job_id))
    subs = Subtitles()
    subs.gate = asyncio.Event()
    run.delivery = _delivery(delivery_cfg, store, job_id, friends, _tools(targets), subs)

    async def go():
        run.delivery.submit(targets[0])
        return await run.run([], display=False)

    task = asyncio.create_task(go())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.job(job_id).status == "paused"
    assert [r.key for r in store.open_deliveries(job_id)] == ["S01E01"]


# -- the command line ------------------------------------------------------------------------------


def _seeded(cfg, friends) -> tuple[int, list[FileTarget]]:
    store = Store(data_dir() / "state.db")
    try:
        job_id, targets = _job(store, cfg, friends, episodes=(1, 2))
        for target, stage in zip(targets, ("needs_review", "delivered"), strict=True):
            key = target.refs[0].code
            store.start_delivery(job_id, key, str(target.final_path), key)
            store.set_delivery(job_id, key, stage,
                               review="no he subtitle: none matched" if stage != "delivered"
                               else None)  # fmt: skip
    finally:
        store.close()
    return job_id, targets


def test_review_lists_what_needs_you_and_dismiss_closes_it(cfg, friends):
    job_id, _ = _seeded(cfg, friends)
    runner = CliRunner()
    listed = runner.invoke(cli.app, ["review"])
    assert listed.exit_code == 0, listed.output
    assert "S01E01" in listed.output and "none matched" in listed.output
    assert "S01E02" not in listed.output  # delivered: nothing to do
    assert "S01E02" in runner.invoke(cli.app, ["review", "--all"]).output

    closed = runner.invoke(cli.app, ["review", "dismiss", str(job_id), "s01e01"])
    assert closed.exit_code == 0 and "Dismissed S01E01" in closed.output
    assert "Nothing needs you" in runner.invoke(cli.app, ["review"]).output
    missing = runner.invoke(cli.app, ["review", "dismiss", str(job_id), "S09E09"])
    assert missing.exit_code == 1 and "No delivery for S09E09" in missing.output


def test_a_subtitle_you_pick_is_saved_as_utf8_next_to_the_video(cfg, friends, tmp_path):
    job_id, targets = _seeded(cfg, friends)
    picked = tmp_path / "found-it.srt"
    picked.write_bytes("1\r\n00:00:01,000 --> 00:00:02,500\r\nשלום\r\n".encode("cp1255"))
    result = CliRunner().invoke(cli.app, ["review", "subtitle", str(job_id), "S01E01",
                                          str(picked), "--lang", "he"])  # fmt: skip
    assert result.exit_code == 0, result.output
    video = targets[0].final_path
    saved = video.with_name(f"{video.stem}.he.srt")
    assert saved.read_bytes().startswith(b"\xef\xbb\xbf")
    assert "שלום" in saved.read_text(encoding="utf-8-sig")
    store = Store(data_dir() / "state.db")
    try:
        row = store.delivery(job_id, "S01E01")
    finally:
        store.close()
    assert row.stage == "delivered" and row.subtitle == str(saved) and row.review is None


def test_retry_and_deliver_need_a_stage_turned_on(cfg, friends):
    job_id, _ = _seeded(cfg, friends)
    for args in (["review", "retry", str(job_id), "S01E01"], ["deliver", str(job_id)]):
        result = CliRunner().invoke(cli.app, args)
        assert result.exit_code == 1 and "Delivery is off" in result.output
