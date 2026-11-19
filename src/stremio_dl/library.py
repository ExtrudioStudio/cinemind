"""Library paths: naming templates, Windows-safe file names and "already have it" checks."""

from __future__ import annotations

import re
import shutil
import unicodedata
from pathlib import Path

from .config import Config
from .models import EpisodeRef, Title
from .streams import VIDEO_EXTS

MAX_PATH = 240  # stay under Windows' 260-character MAX_PATH with room for ".part"
_INVALID = re.compile(r'[<>"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}  # fmt: skip


def sanitize(name: str, max_len: int = 150) -> str:
    """Make one path component safe on Windows (and everywhere else)."""
    name = unicodedata.normalize("NFC", name)
    name = name.replace(": ", " - ").replace(":", "-")
    name = _INVALID.sub("", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name[:max_len].rstrip(". ")
    if not name:
        return "_"
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    return name


def episode_code(refs: tuple[EpisodeRef, ...]) -> str:
    if not refs:
        return ""
    first, last = refs[0], refs[-1]
    if len(refs) == 1:
        return first.code
    if first.season == last.season:
        return f"{first.code}-E{last.episode:02d}"
    return f"{first.code}-{last.code}"


def _render(template: str, root: Path, fields: dict, ext: str) -> Path:
    parts = [p for p in template.replace("\\", "/").split("/") if p]
    rendered = [p.format(**fields) for p in parts]
    dirs = [sanitize(p) for p in rendered[:-1]]
    filename = rendered[-1]
    stem = filename[: -len(ext)] if ext and filename.endswith(ext) else filename
    stem = re.sub(r"[\s\-–]+$", "", stem)  # "Show - S01E01 - " when the title is empty
    stem = sanitize(stem, max_len=200)

    path = root.joinpath(*dirs, stem + ext)
    overflow = len(str(path)) - MAX_PATH
    if overflow > 0:
        stem = sanitize(stem[: max(20, len(stem) - overflow)])
        path = root.joinpath(*dirs, stem + ext)
    # sanitize() already removes separators, and turns "." and ".." into "_". This is the
    # backstop: whatever the template or the metadata says, files stay inside the library root.
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"naming template puts {path} outside the library root {root}")
    return path


def tv_path(cfg: Config, title: Title, refs: tuple[EpisodeRef, ...], ext: str) -> Path:
    names = []
    for ref in refs:
        ep = title.episode(ref)
        if ep and ep.title and ep.title not in names:
            names.append(ep.title)
    fields = {
        "show": title.name,
        "year": title.year or "",
        "season": refs[0].season,
        "episode": refs[0].episode,
        "code": episode_code(refs),
        "title": " & ".join(names),
        "ext": ext,
    }
    return _render(cfg.tv_template, Path(cfg.tv_root), fields, ext)


def movie_path(cfg: Config, title: Title, ext: str) -> Path:
    fields = {"title": title.name, "year": title.year or "", "ext": ext}
    template = cfg.movie_template if title.year else cfg.movie_template.replace(" ({year})", "")
    return _render(template, Path(cfg.movie_root), fields, ext)


def target_path(cfg: Config, title: Title, refs: tuple[EpisodeRef, ...], ext: str) -> Path:
    return movie_path(cfg, title, ext) if title.type == "movie" else tv_path(cfg, title, refs, ext)


def _match_video(entries: list[Path], refs: tuple[EpisodeRef, ...]) -> Path | None:
    patterns = [
        re.compile(rf"s0*{r.season}\s*e0*{r.episode}(?!\d)", re.I) for r in refs
    ]  # matches S01E05, s1e5, "S01 E05"
    for entry in entries:
        if not entry.is_file() or entry.suffix.lower() not in VIDEO_EXTS:
            continue
        if not refs:
            return entry  # a movie folder that already holds a video
        if all(p.search(entry.name) for p in patterns):
            return entry
    return None


def find_existing(path: Path, refs: tuple[EpisodeRef, ...]) -> Path | None:
    """A finished video already in the target folder for these episodes (or this movie)."""
    if path.exists():
        return path
    folder = path.parent
    if not folder.is_dir():
        return None
    return _match_video(list(folder.iterdir()), refs)


_ID_HINT = re.compile(r"\s*\{[a-z]+-[^}]*\}\s*$", re.I)  # Plex-style "{tvdb-81797}", "{imdb-tt...}"


class ExtraLibraries:
    """Finished videos in other library roots, e.g. a NAS that downloads are moved to.

    Their folders may be named differently from ours: "Show", "Show (1999)" or
    "Show (1999) {tvdb-81797}" all count, as does any folder tagged "{imdb-<this id>}".
    Folder listings are cached, so a plan touches each remote folder once.
    """

    def __init__(self, roots: list[str], title: Title):
        self.roots = [Path(r) for r in roots if r.strip()]
        self.title = title
        self._listings: dict[Path, list[Path]] = {}
        self._title_dirs: list[Path] | None = None

    def _list(self, folder: Path) -> list[Path]:
        if folder not in self._listings:
            try:
                self._listings[folder] = list(folder.iterdir())
            except OSError:  # missing folder, NAS offline, no permission
                self._listings[folder] = []
        return self._listings[folder]

    def title_dirs(self) -> list[Path]:
        if self._title_dirs is None:
            name = sanitize(self.title.name).casefold()
            year = self.title.year
            tag = f"{{imdb-{self.title.id}}}".casefold()
            found = []
            for root in self.roots:
                for entry in self._list(root):
                    folder = entry.name.casefold()
                    base = _ID_HINT.sub("", folder)
                    same_name = base == name or (
                        base == f"{name} ({year})"
                        if year
                        else re.fullmatch(rf"{re.escape(name)} \(\d{{4}}\)", base) is not None
                    )
                    if (same_name or tag in folder) and entry.is_dir():
                        found.append(entry)
            self._title_dirs = found
        return self._title_dirs

    def find(self, refs: tuple[EpisodeRef, ...]) -> Path | None:
        if not self.roots:
            return None
        for folder in self.title_dirs():
            if not refs:
                hit = _match_video(self._list(folder), refs)
            else:
                season = refs[0].season
                hit = None
                for name in (f"Season {season:02d}", f"Season {season}"):
                    hit = hit or _match_video(self._list(folder / name), refs)
            if hit:
                return hit
        return None


def free_space(path: Path) -> int:
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return shutil.disk_usage(probe).free
