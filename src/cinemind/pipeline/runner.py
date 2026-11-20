"""Glue between a plan, the download engines, the state store and the live progress display.

When a torrent (or direct download) finishes, its files go to the delivery stages (subtitle,
ingest; see :mod:`cinemind.pipeline.delivery`) if they are turned on, and then to the
post-download command, with the path where each file ended up.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import subprocess
from contextlib import nullcontext
from pathlib import Path

from rich.live import Live

from cinemind.ai.judge import judge_from_config
from cinemind.cli.ui import console, delivery_line, progress_table
from cinemind.core.config import Config, data_dir
from cinemind.core.logs import log_context
from cinemind.core.models import FileTarget, TorrentJob
from cinemind.core.store import DeliveryRow, Store, key_name
from cinemind.engines.base import NOT_IN_TORRENT, JobView
from cinemind.engines.multi import Engines
from cinemind.pipeline.delivery import Delivery
from cinemind.pipeline.postprocess import hand_off, new_batch
from cinemind.planning.matching import PackFile, Wanted, resolve
from cinemind.planning.planner import Plan, replan

log = logging.getLogger(__name__)


class DownloadRunner:
    """Runs one stored job to completion; implements :class:`EngineListener`."""

    def __init__(self, cfg: Config, store: Store, job_id: int, plan: Plan):
        self.cfg = cfg
        self.store = store
        self.job_id = job_id
        self.plan = plan
        self.judge = judge_from_config(cfg, store)  # for files the names don't place
        self.engine = Engines(cfg, data_dir() / "staging", listener=self,
                              matcher=self.match_files if self.judge else None)  # fmt: skip
        self.handed_off: list[Path] = []  # files given to cfg.post_download_command
        self.batch = new_batch("job", job_id)  # this run's files form one batch for that command
        self.batch_size = sum(len(job.targets) for job in plan.jobs)
        self._hooks: list[subprocess.Popen[bytes]] = []  # running hand-off wrappers
        self.views: list[JobView] = []  # the latest progress snapshot
        self.display = True
        self.delivery: Delivery | None = None
        if Delivery.wanted(cfg):
            self.delivery = Delivery(cfg, store, job_id, plan.title,
                                     on_delivered=self._after_delivery,
                                     on_update=self._show_delivery)  # fmt: skip

    async def match_files(self, job: TorrentJob, targets: list[FileTarget],
                          files: list[PackFile]) -> list[int | None]:  # fmt: skip
        """The torrent engine's matcher: the same :func:`resolve` the pack-matching eval
        measures, for each target the name rules couldn't place, one at a time."""
        chosen: list[int | None] = []
        free = list(files)
        for target in targets:
            wanted = Wanted.from_title(self.plan.title, target.refs, job.release)
            found = await resolve(wanted, free, self.judge, target.candidate.filename,
                                  target.file_idx)  # fmt: skip
            if found.how == "judge":
                log.info("Judge placed %s in %s: %s (%s)", target.label, job.release,
                         found.index, found.reason)  # fmt: skip
            elif found.rejected:
                log.info("Judge's pick for %s in %s thrown out: %s", target.label, job.release,
                         found.rejected)  # fmt: skip
            chosen.append(found.index)
            free = [f for f in free if f.index != found.index]
        return chosen

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
        # delivery or the post-download command may move it away.
        for target in job.targets:
            if self.delivery is not None:
                if target.final_path.is_file():
                    self.delivery.submit(target)  # the hand-off follows the delivery
                continue
            self._hand_off(target)

    def _hand_off(self, target: FileTarget) -> None:
        proc = hand_off(self.cfg, self.plan.title, target, self.batch, self.batch_size,
                        store=self.store, job_id=self.job_id)  # fmt: skip
        if proc:
            self.handed_off.append(target.final_path)
            self._hooks.append(proc)

    def _after_delivery(self, key: str, path: Path) -> None:
        """The post-download command, on the file where the delivery left it."""
        for job in [*self.plan.jobs, *self.store.finished_torrents(self.job_id)]:
            for target in job.targets:
                if key_name(target.refs[0] if target.refs else None) == key:
                    self._hand_off(dataclasses.replace(target, final_path=path))
                    return

    def _show_delivery(self, row: DeliveryRow) -> None:
        if self.display:
            console.print(delivery_line(row))

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
        latest progress is kept in :attr:`views` instead. Everything logged meanwhile, in the
        engines and the deliveries too, carries the job's id.
        """
        with log_context(job_id=self.job_id):
            files = sum(len(job.targets) for job, _ in torrents)
            log.info("Job #%s (%s): %d file(s) to fetch", self.job_id, self.plan.title.name, files)
            try:
                status = await self._run(torrents, display)
            except asyncio.CancelledError:
                log.info("Job #%s paused", self.job_id)
                raise
            log.info("Job #%s %s", self.job_id, status)
            return status

    async def _run(self, torrents: list[tuple[TorrentJob, bytes | None]], display: bool) -> str:
        self.display = display
        self.batch_size = sum(len(job.targets) for job, _ in torrents) or self.batch_size
        for job, resume in torrents:
            self.engine.add_job(job, resume)
        if self.delivery is not None:
            self.delivery.resume()  # files an earlier run didn't finish delivering
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
            if self.delivery is not None and self.delivery.busy:
                if live is not None:
                    live.stop()
                waiting = f"Delivering {self.delivery.busy} file(s)…"
                with console.status(waiting) if display else nullcontext():
                    await self.delivery.wait()
        except asyncio.CancelledError:
            if self.delivery is not None:
                await self.delivery.aclose()  # they stay in their stage for `resume`
            self.store.pause_open(self.job_id)
            self.store.finish_job(self.job_id)
            raise
        finally:
            if self.delivery is not None:
                await self.delivery.aclose()  # only does something after an error above
            self.engine.shutdown(save_resume=True)  # no-op when run() already shut down
            self.reap()
            if live is not None:
                live.stop()
        return self.store.finish_job(self.job_id)
