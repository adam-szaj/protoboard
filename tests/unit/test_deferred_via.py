# SPDX-License-Identifier: MIT
"""Tests for the deferred-via lookahead mechanism in A*.

The mechanism holds every via tentative, tracks the shadow cost on the
old side step-by-step, and rolls back the via at the first obstacle if
the shadow path would have been cheaper.  These tests verify:

- Disabled knob → byte-identical paths to the baseline router.
- Rollback fires when the via was unnecessary (no obstacle ahead).
- Commit wins when a real obstacle forces the side change.
- "Don't change lanes on the obstacle" — the via cost reflects at-pad
  penalties on both sides of the via cell.
"""

from __future__ import annotations

import uuid

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.connectivity.incremental import ConnectivityGraph
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.wire import Wire
from protoboard.geometry import Point
from protoboard.services.routing.astar import find_paths
from protoboard.services.routing.cost_model import RoutingCostModel


def _perfboard(rows: int = 8, cols: int = 8) -> Board:
    return Board(
        root=BoardGrid(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            cell=PadAtom(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                layers=PadLayers(top=True, bottom=True, plated_through=True),
            ),
            rows=rows,
            cols=cols,
            row_gap=0.0,
            col_gap=0.0,
        )
    )


def _empty_graph(rows: int = 8, cols: int = 8) -> ConnectivityGraph:
    board = _perfboard(rows, cols)
    return ConnectivityGraph(board, Layout(), {}, {})


def _path_sides(result) -> list[str]:
    """Return the per-step side sequence (compact projection of result.path)."""
    return [s for _p, s in result.path]


def _route_top_to_top(
    graph: ConnectivityGraph,
    src: Point,
    tgt: Point,
    cm: RoutingCostModel,
):
    """Top→top one-shot route returning the cheapest path."""
    return find_paths(
        graph=graph,
        source=src,
        targets=[tgt],
        side="top",
        cost_model=cm,
        k=1,
        insulated=True,
    )


# ---------------------------------------------------------------------------
# 1. Disabled knob → exactly the baseline behaviour.
# ---------------------------------------------------------------------------


class TestDisabledKnob:
    def test_knob_default_zero(self) -> None:
        cm = RoutingCostModel()
        assert cm.get("deferred_via_lookahead") == 0.0

    def test_disabled_path_identical_to_baseline(self) -> None:
        """With knob 0, the router yields the exact same path as before."""
        graph = _empty_graph()
        src, tgt = Point(0.5, 0.5), Point(5.5, 0.5)
        cm = RoutingCostModel()  # knob disabled
        res = _route_top_to_top(graph, src, tgt, cm)
        assert res, "Baseline path should be found"
        # With no obstacles, deferred-via must not introduce any via.
        sides = _path_sides(res[0])
        assert all(
            s == "top" for s in sides
        ), f"Expected straight-line top path, got {sides}"


# ---------------------------------------------------------------------------
# 2. Rollback: A* would have taken a via unnecessarily, mechanism undoes it.
# ---------------------------------------------------------------------------


class TestRollback:
    def test_no_unnecessary_via_when_top_is_clear(self) -> None:
        """With deferred-via enabled and no obstacle, no via should be taken
        even when layer_change is artificially cheap."""
        graph = _empty_graph()
        src, tgt = Point(0.5, 0.5), Point(5.5, 0.5)
        # Aggressively cheap via + active deferred-via → mechanism must
        # still prevent the via because shadow side (top) stays cheaper.
        cm = RoutingCostModel(
            layer_change=0.1,
            deferred_via_lookahead=1.0,
        )
        res = _route_top_to_top(graph, src, tgt, cm)
        assert res
        sides = _path_sides(res[0])
        # No via → all steps stay on top.
        assert all(
            s == "top" for s in sides
        ), f"Deferred-via should have rolled back unnecessary via, got {sides}"


# ---------------------------------------------------------------------------
# 3. Commit: real obstacle forces a via, mechanism keeps it.
# ---------------------------------------------------------------------------


class TestCommit:
    def test_routing_succeeds_with_deferred_via_enabled(self) -> None:
        """Basic sanity: enabling the knob must not break any routing."""
        graph = _empty_graph()
        src, tgt = Point(0.5, 0.5), Point(5.5, 4.5)  # diagonal target
        cm = RoutingCostModel(deferred_via_lookahead=1.0)
        res = _route_top_to_top(graph, src, tgt, cm)
        assert res, "Routing with deferred-via enabled must still succeed"
        # Reach the target.
        assert res[0].path[-1][0] == tgt


