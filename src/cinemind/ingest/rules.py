"""The Direct Play verdict: can the TV play this file as it is, after a fix, or not at all?

:func:`evaluate` reads an ffprobe result (see :mod:`cinemind.ingest.probe`) against a
:class:`DeviceProfile` and returns a :class:`Verdict`:

- ``COMPLIANT``: plays directly; the file can be placed as it is.
- ``FIXABLE``: plays after a remux. Audio may be converted, subtitles converted or dropped,
  extra streams dropped. **Video is never re-encoded**; see :mod:`cinemind.ingest.fix`.
- ``REJECT``: the video itself can't be played (codec, profile, bit depth, resolution, frame
  rate, Dolby Vision without a base layer) or the container is refused. Quarantine it and
  find another release.

This is a port of MediaPipeline v1's ``Test-DirectPlay``, and the golden cases in
``tests/fixtures/ingest/golden`` hold it to the same reasons, actions and warnings, word for
word. That is why some helpers below mimic PowerShell: case-insensitive comparisons, ``[int]``
casts of missing values, half-even rounding, and .NET number formatting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from cinemind.ingest.profile import DeviceProfile

VerdictKind = Literal["COMPLIANT", "FIXABLE", "REJECT"]


# -- PowerShell-compatible helpers -----------------------------------------------------------


def _get(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else None


def _str(value: Any) -> str:
    """``[string]$value``: None becomes ''."""
    return "" if value is None else str(value)


def _int(value: Any) -> int:
    """``[int]$value``: None and '' become 0; numeric strings are parsed."""
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return round(float(value))


def _float(value: Any) -> float:
    """``[double]$value``: None becomes 0.0."""
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _in(value: str, options: list[str]) -> bool:
    """PowerShell's ``-contains`` for strings: ignores case."""
    folded = value.casefold()
    return any(folded == o.casefold() for o in options)


def _lookup[T](table: dict[str, T], key: str) -> T | None:
    """Property lookup by name, ignoring case (PSObject semantics)."""
    folded = key.casefold()
    return next((v for k, v in table.items() if k.casefold() == folded), None)


def _num(value: float) -> str:
    """A number the way .NET Framework prints a double (G15): 60.0 -> '60', 23.976 -> '23.976'."""
    return format(value, ".15g")


def _rational(value: str) -> float:
    if not value or value == "0/0":
        return 0.0
    num, _, den = value.partition("/")
    try:
        return float(num) / float(den) if den and float(den) != 0 else float(num)
    except ValueError:
        return 0.0


def _bit_depth(stream: dict[str, Any]) -> int:
    raw = _get(stream, "bits_per_raw_sample")
    try:
        if raw and int(raw):
            return int(raw)
    except (TypeError, ValueError):
        pass
    m = re.search(r"p(\d{2})(le|be)?$", _str(_get(stream, "pix_fmt")))
    return int(m.group(1)) if m else 8


# -- the plan -----------------------------------------------------------------------------------


@dataclass
class AudioPlan:
    index: int
    codec: str
    lang: str
    channels: int
    was_default: bool
    action: Literal["copy", "convert", "drop"]
    target: str | None = None
    bitrate: str | None = None
    out_channels: int | None = None
    make_default: bool = False


@dataclass
class SubtitlePlan:
    index: int
    codec: str
    lang: str
    action: Literal["copy", "srt", "drop"]


@dataclass
class Verdict:
    verdict: VerdictKind
    reasons: list[str]
    actions: list[str]
    warnings: list[str]
    video: dict[str, Any] | None
    bitrate_mbps: float
    duration_sec: float
    video_index: int | None = None
    audio: list[AudioPlan] = field(default_factory=list)
    subtitles: list[SubtitlePlan] = field(default_factory=list)


# -- the rules ----------------------------------------------------------------------------------


