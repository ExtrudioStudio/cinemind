# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- CI on Windows and Alpine Linux for Python 3.12 and 3.13: tests, coverage, ruff and mypy.
- CodeQL scanning, Dependabot updates and pre-commit hooks.
- MIT license, contributing guide, security policy, and issue and pull request templates.
- `--type` also accepts `tv`, `show` and `film`.

### Fixed
- An unknown `--type` value is now a usage error. Before, it was sent to the addons.
- An addon response that is valid JSON but not an object (a list, `null`, a number) now fails
  with a clear `AddonError`. List fields that contain something other than objects are skipped
  instead of crashing the search or the stream lookup.
- A request cancelled while several addons are queried now cancels the caller, instead of being
  logged as one failed addon.
- The engine tests no longer race for a free port.

### Changed
- Requires tenacity 9.2 or later.

## [0.1.0] - 2026-11-19

### Added
- Search Cinemeta, pick episodes with selections such as `s1 eps10 - s2 eps 3 /full hd`, and
  download the best-seeded Torrentio streams with libtorrent, straight into a
  `Show/Season NN/` or `Movie (Year)/` library.
- Fallback to the next candidate when a torrent has no metadata or stalls; Ctrl+C pauses and
  `sdl resume` continues.
- A home menu, a desktop and Start menu shortcut, and download history.
- `post_download_command` for each finished file, with batch placeholders, plus `sdl handoff`.
- `extra_tv_roots` / `extra_movie_roots`, so files already moved elsewhere aren't downloaded again.

[Unreleased]: https://github.com/ExtrudioStudio/stremio-dl/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ExtrudioStudio/stremio-dl/releases/tag/v0.1.0
