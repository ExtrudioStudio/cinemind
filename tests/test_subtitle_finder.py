"""Finding a subtitle: providers (mocked), candidate order, the sync gate, and saving."""

from __future__ import annotations

import io
import json
import struct
import zipfile
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from cinemind.cli import app as cli
from cinemind.core.config import Config
from cinemind.subtitles.finder import find_for_video, find_subtitle, order
from cinemind.subtitles.providers import OpenSubtitles, SubCandidate, Wizdom, osdb_hash
from cinemind.subtitles.srt import Cue, format_srt, parse

from .test_subtitles import AUDIO, SPEECH, _cues, _speech

WIZ = "https://wizdom.xyz/api"
OS = "https://api.opensubtitles.com/api/v1"
VIDEO_NAME = "Film.2010.1080p.BluRay.x264-SPARKS.mkv"


def _srt(cues: list[Cue]) -> bytes:
    hebrew = [Cue(c.start, c.end, "שלום") for c in cues]
    return format_srt(hebrew).encode("cp1255")  # classic Hebrew subtitle encoding


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


async def _audio():
    return [AUDIO]


@pytest.fixture
def video(tmp_path) -> Path:
    path = tmp_path / VIDEO_NAME
    path.write_bytes(b"\x01" * 200_000)
    return path


async def test_the_first_candidate_that_matches_the_speech_is_saved(video, tmp_path):
    wrong, right, late = (
        _srt(_cues(_speech(99))),
        _srt(_cues(SPEECH)),
        _srt(_cues(SPEECH, offset=7)),
    )
    with respx.mock() as router:
        router.get(f"{WIZ}/search").respond(json=[
            # Ranked first, but another film; its name (score < 50) doesn't make it trusted.
            {"id": 1, "versioname": "Film.2010.1080p.BluRay.x264-OTHER"},
            {"id": 2, "versioname": "Film.2010.720p.BluRay.x264-OTHER"},
            {"id": 3, "versioname": "Film.2010.HDCAM-NOBODY"},  # never downloaded
        ])  # fmt: skip
        router.get(f"{WIZ}/files/sub/1").respond(content=_zip({"a.srt": wrong}))
        router.get(f"{WIZ}/files/sub/2").respond(content=_zip({
            "../../evil.srt": late, "readme.txt": b"x", "Film.720p.srt": right}))  # fmt: skip
        async with httpx.AsyncClient() as http:
            wizdom = Wizdom(http)
            out = tmp_path / "Film.he.srt"
            result = await find_subtitle(video, out, {"wizdom": wizdom},
                                         [lambda: wizdom.search("tt1", None, None, video.name)],
                                         _audio)  # fmt: skip
    assert result.status == "found" and result.source == "wizdom" and result.sync.status == "synced"
    assert result.release == "Film.2010.720p.BluRay.x264-OTHER" and len(result.tried) == 2
    assert not any("HDCAM" in label for label in result.tried)
    saved = out.read_bytes()
    assert saved.startswith(b"\xef\xbb\xbf")  # clean UTF-8 with a BOM, converted from cp1255
    assert parse(saved.decode("utf-8-sig"))[0].text == "שלום"


async def test_a_late_subtitle_is_retimed_when_saved(video, tmp_path):
    late = _srt(_cues(SPEECH, offset=7))
    with respx.mock() as router:
        router.get(f"{WIZ}/search").respond(json=[{"id": 9, "versioname": "x"}])
        router.get(f"{WIZ}/files/sub/9").respond(content=_zip({"x.srt": late}))
        async with httpx.AsyncClient() as http:
            wizdom = Wizdom(http)
            out = tmp_path / "late.he.srt"
            result = await find_subtitle(video, out, {"wizdom": wizdom},
                                         [lambda: wizdom.search("tt1", 1, 2, video.name)],
                                         _audio)  # fmt: skip
    assert result.sync.status == "shifted"
    first = parse(out.read_text(encoding="utf-8-sig"))[0]
    assert first.start == pytest.approx(SPEECH[0][0], abs=0.15)  # back on the speech