def evaluate(probe: dict[str, Any], profile: DeviceProfile, extension: str) -> Verdict:
    """The verdict for one file. ``extension`` is the file's extension, e.g. ``.mkv``."""
    reasons: list[str] = []
    actions: list[str] = []
    warnings: list[str] = []
    streams = [s for s in (_get(probe, "streams") or []) if isinstance(s, dict)]
    fmt = _get(probe, "format") or {}
    ext = extension.lower()
    needs_remux = False

    # container
    fmt_name = _str(_get(fmt, "format_name"))
    if _in(ext, profile.containers.direct):
        pass
    elif _in(ext, profile.containers.remux):
        needs_remux = True
        actions.append(f"remux container {ext} -> .mkv")
    elif _in(ext, profile.containers.reject):
        reasons.append(f"container {ext} is not accepted (profile containers.reject)")
    elif re.search("matroska", fmt_name, re.IGNORECASE):
        needs_remux = ext != ".mkv"
        if needs_remux:
            actions.append("normalise extension -> .mkv")
    else:
        needs_remux = True
        actions.append(f"remux unknown container '{ext}' ({fmt_name}) -> .mkv")

    # video
    videos = [s for s in streams
              if _str(_get(s, "codec_type")).casefold() == "video"
              and _int(_get(_get(s, "disposition"), "attached_pic")) != 1]  # fmt: skip
    video_info: dict[str, Any] | None = None
    video_index: int | None = None
    if not videos:
        reasons.append("no video stream")
    else:
        video = videos[0]
        if len(videos) > 1:
            needs_remux = True
            actions.append(f"drop {len(videos) - 1} extra video stream(s)")
        reasons_v, warnings_v, video_info = _video(video, profile)
        reasons += reasons_v
        warnings += warnings_v
        video_index = video_info["index"]

    # audio
    audio = _audio(streams, profile, actions, warnings)
    if any(a.action != "copy" for a in audio):
        needs_remux = True

    # subtitles
    subtitles: list[SubtitlePlan] = []
    for s in streams:
        if _str(_get(s, "codec_type")).casefold() != "subtitle":
            continue
        codec = _str(_get(s, "codec_name"))
        lang = _str(_get(_get(s, "tags"), "language")) or "und"
        raw_index = _str(_get(s, "index"))
        action: Literal["copy", "srt", "drop"] = "drop"
        if _in(codec, profile.subtitles.direct):
            action = "copy"
        elif _in(codec, profile.subtitles.convert_to_srt):
            action = "srt"
            actions.append(f"convert subtitle #{raw_index} {codec} [{lang}] -> SRT")
        else:
            actions.append(f"drop subtitle #{raw_index} {codec} [{lang}] "
                           "(image/unsupported subs force a burn-in video transcode)")  # fmt: skip
        subtitles.append(SubtitlePlan(_int(_get(s, "index")), codec, lang, action))
    if any(s.action != "copy" for s in subtitles):
        needs_remux = True
    if ext in (".mp4", ".m4v") and any(s.action == "copy" for s in subtitles):
        needs_remux = True  # MP4 can't carry SRT

    # bitrate
    mbps = round(_float(_get(fmt, "bit_rate")) / 1e6, 1)
    limit = profile.bitrate.warn_above_mbps
    if mbps > limit:
        warnings.append(f"average bitrate {_num(mbps)} Mbps > {_num(limit)} Mbps: TV LAN port "
                        "may be 10/100 - prefer strong 5 GHz Wi-Fi on the TV")  # fmt: skip

    verdict: VerdictKind = "REJECT" if reasons else "FIXABLE" if needs_remux else "COMPLIANT"
    return Verdict(verdict, reasons, actions, warnings, video_info, mbps,
                   _float(_get(fmt, "duration")), video_index, audio, subtitles)  # fmt: skip


