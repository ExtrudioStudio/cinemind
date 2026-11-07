"""Hand finished files to an external command, e.g. a script that moves them to a NAS.

The command is started detached, in its own (hidden) console and process group, so it keeps
running after stremio-dl moves on or quits and never receives our Ctrl+C. Its output goes to
``<log_dir>/post-download/<time>-<file>.log``.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .config import Config, log_dir
from .models import FileTarget, Title

log = logging.getLogger(__name__)

_DETACH = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
    subprocess, "CREATE_NO_WINDOW", 0
)


def command_fields(cfg: Config, title: Title, target: FileTarget) -> dict[str, str]:
    ref = target.refs[0] if target.refs else None
    fields = {
        "path": str(target.final_path),
        "id": title.stream_id(ref),
        "imdb": title.id if title.id.startswith("tt") else "",
        "type": title.type,
        "name": title.name,
        "year": str(title.year or ""),
        "season": str(ref.season) if ref else "",
        "episode": str(ref.episode) if ref else "",
        "root": cfg.movie_root if title.type == "movie" else cfg.tv_root,
    }
    # Values usually sit inside "..." in the command line; a stray quote would split them.
    return {key: value.replace('"', "'") for key, value in fields.items()}


def log_folder() -> Path:
    return log_dir() / "post-download"


def build_command(cfg: Config, title: Title, target: FileTarget) -> str:
    return cfg.post_download_command.format_map(command_fields(cfg, title, target))


def hand_off(cfg: Config, title: Title, target: FileTarget) -> subprocess.Popen | None:
    """Start the post-download command for one finished file (None when off or impossible)."""
    if not cfg.post_download_command.strip() or not target.final_path.is_file():
        return None
    try:
        command = build_command(cfg, title, target)
    except (KeyError, IndexError, ValueError) as exc:
        log.error("post_download_command has an unknown placeholder or stray brace: %s", exc)
        return None
    folder = log_folder()
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{datetime.now():%Y%m%d-%H%M%S}-{target.final_path.stem[:80]}.log"
    args: str | list[str] = command if sys.platform == "win32" else shlex.split(command)
    try:
        with out.open("wb") as fh:
            proc = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                creationflags=_DETACH, close_fds=True,
            )  # fmt: skip
    except OSError as exc:
        log.error("Could not start the post-download command for %s: %s", target.final_path, exc)
        return None
    log.info("Post-download started for %s (pid %s, log %s)", target.final_path, proc.pid, out)
    return proc
