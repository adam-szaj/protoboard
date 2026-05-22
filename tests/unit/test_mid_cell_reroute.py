# SPDX-License-Identifier: MIT
"""Tests for mid-cell-preferred push-and-shove (Feature C).

``cost_model.max_reroute_factor > 0`` enables Strategy 1 of the
enhanced router's push-and-shove pipeline: the blocker is rerouted
under ``bare_diagonal_mode=True`` so any unavoidable crossing falls at
mesh-cell centres rather than on a pad.

Test plan:
* ``max_reroute_factor=0`` (default) → Strategy 1 returns None
  unconditionally; legacy behaviour preserved.
* ``max_reroute_factor>0`` → reroute attempted; budget cap respected.
* Already-rerouted wires are not picked twice in the same attempt loop.
"""

from __future__ import annotations

import uuid

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.connectivity.graph import ConnectivityGraph
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.wire import WireSegment
from protoboard.geometry import Point
from protoboard.services.routing.autorouter import _try_mid_cell_reroute
from protoboard.services.routing.cost_model import RoutingCostModel


def _perfboard(rows: int = 6, cols: int = 6) -> Board:
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


def _make_graph_with_blocker() -> tuple[ConnectivityGraph, WireSegment]:
    board = _perfboard()
    layout = Layout()
    graph = ConnectivityGraph(board, layout, {}, {})
    blocker = WireSegment(
        id=uuid.uuid4(),
        path=[
            Point(0.5, 2.5),
            Point(1.5, 2.5),
            Point(2.5, 2.5),
            Point(3.5, 2.5),
            Point(4.5, 2.5),
        ],
        insulated=False,
        side="top",
    )
    graph.add_wire(blocker)
    return graph, blocker


class TestCostModelKnobDefault:
    def test_max_reroute_factor_defaults_zero(self) -> None:
        assert RoutingCostModel().max_reroute_factor == 0.0

    def test_field_is_settable(self) -> None:
        cm = RoutingCostModel(max_reroute_factor=1.5)
        assert cm.max_reroute_factor == 1.5


class TestDisabledByDefault:
    def test_returns_none_when_factor_is_zero(self) -> None:
        graph, blocker = _make_graph_with_blocker()
        cm = RoutingCostModel()  # max_reroute_factor=0
        out = _try_mid_cell_reroute(
            source=Point(0.5, 0.5),
            target=Point(4.5, 4.5),
            working_wires=[blocker],
            additional_wires=[],
            connectivity_graph=graph,
            cost_model=cm,
            occupied_pins={},
            same_net_pins={},
            plated_through_pads=set(),
            under_component_penalty={},
            already_rerouted=set(),
        )
        assert out is None


class TestAlreadyReroutedSetRespected:
    def test_wire_in_already_rerouted_is_skipped(self) -> None:
        """A wire whose id sits in ``already_rerouted`` must not be picked
        again in the same attempts loop."""
        graph, blocker = _make_graph_with_blocker()
        cm = RoutingCostModel(max_reroute_factor=99.0)  # very permissive
        out = _try_mid_cell_reroute(
            source=Point(0.5, 0.5),
            target=Point(4.5, 4.5),
            working_wires=[blocker],
            additional_wires=[],
            connectivity_graph=graph,
            cost_model=cm,
            occupied_pins={},
            same_net_pins={},
            plated_through_pads=set(),
            under_component_penalty={},
            already_rerouted={blocker.id},
        )
        assert out is None


class TestProtectedWiresSkipped:
    def test_additional_wires_are_protected(self) -> None:
        """Wires passed via ``additional_wires`` (typically pinned to the
        current net) must never be rerouted."""
        graph, blocker = _make_graph_with_blocker()
        cm = RoutingCostModel(max_reroute_factor=99.0)
        out = _try_mid_cell_reroute(
            source=Point(0.5, 0.5),
            target=Point(4.5, 4.5),
            working_wires=[blocker],
            additional_wires=[blocker],  # protected!
            connectivity_graph=graph,
            cost_model=cm,
            occupied_pins={},
            same_net_pins={},
            plated_through_pads=set(),
            under_component_penalty={},
            already_rerouted=set(),
        )
        assert out is None


class TestBudgetCap:
    def test_tight_budget_rejects_reroute(self) -> None:
        """``max_reroute_factor`` close to 0 forbids any cost increase →
        the rerouter cannot return a longer path even if one exists."""
        graph, blocker = _make_graph_with_blocker()
        cm = RoutingCostModel(max_reroute_factor=0.01)  # essentially 0
        # The original Chebyshev cost is 4.  Budget = 4 * 0.01 = 0.04 →
        # nothing can beat that, so rerouter rejects whatever it finds.
        out = _try_mid_cell_reroute(
            source=Point(0.5, 0.5),
            target=Point(4.5, 4.5),
            working_wires=[blocker],
            additional_wires=[],
            connectivity_graph=graph,
            cost_model=cm,
            occupied_pins={},
            same_net_pins={},
            plated_through_pads=set(),
            under_component_penalty={},
            already_rerouted=set(),
        )
        # Either no reroute (budget exhausted) or reroute returns same path.
        if out is not None:
            old_wire, new_wire = out
            # Same path is excluded by the helper.  So if it returned a
            # tuple, the cost must satisfy the budget — which won't happen
            # with factor=0.01 on a 4-step blocker.
            assert False, "tight budget should not yield a reroute"

    def test_blocker_outside_corridor_skipped(self) -> None:
        """A wire that doesn't run through the source↔target bbox isn't
        even considered for reroute."""
        graph, _ = _make_graph_with_blocker()
        # Add a different blocker, far from the corridor.
        far = WireSegment(
            id=uuid.uuid4(),
            path=[Point(0.5, 5.5), Point(4.5, 5.5)],
            insulated=False,
            side="top",
        )
        graph.add_wire(far)
        cm = RoutingCostModel(max_reroute_factor=99.0)
        out = _try_mid_cell_reroute(
            source=Point(0.5, 0.5),
            target=Point(0.5, 4.5),  # vertical corridor at x=0.5
            working_wires=[far],  # blocker not in corridor
            additional_wires=[],
            connectivity_graph=graph,
            cost_model=cm,
            occupied_pins={},
            same_net_pins={},
            plated_through_pads=set(),
            under_component_penalty={},
            already_rerouted=set(),
        )
        assert out is None
