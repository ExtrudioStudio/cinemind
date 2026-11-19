"""Plex names: ``Movies/Title (Year) {imdb-tt…}/Title (Year).mkv`` and
``TV Shows/Show (Year) {tvdb-…}/Season 01/Show (Year) - S01E01 - Episode.mkv``.

A port of MediaPipeline v1's ``Get-PlexTargetPath``, kept identical so files land in the
folders v1 already made on the NAS: an existing ``Title (Year)`` or ``Title (Year) {id-…}``
folder is reused, so a new episode or a second version never creates a duplicate show.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Literal

Kind = Literal["movie", "episode"]


def plex_safe_name(text: str | None) -> str:
    """v1's ConvertTo-SafeName: ':' becomes ' -', characters Windows forbids are removed."""
    if not text:
        return ""
    s = text.replace(":", " -")
    s = re.sub(r'[<>"/\\|?*]', "", s)
    s = re.sub(r"[\x00-\x1f]", "", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s.rstrip(". ")


@dataclass(frozen=True)
class Target:
    kind: Kind
    library_dir: str  # "Movies" or "TV Shows"
    scan_dir: PurePath  # the folder Plex should look at, relative to the share root
    relative_path: PurePath  # the file, relative to the share root


def plex_target(
    kind: Kind,
    title: str,
    *,
    year: int | None = None,
    imdb: str | None = None,
    tmdb: str | None = None,
    tvdb: str | None = None,
    season: int | None = None,
    episode: int | None = None,
    end_episode: int | None = None,
    episode_title: str | None = None,
    extension: str = ".mkv",
    movies_dir: str = "Movies",
    tv_dir: str = "TV Shows",
    library_root: Path | None = None,
) -> Target:
    name = plex_safe_name(title)
    if not name:
        raise ValueError("The title is empty after removing characters a file name can't hold")
    base = f"{name} ({year})" if year and year > 1800 else name

    ids: dict[str, str] = {}
    if imdb:
        if not re.fullmatch(r"tt\d+", imdb, re.IGNORECASE):  # v1 matched without regard to case
            raise ValueError(f"An IMDb id looks like tt1234567 (got {imdb!r})")
        ids["imdb"] = imdb
    for label, value in (("tmdb", tmdb), ("tvdb", tvdb)):
        if value:
            if not re.fullmatch(r"\d+", value):
                raise ValueError(f"A {label.upper()} id is a number (got {value!r})")
            ids[label] = value
    order = ("imdb", "tmdb", "tvdb") if kind == "movie" else ("tvdb", "tmdb", "imdb")
    hint = next((f" {{{k}-{ids[k]}}}" for k in order if k in ids), "")

    library_dir = movies_dir if kind == "movie" else tv_dir
    folder = _existing_folder(library_root / library_dir, base, hint) if library_root else None
    folder = folder or f"{base}{hint}"

    if kind == "movie":
        scan = PurePath(library_dir, folder)
        return Target(kind, library_dir, scan, scan / f"{base}{extension}")

    if season is None or season < 0 or not episode or episode < 1:
        raise ValueError("An episode needs a season (0 for specials) and an episode number")
    code = f"S{season:02d}E{episode:02d}"
    if end_episode and end_episode > episode:
        code += f"-E{end_episode:02d}"
    file_name = f"{base} - {code}"
    if ep_name := plex_safe_name(episode_title):
        file_name += f" - {ep_name}"
    scan = PurePath(library_dir, folder)
    season_dir = scan / f"Season {season:02d}"
    return Target(kind, library_dir, scan, season_dir / f"{file_name}{extension}")


def _existing_folder(library: Path, base: str, hint: str) -> str | None:
    """A folder already made for this title: the exact name first, then any id variant."""
    try:
        names = [p.name for p in library.iterdir() if p.is_dir()]
    except OSError:
        return None
    exact = f"{base}{hint}"
    if exact in names:
        return exact
    return next((n for n in names if n == base or n.startswith(f"{base} {{")), None)
