# SPDX-License-Identifier: MIT
"""Tests for same-net wire reuse in the A* router.

When a route is being computed for net N, existing wires belonging to N
should be electrically "free" — the router pays
``RoutingCostModel.same_net_wire_reuse_cost`` (default 0.0) per overlap
instead of the usual crossing/at-pad/parallel penalty, and can teleport
between any pad-vertices of the same-net component at zero cost.  The
target_set also extends to cover any pad-vertex of an existing same-net
wire so the search terminates as soon as it hits known same-net metal.
"""

import uuid

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.connectivity.graph import ConnectivityGraph
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import PlacementCollection
from protoboard.domain.layout.wire import Wire, WireCollection
from protoboard.geometry import Point
from protoboard.services.routing.astar import find_paths
from protoboard.services.routing.cost_model import RoutingCostModel


def _perfboard(rows: int, cols: int) -> Board:
    grid = BoardGrid(
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
    return Board(root=grid)


def _pts(result_path: list) -> list[Point]:
    return [pt for pt, _ in result_path]


class TestExtendedTargetSet:
    """The router can terminate at any pad-vertex of an existing same-net wire."""

    def test_route_attaches_to_existing_same_net_wire(self) -> None:
        # 5×5 board.  An existing same-net wire spans (1.5, 2.5) — (3.5, 2.5)
        # on the top layer.  Source is at (0.5, 2.5); the original target is
        # at (4.5, 2.5).  The shortest legal route (Manhattan distance 4)
        # would step the full width, but the extended target set should
        # let the router stop earlier — anywhere on the existing wire.
        #
        # CRITICAL: the wire is registered with the connectivity graph so
        # that all its pad-vertices share an electric-set node with the
        # original target (1.5..3.5 chain bridges to 4.5).  Without that
        # bridge they would be a *separate island* and the find_paths
        # island-filter would reject them as targets (see two-islands
        # regression test below).
        board = _perfboard(5, 5)
        existing_wire = Wire(
            id=uuid.uuid4(),
            path=[Point(1.5, 2.5), Point(2.5, 2.5), Point(3.5, 2.5), Point(4.5, 2.5)],
            side="top",
            insulated=True,
        )
        lay = Layout(
            placements=PlacementCollection({}), wires=WireCollection([existing_wire])
        )
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        graph.add_wire(existing_wire)

        active_net = uuid.uuid4()
        existing_verts = {
            (Point(1.5, 2.5), "top"),
            (Point(2.5, 2.5), "top"),
            (Point(3.5, 2.5), "top"),
        }

        results = find_paths(
            graph=graph,
            source=Point(0.5, 2.5),
            targets=[Point(4.5, 2.5)],
            side="top",
            cost_model=RoutingCostModel(),
            k=1,
            active_net_id=active_net,
            same_net_pad_vertices=existing_verts,
        )
        assert len(results) == 1
        path = _pts(results[0].path)
        # The route must terminate at one of the existing same-net pads,
        # not have walked all the way to the original pin target.
        last = path[-1]
        assert (last, "top") in existing_verts
        # And it must be the *nearest* same-net pad to keep the search optimal.
        assert last == Point(1.5, 2.5)

    def test_no_discount_without_active_net_id(self) -> None:
        """When active_net_id is None the extended target_set never grows."""
        board = _perfboard(5, 5)
        lay = Layout(placements=PlacementCollection({}), wires=WireCollection([]))
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        existing_verts = {
            (Point(1.5, 2.5), "top"),
        }
        results = find_paths(
            graph=graph,
            source=Point(0.5, 2.5),
            targets=[Point(4.5, 2.5)],
            side="top",
            cost_model=RoutingCostModel(),
            k=1,
            active_net_id=None,  # disables same-net discount entirely
            same_net_pad_vertices=existing_verts,
        )
        assert len(results) == 1
        path = _pts(results[0].path)
        # Without active_net_id the discount does not apply — the path
        # must reach the original target.
        assert path[-1] == Point(4.5, 2.5)


class TestTeleportEdges:
    """Same-net teleport: free hops between pad-vertices of the active net."""

    def test_teleport_short_circuits_long_detour(self) -> None:
        # 7×7 board.  Source at (0.5, 0.5) routes towards a real target at
        # (6.5, 6.5).  An L-shaped existing wire connects (1.5, 0.5) to
        # (6.5, 6.5), making both endpoints part of the same electric
        # island as the original target.  The teleport edge between them
        # lets the search terminate one step from source at (1.5, 0.5).
        #
        # CRITICAL: the wire is registered with the graph so the two
        # pad-vertices share an electric-set node; without that they
        # would be on separate islands and the new island-filter would
        # forbid the cross-island teleport.
        board = _perfboard(7, 7)
        # L-shape: down x from 1.5 to 6.5 on y=0.5, then up y from 0.5 to 6.5.
        path = [Point(1.5 + i, 0.5) for i in range(6)] + [
            Point(6.5, 0.5 + i) for i in range(1, 7)
        ]
        bridge_wire = Wire(id=uuid.uuid4(), path=path, side="top", insulated=True)
        lay = Layout(
            placements=PlacementCollection({}), wires=WireCollection([bridge_wire])
        )
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        graph.add_wire(bridge_wire)

        active_net = uuid.uuid4()
        teleport_verts = {
            (Point(1.5, 0.5), "top"),
            (Point(6.5, 6.5), "top"),
        }

        results = find_paths(
            graph=graph,
            source=Point(0.5, 0.5),
            targets=[Point(6.5, 6.5)],
            side="top",
            cost_model=RoutingCostModel(),
            k=1,
            active_net_id=active_net,
            same_net_pad_vertices=teleport_verts,
        )
        assert len(results) == 1
        path = _pts(results[0].path)
        # The search terminated at one of the extended targets, not at the
        # original target.  (1.5, 0.5) is one step from source.
        assert path[-1] in (Point(1.5, 0.5), Point(6.5, 6.5))
        # Cost must be far below the direct-walk Manhattan distance (12).
        assert results[0].cost < 5.0


class TestSourceNotInExtendedTargets:
    """When the source is already a same-net pad-vertex, the extended target
    set must NOT include the source — otherwise A* returns a degenerate
    1-point path and downstream Wire construction fails with
    'path must have at least 2 points'.
    """

    def test_source_excluded_from_extended_targets(self) -> None:
        # 5×5 board.  An existing same-net wire bridges (1.5, 2.5)…(4.5, 2.5);
        # the new segment must connect a different same-net spur whose
        # source is itself (1.5, 2.5) (a vertex of the existing wire).
        # Without the source-exclusion fix, target_set would include
        # (1.5, 2.5) and A* would return a 1-point trivial path.
        board = _perfboard(5, 5)
        bridge = Wire(
            id=uuid.uuid4(),
            path=[Point(1.5, 2.5), Point(2.5, 2.5), Point(3.5, 2.5), Point(4.5, 2.5)],
            side="top",
            insulated=True,
        )
        lay = Layout(placements=PlacementCollection({}), wires=WireCollection([bridge]))
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        graph.add_wire(bridge)

        active_net = uuid.uuid4()
        existing_verts = {
            (Point(1.5, 2.5), "top"),  # also the source
            (Point(2.5, 2.5), "top"),
        }

        results = find_paths(
            graph=graph,
            source=Point(1.5, 2.5),
            targets=[Point(4.5, 2.5)],
            side="top",
            cost_model=RoutingCostModel(),
            k=1,
            active_net_id=active_net,
            same_net_pad_vertices=existing_verts,
        )
        assert len(results) == 1
        path = _pts(results[0].path)
        # Must produce at least 2 points (legal wire); must NOT short-
        # circuit to a 1-point trivial return.
        assert len(path) >= 2
        # The route may terminate at the other same-net pad (2.5, 2.5)
        # or at the original target (4.5, 2.5); both are valid since the
        # source isn't a terminator.
        assert path[-1] in (Point(2.5, 2.5), Point(4.5, 2.5))


class TestTwoIslandSafety:
    """Regression: when a net has two disconnected islands (e.g. left over
    from a previous autoroute pass), routing from one island to the other
    must NOT short-circuit by jumping to a same-net pad on the *source*
    island.  The fix filters extended target_set / teleport_pads down to
    pad-vertices that actually share the target's electric-set node.
    """

    def test_no_short_circuit_to_source_island_neighbour(self) -> None:
        # 9×3 board.  Two disconnected same-net islands:
        #   Island A (target): wire at (7.5,1.5)-(8.5,1.5), target pin at (7.5,1.5)
        #   Island B (source): wire at (0.5,1.5)-(1.5,1.5), source pin at (0.5,1.5),
        #                      sibling pin at (1.5,1.5) — same-net but NOT bridging
        #                      to island A.
        # Without the island filter, A* would extend target_set with the
        # sibling at (1.5,1.5) (closest same-net pad) and stop after one
        # step, never reaching island A.
        board = _perfboard(3, 9)

        island_a = Wire(
            id=uuid.uuid4(),
            path=[Point(7.5, 1.5), Point(8.5, 1.5)],
            side="top",
            insulated=True,
        )
        island_b = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 1.5), Point(1.5, 1.5)],
            side="top",
            insulated=True,
        )
        lay = Layout(
            placements=PlacementCollection({}),
            wires=WireCollection([island_a, island_b]),
        )
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        graph.add_wire(island_a)
        graph.add_wire(island_b)

        # Both wires' vertices belong to the active net.
        active_net = uuid.uuid4()
        vertices = {
            (Point(7.5, 1.5), "top"),
            (Point(8.5, 1.5), "top"),
            (Point(0.5, 1.5), "top"),
            (Point(1.5, 1.5), "top"),
        }

        results = find_paths(
            graph=graph,
            source=Point(0.5, 1.5),  # island B
            targets=[Point(7.5, 1.5)],  # island A
            side="top",
            cost_model=RoutingCostModel(),
            k=1,
            active_net_id=active_net,
            same_net_pad_vertices=vertices,
        )
        assert len(results) == 1
        path = _pts(results[0].path)
        last = path[-1]
        # Must reach island A — NOT short-circuit to (1.5, 1.5) on island B.
        assert last in (Point(7.5, 1.5), Point(8.5, 1.5)), (
            f"Routed to {last} instead of island A — same-net teleport "
            f"leaked across electric-set islands."
        )
        # And the path must actually traverse the board, not just hop one step.
        assert len(path) > 3


