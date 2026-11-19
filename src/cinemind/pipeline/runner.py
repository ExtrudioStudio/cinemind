"""Glue between a plan, the torrent engine, the state store and the live progress display."""

from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path

from rich.live import Live

from cinemind.cli.ui import console, progress_table
from cinemind.core.config import Config, data_dir
from cinemind.core.models import FileTarget, TorrentJob
from cinemind.core.store import Store
from cinemind.engines.base import NOT_IN_TORRENT, JobView
from cinemind.engines.multi import Engines
from cinemind.pipeline.postprocess import hand_off, new_batch
from cinemind.planning.planner import Plan, replan

log = logging.getLogger(__name__)


class DownloadRunner:
    """Runs one stored job to completion; implements :class:`EngineListener`."""

    def __init__(self, cfg: Config, store: Store, job_id: int, plan: Plan):
        self.cfg = cfg
        self.store = store
        self.job_id = job_id
        self.plan = plan
        self.engine = Engines(cfg, data_dir() / "staging", listener=self)
        self.handed_off: list[Path] = []  # files given to cfg.post_download_command
        self.batch = new_batch("job", job_id)  # this run's files form one batch for that command
        self.batch_size = sum(len(job.targets) for job in plan.jobs)
        self._hooks: list[subprocess.Popen[bytes]] = []  # running hand-off wrappers
        self.views: list[JobView] = []  # the latest progress snapshot

    # -- EngineListener --------------------------------------------------------------------

    def on_metadata(self, job: TorrentJob) -> None:
        self.store.update_torrent(self.job_id, job.info_hash, status="active", job=job)
        for target in job.targets:
            for key in target.refs or (None,):
                self.store.set_item(self.job_id, key, "downloading", info_hash=job.info_hash,
                                    path=str(target.final_path))  # fmt: skip

    def on_target_done(self, job: TorrentJob, target: FileTarget) -> None:
        for key in target.refs or (None,):
            self.store.set_item(self.job_id, key, "done", path=str(target.final_path))
        self.store.update_torrent(self.job_id, job.info_hash, job=job)

    def on_target_failed(self, job: TorrentJob, target: FileTarget, reason: str) -> None:
        for key in target.refs or (None,):
            self.store.set_item(self.job_id, key, "failed", error=reason)

    def on_job_done(self, job: TorrentJob) -> None:
        self.store.update_torrent(self.job_id, job.info_hash, status="done", job=job)
        # Only now, with the torrent removed from the session: earlier (on_target_done) the
        # file may still be open for the rest of a season pack or for seeding, and the
        # post-download command may move it away.
        for target in job.targets:
            proc = hand_off(self.cfg, self.plan.title, target, self.batch, self.batch_size,
                            store=self.store, job_id=self.job_id)  # fmt: skip
            if proc:
                self.handed_off.append(target.final_path)
                self._hooks.append(proc)

    def reap(self) -> None:
        """Collect hand-off wrappers that have exited, so they don't linger as zombies.

        Their results are already in the store (the wrapper writes them); this only releases
        the processes. Wrappers still running when we quit carry on by themselves.
        """
        self._hooks = [p for p in self._hooks if p.poll() is None]

    def on_job_dead(
        self, job: TorrentJob, reason: str, remaining: list[FileTarget]
    ) -> list[TorrentJob]:
        if reason != NOT_IN_TORRENT:  # otherwise the torrent keeps serving its other files
            self.store.update_torrent(self.job_id, job.info_hash, status="dead", error=reason)
        keys = [key for t in remaining for key in (t.refs or (None,))]
        replacements = replan(self.plan, self.cfg, job.info_hash, keys)
        for key in keys:
            if key in self.plan.missing:
                self.store.set_item(self.job_id, key, "failed", error=self.plan.missing[key])
        for new_job in replacements:
            self.store.add_torrent(self.job_id, new_job)
        self.store.save_plan(self.job_id, self.plan)
        return replacements

    def on_resume_data(self, info_hash: str, data: bytes) -> None:
        self.store.update_torrent(self.job_id, info_hash, resume=data)

    # -- running ---------------------------------------------------------------------------

    async def run(
        self, torrents: list[tuple[TorrentJob, bytes | None]], display: bool = True
    ) -> str:
        """Download the job's open torrents and direct downloads; return the job's final status.

        With ``display=False`` nothing is printed (the MCP server talks over stdout); the
        latest progress is kept in :attr:`views` instead.
        """
        self.batch_size = sum(len(job.targets) for job, _ in torrents) or self.batch_size
        for job, resume in torrents:
            self.engine.add_job(job, resume)
        live = Live(progress_table([]), console=console, refresh_per_second=4) if display else None

        def tick(views: list[JobView]) -> None:
            self.views = views
            if live is not None:
                live.update(progress_table(views))
            self.reap()

        if live is not None:
            live.start()
        try:
            await self.engine.run(on_tick=tick)
        except asyncio.CancelledError:
            self.store.pause_open(self.job_id)
            self.store.finish_job(self.job_id)
            raise
        finally:
            self.engine.shutdown(save_resume=True)  # no-op when run() already shut down
            self.reap()
            if live is not None:
                live.stop()
        return self.store.finish_job(self.job_id)
