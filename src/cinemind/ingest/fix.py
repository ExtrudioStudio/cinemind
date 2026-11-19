"""The ffmpeg command that turns a FIXABLE file into one the TV plays directly.

Every stream is copied (``-c copy``) except the ones the verdict says to convert, and the
video is never re-encoded: that is the whole point of the pipeline (the NAS can't transcode, so
the TV must be able to play the video as it is). Audio is converted per track (for example DTS
to E-AC-3) with a title saying what it was, text subtitles become SRT, image subtitles and
extra video streams are left out, and the default flags follow the verdict.

A port of MediaPipeline v1's ``New-FixArguments``; the golden cases hold it to the same
arguments, in the same order.
"""

from __future__ import annotations

from cinemind.ingest.rules import Verdict


def fix_arguments(verdict: Verdict, input_path: str, output_path: str) -> list[str]:
    """ffmpeg arguments (without the program name) for a FIXABLE verdict."""
    if verdict.video_index is None:
        raise ValueError("A file with no video stream can't be fixed")
    kept_audio = [a for a in verdict.audio if a.action != "drop"]
    kept_subs = [s for s in verdict.subtitles if s.action != "drop"]

    args = ["-hide_banner", "-nostdin", "-nostats", "-loglevel", "error", "-y", "-i", input_path]
    args += ["-map", f"0:{verdict.video_index}"]
    for index in [a.index for a in kept_audio] + [s.index for s in kept_subs]:
        args += ["-map", f"0:{index}"]
    args += ["-map_metadata", "0", "-map_chapters", "0", "-c", "copy", "-disposition:v:0",
             "default"]  # fmt: skip
    for n, a in enumerate(kept_audio):
        if a.action == "convert":
            assert a.target and a.bitrate and a.out_channels
            args += [f"-c:a:{n}", a.target, f"-b:a:{n}", a.bitrate, f"-ac:a:{n}",
                     str(a.out_channels)]  # fmt: skip
            args += [f"-metadata:s:a:{n}", f"title={a.target.upper()} {a.out_channels}ch "
                                           f"(from {a.codec})"]  # fmt: skip
        args += [f"-disposition:a:{n}", "default" if a.make_default else "0"]
    for m, s in enumerate(kept_subs):
        if s.action == "srt":
            args += [f"-c:s:{m}", "srt"]
    args += ["-f", "matroska", output_path]
    return args
