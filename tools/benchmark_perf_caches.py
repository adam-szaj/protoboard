#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Reproducible micro-benchmark for the three perf-cache fixes.

Loads a fixture once, then runs a fixed workload that exercises each
hot path the caches are supposed to accelerate.  Reports per-operation
medians and totals so a before/after comparison on the SAME workload
is a one-line diff instead of two profiling sessions with arbitrary
user activity.

Usage:
    uv run python tools/benchmark_perf_caches.py FIXTURE_DIR [--iters N]
                                                              [--no-cache]
                                                              [--json PATH]

``--no-cache`` monkey-patches the three caches off so the same
workload runs in the "before fix" mode.  Pair it with a plain run
on the same fixture to read the speedup directly:

    uv run python tools/benchmark_perf_caches.py tests/gold_performance_test_check
    uv run python tools/benchmark_perf_caches.py tests/gold_performance_test_check --no-cache

The benchmarked operations:

  1. ``board.atoms()``                 — fix #3
  2. ``insert_pad_nodes`` on N wires    — fix #1
  3. ``find_owned_wires(all wires)``    — fix #2

Each is run ``--iters`` times (default 100).  The first iteration is
discarded as warm-up.  Output: min / median / mean / total wall time
plus an aggregate "delta vs --no-cache" if a paired JSON is provided.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Make `protoboard` importable when run as a script.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from protoboard.paths import BUILTIN_LIBRARY_DIR
from protoboard.services.library.loader import load_library
from protoboard.services.persistence.project_yaml import (
    load_project,
    set_migration_library,
)


@dataclass
class _Stats:
    name: str
    samples: list[float]

    @property
    def n(self) -> int:
        return len(self.samples)

    @property
    def total_ms(self) -> float:
        return sum(self.samples) * 1000.0

    @property
    def median_ms(self) -> float:
        return statistics.median(self.samples) * 1000.0

    @property
    def mean_ms(self) -> float:
        return statistics.mean(self.samples) * 1000.0

    @property
    def min_ms(self) -> float:
        return min(self.samples) * 1000.0

    def to_dict(self) -> dict[str, float]:
        return {
            "n": self.n,
            "total_ms": round(self.total_ms, 3),
            "median_ms": round(self.median_ms, 4),
            "mean_ms": round(self.mean_ms, 4),
            "min_ms": round(self.min_ms, 4),
        }


def _measure(name: str, fn, iters: int) -> _Stats:
    """Run *fn* ``iters`` times, return per-call wall-time samples
    (first sample discarded as warm-up)."""
    samples: list[float] = []
    for i in range(iters + 1):
        t0 = time.perf_counter()
        fn()
        elapsed = time.perf_counter() - t0
        if i > 0:  # skip warm-up
            samples.append(elapsed)
    return _Stats(name=name, samples=samples)


