"""Tell Plex a file has arrived: refresh the library section whose folder holds it.

A port of MediaPipeline v1's ``Invoke-PlexPartialScan``. It refreshes the whole section rather
than one folder. On QNAP, ``/share/<Share>`` is a symlink: Plex answered 200 to
``?path=<folder>`` but never scanned it, while a section refresh picked the file up in about
15 s (verified 2026-11-06). Plex only rescans folders that changed, so this stays cheap.

The token goes in the ``X-Plex-Token`` header, never in the URL, so it can't end up in logs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, Literal

import httpx

log = logging.getLogger(__name__)


@dataclass
class PlexResult:
    status: Literal["ok", "skipped", "unauthorized", "error"]
    section: str | None = None
    path: str | None = None
    detail: str | None = None


class Plex:
    def __init__(self, url: str, token: str | None, http: httpx.AsyncClient | None = None):
        self.url = url.rstrip("/")
        self.token = token
        self._http = http

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "X-Plex-Product": "cinemind",
                   "X-Plex-Client-Identifier": "cinemind"}  # fmt: skip
        if self.token:
            headers["X-Plex-Token"] = self.token
        return headers

    async def _get(self, path: str) -> Any:
        client = self._http or httpx.AsyncClient(timeout=15.0)
        try:
            resp = await client.get(self.url + path, headers=self._headers())
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        finally:
            if self._http is None:
                await client.aclose()

    async def sections(self) -> list[dict[str, Any]]:
        data = await self._get("/library/sections")
        dirs = (data.get("MediaContainer") or {}).get("Directory") or []
        return [d for d in dirs if isinstance(d, dict)]

    async def refresh(self, library_dir: str, scan_dir: PurePath) -> PlexResult:
        """Refresh the section with a folder ending in ``/<library_dir>``."""
        pattern = re.compile(r"[/\\]" + re.escape(library_dir) + r"[/\\]?$", re.IGNORECASE)
        try:
            for section in await self.sections():
                for location in section.get("Location") or []:
                    path = str(location.get("path") or "")
                    if not pattern.search(path):
                        continue
                    relative = PurePath(*scan_dir.parts[1:]).as_posix()  # without "Movies/"
                    await self._get(f"/library/sections/{section.get('key')}/refresh")
                    return PlexResult("ok", str(section.get("title")),
                                      f"{path.rstrip('/')}/{relative}")  # fmt: skip
            detail = f"no Plex library has a folder ending in '/{library_dir}'"
            return PlexResult("skipped", detail=detail)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                return PlexResult("unauthorized", detail="Plex refused the request; store the "
                                  "token with `cinemind secrets set plex`")  # fmt: skip
            return PlexResult("error", detail=f"HTTP {exc.response.status_code} from Plex")
        except httpx.HTTPError as exc:
            return PlexResult("error", detail=f"Could not reach Plex: {exc}")
