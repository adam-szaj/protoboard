#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Reproducible call-count benchmark for the three perf-cache fixes.

Loads a fixture once, then runs a fixed workload exercising each hot
path the caches accelerate.  Reports per-action call counts collected
via :mod:`protoboard.instrumentation.call_counters` so before/after
comparisons are deterministic — no wall-clock noise, no PID-tied
sampling — and so the observed counts can be compared against the
theoretical complexity each fix targets.

Usage:
    uv run python tools/benchmark_perf_caches.py FIXTURE_DIR [--iters N]
                                                              [--no-cache]
                                                              [--max-depth N]
                                                              [--filter PATTERN]
                                                              [--json PATH]
                                                              [--compare PATH]

``--no-cache`` monkey-patches the three caches off so the same
workload runs in the "before fix" mode.  Pair it with a plain run on
the same fixture to read the speedup directly:

    uv run python tools/benchmark_perf_caches.py micek2
    uv run python tools/benchmark_perf_caches.py micek2 --no-cache

The benchmarked actions (each wrapped in its own top-level @counted
label so children show up as ``action.<inner>``):

  1. ``atoms_walk``        — drives ``Board.atoms()``                (fix #3)
  2. ``pad_node_insert``   — drives ``insert_pad_nodes`` ×50 paths   (fix #1)
  3. ``ownership_all``     — drives ``find_owned_wires`` once         (fix #2)
  4. ``ownership_groups``  — drives ``find_owned_wires`` once per group

Output: per-action call counts grouped by path.  Use ``--max-depth``
to cap how deep the recorded stack may grow (useful for keeping the
report short — depth 2 shows only direct children of each action).
Use ``--filter`` to restrict counting to specific path prefixes; the
syntax matches :func:`call_counters.set_path_filter` —
``"a.b.*;g.h.*"`` etc.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

# Make `protoboard` importable when run as a script.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from protoboard.instrumentation.call_counters import (
    counted,
    fresh_counters,
    snapshot,
)
from protoboard.paths import BUILTIN_LIBRARY_DIR
from protoboard.services.library.loader import load_library
from protoboard.services.persistence.project_yaml import (
    load_project,
    set_migration_library,
)


# ---------------------------------------------------------------------------
# Cache toggling
# ---------------------------------------------------------------------------


def _disable_caches() -> None:
    """Monkey-patch the three caches OFF for an apples-to-apples
    "pre-fix" benchmark.  Restoration is process-scoped — exit after."""
    import protoboard.domain.layout.wire_utils as wu

    original_bucket = wu._get_pad_bucket_index

    def uncached_bucket(pad_positions, _o=original_bucket):  # type: ignore[no-untyped-def]
        # Force rebuild every call by bypassing the module-level cache.
        pads_by_x: dict[int, list] = {}
        pads_by_y: dict[int, list] = {}
        for pad in pad_positions:
            pads_by_x.setdefault(round(pad.x), []).append(pad)
            pads_by_y.setdefault(round(pad.y), []).append(pad)
        return (pads_by_x, pads_by_y)
    # Preserve the @counted wrapper so the call still shows up in
    # snapshots even when uncached.
    wu._get_pad_bucket_index = counted("_get_pad_bucket_index")(uncached_bucket)  # type: ignore[assignment]

    import protoboard.domain.board.board as board_mod
    from protoboard.domain.board.board import _walk_atoms as _orig_walk

    def uncached_atoms(self):  # type: ignore[no-untyped-def]
        yield from _orig_walk(self.layout)
    board_mod.Board.atoms = counted("Board.atoms")(uncached_atoms)  # type: ignore[assignment,method-assign]

    import protoboard.domain.layout.group_ownership as go

    def uncached_find(*args, **kwargs):  # type: ignore[no-untyped-def]
        # Replicate find_owned_wires but pass _pin_world_cache=None
        # to wire_is_owned_by_group so every call re-resolves the pin.
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
    go.find_owned_wires = counted("find_owned_wires")(uncached_find)  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------


def _load_fixture(fixture: Path):
    lib = load_library([BUILTIN_LIBRARY_DIR])
    set_migration_library(lib)
    board, layout, schematic, instances, *_ = load_project(fixture)
    components = {name: lib.get(name) for name in lib.names()}
    return board, layout, schematic, instances, components


# ---------------------------------------------------------------------------
# Workload actions (each wrapped in its own @counted so children show
# up as e.g. ``ownership_all.find_owned_wires.pin_world_position``).
# ---------------------------------------------------------------------------


def _build_actions(board, layout, schematic, instances, components):
    """Return a dict of action callables, plus stats about the
    workload (so the caller can attach the n_wires / n_pads context
    to the JSON output)."""
    from protoboard.geometry import Point
    from protoboard.domain.layout.group_ownership import find_owned_wires
    from protoboard.domain.layout.wire_utils import (
        board_pads_union,
        insert_pad_nodes,
    )

    pads = board_pads_union(board)
    bounds = board.bounds
    paths = [
        [Point(0.5, float(int(bounds.height * (i + 1) / 51)) + 0.5),
         Point(bounds.width - 0.5, float(int(bounds.height * (i + 1) / 51)) + 0.5)]
        for i in range(50)
    ]

    @counted("atoms_walk")
    def atoms_walk() -> None:
        # Consume the iterator so caching has measurable effect.
        for _ in board.atoms():
            pass

    @counted("pad_node_insert")
    def pad_node_insert() -> None:
        for p in paths:
            insert_pad_nodes(p, pads)

    # Build wire-net map covering whatever schematic pins we can match
    # (good-enough for the half-orphan paths in wire_is_owned_by_group).
    wire_net_map: dict = {}
    from protoboard.domain.layout.knot import PinKnot
    from protoboard.domain.schematic.net import PinRef
    for net in schematic.nets.values():
        for wire in layout.wires:
            for ref in (wire.a, wire.b):
                if ref is None:
                    continue
                k = layout.knots.get(ref.id)
                if not isinstance(k, PinKnot):
                    continue
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
    )
    wires_list = list(layout.wires)

    @counted("ownership_all")
    def ownership_all() -> None:
        find_owned_wires(
            wires_list, universal_group,
            wire_net_map=wire_net_map,
            knots=layout.knots,
            group_comp_ids=all_comp_ids,
            schematic=schematic,
            placements=layout.placements,
            instances=instances,
            definitions=components,
        )

    real_groups = [
        g for gid, g in layout.groups.items()
        if g.name != "_root"
    ] or [universal_group]

    @counted("ownership_groups")
    def ownership_groups() -> None:
        for g in real_groups:
            find_owned_wires(
                wires_list, g,
                wire_net_map=wire_net_map,
                knots=layout.knots,
                group_comp_ids=g.component_ids,
                schematic=schematic,
                placements=layout.placements,
                instances=instances,
                definitions=components,
            )

    actions: dict[str, Callable[[], None]] = {
        "atoms_walk": atoms_walk,
        "pad_node_insert": pad_node_insert,
        "ownership_all": ownership_all,
        "ownership_groups": ownership_groups,
    }
    workload = {
        "n_wires": len(wires_list),
        "n_pads": len(pads),
        "n_groups": len(real_groups),
        "n_paths": len(paths),
        "n_components": len(layout.placements),
    }
    return actions, workload


# ---------------------------------------------------------------------------
# Run / report
# ---------------------------------------------------------------------------


def _run_action(
    fn: Callable[[], None], iters: int,
    max_depth: int | None, path_filter: str | None,
) -> dict[str, int]:
    """Run *fn* ``iters`` times under counter capture; return snapshot."""
    with fresh_counters(max_depth=max_depth, path_filter=path_filter):
        for _ in range(iters):
            fn()
        return snapshot()


def _print_action_report(
    label: str, iters: int, counts: dict[str, int],
) -> None:
    print(f"\n=== {label}  ({iters} iterations) ===")
    if not counts:
        print("  (no counts — filter matched nothing or counters disabled)")
        return
    # Sort by path so the hierarchy reads nicely.
    width = max(len(p) for p in counts)
    for path in sorted(counts):
        n = counts[path]
        per_iter = n / iters
        print(f"  {path:<{width}}  {n:>8d}  (per-iter: {per_iter:>8.2f})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path)
    parser.add_argument(
        "--iters", type=int, default=20,
        help="iterations per action (default: 20)",
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="monkey-patch caches off → simulates pre-fix call counts",
    )
    parser.add_argument(
        "--max-depth", type=int, default=None,
        help="cap recorded stack depth (passed to call_counters)",
    )
    parser.add_argument(
        "--filter", type=str, default=None,
        help='path filter, e.g. "ownership_all.**;ownership_groups.**"',
    )
    parser.add_argument("--json", type=Path, help="write JSON results")
    parser.add_argument(
        "--compare", type=Path,
        help="JSON of a previous run to diff against",
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
    actions, workload = _build_actions(
        board, layout, schematic, instances, components,
    )

    print(f"Mode: {mode_label}")
    print(f"Fixture: {args.fixture.name}  " + "  ".join(
        f"{k}={v}" for k, v in workload.items()
    ))
    print(f"max_depth={args.max_depth}  filter={args.filter!r}")

    per_action: dict[str, dict[str, int]] = {}
    for name, fn in actions.items():
        counts = _run_action(fn, args.iters, args.max_depth, args.filter)
        per_action[name] = counts
        _print_action_report(name, args.iters, counts)

    payload = {
        "fixture": str(args.fixture),
        "mode": "no-cache" if args.no_cache else "cached",
        "iters": args.iters,
        "max_depth": args.max_depth,
        "filter": args.filter,
        "workload": workload,
        "actions": per_action,
    }

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2))
        print(f"\nwrote {args.json}", file=sys.stderr)

    if args.compare:
        other = json.loads(args.compare.read_text())
        other_mode = other.get("mode", "?")
        print(f"\n=== Δ vs {args.compare.name} (mode={other_mode}) ===")
        for action_name, our_counts in per_action.items():
            other_counts = other.get("actions", {}).get(action_name, {})
            paths = sorted(set(our_counts) | set(other_counts))
            if not paths:
                continue
            print(f"\n  {action_name}:")
            width = max(len(p) for p in paths)
            for p in paths:
                a = other_counts.get(p, 0)
                b = our_counts.get(p, 0)
                if a == b:
                    ratio_str = "        same"
                elif a == 0:
                    ratio_str = f"   0 → {b} (new)"
                elif b == 0:
                    ratio_str = f"   {a} → 0 (gone)"
                elif a > b:
                    ratio_str = f"{a/b:5.2f}× fewer"
                else:
                    ratio_str = f"{b/a:5.2f}× more"
                print(f"    {p:<{width}}  {a:>8d} → {b:>8d}  {ratio_str}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
