"""Release plumbing: the notes come from CHANGELOG.md, and the package builds with its files."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _notes_module():
    path = ROOT / "tools" / "release_notes.py"
    spec = importlib.util.spec_from_file_location("release_notes", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_notes"] = module
    spec.loader.exec_module(module)
    return module


CHANGELOG = """# Changelog

## [Unreleased]

- Next thing.

## [0.2.0] - 2026-12-01

### Added
- Doctor.

## [0.1.0] - 2026-11-01

- First.
"""


def test_a_versions_notes_are_its_section_only():
    notes = _notes_module()
    assert notes.section(CHANGELOG, "0.2.0") == "### Added\n- Doctor."
    assert notes.section(CHANGELOG, "0.1.0") == "- First."
    assert notes.section(CHANGELOG, "0.3.0") is None
    assert notes.section(CHANGELOG, "0.2") is None  # no prefix matches


def test_a_tag_without_notes_is_refused(capsys):
    assert _notes_module().main(["v99.0.0"]) == 1
    assert "no '## [99.0.0]' section" in capsys.readouterr().err


def test_the_changelog_has_a_section_for_unreleased_work():
    text = (ROOT / "CHANGELOG.md").read_text("utf-8")
    assert _notes_module().section(text, "Unreleased")


def test_the_image_gets_only_what_it_needs():
    lines = (ROOT / ".dockerignore").read_text("utf-8").splitlines()
    ignore = [x.strip() for x in lines if x.strip() and not x.startswith("#")]
    dockerfile = (ROOT / "Dockerfile").read_text("utf-8")
    assert ignore[0] == "*"  # everything else stays out (no .git, .venv, evals, secrets)
    for needed in ("pyproject.toml", "uv.lock", "README.md", "LICENSE", "src/"):
        assert f"!{needed}" in ignore and needed.rstrip("/") in dockerfile
    assert "USER cinemind" in dockerfile and "--locked" in dockerfile
