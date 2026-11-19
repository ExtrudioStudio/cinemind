"""Hand finished files to an external command, e.g. a script that moves them to a NAS.

The configured command is split into arguments first, the way the operating system would split
it (``CommandLineToArgvW`` on Windows, POSIX shell rules elsewhere), and only then are the
``{placeholders}`` filled in, one argument at a time. A title or path that contains quotes,
spaces or a trailing backslash therefore stays one argument and can't add new ones. The same
values are also passed as ``SDL_*`` environment variables.

The command runs through :mod:`stremio_dl.hookrun`, started detached in its own (hidden)
console and process group. It keeps running after stremio-dl moves on or quits, never receives
our Ctrl+C, and records its exit code in the ``handoffs`` table. Its output goes to
``<log_dir>/post-download/<time>-<file>.log``.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .config import Config, log_dir
from .models import FileTarget, Title, TorrentJob
from .store import Store, key_name

log = logging.getLogger(__name__)

_DETACH = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
    subprocess, "CREATE_NO_WINDOW", 0
)


def command_fields(
    cfg: Config, title: Title, target: FileTarget, batch: str = "", batch_size: int = 0
) -> dict[str, str]:
    ref = target.refs[0] if target.refs else None
    return {
        "path": str(target.final_path),
        "id": title.stream_id(ref),
        "imdb": title.id if title.id.startswith("tt") else "",
        "type": title.type,
        "name": title.name,
        "year": str(title.year or ""),
        "season": str(ref.season) if ref else "",
        "episode": str(ref.episode) if ref else "",
        "root": cfg.movie_root if title.type == "movie" else cfg.tv_root,
        # files handed off together (one download run), so the command can treat them as a batch
        "batch": batch,
        "batch_size": str(batch_size),
    }


def command_env(fields: dict[str, str]) -> dict[str, str]:
    """The fields as environment variables: ``SDL_PATH``, ``SDL_ID``, ``SDL_BATCH_SIZE``..."""
    return {f"SDL_{name.upper()}": value for name, value in fields.items()}


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _CommandLineToArgvW = ctypes.windll.shell32.CommandLineToArgvW
    _CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    _CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    _LocalFree = ctypes.windll.kernel32.LocalFree

    def split_command(command: str) -> list[str]:
        """Split a command line exactly as a Windows program receives it."""
        if not command.strip():
            return []  # CommandLineToArgvW("") would return our own executable's path
        argc = ctypes.c_int()
        argv = _CommandLineToArgvW(command, ctypes.byref(argc))
        if not argv:
            raise ValueError("post_download_command could not be split into arguments")
        try:
            return [argv[i] for i in range(argc.value)]
        finally:
            _LocalFree(argv)

else:

    def split_command(command: str) -> list[str]:
        """Split a command line with POSIX shell quoting rules."""
        return shlex.split(command)


def build_argv(
    cfg: Config, title: Title, target: FileTarget, batch: str = "", batch_size: int = 0
) -> list[str]:
    """The command's arguments, with placeholders filled in per argument.

    Raises ``ValueError`` for an unparsable command (unbalanced quotes), and ``KeyError`` /
    ``IndexError`` / ``ValueError`` for an unknown placeholder or a stray brace.
    """
    fields = command_fields(cfg, title, target, batch, batch_size)
    return [arg.format_map(fields) for arg in split_command(cfg.post_download_command)]


def log_folder() -> Path:
    return log_dir() / "post-download"


def new_batch(kind: str, job_id: int) -> str:
    """A batch id unique to this run, e.g. ``sdl-job-4-20261107-150212``."""
    return f"sdl-{kind}-{job_id}-{datetime.now():%Y%m%d-%H%M%S}"


def hand_off(
    cfg: Config,
    title: Title,
    target: FileTarget,
    batch: str = "",
    batch_size: int = 0,
    *,
    store: Store | None = None,
    job_id: int = 0,
) -> subprocess.Popen[bytes] | None:
    """Start the post-download command for one finished file (None when off or impossible).

    With a ``store``, the run is recorded in its ``handoffs`` table and the wrapper fills in the
    exit code when the command ends.
    """
    if not cfg.post_download_command.strip() or not target.final_path.is_file():
        return None
    try:
        argv = build_argv(cfg, title, target, batch, batch_size)
    except (KeyError, IndexError, ValueError) as exc:
        log.error("post_download_command can't be used (unknown placeholder, stray brace or "
                  "unbalanced quote): %s", exc)  # fmt: skip
        return None
    if not argv:
        return None
    folder = log_folder()
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{datetime.now():%Y%m%d-%H%M%S}-{target.final_path.stem[:80]}.log"

    wrapper = [sys.executable, "-m", "stremio_dl.hookrun"]
    handoff_id = None
    if store is not None:
        key = key_name(target.refs[0] if target.refs else None)
        handoff_id = store.add_handoff(job_id, key, str(target.final_path), batch, str(out))
        wrapper += ["--db", str(store.path), "--handoff", str(handoff_id)]
    env = {**os.environ, **command_env(command_fields(cfg, title, target, batch, batch_size))}
    try:
        with out.open("wb") as fh:
            proc = subprocess.Popen(
                [*wrapper, "--", *argv], stdin=subprocess.DEVNULL, stdout=fh,
                stderr=subprocess.STDOUT, env=env, creationflags=_DETACH, close_fds=True,
                start_new_session=sys.platform != "win32",
            )  # fmt: skip
    except OSError as exc:
        log.error("Could not start the post-download command for %s: %s", target.final_path, exc)
        if store is not None and handoff_id is not None:
            store.finish_handoff(handoff_id, None, f"could not start: {exc}")
        return None
    log.info("Post-download started for %s (pid %s, log %s)", target.final_path, proc.pid, out)
    return proc


def hand_off_job(
    cfg: Config, title: Title, torrents: list[TorrentJob], batch: str,
    store: Store | None = None, job_id: int = 0,
) -> list[Path]:  # fmt: skip
    """Hand off every file of finished torrents that is still in the library (``sdl handoff``)."""
    present = [t for job in torrents for t in job.targets if t.final_path.is_file()]
    return [
        t.final_path
        for t in present
        if hand_off(cfg, title, t, batch, len(present), store=store, job_id=job_id)
    ]
