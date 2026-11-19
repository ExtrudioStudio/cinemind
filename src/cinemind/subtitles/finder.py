"""Find a subtitle that is really in sync with a video, and save it next to the video.

1. Candidates come from every provider that has the language, ordered by: an OpenSubtitles hash
   match, then release-name score, then Wizdom first (no daily download limit), then
   popularity.
2. The audio sync check runs against each audio language of the video (dual-audio files are
   usually timed to the original language, not always English).
3. Up to ``max_candidates`` are downloaded and checked, best first. The first one that is
   ``synced``, ``shifted`` or ``rescaled`` is saved, retimed if needed, as clean UTF-8 SRT. An
   ``inconclusive`` one (too little signal to call; music-heavy films do this) is only accepted
   when the hash or the release name (score >= 50) says it's the same release.
   Cam releases and other cuts (score <= -30) are never downloaded.

A port of MediaPipeline v1's ``Get-HebrewSubtitle``, without its drag-and-drop window: a
headless run can't wait for a window. When nothing fits, the result says why and lists what
was tried, so the CLI (or an agent) can ask you.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from cinemind.ingest.probe import run_tool
from cinemind.subtitles.providers import SubCandidate
from cinemind.subtitles.srt import decode, format_srt, parse
from cinemind.subtitles.sync import AudioWindows, SyncResult, audio_windows, check_sync

log = logging.getLogger(__name__)

ACCEPTED = ("synced", "shifted", "rescaled")
MIN_CUES = 20


class Provider(Protocol):
    name: str

    def supports(self, language: str) -> bool: ...

    async def fetch(self, cand: SubCandidate, work_dir: Path) -> Path: ...


SearchFn = Callable[[], Awaitable[list[SubCandidate]]]
AudioFn = Callable[[], Awaitable[list[AudioWindows]]]


@dataclass
class FindResult:
    status: Literal["found", "none"]
    path: Path | None = None
    source: str | None = None
    release: str | None = None
    sync: SyncResult | None = None
    reason: str | None = None
    tried: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)


def order(cands: list[SubCandidate]) -> list[SubCandidate]:
    return sorted(cands, key=lambda c: (not c.hash_match, -c.score, c.source != "wizdom",
                                        -c.downloads))  # fmt: skip


async def audio_sets(video: Path, probe: dict[str, Any], ffmpeg: str,
                     max_tracks: int = 3) -> list[AudioWindows]:  # fmt: skip
    """One set of windows per audio language (at most 3). Hebrew dubs are skipped: Hebrew
    subtitles are never timed to them."""
    sets: list[AudioWindows] = []
    first = await audio_windows(str(video), probe, ffmpeg)
    if first is None:
        return sets
    sets.append(first)
    seen = {"heb", "he", first.language}
    for stream in probe.get("streams") or []:
        if len(sets) >= max_tracks:
            break
        lang = str((stream.get("tags") or {}).get("language") or "")
        if stream.get("codec_type") != "audio" or not lang or lang == "und" or lang in seen:
            continue
        seen.add(lang)
        extra = await audio_windows(str(video), probe, ffmpeg, stream=stream)
        if extra is not None:
            sets.append(extra)
    return sets


def _check(cues: list[Any], sets: list[AudioWindows]) -> SyncResult | None:
    """The first accepted result over the audio languages, else the most confident one."""
    best: SyncResult | None = None
    for audio in sets:
        result = check_sync(cues, audio)
        result.detail = f"audio={audio.language or '?'} {result.detail}"
        if result.status in ACCEPTED:
            return result
        if best is None or result.confidence > best.confidence:
            best = result
    return best


async def to_srt_text(path: Path, ffmpeg: str | None) -> str:
    """The subtitle as SRT text; .ass/.ssa/.vtt are converted with FFmpeg first."""
    if path.suffix.lower() == ".srt":
        return decode(path.read_bytes())
    if ffmpeg is None:
        raise ValueError(f"converting {path.suffix} to SRT needs FFmpeg")
    out = path.with_suffix(".converted.srt")
    done = await run_tool(ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
                          "-c:s", "srt", str(out))  # fmt: skip
    if done.returncode != 0 or not out.is_file():
        raise ValueError(f"could not convert {path.name} to SRT")
    return decode(out.read_bytes())


async def find_subtitle(
    video: Path,
    out_path: Path,
    providers: dict[str, Provider],
    searches: list[SearchFn],
    audio: AudioFn | None,
    ffmpeg: str | None = None,
    max_candidates: int = 4,
) -> FindResult:
    """Search, download, check and save. ``audio`` is None to skip the sync check."""
    result = FindResult("none")
    sets: list[AudioWindows] = []
    if audio is not None:
        try:
            sets = await audio()
        except Exception as exc:  # no FFmpeg, unreadable file...: carry on by release name
            result.log.append(f"audio sync check unavailable: {exc}")
    cands: list[SubCandidate] = []
    for search in searches:
        try:
            cands += await search()
        except Exception as exc:
            result.log.append(f"search failed: {exc}")
    cands = order(cands)
    result.log.append(f"candidates: {len(cands)}")

    with tempfile.TemporaryDirectory(prefix="cinemind-subs-") as tmp:
        work = Path(tmp)
        tried = 0
        for cand in cands:
            if tried >= max_candidates:
                break
            label = f"{cand.source} '{cand.release}' score={cand.score} hash={cand.hash_match}"
            if cand.score <= -30:
                result.log.append(f"{label}: skipped (cam or another cut)")
                continue
            trusted = cand.hash_match or cand.score >= 50
            if not sets and not trusted:
                result.log.append(f"{label}: not downloaded (no audio check, release differs)")
                continue
            tried += 1
            result.tried.append(label)
            try:
                file = await providers[cand.source].fetch(cand, work)
                cues = parse(await to_srt_text(file, ffmpeg))
            except Exception as exc:
                result.log.append(f"{label}: {exc}")
                continue
            if len(cues) < MIN_CUES:
                result.log.append(f"{label}: only {len(cues)} cues")
                continue
            sync = _check(cues, sets) if sets else None
            result.log.append(f"{label}: {sync.status if sync else 'no audio check'} "
                              f"{sync.detail if sync else ''}".rstrip())  # fmt: skip
            accepted = (sync is not None and sync.status in ACCEPTED) or (
                trusted and (sync is None or sync.status == "inconclusive")
            )
            if not accepted:
                continue
            retime = sync is not None and sync.status in ACCEPTED
            scale, offset = (sync.scale, sync.offset) if sync and retime else (1.0, 0.0)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(format_srt(cues, scale, offset), encoding="utf-8-sig", newline="")
            result.status, result.path, result.sync = "found", out_path, sync
            result.source, result.release = cand.source, cand.release
            return result

    result.reason = (
        "no subtitles were found" if not cands
        else f"found {len(cands)} candidate(s), but none matched this file's audio timing"
    )  # fmt: skip
    return result


async def find_for_video(
    cfg: Any,
    video: Path,
    imdb: str,
    season: int | None = None,
    episode: int | None = None,
    language: str | None = None,
    out_path: Path | None = None,
    sync_check: bool = True,
) -> FindResult:
    """find_subtitle with the real providers, credentials and tools from the config."""
    import httpx

    from cinemind.core.config import cache_dir
    from cinemind.core.secrets import get_secret
    from cinemind.ingest.probe import ToolMissing, find_tool, probe
    from cinemind.subtitles.providers import OpenSubtitles, Wizdom

    language = (language or cfg.subtitle_language).lower()
    out_path = out_path or video.with_name(f"{video.stem}.{language}.srt")
    try:
        ffmpeg: str | None = find_tool("ffmpeg", cfg.ffmpeg_path)
        ffprobe: str | None = find_tool("ffprobe", cfg.ffprobe_path)
    except ToolMissing:
        ffmpeg = ffprobe = None
    headers = {"User-Agent": cfg.user_agent}
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, headers=headers) as http:
        providers: dict[str, Provider] = {}
        searches: list[SearchFn] = []
        wizdom = Wizdom(http)
        if wizdom.supports(language):
            providers[wizdom.name] = wizdom
            searches.append(lambda: wizdom.search(imdb, season, episode, video.name))
        key, password = get_secret("opensubtitles_api_key"), get_secret("opensubtitles_password")
        if key and password and cfg.opensubtitles_username:
            osub = OpenSubtitles(http, key, cfg.opensubtitles_username, password,
                                 cache_dir() / "subtitles", cfg.user_agent)  # fmt: skip
            providers[osub.name] = osub
            searches.append(lambda: osub.search(imdb, season, episode, video, language))

        audio: AudioFn | None = None
        if sync_check and ffmpeg and ffprobe:
            tools = (ffmpeg, ffprobe)

            async def audio_fn() -> list[AudioWindows]:
                return await audio_sets(video, await probe(video, tools[1]), tools[0])

            audio = audio_fn
        result = await find_subtitle(video, out_path, providers, searches, audio, ffmpeg,
                                     cfg.subtitle_max_candidates)  # fmt: skip
        if sync_check and audio is None:
            result.log.insert(0, "audio sync check skipped: FFmpeg not found")
        if not providers:
            result.reason = (f"no subtitle provider for '{language}': Wizdom only has Hebrew; "
                             "set up OpenSubtitles with `cinemind secrets set "
                             "opensubtitles_api_key` and opensubtitles_password")  # fmt: skip
        return result
