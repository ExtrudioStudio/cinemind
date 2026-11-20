"""Stream sources: the Internet Archive source, the plugin registry and mixed-source ranking."""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
import respx
import typer
from typer.testing import CliRunner

from cinemind.cli import app as cli
from cinemind.core import config as config_mod
from cinemind.core.config import Config, save_config
from cinemind.core.models import Candidate, Title
from cinemind.planning.streams import StreamResolver, rank, resolution_from_height
from cinemind.sources import registry
from cinemind.sources.base import SourceContext, StreamSource
from cinemind.sources.internet_archive import (
    InternetArchiveSource,
    is_public_domain,
    title_matches,
)
from cinemind.sources.stremio.client import AddonClient

from .conftest import load

NOTLD = Title("tt0063350", "movie", "Night of the Living Dead", 1968)
TOP_ITEM = "Night.Of.The.Living.Dead_1080p"


@pytest.fixture
def archive():
    with respx.mock(assert_all_called=False) as router:
        search = router.get(url__startswith="https://archive.org/advancedsearch.php")
        search.respond(json=load("ia_search_notld.json"))
        router.get(f"https://archive.org/metadata/{TOP_ITEM}").respond(
            json=load("ia_metadata_notld.json")
        )
        router.get(url__startswith="https://archive.org/metadata/").respond(500)  # the others
        yield router, search


async def test_archive_offers_the_public_domain_video_files(archive):
    router, search = archive
    async with AddonClient(Config(), retry_wait=0.0) as client:
        found = await InternetArchiveSource(client).candidates(NOTLD, None)

    query = parse_qs(urlparse(str(search.calls[0].request.url)).query)
    assert 'title:("Night of the Living Dead")' in query["q"][0] and "year:1968" in query["q"][0]
    names = sorted(c.filename for c in found)
    # .m2ts, .mpg, .ogv, subtitles and the .torrent are left out
    assert names == ["NightOfTheLivingDead_1080p.mkv", "NightOfTheLivingDead_1080p.mp4",
                     "NightOfTheLivingDead_1080p_512kb.mp4", "NightOfTheLivingDead_720p.mkv",
                     "NightOfTheLivingDead_PS3.mp4"]  # fmt: skip
    by_name = {c.filename: c for c in found}
    mkv = by_name["NightOfTheLivingDead_1080p.mkv"]
    assert (mkv.resolution, mkv.size, mkv.md5) == ("1080p", 11523767255,
                                                   "e88c05d6cce29346bbbdc838f997baec")  # fmt: skip
    assert mkv.url == f"https://archive.org/download/{TOP_ITEM}/NightOfTheLivingDead_1080p.mkv"
    assert mkv.is_direct and mkv.info_hash == Candidate.id_for_url(mkv.url)
    assert mkv.languages == ("en",) and mkv.seeders == 318927 and mkv.addon == "internet-archive"
    assert by_name["NightOfTheLivingDead_720p.mkv"].resolution == "720p"
    assert by_name["NightOfTheLivingDead_1080p.mp4"].resolution == "480p"  # a 640x480 derivative


async def test_archive_search_keeps_only_public_domain_matches(archive):
    async with AddonClient(Config(), retry_wait=0.0) as client:
        items = await InternetArchiveSource(client).search(NOTLD)
    ids = [i["identifier"] for i in items]
    assert "NightOfTheLivingDead-MPEG" not in ids  # no licence on the item
    assert "night_of_the_living_dead_1990" not in ids  # same title, other year
    assert ids[0] == TOP_ITEM


def test_archive_does_series_not():
    source = InternetArchiveSource(None)  # type: ignore[arg-type]
    assert source.supports(NOTLD)
    assert not source.supports(Title("tt0108778", "series", "Friends", 1994))


@pytest.mark.parametrize(
    ("url", "expected"),
    [("http://creativecommons.org/licenses/publicdomain/", True),
     ("https://creativecommons.org/publicdomain/mark/1.0/", True),
     ("http://creativecommons.org/publicdomain/zero/1.0/", True),
     ("http://creativecommons.org/licenses/by-nc/3.0/", False), (None, False), ("", False)],
)  # fmt: skip
def test_public_domain_licences(url, expected):
    assert is_public_domain(url) is expected


