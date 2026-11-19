"""Where subtitles come from: Wizdom (Hebrew, no account) and OpenSubtitles (any language).

Ported from MediaPipeline v1. Downloads are untrusted: zip archives are opened in memory, only
subtitle entries are read, their names are sanitised, and nothing is executed. OpenSubtitles
free accounts get about 20 downloads a day, so every downloaded file is cached by its file id
and re-runs cost nothing.
"""

from __future__ import annotations

import io
import re
import struct
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from cinemind.subtitles import release

SUBTITLE_EXTS = (".srt", ".ass", ".ssa", ".vtt")
MAX_SUBTITLE_BYTES = 20 * 1024 * 1024


class ProviderError(Exception):
    pass


@dataclass
class SubCandidate:
    source: str
    id: str
    release: str
    score: int = 0
    hash_match: bool = False
    downloads: int = 0


def osdb_hash(path: Path) -> str:
    """OpenSubtitles' "moviehash": the size plus the 64-bit little-endian sums of the first and
    last 64 KiB, modulo 2**64."""
    size = path.stat().st_size
    total = size
    with path.open("rb") as fh:
        for start in (0, max(0, size - 65536)):
            fh.seek(start)
            chunk = fh.read(min(65536, size))
            words = len(chunk) // 8
            total += sum(struct.unpack(f"<{words}Q", chunk[: words * 8]))
    return f"{total & 0xFFFFFFFFFFFFFFFF:016x}"


def _safe(name: str) -> str:
    return re.sub(r"[^\w.\-]", "_", Path(name.replace("\\", "/")).name)  # no paths from archives


class Wizdom:
    """wizdom.xyz: Israeli, Hebrew only, no account and no download limit."""

    name = "wizdom"
    base = "https://wizdom.xyz/api"

    def __init__(self, http: httpx.AsyncClient):
        self.http = http

    def supports(self, language: str) -> bool:
        return language.lower() in ("he", "heb")

    async def search(self, imdb: str, season: int | None, episode: int | None,
                     video_name: str) -> list[SubCandidate]:  # fmt: skip
        params: dict[str, Any] = {"action": "by_id", "imdb": imdb}
        if episode:
            params |= {"season": season or 0, "episode": episode}
        resp = await self.http.get(f"{self.base}/search", params=params)
        resp.raise_for_status()
        items = resp.json() if resp.content else []
        out = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            name = str(item.get("versioname") or "")
            fit = release.score(video_name, name) if video_name else 0
            out.append(SubCandidate(self.name, str(item["id"]), name, fit))
        return out

    async def fetch(self, cand: SubCandidate, work_dir: Path) -> Path:
        resp = await self.http.get(f"{self.base}/files/sub/{cand.id}")
        resp.raise_for_status()
        try:
            archive = zipfile.ZipFile(io.BytesIO(resp.content))
        except zipfile.BadZipFile as exc:
            raise ProviderError(f"Wizdom file {cand.id} is not a zip archive") from exc
        with archive:
            entries = [e for e in archive.infolist()
                       if e.filename.lower().endswith(SUBTITLE_EXTS)
                       and 0 < e.file_size < MAX_SUBTITLE_BYTES]  # fmt: skip
            if not entries:
                raise ProviderError(f"Wizdom archive {cand.id} has no subtitle file")
            pick = max(entries, key=lambda e: release.score(cand.release, e.filename))
            out = work_dir / f"wizdom-{cand.id}-{_safe(pick.filename)}"
            out.write_bytes(archive.read(pick))
        return out


class OpenSubtitles:
    """api.opensubtitles.com (REST v1): needs an API key, and an account for downloads."""

    name = "opensubtitles"
    base = "https://api.opensubtitles.com/api/v1"

    def __init__(self, http: httpx.AsyncClient, api_key: str, username: str, password: str,
                 cache_dir: Path, user_agent: str = "cinemind"):  # fmt: skip
        self.http = http
        self.api_key, self.username, self.password = api_key.strip(), username.strip(), password
        self.cache_dir = cache_dir
        self.user_agent = user_agent
        self._token: tuple[str, str, float] | None = None  # token, host, expires

    def supports(self, language: str) -> bool:
        return True

    def _headers(self) -> dict[str, str]:
        return {"Api-Key": self.api_key, "User-Agent": self.user_agent,
                "Accept": "application/json"}  # fmt: skip

    async def _call(self, method: str, url: str, **kwargs: Any) -> Any:
        resp = await self.http.request(method, url, headers=kwargs.pop("headers", self._headers()),
                                       **kwargs)  # fmt: skip
        if resp.status_code >= 400:
            try:
                message = str(resp.json().get("message") or resp.text)
            except ValueError:
                message = resp.text
            endpoint = re.sub(r"^https://[^/]+/api/v1|\?.*$", "", url)
            raise ProviderError(f"OpenSubtitles {endpoint} HTTP {resp.status_code}: "
                                f"{message.strip()}")  # fmt: skip
        return resp.json()

    async def _login(self) -> tuple[str, str]:
        if self._token and self._token[2] > time.monotonic():
            return self._token[0], self._token[1]
        login = {"username": self.username, "password": self.password}
        data = await self._call("POST", f"{self.base}/login", json=login)
        token, host = str(data.get("token") or ""), str(data.get("base_url") or "")
        self._token = (token, host or "api.opensubtitles.com", time.monotonic() + 12 * 3600)
        return self._token[0], self._token[1]

    async def search(self, imdb: str, season: int | None, episode: int | None,
                     video: Path | None, language: str) -> list[SubCandidate]:  # fmt: skip
        number = int(imdb.removeprefix("tt"))
        query: dict[str, Any] = {"languages": language}
        if episode:
            query |= {"parent_imdb_id": number, "season_number": season or 0,
                      "episode_number": episode}  # fmt: skip
        else:
            query["imdb_id"] = number
        if video is not None and video.is_file():
            query["moviehash"] = osdb_hash(video)
        # The API asks for sorted, lower-case parameters (it redirects otherwise).
        data = await self._call("GET", f"{self.base}/subtitles", params=sorted(query.items()))
        name = video.name if video else ""
        out = []
        for item in data.get("data") or []:
            attrs = item.get("attributes") or {}
            if attrs.get("ai_translated") or attrs.get("machine_translated"):
                continue
            files = attrs.get("files") or []
            if not files:
                continue
            rel = str(attrs.get("release") or "")
            out.append(SubCandidate(self.name, str(files[0].get("file_id")), rel,
                                    release.score(name, rel) if name else 0,
                                    bool(attrs.get("moviehash_match")),
                                    int(attrs.get("download_count") or 0)))  # fmt: skip
        return out

    async def fetch(self, cand: SubCandidate, work_dir: Path) -> Path:
        out = work_dir / f"os-{cand.id}.srt"
        cached = self.cache_dir / f"os-{cand.id}.srt"
        if cached.is_file() and cached.stat().st_size > 0:
            out.write_bytes(cached.read_bytes())
            return out
        token, host = await self._login()
        headers = {**self._headers(), "Authorization": f"Bearer {token}"}
        data = await self._call("POST", f"https://{host}/api/v1/download", headers=headers,
                                json={"file_id": int(cand.id)})  # fmt: skip
        link = data.get("link")
        if not link:
            raise ProviderError("OpenSubtitles returned no download link "
                                f"(remaining today: {data.get('remaining')})")  # fmt: skip
        resp = await self.http.get(str(link), headers={"User-Agent": self.user_agent})
        resp.raise_for_status()
        out.write_bytes(resp.content)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(resp.content)
        return out
