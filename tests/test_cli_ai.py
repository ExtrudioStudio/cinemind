"""The command line's plain-language paths: `cinemind ask`, and `get` with unparsable words."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from cinemind.cli import app as cli
from cinemind.core.config import save_config
from cinemind.core.store import Store
from cinemind.planning.selection import SelectionError

from .fakes import FakeLLM


def test_ask_without_ai_says_how_to_turn_it_on(cfg):
    cfg.llm_enabled = False
    save_config(cfg)
    result = CliRunner().invoke(cli.app, ["ask", "the last two seasons of Friends"])
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.output and "cinemind[ai]" in result.output


async def test_get_falls_back_to_the_model_and_shows_how_it_read_the_words(
    cfg, friends, tmp_path, monkeypatch, capsys
):
    llm = FakeLLM({"episodes": {"scope": "first_seasons", "count": 1}, "quality": "720p"})
    monkeypatch.setattr(cli.LLM, "from_config", classmethod(lambda cls, c, s=None: llm))
    store = Store(tmp_path / "state.db")
    picked = await cli._read_selection("the very first season in hd", friends, cfg, store)
    store.close()
    assert {r.season for r in picked.refs} == {1} and picked.quality == "720p"
    assert "s1 /720p" in capsys.readouterr().out


async def test_get_without_ai_keeps_the_parser_error_and_adds_a_hint(cfg, friends, tmp_path):
    cfg.llm_enabled = False
    store = Store(tmp_path / "state.db")
    with pytest.raises(SelectionError, match="cinemind\\[ai\\]"):
        await cli._read_selection("the very first season", friends, cfg, store)
    store.close()
