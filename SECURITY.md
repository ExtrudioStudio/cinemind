# Security policy

## Supported versions

Only the latest release gets security fixes.

## Reporting a vulnerability

Please report it privately with GitHub's **Security → Report a vulnerability** button on this
repository. Don't open a public issue for it. You should get an answer within a week.

Include the version (`sdl --version`), your OS, and steps to reproduce.

## Areas that matter most

- **`post_download_command`.** It runs a command you configure with values (titles, paths) that
  come from remote addons. Anything that lets an addon response change which program runs, or
  add arguments to it, is a vulnerability.
- **Library paths.** File and folder names are built from addon metadata and torrent file
  lists. Any way to write outside the configured library roots is a vulnerability.
- **Addon responses.** Addons are third-party servers. A malformed or hostile response should
  fail with an error, not crash the app or corrupt state.
