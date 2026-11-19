<img src="src/cinemind/assets/cinemind.png" width="96" align="right" alt="">

# cinemind

[![CI](https://github.com/ExtrudioStudio/cinemind/actions/workflows/ci.yml/badge.svg)](https://github.com/ExtrudioStudio/cinemind/actions/workflows/ci.yml)
[![CodeQL](https://github.com/ExtrudioStudio/cinemind/actions/workflows/codeql.yml/badge.svg)](https://github.com/ExtrudioStudio/cinemind/actions/workflows/codeql.yml)
![Python 3.12 | 3.13](https://img.shields.io/badge/python-3.12%20%7C%203.13-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

cinemind finds a movie or TV show, picks the best copy at the resolution you ask for, downloads
only the files you selected, and writes them straight into a Plex-ready library:

```
Movies\Night of the Living Dead (1968)\Night of the Living Dead (1968).mkv
TV Shows\Show\Season 01\Show - S01E10 - Episode Title.mkv
```

Titles and episode lists come from [Cinemeta](https://v3-cinemeta.strem.io/manifest.json).
Downloads come from **stream sources** that you choose:

- **Internet Archive** (on by default): public-domain films only, as direct HTTP downloads
  that are checked against the archive's published MD5.
- **Stremio stream addons** (none by default): paste an addon's manifest URL, for example a
  Torrentio configuration, to download torrents through libtorrent.
- **Plugins**: other packages can add sources (see [Stream sources](#stream-sources)).

## Quick start

Requires [uv](https://docs.astral.sh/uv/). From this folder:

```powershell
uv sync                                    # .venv with Python 3.13 (and libtorrent where available)
uv run cinemind get "Night of the Living Dead" "1080p" --type movie --dry-run
```

```
         Release                    Res     Seeds      Size   Source        Note
 Movie   Night of the Living Dead   1080p   direct   10.7 GB   archive.org   en, md5-checked
1 file from 1 direct download · 10.7 GB
```

Drop `--dry-run` to download it. `uv run cinemind shortcut` adds a desktop and Start menu
icon that opens the home menu, and `uv tool install ".[torrent]"` gives you a global
`cinemind` command.

Installing from a wheel or PyPI: `pip install cinemind` for direct downloads, or
`pip install "cinemind[torrent]"` to add BitTorrent. libtorrent has wheels for Windows, macOS
and Alpine (musl) Linux. On other Linux distributions, cinemind works without it, for direct
downloads only.

The first run creates `%APPDATA%\cinemind\config.toml`. **Upgrading from stremio-dl:** your
`config.toml` and download history are copied over on the first run (the old files stay
where they are), and the `sdl` command still works for this release.

## Use

The icon (or `cinemind` with no command) opens a home menu:

```
? What would you like to do?
 » Download a movie or TV show
   Resume paused downloads (1)
   Download history
   Open TV Shows folder
   Open Movies folder
   Settings
   Quit
```

**Download a movie or TV show** walks you through search → pick → overview → selection →
plan → download. When it finishes, or when you press Ctrl+C to pause, you're back at the
menu; the window only closes when you pick **Quit**. **Settings** opens the config file in
Notepad.

One-shot:

```powershell
cinemind get "Night of the Living Dead" "720p" --type movie   # asks before starting
cinemind get tt0063350 "best" --type movie --yes              # by IMDb id, no confirmation
cinemind get "Show" "s1 eps10 - s2 eps 3 /full hd" --dry-run  # series need a stream addon
```

Other commands:

| Command | What it does |
|---|---|
| `cinemind search "the office"` | List matching titles |
| `cinemind info tt0063350` | Seasons, episode counts and sampled resolutions |
| `cinemind status` | Download jobs, their progress, and how their post-download commands ended |
| `cinemind resume [JOB]` | Continue paused or interrupted jobs (Ctrl+C pauses) |
| `cinemind handoff JOB` | Run `post_download_command` for a finished job's files still in the library |
| `cinemind sources` | The stream sources and which ones are on |
| `cinemind shortcut` | Create the desktop (and Start menu) icon |
| `cinemind config show` / `set KEY VALUE` / `reset` / `path` | Settings |

### Selection syntax

| You type | You get |
|---|---|
| `s1 eps10 - s2 eps 3 /full hd` | S01E10 through S02E03 (crosses seasons), 1080p |
| `s3` | All of season 3 at `default_quality` |
| `s1-s3 /4k` or `s1-3 /4k` | Seasons 1–3 in 2160p |
| `s2e1-5, s4e2 /720` | S02E01–05 and S04E02 in 720p |
| `1x05`, `season 1 episode 5` | S01E05 |
| `all /best` | Every aired episode, highest resolution available per episode |

Quality words: `4k`/`uhd`, `1440p`/`2k`, `1080p`/`full hd`/`fhd`, `720p`/`hd`, `480p`/`sd`,
`best`. Separate them with `/` or `@`, or just put them at the end (`s1 1080p`).

In Git Bash, an argument that *starts* with `/` (like `"/4k"`) gets rewritten into a
Windows path; use `@4k`, `-q 4k` or plain `4k` there. PowerShell and cmd are fine.

## Stream sources

`stream_sources` lists the sources to ask, in order (`cinemind sources` shows them):

| Source | Offers | Notes |
|---|---|---|
| `internet-archive` | Public-domain films from archive.org | Only items whose licence is the Public Domain Mark or CC0. Movies only. Direct HTTP, resumable, MD5-checked. |
| `stremio-addons` | Torrents from the addons in `stream_addons` | Empty by default. Needs the `torrent` extra. |

A plugin package adds a source with an entry point in its `pyproject.toml`:

```toml
[project.entry-points."cinemind.sources"]
my-archive = "my_package.source:create"   # create(ctx: SourceContext) -> StreamSource
```

A source implements `supports(title)` and `async candidates(title, episode)`
(`cinemind.sources.base.StreamSource`). The resolver handles filtering, de-duplication and
ranking.

## How a copy is chosen

1. Every selected episode (or the movie) is looked up on each source that supports it.
2. Unusable offers are dropped:
   - not a video file (`.exe`, `.lnk`, archives and so on)
   - cam/screener releases
   - releases whose audio is only in languages outside `languages` (default `["en"]`)
   - optionally, Dolby-Vision-only releases (`exclude_dv_only`)
3. The rest are filtered to the requested resolution. Direct downloads come first (they need
   no peers), then torrents by seeders. The top one wins, and up to `fallbacks_per_episode`
   more are kept as fallbacks.
4. Episodes whose winners share a torrent (a season pack) are downloaded as one torrent.
   `--prefer-packs` goes further: it consolidates onto a pack that has at least half the
   best seeder count.
5. If a download fails (no torrent metadata within `metadata_timeout`, no progress for
   `stall_timeout`, an HTTP error or a checksum mismatch), its episodes move to their next
   candidate, which may come from another source.
6. When an episode has no copy at the requested resolution, `on_missing` decides what
   happens: `ask` (the default), `lower` or `skip`.

Files are written to their library path as `name.mkv.part` and renamed when complete: from a
torrent only the wanted files are downloaded, and a direct download resumes where it stopped.
Files already in the library are skipped. That includes the `extra_tv_roots` /
`extra_movie_roots` folders, such as a NAS that finished files are moved to. Plex-style folder
names there (`Show (1999) {tvdb-81797}`, `Movie (2010) {imdb-tt1375666}`) are recognised.

## After a download

Set `post_download_command` to run a command for every finished file, for example a script
that checks it and moves it to a NAS. It starts only once the download is complete (a season
pack hands over all its episodes at the end). It runs in the background, in its own hidden
console, so it keeps going if you return to the menu or quit. `cinemind status` shows how it
ended, and its output goes to `%LOCALAPPDATA%\cinemind\Logs\post-download\`.

The command is split into arguments first, and the placeholders are filled into each argument,
so a value with spaces or quotes stays one argument. The same values are in the environment as
`CINEMIND_PATH`, `CINEMIND_ID`, `CINEMIND_TYPE` and so on.

| Placeholder | Value |
|---|---|
| `{path}` | The finished file |
| `{id}` | Stremio id: `tt0388629:1:2` for an episode, `tt0063350` for a movie |
| `{imdb}` | `tt…`, or empty for non-IMDb ids |
| `{type}` | `movie` or `series` |
| `{name}`, `{year}`, `{season}`, `{episode}` | Title details |
| `{root}` | The library root the file is in |
| `{batch}`, `{batch_size}` | This run's batch id (e.g. `cinemind-job-4-20261107-150212`) and its number of files |

Example (a NAS ingest script that also adds subtitles; replace the paths with your own):

```toml
post_download_command = 'powershell.exe -NoProfile -Sta -ExecutionPolicy Bypass -WindowStyle Hidden -File "C:\path\to\Invoke-PostDownload.ps1" -Path "{path}" -StremioId "{id}" -CleanupRoot "{root}" -BatchId "{batch}" -BatchSize "{batch_size}"'
extra_tv_roots = ['\\nas\Media\TV Shows']
extra_movie_roots = ['\\nas\Media\Movies']
```

## Settings

`cinemind config show` lists everything. The ones you're most likely to change:

| Key | Default | |
|---|---|---|
| `tv_root`, `movie_root` | `Videos\TV Shows`, `Videos\Movies` | Library roots |
| `tv_template` | `{show}/Season {season:02}/{show} - {code} - {title}{ext}` | Also `{year}`, `{episode}` |
| `extra_tv_roots`, `extra_movie_roots` | (empty) | Other libraries checked for files you already have |
| `post_download_command` | (empty) | Run for each finished file (see [After a download](#after-a-download)) |
| `default_quality` | `1080p` | Used when the selection has no quality |
| `languages` | `en` | Comma-separated; `any` disables the filter |
| `stream_sources` | `internet-archive`, `stremio-addons` | See [Stream sources](#stream-sources) |
| `stream_addons` | (empty) | Stremio addon manifest URLs; a *configured* URL keeps its options |
| `max_active_torrents`, `max_active_http` | `3`, `2` | Downloads running at once |
| `seed_ratio` | `0` | Seed after finishing until this upload ratio |
| `outgoing_interfaces` | (empty) | Bind torrents to a VPN adapter so traffic can't leak |

## Development

```powershell
uv run pytest --cov                # offline: recorded responses, a local HTTP server and seeder
uv run ruff check src tests tools
uv run mypy
uv run pre-commit install          # run the checks on every commit
uv run python tools/make_icon.py   # redraw the icon (standard library only)
```

CI runs these on Windows and Alpine Linux for Python 3.12 and 3.13, and checks that a
plain Ubuntu install works without libtorrent. See [CONTRIBUTING.md](CONTRIBUTING.md).

| Package | Contents |
|---|---|
| `cinemind.core` | Config, data models, the SQLite store |
| `cinemind.sources` | The source contract and registry, the Internet Archive source, the Stremio addon client |
| `cinemind.planning` | Selection syntax, quality, filtering and ranking, the download plan, library paths |
| `cinemind.engines` | Direct HTTP and BitTorrent engines, run side by side |
| `cinemind.pipeline` | Running a plan, the post-download hand-off and its wrapper |
| `cinemind.cli` | Command line, home menu, wizard and terminal UI |

## A word of caution

The Internet Archive source only offers public-domain items. Stremio stream addons such as
Torrentio list torrents from public trackers, and much of what they list is copyrighted:
downloading it without permission is illegal in many countries, and torrent peers can see
your IP address. Only download content you have the right to.
