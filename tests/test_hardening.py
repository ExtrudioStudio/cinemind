"""Inputs that used to slip through: malformed addon JSON, cancelled gathers, bad --type values."""

from __future__ import annotations

import asyncio

import pytest
import respx
import typer
from typer.testing import CliRunner

from stremio_dl import cli
from stremio_dl.addons.client import Addon, AddonClient, AddonError
from stremio_dl.aio import reraise_cancelled
from stremio_dl.config import Config

BASE = "https://addon.example"


def test_reraise_cancelled_passes_values_and_errors_through():
    error = ValueError("boom")
    assert reraise_cancelled([1, 2]) == [1, 2]
    assert reraise_cancelled(error) is error


@pytest.mark.parametrize("exc", [asyncio.CancelledError(), KeyboardInterrupt()])
def test_reraise_cancelled_raises_cancellation(exc):
    with pytest.raises(type(exc)):
        reraise_cancelled(exc)


async def test_gather_with_a_cancelled_call_cancels_the_caller():
    async def slow():
        await asyncio.sleep(10)

    async def caller():
        inner = asyncio.ensure_future(slow())
        asyncio.get_running_loop().call_later(0.01, inner.cancel)
        results = await asyncio.gather(inner, return_exceptions=True)
        return [reraise_cancelled(r) for r in results]

    with pytest.raises(asyncio.CancelledError):
        await caller()


@pytest.fixture
def client():
    return AddonClient(Config(), retry_wait=0.0)


@pytest.mark.parametrize("body", [b"[1, 2]", b'"text"', b"null", b"3"])
async def test_json_that_is_not_an_object_is_an_addon_error(client, body):
    with respx.mock() as router:
        router.get(f"{BASE}/manifest.json").respond(
            content=body, headers={"content-type": "application/json"}
        )
        with pytest.raises(AddonError, match="not an object"):
            await client.get_json(f"{BASE}/manifest.json", ttl=0)
    await client.aclose()


async def test_list_fields_keep_only_objects(client):
    addon = Addon(f"{BASE}/manifest.json")
    with respx.mock() as router:
        router.get(f"{BASE}/stream/movie/tt1.json").respond(
            json={"streams": [{"infoHash": "a" * 40}, "junk", 7, None]}
        )
        router.get(f"{BASE}/catalog/movie/top/search=x.json").respond(json={"metas": {"id": "x"}})
        assert await client.streams(addon, "movie", "tt1") == [{"infoHash": "a" * 40}]
        assert await client.catalog_search(addon, "movie", "top", "x") == []
    await client.aclose()


async def test_meta_that_is_not_an_object_is_an_addon_error(client):
    addon = Addon(f"{BASE}/manifest.json")
    with respx.mock() as router:
        router.get(f"{BASE}/meta/movie/tt1.json").respond(json={"meta": ["not", "an", "object"]})
        with pytest.raises(AddonError, match="no metadata"):
            await client.meta(addon, "movie", "tt1")
    await client.aclose()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("series", "series"), ("TV", "series"), (" show ", "series"), ("movie", "movie"),
     ("film", "movie"), (None, None)],
)  # fmt: skip
def test_media_type_aliases(raw, expected):
    assert cli._media_type(raw) == expected


def test_media_type_rejects_unknown_values():
    with pytest.raises(typer.BadParameter):
        cli._media_type("anime")


def test_unknown_type_is_a_usage_error_before_any_network_call():
    result = CliRunner().invoke(cli.app, ["search", "friends", "--type", "anime"])
    assert result.exit_code == 2
    assert "series" in result.output and "movie" in result.output
