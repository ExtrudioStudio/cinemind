"""`cinemind doctor`: each check's verdict and fix, with the network mocked."""

from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from cinemind.cli import app as cli
from cinemind.core.config import save_config
from cinemind.core.network import Interface
from cinemind.doctor import (
    check_ingest,
    check_judge,
    check_plex,
    check_sources,
    check_subtitles,
    check_vpn,
    run_checks,
)

VPN_UP = [Interface("Wi-Fi", True, ("192.168.1.20",)), Interface("NordLynx", True, ("10.5.0.2",))]
VPN_GONE = [Interface("Wi-Fi", True, ("192.168.1.20",))]


def _http(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _by_name(checks):
    return {c.name: c for c in checks}


async def test_a_fresh_install_offline_fails_nothing_and_skips_what_isnt_set_up(cfg):
    checks = _by_name(await run_checks(cfg, offline=True))
    assert not [c for c in checks.values() if c.status == "fail"]
    for name in ("VPN", "Library share", "Plex", "Providers"):
        assert checks[name].status == "skip", name
    assert "--offline" in checks["Pack-file judge"].detail and "Metadata" not in checks
    assert checks["TV library"].status == "ok" and "will be created" in checks["TV library"].detail


def test_the_vpn_check_says_whether_torrents_will_start(cfg):
    cfg.outgoing_interfaces = "NordLynx"
    down = check_vpn(cfg, VPN_GONE)
    assert down.status == "fail" and "won't start" in down.detail and "Wi-Fi" in down.fix
    assert check_vpn(cfg, VPN_UP).status == "ok"
    cfg.vpn_kill_switch = False
    assert check_vpn(cfg, VPN_UP).status == "warn"  # works, but nothing stops a leak
    assert "bypass" in check_vpn(cfg, VPN_GONE).detail


async def test_plex_is_tried_with_the_token_and_a_rejected_token_is_named(cfg, monkeypatch):
    cfg.plex_url = "http://nas:32400"
    no_token = await check_plex(cfg, None)
    assert no_token.status == "fail" and "secrets set plex" in no_token.fix
    monkeypatch.setenv("CINEMIND_PLEX", "tok")
    seen = []

    def plex(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("X-Plex-Token"))
        return httpx.Response(
            200, json={"MediaContainer": {"Directory": [{"key": "1"}, {"key": "2"}]}}
        )

    async with _http(plex) as http:
        ok = await check_plex(cfg, http)
    assert ok.status == "ok" and "2 libraries" in ok.detail and seen == ["tok"]
    assert "tok" not in ok.detail  # never the secret itself
    async with _http(lambda r: httpx.Response(401)) as http:
        assert "rejected the token" in (await check_plex(cfg, http)).detail


@pytest.mark.parametrize(
    ("handler", "status", "says"),
    [
        (lambda r: httpx.Response(200, json={"models": [{"name": "qwen2.5:7b"}]}), "ok", "ollama/"),
        (
            lambda r: httpx.Response(200, json={"models": [{"name": "llama3:8b"}]}),
            "warn",
            "ollama pull qwen2.5:7b",
        ),
        (lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused")), "warn", "isn't running"),
    ],
)
async def test_the_judge_check_finds_ollama_and_its_model(cfg, handler, status, says):
    async with _http(handler) as http:
        check = await check_judge(cfg, http)
    assert check.status == status and says in check.detail + check.fix


async def test_the_metadata_addon_must_answer(cfg):
    async with _http(lambda r: httpx.Response(200, json={"name": "Cinemeta"})) as http:
        ok = _by_name(await check_sources(cfg, http))
    assert ok["Metadata"].status == "ok" and "Cinemeta" in ok["Metadata"].detail
    async with _http(lambda r: httpx.Response(503)) as http:
        assert _by_name(await check_sources(cfg, http))["Metadata"].status == "fail"
    cfg.stream_sources = ["internet-archive", "pirate-bay"]
    assert _by_name(await check_sources(cfg, None))["Sources"].status == "fail"


def test_what_the_settings_turn_on_must_be_there(cfg, tmp_path):
    cfg.auto_ingest = True
    assert check_ingest(cfg)[0].status == "fail"  # on, with no share
    cfg.ingest_root = str(tmp_path / "unplugged-nas" / "Media")
    assert "can't be reached" in check_ingest(cfg)[0].detail
    share = tmp_path / "share"
    share.mkdir()
    cfg.ingest_root = str(share)
    assert [c.status for c in check_ingest(cfg)] == ["ok", "ok"]  # the share, the TV profile

    cfg.opensubtitles_username = "someone"
    subtitles = check_subtitles(cfg)
    assert subtitles.status == "fail" and "opensubtitles_api_key" in subtitles.fix


def test_the_command_prints_json_and_exits_1_when_something_fails(cfg):
    save_config(cfg)
    ready = CliRunner().invoke(cli.app, ["doctor", "--offline", "--json"])
    report = json.loads(ready.output)
    assert ready.exit_code == 0 and report["healthy"] and report["fail"] == 0
    assert {"area", "name", "status", "detail", "fix"} <= set(report["checks"][0])

    cfg.outgoing_interfaces = "NoSuchAdapter-cinemind-test"
    save_config(cfg)
    broken = CliRunner().invoke(cli.app, ["doctor", "--offline"])
    assert broken.exit_code == 1 and "Not ready" in broken.output
