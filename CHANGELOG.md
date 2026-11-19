# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

The project is now **cinemind** (it was stremio-dl). On the first run, your `config.toml`
and download history are copied from the stremio-dl folders, and the `sdl` command keeps
working for this release.

### Added
- **Ingest** (`cinemind ingest`): checks a file against a TV profile (Direct Play through
  Plex), places it as it is, fixes it with FFmpeg without re-encoding the video, or quarantines
  it with the reason. Plex names (reusing existing folders), a Plex section refresh, subtitle
  sidecars, a JSON-lines log and v1's exit codes (0/10/20/1). A port of the MediaPipeline v1
  PowerShell rules that matches all 60 recorded cases word for word.
- **Subtitles** (`cinemind subtitles`): searches Wizdom (Hebrew) and OpenSubtitles, then
  checks each candidate against the video's speech before saving it. A late subtitle, or one
  timed for 25 fps, is retimed; one for another cut or film is skipped. Saved as UTF-8 SRT
  (Windows-1255 Hebrew is converted). A numpy port of MediaPipeline v1's sync check.
- `cinemind secrets` stores tokens and passwords in the system keyring (or `CINEMIND_*`
  environment variables), never in config.toml.
- **MCP server** (`cinemind mcp`, `mcp` extra): an AI assistant can search, plan and, with a
  single-use plan id that expires, start downloads, then follow and pause them. Plans are
  limited by size, file count and library paths; `--read-only` offers no side-effect tools;
  every tool call is audited in `state.db`.
- **Plain-language requests** (`cinemind ask`, `ai` extra): Claude reads a request into a typed
  form that is resolved against the real season list. `get` falls back to it only when the
  selection parser can't read the text. Every LLM call is logged with its token usage.
- A headless service layer (`cinemind.service`) with a one-at-a-time background download queue.
- **Stream sources.** Downloads come from pluggable sources (`stream_sources`, and
  `cinemind sources` to list them). Other packages can add one through the `cinemind.sources`
  entry point.
- **Internet Archive source**, on by default: public-domain films (Public Domain Mark or CC0
  only) as direct downloads.
- **Direct HTTP downloads**: resumable with `Range`, size- and MD5-checked before the file is
  renamed into place, and run side by side with torrents. A failed download falls back to the
  next candidate, from any source.
- BitTorrent support is the optional `torrent` extra (`pip install "cinemind[torrent]"`), so
  cinemind installs on Linux distributions that have no libtorrent wheel.
- CI on Windows and Alpine Linux for Python 3.12 and 3.13: tests, coverage, ruff and mypy,
  plus an Ubuntu job without libtorrent.
- CodeQL scanning, Dependabot updates and pre-commit hooks.
- MIT license, contributing guide, security policy, and issue and pull request templates.
- `--type` also accepts `tv`, `show` and `film`.
- `cinemind status` has a **Hand-off** column: how each job's post-download commands ended
  (`2 ok, 1 failed`, `1 running`). Every run is recorded with its exit code and log file.
- The post-download command also gets its values as environment variables
  (`CINEMIND_PATH`, `CINEMIND_ID`, `CINEMIND_TYPE`, `CINEMIND_NAME`, `CINEMIND_YEAR`,
  `CINEMIND_SEASON`, `CINEMIND_EPISODE`, `CINEMIND_ROOT`, `CINEMIND_BATCH`,
  `CINEMIND_BATCH_SIZE`).

### Fixed
- Error messages no longer lose bracketed text such as `cinemind[torrent]` (Rich markup).
- A file that finished downloading but couldn't be renamed into place (it stays `.part`) is
  now marked **failed**. Before, the job ended as "done" and the file was silently skipped.
- An error while recording progress (for example a locked database) no longer stops every
  running torrent; it is logged and the downloads continue.
- The post-download command is split into arguments before the placeholders are filled in. A
  title or path with quotes, spaces or a trailing backslash can no longer split or merge
  arguments, and an unbalanced quote is reported instead of crashing the download.
- Creating a download job is one transaction: a failure part-way leaves nothing behind.
- `state.db` uses WAL mode with a busy timeout, so `cinemind status` can run during a download. The
  schema is versioned and older databases are upgraded in place.
- Library paths are checked to stay inside the library root.
- An unknown `--type` value is now a usage error. Before, it was sent to the addons.
- An addon response that is valid JSON but not an object (a list, `null`, a number) now fails
  with a clear `AddonError`. List fields that contain something other than objects are skipped
  instead of crashing the search or the stream lookup.
- A request cancelled while several addons are queried now cancels the caller, instead of being
  logged as one failed addon.
- The engine tests no longer race for a free port.

### Changed
- **No torrent source is configured by default.** `stream_addons` used to default to
  Torrentio; it is now empty, and setting it prints a legal notice. Existing configs keep
  their value.
- At the same resolution, a direct download is preferred to torrents (it needs no peers).
- The package is laid out by layer: `core`, `sources`, `planning`, `engines`, `pipeline`, `cli`.
- Requires tenacity 9.2 or later.
- The default User-Agent says what it is: `Mozilla/5.0 (compatible; cinemind/<version>; +<repo>)`.
  Existing configs keep the value they already have.
- Values in the post-download command keep their double quotes (they used to be changed to
  single quotes).

## [0.1.0] - 2026-11-19

Released as stremio-dl, with the `sdl` command.

### Added
- Search Cinemeta, pick episodes with selections such as `s1 eps10 - s2 eps 3 /full hd`, and
  download the best-seeded Torrentio streams with libtorrent, straight into a
  `Show/Season NN/` or `Movie (Year)/` library.
- Fallback to the next candidate when a torrent has no metadata or stalls; Ctrl+C pauses and
  `sdl resume` continues.
- A home menu, a desktop and Start menu shortcut, and download history.
- `post_download_command` for each finished file, with batch placeholders, plus `sdl handoff`.
- `extra_tv_roots` / `extra_movie_roots`, so files already moved elsewhere aren't downloaded again.

[Unreleased]: https://github.com/ExtrudioStudio/cinemind/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ExtrudioStudio/cinemind/releases/tag/v0.1.0
