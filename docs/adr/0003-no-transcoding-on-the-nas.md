# 0003: Files are made playable before they reach the library; the server never transcodes

Status: accepted (Phase 5; the rule dates from MediaPipeline v1)

## Context

The library lives on an entry-level ARM NAS with 2 GB of RAM. Plex runs on it, and a TV plays
from it through the Plex app. The NAS's CPU can't transcode video in real time, so Plex has
video transcoding turned off, and anything the TV can't decode itself simply fails to play,
usually halfway through an evening.

The TV has its own limits. It has no DTS and no Dolby Vision, it can't play 10-bit H.264
(Hi10P) or H.264 above 30 fps at 4K, and image subtitles (PGS, VobSub) can make Plex burn
them into the picture, which is a transcode. Releases vary widely, and a file name doesn't
say which of these a file breaks.

## Decision

1. **Check every file against a device profile before it enters the library**
   (`ingest/rules.py`, `ingest/profiles/samsung-q60b.json`). The check reads the headers with
   ffprobe (a few kilobytes, even over SMB) and gives one of three verdicts:
   - **COMPLIANT:** moved in as it is.
   - **FIXABLE:** remuxed on the laptop.
   - **REJECT:** quarantined with the reason.
2. **Never re-encode video.** The fix is limited to copying video. It may convert audio (DTS to
   E-AC-3), turn text subtitles into SRT, drop image subtitles and extra streams, and change
   the container. Video the TV can't decode is never "fixed" by re-encoding it. That costs
   quality and an hour of laptop time per film, and a better release is usually one search
   away.
3. **Prove the fix before it replaces anything.** The remuxed file is probed again. It must
   be COMPLIANT and within 2 s of the source's duration, or the source stays where it was.
4. **The profile is data.** Another TV means another JSON file (`ingest_profile`), not a code
   change.

## Consequences

- What reaches the library plays directly, without the server doing any work. Playback
  doesn't depend on the NAS's CPU at all.
- Some releases are refused outright (Hi10P anime, Dolby Vision profile 5, VC-1). The
  quarantine reason says why, and `cinemind review` lists them, so the fix is to download a
  different release, not to debug the TV.
- The laptop does the probing and remuxing, and it reads and writes the share over SMB.
  Measured with SMB signing on, that runs at about 30-40 MB/s, so a remux is written next to
  its target on the share. The final step is then a rename, not a second copy.
- `max_delivery_jobs` defaults to 2, because a weak NAS slows down sharply when more files are
  written at once.
