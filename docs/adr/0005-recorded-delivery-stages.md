# 0005: Every file's delivery is a recorded state machine, reviewed by a person when needed

Status: accepted (Phase 5)

## Context

After a download, each file needs a subtitle that really matches its audio, then a check, a
fix if needed and filing on the NAS (ADR 0003). These steps take minutes per file. They depend
on outside services (subtitle sites, the share, Plex) and they fail in ordinary ways: a site
is down, the share is asleep, no subtitle fits.

v1 ran them as a detached script per file. When cinemind was interrupted, nothing knew which
step a file had reached. A missing subtitle opened a WinForms window that blocked the queue
until someone dropped a file on it, and nobody does that for a download that finished at 3 am.

## Decision

1. **One row per file in `state.db`** (`deliveries`, schema v4), moving through
   `pending → subtitling → ingesting → delivered | needs_review | quarantined | failed`.
   Each step is written before it starts and after it ends, along with attempts, the review
   reason, the error, and the ingest result as JSON.
2. **Every stage can run again.**
   - A subtitle already next to the video is kept.
   - A file already filed isn't filed twice.
   - A file that is no longer where the download left it is reported, never guessed at.
   - A job with unfinished deliveries is `paused`, so `cinemind resume` (or the MCP
     `resume_job`) finishes them after a crash or Ctrl+C.
3. **Subtitle first, then the ingest.** The audio check reads the local file quickly, and the
   ingest then moves the subtitle along with the video. A failed subtitle search never stops
   the file from being filed.
4. **Ask a person afterwards, not during.** `needs_review` replaces the window. The file is
   filed anyway, and `cinemind review` (or the MCP `list_reviews`) says what is missing and
   why. Then `review retry`, `review subtitle FILE` or `review dismiss` resolves it, or
   `resolve_review` does through MCP. A quarantined file can't be retried, only dismissed:
   it needs another release.
5. **Bounded and in-process.** Deliveries run in the downloader's own event loop, at most
   `max_delivery_jobs` at once. Large moves and the numpy work run in threads, so downloads
   keep going. The post-download command still exists: it runs after the delivery, on the
   file's new path.

## Consequences

- `cinemind status` shows where every file is, end to end (Done · Hand-off · Delivery), not
  just whether the bytes arrived.
- Nothing ever waits on a person. A night of downloads ends with a short review list rather
  than a dialog left open.
- The agent and the person see the same state, through the same table. An agent can retry or
  dismiss a delivery, but it can't mark a missing subtitle as found or move a quarantined file
  into the library.
- The ingest runs inside the downloader process. One process failing stops both, which is
  the price of a single source of truth. Each stage is recorded, so nothing is lost.
