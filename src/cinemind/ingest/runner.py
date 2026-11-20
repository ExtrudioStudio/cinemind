"""Ingest one finished file: check it, fix it if it can be fixed, and file it for Plex.

A port of MediaPipeline v1's ``Invoke-MediaIngestItem``, keeping its outcomes and exit codes:

========  =====  ====================================================================
status    exit   what happened
========  =====  ====================================================================
placed    0      COMPLIANT: moved into the library as it is (a rename on the same share)
fixed     10     FIXABLE: remuxed with ffmpeg (video copied, never re-encoded), checked
                 again, then moved in; the source is removed unless ``keep_source``
quarantined 20   REJECT, or the fix failed: moved to the quarantine folder with a
                 ``.reason.txt`` that says why
error     1      anything else, e.g. the target exists (use ``overwrite``) or the share
                 is unreachable
dry-run   0      ``dry_run``: what would happen, with nothing changed
========  =====  ====================================================================

A fixed file must pass the rules again and keep its duration (within 2 s) before it replaces
anything, so a bad remux never reaches the library. Subtitle sidecars next to the source
(``<name>.srt``, ``<name>.en.forced.srt``) move along with the new name. Every run is logged
as one JSON line in ``<log dir>/ingest/ingest-YYYYMMDD.log``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from cinemind.core.config import Config, log_dir
from cinemind.core.secrets import get_secret
from cinemind.ingest.fix import fix_arguments
from cinemind.ingest.naming import Kind, plex_target
from cinemind.ingest.plex import Plex, PlexResult
from cinemind.ingest.probe import Completed, find_tool, probe, run_tool
from cinemind.ingest.profile import DeviceProfile, load_profile
from cinemind.ingest.rules import Verdict, evaluate

log = logging.getLogger(__name__)

PART_SUFFIXES = (".part", ".partial", ".tmp", ".crdownload", ".download")
DURATION_TOLERANCE = 2.0
Status = Literal["placed", "fixed", "quarantined", "error", "dry-run"]
EXIT_CODES: dict[str, int] = {"placed": 0, "fixed": 10, "quarantined": 20, "error": 1,
                              "dry-run": 0}  # fmt: skip

ProbeFn = Callable[[Path], Awaitable[dict[str, Any]]]
RunFn = Callable[..., Awaitable[Completed]]


@dataclass
class Media:
    """What the file is, for naming: a movie, or one (or a run of) episodes."""

    kind: Kind
    title: str
    year: int | None = None
    imdb: str | None = None
    tmdb: str | None = None
    tvdb: str | None = None
    season: int | None = None
    episode: int | None = None
    end_episode: int | None = None
    episode_title: str | None = None


@dataclass
class IngestResult:
    status: Status
    source: str
    target: str | None = None
    verdict: Verdict | None = None
    plex: PlexResult | None = None
    sidecars: list[str] = field(default_factory=list)
    seconds: float = 0.0
    error: str | None = None

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.status]

    def to_dict(self) -> dict[str, Any]:
        v = self.verdict
        return {
            "status": self.status, "exitCode": self.exit_code, "source": self.source,
            "target": self.target, "error": self.error, "seconds": self.seconds,
            "sidecars": self.sidecars, "plex": asdict(self.plex) if self.plex else None,
            "verdict": None if v is None else {
                "verdict": v.verdict, "reasons": v.reasons, "actions": v.actions,
                "warnings": v.warnings, "video": v.video, "bitrateMbps": v.bitrate_mbps},
        }  # fmt: skip


def logical_name(name: str) -> str:
    """``job42.mkv.part`` -> ``job42.mkv``."""
    lower = name.lower()
    for suffix in PART_SUFFIXES:
        if lower.endswith(suffix):
            return name[: -len(suffix)]
    return name


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    return path.with_name(f"{path.stem}.{datetime.now():%Y%m%d-%H%M%S}{path.suffix}")


def move(source: Path, dest: Path, overwrite: bool = False) -> None:
    """Move a file; on the same share this is a rename, so no data is copied."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if not overwrite:
            raise FileExistsError(f"target already exists: {dest} (use --overwrite to replace)")
        dest.unlink()
    shutil.move(str(source), str(dest))


