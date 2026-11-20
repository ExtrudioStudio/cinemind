# 0004: Port the PowerShell pipeline to Python, proven by recorded verdicts

Status: accepted (Phase 5)

## Context

cinemind started as two programs:
- **The downloader:** Python, cross-platform, tested.
- **MediaPipeline v1:** Windows PowerShell 5.1 with inline C#, which checked, fixed,
  subtitled and filed what the downloader produced.

The downloader called v1 as a fire-and-forget command and never learned whether the file
reached the library. v1 had no tests. It needed Windows PowerShell 5.1 (DPAPI secrets,
WinForms), and its settings for one NAS and one TV were hard-coded.

v1's rules were right, though. They were tuned on real files and a real TV, and every rule
exists because some file failed to play. A rewrite that quietly changed a verdict would bring
back a bug that had already been fixed once.

## Decision

1. **Port, don't redesign, the rules.** `ingest/rules.py` and `ingest/fix.py` reproduce
   `Test-DirectPlay` and `New-FixArguments` line by line. Even the PowerShell quirks the
   verdicts depend on are kept: case-insensitive comparisons, half-even rounding, and the way
   numbers are formatted in messages.
2. **Prove it with recorded cases, not by reading code.** Before the port, v1 ran on 23 real
   library files and 37 synthetic probes, one per rule. Their probe JSON (headers only) and
   v1's output were saved as golden files (`tests/fixtures/ingest/golden/`). The parity test
   requires the same verdict, reasons, warnings and FFmpeg arguments, word for word.
3. **Port the signal processing with numpy.** The subtitle sync check (energy windows,
   correlation, z-scores, three frame-rate scales) keeps v1's thresholds, which were
   calibrated on real files. The tests are synthetic, so they can run in CI: generated speech
   bursts must give back a 7 s shift and a 25 fps stretch, and must reject another film's
   subtitle.
4. **Replace what was tied to Windows:**
   - DPAPI secret files become the system keyring (`cinemind secrets`).
   - The WinForms drop window becomes a headless review (ADR 0005).
   - Process-scanning job coordination becomes the `deliveries` table.

## Consequences

- The parity test earned its keep: it found a real porting bug. Pydantic's camel-case alias
  generator turned `max4k_fps` into `max4KFps`, so the 4K frame-rate limit was silently
  ignored. Reading the code would very likely have missed it.
- Two v1 behaviours were changed on purpose, and both are listed in the changelog:
  - The release-name parser strips only real file extensions. v1 read the group of
    `…WEB-DL` as `dl`.
  - When nothing fits, a headless result replaces the window.
- v1 stays as the reference. To change a rule, update the golden case (or add one) in the same
  change, so the parity test still documents the behaviour.
