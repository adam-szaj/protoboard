#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Headless edit-CLI — drives the same EditController verbs as the GUI.

The MVC acceptance front-end: builds a HeadlessSession + EditController and maps
a line grammar onto the edit verbs (see ``protoboard.cli`` for the grammar).
Reads a batch script file, or falls back to an interactive readline loop.

Usage::

    tools/edit_cli.py script.txt          # run a batch script
    tools/edit_cli.py                      # interactive (readline)

PDF/PNG export needs Qt's paint device; run under an offscreen platform::

    QT_QPA_PLATFORM=offscreen tools/edit_cli.py script.txt
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running directly from a source checkout without installation.
_THIS = Path(__file__).resolve()
_REPO = _THIS.parent.parent
sys.path.insert(0, str(_REPO / "src"))

from protoboard.cli import CliDriver, CliError  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Headless board edit CLI.")
    parser.add_argument(
        "script", nargs="?", type=Path,
        help="batch script of grammar lines; omit for interactive mode",
    )
    args = parser.parse_args(argv)

    # A QApplication makes the Qt export device (PDF/PNG, which build a
    # QGraphicsScene) usable headlessly; harmless when no export runs. Skip
    # cleanly if PySide is absent. Run under QT_QPA_PLATFORM=offscreen.
    try:
        from PySide6.QtWidgets import QApplication

        if QApplication.instance() is None:
            QApplication([])
    except Exception:  # noqa: BLE001
        pass

    driver = CliDriver.new()

    if args.script is not None:
        lines = args.script.read_text().splitlines()
        try:
            for line in lines:
                out = driver.execute(line.strip())
                if out:
                    print(out)
        except CliError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        return 0

    # Interactive readline loop.
    print("edit-cli — type commands, Ctrl-D to quit")
    while True:
        try:
            line = input("> ")
        except EOFError:
            print()
            break
        try:
            out = driver.execute(line.strip())
            if out:
                print(out)
        except CliError as e:
            print(f"error: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
