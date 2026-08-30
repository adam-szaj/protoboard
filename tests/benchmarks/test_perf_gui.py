# SPDX-License-Identifier: MIT
"""GUI performance benchmarks.

Run with:
    uv run pytest tests/benchmarks/ --benchmark-only -v
Save baseline:
    uv run pytest tests/benchmarks/ --benchmark-only --benchmark-save=baseline
Compare:
    uv run pytest tests/benchmarks/ --benchmark-only --benchmark-compare=baseline

Fixtures come from ``tests/benchmarks/conftest.py``:
  synthetic_medium_*         — 50 components, no wires (placement/refresh bench)
  synthetic_medium_routed_*  — same, but with autoroute applied (wire bench)
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# BoardView.refresh() — warm path (nothing changed)
# ---------------------------------------------------------------------------


def test_bench_refresh_warm_nothing_changed(benchmark, synthetic_medium_board_view):
    """Warm refresh when layout is unchanged — should reuse all groups."""
    benchmark(synthetic_medium_board_view.refresh)


def test_bench_refresh_warm_one_comp_moved(
    benchmark,
    synthetic_medium_board_view,
    synthetic_medium_session,
):
    """Warm refresh after moving one component — only that group rebuilds."""
    layout = synthetic_medium_session.layout
    cid = next(iter(layout.placements))
    pl = layout.placements[cid]

    from dataclasses import replace

    from protoboard.geometry import Point

    toggle = [False]

    def move_and_refresh():
        toggle[0] = not toggle[0]
        dx = 1 if toggle[0] else -1
        layout.placements[cid] = replace(
            pl,
            position=Point(pl.position.x + dx, pl.position.y),
        )
        synthetic_medium_session.invalidate_graph()
        synthetic_medium_board_view.refresh()

    benchmark(move_and_refresh)


# ---------------------------------------------------------------------------
# set_highlighted_net — opacity-only path, no rebuild
# ---------------------------------------------------------------------------


def test_bench_set_highlighted_net(
    benchmark,
    synthetic_medium_routed_board_view,
    synthetic_medium_routed_session,
):
    """Switching highlighted net must be O(N) setOpacity calls, not rebuilds."""
    from collections import Counter

    wire_net_map = synthetic_medium_routed_session.cached_wire_net_map()
    if not wire_net_map:
        pytest.skip("routed fixture produced no wires")
    busiest_net_id = Counter(wire_net_map.values()).most_common(1)[0][0]

    def cycle_highlight():
        synthetic_medium_routed_board_view.set_highlighted_net(busiest_net_id)
        synthetic_medium_routed_board_view.set_highlighted_net(None)

    benchmark(cycle_highlight)


# ---------------------------------------------------------------------------
# Spatial index — hit-test on click
# ---------------------------------------------------------------------------


def _medium_hit_tester(session, board_view):
    """A HitTester bound to the medium fixture (v2's hit-test home)."""
    from typing import cast

    from protoboard.controller.selection.hit_test import HitTester, SelectionFilters
    from protoboard.types import Side

    tester = HitTester(session, camera_side=cast(Side, board_view.camera_side))
    return tester, SelectionFilters()


def test_bench_hit_test_component(
    benchmark,
    synthetic_medium_board_view,
    synthetic_medium_session,
):
    """HitTester.hit should be fast via spatial index (not O(C×P) brute force)."""
    tester, filters = _medium_hit_tester(
        synthetic_medium_session, synthetic_medium_board_view
    )
    layout = synthetic_medium_session.layout
    instances = synthetic_medium_session.instances
    components = synthetic_medium_session.components
    cid = next(iter(layout.placements))
    pl = layout.placements[cid]
    inst = instances.get(cid)
    defn = components.get(inst.definition_ref) if inst else None
    if defn is None:
        pytest.skip("no component definition")
    from protoboard.domain.layout.placement import pin_world_position

    pos = pin_world_position(defn.default_footprint(), pl, 1)

    benchmark(tester.hit, pos.x, pos.y, filters)


def test_bench_hit_test_wire(
    benchmark,
    synthetic_medium_routed_board_view,
    synthetic_medium_routed_session,
):
    """HitTester wire hit-test via spatial index."""
    tester, filters = _medium_hit_tester(
        synthetic_medium_routed_session, synthetic_medium_routed_board_view
    )
    if not synthetic_medium_routed_session.layout.wires:
        pytest.skip("routed fixture produced no wires")
    wire = synthetic_medium_routed_session.layout.wires[0]
    pt = wire.path[0]

    benchmark(tester.hit, pt.x, pt.y, filters)


# ---------------------------------------------------------------------------
# ConnectivityGraph build
# ---------------------------------------------------------------------------


def test_bench_connectivity_graph_build(benchmark, synthetic_medium_project):
    """Full connectivity graph build from scratch."""
    board, layout, _schematic, instances, _routing, _lib, components = (
        synthetic_medium_project
    )
    from protoboard.domain.connectivity.graph import ConnectivityGraph

    def build():
        ConnectivityGraph(
            board=board,
            layout=layout,
            components=components,
            instances=instances,
        )

    benchmark(build)


def test_bench_session_connectivity_graph_cached(benchmark, synthetic_medium_session):
    """Session cached graph — rebuild only on invalidate, reuse otherwise."""
    synthetic_medium_session.invalidate_graph()

    def access():
        synthetic_medium_session.invalidate_graph()
        return synthetic_medium_session.connectivity_graph()

    benchmark(access)


# ---------------------------------------------------------------------------
# Spatial index build
# ---------------------------------------------------------------------------


def test_bench_spatial_index_build(benchmark, synthetic_medium_session):
    """Building comp+wire spatial indexes from scratch (happens after layout_changed)."""  # noqa: E501

    def build():
        synthetic_medium_session._comp_spatial_index = None
        synthetic_medium_session._wire_spatial_index = None
        synthetic_medium_session.comp_spatial_index()
        synthetic_medium_session.wire_spatial_index()

    benchmark(build)
