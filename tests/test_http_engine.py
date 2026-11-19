"""The direct-download engine against a local HTTP server that can resume, drop and stall."""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from cinemind.core.config import Config
from cinemind.core.models import Candidate, FileTarget, TorrentJob
from cinemind.engines.http import HttpEngine
from cinemind.engines.multi import Engines, TorrentSupportMissing

BODY = os.urandom(300_000)


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, *args):  # keep test output clean
        pass

    def do_GET(self):  # noqa: N802 (http.server naming)
        srv = self.server
        srv.requests.append((self.path, self.headers.get("Range")))
        body = srv.files.get(self.path)
        if body is None:
            self.send_error(404)
            return
        start = 0
        wanted = self.headers.get("Range")
        if wanted and self.path not in srv.ignore_range:
            start = int(wanted.split("=")[1].split("-")[0])
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
        else:
            self.send_response(200)
        payload = body[start:]
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.path in srv.stall:
            time.sleep(srv.stall[self.path])
        cut = srv.drop_after.pop(self.path, None)
        try:
            self.wfile.write(payload if cut is None else payload[:cut])
        except OSError:
            pass  # the client gave up (stall test)
        if cut is not None:
            self.close_connection = True


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.files: dict[str, bytes] = {}
        self.requests: list[tuple[str, str | None]] = []
        self.ignore_range: set[str] = set()
        self.drop_after: dict[str, int] = {}
        self.stall: dict[str, float] = {}

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}{path}"


@pytest.fixture
def server():
    srv = _Server()
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


class Recorder:
    def __init__(self, replacements=None):
        self.done, self.jobs_done, self.dead = [], [], []
        self.replacements = list(replacements or [])

    def on_target_done(self, job, target):
        self.done.append(target.final_path)

    def on_job_done(self, job):
        self.jobs_done.append(job.info_hash)

    def on_job_dead(self, job, reason, remaining):
        self.dead.append(reason)
        out, self.replacements = self.replacements, []
        return out


def _job(server, library: Path, path: str = "/film.mp4", body: bytes = BODY,
         md5: str | None = None, name: str = "Film (1968).mp4") -> TorrentJob:  # fmt: skip
    server.files.setdefault(path, body)
    url = server.url(path)
    digest = hashlib.md5(body, usedforsecurity=False).hexdigest() if md5 is None else md5
    cand = Candidate(Candidate.id_for_url(url), None, "1080p", 1, len(body), release="Film",
                     filename=path.rsplit("/", 1)[1], url=url, md5=digest)  # fmt: skip
    target = FileTarget(None, (), library / "Film (1968)" / name, len(body), cand)
    return TorrentJob(cand.info_hash, "Film", (), [target])


def _cfg(**kwargs) -> Config:
    return Config(**{"stall_timeout": 30, **kwargs})


async def _drive(engine, timeout: float = 30):
    if isinstance(engine, Engines):
        await asyncio.wait_for(engine.run(), timeout)
        return

    async def loop():
        while not engine.idle:
            engine.step()
            await asyncio.sleep(0.02)

    try:
        await asyncio.wait_for(loop(), timeout)
    finally:
        await engine.aclose()


async def test_download_is_verified_and_renamed_into_place(tmp_path, server):
    rec = Recorder()
    engines = Engines(_cfg(), tmp_path / "staging", rec, poll_interval=0.02)
    job = _job(server, tmp_path / "lib")
    engines.add_job(job)
    await _drive(engines)

    final = job.targets[0].final_path
    assert final.read_bytes() == BODY and not final.with_name(final.name + ".part").exists()
    assert rec.done == [final] and rec.jobs_done == [job.info_hash] and rec.dead == []
    (view,) = engines.snapshot()
    assert view.state == "done" and view.targets[0].done == len(BODY)
    assert engines._torrent is None  # libtorrent never loaded for direct downloads


