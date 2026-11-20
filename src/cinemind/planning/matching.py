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
4. **Checks on the judge's pick** (:func:`contradiction`): a file whose name says it's another
   episode is thrown out, and the name rules' answer stands. The name names another episode
   when it has a different episode code (``S01E01`` for a special), a season-relative number
   with no sign of that season (``- 24`` for S02E24, absolute 48), or another episode's full
   title and not this one's ("The Well" for "The Well's Edge").

Without a judge the result is what the name rules give, so turning the judge off changes
nothing else. Everything here is pure (no libtorrent), so the eval runs the real code.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
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
    other_titles: tuple[str, ...] = ()  # the show's other episodes (the checks, not the judge)

    @classmethod
    def from_title(cls, title: Title, refs: Sequence[EpisodeRef], release: str = "") -> Wanted:
        regular = sorted((e for e in title.episodes if e.season > 0),
                         key=lambda e: (e.season, e.episode))  # fmt: skip
        numbers = {e.ref: i for i, e in enumerate(regular, start=1)}
        episodes = [title.episode(r) for r in refs]
        others = tuple(e.title for e in title.episodes if e.ref not in set(refs) and e.title)
        return cls(title.name, title.year, tuple(refs),
                   tuple(e.title if e else "" for e in episodes),
                   tuple(numbers.get(r) for r in refs), release, others)  # fmt: skip


@dataclass(frozen=True)
class Located:
    index: int | None  # the chosen file, or None
    how: How
    candidates: tuple[int, ...] = field(default=())  # the files an ambiguous match was between
    reason: str = ""  # the judge's reason, when it decided
    rejected: str = ""  # why the judge's pick was thrown out, when it was


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


# -- checks on the judge's pick (rule 4) ----------------------------------------------------------

# "S01E05", "s1.e5", and more episodes after it: "S01E01E02", "S02E09-E10", "S02E09-10"
_CODE = re.compile(
    r"(?<![a-z0-9])s(\d{1,2})[\s._-]*e(\d{1,3})"
    r"((?:[\s._-]*[-&+]?[\s._-]*e\d{1,3}(?!\d)|-\d{1,3}(?![\dp]))*)(?!\d)",
    re.IGNORECASE,
)
_NXNN = re.compile(r"(?<![a-z0-9])(\d{1,2})x(\d{2,3})((?:-\d{2,3})?)(?![\dp])", re.IGNORECASE)
_BRACKETS = re.compile(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}")  # groups, checksums, years
_TECH = re.compile(r"\d{3,4}[pi]|[hx]\.?26[45]|\d+[\s.-]?bits?|[257]\.[01](?!\d)|(?<=\d)v\d+",
                   re.IGNORECASE)  # fmt: skip
_GENERIC_TITLE = re.compile(r"(?:(?:episode|chapter|part) )?\d+(?: \d+)*")  # "Episode 5"
_COMMON = frozenset({"the", "and", "part", "with", "from", "into", "that", "this", "what"})


def _codes(name: str) -> set[tuple[int, int]]:
    """The (season, episode) pairs a file name states with an episode code."""
    out: set[tuple[int, int]] = set()
    for m in [*_CODE.finditer(name), *_NXNN.finditer(name)]:
        season, first = int(m[1]), int(m[2])
        last = max([first, *map(int, re.findall(r"\d+", m[3]))])
        out |= {(season, e) for e in range(first, min(last, first + 30) + 1)}
    return out


def _numbers(name: str) -> set[int]:
    """The bare numbers in a file name, leaving out episode codes, brackets and tags."""
    text = _TECH.sub(" ", _BRACKETS.sub(" ", _NXNN.sub(" ", _CODE.sub(" ", name))))
    return {int(n) for n in re.findall(r"\d+", text) if len(n) <= 3}


def _says_season(text: str, season: int) -> bool:
    """``S2``, ``Season 2``, ``Series 2`` or ``2nd Season`` somewhere in the text."""
    nth = {1: "1st", 2: "2nd", 3: "3rd"}.get(season, f"{season}th")
    return bool(re.search(rf"(?<![a-z0-9])(?:s0*{season}|(?:season|series)[\s._-]*0*{season}"
                          rf"|{nth}[\s._-]*season)(?!\d)", text, re.IGNORECASE))  # fmt: skip


