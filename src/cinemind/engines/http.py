"""Direct HTTP downloads (for example from the Internet Archive), with resume and checksums.

Each job is one file, written to ``<final path>.part``. An interrupted download continues with
a ``Range`` request, and a server that ignores ``Range`` starts it over. Once the bytes are in,
the size is checked and, when the source publishes an MD5, the file is hashed (in a worker
thread); a mismatch fails the job and deletes the part, so the next candidate is tried. Only
then is the file renamed into place: the library never holds a half-written file under its
real name.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from cinemind.core.config import Config
from cinemind.core.models import FileTarget, TorrentJob
from cinemind.engines.base import EngineListener, JobView, ListenerMixin, Submit, TargetView

log = logging.getLogger(__name__)

CHUNK = 1 << 20
TRANSPORT_RETRIES = 3


class HttpDownloadError(Exception):
    pass


def _part(target: FileTarget) -> Path:
    return target.final_path.with_name(target.final_path.name + ".part")


def _hash_file(path: Path, digest: Any) -> None:
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)


@dataclass
class _Download:
    job: TorrentJob
    target: FileTarget
    task: asyncio.Task[None] | None = None
    done_bytes: int = 0
    total: int = 0
    last_progress_at: float = field(default_factory=time.monotonic)
    rate: int = 0
    _rate_mark: tuple[float, int] = (0.0, 0)
    error: str = ""

    def view(self, state: str) -> JobView:
        tstate = {"done": "done", "dead": "failed"}.get(state, "downloading")
        size = self.total or self.target.size
        got = size if state == "done" else self.done_bytes
        target = TargetView(self.target.label, self.target.final_path.name, size, got, tstate)
        return JobView(self.job.info_hash, self.job.release, state, down_rate=self.rate,
                       targets=[target], error=self.error)  # fmt: skip


class HttpEngine(ListenerMixin):
    def __init__(
        self,
        cfg: Config,
        listener: EngineListener | None = None,
        submit: Submit | None = None,
        http: httpx.AsyncClient | None = None,
        retry_wait: float = 1.0,
    ):
        self.cfg = cfg
        self.listener = listener
        self.submit: Submit = submit or self.add_job
        self.listener_errors: list[str] = []
        self.pending: deque[TorrentJob] = deque()
        self.active: dict[str, _Download] = {}
        self.finished: list[JobView] = []
        self._http = http
        self._own_http = http is None
        self._retry_wait = retry_wait

    # -- jobs ------------------------------------------------------------------------------

    def add_job(self, job: TorrentJob, resume_data: bytes | None = None) -> None:
        """Queue a direct download. ``resume_data`` is unused: the ``.part`` file is the state."""
        if job.info_hash in self.active or any(j.info_hash == job.info_hash for j in self.pending):
            return
        self.pending.append(job)

    @property
    def idle(self) -> bool:
        return not self.pending and not self.active

    def step(self) -> None:
        now = time.monotonic()
        for key, d in list(self.active.items()):
            if d.task is not None and d.task.done():
                del self.active[key]
                self._settle(d)
            elif now - d.last_progress_at > self.cfg.stall_timeout and d.task is not None:
                d.error = f"no progress for {self.cfg.stall_timeout}s"
                d.task.cancel()  # settled on the next step
            else:
                self._update_rate(d, now)
        while self.pending and len(self.active) < max(1, self.cfg.max_active_http):
            job = self.pending.popleft()
            d = _Download(job, job.targets[0])
            d.task = asyncio.get_running_loop().create_task(self._download(d))
            self.active[job.info_hash] = d
            log.info("Downloading %s from %s", d.target.final_path.name, d.target.candidate.url)

    def finish(self) -> None:
        pass

    def shutdown(self, save_resume: bool = True) -> None:
        """Stop every download. The ``.part`` files stay, so a later run resumes them."""
        for d in self.active.values():
            if d.task is not None:
                d.task.cancel()

    async def aclose(self) -> None:
        if self._http is not None and self._own_http:
            await self._http.aclose()
            self._http = None

    def snapshot(self) -> list[JobView]:
        views = list(self.finished)
        views += [d.view("downloading") for d in self.active.values()]
        for job in self.pending:
            t = job.targets[0]
            queued = TargetView(t.label, t.final_path.name, t.size, 0, "queued")
            views.append(JobView(job.info_hash, job.release, "queued", targets=[queued]))
        return views

    # -- internals -------------------------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                headers={"User-Agent": self.cfg.user_agent},
                follow_redirects=True,
                timeout=httpx.Timeout(self.cfg.http_timeout, read=60.0),
            )
        return self._http

    @staticmethod
    def _update_rate(d: _Download, now: float) -> None:
        mark_t, mark_bytes = d._rate_mark
        if now - mark_t >= 1.0:
            if mark_t:
                d.rate = int((d.done_bytes - mark_bytes) / (now - mark_t))
            d._rate_mark = (now, d.done_bytes)

    def _settle(self, d: _Download) -> None:
        assert d.task is not None
        if d.task.cancelled():
            reason = d.error or "cancelled"
        else:
            exc = d.task.exception()
            reason = "" if exc is None else (str(exc) or type(exc).__name__)
        if not reason:
            log.info("Finished %s -> %s", d.target.label, d.target.final_path)
            self.finished.append(d.view("done"))
            self._notify("on_target_done", d.job, d.target)
            self._notify("on_job_done", d.job)
            return
        d.error = reason
        log.warning("Giving up on %s: %s", d.target.candidate.url, reason)
        self.finished.append(d.view("dead"))
        for replacement in self._notify("on_job_dead", d.job, reason, [d.target]) or []:
            self.submit(replacement)

    async def _download(self, d: _Download) -> None:
        final, part = d.target.final_path, _part(d.target)
        expected = d.target.candidate.size or d.target.size
        if expected and final.is_file() and final.stat().st_size == expected:
            d.done_bytes = d.total = expected
            return  # finished on an earlier run
        final.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(TRANSPORT_RETRIES):
            try:
                await self._fetch(d, part, expected)
                break
            except httpx.TransportError as exc:
                if attempt == TRANSPORT_RETRIES - 1:
                    raise HttpDownloadError(f"could not download: {exc}") from exc
                log.warning("Download of %s interrupted (%s); resuming", final.name, exc)
                await asyncio.sleep(self._retry_wait * 2**attempt)
        await asyncio.to_thread(self._verify, d, part, expected)
        os.replace(part, final)

    async def _fetch(self, d: _Download, part: Path, expected: int) -> None:
        url = d.target.candidate.url
        offset = part.stat().st_size if part.exists() else 0
        if expected and offset > expected:
            part.unlink()
            offset = 0
        if expected and offset == expected:
            d.done_bytes = d.total = expected
            return  # complete .part from an earlier run; only the checks are left
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        async with self._client().stream("GET", url, headers=headers) as resp:
            if resp.status_code == 200 and offset:
                offset = 0  # the server ignored Range: start over
            elif resp.status_code not in (200, 206):
                raise HttpDownloadError(f"HTTP {resp.status_code} from {url}")
            length = int(resp.headers.get("content-length") or 0)
            d.total = expected or (offset + length)
            d.done_bytes = offset
            with part.open("ab" if offset else "wb") as fh:
                # No chunk size: write bytes as they arrive. A fixed size would hold up to that
                # much back, and lose it if the connection drops before the chunk fills.
                async for chunk in resp.aiter_bytes():
                    fh.write(chunk)
                    d.done_bytes += len(chunk)
                    d.last_progress_at = time.monotonic()

    @staticmethod
    def _verify(d: _Download, part: Path, expected: int) -> None:
        size = part.stat().st_size
        if expected and size != expected:
            raise HttpDownloadError(f"got {size} bytes, expected {expected}")
        want = d.target.candidate.md5
        if want:
            digest = hashlib.md5(usedforsecurity=False)
            _hash_file(part, digest)
            if digest.hexdigest() != want.lower():
                part.unlink()
                raise HttpDownloadError(f"MD5 mismatch for {d.target.final_path.name}")


__all__ = ["HttpDownloadError", "HttpEngine"]
