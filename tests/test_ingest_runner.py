"""Ingest: naming, the place / fix / quarantine paths, Plex refreshes, and a real-FFmpeg run."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path, PurePath

import httpx
import pytest
import respx

from cinemind.core.config import Config, log_dir
from cinemind.ingest.naming import plex_safe_name, plex_target
from cinemind.ingest.plex import Plex
from cinemind.ingest.probe import Completed, probe
from cinemind.ingest.runner import Ingester, Media, logical_name

from .conftest import FIXTURES

GOLDEN = FIXTURES / "ingest" / "golden"
NOTLD = Media("movie", "Night of the Living Dead", year=1968, imdb="tt0063350")


def _probe(name: str) -> dict:
    return json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8-sig"))["probe"]


COMPLIANT = _probe("syn-compliant-h264-eac3-srt")
FIXABLE = _probe("syn-fix-audio-dts-only")
REJECT = _probe("syn-reject-dv-profile5")


# -- naming ---------------------------------------------------------------------------------------


def test_movie_and_episode_names():
    movie = plex_target("movie", "Spider-Man: Brand New Day", year=2026, imdb="tt22084616")
    folder, name = "Spider-Man - Brand New Day (2026) {imdb-tt22084616}", "Spider-Man - Brand"
    assert movie.relative_path == PurePath("Movies", folder, f"{name} New Day (2026).mkv")
    ep = plex_target("episode", "One Piece", year=1999, tvdb="81797", imdb="tt0388629",
                     season=1, episode=1, episode_title="I'm Luffy!", extension=".mkv")  # fmt: skip
    assert ep.relative_path == PurePath("TV Shows/One Piece (1999) {tvdb-81797}/Season 01/"
                                        "One Piece (1999) - S01E01 - I'm Luffy!.mkv")  # fmt: skip
    assert ep.scan_dir == PurePath("TV Shows/One Piece (1999) {tvdb-81797}")
    double = plex_target("episode", "Show", season=0, episode=3, end_episode=4)
    assert double.relative_path.name == "Show - S00E03-E04.mkv"  # specials and two-parters


def test_ids_are_checked_and_ordered_by_kind():
    assert "{imdb-tt1}" in str(plex_target("movie", "M", imdb="tt1", tmdb="2", tvdb="3").scan_dir)
    assert "{tvdb-3}" in str(plex_target("episode", "S", imdb="tt1", tvdb="3", season=1,
                                         episode=1).scan_dir)  # fmt: skip
    assert plex_target("movie", "M", imdb="TT9").scan_dir.name == "M {imdb-TT9}"  # as v1
    for bad in ({"imdb": "12345"}, {"tmdb": "abc"}):
        with pytest.raises(ValueError):
            plex_target("movie", "M", **bad)
    with pytest.raises(ValueError, match="season"):
        plex_target("episode", "S", season=1)
    with pytest.raises(ValueError, match="empty"):
        plex_target("movie", '???"')
    assert plex_safe_name(' Who? / What: "Why" . ') == "Who What - Why"


def test_an_existing_show_folder_is_reused(tmp_path):
    (tmp_path / "TV Shows" / "One Piece (1999) {tvdb-81797}").mkdir(parents=True)
    t = plex_target("episode", "One Piece", year=1999, imdb="tt0388629", season=2, episode=1,
                    library_root=tmp_path)  # fmt: skip
    assert t.scan_dir.name == "One Piece (1999) {tvdb-81797}"  # not a second "{imdb-…}" folder
    (tmp_path / "Movies" / "Plain (2001)").mkdir(parents=True)
    assert plex_target("movie", "Plain", year=2001, imdb="tt5",
                       library_root=tmp_path).scan_dir.name == "Plain (2001)"  # fmt: skip


# -- the runner ---------------------------------------------------------------------------------


class FakeTools:
    """ffprobe answers from a table; ffmpeg writes its output file (or fails)."""

    def __init__(
        self, probes: dict[str, dict], ffmpeg_rc: int = 0, output_probe: dict | None = None
    ):
        self.probes, self.ffmpeg_rc = probes, ffmpeg_rc
        self.output_probe = output_probe if output_probe is not None else copy.deepcopy(COMPLIANT)
        self.ffmpeg_calls: list[tuple[str, ...]] = []

    async def probe(self, path: Path) -> dict:
        if path.name.endswith(".part") and ".fixing" in path.name:
            return self.output_probe
        return self.probes[path.name]

    async def run(self, *args: str) -> Completed:
        self.ffmpeg_calls.append(args)
        if self.ffmpeg_rc == 0:
            Path(args[-1]).write_bytes(b"fixed")
            return Completed(0, b"", b"")
        return Completed(self.ffmpeg_rc, b"", b"Invalid data found when processing input")


@pytest.fixture
def share(tmp_path):
    root = tmp_path / "share"
    (root / "_incoming").mkdir(parents=True)
    return root


def _ingester(share: Path, tools: FakeTools, plex: Plex | None = None) -> Ingester:
    cfg = Config(ingest_root=str(share), file_unlock_timeout=1)
    return Ingester(cfg, plex=plex, probe_fn=tools.probe, run_fn=tools.run)


def _drop(share: Path, name: str, data: bytes = b"video") -> Path:
    path = share / "_incoming" / name
    path.write_bytes(data)
    return path


async def test_a_compliant_movie_is_placed_with_its_subtitles(share):
    source = _drop(share, "job1.mkv.part")
    (share / "_incoming" / "job1.en.srt").write_text("1\n", encoding="utf-8")
    result = await _ingester(share, FakeTools({"job1.mkv.part": COMPLIANT})).ingest(
        "job1.mkv.part", NOTLD
    )

    final = share / "Movies" / "Night of the Living Dead (1968) {imdb-tt0063350}" / \
        "Night of the Living Dead (1968).mkv"  # fmt: skip
    assert (result.status, result.exit_code, result.target) == ("placed", 0, str(final))
    assert final.read_bytes() == b"video" and not source.exists()
    assert result.sidecars == [str(final.with_name("Night of the Living Dead (1968).en.srt"))]
    entry = json.loads((log_dir() / "ingest").glob("ingest-*.log").__next__().read_text(
        encoding="utf-8").splitlines()[-1])  # fmt: skip
    assert entry["status"] == "placed" and entry["exitCode"] == 0


async def test_a_fixable_file_is_remuxed_checked_and_placed(share):
    source = _drop(share, "job2.mkv")
    tools = FakeTools({"job2.mkv": FIXABLE})
    result = await _ingester(share, tools).ingest(source, NOTLD)
    assert (result.status, result.exit_code) == ("fixed", 10)
    assert Path(result.target).read_bytes() == b"fixed" and not source.exists()
    (args,) = tools.ffmpeg_calls
    assert args[args.index("-c:a:0") + 1] == "eac3" and "-c:v" not in args  # video copied
    assert not list((share / "_incoming").glob("*.part"))  # the temporary output was moved in


async def test_keep_source_keeps_the_original_after_a_fix(share):
    source = _drop(share, "job3.mkv")
    result = await _ingester(share, FakeTools({"job3.mkv": FIXABLE})).ingest(
        source, NOTLD, keep_source=True)  # fmt: skip
    assert result.status == "fixed" and source.exists()


async def test_a_failed_fix_quarantines_the_file_with_the_reason(share):
    source = _drop(share, "job4.mkv")
    result = await _ingester(share, FakeTools({"job4.mkv": FIXABLE}, ffmpeg_rc=1)).ingest(
        source, NOTLD
    )
    assert (result.status, result.exit_code, result.error) == ("quarantined", 20, "ffmpeg failed")
    reason = Path(result.target + ".reason.txt").read_text(encoding="utf-8")
    assert "ffmpeg failed: Invalid data" in reason and "intended:" in reason


@pytest.mark.parametrize(
    ("output", "message"),
    [(REJECT, "still not compliant"),
     ({**COMPLIANT, "format": {**COMPLIANT["format"], "duration": "100.0"}}, "lasts 100s")],
)  # fmt: skip
async def test_a_bad_fix_never_replaces_anything(share, output, message):
    source = _drop(share, "job5.mkv")
    tools = FakeTools({"job5.mkv": FIXABLE}, output_probe=output)
    result = await _ingester(share, tools).ingest(source, NOTLD)
    assert result.status == "error" and message in result.error
    assert source.exists() and not Path(result.target).exists()
    assert not list((share / "_incoming").glob("*.fixing*"))


async def test_a_rejected_file_is_quarantined(share):
    source = _drop(share, "job6.mkv")
    result = await _ingester(share, FakeTools({"job6.mkv": REJECT})).ingest(source, NOTLD)
    assert (result.status, result.exit_code) == ("quarantined", 20)
    assert Path(result.target).parent == share / "_quarantine" and not source.exists()
    assert "Dolby Vision profile 5" in Path(result.target + ".reason.txt").read_text(
        encoding="utf-8"
    )


async def test_an_existing_target_needs_overwrite(share):
    final = share / "Movies" / "Night of the Living Dead (1968) {imdb-tt0063350}" / \
        "Night of the Living Dead (1968).mkv"  # fmt: skip
    final.parent.mkdir(parents=True)
    final.write_bytes(b"old")
    tools = FakeTools({"job7.mkv": COMPLIANT})
    result = await _ingester(share, tools).ingest(_drop(share, "job7.mkv"), NOTLD)
    assert (
        result.status == "error" and "--overwrite" in result.error and final.read_bytes() == b"old"
    )
    result = await _ingester(share, tools).ingest(share / "_incoming" / "job7.mkv", NOTLD,
                                                  overwrite=True)  # fmt: skip
    assert result.status == "placed" and final.read_bytes() == b"video"


async def test_dry_run_changes_nothing(share):
    source = _drop(share, "job8.mkv")
    result = await _ingester(share, FakeTools({"job8.mkv": FIXABLE})).ingest(
        source, NOTLD, dry_run=True
    )
    assert (result.status, result.exit_code) == ("dry-run", 0)
    assert source.exists() and not (share / "Movies").exists()


async def test_missing_files_are_errors(share):
    result = await _ingester(share, FakeTools({})).ingest("nope.mkv", NOTLD)
    assert result.status == "error" and "source not found" in result.error


def test_part_suffixes_are_stripped():
    assert logical_name("job42.mkv.part") == "job42.mkv" and logical_name("x.crdownload") == "x"
    assert logical_name("plain.mkv") == "plain.mkv"


def test_ingest_needs_a_root():
    with pytest.raises(ValueError, match="ingest_root"):
        Ingester(Config())


# -- Plex -----------------------------------------------------------------------------------------

PLEX = "http://nas:32400"
SECTIONS = {"MediaContainer": {"Directory": [
    {"key": "1", "title": "Movies", "Location": [{"path": "/share/Media/Movies"}]},
    {"key": "2", "title": "TV Shows", "Location": [{"path": "/share/Media/TV Shows/"}]},
]}}  # fmt: skip


async def test_plex_refreshes_the_section_that_holds_the_folder():
    with respx.mock() as router:
        sections = router.get(f"{PLEX}/library/sections").respond(json=SECTIONS)
        refresh = router.get(f"{PLEX}/library/sections/2/refresh").respond(200)
        result = await Plex(PLEX, "secret-token").refresh(
            "TV Shows", PurePath("TV Shows/One Piece (1999)"))  # fmt: skip
    assert (result.status, result.section) == ("ok", "TV Shows")
    assert result.path == "/share/Media/TV Shows/One Piece (1999)" and refresh.called
    request = sections.calls[0].request
    assert request.headers["X-Plex-Token"] == "secret-token" and "token" not in str(request.url)


async def test_plex_reports_missing_libraries_and_bad_tokens():
    with respx.mock() as router:
        router.get(f"{PLEX}/library/sections").respond(json=SECTIONS)
        skipped = await Plex(PLEX, "t").refresh("Anime", PurePath("Anime/X"))
    assert skipped.status == "skipped" and "/Anime" in skipped.detail
    with respx.mock() as router:
        router.get(f"{PLEX}/library/sections").respond(401)
        refused = await Plex(PLEX, None).refresh("Movies", PurePath("Movies/X"))
    assert refused.status == "unauthorized" and "cinemind secrets set plex" in refused.detail
    with respx.mock() as router:
        router.get(f"{PLEX}/library/sections").mock(side_effect=httpx.ConnectError("down"))
        down = await Plex(PLEX, "t").refresh("Movies", PurePath("Movies/X"))
    assert down.status == "error"


async def test_placing_a_file_refreshes_plex(share):
    with respx.mock() as router:
        router.get(f"{PLEX}/library/sections").respond(json=SECTIONS)
        router.get(f"{PLEX}/library/sections/1/refresh").respond(200)
        result = await _ingester(share, FakeTools({"j.mkv": COMPLIANT}), Plex(PLEX, "t")).ingest(
            _drop(share, "j.mkv"), NOTLD)  # fmt: skip
    assert result.plex.status == "ok" and result.plex.section == "Movies"


# -- with real FFmpeg ---------------------------------------------------------------------------


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                    reason="needs FFmpeg on PATH")  # fmt: skip
async def test_a_real_file_with_flac_audio_is_fixed_by_ffmpeg(share):
    source = share / "_incoming" / "real.mkv"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24",
         "-f", "lavfi", "-i", "sine=frequency=440", "-t", "3", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", "-c:a", "flac", str(source)], check=True)  # fmt: skip
    cfg = Config(ingest_root=str(share), file_unlock_timeout=1)
    result = await Ingester(cfg).ingest(source, NOTLD)
    assert (result.status, result.exit_code) == ("fixed", 10), result.error
    assert result.verdict.actions == ["convert audio #1 flac 1ch [und] -> aac 256k 1ch"]
    after = await probe(Path(result.target), shutil.which("ffprobe"))
    codecs = [s["codec_name"] for s in after["streams"]]
    assert codecs == ["h264", "aac"]  # video copied, audio converted
