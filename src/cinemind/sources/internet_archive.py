"""Public-domain films from the Internet Archive, as direct HTTP downloads.

For a movie, the source searches archive.org's ``movies`` media type by title and year. It keeps
only items whose ``licenseurl`` marks them as public domain (the Public Domain Mark or CC0),
and offers their video files with the size and MD5 the archive publishes. Series aren't
supported: the archive has few, and no episode numbering to match on.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlencode

from cinemind.core.aio import reraise_cancelled
from cinemind.core.models import Candidate, EpisodeRef, Title
from cinemind.planning.streams import detect_resolution, resolution_from_height
from cinemind.sources.base import SourceContext
from cinemind.sources.stremio.client import AddonClient

log = logging.getLogger(__name__)

SEARCH_URL = "https://archive.org/advancedsearch.php"
METADATA_URL = "https://archive.org/metadata/{identifier}"
DOWNLOAD_URL = "https://archive.org/download/{identifier}/{name}"
TTL_SEARCH = 6 * 3600
TTL_METADATA = 24 * 3600
MAX_ITEMS = 3  # look inside the most-downloaded matching items only
SEARCH_ROWS = 10
# Containers worth offering. MPEG-2 (.mpg), Ogg and .m2ts originals exist too, but are poorly
# supported by TVs or huge; the archive's H.264 copies cover the same films.
EXTENSIONS = {".mp4", ".m4v", ".mkv", ".webm", ".mov"}
_PUBLIC_DOMAIN = re.compile(
    r"creativecommons\.org/(?:publicdomain/(?:mark|zero)|licenses/publicdomain)", re.I
)
_LANGUAGES = {"english": "en", "eng": "en", "en": "en", "french": "fr", "fre": "fr",
              "fra": "fr", "german": "de", "ger": "de", "deu": "de", "spanish": "es",
              "spa": "es", "italian": "it", "ita": "it", "japanese": "ja", "jpn": "ja",
              "russian": "ru", "rus": "ru"}  # fmt: skip


def is_public_domain(license_url: Any) -> bool:
    return isinstance(license_url, str) and bool(_PUBLIC_DOMAIN.search(license_url))


def _words(text: str) -> list[str]:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.findall(r"[a-z0-9]+", text.lower().replace("'", ""))


def title_matches(item: dict[str, Any], title: Title) -> bool:
    """Whether an archive item is this film: an IMDb id match, or the same leading words.

    Comparing whole words keeps "It" from matching "It's a Wonderful Life", while still
    accepting the archive's extended titles ("Night of the Living Dead iPod and Flash video
    versions").
    """
    ids = item.get("external-identifier") or []
    ids = [ids] if isinstance(ids, str) else ids
    if title.id.startswith("tt") and f"urn:imdb:{title.id}" in ids:
        return True
    want, have = _words(title.name), _words(str(item.get("title") or ""))
    if not want or have[: len(want)] != want:
        return False
    year = str(item.get("year") or "")[:4]
    return not (title.year and year.isdigit() and int(year) != title.year)


def _languages(value: Any) -> tuple[str, ...]:
    values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    codes = [_LANGUAGES.get(str(v).strip().lower()) for v in values]
    return tuple(dict.fromkeys(c for c in codes if c))


def _int(value: Any) -> int:
    try:
        return int(float(str(value)))
    except ValueError:
        return 0


class InternetArchiveSource:
    name = "internet-archive"

    def __init__(self, client: AddonClient):
        self.client = client

    def supports(self, title: Title) -> bool:
        return title.type == "movie"

    async def candidates(self, title: Title, ref: EpisodeRef | None) -> list[Candidate]:
        items = (await self.search(title))[:MAX_ITEMS]
        results = await asyncio.gather(
            *(self.metadata(item["identifier"]) for item in items), return_exceptions=True
        )
        found: list[Candidate] = []
        for item, raw in zip(items, results, strict=True):
            meta = reraise_cancelled(raw)
            if isinstance(meta, Exception):
                log.warning("Internet Archive item %s failed: %s", item["identifier"], meta)
                continue
            found.extend(self.files(item, meta))
        return found

    async def search(self, title: Title) -> list[dict[str, Any]]:
        """Matching public-domain items, most downloaded first."""
        name = re.sub(r'["\\]', " ", title.name)  # a quote would end the phrase query
        clauses = [f'title:("{name}")', "mediatype:movies"]
        if title.year:
            clauses.append(f"year:{title.year}")
        params = {
            "q": " AND ".join(clauses),
            "fl[]": ["identifier", "title", "year", "downloads", "licenseurl",
                     "external-identifier", "language"],
            "sort[]": "downloads desc",
            "rows": str(SEARCH_ROWS),
            "output": "json",
        }  # fmt: skip
        data = await self.client.get_json(f"{SEARCH_URL}?{urlencode(params, doseq=True)}",
                                          TTL_SEARCH)  # fmt: skip
        response = data.get("response")
        docs = response.get("docs") if isinstance(response, dict) else None
        if not isinstance(docs, list):
            return []
        return [
            d for d in docs
            if isinstance(d, dict) and isinstance(d.get("identifier"), str)
            and is_public_domain(d.get("licenseurl")) and title_matches(d, title)
        ]  # fmt: skip

    async def metadata(self, identifier: str) -> dict[str, Any]:
        url = METADATA_URL.format(identifier=quote(identifier, safe=""))
        return await self.client.get_json(url, TTL_METADATA)

    def files(self, item: dict[str, Any], meta: dict[str, Any]) -> list[Candidate]:
        """The item's video files as candidates."""
        identifier = item["identifier"]
        files = meta.get("files")
        info = meta.get("metadata")
        info = info if isinstance(info, dict) else {}
        languages = _languages(info.get("language") or item.get("language"))
        out: list[Candidate] = []
        for f in files if isinstance(files, list) else []:
            name = f.get("name") if isinstance(f, dict) else None
            if not isinstance(name, str) or PurePosixPath(name).suffix.lower() not in EXTENSIONS:
                continue
            height = _int(f.get("height"))
            url = DOWNLOAD_URL.format(identifier=quote(identifier, safe=""),
                                      name=quote(name, safe="/"))  # fmt: skip
            release = str(item.get("title") or identifier)
            out.append(
                Candidate(
                    info_hash=Candidate.id_for_url(url),
                    file_idx=None,
                    resolution=(
                        resolution_from_height(height)
                        if height
                        else detect_resolution("", name, release)
                    ),  # fmt: skip
                    seeders=_int(item.get("downloads")),
                    size=_int(f.get("size")),
                    source="archive.org",
                    languages=languages,
                    filename=PurePosixPath(name).name,
                    release=release,
                    label=str(f.get("format") or ""),
                    addon=self.name,
                    url=url,
                    md5=str(f.get("md5") or "").lower(),
                )
            )
        return out


def create(ctx: SourceContext) -> InternetArchiveSource:
    return InternetArchiveSource(ctx.client)
