"""Stremio stream addons (such as Torrentio) as a stream source.

No addon is configured by default. Paste a manifest URL into ``stream_addons`` to use one; a
*configured* Torrentio URL keeps its options in the path.

Torrentio stream example (abridged)::

    name:  "Torrentio\\n1080p"                     (line 2 is usually, but not always, a resolution)
    title: "Friends S01 1080p BluRay x265-GROUP\\n"  (release name)
           "Friends.S01E10.1080p.mkv\\n"            (file inside a pack; absent for single files)
           "👤 21 💾 6.55 GB ⚙️ Rutracker\\n"        (seeders, size, source site)
           "🇬🇧 / 🇷🇺"                              (audio languages; absent for English-only)
    infoHash, fileIdx, behaviorHints.filename, sources ["tracker:udp://…", "dht:<hash>"]
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import PurePosixPath
from typing import Any

from cinemind.core.aio import reraise_cancelled
from cinemind.core.models import Candidate, EpisodeRef, Title
from cinemind.planning.streams import detect_resolution
from cinemind.sources.base import SourceContext
from cinemind.sources.stremio.client import Addon, AddonClient

log = logging.getLogger(__name__)

_SEEDERS = re.compile(r"👤\s*(\d+)")
_SIZE = re.compile(r"💾\s*([\d.,]+)\s*([KMGT]?B)", re.I)
_SOURCE = re.compile(r"⚙️?\s*(.+?)\s*$")
_FLAG = re.compile(r"[\U0001F1E6-\U0001F1FF]{2}")
_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}

# Country flag → audio language. Unlisted flags fall back to the lowercase country code.
_COUNTRY_LANG = {
    "GB": "en", "US": "en", "AU": "en", "CA": "en", "IE": "en", "NZ": "en",
    "RU": "ru", "UA": "uk", "FR": "fr", "IT": "it", "ES": "es", "MX": "es", "AR": "es",
    "DE": "de", "AT": "de", "PT": "pt", "BR": "pt", "NL": "nl", "PL": "pl", "CZ": "cs",
    "SK": "sk", "HU": "hu", "RO": "ro", "BG": "bg", "GR": "el", "TR": "tr", "IL": "he",
    "SA": "ar", "AE": "ar", "EG": "ar", "IR": "fa", "IN": "hi", "JP": "ja", "KR": "ko",
    "CN": "zh", "TW": "zh", "HK": "zh", "TH": "th", "VN": "vi", "ID": "id", "MY": "ms",
    "PH": "tl", "SE": "sv", "NO": "no", "DK": "da", "FI": "fi", "EE": "et", "LV": "lv",
    "LT": "lt", "RS": "sr", "HR": "hr", "SI": "sl", "LA": "es",
}  # fmt: skip


def _flag_to_country(flag: str) -> str:
    return "".join(chr(ord(ch) - 0x1F1E6 + ord("A")) for ch in flag)


def parse_languages(text: str) -> tuple[str, ...]:
    langs: list[str] = []
    for flag in _FLAG.findall(text):
        country = _flag_to_country(flag)
        lang = _COUNTRY_LANG.get(country, country.lower())
        if lang not in langs:
            langs.append(lang)
    if "🌐" in text and "multi" not in langs:
        langs.append("multi")
    return tuple(langs)


def parse_size(text: str) -> int:
    m = _SIZE.search(text)
    if not m:
        return 0
    number = float(m.group(1).replace(",", ""))
    return int(number * _UNITS.get(m.group(2).upper(), 1))


def normalize_stream(stream: dict[str, Any], addon: str = "") -> Candidate | None:
    info_hash = (stream.get("infoHash") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{40}", info_hash):
        return None  # HTTP/debrid or malformed streams aren't handled by the torrent engine
    name = stream.get("name") or ""
    label = name.split("\n", 1)[1].strip() if "\n" in name else ""
    title = stream.get("title") or stream.get("description") or ""
    lines = [line.strip() for line in title.split("\n") if line.strip()]

    stats_idx = next((i for i, line in enumerate(lines) if "👤" in line or "💾" in line), None)
    head = lines[:stats_idx] if stats_idx is not None else lines
    tail = lines[stats_idx + 1 :] if stats_idx is not None else []
    stats = lines[stats_idx] if stats_idx is not None else ""

    release = head[0] if head else ""
    inner_path = head[1] if len(head) > 1 else ""
    hints = stream.get("behaviorHints") or {}
    filename = hints.get("filename") or PurePosixPath(inner_path).name or release

    seeders_m = _SEEDERS.search(stats)
    source_m = _SOURCE.search(stats)
    raw_idx = stream.get("fileIdx")
    file_idx = int(raw_idx) if raw_idx is not None and str(raw_idx).isdigit() else None
    hdr = tuple(t for t in re.split(r"[\s|]+", label) if t.upper() in {"DV", "HDR", "HDR10+"})

    return Candidate(
        info_hash=info_hash,
        file_idx=file_idx,
        resolution=detect_resolution(label, filename, release),
        seeders=int(seeders_m.group(1)) if seeders_m else 0,
        size=parse_size(stats),
        source=source_m.group(1) if source_m else "",
        languages=parse_languages(" ".join(tail)),
        filename=filename,
        release=release,
        is_pack=bool(inner_path),
        hdr=hdr,
        trackers=tuple(
            s.split(":", 1)[1] for s in stream.get("sources") or [] if s.startswith("tracker:")
        ),
        label=label,
        addon=addon,
    )


class StremioAddonSource:
    """Torrent streams from the Stremio addons in ``stream_addons``."""

    name = "stremio-addons"

    def __init__(self, client: AddonClient, addons: list[Addon]):
        self.client = client
        self.addons = addons

    def _addons_for(self, title: Title, stream_id: str) -> list[Addon]:
        return [a for a in self.addons if a.supports("stream", title.type, stream_id)]

    def supports(self, title: Title) -> bool:
        return any(a.supports("stream", title.type) for a in self.addons)

    async def candidates(self, title: Title, ref: EpisodeRef | None) -> list[Candidate]:
        stream_id = title.stream_id(ref)
        addons = self._addons_for(title, stream_id)
        results = await asyncio.gather(
            *(self.client.streams(a, title.type, stream_id) for a in addons),
            return_exceptions=True,
        )
        found: list[Candidate] = []
        for addon, raw in zip(addons, results, strict=True):
            streams = reraise_cancelled(raw)
            if isinstance(streams, Exception):
                log.warning("Streams failed on %s for %s: %s", addon.name, stream_id, streams)
                continue
            for stream in streams:
                cand = normalize_stream(stream, addon.name)
                if cand is not None:
                    found.append(cand)
        return found


def create(ctx: SourceContext) -> StremioAddonSource:
    return StremioAddonSource(ctx.client, ctx.stream_addons)
