from __future__ import annotations

import json
import sys
from pathlib import Path

from stremio_dl import postprocess, runner
from stremio_dl.models import Candidate, EpisodeRef, FileTarget, Title
from stremio_dl.planner import build_plan

INCEPTION = Title("tt1375666", "movie", "Inception", 2010)


def target(path: Path, *refs: EpisodeRef) -> FileTarget:
    return FileTarget(0, refs, path, 10, Candidate("a" * 40, 0, "1080p", 5, 10))


def recorder(tmp_path: Path) -> tuple[str, Path]:
    """A command that writes the arguments it got to a JSON file."""
    script = tmp_path / "record.py"
    script.write_text(
        "import json, sys\n"
        "out, *rest = sys.argv[1:]\n"
        "open(out, 'w', encoding='utf-8').write(json.dumps(rest))\n",
        encoding="utf-8",
    )
    out = tmp_path / "args.json"
    fields = " ".join(f'"{{{name}}}"' for name in ("path", "id", "imdb", "type", "season", "root"))
    return f'"{sys.executable}" "{script}" "{out}" {fields}', out


def test_fields_for_an_episode_and_a_movie(cfg, friends):
    ep = target(Path(cfg.tv_root) / "Friends" / "x.mkv", EpisodeRef(1, 3))
    fields = postprocess.command_fields(cfg, friends, ep)
    assert fields["id"] == "tt0108778:1:3" and fields["imdb"] == "tt0108778"
    assert (fields["type"], fields["season"], fields["episode"]) == ("series", "1", "3")
    assert fields["root"] == cfg.tv_root

    movie = postprocess.command_fields(cfg, INCEPTION, target(Path("i.mkv")))
    assert (movie["id"], movie["season"], movie["root"]) == ("tt1375666", "", cfg.movie_root)

    batch = postprocess.command_fields(cfg, INCEPTION, target(Path("i.mkv")), "sdl-job-4-x", 13)
    assert (batch["batch"], batch["batch_size"]) == ("sdl-job-4-x", "13")
    assert postprocess.new_batch("job", 4).startswith("sdl-job-4-")

    quoted = Title("kitsu:12", "movie", 'The "Best" Movie')
    fields = postprocess.command_fields(cfg, quoted, target(Path("b.mkv")))
    assert fields["imdb"] == "" and fields["name"] == "The 'Best' Movie"


def test_hand_off_runs_the_command_and_keeps_a_log(cfg, friends, tmp_path):
    video = Path(cfg.tv_root) / "Friends" / "Season 01" / "Friends - S01E03 - Thumb.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"x")
    cfg.post_download_command, out = recorder(tmp_path)

    proc = postprocess.hand_off(cfg, friends, target(video, EpisodeRef(1, 3)))
    assert proc is not None and proc.wait(timeout=60) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == [
        str(video), "tt0108778:1:3", "tt0108778", "series", "1", cfg.tv_root
    ]  # fmt: skip
    assert [p.suffix for p in postprocess.log_folder().iterdir()] == [".log"]


def test_hand_off_is_off_by_default_and_skips_unfinished_files(cfg, tmp_path):
    video = tmp_path / "Inception (2010).mkv"
    assert postprocess.hand_off(cfg, INCEPTION, target(video)) is None  # no command set
    cfg.post_download_command, _ = recorder(tmp_path)
    assert postprocess.hand_off(cfg, INCEPTION, target(video)) is None  # file isn't there
    video.write_bytes(b"x")
    cfg.post_download_command = "tool {path} {nope}"
    assert postprocess.hand_off(cfg, INCEPTION, target(video)) is None  # reported, not raised


def test_handoff_command_sends_a_finished_jobs_remaining_files(cfg, friends, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from stremio_dl import cli
    from stremio_dl.config import data_dir, save_config
    from stremio_dl.store import Store

    keys = [EpisodeRef(1, 1), EpisodeRef(1, 2)]
    pack = [Candidate("b" * 40, i, "1080p", 50, 10, filename=f"S01E0{i + 1}.mkv") for i in (0, 1)]
    plan = build_plan(friends, keys, {keys[0]: [pack[0]], keys[1]: [pack[1]]}, cfg, "1080p")
    (job,) = plan.jobs
    store = Store(data_dir() / "state.db")
    job_id = store.create_job(plan)
    store.update_torrent(job_id, job.info_hash, status="done", job=job)
    store.close()
    kept = job.targets[0].final_path  # the other episode was already moved away
    kept.parent.mkdir(parents=True)
    kept.write_bytes(b"x")

    seen: list[Path] = []

    batches: set[tuple[str, int]] = set()

    def fake_hand_off(c, t, tgt, batch="", batch_size=0) -> bool:
        if tgt.final_path.is_file():
            seen.append(tgt.final_path)
            batches.add((batch.rsplit("-", 2)[0], batch_size))
            return True
        return False

    monkeypatch.setattr(postprocess, "hand_off", fake_hand_off)
    runner_cli = CliRunner()
    save_config(cfg)  # post_download_command still empty
    assert runner_cli.invoke(cli.app, ["handoff", str(job_id)]).exit_code == 1
    cfg.post_download_command = "tool {path}"
    save_config(cfg)
    result = runner_cli.invoke(cli.app, ["handoff", str(job_id)])
    assert result.exit_code == 0 and seen == [kept] and "1 file(s) handed" in result.output
    assert batches == {(f"sdl-handoff-{job_id}", 1)}  # one batch, sized to the files still there
    assert runner_cli.invoke(cli.app, ["handoff", "999"]).exit_code == 1


class _Store:
    def set_item(self, *args, **kwargs): ...

    def update_torrent(self, *args, **kwargs): ...


def test_runner_hands_files_off_only_once_the_torrent_is_done(monkeypatch, cfg, friends):
    seen: list[Path] = []
    calls: list[tuple[str, int]] = []

    def fake_hand_off(c, t, tgt, batch="", batch_size=0):
        seen.append(tgt.final_path)
        calls.append((batch, batch_size))
        return 1

    monkeypatch.setattr(runner, "hand_off", fake_hand_off)
    keys = [EpisodeRef(1, 1), EpisodeRef(1, 2)]
    pack = [Candidate("b" * 40, i, "1080p", 50, 10, filename=f"S01E0{i + 1}.mkv") for i in (0, 1)]
    plan = build_plan(friends, keys, {keys[0]: [pack[0]], keys[1]: [pack[1]]}, cfg, "1080p")
    (job,) = plan.jobs

    run = runner.DownloadRunner(cfg, _Store(), 1, plan)
    run.on_target_done(job, job.targets[0])
    assert seen == []  # the rest of the pack is still downloading from this torrent
    run.on_job_done(job)
    assert seen == [t.final_path for t in job.targets] == run.handed_off
    assert len(set(calls)) == 1 and calls[0][0].startswith("sdl-job-1-") and calls[0][1] == 2
