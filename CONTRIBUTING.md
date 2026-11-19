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
Debian, Fedora), use `uv sync --no-group torrent` and `uv run --no-sync ...`: everything but
the BitTorrent tests runs. To work on the torrent engine there, use a `python:3.13-alpine`
container, as CI does.

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

| Path (under `src/cinemind/`) | Contents |
|---|---|
| `core/` | `config.py` (settings, folders, the stremio-dl migration), `models.py`, `store.py` (SQLite: jobs, items, downloads, hand-offs; WAL and migrations) |
| `sources/` | `base.py` (the `StreamSource` contract), `registry.py` (built-ins and entry-point plugins), `internet_archive.py` |
| `sources/stremio/` | Stremio addon protocol client and disk cache, catalog search, metadata, the addon stream source |
| `planning/` | `selection.py` / `quality.py` (the `s1e2-5 /1080p` language), `streams.py` (filtering, ranking, the resolver), `planner.py`, `library.py` |
| `engines/` | `base.py` (the downloader protocol), `http.py`, `torrent.py` (libtorrent), `multi.py` (runs both) |
| `pipeline/` | `runner.py`, `postprocess.py` (the post-download command), `hookrun.py` (its wrapper) |
| `cli/` | `app.py` (Typer commands), `home.py`, `wizard.py`, `ui.py`, `shortcut.py` |
| `tests/fixtures/` | Recorded addon and Internet Archive responses, and the ingest golden cases |
