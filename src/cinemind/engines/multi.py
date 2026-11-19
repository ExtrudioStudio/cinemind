"""Run torrent and direct downloads side by side, in one loop.

Jobs go to the engine that handles their kind. Replacement jobs (a dead torrent's fallback can
be a direct download, and the other way round) come back through :meth:`Engines.add_job`, so
they land in the right engine too. libtorrent is only imported when a torrent job shows up, so
cinemind works without it for direct downloads.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from cinemind.core.config import Config
from cinemind.core.models import TorrentJob
from cinemind.engines.base import Downloader, EngineListener, JobView
from cinemind.engines.http import HttpEngine

if TYPE_CHECKING:
    from cinemind.engines.torrent import TorrentEngine


class TorrentSupportMissing(RuntimeError):
    """A torrent download was planned, but libtorrent isn't installed."""


class Engines:
    def __init__(
        self,
        cfg: Config,
        staging_root: Path,
        listener: EngineListener | None = None,
        poll_interval: float = 0.5,
    ):
        self.cfg = cfg
        self.staging_root = staging_root
        self.listener = listener
        self.poll_interval = poll_interval
        self.http = HttpEngine(cfg, listener, submit=self.add_job)
        self._torrent: TorrentEngine | None = None

    @property
    def torrent(self) -> TorrentEngine:
        if self._torrent is None:
            try:
                from cinemind.engines.torrent import TorrentEngine
            except ImportError as exc:
                raise TorrentSupportMissing(
                    "This download needs BitTorrent support, which isn't installed. "
                    "Install it with: pip install 'cinemind[torrent]'"
                ) from exc
            self._torrent = TorrentEngine(self.cfg, self.staging_root, self.listener,
                                          poll_interval=self.poll_interval,
                                          submit=self.add_job)  # fmt: skip
        return self._torrent

    @property
    def engines(self) -> list[Downloader]:
        running: list[Downloader] = [self.http]
        if self._torrent is not None:
            running.insert(0, self._torrent)
        return running

    @property
    def listener_errors(self) -> list[str]:
        return [e for engine in self.engines for e in engine.listener_errors]

    def add_job(self, job: TorrentJob, resume_data: bytes | None = None) -> None:
        engine: Downloader = self.http if job.is_direct else self.torrent
        engine.add_job(job, resume_data)

    async def run(self, on_tick: Callable[[list[JobView]], None] | None = None) -> None:
        try:
            while not all(engine.idle for engine in self.engines):
                for engine in self.engines:
                    engine.step()
                if on_tick:
                    on_tick(self.snapshot())
                await asyncio.sleep(self.poll_interval)
            for engine in self.engines:
                engine.finish()
            if on_tick:
                on_tick(self.snapshot())
        except asyncio.CancelledError:
            self.shutdown(save_resume=True)
            raise
        finally:
            await self.http.aclose()

    def shutdown(self, save_resume: bool = True) -> None:
        for engine in self.engines:
            engine.shutdown(save_resume)

    def snapshot(self) -> list[JobView]:
        return [view for engine in self.engines for view in engine.snapshot()]
