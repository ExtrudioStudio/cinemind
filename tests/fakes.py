"""Test doubles shared by the AI, service and MCP tests."""

from __future__ import annotations

import asyncio
from typing import Any

from cinemind.core.models import Candidate, EpisodeRef, Title


class FakeLLM:
    """Answers LLM.parse with canned readings, and remembers what it was asked."""

    def __init__(self, *answers: Any):
        self.answers = list(answers)
        self.calls: list[tuple[str, str]] = []

    async def parse(self, purpose, system, user, output, max_tokens=2048):
        self.calls.append((purpose, user))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return output.model_validate(answer) if isinstance(answer, dict) else answer


class FixedSource:
    """A stream source offering one direct 1080p file per episode (or for the movie)."""

    name = "fixed"

    def __init__(self, size: int = 400_000_000):
        self.size = size
        self.asked: list[EpisodeRef | None] = []

    def supports(self, title: Title) -> bool:
        return True

    async def candidates(self, title: Title, ref: EpisodeRef | None) -> list[Candidate]:
        self.asked.append(ref)
        code = ref.code if ref else "movie"
        url = f"https://files.example/{title.id}/{code}.mkv"
        return [Candidate(Candidate.id_for_url(url), None, "1080p", 10, self.size,
                          source="example", filename=f"{title.name}.{code}.mkv",
                          release=f"{title.name} {code}", url=url, md5="0" * 32)]  # fmt: skip


class FakeRunner:
    """Stands in for DownloadRunner: 'downloads' until released or cancelled."""

    instances: list[FakeRunner] = []

    def __init__(self, cfg, store, job_id, plan):
        self.store, self.job_id = store, job_id
        self.release = asyncio.Event()
        self.fail: Exception | None = None
        self.views: list = []
        FakeRunner.instances.append(self)

    async def run(self, torrents, display=True):
        assert display is False  # headless in the background
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.store.pause_open(self.job_id)
            self.store.finish_job(self.job_id)
            raise
        if self.fail:
            raise self.fail
        for job, _ in torrents:
            self.store.update_torrent(self.job_id, job.info_hash, status="done")
            for key in job.keys:
                self.store.set_item(self.job_id, key, "done")
        return self.store.finish_job(self.job_id)