async def test_without_an_audio_check_only_the_same_release_is_trusted(video, tmp_path):
    exact = SubCandidate("fake", "1", "Film.2010.1080p.BluRay.x264-SPARKS", score=85)
    other = SubCandidate("fake", "2", "Film.2010.WEBRip-X", score=0)

    class Fake:
        name = "fake"
        fetched: list[str] = []

        def supports(self, language):
            return True

        async def fetch(self, cand, work):
            self.fetched.append(cand.id)
            path = work / f"{cand.id}.srt"
            path.write_bytes(_srt(_cues(SPEECH)))
            return path

    fake = Fake()

    async def search():
        return [other, exact]

    result = await find_subtitle(video, tmp_path / "o.srt", {"fake": fake}, [search], audio=None)
    assert result.status == "found" and result.release == exact.release and result.sync is None
    assert fake.fetched == ["1"]  # the other one wasn't worth a download
    cam = SubCandidate("fake", "3", "Film.2010.HDCAM-X", score=-40)
    none = await find_subtitle(video, tmp_path / "p.srt", {"fake": fake},
                               [lambda: _const([other, cam])], audio=None)  # fmt: skip
    assert none.status == "none" and "none matched" in none.reason
    assert any("skipped (cam" in line for line in none.log) and fake.fetched == ["1"]


async def _const(value):
    return value


def test_candidates_are_ordered_hash_then_score_then_wizdom():
    a = SubCandidate("opensubtitles", "1", "a", score=10, downloads=500)
    b = SubCandidate("wizdom", "2", "b", score=10)
    c = SubCandidate("opensubtitles", "3", "c", score=0, hash_match=True)
    d = SubCandidate("opensubtitles", "4", "d", score=60)
    assert [x.id for x in order([a, b, c, d])] == ["3", "4", "2", "1"]


async def test_opensubtitles_search_login_download_and_cache(video, tmp_path):
    srt = _srt(_cues(SPEECH))
    with respx.mock() as router:
        search = router.get(f"{OS}/subtitles").respond(json={"data": [
            {"attributes": {"release": "Film.2010.1080p.BluRay.x264-SPARKS",
                            "moviehash_match": True, "download_count": 50,
                            "files": [{"file_id": 77}]}},
            {"attributes": {"release": "machine", "machine_translated": True,
                            "files": [{"file_id": 78}]}},
        ]})  # fmt: skip
        login = router.post(f"{OS}/login").respond(json={"token": "tok", "base_url": "vip.example"})
        download = router.post("https://vip.example/api/v1/download").respond(
            json={"link": "https://dl.example/77.srt"})  # fmt: skip
        router.get("https://dl.example/77.srt").respond(content=srt)
        async with httpx.AsyncClient() as http:
            osub = OpenSubtitles(http, "KEY", "me", "pw", tmp_path / "cache")
            (cand,) = await osub.search("tt0001234", None, None, video, "he")
            assert cand.hash_match and cand.score == 85 and cand.id == "77"
            first = await osub.fetch(cand, tmp_path)
            second = await osub.fetch(cand, tmp_path)  # from the cache: no second download
    query = dict(search.calls[0].request.url.params)
    assert query == {"imdb_id": "1234", "languages": "he", "moviehash": osdb_hash(video)}
    assert list(search.calls[0].request.url.params.keys()) == sorted(query)
    assert search.calls[0].request.headers["Api-Key"] == "KEY"
    assert json.loads(login.calls[0].request.content) == {"username": "me", "password": "pw"}
    assert download.call_count == 1
    assert download.calls[0].request.headers["Authorization"] == "Bearer tok"
    assert first.read_bytes() == second.read_bytes() == srt


async def test_opensubtitles_errors_carry_the_sites_message(tmp_path):
    with respx.mock() as router:
        router.post(f"{OS}/login").respond(401, json={"message": "invalid username/password"})
        async with httpx.AsyncClient() as http:
            osub = OpenSubtitles(http, "KEY", "me", "bad", tmp_path)
            with pytest.raises(Exception, match="HTTP 401: invalid username/password"):
                await osub.fetch(SubCandidate("opensubtitles", "1", "x"), tmp_path)


def test_osdb_hash_is_the_size_plus_both_ends(tmp_path):
    small = tmp_path / "small.bin"
    small.write_bytes(struct.pack("<2Q", 1, 2))  # 16 bytes: both "ends" are the whole file
    assert osdb_hash(small) == f"{16 + 2 * (1 + 2):016x}"


async def test_no_provider_for_the_language_says_how_to_add_one(video):
    result = await find_for_video(Config(), video, "tt1", language="fr", sync_check=False)
    assert result.status == "none" and "opensubtitles_api_key" in result.reason


def test_the_command_checks_the_video_exists(tmp_path):
    result = CliRunner().invoke(cli.app, ["subtitles", str(tmp_path / "nope.mkv"), "--imdb", "tt1"])
    assert result.exit_code == 2