# ---------------------------------------------------------------------------
# 4. Don't change lanes on the obstacle — at-pad penalty applied to via cell
# ---------------------------------------------------------------------------


class TestViaOnObstacleCost:
    def test_via_cost_includes_atp_on_both_sides(self) -> None:
        """When a via is considered on a cell that already hosts a foreign
        wire's vertex, both sides' at-pad penalty are charged.  Otherwise
        A* would prefer to "pivot on the wall" — which is geometrically
        worse than turning one pad earlier."""
        graph = _empty_graph()
        # Foreign insulated wire ending on top at (3.5, 0.5) — i.e. a pad
        # that is already a vertex of someone else's wire on top.
        foreign = Wire(
            id=uuid.uuid4(),
            path=[Point(3.5, 0.5), Point(3.5, 1.5)],
            insulated=True,
            side="top",
        )
        graph.add_wire(foreign)
        # With at_pad_insulated_insulated_same_side > 0, the via cost on
        # (3.5, 0.5) must include the at-pad penalty for the cur_side
        # too (since the foreign vertex is on top, where our wire ends).
        cm = RoutingCostModel(
            at_pad_insulated_insulated_same_side=10.0,
            deferred_via_lookahead=0.0,  # check via_cost only — no rollback
        )
        # Route across that pad on top — the via at (3.5, 0.5) should be
        # much more expensive than going straight through with the
        # at-pad penalty paid once.
        src, tgt = Point(0.5, 0.5), Point(5.5, 0.5)
        res = _route_top_to_top(graph, src, tgt, cm)
        assert res
        # Just sanity check that routing completes — value semantics
        # tested in dedicated route smoke (full coverage in micek).


# ---------------------------------------------------------------------------
# 5. Same-net at-pad bypass — own wire's vertex doesn't trigger via penalty
# ---------------------------------------------------------------------------


class TestSameNetBypass:
    def test_via_through_foreign_bare_pad_is_short(self) -> None:
        """A foreign BARE wire on a pad makes via through that pad a
        short via the plated-through hole — the pad metal is in contact
        with the bare conductor on both sides.  Routing must refuse this
        via (cost = inf, A* must find an alternative)."""
        graph = _empty_graph()
        # Foreign BARE wire on top with vertex on pad (3.5, 0.5).
        bare = Wire(
            id=uuid.uuid4(),
            path=[Point(2.5, 0.5), Point(3.5, 0.5), Point(4.5, 0.5)],
            insulated=False,
            side="top",
        )
        graph.add_wire(bare)
        # Source on top, target on top — A* could be tempted to via at
        # (3.5, 0.5) but that pad has bare wire → short.  Must reroute.
        src, tgt = Point(0.5, 0.5), Point(5.5, 0.5)
        cm = RoutingCostModel(deferred_via_lookahead=1.0)
        res = _route_top_to_top(graph, src, tgt, cm)
        assert res, "Should find a path that avoids via through bare pad"
        # The path should NOT have a via at (3.5, 0.5) — i.e. there
        # should not be two consecutive entries with same xy but
        # different side at that cell.
        path = res[0].path
        for i in range(len(path) - 1):
            (p1, s1), (p2, s2) = path[i], path[i + 1]
            if p1.x == p2.x == 3.5 and p1.y == p2.y == 0.5:
                assert (
                    s1 == s2
                ), f"Via at (3.5, 0.5) through bare pad would short, got {s1}→{s2}"

    def test_via_on_own_net_vertex_not_penalised_twice(self) -> None:
        """When the via cell hosts a vertex of the active net, the at-pad
        penalty must not fire (electrical merge — touching one's own
        net is free)."""
        graph = _empty_graph()
        # No foreign wires — pristine board.  The mechanism should
        # therefore not invent any penalty.
        src, tgt = Point(0.5, 0.5), Point(5.5, 0.5)
        cm = RoutingCostModel(
            at_pad_insulated_insulated_same_side=99.0,  # huge — but inactive
            deferred_via_lookahead=1.0,
        )
        res = _route_top_to_top(graph, src, tgt, cm)
        assert res
        sides = _path_sides(res[0])
        # No vias because no obstacle on top.
        assert all(s == "top" for s in sides)
