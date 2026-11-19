"""SRT in and out: decode whatever encoding a subtitle site used, parse cues, write clean UTF-8.

Hebrew subtitles still often come in Windows-1255, without a BOM. Decoding tries a BOM first,
then strict UTF-8, then Windows-1255, as MediaPipeline v1 did.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

_CUE = re.compile(
    r"^\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
    r"[^\r\n]*\r?\n(?P<txt>(?:.*\S.*(?:\r?\n|$))*)",
    re.MULTILINE,
)


@dataclass(frozen=True)
class Cue:
    start: float  # seconds
    end: float
    text: str


def decode(data: bytes) -> str:
    """BOM (UTF-8/UTF-16) -> strict UTF-8 -> Windows-1255 (classic Hebrew)."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data.startswith(b"\xff\xfe"):
        return data[2:].decode("utf-16-le", "replace")
    if data.startswith(b"\xfe\xff"):
        return data[2:].decode("utf-16-be", "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1255", "replace")


def _seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + float("0." + ms.ljust(3, "0"))


def parse(text: str) -> list[Cue]:
    return [
        Cue(
            _seconds(*m.group(1, 2, 3, 4)), _seconds(*m.group(5, 6, 7, 8)), m.group("txt").rstrip()
        )  # fmt: skip
        for m in _CUE.finditer(text)
    ]


def _timestamp(t: float) -> str:
    ms = round(t * 1000)
    hours, rest = divmod(ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{math.floor(hours):02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def format_srt(cues: list[Cue], scale: float = 1.0, offset: float = 0.0) -> str:
    """SRT text, retimed as ``t' = t * scale + offset``. Cues that end before 0 are dropped."""
    out: list[str] = []
    number = 0
    for cue in cues:
        start, end = cue.start * scale + offset, cue.end * scale + offset
        if end <= 0:
            continue
        number += 1
        out += [str(number), f"{_timestamp(max(0.0, start))} --> {_timestamp(end)}", cue.text, ""]
    return "\r\n".join(out) + ("\r\n" if out else "")
