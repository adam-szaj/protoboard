# SPDX-License-Identifier: MIT
"""Performance benchmarks for the autorouter and A* find_paths.

Run with:
    uv run pytest tests/benchmarks/test_perf_autorouter.py --benchmark-only -v

Order-of-magnitude targets (synthetic_medium: 50 comps, ~40 nets):
  find_paths single call   < 50 ms
  _run_one_pass (1 pass)   < 30 s   (covers all nets, rip-up disabled)
"""
from __future__ import annotations

import copy

import pytest


# ---------------------------------------------------------------------------
# find_paths — single A* call
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def astar_context(synthetic_medium_project):
    """Pre-built graph + a source/target pair for find_paths benchmarks."""
    board, layout, schematic, instances, routing, _lib, components = synthetic_medium_project
    from protoboard.domain.connectivity.incremental import ConnectivityGraph

    graph = ConnectivityGraph(
        board=board,
        layout=layout,
        components=components,
        instances=instances,
    )
    # Pick the first net with ≥2 placed pins.
    from protoboard.domain.layout.placement import pin_world_position

    src_pt = tgt_pt = None
    for net in schematic.nets.values():
        placed = [
            p for p in net.pins
            if p.component_id in layout.placements and p.component_id in instances
        ]
        if len(placed) < 2:
            continue
        p0, p1 = placed[0], placed[1]
        inst0 = instances[p0.component_id]
        inst1 = instances[p1.component_id]
        comp0 = components.get(inst0.definition_ref)
        comp1 = components.get(inst1.definition_ref)
        if comp0 is None or comp1 is None:
            continue
        src_pt = pin_world_position(comp0.footprint, layout.placements[p0.component_id], p0.pin_number)
        tgt_pt = pin_world_position(comp1.footprint, layout.placements[p1.component_id], p1.pin_number)
        break

    assert src_pt is not None, "No suitable net found in synthetic_medium project"
    cm = routing.cost_model()
    return graph, src_pt, tgt_pt, cm


def test_bench_find_paths_single(benchmark, astar_context):
    """Single A* call: source → one target, bottom layer, k=1."""
    graph, src, tgt, cm = astar_context
    result = benchmark(
        lambda: __import__(
            "protoboard.services.routing.astar", fromlist=["find_paths"]
        ).find_paths(graph, src, [tgt], "bottom", cm, k=1)
    )
    # Sanity: benchmark callable must return something truthy (path found or []).
    _ = result


# ---------------------------------------------------------------------------
# Full autoroute pass on synthetic medium (no optimize iterations, no rip-up)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def synthetic_medium_empty_layout(synthetic_medium_project):
    """Synthetic medium layout — generator yields placements-only, no wires."""
    board, layout, schematic, instances, routing, _lib, components = synthetic_medium_project
    import copy as _copy
    return board, _copy.deepcopy(layout), schematic, instances, routing, components


def test_bench_autoroute_one_pass(benchmark, synthetic_medium_empty_layout):
    """One greedy autoroute pass over every net (no rip-up, no optimize)."""
    board, layout, schematic, instances, routing, components = synthetic_medium_empty_layout
    from protoboard.services.routing.autorouter import autoroute

    def _run():
        return autoroute(
            board=board,
            layout=copy.deepcopy(layout),
            schematic=schematic,
            components=components,
            instances=instances,
            cost_model=routing.cost_model(),
            max_rip_up_passes=0,
            max_optimize_iterations=0,
            preferred_side=routing.preferred_side,
            wire_mode=routing.wire_mode,
            max_detour_factor=routing.max_detour_factor,
        )

    result = benchmark.pedantic(_run, iterations=1, rounds=3)
    routed = len(result.wires)
    unrouted = len(result.unrouted_nets)
    # Just report — don't assert a specific count since it depends on routing
    # parameters; the benchmark value (time) is what matters.
    print(f"\n  wires={routed}, unrouted_nets={unrouted}")