def test_title_matching_uses_whole_words_and_the_year():
    it = Title("tt0018033", "movie", "It", 1927)
    assert title_matches({"title": "It", "year": "1927"}, it)
    assert not title_matches({"title": "It's a Wonderful Life", "year": "1927"}, it)
    assert title_matches({"title": "Night of the Living Dead iPod version", "year": 1968}, NOTLD)
    assert not title_matches({"title": "Night of the Living Dead", "year": 1990}, NOTLD)
    assert title_matches({"title": "Totally different", "external-identifier":
                          ["urn:imdb:tt0063350"]}, NOTLD)  # fmt: skip


def test_heights_map_to_resolutions():
    assert [resolution_from_height(h) for h in (2160, 1440, 1080, 720, 576, 480, 360, 240, 0)] == [
        "2160p", "1440p", "1080p", "720p", "480p", "480p", "360p", "360p", "unknown"
    ]  # fmt: skip


# -- ranking across sources -------------------------------------------------------------


def _cand(name, resolution="1080p", seeders=10, size=1, url=""):
    info_hash = Candidate.id_for_url(url) if url else name[0] * 40
    return Candidate(info_hash, None, resolution, seeders, size, filename=f"{name}.mkv",
                     release=name, url=url)  # fmt: skip


def test_direct_downloads_come_before_torrents_at_the_same_resolution():
    torrent = _cand("busy", seeders=900)
    archive = _cand("archive", seeders=5, url="https://archive.org/download/x/a.mkv")
    low = _cand("low", resolution="720p", seeders=5, url="https://archive.org/download/x/b.mkv")
    assert rank([torrent, archive, low], "1080p") == [archive, torrent]
    assert rank([torrent, low], "best") == [torrent, low]  # resolution still comes first


class _Fixed:
    def __init__(self, name, cands, fail=False):
        self.name, self.cands, self.fail = name, cands, fail

    def supports(self, title):
        return True

    async def candidates(self, title, ref):
        if self.fail:
            raise RuntimeError("source down")
        return self.cands


async def test_resolver_merges_sources_and_survives_a_failing_one(cfg):
    a = _cand("alpha")
    b = _cand("bravo", url="https://archive.org/download/x/b.mkv")
    resolver = StreamResolver([_Fixed("one", [a]), _Fixed("broken", [], fail=True),
                               _Fixed("two", [b, a])], cfg)  # fmt: skip
    found = await resolver.candidates(NOTLD, None)
    assert sorted(c.release for c in found) == ["alpha", "bravo"]  # de-duplicated


# -- registry ------------------------------------------------------------------------------


def test_registry_builds_the_builtin_sources(cfg):
    ctx = SourceContext(cfg, AddonClient(cfg))
    sources = registry.load_sources(["internet-archive", "stremio-addons", "internet-archive"],
                                    ctx)  # fmt: skip
    assert [s.name for s in sources] == ["internet-archive", "stremio-addons"]
    assert all(isinstance(s, StreamSource) for s in sources)
    assert not sources[1].supports(NOTLD)  # no stream addons configured


def test_registry_rejects_unknown_names(cfg):
    with pytest.raises(ValueError, match="Available: internet-archive, stremio-addons"):
        registry.load_sources(["nope"], SourceContext(cfg, AddonClient(cfg)))


def test_registry_finds_plugins_through_entry_points(cfg, monkeypatch):
    plugin = _Fixed("my-archive", [])
    ep = SimpleNamespace(name="my-archive", load=lambda: lambda ctx: plugin)
    clash = SimpleNamespace(name="internet-archive", load=lambda: pytest.fail("must not load"))
    monkeypatch.setattr(registry, "entry_points", lambda group: [ep, clash])
    assert "my-archive" in registry.available()
    assert registry.load_sources(["my-archive"], SourceContext(cfg, AddonClient(cfg))) == [plugin]


