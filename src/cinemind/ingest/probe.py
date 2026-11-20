"""Run ffprobe (headers only) and ffmpeg, and find them.

``probe`` asks ffprobe for exactly the entries the rules read: codec fields, dispositions,
Dolby Vision side data and the ``language`` tag. Only the headers are read, so probing a file
on a NAS costs a few kilobytes, not the whole file.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENTRIES = (
    "format=format_name,duration,bit_rate"
    ":stream=index,codec_name,codec_type,profile,width,height,pix_fmt,bits_per_raw_sample,"
    "avg_frame_rate,r_frame_rate,color_transfer,channels"
    ":stream_disposition=default,forced,attached_pic"
    ":stream_tags=language"
    ":stream_side_data=side_data_type,dv_profile,dv_bl_signal_compatibility_id"
)
INSTALL_HINT = "Install FFmpeg (Windows: winget install Gyan.FFmpeg; Linux: apt/apk install ffmpeg)"


class ToolMissing(RuntimeError):
    pass


class ToolFailed(RuntimeError):
    pass


def find_tool(name: str, override: str = "") -> str:
    """The path to ffmpeg or ffprobe: the configured one, or the first on PATH."""
    if override:
        if Path(override).is_file():
            return override
        raise ToolMissing(f"{name} not found at {override!r}")
    found = shutil.which(name)
    if found:
        return found
    raise ToolMissing(f"{name} not found on PATH. {INSTALL_HINT}.")


@dataclass
class Completed:
    returncode: int
    stdout: bytes
    stderr: bytes


async def run_tool(program: str, *args: str) -> Completed:
    """Run a tool without a console window, capturing its output."""
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    proc = await asyncio.create_subprocess_exec(
        program, *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, creationflags=flags,
    )  # fmt: skip
    try:
        stdout, stderr = await proc.communicate()
    except asyncio.CancelledError:  # paused or stopped: don't leave FFmpeg running on its own
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise
    return Completed(proc.returncode or 0, stdout, stderr)


async def probe(path: Path, ffprobe: str) -> dict[str, Any]:
    """ffprobe's JSON for the entries the rules need."""
    done = await run_tool(ffprobe, "-v", "error", "-print_format", "json",
                          "-show_entries", ENTRIES, "-i", str(path))  # fmt: skip
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip()
        raise ToolFailed(f"ffprobe failed (exit {done.returncode}): {detail}")
    data: dict[str, Any] = json.loads(done.stdout.decode("utf-8", "replace") or "{}")
    return data