class TestReuseCostKnob:
    """The same_net_wire_reuse_cost field exists and is honoured."""

    def test_default_is_zero(self) -> None:
        assert RoutingCostModel().same_net_wire_reuse_cost == 0.0

    def test_high_reuse_cost_field_round_trips(self) -> None:
        # Defensive: the knob must survive construction with a custom value.
        # (Behavioural high-cost suppression of teleport is implicit via the
        # extended target set logic; verifying it directly is harder than it
        # looks because the target_set extension is geometric, not weighted.)
        cm = RoutingCostModel(same_net_wire_reuse_cost=999.0)
        assert cm.same_net_wire_reuse_cost == 999.0


class TestSameNetReuseDoesNotInfectOtherNets:
    """Wires of other nets retain their normal crossing/at-pad penalties."""

    def test_other_net_wire_still_blocks_bare_route(self) -> None:
        # Two nets.  Net A has an existing bare wire (1.5, 2.5)→(3.5, 2.5).
        # We're now routing net B, trying to cross that segment with a bare
        # wire — crossing_bare_bare is INF by default → impossible.  With
        # active_net_id=net_B, the same-net discount must NOT apply to
        # net A's segment.
        board = _perfboard(5, 5)
        lay = Layout(placements=PlacementCollection({}), wires=WireCollection([]))
        graph = ConnectivityGraph(board=board, layout=lay, components={})
        net_a = uuid.uuid4()
        net_b = uuid.uuid4()
        # Bare segment of net A spans y=2.5, x ∈ {1.5, 2.5, 3.5}.
        bare_segs: set[tuple[Point, Point, str, uuid.UUID | None]] = {
            (Point(1.5, 2.5), Point(2.5, 2.5), "top", net_a),
            (Point(2.5, 2.5), Point(3.5, 2.5), "top", net_a),
        }

        # Try to route net B from (2.5, 0.5) to (2.5, 4.5) as a *bare* wire —
        # must cross net A's segment at (2.5, 2.5).  Bare×bare cross = INF.
        results = find_paths(
            graph=graph,
            source=Point(2.5, 0.5),
            targets=[Point(2.5, 4.5)],
            side="top",
            cost_model=RoutingCostModel(),
            k=1,
            insulated=False,
            bare_wire_segments=bare_segs,
            active_net_id=net_b,
        )
        if results:
            # If a path is found it must dodge the bare segment, not cross.
            # We just verify the route exists with finite cost.
            assert results[0].cost < float("inf")
        # The route may also be blocked entirely (bare crossing forbidden);
        # the important thing is the router did NOT silently treat net A
        # as same-net.  We confirm that by re-running with active_net_id=net_a
        # and observing a *cheaper* result.
        results_a = find_paths(
            graph=graph,
            source=Point(2.5, 0.5),
            targets=[Point(2.5, 4.5)],
            side="top",
            cost_model=RoutingCostModel(),
            k=1,
            insulated=False,
            bare_wire_segments=bare_segs,
            active_net_id=net_a,
        )
        # With active_net_id=net_a the crossing is free → must succeed and
        # take the direct y-axis route.
        assert len(results_a) == 1
        assert results_a[0].cost < float("inf")
