"""The downloader contract shared by the torrent and HTTP engines, plus their progress views."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from cinemind.core.models import FileTarget, TorrentJob

log = logging.getLogger(__name__)

NOT_IN_TORRENT = "file not in torrent"  # on_job_dead reason: re-plan these files only


@dataclass
class TargetView:
    label: str
    name: str
    size: int
    done: int
    state: str  # queued | metadata | downloading | done | failed | vpn down


@dataclass
class JobView:
    info_hash: str
    release: str
    state: str  # queued | metadata | downloading | seeding | done | dead
    down_rate: int = 0
    up_rate: int = 0
    peers: int = 0
    seeds: int = 0
    targets: list[TargetView] = field(default_factory=list)
    error: str = ""


class EngineListener(Protocol):
    def on_metadata(self, job: TorrentJob) -> None: ...

    def on_target_done(self, job: TorrentJob, target: FileTarget) -> None: ...

    def on_target_failed(self, job: TorrentJob, target: FileTarget, reason: str) -> None:
        """A file finished downloading but could not be put in place (it stays as ``.part``)."""
        ...

    def on_job_done(self, job: TorrentJob) -> None: ...

    def on_job_dead(
        self, job: TorrentJob, reason: str, remaining: list[FileTarget]
    ) -> list[TorrentJob]:
        """Called when a download is abandoned; returns replacement jobs to try next."""
        ...

    def on_resume_data(self, info_hash: str, data: bytes) -> None: ...


Submit = Callable[[TorrentJob], None]


class Downloader(Protocol):
    """One kind of download. :class:`cinemind.engines.multi.Engines` drives several at once."""

    listener_errors: list[str]

    def add_job(self, job: TorrentJob, resume_data: bytes | None = None) -> None: ...

    @property
    def idle(self) -> bool:
        """Nothing queued and nothing running."""
        ...

    def step(self) -> None:
        """Do one round of work: start queued jobs, check progress, settle finished ones."""
        ...

    def finish(self) -> None:
        """Last bookkeeping once idle (late alerts, cleanup)."""
        ...

    def shutdown(self, save_resume: bool = True) -> None: ...

    def snapshot(self) -> list[JobView]: ...


class ListenerMixin:
    """Guarded listener calls, shared by the engines."""

    listener: EngineListener | None
    listener_errors: list[str]

    def _notify(self, method: str, *args: Any) -> Any:
        """Call one listener method. If it raises, log it and carry on.

        The listener records state (SQLite) and starts hand-offs. A failure there must not
        stop every other download, so it is logged and kept in ``listener_errors``.
        """
        if self.listener is None:
            return None
        try:
            return getattr(self.listener, method)(*args)
        except Exception as exc:
            log.exception("Listener %s failed", method)
            self.listener_errors.append(f"{method}: {exc}")
            return None
