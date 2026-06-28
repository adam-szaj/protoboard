# SPDX-License-Identifier: MIT
"""Tests for SimplifyOptimizer — the plugin wrapper around simplify_wires_generic.

Behavioural parity with the legacy inline simplify pass in ``autoroute()``
is the most important property: a project routed with the old code path
must produce the same wire layout as the same project routed through the
optimizer.
"""

from __future__ import annotations

import uuid

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.routing_settings import RoutingSettings
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.services.routing.autorouter import AutorouteResult, autoroute
from protoboard.services.routing.builtin_optimizers import SimplifyOptimizer
from protoboard.services.routing.optimizer import (
    OptimizerCapabilities,
    WireOptimizer,
    registry,
)
from protoboard.services.routing.postprocess import SimplifyMode
from protoboard.services.routing.protocol import ProgressSink
from protoboard.services.routing.snapshot import RouteRequest

from tests.instance_utils import bind


def _perfboard(rows: int = 5, cols: int = 5) -> Board:
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


def _sil2() -> ComponentDefinition:
    return ComponentDefinition(
        name="COMP", footprint=SIL(n=2), pin_names={1: "A", 2: "B"}
    )


def _place(cid: uuid.UUID, x: float, y: float, rotation: int = 90) -> ComponentPlacement:
    return ComponentPlacement(
        component_id=cid, position=Point(x, y), rotation=rotation, side="top"
    )


def _two_pin_diag_project() -> tuple[Board, Layout, Schematic, dict, dict]:
    """A 5×5 board with two SIL2 parts placed diagonally — bare_diagonal route."""
    board = _perfboard()
    defn = _sil2()
    cid_a, cid_b = uuid.uuid4(), uuid.uuid4()
    inst_a = bind(ComponentInstance(id=cid_a, definition_ref="COMP", refdes="R1", value=""), defn, {})
    inst_b = bind(ComponentInstance(id=cid_b, definition_ref="COMP", refdes="R2", value=""), defn, {})
    layout = Layout(placements={
        cid_a: _place(cid_a, 0.5, 0.5),
        cid_b: _place(cid_b, 3.5, 3.5),
    })
    ref_a2 = PinRef(component_id=cid_a, pin_number=2)
    ref_b1 = PinRef(component_id=cid_b, pin_number=1)
    net = Net(id=uuid.uuid4(), name="N1", pins={ref_a2, ref_b1})
    schematic = Schematic(nets={net.id: net})
    return board, layout, schematic, {"COMP": defn}, {cid_a: inst_a, cid_b: inst_b}


class TestProtocolConformance:
    def test_satisfies_wire_optimizer_protocol(self) -> None:
        assert isinstance(SimplifyOptimizer(), WireOptimizer)

    def test_registered_at_import(self) -> None:
        import protoboard.services.routing.builtin_optimizers  # noqa: F401
        assert "simplify" in registry.list_available()

    def test_capabilities_advertise_shortens_paths(self) -> None:
        caps = SimplifyOptimizer().capabilities()
        assert caps.shortens_paths
        assert not caps.rebalances_sides
        assert isinstance(caps, OptimizerCapabilities)


class TestEmptyInputs:
    def test_empty_wires_return_unchanged(self) -> None:
        opt = SimplifyOptimizer()
        req = RouteRequest.from_session(
            board=_perfboard(), layout=Layout(),
            schematic=Schematic(nets={}),
            components={}, instances={},
            routing_settings=RoutingSettings(),
        )
        result = AutorouteResult(wires=[], unrouted_nets=[], pass_stats=[])
        assert opt.optimize(req, result, ProgressSink()) is result

    def test_cancelled_returns_input_unchanged(self) -> None:
        """If the sink reports cancellation, the optimizer is a no-op."""
        import threading
        board, layout, sch, comps, insts = _two_pin_diag_project()
        rs = RoutingSettings(wire_mode="bare_diagonal", enable_simplify_pass=False)
        autoroute_result = autoroute(
            board, layout, sch, comps, insts,
            cost_model=rs.cost_model(),
            wire_mode=rs.wire_mode,
            enable_simplify_pass=False,
        )
        assert autoroute_result.wires  # routed something

        req = RouteRequest.from_session(
            board=board, layout=layout, schematic=sch,
            components=comps, instances=insts,
            routing_settings=rs,
        )
        ev = threading.Event(); ev.set()
        sink = ProgressSink(cancel_flag=ev)
        result_in = AutorouteResult(
            wires=list(autoroute_result.wires),
            unrouted_nets=list(autoroute_result.unrouted_nets),
            pass_stats=list(autoroute_result.pass_stats),
        )
        result_out = SimplifyOptimizer().optimize(req, result_in, sink)
        assert result_out is result_in


class TestParityWithLegacyInlinePass:
    """The plugin must produce the same wires as the legacy inline pass."""

    def test_diagonal_route_matches_legacy(self) -> None:
        board, layout, sch, comps, insts = _two_pin_diag_project()
        rs = RoutingSettings(wire_mode="bare_diagonal")

        # Path A: rely on autoroute()'s embedded simplify pass.
        legacy_result = autoroute(
            board, layout, sch, comps, insts,
            cost_model=rs.cost_model(),
            wire_mode=rs.wire_mode,
            enable_simplify_pass=True,
            simplify_mode=SimplifyMode.BARE_PARITY,
        )

        # Path B: autoroute without simplify, then run the optimizer.
        unsimplified = autoroute(
            board, layout, sch, comps, insts,
            cost_model=rs.cost_model(),
            wire_mode=rs.wire_mode,
            enable_simplify_pass=False,
        )
        req = RouteRequest.from_session(
            board=board, layout=layout, schematic=sch,
            components=comps, instances=insts,
            routing_settings=rs,
        )
        optimized = SimplifyOptimizer(mode=SimplifyMode.BARE_PARITY).optimize(
            req,
            AutorouteResult(
                wires=list(unsimplified.wires),
                unrouted_nets=list(unsimplified.unrouted_nets),
                pass_stats=list(unsimplified.pass_stats),
            ),
            ProgressSink(),
        )

        # Same set of paths regardless of wire IDs and direction.
        def _canon(wires):
            return sorted(
                tuple(sorted((p.x, p.y) for p in w.path))
                for w in wires
            )

        assert _canon(optimized.wires) == _canon(legacy_result.wires)


class TestNonMutation:
    def test_optimizer_does_not_mutate_input_result(self) -> None:
        board, layout, sch, comps, insts = _two_pin_diag_project()
        rs = RoutingSettings(wire_mode="bare_diagonal", enable_simplify_pass=False)
        routed = autoroute(
            board, layout, sch, comps, insts,
            cost_model=rs.cost_model(),
            wire_mode=rs.wire_mode,
            enable_simplify_pass=False,
        )
        req = RouteRequest.from_session(
            board=board, layout=layout, schematic=sch,
            components=comps, instances=insts,
            routing_settings=rs,
        )
        in_wires = list(routed.wires)
        in_id = id(in_wires)
        in_snapshot = [w.id for w in in_wires]

        result_in = AutorouteResult(
            wires=in_wires,
            unrouted_nets=list(routed.unrouted_nets),
            pass_stats=list(routed.pass_stats),
        )
        out = SimplifyOptimizer(mode=SimplifyMode.BARE_PARITY).optimize(
            req, result_in, ProgressSink(),
        )

        # The optimizer must return a fresh AutorouteResult, not in-place.
        assert out is not result_in
        # The input wires list identity is preserved (no in-place rewrite).
        assert id(result_in.wires) == in_id
        assert [w.id for w in result_in.wires] == in_snapshot
