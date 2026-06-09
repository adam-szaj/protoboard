#!/usr/bin/env python
# SPDX-License-Identifier: MIT
"""Emit each synthetic bench preset to disk as a load_project()-compatible folder.

Bench fixtures are normally consumed in-memory (see
``tests/benchmarks/conftest.py``), so this script is only useful when you
want to open a fixture in the GUI for visual inspection or hand-trace a
quirky route.  Output goes under
``tests/fixtures/synthetic_bench/<spec.name>/``.

Usage::

    uv run python -m tests.fixtures.synthetic_bench.regenerate            # all
    uv run python -m tests.fixtures.synthetic_bench.regenerate small medium
"""
from __future__ import annotations

import sys
from pathlib import Path

from protoboard.services.persistence.project_yaml import save_project

from tests.fixtures.synthetic_bench.generator import (
    LARGE,
    MEDIUM,
    PLATED_THROUGH,
    SMALL,
    STRIPBOARD_MIXED,
    generate_project,
)

_ALL = {
    s.name: s for s in (SMALL, MEDIUM, LARGE, PLATED_THROUGH, STRIPBOARD_MIXED)
}


def main(argv: list[str]) -> int:
    names = argv or list(_ALL)
    out_root = Path(__file__).parent
    for name in names:
        spec = _ALL.get(name)
        if spec is None:
            print(f"unknown spec: {name!r} (known: {list(_ALL)})", file=sys.stderr)
            return 2
        bundle = generate_project(spec)
        out_dir = out_root / name
        out_dir.mkdir(exist_ok=True)
        save_project(
            directory=out_dir,
            board=bundle.board,
            layout=bundle.layout,
            schematic=bundle.schematic,
            instances=bundle.instances,
            routing_settings=bundle.routing,
        )
        print(f"  wrote {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
