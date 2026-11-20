"""After a download: a subtitle, then the ingest, for each finished file, a few files at a time.

Each file moves through stages that are recorded in the ``deliveries`` table:

    pending → subtitling → ingesting → delivered | needs_review | quarantined | failed

``dismissed`` marks one a person has looked at and closed. The subtitle comes first, while the
file is still on the local disk: the audio check reads it quickly there, and the ingest then
moves the subtitle along with the video.

Every stage can run again. A subtitle that is already next to the video is kept, a file that
was already filed isn't filed twice, and a file that is no longer where the download left it is
reported, not guessed at. So when cinemind stops half-way (Ctrl+C, a crash, a reboot), the
job is left ``paused`` and ``cinemind resume`` finishes its deliveries.

``needs_review`` replaces MediaPipeline v1's drag-and-drop window: a background run can't wait
for a person. The file is still filed, without the subtitle, and ``cinemind review`` (or the
MCP server's ``list_reviews`` and ``resolve_review``) shows what is missing and why.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from cinemind.core.config import Config
from cinemind.core.models import EpisodeRef, FileTarget, Title
from cinemind.core.store import OPEN_DELIVERY, DeliveryRow, Store, key_name
from cinemind.ingest.runner import Ingester, IngestResult, Media
from cinemind.subtitles.finder import FindResult

log = logging.getLogger(__name__)

FindFn = Callable[[Path, str, int | None, int | None], Awaitable[FindResult]]
DeliveredFn = Callable[[str, Path], None]  # key, where the file is now
UpdateFn = Callable[[DeliveryRow], None]

_CODE = re.compile(r"S(\d+)E(\d+)", re.IGNORECASE)


def ref_codes(refs: tuple[EpisodeRef, ...]) -> str:
    return ",".join(r.code for r in refs)


def parse_codes(codes: str) -> tuple[EpisodeRef, ...]:
    return tuple(EpisodeRef(int(s), int(e)) for s, e in _CODE.findall(codes))


def media_for(title: Title, refs: tuple[EpisodeRef, ...]) -> Media:
    """What the ingest needs to name the file: the movie, or its episode(s)."""
    imdb = title.id if title.id.startswith("tt") else None
    if title.type == "movie" or not refs:
        return Media("movie", title.name, year=title.year, imdb=imdb)
    first, last = refs[0], refs[-1]
    run = len(refs) > 1 and last.season == first.season and last.episode > first.episode
    episode = title.episode(first) if len(refs) == 1 else None
    return Media("episode", title.name, year=title.year, imdb=imdb, season=first.season,
                 episode=first.episode, end_episode=last.episode if run else None,
                 episode_title=episode.title if episode and episode.title else None)  # fmt: skip


def reopen(store: Store, job_id: int, key: str) -> str:
    """Put a finished delivery back to ``pending``, to run again once a person has fixed what
    was wrong (a share that was down, a missing subtitle provider). Returns the key as stored.

    ValueError for a quarantined file: it can't play directly, so it needs another release.
    """
    row = store.delivery(job_id, key)
    if row.stage == "quarantined":
        raise ValueError(
            f"{row.key} of job #{job_id} was quarantined: it can't play directly on the TV, so "
            "download another release, then dismiss this one."
        )
    if row.stage not in OPEN_DELIVERY:
        store.set_delivery(job_id, row.key, "pending")
    return row.key


def dismiss(store: Store, job_id: int, key: str) -> str:
    """Close a delivery a person has looked at; it is no longer listed for review. Its reason
    is kept for the record. Returns the key as stored."""
    row = store.delivery(job_id, key)
    if row.stage in OPEN_DELIVERY:
        raise ValueError(f"{row.key} of job #{job_id} is still being delivered ({row.stage}).")
    store.set_delivery(job_id, row.key, "dismissed", review=row.review, error=row.error)
    return row.key


class Delivery:
    """Runs the after-download stages for one job's files, at most ``max_delivery_jobs`` at once.

    ``find_subtitle`` and ``ingester`` can be swapped out (tests); by default they are
    :func:`cinemind.subtitles.finder.find_for_video` and an :class:`Ingester` from the config.
    """

    def __init__(
        self,
        cfg: Config,
        store: Store,
        job_id: int,
        title: Title,
        *,
        find_subtitle: FindFn | None = None,
        ingester: Ingester | None = None,
        on_delivered: DeliveredFn | None = None,
        on_update: UpdateFn | None = None,
    ):
        self.cfg = cfg
        self.store = store
        self.job_id = job_id
        self.title = title
        self._find = find_subtitle
        self._ingester = ingester
        self._on_delivered = on_delivered
        self._on_update = on_update
        self._slots = asyncio.Semaphore(max(1, cfg.max_delivery_jobs))
        self._tasks: dict[str, asyncio.Task[None]] = {}

    @staticmethod
    def wanted(cfg: Config) -> bool:
        return cfg.auto_subtitles or cfg.auto_ingest

    # -- scheduling ------------------------------------------------------------------------

    def submit(self, target: FileTarget) -> str:
        """Start delivering a freshly downloaded file. Returns its key."""
        key = key_name(target.refs[0] if target.refs else None)
        self.store.start_delivery(self.job_id, key, str(target.final_path), ref_codes(target.refs))
        self._spawn(key)
        return key

    def resume(self) -> int:
        """Start again the deliveries an earlier run didn't finish. Returns how many."""
        rows = self.store.open_deliveries(self.job_id)
        for row in rows:
            self._spawn(row.key)
        return len(rows)

    def retry(self, key: str) -> str:
        """Run a finished delivery again (see :func:`reopen`). Returns the key as stored."""
        key = reopen(self.store, self.job_id, key)
        self._spawn(key)
        return key

    @property
    def busy(self) -> int:
        return sum(not task.done() for task in self._tasks.values())

    async def wait(self) -> None:
        """Until every delivery started so far has finished."""
        while pending := [t for t in self._tasks.values() if not t.done()]:
            await asyncio.wait(pending)

    async def aclose(self) -> None:
        """Stop the running deliveries. They stay in their stage, for the next run."""
        pending = [t for t in self._tasks.values() if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending)

    def _spawn(self, key: str) -> None:
        task = self._tasks.get(key)
        if task is not None and not task.done():
            return
        self._tasks[key] = asyncio.get_running_loop().create_task(self._run(key))

    async def _run(self, key: str) -> None:
        async with self._slots:
            try:
                await self._deliver(key)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # one bad file never stops the others
                log.exception("Delivery of %s in job #%s failed", key, self.job_id)
                self.store.set_delivery(self.job_id, key, "failed",
                                        error=str(exc) or type(exc).__name__)  # fmt: skip
        row = self.store.delivery(self.job_id, key)
        if self._on_update is not None:
            try:
                self._on_update(row)
            except Exception:
                log.exception("Reporting the delivery of %s failed", key)

    # -- the stages ------------------------------------------------------------------------

    async def _deliver(self, key: str) -> None:
        row = self.store.delivery(self.job_id, key)
        video = Path(row.path)
        if not video.is_file():
            self.store.set_delivery(
                self.job_id, key, "failed", attempt=True,
                error=f"the file is no longer at {video}. If it was filed already, close this "
                      f"with `cinemind review dismiss {self.job_id} {key}`.",
            )  # fmt: skip
            return
        refs = parse_codes(row.refs)
        detail: dict[str, Any] = json.loads(row.detail or "{}")
        notes: list[str] = []
        subtitle = row.subtitle if row.subtitle and Path(row.subtitle).is_file() else None
        self.store.set_delivery(self.job_id, key, row.stage, attempt=True)  # one per run

        if self.cfg.auto_subtitles and subtitle is None:
            self.store.set_delivery(self.job_id, key, "subtitling")
            subtitle, note, detail["subtitles"] = await self._subtitle_stage(video, refs)
            if note:
                notes.append(note)
            self.store.set_delivery(self.job_id, key, "subtitling", subtitle=subtitle,
                                    detail=json.dumps(detail))  # fmt: skip

        if self.cfg.auto_ingest and row.target is None:
            self.store.set_delivery(self.job_id, key, "ingesting")
            result = await self._ingest().ingest(video, media_for(self.title, refs))
            detail["ingest"] = result.to_dict()
            if result.status in ("placed", "fixed"):
                assert result.target is not None
                video = Path(result.target)
                subtitle = _moved(subtitle, result, self.cfg.subtitle_language) or subtitle
                self.store.set_delivery(self.job_id, key, "ingesting", target=result.target,
                                        subtitle=subtitle, detail=json.dumps(detail))  # fmt: skip
            elif result.status == "quarantined":
                reasons = result.verdict.reasons if result.verdict else []
                why = "; ".join(reasons) or result.error or "rejected"
                self.store.set_delivery(
                    self.job_id, key, "quarantined", detail=json.dumps(detail),
                    review=f"won't play directly on the TV ({why}); moved to {result.target}. "
                           "Find another release.",
                )  # fmt: skip
                return
            else:
                self.store.set_delivery(self.job_id, key, "failed", detail=json.dumps(detail),
                                        error=f"ingest: {result.error}")  # fmt: skip
                return

        stage = "needs_review" if notes else "delivered"
        self.store.set_delivery(self.job_id, key, stage, review="; ".join(notes) or None,
                                detail=json.dumps(detail))  # fmt: skip
        if self._on_delivered is not None:
            try:
                self._on_delivered(key, video)
            except Exception:  # e.g. the post-download command; the file is delivered anyway
                log.exception("After delivering %s", video)

    async def _subtitle_stage(
        self, video: Path, refs: tuple[EpisodeRef, ...]
    ) -> tuple[str | None, str | None, list[str]]:
        """(subtitle path, what a person should know, the search log)."""
        language = self.cfg.subtitle_language.lower()
        existing = video.with_name(f"{video.stem}.{language}.srt")
        if existing.is_file():
            return str(existing), None, [f"kept the subtitle already there: {existing.name}"]
        if not self.title.id.startswith("tt"):
            return None, "no subtitle search: the title has no IMDb id", []
        if len(refs) > 1:
            return (
                None,
                (
                    f"no subtitle search for a file with {len(refs)} episodes; add one "
                    f"with `cinemind review subtitle {self.job_id} {refs[0].code} FILE`"
                ),
                [],
            )
        ref = refs[0] if refs else None
        try:
            found = await self._search(video, self.title.id, ref.season if ref else None,
                                       ref.episode if ref else None)  # fmt: skip
        except Exception as exc:  # no network, no FFmpeg...: the file is still worth filing
            log.warning("Subtitle search for %s failed: %s", video, exc)
            return None, f"subtitle search failed: {exc}", []
        if found.status == "found" and found.path is not None:
            return str(found.path), None, found.log
        return None, f"no {language} subtitle: {found.reason}", found.log

    async def _search(self, video: Path, imdb: str, season: int | None,
                      episode: int | None) -> FindResult:  # fmt: skip
        if self._find is not None:
            return await self._find(video, imdb, season, episode)
        from cinemind.subtitles.finder import find_for_video

        return await find_for_video(self.cfg, video, imdb, season, episode)

    def _ingest(self) -> Ingester:
        if self._ingester is None:
            self._ingester = Ingester.from_config(self.cfg)  # ValueError without ingest_root
        return self._ingester


def _moved(subtitle: str | None, result: IngestResult, language: str) -> str | None:
    """Where the ingest moved the subtitle to, along with the video."""
    if subtitle is None:
        return None
    suffix = f".{language.lower()}.srt"
    return next((p for p in result.sidecars if p.lower().endswith(suffix)), None)
