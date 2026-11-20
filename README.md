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

You can also drive it from an AI assistant: `cinemind mcp` is an
[MCP](https://modelcontextprotocol.io) server, so Claude can search, show you a plan, and
download only after you agree. `cinemind ask "the last two seasons of Severance in 4K"` takes
plain language (see [AI features](#ai-features)).

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

Installing from a wheel or PyPI: `pip install cinemind` for direct downloads, plus extras as
needed. `torrent` adds BitTorrent; libtorrent has wheels for Windows, macOS and Alpine (musl)
Linux, and on other distributions cinemind works without it. `ai` adds plain-language
requests, `mcp` adds the MCP server, and `all` adds everything:
`pip install "cinemind[all]"`.

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
| `cinemind ask "REQUEST"` | Find, plan and download from plain language (see [AI features](#ai-features)) |
| `cinemind mcp` | Run the MCP server for an AI assistant (see [AI features](#ai-features)) |
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

## AI features

Plain-language requests and the MCP server's `plan_request` use Claude, which needs Anthropic
API credentials (`ANTHROPIC_API_KEY`, or `ant auth login`). The pack-file judge runs on a
local model by default. Without any of them everything else works, and the selection syntax
above still does.

### Plain-language requests

```powershell
cinemind ask "the last two seasons of Severance in 4K" --dry-run
cinemind get "Friends" "the very first season in hd"     # words the parser can't read
```

```
Read as Severance (2022) s2-s3 /2160p
```

The deterministic parser always goes first, and Claude is only called when it can't read the
text. Even then, the model doesn't pick episodes or files. It fills in a small typed form
(title, scope such as "last 2 seasons", quality), and cinemind resolves that against the
title's real season list. The result is checked by the same parser and shown as selection
syntax ("Read as ..."), so you can see and reuse exactly what it understood. Every call is
logged with its token usage in `state.db` (`llm_calls`). The model is `llm_model`
(`claude-opus-5-5` by default, at `llm_effort` `low`).

### Which file is the episode? The pack-file judge

Sources usually say which file of a season pack is which episode. When they don't, cinemind
reads the file names: `S01E05`, `1x05`. Anime batches numbered across seasons
(`[Group] Show - 29 [1080p].mkv` for S02E05), files named only by the episode's title, season
folders (`Season 2/05.mkv`) and two-part finales released under one number all defeat that.
Before, the episode was dropped and re-planned onto another release.

Now those files go to a judge. It sees the episode wanted (with its title and its absolute
number), the torrent's name and its files, and it can only answer with one of those files or
"none": the answer's schema is an enum of their ids. An unsure answer counts as no answer,
because a wrong file downloads the wrong episode, while no file only costs a re-plan. The names
reach the model as data in a JSON document, never as instructions. The files it has placed
download meanwhile.

```powershell
# local and free (the default): install Ollama, then
ollama pull qwen2.5:7b
cinemind config set judge_provider anthropic    # or Claude (llm_model), or "off"
```

How well it works is measured, not assumed. [The eval](docs/evals.md) runs 61 reviewed cases
through the same code the engine uses, and scores wrong picks separately from misses:

| | correct | wrong picks | present episodes found |
|---|---|---|---|
| name rules only | 33% | 3.3% | 20% |
| + judge on `qwen2.5:7b` (local) | 93% | 6.6% | 96% |

The wrong picks are near misses, all answered with high confidence. One is a special taken for
S01E01; another is a shorter title taken for the full one. Checks for those, and Claude as the
comparison, are next.

### Use from an AI assistant (MCP)

`cinemind mcp` runs an [MCP](https://modelcontextprotocol.io) server on stdio.
`cinemind mcp --print-config` prints the entry for Claude Desktop's
`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "cinemind": { "command": "C:\\path\\to\\.venv\\Scripts\\python.exe", "args": ["-m", "cinemind", "mcp"] }
  }
}
```

| Tool | |
|---|---|
| `search_titles`, `title_info` | Find a title and its seasons |
| `plan_download`, `plan_request` | Build a plan from a selection or a plain-language request. Downloads nothing. |
| `start_download` | Start a plan by its `plan_id` |
| `job_status`, `list_jobs`, `pause_job`, `resume_job` | Follow and control jobs |
| `list_reviews`, `resolve_review` | Files that need you after their download; retry or dismiss one |

Guardrails:

- **Two-phase confirmation.** `start_download` only accepts a `plan_id` from a plan made in
  the same session. It works once and expires after `mcp_plan_ttl_minutes` (15), so the
  assistant has to show you a current plan before anything downloads.
- **Limits.** Plans over `mcp_max_plan_gb` (60) or `mcp_max_files` (60) are refused, and
  every file must land inside your library roots.
- **Read-only mode.** `cinemind mcp --read-only` doesn't offer the tools that change anything.
- **Audit.** Every tool call is recorded in `state.db` (`tool_calls`) with its outcome.
- **Untrusted text.** Titles and release names come from third parties. The server's
  instructions tell the assistant to treat them as data, never as instructions.

Downloads run in the background, one job at a time, while the server runs. If the
assistant closes the server, the running job is paused with its progress saved, and
`cinemind resume` (or `resume_job`) continues it.

## Ingest: make every file play directly on the TV

`cinemind ingest` gets a file ready for a weak NAS. The NAS only stores and serves files,
Plex never transcodes video, and the TV decodes everything itself. So each file is checked
against a **device profile** (what the TV direct-plays through Plex) before it reaches the
library:

| Verdict | What happens | Exit |
|---|---|---|
| COMPLIANT | Moved into place as it is (a rename on the same share: no data copied) | 0 |
| FIXABLE | Remuxed with FFmpeg: **video copied, never re-encoded**. Audio converted (DTS → E-AC-3), text subtitles made SRT, image subtitles and extra streams dropped. The output is checked again (rules and duration) before it replaces anything. | 10 |
| REJECT | Moved to the quarantine folder with a `.reason.txt` (Hi10P, Dolby Vision profile 5, VC-1, …): find another release | 20 |

```powershell
cinemind config set ingest_root '\\nas\Media'       # with Movies, TV Shows, _incoming, _quarantine
cinemind config set plex_url http://nas:32400
cinemind secrets set plex                           # the X-Plex-Token, into the system keyring
cinemind ingest job42.mkv.part --kind episode --title "One Piece" --year 1999 --tvdb 81797 --season 1 --episode 1
```

Files get Plex names (`TV Shows/One Piece (1999) {tvdb-81797}/Season 01/...`), and an
existing show or movie folder is reused. The Plex section that holds the folder is refreshed,
and subtitle sidecars move with the file. `--dry-run` reports without changing anything, and
`--json` prints a machine-readable result. Every run is logged as a JSON line.

The rules are a port of the PowerShell pipeline this project started as. All 60 recorded
cases from it (23 real files, 37 synthetic, one per rule) give the same verdict, word for word,
including the exact FFmpeg arguments. The bundled profile is `samsung-q60b`; set
`ingest_profile` to a JSON file of your own for another TV.

## Subtitles that match the audio

A subtitle with the right name can still be for another cut, or a few seconds late.
`cinemind subtitles` doesn't trust names alone: it checks each candidate against the video's
**speech**.

```powershell
cinemind subtitles "Film (2010).mkv" --imdb tt1375666 --lang he
cinemind subtitles episode.mkv --imdb tt0388629 --season 1 --episode 2   # subtitle_language
```

1. Candidates come from Wizdom (Hebrew, no account) and OpenSubtitles (any language; set
   `opensubtitles_username`, then `cinemind secrets set opensubtitles_api_key` and
   `opensubtitles_password`). They are ranked by file-hash match, then by how close the
   release name is to the video's. Cam releases are never downloaded.
2. FFmpeg decodes a few 5-minute windows of the dialogue track (only the centre channel of a
   5.1 mix). The subtitle's on/off timeline is slid across their speech energy, at the
   original speed and at both 23.976 ↔ 25 fps speeds.
3. A subtitle that lines up at the same shift in at least half the windows is saved as UTF-8
   SRT next to the video (`Film (2010).he.srt`), **retimed** if it was early, late or at PAL
   speed. One for another cut or film doesn't line up and is skipped.

With no FFmpeg (or `--no-sync-check`), only a candidate with a matching hash or release name
is used. When nothing fits, the exit code is 1 and the output lists what was tried and why.

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

### Subtitle, ingest, review

Turn on the stages you want, and every finished file goes through them on its own:

```powershell
cinemind config set auto_subtitles true   # a subtitle that matches the audio (see above)
cinemind config set auto_ingest true      # check, fix and file it for Plex (needs ingest_root)
```

Each file moves through `pending → subtitling → ingesting`, and ends as one of:
- `delivered`;
- `needs_review`: no subtitle matched, but the file is filed anyway;
- `quarantined`: the release can't play directly on the TV;
- `failed`: the share was unreachable, for example.

Every step is recorded in `state.db`. If cinemind stops half-way (Ctrl+C, a crash, a reboot),
the job stays `paused` and `cinemind resume` finishes the files from the step they were at.
At most `max_delivery_jobs` (2) files are processed at once, next to the running downloads.

Nothing waits for you in the middle of the night. What needs you is listed afterwards:

```powershell
cinemind review                         # what needs you, and why
cinemind review retry 12 S01E03         # run it again, e.g. once the share is back
cinemind review subtitle 12 S01E03 C:\Downloads\found.srt   # use a subtitle you found
cinemind review dismiss 12 S01E04       # you've dealt with it
cinemind deliver 9                      # files downloaded before the stages were on
```

`cinemind status` has a **Delivery** column (`3 delivered, 1 needs review`). An AI assistant
sees the same list through the MCP tools `list_reviews` and `resolve_review`. The design is in
[ADR 0005](docs/adr/0005-recorded-delivery-stages.md).

### Your own command

Set `post_download_command` to run a command for every finished file, for example a script
that checks it and moves it to a NAS. With the stages above turned on, it runs after them, on
the file where it ended up. It starts only once the download is complete (a season pack hands
over all its episodes at the end). It runs in the background, in its own hidden
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
| `llm_enabled`, `llm_model`, `llm_effort` | `true`, `claude-opus-5-5`, `low` | Plain-language requests |
| `judge_provider`, `judge_model`, `ollama_url` | `ollama`, `qwen2.5:7b`, `http://localhost:11434` | The [pack-file judge](#which-file-is-the-episode-the-pack-file-judge) (`anthropic` uses `llm_model`; `off`) |
| `judge_min_confidence` | `medium` | Below this the judge abstains |
| `mcp_max_plan_gb`, `mcp_max_files`, `mcp_plan_ttl_minutes` | `60`, `60`, `15` | Limits for plans made through the MCP server |
| `ingest_root`, `plex_url`, `ingest_profile` | (empty), (empty), `samsung-q60b` | See [Ingest](#ingest-make-every-file-play-directly-on-the-tv) |
| `subtitle_language`, `opensubtitles_username` | `en`, (empty) | See [Subtitles](#subtitles-that-match-the-audio) |
| `auto_subtitles`, `auto_ingest`, `max_delivery_jobs` | `false`, `false`, `2` | See [After a download](#subtitle-ingest-review) |

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
| `cinemind.service` | Headless find, plan and background downloads, shared by the CLI and the MCP server |
| `cinemind.ai` | The Claude wrapper (structured output, error mapping, usage log) and request reading |
| `cinemind.mcp` | The MCP server and its guardrails |
| `cinemind.cli` | Command line, home menu, wizard and terminal UI |

Design decisions are recorded in [docs/adr](docs/adr).

## A word of caution

The Internet Archive source only offers public-domain items. Stremio stream addons such as
Torrentio list torrents from public trackers, and much of what they list is copyrighted:
downloading it without permission is illegal in many countries, and torrent peers can see
your IP address. Only download content you have the right to.