def test_source_names_from_the_command_line():
    assert registry.resolve(["archive"]) == ["internet-archive"]
    assert registry.resolve(["IA, addons", "internet-archive"]) == [
        "internet-archive",
        "stremio-addons",
    ]
    with pytest.raises(ValueError, match="'torrentio'.*short names: archive = internet-archive"):
        registry.resolve(["archive", "torrentio"])


# -- defaults, migration and CLI -----------------------------------------------------------


def test_new_configs_have_no_torrent_addons():
    cfg = Config()
    assert cfg.stream_addons == [] and cfg.stream_sources[0] == "internet-archive"


def test_legacy_files_are_copied_once(tmp_path):
    old_cfg, old_db = tmp_path / "old" / "config.toml", tmp_path / "old" / "state.db"
    old_cfg.parent.mkdir()
    old_cfg.write_text('tv_root = "X"\n', encoding="utf-8")
    db = sqlite3.connect(old_db)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE t (x)")
    db.execute("INSERT INTO t VALUES (42)")
    db.commit()  # stays in the WAL until a checkpoint: the backup must still see it
    new_cfg, new_db = tmp_path / "new" / "config.toml", tmp_path / "new" / "state.db"
    pairs = [(old_cfg, new_cfg), (old_db, new_db)]

    assert config_mod.migrate_legacy_files(pairs) == [new_cfg, new_db]
    db.close()
    assert new_cfg.read_text(encoding="utf-8") == 'tv_root = "X"\n'
    copy = sqlite3.connect(new_db)
    assert copy.execute("SELECT x FROM t").fetchall() == [(42,)]
    copy.close()
    old_cfg.write_text('tv_root = "Y"\n', encoding="utf-8")
    assert config_mod.migrate_legacy_files(pairs) == []  # once only: new files are kept
    assert new_cfg.read_text(encoding="utf-8") == 'tv_root = "X"\n'


def test_private_home_skips_the_migration():
    assert config_mod.migrate_legacy_files() == []  # CINEMIND_HOME is set by conftest


def test_sources_command_and_the_stream_addon_notice(cfg):
    save_config(cfg)
    runner = CliRunner()
    listing = runner.invoke(cli.app, ["sources"])
    assert listing.exit_code == 0 and "internet-archive" in listing.output
    result = runner.invoke(cli.app, ["config", "set", "stream_addons",
                                     "https://addon.example/manifest.json"])  # fmt: skip
    assert result.exit_code == 0 and "right to" in result.output
    quiet = runner.invoke(cli.app, ["config", "set", "tv_root", "D:/TV"])
    assert quiet.exit_code == 0 and "right to" not in quiet.output


def test_get_source_limits_only_this_run(cfg, monkeypatch):
    save_config(cfg)  # both sources on
    seen: list[list[str]] = []

    @asynccontextmanager
    async def session(run_cfg):
        seen.append(run_cfg.stream_sources)
        raise typer.Exit(3)  # stop before any network
        yield

    monkeypatch.setattr(cli, "open_session", session)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["get", "Night of the Living Dead 1968", "--source", "archive"])
    assert result.exit_code == 3 and seen == [["internet-archive"]]
    assert config_mod.load_config().stream_sources == cfg.stream_sources  # settings unchanged

    wrong = runner.invoke(cli.app, ["get", "Night of the Living Dead", "-s", "torrentio"])
    assert wrong.exit_code == 2 and "Unknown stream source 'torrentio'" in wrong.output
    assert len(seen) == 1  # refused before a session opened


def test_planning_a_direct_download(cfg):
    from cinemind.planning.planner import build_plan

    c = _cand("archive", url="https://archive.org/download/x/film.mp4")
    c = Candidate(**{**c.to_dict(), "filename": "film.mp4"})
    plan = build_plan(NOTLD, [None], {None: [c]}, cfg, "1080p")
    (job,) = plan.jobs
    assert job.is_direct and job.info_hash == c.info_hash
    assert job.targets[0].final_path.name == "Night of the Living Dead (1968).mp4"