def _video(
    video: dict[str, Any], profile: DeviceProfile
) -> tuple[list[str], list[str], dict[str, Any]]:
    reasons: list[str] = []
    warnings: list[str] = []
    codec = _str(_get(video, "codec_name"))
    vprof = _str(_get(video, "profile"))
    w, h = _int(_get(video, "width")), _int(_get(video, "height"))
    fps = _rational(_str(_get(video, "avg_frame_rate")))
    if fps <= 0:
        fps = _rational(_str(_get(video, "r_frame_rate")))
    depth = _bit_depth(video)
    transfer = _str(_get(video, "color_transfer")).casefold()
    hdr = {"smpte2084": "HDR10 (PQ)", "arib-std-b67": "HLG"}.get(transfer, "SDR")

    dv: tuple[int, int] | None = None
    for sd in _get(video, "side_data_list") or []:
        if _str(_get(sd, "side_data_type")).casefold() == "dovi configuration record":
            dv = (_int(_get(sd, "dv_profile")), _int(_get(sd, "dv_bl_signal_compatibility_id")))
    info: dict[str, Any] = {"index": _int(_get(video, "index")), "codec": codec, "profile": vprof,
                            "width": w, "height": h, "fps": round(fps, 3), "bitDepth": depth,
                            "hdr": hdr}  # fmt: skip
    if dv:
        info["dolbyVision"] = f"profile {dv[0]}, base-layer compat {dv[1]}"

    rules = _lookup(profile.video.direct, codec)
    if rules:
        if not _in(vprof, rules.profiles):
            reasons.append(f"{codec} profile '{vprof}' is not Direct Play safe on the "
                           f"{profile.device}")  # fmt: skip
        if depth > rules.max_bit_depth:
            reasons.append(f"{codec} {depth}-bit exceeds {rules.max_bit_depth}-bit "
                           "(e.g. H.264 Hi10P cannot be hardware decoded)")  # fmt: skip
        if w > rules.max_width or h > rules.max_height:
            reasons.append(f"resolution {w}x{h} exceeds {rules.max_width}x{rules.max_height}")
        if fps > rules.max_fps + 0.5:
            reasons.append(f"frame rate {_num(round(fps, 2))} exceeds {_num(rules.max_fps)}")
        max4k = rules.max4k_fps
        if max4k and (w > 1920 or h > 1088) and fps > max4k + 0.5:
            reasons.append(f"{codec} at 4K above {_num(max4k)} fps is not supported by the TV "
                           "decoder")  # fmt: skip
    elif _in(codec, profile.video.review):
        warnings.append(f"{codec}: the {profile.device} hardware decodes it, but confirm "
                        "'Direct Play' once in Plex > Dashboard")  # fmt: skip
    else:
        reasons.append(_lookup(profile.video.reject_reasons, codec)
                       or f"video codec '{codec}' is not in the Direct Play profile")  # fmt: skip

    if dv:
        dv_rules = profile.video.dolby_vision
        if dv[0] in dv_rules.reject_profiles:
            reasons.append(f"Dolby Vision profile {dv[0]} has no HDR10 base layer; {profile.brand}"
                           " TVs cannot display it without a tone-map transcode")  # fmt: skip
        elif dv_rules.reject_when_no_compatible_base_layer and dv[1] == 0:
            reasons.append(f"Dolby Vision profile {dv[0]} without a compatible base layer")
        else:
            warnings.append(f"Dolby Vision profile {dv[0]}: the TV plays the HDR10/SDR base "
                            f"layer (no DV on {profile.brand})")  # fmt: skip
    return reasons, warnings, info


def _audio(
    streams: list[dict[str, Any]], profile: DeviceProfile, actions: list[str], warnings: list[str]
) -> list[AudioPlan]:
    rules = profile.audio
    plan: list[AudioPlan] = []
    tracks = [s for s in streams if _str(_get(s, "codec_type")).casefold() == "audio"]
    if not tracks:
        warnings.append("no audio stream")
    for track in tracks:
        codec = _str(_get(track, "codec_name"))
        ok = _in(codec, rules.direct)
        aac_profile = _str(_get(track, "profile"))
        if ok and codec.casefold() == "aac" and aac_profile:
            ok = _in(aac_profile, rules.aac_profiles)
        plan.append(AudioPlan(
            index=_int(_get(track, "index")), codec=codec,
            lang=_str(_get(_get(track, "tags"), "language")) or "und",
            channels=_int(_get(track, "channels")),
            was_default=_int(_get(_get(track, "disposition"), "default")) == 1,
            action="copy" if ok else "convert",
        ))  # fmt: skip

    for ap in plan:
        if ap.action != "convert":
            continue
        sibling = None
        if rules.prefer_existing_compatible_track_same_language:
            sibling = next((s for s in plan if s.action == "copy" and (
                s.lang.casefold() == ap.lang.casefold() or ap.lang.casefold() == "und"
                or s.lang.casefold() == "und")), None)  # fmt: skip
        if sibling:
            ap.action = "drop"
            actions.append(f"drop audio #{ap.index} {ap.codec} [{ap.lang}] - compatible "
                           f"{sibling.codec} track #{sibling.index} already present")  # fmt: skip
            if ap.was_default:
                sibling.make_default = True
        else:
            target = rules.target_multichannel if ap.channels > 2 else rules.target_stereo
            ap.target, ap.bitrate = target.codec, target.bitrate
            ap.out_channels = min(target.channels, max(1, ap.channels))
            actions.append(f"convert audio #{ap.index} {ap.codec} {ap.channels}ch [{ap.lang}] -> "
                           f"{ap.target} {ap.bitrate} {ap.out_channels}ch")  # fmt: skip

    kept = [a for a in plan if a.action != "drop"]
    if kept:
        default = next((a for a in kept if a.make_default), None) or next(
            (a for a in kept if a.was_default), kept[0]
        )
        for a in kept:
            a.make_default = a is default
    return plan
