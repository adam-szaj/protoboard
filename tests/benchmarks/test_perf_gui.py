# SPDX-License-Identifier: MIT
"""GUI performance benchmarks.

Run with:
    uv run pytest tests/benchmarks/ --benchmark-only -v
Save baseline:
    uv run pytest tests/benchmarks/ --benchmark-only --benchmark-save=baseline
Compare:
    uv run pytest tests/benchmarks/ --benchmark-only --benchmark-compare=baseline
"""
from __future__ import annotations

import math
import uuid

import pytest
from PySide6.QtCore import QPointF


# ---------------------------------------------------------------------------
# BoardView.refresh() — warm path (nothing changed)
# ---------------------------------------------------------------------------

def test_bench_refresh_warm_nothing_changed(benchmark, m4_board_view):
    """Warm refresh when layout is unchanged — should reuse all groups."""
    benchmark(m4_board_view.refresh)


def test_bench_refresh_warm_one_comp_moved(benchmark, m4_board_view, m4_session):
    """Warm refresh after moving one component — only that group rebuilds."""
    layout = m4_session.layout
    cid = next(iter(layout.placements))
    pl = layout.placements[cid]

    from protoboard.geometry import Point
    from dataclasses import replace

    toggle = [False]

    def move_and_refresh():
        toggle[0] = not toggle[0]
        dx = 1 if toggle[0] else -1
        layout.placements[cid] = replace(pl, position=Point(pl.position.x + dx, pl.position.y))
        m4_session.invalidate_graph()
        m4_board_view.refresh()

    benchmark(move_and_refresh)


# ---------------------------------------------------------------------------
# set_highlighted_net — opacity-only path, no rebuild
# ---------------------------------------------------------------------------

def test_bench_set_highlighted_net(benchmark, m4_board_view, m4_session):
    """Switching highlighted net must be O(N) setOpacity calls, not rebuilds."""
    nets = list(m4_session.schematic.nets.keys())
    # Use the net with the most wires (GND-equivalent) as the worst case.
    from protoboard.gui.board_view import _build_wire_net_map
    from collections import Counter
    wire_net_map = _build_wire_net_map(m4_session)
    busiest_net_id = Counter(wire_net_map.values()).most_common(1)[0][0]

    def cycle_highlight():
        m4_board_view.set_highlighted_net(busiest_net_id)
        m4_board_view.set_highlighted_net(None)

    benchmark(cycle_highlight)


# ---------------------------------------------------------------------------
# Spatial index — hit-test on click
# ---------------------------------------------------------------------------

def test_bench_hit_test_component(benchmark, m4_board_view, m4_session):
    """_hit_test should be fast via spatial index (not O(C×P) brute force)."""
    from protoboard.gui.tools.select_tool import SelectTool
    tool = SelectTool()
    tool.activate(m4_session, m4_board_view)

    # Pick a point near a component pin.
    layout = m4_session.layout
    instances = m4_session.instances
    components = m4_session.components
    cid = next(iter(layout.placements))
    pl = layout.placements[cid]
    inst = instances.get(cid)
    defn = components.get(inst.definition_ref) if inst else None
    if defn is None:
        pytest.skip("no component definition")
    from protoboard.domain.layout.placement import pin_world_position
    pos = pin_world_position(defn.footprint, pl, 1)
    scene_pt = QPointF(pos.x, pos.y)

    benchmark(tool._hit_test, scene_pt)


def test_bench_hit_test_pin_for_comp(benchmark, m4_board_view, m4_session):
    """_hit_test_pin_for_comp: O(P) scan of one component's pins."""
    from protoboard.gui.tools.select_tool import SelectTool
    tool = SelectTool()
    tool.activate(m4_session, m4_board_view)

    layout = m4_session.layout
    instances = m4_session.instances
    components = m4_session.components
    cid = next(iter(layout.placements))
    pl = layout.placements[cid]
    inst = instances.get(cid)
    defn = components.get(inst.definition_ref) if inst else None
    if defn is None:
        pytest.skip("no component definition")
    from protoboard.domain.layout.placement import pin_world_position
    pos = pin_world_position(defn.footprint, pl, 1)
    scene_pt = QPointF(pos.x, pos.y)

    benchmark(tool._hit_test_pin_for_comp, scene_pt, cid)


def test_bench_hit_test_wire(benchmark, m4_board_view, m4_session):
    """_hit_test_wire via spatial index."""
    from protoboard.gui.tools.select_tool import SelectTool
    tool = SelectTool()
    tool.activate(m4_session, m4_board_view)

    # Pick a point on the first wire.
    wire = m4_session.layout.wires[0]
    pt = wire.path[0]
    scene_pt = QPointF(pt.x, pt.y)

    benchmark(tool._hit_test_wire, scene_pt)


# ---------------------------------------------------------------------------
# ConnectivityGraph build
# ---------------------------------------------------------------------------

def test_bench_connectivity_graph_build(benchmark, m4_project):
    """Full connectivity graph build from scratch."""
    board, layout, schematic, instances, _, _, components = m4_project
    from protoboard.domain.connectivity.graph import ConnectivityGraph

    def build():
        ConnectivityGraph(board=board, layout=layout, components=components, instances=instances)

    benchmark(build)


def test_bench_session_connectivity_graph_cached(benchmark, m4_session):
    """Session cached graph — rebuild only on invalidate, reuse otherwise."""
    m4_session.invalidate_graph()

    def access():
        m4_session.invalidate_graph()
        return m4_session.connectivity_graph()

    benchmark(access)


# ---------------------------------------------------------------------------
# Spatial index build
# ---------------------------------------------------------------------------

def test_bench_spatial_index_build(benchmark, m4_session):
    """Building comp+wire spatial indexes from scratch (happens after layout_changed)."""
    from protoboard.gui.session import _build_comp_spatial_index, _build_wire_spatial_index

    def build():
        m4_session._comp_spatial_index = None
        m4_session._wire_spatial_index = None
        m4_session.comp_spatial_index()
        m4_session.wire_spatial_index()

    benchmark(build)
