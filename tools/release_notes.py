"""Print a version's section of CHANGELOG.md, for its GitHub Release.

    python tools/release_notes.py 0.2.0 > notes.md

Exits 1 when the changelog has no ``## [0.2.0]`` section: a release needs its notes written
first (move them from ``## [Unreleased]``).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def section(text: str, version: str) -> str | None:
    """The body under ``## [version]`` (with or without a date), up to the next ``## ``."""
    heading = re.compile(rf"^## \[{re.escape(version)}\][^\n]*$", re.MULTILINE)
    found = heading.search(text)
    if found is None:
        return None
    rest = text[found.end() :]
    end = re.search(r"^## ", rest, re.MULTILINE)
    return (rest[: end.start()] if end else rest).strip()


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    version = argv[0].removeprefix("v")
    body = section(CHANGELOG.read_text(encoding="utf-8"), version)
    if body is None:
        print(f"CHANGELOG.md has no '## [{version}]' section.", file=sys.stderr)
        return 1
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
