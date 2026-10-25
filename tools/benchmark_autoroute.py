#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run autoroute on a fixture under a matrix of settings and snapshot metrics.

Usage:
    uv run python tools/benchmark_autoroute.py FIXTURE_DIR [--commit SHA]
                                              [--quick]
                                              [--out PATH]

For each combination of `wire_mode × max_rip_up_passes × max_optimize_iterations`
the tool runs `autoroute()` from a clean layout (the fixture's pre-routed wires
are dropped before each run) and records routing quality metrics — completion
rate, redundant-wire count, wire/MST ratio, hull ratio — into a JSON file
committed alongside the fixture.

The default matrix is 4 × 5 × 5 = 100 runs.  Use `--quick` to do a single run
with the fixture's own routing.yaml defaults, for a smoke check.

Output path defaults to `<FIXTURE_DIR>/benchmarks/<sha>.json` so that commits
can carry both the algorithm change and its measured impact.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

# Make `protoboard` and `tests` importable when run as a script.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

from tests.autoroute_metrics import RunSummary, compute_metrics  # noqa: E402

from protoboard.paths import BUILTIN_LIBRARY_DIR  # noqa: E402
from protoboard.services.library.loader import load_library  # noqa: E402
from protoboard.services.persistence.project_yaml import load_project  # noqa: E402
from protoboard.services.routing.autorouter import autoroute  # noqa: E402

WIRE_MODES = ["insulated", "auto", "bare", "bare_diagonal"]
RIPUP_PASSES = [0, 2, 3, 5, 8]
OPTIMIZE_ITERS = [0, 2, 3, 5, 8]


def _git_short_sha(repo: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short=8", "HEAD"],
            cwd=str(repo),
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except subprocess.CalledProcessError:
        return "uncommitted"


def _git_iso_commit_date(repo: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "show", "-s", "--format=%cI", "HEAD"],
            cwd=str(repo),
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except subprocess.CalledProcessError:
        return datetime.now(UTC).isoformat()


def _run_one(
    board,
    layout_clean,
    schematic,
    components,
    instances,
    cost_model,
    wire_mode: str,
    ripup: int,
    optimize: int,
) -> RunSummary:
    t0 = time.perf_counter()
    result = autoroute(
        board,
        layout_clean,
        schematic,
        components,
        instances,
        cost_model=cost_model,
        wire_mode=wire_mode,  # type: ignore[arg-type]
        max_rip_up_passes=ripup,
        max_optimize_iterations=optimize,
    )
    elapsed = time.perf_counter() - t0
    return compute_metrics(
        board=board,
        layout=layout_clean,
        schematic=schematic,
        components=components,
        instances=instances,
        wires=list(result.wires),
        wire_mode=wire_mode,
        max_rip_up_passes=ripup,
        max_optimize_iterations=optimize,
        elapsed_s=elapsed,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path, help="Path to fixture directory")
    parser.add_argument(
        "--commit", default=None, help="Override commit SHA (default: git HEAD short)"
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Single run with fixture's own routing.yaml defaults",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output JSON path " "(default: <fixture>/benchmarks/<sha>.json)",
    )
    args = parser.parse_args()

    fixture: Path = args.fixture.resolve()
    if not fixture.exists():
        print(f"Fixture not found: {fixture}", file=sys.stderr)
        return 2

    sha = args.commit or _git_short_sha(_REPO)
    commit_date = _git_iso_commit_date(_REPO)
    out_path: Path = args.out or fixture / "benchmarks" / f"{sha}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Loading project: {fixture}", file=sys.stderr)
    lib = load_library([BUILTIN_LIBRARY_DIR])
    board, layout, schematic, instances, routing, _, _ = load_project(fixture)
    components = {name: lib.get(name) for name in lib.names()}
    cost_model = routing.cost_model()
    layout_clean = replace(layout, wires=[])

    if args.quick:
        combos = [
            (
                routing.wire_mode,
                routing.max_rip_up_passes,
                routing.max_optimize_iterations,
            )
        ]
    else:
        combos = [
            (wm, ru, op)
            for wm in WIRE_MODES
            for ru in RIPUP_PASSES
            for op in OPTIMIZE_ITERS
        ]

    print(f"Commit: {sha} ({commit_date})", file=sys.stderr)
    print(f"Running {len(combos)} combinations on '{fixture.name}'", file=sys.stderr)

    runs: list[RunSummary] = []
    t_total = time.perf_counter()
    for idx, (wm, ru, op) in enumerate(combos, start=1):
        t0 = time.perf_counter()
        summary = _run_one(
            board,
            layout_clean,
            schematic,
            components,
            instances,
            cost_model,
            wm,
            ru,
            op,
        )
        dt = time.perf_counter() - t0
        runs.append(summary)
        print(
            f"  [{idx:>3}/{len(combos)}] mode={wm:<14} ripup={ru} opt={op}: "
            f"routed={summary.routed_nets}/{summary.total_nets} "
            f"wires={summary.total_wires} "
            f"redundant={summary.redundant_wires_total} "
            f"len={summary.total_length:.0f} "
            f"({dt:.1f}s)",
            file=sys.stderr,
        )

    total_dt = time.perf_counter() - t_total
    print(f"Total time: {total_dt:.1f}s", file=sys.stderr)

    payload = {
        "meta": {
            "commit": sha,
            "commit_date": commit_date,
            "fixture": fixture.name,
            "n_runs": len(runs),
            "matrix": {
                "wire_modes": WIRE_MODES if not args.quick else [combos[0][0]],
                "ripup_passes": RIPUP_PASSES if not args.quick else [combos[0][1]],
                "optimize_iters": OPTIMIZE_ITERS if not args.quick else [combos[0][2]],
            },
        },
        "runs": [r.to_dict() for r in runs],
    }

    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"Wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