def _disable_caches() -> None:
    """Monkey-patch the three caches OFF for an apples-to-apples
    "pre-fix" benchmark.  Restoration is process-scoped — exit after."""
    # Fix #1: kill the pad-bucket identity cache.
    import protoboard.domain.layout.wire_utils as wu
    _original_bucket = None
    for name in ("_get_pad_bucket_index",):
        original = getattr(wu, name)
        if not hasattr(original, "_uncached"):
            def uncached(pad_positions, _o=original):  # type: ignore[no-untyped-def]
                # Force rebuild every call by bypassing the cache dict.
                pads_by_x: dict[int, list] = {}
                pads_by_y: dict[int, list] = {}
                for pad in pad_positions:
                    pads_by_x.setdefault(round(pad.x), []).append(pad)
                    pads_by_y.setdefault(round(pad.y), []).append(pad)
                return (pads_by_x, pads_by_y)
            uncached._uncached = True  # type: ignore[attr-defined]
            setattr(wu, name, uncached)

    # Fix #3: kill the atoms() cache.
    import protoboard.domain.board.board as board_mod
    original_atoms = board_mod.Board.atoms

    def uncached_atoms(self):  # type: ignore[no-untyped-def]
        # Skip the cache lookup, walk every time.
        from protoboard.domain.board.board import _walk_atoms
        yield from _walk_atoms(self.layout)
    board_mod.Board.atoms = uncached_atoms  # type: ignore[assignment,method-assign]

    # Fix #2: kill the find_owned_wires pin-world cache.
    import protoboard.domain.layout.group_ownership as go
    original_find = go.find_owned_wires

    def uncached_find(*args, **kwargs):  # type: ignore[no-untyped-def]
        # Strip the cache-passing path; iterate per-wire without sharing.
        from protoboard.domain.layout.group_ownership import (
            wire_is_owned_by_group,
        )
        wires = args[0] if args else kwargs["wires"]
        group = args[1] if len(args) > 1 else kwargs["group"]
        wire_net_map = kwargs.get("wire_net_map", {})
        knots = kwargs["knots"]
        group_comp_ids = kwargs["group_comp_ids"]
        schematic = kwargs.get("schematic")
        placements = kwargs.get("placements")
        instances = kwargs.get("instances")
        definitions = kwargs.get("definitions")
        result: set = set()
        for w in wires:
            # Fresh None cache each call → mimics pre-fix behaviour
            # (no shared memoisation across wires).
            if wire_is_owned_by_group(
                w, group,
                wire_net_map=wire_net_map,
                knots=knots,
                group_comp_ids=group_comp_ids,
                schematic=schematic,
                placements=placements,
                instances=instances,
                definitions=definitions,
                _pin_world_cache=None,
            ):
                result.add(w.id)
        return result
    go.find_owned_wires = uncached_find  # type: ignore[assignment]


def _load_fixture(fixture: Path):
    lib = load_library([BUILTIN_LIBRARY_DIR])
    set_migration_library(lib)
    board, layout, schematic, instances, *_ = load_project(fixture)
    components = {name: lib.get(name) for name in lib.names()}
    return board, layout, schematic, instances, components


def _build_workloads(board, layout, schematic, instances, components):
    """Return three callables wrapping the operations under test."""
    from protoboard.geometry import Point
    from protoboard.domain.layout.group_ownership import find_owned_wires
    from protoboard.domain.layout.wire_utils import (
        board_pads_union,
        insert_pad_nodes,
    )

    pads = board_pads_union(board)
    bounds = board.bounds
    # 50 synthetic paths spanning the board on integer rows.
    paths = []
    for i in range(50):
        y = float(int(bounds.height * (i + 1) / 51)) + 0.5
        paths.append([Point(0.5, y), Point(bounds.width - 0.5, y)])

    def op_atoms() -> None:
        # Consume the iterator so caching has measurable effect.
        for _ in board.atoms():
            pass

    def op_insert_pad_nodes() -> None:
        for p in paths:
            insert_pad_nodes(p, pads)

    # Build a wire-net map and group-comp set covering the entire layout
    # (one group containing every component) so find_owned_wires runs on
    # all wires.
    wire_net_map: dict = {}
    for net in schematic.nets.values():
        for wire in layout.wires:
            for ref in (wire.a, wire.b):
                if ref is None:
                    continue
                k = layout.knots.get(ref.id)
                if k is None:
                    continue
                from protoboard.domain.layout.knot import PinKnot
                if not isinstance(k, PinKnot):
                    continue
                from protoboard.domain.schematic.net import PinRef
                if PinRef(k.pin.component_id, k.pin.pin_number) in net.pins:
                    wire_net_map[wire.id] = net.id
                    break
    all_comp_ids = frozenset(layout.placements.keys())
    from protoboard.domain.layout.group import ComponentGroup
    import uuid as _uuid
    universal_group = ComponentGroup(
        id=_uuid.uuid4(),
        name="_bench_universe",
        component_ids=all_comp_ids,
        origin=None,
    )
    wires_list = list(layout.wires)

    def op_find_owned_wires() -> None:
        find_owned_wires(
            wires_list, universal_group,
            wire_net_map=wire_net_map,
            knots=layout.knots,  # type: ignore[arg-type]
            group_comp_ids=all_comp_ids,
            schematic=schematic,
            placements=layout.placements,
            instances=instances,
            definitions=components,
        )

    # Per-group classifier batch — mimics BoardView.refresh which calls
    # find_owned_wires once per non-root group.  Worst-case for the
    # pin-world cache because each group adds duplicate lookups on the
    # same pins (every overlapping ancestor group sees the same wires).
    groups_list = [
        g for gid, g in layout.groups.items()
        if g.name != "_root"
    ] or [universal_group]

    def op_find_owned_wires_per_group() -> None:
        for g in groups_list:
            find_owned_wires(
                wires_list, g,
                wire_net_map=wire_net_map,
                knots=layout.knots,  # type: ignore[arg-type]
                group_comp_ids=g.component_ids,
                schematic=schematic,
                placements=layout.placements,
                instances=instances,
                definitions=components,
            )

    return (
        op_atoms, op_insert_pad_nodes, op_find_owned_wires,
        op_find_owned_wires_per_group, len(wires_list), len(pads),
        len(groups_list),
    )


