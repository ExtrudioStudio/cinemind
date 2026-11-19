"""Does this subtitle belong to this video? Correlate its timing with the video's speech.

The idea: people speak when subtitles are on screen. Decode a few windows of the dialogue track
(8 kHz mono, 300-3400 Hz speech band; only the centre channel for 5.1, where the dialogue is),
turn each into z-normalised log energy per 100 ms frame, and slide the subtitle's on/off
timeline across it. A matching subtitle lines up with speech at one shift, the same in every
window; a subtitle for another cut or another film doesn't.

The check tries three speeds (as is, and both ways between 23.976 and 25 fps, the classic
PAL speed-up) and returns one of:

- ``synced`` or ``shifted``: apply ``offset``
- ``rescaled``: apply ``scale`` (and ``offset``), via ``t' = t * scale + offset``
- ``nomatch``: some alignment, but too weak or not consistent across windows
- ``inconclusive``: not enough audio or cues to judge

A port of MediaPipeline v1's ``Test-SubtitleSync`` and its C# helpers, with numpy. v1's
thresholds were calibrated on real files: a true match scored z≈9, the wrong film z≈2. A
+7 s shift and a 25 fps stretch were both recovered.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
import numpy.typing as npt

from cinemind.ingest.probe import run_tool
from cinemind.subtitles.srt import Cue

FRAME = 0.1  # seconds per energy frame
SAMPLE_RATE = 8000
SCALES = (1.0, 25 / 23.976, 23.976 / 25)
Status = Literal["synced", "shifted", "rescaled", "nomatch", "inconclusive"]
Floats = npt.NDArray[np.float64]


@dataclass
class Window:
    start: float  # seconds into the video where the window begins (after its padding)
    energy: Floats  # one value per frame, from start - pad to start + length + pad


@dataclass
class AudioWindows:
    duration: float
    window_sec: float
    pad_sec: float
    windows: list[Window] = field(default_factory=list)
    language: str = ""
    stream_index: int = -1
    centre_only: bool = False


@dataclass
class SyncResult:
    status: Status
    scale: float = 1.0
    offset: float = 0.0
    confidence: float = 0.0  # combined z-score of the best alignment
    detail: str = ""


# -- signal helpers (v1's MpSubsV2) ------------------------------------------------------------


def frame_energy(pcm: bytes, sample_rate: int = SAMPLE_RATE, frame_sec: float = FRAME) -> Floats:
    """s16le mono PCM -> z-normalised log energy per frame."""
    size = int(sample_rate * frame_sec)
    samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype="<i2")
    n = len(samples) // size
    if n == 0:
        return np.zeros(0)
    frames = samples[: n * size].astype(np.float64).reshape(n, size)
    energy = np.log10((frames**2).mean(axis=1) + 1.0)
    sd = energy.std()
    return (energy - energy.mean()) / (sd if sd >= 1e-9 else 1.0)


def correlate(active: Floats, energy: Floats, pad: int, max_shift: int) -> Floats:
    """Mean energy under the subtitle-active frames, for every shift in [-max, +max] frames."""
    result = np.zeros(2 * max_shift + 1)
    on = np.flatnonzero(active > 0)
    if on.size == 0:
        return result
    for k in range(-max_shift, max_shift + 1):
        idx = on + pad + k
        idx = idx[(idx >= 0) & (idx < energy.size)]
        result[k + max_shift] = energy[idx].sum() / on.size
    return result


def zcurve(r: Floats) -> Floats | None:
    """z-normalise a correlation curve; None if it's flat (no subtitle activity)."""
    sd = r.std()
    return None if sd < 1e-12 else (r - r.mean()) / sd


def combine(curves: list[Floats]) -> Floats:
    """Per-window z curves summed and divided by sqrt(windows): agreement adds up."""
    return np.sum(curves, axis=0) / math.sqrt(len(curves))


def max_around(z: Floats, centre: int, radius: int) -> float:
    return float(z[max(0, centre - radius) : min(z.size - 1, centre + radius) + 1].max())


# -- the check ---------------------------------------------------------------------------------


