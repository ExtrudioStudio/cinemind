"""Failure handling: store transactions and migrations, hand-off results, engine fault isolation."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from stremio_dl import hookrun, library, postprocess
from stremio_dl.config import Config
from stremio_dl.engine.torrent import TorrentEngine
from stremio_dl.models import Candidate, EpisodeRef, FileTarget, Title
from stremio_dl.planner import build_plan
from stremio_dl.runner import DownloadRunner
from stremio_dl.store import SCHEMA_V1, SCHEMA_VERSION, Store

from .test_engine import LOCAL, Recorder, _job, seeder  # noqa: F401  (seeder is a fixture)

INCEPTION = Title("tt1375666", "movie", "Inception", 2010)


def _plan(cfg, friends, episodes=(1, 2)):
    keys = [EpisodeRef(1, e) for e in episodes]
    cands = {
        k: [Candidate(f"{i:x}" * 40, 0, "1080p", 50, 10, filename=f"S01E0{k.episode}.mkv")]
        for i, k in enumerate(keys, start=1)
    }
    return build_plan(friends, keys, cands, cfg, "1080p")


# -- store ---------------------------------------------------------------------------------------


def test_new_database_is_wal_and_current(tmp_path):
    store = Store(tmp_path / "state.db")
    assert store.schema_version == SCHEMA_VERSION
    assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    store.close()


def test_database_from_before_versioning_is_migrated_in_place(tmp_path, cfg, friends):
    path = tmp_path / "state.db"
    old = sqlite3.connect(path)
    old.executescript(SCHEMA_V1)
    old.execute(
        "INSERT INTO jobs (title_id, title, type, quality, status, created, updated, plan_json) "
        "VALUES ('tt1', 'Old job', 'series', '1080p', 'done', 'x', 'x', ?)",
        (json.dumps(_plan(cfg, friends).to_dict()),),
    )
    old.commit()
    old.close()

    store = Store(path)
    assert store.schema_version == SCHEMA_VERSION
    assert [r.title for r in store.jobs()] == ["Old job"]  # data kept
    assert store.handoffs(1) == []  # the new table exists
    store.close()


def test_database_from_a_newer_version_is_refused(tmp_path):
    path = tmp_path / "state.db"
    db = sqlite3.connect(path)
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    db.close()
    with pytest.raises(RuntimeError, match="newer"):
        Store(path)


def test_create_job_is_all_or_nothing(tmp_path, cfg, friends, monkeypatch):
    store = Store(tmp_path / "state.db")
    plan = _plan(cfg, friends)
    assert len(plan.jobs) == 2
    real_add = store.add_torrent
    calls = []

    def flaky_add(job_id, job):
        calls.append(job.info_hash)
        if len(calls) == 2:
            raise OSError("disk full")
        real_add(job_id, job)

    monkeypatch.setattr(store, "add_torrent", flaky_add)
    with pytest.raises(OSError):
        store.create_job(plan)
    for table in ("jobs", "items", "torrents"):
        assert store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    store.close()


def test_a_second_connection_can_read_during_a_write(tmp_path, cfg, friends):
    writer = Store(tmp_path / "state.db")
    reader = Store(tmp_path / "state.db")
    with writer._tx():
        writer.create_job(_plan(cfg, friends))
        assert reader.jobs() == []  # WAL: the reader sees the last commit, without blocking
    assert [r.title for r in reader.jobs()] == ["Friends"]
    writer.close()
    reader.close()


# -- hand-offs -----------------------------------------------------------------------------------


def _video(cfg, name="Friends - S01E03 - Thumb.mkv") -> Path:
    video = Path(cfg.tv_root) / "Friends" / "Season 01" / name
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"x")
    return video


def _target(path: Path, *refs: EpisodeRef) -> FileTarget:
    return FileTarget(0, refs, path, 1, Candidate("a" * 40, 0, "1080p", 5, 1))


def _py(code: str) -> str:
    """A post_download_command that runs ``code`` with this Python."""
    return f'"{sys.executable}" -c "{code}"'


@pytest.mark.parametrize(("code", "state"), [(0, "ok"), (3, "failed")])
def test_hand_off_records_the_exit_code(tmp_path, cfg, friends, code, state):
    store = Store(tmp_path / "state.db")
    job_id = store.create_job(_plan(cfg, friends))
    cfg.post_download_command = _py(f"import sys; sys.exit({code})")
    video = _video(cfg)

    proc = postprocess.hand_off(cfg, friends, _target(video, EpisodeRef(1, 3)), "b", 1,
                                store=store, job_id=job_id)  # fmt: skip
    assert proc is not None and proc.wait(timeout=60) == code
    (row,) = store.handoffs(job_id)
    assert (row.state, row.exit_code, row.key, row.path) == (state, code, "S01E03", str(video))
    assert next(r for r in store.jobs() if r.id == job_id).handoffs == {state: 1}
    log_text = Path(row.log_path).read_text(encoding="utf-8")
    assert "running:" in log_text and f"exit code {code}" in log_text
    store.close()


def test_awkward_values_stay_single_arguments_and_are_in_the_environment(tmp_path, cfg):
    script = tmp_path / "record.py"
    script.write_text(
        "import json, os, sys\n"
        "env = {k: v for k, v in os.environ.items() if k.startswith('SDL_')}\n"
        "json.dump({'argv': sys.argv[1:], 'env': env}, open(sys.argv[1], 'w', encoding='utf-8'))\n",
        encoding="utf-8",
    )
    out = tmp_path / "seen.json"
    cfg.post_download_command = (
        f'"{sys.executable}" "{script}" "{out}" "{{name}}" "{{root}}" {{id}}'
    )
    cfg.movie_root = str(tmp_path / "Movies dir") + ("\\" if sys.platform == "win32" else "/")
    title = Title("tt0000001", "movie", 'He said "hi" & left; $(rm -rf x)', 2001)
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"x")

    proc = postprocess.hand_off(cfg, title, _target(video))
    assert proc is not None and proc.wait(timeout=60) == 0
    seen = json.loads(out.read_text(encoding="utf-8"))
    assert seen["argv"][1:] == [title.name, cfg.movie_root, "tt0000001"]  # 3 args, verbatim
    assert seen["env"]["SDL_NAME"] == title.name and seen["env"]["SDL_PATH"] == str(video)
    assert seen["env"]["SDL_TYPE"] == "movie"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows command lines can't be unbalanced")
def test_unbalanced_quotes_are_reported_not_raised(tmp_path, cfg):
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"x")
    cfg.post_download_command = 'tool "{path}'
    assert postprocess.hand_off(cfg, INCEPTION, _target(video)) is None


def test_split_command_follows_the_platform_rules():
    if sys.platform == "win32":
        line = r'"C:\Program Files\tool.exe" -a "b c" "d\\" e'
        assert postprocess.split_command(line) == [r"C:\Program Files\tool.exe", "-a", "b c",
                                                   "d\\", "e"]  # fmt: skip
    else:
        line = "'/opt/my tool' -a \"b c\" d\\ e"
        assert postprocess.split_command(line) == ["/opt/my tool", "-a", "b c", "d e"]
    assert postprocess.split_command("   ") == []


def test_wrapper_records_a_command_that_cannot_start(tmp_path, capsys):
    store = Store(tmp_path / "state.db")
    handoff_id = store.add_handoff(1, "movie", "x.mkv", "b", "x.log")
    store.close()
    code = hookrun.main(["--db", str(tmp_path / "state.db"), "--handoff", str(handoff_id),
                         "--", str(tmp_path / "no-such-program")])  # fmt: skip
    assert code == hookrun.NOT_STARTED
    store = Store(tmp_path / "state.db")
    (row,) = store.handoffs(1)
    assert row.state == "error" and "could not start" in (row.error or "")
    store.close()
    assert "could not start" in capsys.readouterr().out


# -- engine fault isolation ------------------------------------------------------------------


class Exploding(Recorder):
    """A listener whose state recording fails, like a locked or full database."""

    def on_metadata(self, job):
        super().on_metadata(job)
        raise sqlite3.OperationalError("database is locked")

    def on_target_done(self, job, target):
        super().on_target_done(job, target)
        raise OSError("disk full")


def _engine(tmp_path, listener, **kwargs) -> TorrentEngine:
    config = Config(metadata_timeout=20, stall_timeout=30)
    return TorrentEngine(config, tmp_path / "staging", listener, poll_interval=0.1,
                         settings=LOCAL, **kwargs)  # fmt: skip


@pytest.mark.torrent
async def test_a_failing_listener_does_not_stop_the_download(tmp_path, seeder):  # noqa: F811
    library_dir = tmp_path / "library"
    idx = seeder["index"]["Show.S01E02.1080p.WEB.mkv"]
    rec = Exploding()
    engine = _engine(tmp_path, rec)
    engine.add_job(_job(seeder, library_dir, 2, idx, "Show.S01E02.1080p.WEB.mkv"))
    await asyncio.wait_for(engine.run(), 60)
    engine.shutdown()

    assert (library_dir / "Show" / "Season 01" / "Show - S01E02 - Pilot.mkv").exists()
    assert rec.jobs_done == [seeder["info_hash"]]  # later callbacks still ran
    assert [e.split(":")[0] for e in engine.listener_errors] == ["on_metadata", "on_target_done"]


@pytest.mark.torrent
async def test_a_file_that_cannot_be_renamed_is_reported_failed(tmp_path, seeder):  # noqa: F811
    library_dir = tmp_path / "library"
    final = library_dir / "Show" / "Season 01" / "Show - S01E02 - Pilot.mkv"
    final.mkdir(parents=True)  # a folder where the file should go: the final rename must fail
    idx = seeder["index"]["Show.S01E02.1080p.WEB.mkv"]
    rec = Recorder()
    engine = _engine(tmp_path, rec, rename_attempts=2, rename_retry_delay=0.05)
    engine.add_job(_job(seeder, library_dir, 2, idx, "Show.S01E02.1080p.WEB.mkv"))
    await asyncio.wait_for(engine.run(), 60)
    engine.shutdown()

    assert rec.done == [] and [label for label, _ in rec.failed] == ["S01E02"]
    assert "could not rename" in rec.failed[0][1]
    (view,) = engine.snapshot()
    assert view.targets[0].state == "failed"


def test_runner_marks_a_failed_target_and_the_job_fails(tmp_path, cfg, friends):
    store = Store(tmp_path / "state.db")
    plan = _plan(cfg, friends, episodes=(1,))
    job_id = store.create_job(plan)
    (job,) = plan.jobs
    run = DownloadRunner(cfg, store, job_id, plan)
    run.on_target_failed(job, job.targets[0], "could not rename x.part: denied")
    store.update_torrent(job_id, job.info_hash, status="done")
    assert store.finish_job(job_id) == "failed"
    (row,) = store.jobs()
    assert row.counts == {"failed": 1}
    store.close()


# -- small hardening -------------------------------------------------------------------------


def test_user_agent_names_the_app_honestly():
    ua = Config().user_agent
    assert "stremio-dl/" in ua and "github.com" in ua and "Windows NT" not in ua


def test_paths_cannot_leave_the_library_root(cfg, friends, monkeypatch):
    monkeypatch.setattr(library, "sanitize", lambda name, max_len=150: name)  # remove layer 1
    cfg.tv_template = "../../{show}/{show} - {code}{ext}"
    with pytest.raises(ValueError, match="outside the library root"):
        library.tv_path(cfg, friends, (EpisodeRef(1, 1),), ".mkv")