def _format_row(stats: _Stats) -> str:
    return (
        f"  {stats.name:32s}  n={stats.n:4d}  "
        f"min={stats.min_ms:8.3f}ms  med={stats.median_ms:8.3f}ms  "
        f"mean={stats.mean_ms:8.3f}ms  total={stats.total_ms:8.1f}ms"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path)
    parser.add_argument(
        "--iters", type=int, default=100,
        help="per-op iterations (default: 100; first is warm-up)",
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="monkey-patch caches off → simulates pre-fix performance",
    )
    parser.add_argument("--json", type=Path, help="write JSON results")
    parser.add_argument(
        "--compare", type=Path,
        help="JSON of a previous --no-cache run to diff against",
    )
    args = parser.parse_args()

    if args.no_cache:
        _disable_caches()
        mode_label = "NO-CACHE (simulated pre-fix)"
    else:
        mode_label = "CACHED (current main)"

    if not args.fixture.exists():
        print(f"fixture not found: {args.fixture}", file=sys.stderr)
        return 1

    print(f"Loading {args.fixture}…", file=sys.stderr)
    board, layout, schematic, instances, components = _load_fixture(args.fixture)
    ops = _build_workloads(board, layout, schematic, instances, components)
    (
        op_atoms, op_insert, op_find, op_find_per_group,
        n_wires, n_pads, n_groups,
    ) = ops

    print(f"Mode: {mode_label}")
    print(f"Fixture: {args.fixture.name}  "
          f"wires={n_wires}  pads={n_pads}  groups={n_groups}  "
          f"iters={args.iters}")
    print()

    results = [
        _measure("board.atoms()", op_atoms, args.iters),
        _measure("insert_pad_nodes×50 paths", op_insert, args.iters),
        _measure("find_owned_wires(all-in-one)", op_find, args.iters),
        _measure("find_owned_wires×N groups", op_find_per_group, args.iters),
    ]
    for s in results:
        print(_format_row(s))

    payload = {
        "fixture": str(args.fixture),
        "mode": "no-cache" if args.no_cache else "cached",
        "iters": args.iters,
        "n_wires": n_wires,
        "n_pads": n_pads,
        "results": {s.name: s.to_dict() for s in results},
    }

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2))
        print(f"\nwrote {args.json}", file=sys.stderr)

    if args.compare:
        other = json.loads(args.compare.read_text())
        other_mode = other.get("mode", "?")
        print(f"\n=== Comparison vs {args.compare.name} (mode={other_mode}) ===")
        for s in results:
            o = other["results"].get(s.name)
            if o is None:
                continue
            ratio = o["median_ms"] / s.median_ms if s.median_ms > 0 else float("inf")
            direction = "faster" if ratio >= 1.0 else "slower"
            display_ratio = ratio if ratio >= 1.0 else 1.0 / ratio
            print(f"  {s.name:32s}  {o['median_ms']:8.3f}ms → "
                  f"{s.median_ms:8.3f}ms  ({display_ratio:5.2f}× {direction})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
