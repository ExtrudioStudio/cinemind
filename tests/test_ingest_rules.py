"""Parity with MediaPipeline v1: every golden case gets the same verdict, word for word.

The cases in ``fixtures/ingest/golden`` were recorded from the PowerShell implementation
(``Test-DirectPlay`` / ``New-FixArguments``): 23 real files and 37 synthetic probes, one per
rule branch. See ``fixtures/ingest/README.md``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cinemind.ingest.fix import fix_arguments
from cinemind.ingest.profile import load_profile
from cinemind.ingest.rules import evaluate

from .conftest import FIXTURES

GOLDEN = sorted((FIXTURES / "ingest" / "golden").glob("*.json"))
PROFILE = load_profile("samsung-q60b")


def _case(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def test_there_are_golden_cases():
    kinds = [_case(p)["kind"] for p in GOLDEN]
    assert kinds.count("real") == 23 and kinds.count("synthetic") == 37


@pytest.mark.parametrize("path", GOLDEN, ids=lambda p: p.stem)
def test_verdict_matches_v1(path):
    case = _case(path)
    want = case["expected"]
    got = evaluate(case["probe"], PROFILE, case["extension"])
    assert got.verdict == want["verdict"]
    assert got.reasons == want["reasons"]
    assert got.actions == want["actions"]
    assert got.warnings == want["warnings"]
    assert got.video == want["video"]
    assert got.bitrate_mbps == pytest.approx(want["bitrateMbps"])
    assert got.duration_sec == pytest.approx(want["durationSec"])
    if want["fixArgs"] is None:
        assert got.verdict != "FIXABLE"
    else:
        # PowerShell's `,$array` kept the list from unrolling, so the recording wraps it once.
        recorded = want["fixArgs"]
        expected_args = recorded[0] if isinstance(recorded[0], list) else recorded
        assert fix_arguments(got, "INPUT", "OUTPUT") == expected_args


def test_a_rejected_file_without_video_cannot_be_fixed():
    case = _case(FIXTURES / "ingest" / "golden" / "syn-reject-no-video.json")
    with pytest.raises(ValueError, match="no video"):
        fix_arguments(evaluate(case["probe"], PROFILE, ".mkv"), "in", "out")


def test_profiles_load_by_name_or_path(tmp_path):
    assert PROFILE.device == "Q60B" and PROFILE.video.direct["hevc"].max_bit_depth == 10
    assert PROFILE.video.direct["h264"].max4k_fps == 30  # the alias pitfall: see profile.py
    custom = tmp_path / "tv.json"
    data = json.loads((Path(__file__).parents[1] / "src" / "cinemind" / "ingest" / "profiles"
                       / "samsung-q60b.json").read_text(encoding="utf-8"))  # fmt: skip
    data.update(device="TV2", brand="Acme")
    custom.write_text(json.dumps(data), encoding="utf-8")
    other = load_profile(str(custom))
    case = _case(FIXTURES / "ingest" / "golden" / "syn-reject-dv-profile5.json")
    assert "Acme TVs cannot display it" in evaluate(case["probe"], other, ".mkv").reasons[0]