def _plain(text: str) -> str:
    """Lowercase words, one space apart, apostrophes dropped ("The Well's" -> "the wells")."""
    return " ".join(re.findall(r"[a-z0-9]+", re.sub(r"['’]", "", text.lower())))


def _has(text: str, phrase: str) -> bool:
    return bool(phrase) and f" {phrase} " in f" {text} "


def _title_hint(name: str, wanted: Wanted) -> bool:
    """A telling word of a wanted episode's title is in the name ("Reckoning" in
    "S03E10 - Reckoning Part 2" for "Reckoning (2)"): the name may point at it after all."""
    show, words = set(_plain(wanted.show).split()), set(_plain(name).split())
    return any(w in words for t in wanted.titles for w in _plain(t).split()
               if len(w) >= 4 and w not in show and w not in _COMMON)  # fmt: skip


def contradiction(wanted: Wanted, file: PackFile) -> str:
    """Why the file's name says it's another episode, or ``""`` when nothing does.

    Each check passes when the name also points at the wanted episode (a word of its title, its
    number), so it only throws out names that point elsewhere: a wrong file downloads the wrong
    episode, while no file only costs a re-plan.
    """
    name = PurePosixPath(file.name).stem
    hint = _title_hint(name, wanted)
    codes = _codes(name)
    missing = [r for r in wanted.refs if (r.season, r.episode) not in codes]
    if codes and missing and not hint:
        named = ", ".join(f"S{s:02d}E{e:02d}" for s, e in sorted(codes))
        return f"it's named {named}, not {missing[0].code}"
    numbers = _numbers(name)
    where = f"{wanted.release}/{file.path}"
    absolutes = wanted.absolute or (None,) * len(wanted.refs)
    for ref, absolute in zip(wanted.refs, absolutes, strict=False):
        if (not codes and not hint and absolute and absolute != ref.episode
                and ref.episode in numbers and absolute not in numbers
                and not _says_season(where, ref.season)):  # fmt: skip
            return (
                f"it's numbered {ref.episode} with no sign of season {ref.season}, and "
                f"{ref.code} is episode {absolute} counted across seasons"
            )
    wanted_numbers = {r.episode for r in wanted.refs} | {a for a in absolutes if a}
    if codes or numbers & wanted_numbers:
        return ""  # its number is the wanted episode's
    text, show = f" {_plain(name)} ", _plain(wanted.show)
    if show:
        text = text.replace(f" {show} ", " ")
    titles = [_plain(t) for t in wanted.titles if t]
    if not titles or any(_has(text.strip(), t) for t in titles):
        return ""
    for other in wanted.other_titles:
        plain = _plain(other)
        if (not _GENERIC_TITLE.fullmatch(plain) and re.search(r"[a-z]{3}", plain)
                and _has(text.strip(), plain)):  # fmt: skip
            return f'it\'s named after another episode, "{other}"'
    return ""


Judge = Callable[[Wanted, list[PackFile]], Awaitable[Located | None]]


async def resolve(
    wanted: Wanted,
    files: Sequence[PackFile],
    judge: Judge | None = None,
    want_name: str = "",
    want_index: int | None = None,
    skip_decoys: bool = True,
    check: bool = True,
) -> Located:
    """:func:`locate`, then the judge for what the names couldn't settle (rule 3), then the
    checks on its pick (rule 4; ``check=False`` is how the eval's v2 ran).

    The judge returns None when it can't help (no model reachable): the name rules' answer
    stands, so an ambiguous match still takes the biggest file, as it did before the judge. A
    pick the checks throw out counts the same, with ``rejected`` saying why.
    """
    found = locate(wanted.refs, files, want_name, want_index, skip_decoys)
    if judge is None or not wanted.refs or found.how not in ("none", "ambiguous"):
        return found
    pool = [f for f in files if f.index in found.candidates] if found.how == "ambiguous" else files
    decided = await judge(wanted, list(pool))
    if decided is None:
        return found
    if check and decided.index is not None:
        picked = next((f for f in pool if f.index == decided.index), None)
        why = contradiction(wanted, picked) if picked else ""
        if why:
            return replace(found, reason=decided.reason, rejected=f"file {decided.index}: {why}")
    return decided