def check_sync(
    cues: list[Cue],
    audio: AudioWindows | None,
    max_shift_sec: float = 12.0,
    min_z: float = 5.0,
    window_z: float = 2.0,
) -> SyncResult:
    if audio is None or len(audio.windows) < 2 or len(cues) < 20:
        return SyncResult("inconclusive", detail="not enough audio or cues")
    last = max(c.end for c in cues)
    pad, max_k = int(audio.pad_sec / FRAME), int(max_shift_sec / FRAME)
    n = int(audio.window_sec / FRAME)
    results: list[dict[str, Any]] = []
    for scale in SCALES:
        if last * scale > audio.duration + 120:
            continue  # would run past the end of the video
        curves: list[tuple[float, Floats]] = []
        for w in audio.windows:
            active = np.zeros(n)
            for c in cues:
                s, e = c.start * scale - w.start, c.end * scale - w.start
                if e <= 0 or s >= audio.window_sec:
                    continue
                i0 = max(0, math.floor(s / FRAME))
                i1 = min(n - 1, math.ceil(e / FRAME))
                active[i0 : i1 + 1] = 1.0
            z = zcurve(correlate(active, w.energy, pad, max_k))
            if z is not None:
                curves.append((w.start, z))
        if len(curves) < 2:
            continue
        combined = combine([z for _, z in curves])
        ks = int(np.argmax(combined))
        support, per = 0, []
        for start, z in curves:
            local = max_around(z, ks, 5)
            own = (int(np.argmax(z)) - max_k) * FRAME
            support += local >= window_z
            per.append(f"@{start:,.0f}s z@peak={local:.1f} own={own:+.1f}s")
        results.append({"scale": scale, "offset": round((ks - max_k) * FRAME, 1),
                        "z": round(float(combined[ks]), 1), "support": support,
                        "windows": len(curves), "per": per})  # fmt: skip
    if not results:
        return SyncResult("nomatch", detail="the subtitle runs past the end of the video")
    best = max(results, key=lambda r: r["z"])
    need = max(2, math.ceil(best["windows"] / 2))
    windows = ", ".join(best["per"])
    detail = (f"scale={best['scale']:.4f} off={best['offset']:+.1f}s z={best['z']} "
              f"support={best['support']}/{best['windows']} [{windows}]")  # fmt: skip
    if best["z"] < min_z or best["support"] < need:
        status: Status = "nomatch" if best["z"] >= 3.5 else "inconclusive"
        return SyncResult(status, confidence=best["z"], detail=detail)
    if abs(best["scale"] - 1.0) > 0.001:
        status = "rescaled"
    elif abs(best["offset"]) > 0.3:
        status = "shifted"
    else:
        return SyncResult("synced", confidence=best["z"], detail=detail)
    return SyncResult(status, best["scale"], best["offset"], best["z"], detail)


# -- decoding the audio ------------------------------------------------------------------------


def _windows_plan(duration: float) -> tuple[float, float, tuple[float, ...]] | None:
    window, pad = 300.0, 15.0
    fractions: tuple[float, ...] = (0.15, 0.32, 0.5, 0.68, 0.85)
    if duration < 600:
        window, fractions = max(60.0, math.floor((duration - 4 * pad) / 3)), (0.2, 0.5, 0.8)
    if duration < window + 2 * pad + 30:
        return None
    return window, pad, fractions


def pick_audio_stream(probe: dict[str, Any]) -> dict[str, Any] | None:
    """English if there is one, else the default track, else the first."""
    tracks = [s for s in probe.get("streams") or [] if s.get("codec_type") == "audio"]
    for want in (lambda s: (s.get("tags") or {}).get("language") in ("eng", "en"),
                 lambda s: (s.get("disposition") or {}).get("default") == 1):  # fmt: skip
        found: dict[str, Any] | None = next((s for s in tracks if want(s)), None)
        if found:
            return found
    first: dict[str, Any] | None = tracks[0] if tracks else None
    return first


async def audio_windows(video: str, probe: dict[str, Any], ffmpeg: str,
                        stream: dict[str, Any] | None = None) -> AudioWindows | None:  # fmt: skip
    """Decode the windows the check needs from one audio track (default: pick_audio_stream)."""
    duration = float((probe.get("format") or {}).get("duration") or 0)
    plan = _windows_plan(duration)
    stream = stream or pick_audio_stream(probe)
    if plan is None or stream is None:
        return None
    window, pad, fractions = plan
    centre = int(stream.get("channels") or 0) >= 6  # 5.1/7.1: dialogue is in the centre channel
    band = "highpass=f=300,lowpass=f=3400"
    mix = ["-af", f"pan=mono|c0=FC,{band}"] if centre else ["-ac", "1", "-af", band]
    language = str((stream.get("tags") or {}).get("language") or "")
    index = int(stream.get("index") or 0)
    result = AudioWindows(duration, window, pad, language=language, stream_index=index,
                          centre_only=centre)  # fmt: skip
    for fraction in fractions:
        start = round(duration * fraction - window / 2)
        if start < pad or start + window + pad > duration:
            continue
        done = await run_tool(ffmpeg, "-hide_banner", "-nostdin", "-v", "error",
                              "-ss", f"{start - pad:.3f}", "-t", f"{window + 2 * pad:.3f}",
                              "-i", video, "-map", f"0:{result.stream_index}", "-vn", "-sn",
                              "-dn", *mix, "-ar", str(SAMPLE_RATE), "-f", "s16le",
                              "pipe:1")  # fmt: skip
        if done.returncode != 0 or len(done.stdout) < 16000:
            continue
        result.windows.append(Window(float(start), frame_energy(done.stdout)))
    return result
