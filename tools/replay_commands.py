#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Headless replay tool for ``session_commands.yaml`` files.

Loads a project directory plus its recorded command log, replays every
event against a :class:`ReplayState`, then prints a concise summary
(executed / undone / redone / skipped counts and final state stats).

Usage::

    tools/replay_commands.py <project_dir> [log_path]

When *log_path* is omitted, defaults to
``<project_dir>/session_commands.yaml``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running directly from a source checkout without installation.
_THIS = Path(__file__).resolve()
_REPO = _THIS.parent.parent
sys.path.insert(0, str(_REPO / "src"))

from protoboard.paths import BUILTIN_LIBRARY_DIR  # noqa: E402
from protoboard.services.library.loader import load_library  # noqa: E402
from protoboard.services.recording import ReplayState, replay_commands  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0] if __doc__ else None
    )
    parser.add_argument("project_dir", type=Path, help="Project directory to load")
    parser.add_argument(
        "log_path",
        type=Path,
        nargs="?",
        default=None,
        help="Path to session_commands.yaml (defaults to <project_dir>/session_commands.yaml)",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Raise on the first failed event instead of skipping it",
    )
    parser.add_argument(
        "--library",
        type=Path,
        default=None,
        help="Additional component library directory",
    )
    args = parser.parse_args(argv)

    log_path = args.log_path or (args.project_dir / "session_commands.yaml")
    if not log_path.exists():
        print(f"error: recording not found: {log_path}", file=sys.stderr)
        return 2

    library_dirs = [BUILTIN_LIBRARY_DIR] if BUILTIN_LIBRARY_DIR.exists() else []
    if args.library is not None:
        library_dirs.append(args.library)
    library = load_library(library_dirs)

    state = ReplayState.from_project(args.project_dir, library)
    result = replay_commands(state, log_path, stop_on_error=args.stop_on_error)

    print(f"replay: {log_path}")
    print(f"  executed: {result.executed}")
    print(f"  undone:   {result.undone}")
    print(f"  redone:   {result.redone}")
    print(f"  skipped:  {len(result.skipped)}")
    for idx, msg in result.skipped:
        print(f"    [{idx}] {msg}", file=sys.stderr)
    print("final state:")
    print(f"  placements: {len(state.layout.placements)}")
    print(f"  wires:      {len(state.layout.wires)}")
    print(f"  nets:       {len(state.schematic.nets)}")
    print(f"  groups:     {len(state.layout.groups)}")

    return 0 if not result.skipped else 1


if __name__ == "__main__":
    sys.exit(main())
