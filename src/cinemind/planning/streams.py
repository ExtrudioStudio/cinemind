"""Collect candidates from every stream source, then filter, de-duplicate and rank them.

Sources (:mod:`cinemind.sources`) say what is on offer. This module decides what is usable
(:func:`rejection_reason`), merges offers of the same file, and orders them for one requested
resolution (:func:`rank`, :func:`choose`).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from functools import lru_cache
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from cinemind.core.aio import reraise_cancelled
from cinemind.core.config import Config
from cinemind.core.models import Candidate, EpisodeRef, Key, Title
from cinemind.planning import quality as q

if TYPE_CHECKING:
    from cinemind.sources.base import StreamSource

log = logging.getLogger(__name__)

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".webm", ".mov", ".wmv", ".mpg", ".mpeg"}
BLOCKED_EXTS = {
    ".exe", ".lnk", ".scr", ".bat", ".cmd", ".com", ".msi", ".pif", ".vbs", ".js", ".ps1",
    ".jar", ".apk", ".zip", ".rar", ".7z", ".iso", ".url", ".hta",
}  # fmt: skip

_LABEL_RES = [
    (re.compile(r"\b(4k|2160p|uhd)\b", re.I), "2160p"),
    (re.compile(r"\b1440p\b", re.I), "1440p"),
    (re.compile(r"\b1080[pi]\b", re.I), "1080p"),
    (re.compile(r"\b720p\b", re.I), "720p"),
    (re.compile(r"\b(480p|576p|sd)\b", re.I), "480p"),
    (re.compile(r"\b360p\b", re.I), "360p"),
]
_CAM = re.compile(
    r"(?:^|[\s.\-_\[(])(cam|camrip|hdcam|hd-?ts|telesync|ts-?rip|tc|telecine|scr|screener|"
    r"dvdscr|bdscr|hdtc)(?:$|[\s.\-_\])])",
    re.I,
)
# Lowest frame height for each label; a 1440x1080 anamorphic file is still 1080p.
_HEIGHTS = ((2000, "2160p"), (1300, "1440p"), (1000, "1080p"), (700, "720p"), (460, "480p"))


@lru_cache(maxsize=4096)
def _guess_resolution(name: str) -> str | None:
    if not name:
        return None
    try:
        from guessit import guessit  # imported lazily: it's slow to import

        size = guessit(name).get("screen_size")
    except Exception:  # guessit can raise on odd input; treat it as "no information"
        return None
    if not isinstance(size, str):
        return None
    for pattern, res in _LABEL_RES:
        if pattern.search(size):
            return res
    return None


def detect_resolution(label: str, filename: str, release: str) -> str:
    for pattern, res in _LABEL_RES:
        if pattern.search(label):
            return res
    return _guess_resolution(filename) or _guess_resolution(release) or q.UNKNOWN


def resolution_from_height(height: int) -> str:
    """The resolution label for a frame height in pixels (0 or less means unknown)."""
    if height <= 0:
        return q.UNKNOWN
    return next((label for floor, label in _HEIGHTS if height >= floor), "360p")


def rejection_reason(c: Candidate, cfg: Config) -> str | None:
    """Why a candidate is unusable regardless of resolution, or None if it's fine."""
    ext = PurePosixPath(c.filename.lower()).suffix
    if ext in BLOCKED_EXTS:
        return f"not a video file ({ext})"
    if c.filename and c.filename != c.release and ext not in VIDEO_EXTS:
        return f"not a video file ({ext or 'no extension'})"
    stem = c.release + " " + PurePosixPath(c.filename).stem
    if _CAM.search(stem) or _CAM.search(c.label):
        return "cam/screener release"
    wanted = {lang.lower() for lang in cfg.languages}
    if c.languages and "any" not in wanted and "multi" not in c.languages:
        if not wanted.intersection(c.languages):
            return "audio language " + "/".join(c.languages)
    if cfg.exclude_dv_only and "DV" in c.hdr and "HDR" not in c.hdr:
        return "Dolby Vision only"
    return None


def usable(cands: list[Candidate], cfg: Config) -> list[Candidate]:
    return [c for c in cands if rejection_reason(c, cfg) is None]


def _order(c: Candidate) -> tuple[int, int, int]:
    # A direct download needs no peers, so it goes before torrents at the same resolution.
    # Within each kind: most seeders (or most popular archive item) first, bigger file next.
    return (0 if c.is_direct else 1, -c.seeders, -c.size)


def rank(cands: list[Candidate], requested: str) -> list[Candidate]:
    """Filter to the requested resolution and order the rest, best first."""
    pool = [c for c in cands if q.matches(requested, c.resolution)]
    if requested == q.BEST:
        pool = [c for c in pool if c.seeders > 0 or c.is_direct] or pool
        return sorted(pool, key=lambda c: (-q.rank(c.resolution), *_order(c)))
    return sorted(pool, key=_order)


def merge(cands: list[Candidate]) -> list[Candidate]:
    """De-duplicate offers of the same file from several sources, keeping the best-seeded one."""
    best: dict[tuple[str, int | None], Candidate] = {}
    for c in cands:
        key = (c.info_hash, c.file_idx)
        if key not in best or c.seeders > best[key].seeders:
            best[key] = c
    return list(best.values())


class StreamResolver:
    """Asks every configured source that supports a title, and keeps the usable offers."""

    def __init__(self, sources: list[StreamSource], cfg: Config):
        self.sources = sources
        self.cfg = cfg

    async def candidates(self, title: Title, ref: EpisodeRef | None) -> list[Candidate]:
        """All usable candidates for one episode (or the movie), any resolution."""
        sources = [s for s in self.sources if s.supports(title)]
        results = await asyncio.gather(
            *(s.candidates(title, ref) for s in sources), return_exceptions=True
        )
        found: list[Candidate] = []
        for source, raw in zip(sources, results, strict=True):
            got = reraise_cancelled(raw)
            if isinstance(got, Exception):
                log.warning("Source %s failed for %s: %s", source.name, title.stream_id(ref), got)
                continue
            found.extend(got)
        return usable(merge(found), self.cfg)

    async def all_candidates(
        self, title: Title, keys: list[Key], on_progress: Any = None
    ) -> dict[Key, list[Candidate]]:
        out: dict[Key, list[Candidate]] = {}

        async def one(key: Key) -> None:
            out[key] = await self.candidates(title, key)
            if on_progress:
                on_progress(key)

        await asyncio.gather(*(one(k) for k in keys))
        return {k: out[k] for k in keys}

    async def resolution_matrix(self, title: Title, max_seasons: int = 40) -> dict[int, Counter]:
        """Release counts per resolution, sampled from episode 1 of each season."""
        firsts = [eps[0].ref for _, eps in sorted(title.seasons().items())][:max_seasons]
        cands = await self.all_candidates(title, list(firsts))
        return {ref.season: Counter(c.resolution for c in cands[ref]) for ref in firsts}


def choose(
    cands: list[Candidate], requested: str, on_missing: str, limit: int
) -> tuple[list[Candidate], str | None]:
    """Ranked choices for one episode and the resolution actually used.

    With ``on_missing="lower"``, falls back to the next lower resolution that has streams.
    Returns ``([], None)`` when nothing fits.
    """
    ranked = rank(cands, requested)
    if ranked:
        return ranked[:limit], requested
    if on_missing == "lower":
        for res in q.lower_than(requested):
            ranked = rank(cands, res)
            if ranked:
                return ranked[:limit], res
    return [], None
