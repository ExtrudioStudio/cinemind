"""User configuration and app directories.

Config lives in ``%APPDATA%\\cinemind\\config.toml``; state, cache and logs in
``%LOCALAPPDATA%\\cinemind``. Set ``CINEMIND_HOME`` to put everything under one folder
(used by the tests).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tomllib
from pathlib import Path
from typing import Any, Literal

import platformdirs
import tomli_w
from pydantic import BaseModel, ValidationError

from cinemind import __version__

APP_NAME = "cinemind"
PROJECT_URL = "https://github.com/ExtrudioStudio/cinemind"
CINEMETA = "https://v3-cinemeta.strem.io/manifest.json"


def _home_override() -> Path | None:
    value = os.environ.get("CINEMIND_HOME")
    return Path(value) if value else None


def config_dir() -> Path:
    if home := _home_override():
        return home / "config"
    return Path(platformdirs.user_config_dir(APP_NAME, appauthor=False, roaming=True))


def data_dir() -> Path:
    if home := _home_override():
        return home / "data"
    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))


def cache_dir() -> Path:
    if home := _home_override():
        return home / "cache"
    return Path(platformdirs.user_cache_dir(APP_NAME, appauthor=False))


def log_dir() -> Path:
    if home := _home_override():
        return home / "logs"
    return Path(platformdirs.user_log_dir(APP_NAME, appauthor=False))


def config_path() -> Path:
    return config_dir() / "config.toml"


LEGACY_APP_NAME = "stremio-dl"  # the project's name up to 0.1


def _legacy_pairs() -> list[tuple[Path, Path]]:
    """(old file, new file) for what is worth carrying over from a stremio-dl install."""
    old_config = Path(platformdirs.user_config_dir(LEGACY_APP_NAME, appauthor=False, roaming=True))
    old_data = Path(platformdirs.user_data_dir(LEGACY_APP_NAME, appauthor=False))
    return [
        (old_config / "config.toml", config_path()),
        (old_data / "state.db", data_dir() / "state.db"),
    ]


def migrate_legacy_files(pairs: list[tuple[Path, Path]] | None = None) -> list[Path]:
    """Copy settings and download history from a stremio-dl install, the first time only.

    A file is copied only when the old one exists and the new one doesn't, so this runs once.
    The old files stay where they are. ``state.db`` is copied with SQLite's backup API, which
    includes changes still in its WAL file. Returns the files that were created.
    """
    if pairs is None:
        if _home_override():
            return []  # a private home (tests, portable installs) has nothing to inherit
        pairs = _legacy_pairs()
    created: list[Path] = []
    for old, new in pairs:
        if not old.is_file() or new.exists():
            continue
        new.parent.mkdir(parents=True, exist_ok=True)
        if old.suffix == ".db":
            src, dst = sqlite3.connect(old), sqlite3.connect(new)
            try:
                src.backup(dst)
            finally:
                src.close()
                dst.close()
        else:
            shutil.copy2(old, new)
        created.append(new)
    return created


def _videos_dir() -> Path:
    return Path(platformdirs.user_videos_dir())


class Config(BaseModel):
    # Metadata: Stremio meta addons (Cinemeta finds titles and lists their episodes).
    meta_addons: list[str] = [CINEMETA]
    # Where downloads come from, in order (see `cinemind sources`). "internet-archive" offers
    # public-domain films; "stremio-addons" uses the addons in stream_addons below.
    stream_sources: list[str] = ["internet-archive", "stremio-addons"]
    # Stremio stream addons, none by default. Paste a *configured* manifest URL to keep its
    # options (e.g. a Torrentio URL with "sort=seeders|qualityfilter=cam" in the path). Only
    # download content you have the right to.
    stream_addons: list[str] = []

    # Library layout
    tv_root: str = str(_videos_dir() / "TV Shows")
    movie_root: str = str(_videos_dir() / "Movies")
    tv_template: str = "{show}/Season {season:02}/{show} - {code} - {title}{ext}"
    movie_template: str = "{title} ({year})/{title} ({year}){ext}"
    # Other libraries checked for episodes and movies you already have, e.g. the NAS that
    # finished downloads are moved to. Their folders may carry a year and an id hint:
    # "Show (1999) {tvdb-81797}/Season 01/...S01E05...mkv", "Movie (2010) {imdb-tt1375666}/".
    extra_tv_roots: list[str] = []
    extra_movie_roots: list[str] = []

    # Run once per finished file, after its torrent is complete and closed (empty = off).
    # Placeholders: {path} {id} (stream id, e.g. tt0388629:1:2) {imdb} {type} {name}
    # {year} {season} {episode} {root} (the library root the file is in).
    post_download_command: str = ""

    # Stream selection
    default_quality: str = "1080p"
    languages: list[str] = ["en"]  # audio languages to accept; ["any"] disables the filter
    on_missing: Literal["ask", "lower", "skip"] = "ask"
    exclude_dv_only: bool = False
    include_specials: bool = False
    prefer_packs: bool = False
    fallbacks_per_episode: int = 5

    # BitTorrent
    max_active_torrents: int = 3
    listen_interfaces: str = "0.0.0.0:6881,[::]:6881"
    outgoing_interfaces: str = ""  # e.g. the VPN adapter name, to keep traffic on the VPN
    seed_ratio: float = 0.0  # 0 = stop as soon as the wanted files are complete
    metadata_timeout: int = 120
    stall_timeout: int = 180

    # Direct (HTTP) downloads
    max_active_http: int = 2

    # HTTP
    http_concurrency: int = 4
    http_timeout: float = 20.0
    # Says what we are, in the "compatible" form crawlers use, so addon operators can tell us apart.
    user_agent: str = f"Mozilla/5.0 (compatible; cinemind/{__version__}; +{PROJECT_URL})"


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        cfg = Config()
        save_config(cfg, path)
        return cfg
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return Config.model_validate(data)


def save_config(cfg: Config, path: Path | None = None) -> Path:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(cfg.model_dump()), encoding="utf-8")
    return path


def _coerce(field: str, raw: str) -> Any:
    """Turn a command-line string into the type the field expects."""
    annotation = Config.model_fields[field].annotation
    if annotation == list[str]:
        return [part.strip() for part in raw.split(",") if part.strip()]
    if annotation is bool:
        if raw.lower() in {"1", "true", "yes", "on"}:
            return True
        if raw.lower() in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"{field} expects true/false, got {raw!r}")
    return raw


def set_value(cfg: Config, field: str, raw: str) -> Config:
    if field not in Config.model_fields:
        known = ", ".join(Config.model_fields)
        raise KeyError(f"Unknown setting {field!r}. Known settings: {known}")
    data = cfg.model_dump()
    data[field] = _coerce(field, raw)
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise ValueError(exc.errors()[0]["msg"]) from exc
