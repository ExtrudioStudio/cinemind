# Ingest golden fixtures

Each `golden/*.json` file is one case for the Direct Play rules: an ffprobe result and the verdict
the original PowerShell implementation (MediaPipeline v1, `Test-DirectPlay` and
`New-FixArguments`) gave for it. The Python port must produce the same verdict, reasons, actions,
warnings and ffmpeg arguments.

| Field | Meaning |
|---|---|
| `kind` | `real`: probed from a real file. `synthetic`: built by hand to reach one rule branch. |
| `extension` | The file extension the rules saw. |
| `probe` | ffprobe JSON for the entries the rules read: codec fields, dispositions, Dolby Vision side data and the `language` tag. File names and every other tag are left out. |
| `expected.fixArgs` | ffmpeg arguments for `FIXABLE` cases, with `INPUT` and `OUTPUT` as path placeholders. |

The fixtures were generated on 2026-11-19 by `tools/Export-GoldenFixtures.ps1` in MediaPipeline v1,
with the Samsung Q60B profile (`directplay-profile.json`). They record how v1 behaved, not a
specification: where the port deliberately changes behaviour, update the case and explain why in
the commit.