async def test_an_interrupted_download_resumes_with_range(tmp_path, server):
    job = _job(server, tmp_path / "lib")
    final = job.targets[0].final_path
    part = final.with_name(final.name + ".part")
    part.parent.mkdir(parents=True)
    part.write_bytes(BODY[:100_000])
    engine = HttpEngine(_cfg(), Recorder(), retry_wait=0)
    engine.add_job(job)
    await _drive(engine)
    assert server.requests == [("/film.mp4", "bytes=100000-")]
    assert final.read_bytes() == BODY


async def test_a_server_that_ignores_range_starts_over(tmp_path, server):
    job = _job(server, tmp_path / "lib")
    server.ignore_range.add("/film.mp4")
    final = job.targets[0].final_path
    final.parent.mkdir(parents=True)
    final.with_name(final.name + ".part").write_bytes(b"stale bytes")
    engine = HttpEngine(_cfg(), Recorder(), retry_wait=0)
    engine.add_job(job)
    await _drive(engine)
    assert final.read_bytes() == BODY  # not appended to the stale part


async def test_a_dropped_connection_is_retried_from_where_it_stopped(tmp_path, server):
    job = _job(server, tmp_path / "lib")
    server.drop_after["/film.mp4"] = 50_000
    engine = HttpEngine(_cfg(), Recorder(), retry_wait=0)
    engine.add_job(job)
    await _drive(engine)
    assert [r for _, r in server.requests] == [None, "bytes=50000-"]
    assert job.targets[0].final_path.read_bytes() == BODY


async def test_checksum_mismatch_fails_and_the_next_candidate_is_used(tmp_path, server):
    good = _job(server, tmp_path / "lib", path="/mirror.mp4")
    bad = _job(server, tmp_path / "lib", path="/film.mp4", md5="0" * 32)
    rec = Recorder(replacements=[good])
    engines = Engines(_cfg(), tmp_path / "staging", rec, poll_interval=0.02)
    engines.add_job(bad)
    await _drive(engines)
    assert len(rec.dead) == 1 and "MD5 mismatch" in rec.dead[0]
    final = bad.targets[0].final_path
    assert not final.with_name(final.name + ".part").exists()  # bad bytes removed
    assert final.read_bytes() == BODY  # from the mirror
    assert rec.jobs_done == [good.info_hash]


async def test_http_errors_and_stalls_give_up_with_a_reason(tmp_path, server):
    missing = _job(server, tmp_path / "lib", path="/gone.mp4", name="Gone.mp4")
    del server.files["/gone.mp4"]
    slow = _job(server, tmp_path / "lib", path="/slow.mp4", name="Slow.mp4")
    server.stall["/slow.mp4"] = 5
    rec = Recorder()
    engine = HttpEngine(_cfg(stall_timeout=1), rec, retry_wait=0)
    engine.add_job(missing)
    engine.add_job(slow)
    await _drive(engine)
    assert sorted(rec.dead) == ["HTTP 404 from " + server.url("/gone.mp4"), "no progress for 1s"]
    assert {v.state for v in engine.snapshot()} == {"dead"}


async def test_a_file_already_in_place_is_not_downloaded_again(tmp_path, server):
    job = _job(server, tmp_path / "lib")
    final = job.targets[0].final_path
    final.parent.mkdir(parents=True)
    final.write_bytes(BODY)
    rec = Recorder()
    engine = HttpEngine(_cfg(), rec)
    engine.add_job(job)
    engine.add_job(job)  # queued once
    await _drive(engine)
    assert server.requests == [] and rec.jobs_done == [job.info_hash]


def test_a_torrent_without_libtorrent_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "cinemind.engines.torrent", None)  # import now fails
    engines = Engines(_cfg(), tmp_path / "staging")
    cand = Candidate("ab" * 20, 0, "1080p", 5, 10, filename="x.mkv")
    job = TorrentJob(cand.info_hash, "x", (), [FileTarget(0, (), tmp_path / "x.mkv", 10, cand)])
    with pytest.raises(TorrentSupportMissing, match="cinemind\\[torrent\\]"):
        engines.add_job(job)
