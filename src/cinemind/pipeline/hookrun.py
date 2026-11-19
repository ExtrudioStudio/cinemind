"""Run one post-download command and record how it ended: ``python -m cinemind.pipeline.hookrun``.

:func:`cinemind.pipeline.postprocess.hand_off` starts this wrapper detached, with its output going
to the hand-off log. The wrapper runs the real command, waits for it, and writes the exit code to
the ``handoffs`` table. It keeps going after ``cinemind`` itself has quit, so the result is recorded
either way.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from cinemind.core.store import Store

NOT_STARTED = 127  # the shell convention for "command not found"


def _stamp() -> str:
    return f"{datetime.now():%Y-%m-%d %H:%M:%S}"


def run(command: list[str]) -> tuple[int | None, str | None]:
    """Run ``command`` with inherited output; return (exit code, error starting it)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console window on Windows
    try:
        return subprocess.call(command, stdin=subprocess.DEVNULL, creationflags=flags), None
    except OSError as exc:
        return None, f"could not start {command[0]!r}: {exc}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cinemind.pipeline.hookrun")
    parser.add_argument("--db", type=Path, help="state.db to record the result in")
    parser.add_argument("--handoff", type=int, help="row id in the handoffs table")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- program [args...]")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no command given")

    print(f"# {_stamp()} running: {subprocess.list2cmdline(command)}", flush=True)
    code, error = run(command)
    print(f"# {_stamp()} {error or f'exit code {code}'}", flush=True)

    if args.db is not None and args.handoff is not None:
        store = Store(args.db)
        try:
            store.finish_handoff(args.handoff, code, error)
        finally:
            store.close()
    return NOT_STARTED if code is None else code


if __name__ == "__main__":
    sys.exit(main())
