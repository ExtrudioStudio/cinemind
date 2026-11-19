"""How well a subtitle's release name fits a video's release name.

A subtitle made for the same release (same group, same source, same cut) is almost always in
sync; one for another cut (extended, IMAX) almost never is. The score ranks candidates before
the audio check gets the final say. A port of MediaPipeline v1's ``Get-ReleaseScore``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePath

SERVICES = {"amzn", "nf", "dsnp", "atvp", "it", "ma", "hmax", "max", "hulu", "pcok", "pmtp",
            "stan", "cr"}  # fmt: skip
EDITIONS = {"imax", "extended", "unrated", "directors", "dc", "remastered", "theatrical",
            "uncut"}  # fmt: skip
_FAMILY = {"webdl": "web", "webrip": "web", "web": "web", "bluray": "bluray", "hdtv": "hdtv",
           "dvd": "dvd", "cam": "cam"}  # fmt: skip
_PART = (".part", ".partial", ".tmp", ".crdownload", ".download")
_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".srt", ".ass", ".ssa", ".sub", ".zip"}


@dataclass
class Traits:
    group: str = ""
    source: str = ""
    resolution: str = ""
    services: list[str] = field(default_factory=list)
    editions: list[str] = field(default_factory=list)


def traits(name: str) -> Traits:
    for suffix in _PART:
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    # Only real extensions come off. v1 used GetFileNameWithoutExtension, which also cut
    # ".x264-GROUP" from a bare release name and so read the group as "dl".
    path = PurePath(name)
    n = (path.stem if path.suffix.lower() in _EXTENSIONS else name).lower()
    tokens = [t for t in re.split(r"[\s._\-\[\]()]+", n) if t]
    t = Traits()
    if m := re.search(r"-([a-z0-9]+)$", n):
        t.group = m.group(1)
    if re.search(r"web[-. ]?dl", n):
        t.source = "webdl"
    elif "webrip" in tokens:
        t.source = "webrip"
    elif "web" in tokens:
        t.source = "web"
    elif re.search(r"blu[-. ]?ray|bdrip|brrip|bdremux|remux", n):
        t.source = "bluray"
    elif "hdtv" in tokens:
        t.source = "hdtv"
    elif re.search(r"dvdrip|dvd", n):
        t.source = "dvd"
    if re.search(r"telesync|hdts|hdcam|\bcam\b|\bts\b|telecine|\btc\b", n):
        t.source = "cam"
    t.resolution = next((r for r in ("2160p", "1080p", "720p", "576p", "480p") if r in tokens), "")
    if not t.resolution and ("4k" in tokens or "4klight" in n):
        t.resolution = "2160p"
    t.services = [tok for tok in tokens if tok in SERVICES]
    t.editions = [tok for tok in tokens if tok in EDITIONS]
    return t


def score(video: str, candidate: str) -> int:
    """Higher is better: group +50, source family +20 (+5 exact), resolution +10, streaming
    service +10; a cam candidate for a non-cam video -40, each edition mismatch -15."""
    a, b = traits(video), traits(candidate)
    total = 0
    if a.group and a.group == b.group:
        total += 50
    if a.source and b.source:
        if _FAMILY[a.source] == _FAMILY[b.source]:
            total += 20 + (5 if a.source == b.source else 0)
        if b.source == "cam" and a.source != "cam":
            total -= 40
    if a.resolution and a.resolution == b.resolution:
        total += 10
    if any(s in b.services for s in a.services):
        total += 10
    total -= 15 * sum(1 for e in b.editions if e not in a.editions)
    total -= 15 * sum(1 for e in a.editions if e not in b.editions)
    return total
