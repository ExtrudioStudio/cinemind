"""Headless operations shared by the command line and the MCP server.

Find a title, collect what the sources offer, turn that into a plan, and run downloads in the
background. Nothing here prints or prompts: callers decide how to show results and when to ask
the user.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from cinemind.core.config import Config, cache_dir
from cinemind.core.models import Candidate, Key, MediaType, SearchResult, Title
from cinemind.core.store import Store
from cinemind.engines.base import JobView
from cinemind.pipeline.delivery import Delivery, reopen
from cinemind.pipeline.runner import DownloadRunner
from cinemind.planning.planner import Plan, build_plan
from cinemind.planning.selection import Selection
from cinemind.planning.streams import StreamResolver
from cinemind.sources.base import SourceContext
from cinemind.sources.registry import load_sources
from cinemind.sources.stremio.cache import JsonCache
from cinemind.sources.stremio.catalog import search
from cinemind.sources.stremio.client import Addon, AddonClient, AddonError
from cinemind.sources.stremio.metadata import fetch_title

log = logging.getLogger(__name__)


@dataclass
class Session:
    cfg: Config
    client: AddonClient
    meta_addons: list[Addon]
    resolver: StreamResolver


async def _stream_addons(
    client: AddonClient, cfg: Config, warn: Callable[[str], None]
) -> list[Addon]:
    """The configured Stremio stream addons. Unlike metadata, they are optional."""
    if not cfg.stream_addons:
        return []
    try:
        return await client.load_all(cfg.stream_addons)
    except AddonError as exc:
        warn(f"{exc} Continuing with the other sources.")
        return []


@asynccontextmanager
async def open_session(
    cfg: Config, warn: Callable[[str], None] | None = None
) -> AsyncIterator[Session]:
    """Load the metadata addons and the stream sources, for one or more operations."""
    warn = warn or log.warning
    async with AddonClient(cfg, JsonCache(cache_dir() / "http")) as client:
        meta_addons = await client.load_all(cfg.meta_addons)
        stream_addons = await _stream_addons(client, cfg, warn)
        sources = load_sources(cfg.stream_sources, SourceContext(cfg, client, stream_addons))
        yield Session(cfg, client, meta_addons, StreamResolver(sources, cfg))


def is_imdb_id(text: str) -> bool:
    return text.startswith("tt") and text[2:].isdigit()


async def search_titles(
    sess: Session, query: str, type_: MediaType | None = None
) -> list[SearchResult]:
    types: tuple[MediaType, ...] = (type_,) if type_ else ("series", "movie")
    return await search(sess.client, sess.meta_addons, query, types)


async def load_title(sess: Session, title_id: str, type_: MediaType | None = None) -> Title:
    """A title with its episode list. LookupError when no metadata addon knows the id."""
    types: list[MediaType] = [type_] if type_ else ["series", "movie"]
    for t in types:
        try:
            return await fetch_title(sess.client, sess.meta_addons, t, title_id,
                                     sess.cfg.include_specials)  # fmt: skip
        except AddonError:
            continue
    raise LookupError(f"No metadata found for {title_id}")


@dataclass
class Offers:
    """What the sources offer for a selection; plans can be built from it more than once."""

    title: Title
    keys: list[Key]
    requested: str
    candidates: dict[Key, list[Candidate]]

    def plan(self, cfg: Config, on_missing: str = "skip", prefer_packs: bool | None = None) -> Plan:
        packs = cfg.prefer_packs if prefer_packs is None else prefer_packs
        return build_plan(self.title, self.keys, self.candidates, cfg, self.requested,
                          on_missing, packs)  # fmt: skip

    def missing_at_requested(self, plan: Plan) -> list[Key]:
        """Keys with no copy at the requested quality (a lower one might exist)."""
        prefix = f"no {self.requested} streams"
        return [k for k, reason in plan.missing.items() if reason.startswith(prefix)]


async def find_offers(
    sess: Session,
    title: Title,
    selection: Selection,
    on_progress: Callable[[Key], None] | None = None,
) -> Offers:
    requested = selection.quality or sess.cfg.default_quality
    keys: list[Key] = list(selection.refs) if title.type == "series" else [None]
    candidates = await sess.resolver.all_candidates(title, keys, on_progress=on_progress)
    return Offers(title, keys, requested, candidates)


RunnerFactory = Callable[[Config, Store, int, Plan], Any]


class Downloads:
    """Runs download jobs in the background, one at a time, for a long-lived process.

    One job at a time keeps a single BitTorrent session (one listening port) and one set of
    speed limits. Jobs wait in order. Pausing the running job cancels it the way Ctrl+C does,
    so its progress is saved and ``resume`` (or ``cinemind resume``) continues it.
    """

    def __init__(self, cfg: Config, store: Store, runner_factory: RunnerFactory = DownloadRunner):
        self.cfg = cfg
        self.store = store
        self._runner_factory = runner_factory
        self._queue: deque[int] = deque()
        self._worker: asyncio.Task[None] | None = None
        self._current: tuple[int, Any, asyncio.Task[str]] | None = None

    # -- control ---------------------------------------------------------------------------

    def start(self, plan: Plan) -> int:
        """Record a job for the plan and queue it. Returns the job id."""
        job_id = self.store.create_job(plan)
        self.enqueue(job_id)
        return job_id

    def enqueue(self, job_id: int) -> None:
        if job_id in self._queue or self.running_job == job_id:
            return
        self.store.set_job_status(job_id, "queued")
        self._queue.append(job_id)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(self._work())

    def pause(self, job_id: int) -> bool:
        """Pause a running or queued job. False when it is neither."""
        if self._current is not None and self._current[0] == job_id:
            self._current[2].cancel()
            return True
        if job_id in self._queue:
            self._queue.remove(job_id)
            self.store.pause_open(job_id)
            self.store.set_job_status(job_id, "paused")
            return True
        return False

    def redeliver(self, job_id: int, key: str) -> str:
        """Run a file's delivery again (subtitle, ingest): in the job's runner when it is
        running, else by queueing the job. Returns the key as stored."""
        if not Delivery.wanted(self.cfg):
            raise ValueError("Delivery is off: set auto_subtitles or auto_ingest to true.")
        key = reopen(self.store, job_id, key)
        if self._current is not None and self._current[0] == job_id:
            delivery: Delivery | None = getattr(self._current[1], "delivery", None)
            if delivery is not None:
                delivery.retry(key)
            return key
        self.enqueue(job_id)
        return key

    def resume(self, job_id: int) -> None:
        """Queue a paused or interrupted job again. KeyError if there is no such job."""
        row = self.store.job(job_id)
        if row.status not in ("paused", "running", "queued"):
            raise ValueError(f"Job #{job_id} is {row.status}; only paused jobs can be resumed")
        self.enqueue(job_id)

    async def aclose(self) -> None:
        """Pause the running job (saving its progress) and stop the worker."""
        # Empty the queue first: otherwise the worker starts the next job the moment the
        # running one is cancelled, and that one is left "running" with nothing running it.
        for job_id in list(self._queue):
            self.pause(job_id)
        if self._current is not None:
            task = self._current[2]
            task.cancel()
            await asyncio.wait({task})
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.wait({self._worker})

    # -- state -----------------------------------------------------------------------------

    @property
    def running_job(self) -> int | None:
        return self._current[0] if self._current is not None else None

    @property
    def queued(self) -> list[int]:
        return list(self._queue)

    def progress(self, job_id: int) -> list[JobView] | None:
        """Live progress of the running job, or None for any other job."""
        if self._current is None or self._current[0] != job_id:
            return None
        views: list[JobView] = getattr(self._current[1], "views", [])
        return views

    # -- the worker ------------------------------------------------------------------------

    async def _work(self) -> None:
        while self._queue:
            await self._run_one(self._queue.popleft())

    async def _run_one(self, job_id: int) -> None:
        torrents = self.store.open_torrents(job_id)
        if not torrents and not self.store.open_deliveries(job_id):
            self.store.finish_job(job_id)
            return
        self.store.set_job_status(job_id, "running")
        runner = self._runner_factory(self.cfg, self.store, job_id, self.store.load_plan(job_id))
        task = asyncio.get_running_loop().create_task(runner.run(torrents, display=False))
        self._current = (job_id, runner, task)
        try:
            await asyncio.wait({task})
        finally:
            self._current = None
        if task.cancelled():
            return  # paused: the runner saved its progress
        exc = task.exception()
        if exc is not None:
            log.error("Job #%s failed: %s", job_id, exc, exc_info=exc)
            self.store.fail_job(job_id, str(exc) or type(exc).__name__)
