"""Which file in a torrent is the episode we want: by name first, then (optionally) a model.

Sources such as Torrentio usually say which file of a season pack is which episode. When they
don't, or the torrent turns out different, cinemind finds the file itself, in this order:

1. **The file the source named**, by index or by file name, when it's there.
2. **A file whose name says the episode**: ``S01E05``, ``s1.e5``, ``1x05``. Decoys don't count
   (samples, trailers, previews, extras, featurettes, deleted scenes, behind-the-scenes,
   creditless openings and endings): a lone ``Sample/show.s01e05.sample.mkv`` is not episode
   5. If several match, the biggest wins, unless the two biggest are close in size: then
   it's *ambiguous* (``Part 1`` / ``Part 2`` both named ``S03E10``).
3. **The judge** (:mod:`cinemind.ai.judge`), only for a file nothing matched or an ambiguous
   match, and only when one is configured. It chooses among the candidate files, or says none.

Without a judge the result is what the name rules give, so turning the judge off changes
nothing else. Everything here is pure (no libtorrent), so the eval runs the real code.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal

from cinemind.core.models import EpisodeRef, Title

AMBIGUOUS_RATIO = 2.0  # the biggest match must be this much bigger than the next to win outright
_SEP = r"[\s._-]"
# After the episode code in a file name ("show.s01e05.sample.mkv", "S01E04.Deleted.Scenes").
DECOY_WORDS = re.compile(
    rf"(?<![a-z])(?:samples?|trailers?|teasers?|previews?|featurettes?|bloopers|gag{_SEP}reel"
    rf"|deleted{_SEP}scenes?|behind{_SEP}the{_SEP}scenes|making{_SEP}of|nc(?:op|ed)\d*"
    rf"|creditless)(?![a-z])",
    re.IGNORECASE,
)
# A whole folder inside the torrent ("Sample/", "Extras/", "Featurettes/").
DECOY_FOLDERS = re.compile(
    rf"(?:samples?|extras?|featurettes?|bonus(?:{_SEP}features?)?|trailers?|deleted{_SEP}scenes"
    rf"|behind{_SEP}the{_SEP}scenes|creditless|nc(?:op|ed)?)",
    re.IGNORECASE,
)

How = Literal["source", "name", "size", "ambiguous", "judge", "none"]


def basename(path: str) -> str:
    return PurePosixPath(path.replace("\\", "/")).name


def _code_pattern(ref: EpisodeRef) -> str:
    s, e = ref.season, ref.episode
    return rf"(?:s0*{s}[\s._-]*e0*{e}|(?<!\d){s}x0*{e})(?!\d)"


def is_decoy(path: str, refs: Sequence[EpisodeRef]) -> bool:
    """A sample, trailer, extra or the like that carries an episode's code.

    Only folders inside the torrent and the part of the file name after the episode code are
    read, never what comes before it: that's the show's name, and shows are called
    "Trailer Park Boys" or "Extras".
    """
    parts = path.replace("\\", "/").split("/")
    if any(DECOY_FOLDERS.fullmatch(folder.strip()) for folder in parts[1:-1]):
        return True  # parts[0] is the torrent's own folder, named after the show
    name = parts[-1]
    ends = [m.end() for r in refs if (m := re.search(_code_pattern(r), name, re.I))]
    return bool(ends) and bool(DECOY_WORDS.search(name[max(ends) :]))


@dataclass(frozen=True)
class PackFile:
    index: int  # the file's index in the torrent
    path: str  # its path inside the torrent, as published
    size: int

    @property
    def name(self) -> str:
        return basename(self.path)


@dataclass(frozen=True)
class Wanted:
    """What the judge needs to know about the episode(s) a file should hold."""

    show: str
    year: int | None
    refs: tuple[EpisodeRef, ...]
    titles: tuple[str, ...] = ()  # each episode's title, "" when unknown
    absolute: tuple[int | None, ...] = ()  # counting regular episodes from 1, across seasons
    release: str = ""  # the torrent's name

    @classmethod
    def from_title(cls, title: Title, refs: Sequence[EpisodeRef], release: str = "") -> Wanted:
        regular = sorted((e for e in title.episodes if e.season > 0),
                         key=lambda e: (e.season, e.episode))  # fmt: skip
        numbers = {e.ref: i for i, e in enumerate(regular, start=1)}
        episodes = [title.episode(r) for r in refs]
        return cls(title.name, title.year, tuple(refs),
                   tuple(e.title if e else "" for e in episodes),
                   tuple(numbers.get(r) for r in refs), release)  # fmt: skip


@dataclass(frozen=True)
class Located:
    index: int | None  # the chosen file, or None
    how: How
    candidates: tuple[int, ...] = field(default=())  # the files an ambiguous match was between
    reason: str = ""  # the judge's reason, when it decided


def matches_refs(name: str, refs: Sequence[EpisodeRef]) -> bool:
    """Does the file name say every one of these episodes (``S01E05``, ``s1.e5``, ``1x05``)?"""
    for ref in refs:
        if not re.search(_code_pattern(ref), name, re.I):
            return False
    return bool(refs)


def locate(
    refs: Sequence[EpisodeRef],
    files: Sequence[PackFile],
    want_name: str = "",
    want_index: int | None = None,
    skip_decoys: bool = True,
) -> Located:
    """The file for these episodes by the name rules (1 and 2 above). ``files`` are the
    torrent's video files that no other episode has taken. ``skip_decoys=False`` is the rule
    as it was before decoys were skipped (the eval's baseline)."""
    want = basename(want_name).lower() if want_name else ""
    by_index = {f.index: f for f in files}
    if want_index is not None and want_index in by_index:
        name = by_index[want_index].name
        if not want or name.lower() == want or matches_refs(name, refs):
            return Located(want_index, "source")
    if want:
        for f in files:
            if f.name.lower() == want:
                return Located(f.index, "source")
    if not refs:  # a movie: the biggest video
        biggest = max(files, key=lambda f: f.size, default=None)
        return Located(biggest.index if biggest else None, "size" if biggest else "none")
    hits = sorted((f for f in files if matches_refs(f.name, refs)
                   and not (skip_decoys and is_decoy(f.path, refs))),
                  key=lambda f: -f.size)  # fmt: skip
    if not hits:
        return Located(None, "none")
    if len(hits) > 1 and hits[0].size < AMBIGUOUS_RATIO * hits[1].size:
        return Located(hits[0].index, "ambiguous", tuple(f.index for f in hits))
    return Located(hits[0].index, "name")


Judge = Callable[[Wanted, list[PackFile]], Awaitable[Located | None]]


async def resolve(
    wanted: Wanted,
    files: Sequence[PackFile],
    judge: Judge | None = None,
    want_name: str = "",
    want_index: int | None = None,
    skip_decoys: bool = True,
) -> Located:
    """:func:`locate`, then the judge for what the names couldn't settle (rule 3).

    The judge returns None when it can't help (no model reachable): the name rules' answer
    stands, so an ambiguous match still takes the biggest file, as it did before the judge.
    """
    found = locate(wanted.refs, files, want_name, want_index, skip_decoys)
    if judge is None or not wanted.refs or found.how not in ("none", "ambiguous"):
        return found
    pool = [f for f in files if f.index in found.candidates] if found.how == "ambiguous" else files
    decided = await judge(wanted, list(pool))
    return found if decided is None else decided
