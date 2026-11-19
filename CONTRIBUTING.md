# Contributing

Thanks for helping. This guide covers the local setup, the checks CI runs and what a pull
request needs.

## Setup

You need [uv](https://docs.astral.sh/uv/). From the repository root:

```bash
uv sync                      # Python 3.13, libtorrent and the dev tools in .venv
uv run pre-commit install    # run the checks below on every commit
```

libtorrent ships wheels for Windows, macOS and musl Linux (Alpine). On glibc Linux (Ubuntu,
Debian, Fedora), develop in a `python:3.13-alpine` container, as CI does.

## Checks

| Command | What it checks |
|---|---|
| `uv run pytest` | Tests. They run offline: addon responses are recorded and torrents come from a local seeder. |
| `uv run pytest -m "not torrent"` | Skips the libtorrent tests (about 6 s) |
| `uv run pytest --cov` | Coverage; fails under the `fail_under` value in `pyproject.toml` |
| `uv run ruff check src tests tools` | Lint |
| `uv run ruff format src tests tools` | Formatting |
| `uv run mypy` | Types |

Tests marked `network` call the live addon APIs. CI skips them; run them by hand with
`uv run pytest -m network` when you change the addon client.

## Pull requests

- Keep one change per pull request, and describe the user-visible effect first.
- Add a test that fails without your change. For a bug, it should reproduce the bug.
- Update `CHANGELOG.md` under **Unreleased**.
- Don't lower the coverage `fail_under` value. Raise it when your change adds coverage.
- Never commit personal paths, addresses, tokens or real configuration. Examples use
  placeholders such as `\\nas\Media`.

## Where things live

| Path | Contents |
|---|---|
| `src/stremio_dl/addons/` | Stremio addon protocol client and its disk cache |
| `catalog.py`, `metadata.py` | Search and title metadata |
| `selection.py`, `quality.py` | The `s1e2-5 /1080p` selection language |
| `streams.py`, `planner.py` | Stream normalisation, ranking and the download plan |
| `engine/` | The downloader protocol and the libtorrent engine |
| `library.py` | Library paths and "already have it" checks |
| `store.py` | SQLite state: jobs, items, torrents and resume data |
| `runner.py`, `postprocess.py` | Running a plan, and the post-download command |
| `cli.py`, `home.py`, `wizard.py`, `ui.py` | Command line, home menu and terminal UI |
| `tests/fixtures/` | Recorded addon responses and the ingest golden cases |