async def wait_unlocked(path: Path, timeout: float, interval: float = 1.0) -> bool:
    """Wait until no other program holds the file open for writing (Windows locks it)."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with path.open("r+b"):
                return True
        except PermissionError:
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(interval)


class Ingester:
    def __init__(
        self,
        cfg: Config,
        profile: DeviceProfile | None = None,
        plex: Plex | None = None,
        probe_fn: ProbeFn | None = None,
        run_fn: RunFn | None = None,
    ):
        if not cfg.ingest_root:
            raise ValueError("Set ingest_root to the library share first, e.g. "
                             r"`cinemind config set ingest_root \\nas\Media`.")  # fmt: skip
        self.cfg = cfg
        self.root = Path(cfg.ingest_root)
        self.profile = profile or load_profile(cfg.ingest_profile)
        self.plex = plex
        self._probe_fn = probe_fn
        self._run_fn = run_fn

    @classmethod
    def from_config(cls, cfg: Config) -> Ingester:
        plex = Plex(cfg.plex_url, get_secret("plex")) if cfg.plex_url else None
        return cls(cfg, plex=plex)

    async def _probe(self, path: Path) -> dict[str, Any]:
        if self._probe_fn is not None:
            return await self._probe_fn(path)
        return await probe(path, find_tool("ffprobe", self.cfg.ffprobe_path))

    async def _ffmpeg(self, *args: str) -> Completed:
        if self._run_fn is not None:
            return await self._run_fn(*args)
        return await run_tool(find_tool("ffmpeg", self.cfg.ffmpeg_path), *args)

    # -- the pipeline --------------------------------------------------------------------------

    async def ingest(
        self,
        path: Path | str,
        media: Media,
        *,
        dry_run: bool = False,
        overwrite: bool = False,
        keep_source: bool = False,
    ) -> IngestResult:
        started = time.monotonic()
        source = Path(path)
        if not source.is_absolute():
            source = self.root / self.cfg.ingest_incoming_dir / source
        result = IngestResult("error", str(source))
        try:
            await self._ingest(source, media, result, dry_run, overwrite, keep_source)
        except Exception as exc:  # reported in the result, as v1 did, never raised
            result.status, result.error = "error", str(exc) or type(exc).__name__
            log.warning("Ingest of %s failed: %s", source, result.error)
        result.seconds = round(time.monotonic() - started, 2)
        self._log(result)
        return result

    async def _ingest(self, source: Path, media: Media, result: IngestResult,
                      dry_run: bool, overwrite: bool, keep_source: bool) -> None:  # fmt: skip
        if not source.is_file():
            raise FileNotFoundError(f"source not found: {source}")
        if not await wait_unlocked(source, self.cfg.file_unlock_timeout):
            raise TimeoutError(f"source is still open by another program: {source}")

        logical = logical_name(source.name)
        ext = Path(logical).suffix.lower()
        verdict = evaluate(await self._probe(source), self.profile, ext)
        result.verdict = verdict
        out_ext = (ext if verdict.verdict == "COMPLIANT" and ext in (".mkv", ".mp4", ".m4v")
                   else self.profile.output.extension)  # fmt: skip
        target = plex_target(
            media.kind, media.title, year=media.year, imdb=media.imdb, tmdb=media.tmdb,
            tvdb=media.tvdb, season=media.season, episode=media.episode,
            end_episode=media.end_episode, episode_title=media.episode_title,
            extension=out_ext, movies_dir=self.cfg.ingest_movies_dir,
            tv_dir=self.cfg.ingest_tv_dir, library_root=self.root,
        )  # fmt: skip
        dest = self.root / target.relative_path
        result.target = str(dest)

        if verdict.verdict == "REJECT":
            quarantined = unique_path(self.root / self.cfg.ingest_quarantine_dir / logical)
            result.target = str(quarantined)
            if dry_run:
                result.status = "dry-run"
                return
            await self._quarantine(source, quarantined, dest, verdict.reasons, verdict)
            result.status = "quarantined"
            return
        if dest.exists() and not overwrite:
            raise FileExistsError(f"target already exists: {dest} (use --overwrite to replace)")
        if dry_run:
            result.status = "dry-run"
            return

        # Moves run in a thread: from a local disk to a share they copy gigabytes, and the
        # pipeline runs them next to live downloads in the same event loop.
        if verdict.verdict == "COMPLIANT":
            await asyncio.to_thread(move, source, dest, overwrite)
            result.status = "placed"
        else:
            fixed = await self._fix(source, logical, out_ext, verdict, dest, result)
            if not fixed:
                return  # quarantined inside _fix
            await asyncio.to_thread(move, fixed, dest, overwrite)
            if not keep_source:
                source.unlink(missing_ok=True)
            result.status = "fixed"

        result.sidecars = self._move_sidecars(source, logical, dest, overwrite)
        if self.plex is not None:
            result.plex = await self.plex.refresh(target.library_dir, target.scan_dir)

    async def _fix(self, source: Path, logical: str, out_ext: str, verdict: Verdict, dest: Path,
                   result: IngestResult) -> Path | None:  # fmt: skip
        incoming = self.root / self.cfg.ingest_incoming_dir
        incoming.mkdir(parents=True, exist_ok=True)
        tmp = incoming / f"{Path(logical).stem}.fixing{out_ext}.part"
        done = await self._ffmpeg(*fix_arguments(verdict, str(source), str(tmp)))
        if done.returncode != 0 or not tmp.is_file():
            tmp.unlink(missing_ok=True)
            detail = done.stderr.decode("utf-8", "replace").strip()
            quarantined = unique_path(self.root / self.cfg.ingest_quarantine_dir / logical)
            await self._quarantine(source, quarantined, dest, [f"ffmpeg failed: {detail}"],
                                   verdict)  # fmt: skip
            result.status, result.target, result.error = "quarantined", str(quarantined), (
                "ffmpeg failed")  # fmt: skip
            return None
        check = evaluate(await self._probe(tmp), self.profile, out_ext)
        if check.verdict != "COMPLIANT":
            tmp.unlink(missing_ok=True)
            raise RuntimeError("fixed output is still not compliant: "
                               + "; ".join(check.reasons + check.actions))  # fmt: skip
        if verdict.duration_sec > 0 and abs(check.duration_sec - verdict.duration_sec) > \
                DURATION_TOLERANCE:  # fmt: skip
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"fixed output lasts {check.duration_sec:g}s, the source "
                               f"{verdict.duration_sec:g}s")  # fmt: skip
        return tmp

    async def _quarantine(self, source: Path, quarantined: Path, intended: Path,
                          reasons: list[str], verdict: Verdict) -> None:  # fmt: skip
        await asyncio.to_thread(move, source, quarantined)
        lines = [f"QUARANTINED {datetime.now():%Y-%m-%dT%H:%M:%S}", f"source : {source}",
                 f"intended: {intended}", "", "Reasons:", *(f" - {r}" for r in reasons), "",
                 json.dumps({"verdict": verdict.verdict, "reasons": verdict.reasons,
                             "actions": verdict.actions, "warnings": verdict.warnings,
                             "video": verdict.video}, indent=2)]  # fmt: skip
        quarantined.with_name(quarantined.name + ".reason.txt").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")  # fmt: skip

    def _move_sidecars(self, source: Path, logical: str, dest: Path, overwrite: bool) -> list[str]:
        stem = Path(logical).stem
        moved = []
        for srt in sorted(source.parent.glob("*.srt")):
            if srt.name == f"{stem}.srt" or srt.name.startswith(f"{stem}."):
                suffix = srt.name[len(stem) :]  # ".en.forced.srt" or ".srt"
                target = dest.with_name(dest.stem + suffix)
                move(srt, target, overwrite)
                moved.append(str(target))
        return moved

    def _log(self, result: IngestResult) -> None:
        try:
            folder = log_dir() / "ingest"
            folder.mkdir(parents=True, exist_ok=True)
            entry = {"ts": datetime.now().isoformat(timespec="seconds"), **result.to_dict()}
            with (folder / f"ingest-{datetime.now():%Y%m%d}.log").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
        except OSError as exc:
            log.warning("Could not write the ingest log: %s", exc)
