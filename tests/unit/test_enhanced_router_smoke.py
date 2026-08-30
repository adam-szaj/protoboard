# SPDX-License-Identifier: MIT
"""Smoke tests for :class:`EnhancedAStarRouter`.

The enhanced router is a sibling of :class:`AStarRouter` whose contract
is: **with all extra knobs at their defaults (=0 / disabled), it must
produce the same output as the default ``astar`` router.**  Behavioural
parity in the defaults state is what lets the user opt in without
fearing regressions in already-routed projects.

PRs 3-5 will add the enhanced cost shaping; until then the enhanced
router is functionally identical to ``astar``, so the parity test is
strict (canonical wire-set equality).
"""

from __future__ import annotations

import uuid
from typing import Literal

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement, PlacementCollection
from protoboard.domain.routing_settings import RoutingSettings
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.services.routing.builtin import AStarRouter
from protoboard.services.routing.builtin_enhanced import EnhancedAStarRouter
from protoboard.services.routing.optimizer import registry as opt_registry  # noqa: F401
from protoboard.services.routing.protocol import (
    ProgressSink,
    Router,
    RouterCapabilities,
    registry,
)
from protoboard.services.routing.snapshot import RouteRequest
from tests.instance_utils import bind
from tests.unit._autoroute_invariants import make_sil, sil_families


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


def _sil2() -> ComponentDefinition:
    return make_sil("COMP")


def _place(
    cid: uuid.UUID, x: float, y: float, rotation: int = 90
) -> ComponentPlacement:
    return ComponentPlacement(
        component_id=cid, position=Point(x, y), rotation=rotation, side="top"
    )


def _two_pin_project(
    wire_mode: Literal["bare", "insulated", "auto", "bare_diagonal"] = "insulated",
) -> RouteRequest:
    board = _perfboard()
    defn = _sil2()
    families = sil_families(defn)
    cid_a, cid_b = uuid.uuid4(), uuid.uuid4()
    insts = {
        cid_a: bind(
            ComponentInstance(id=cid_a, definition_ref="COMP", refdes="R1", value=""),
            defn,
            families,
        ),
        cid_b: bind(
            ComponentInstance(id=cid_b, definition_ref="COMP", refdes="R2", value=""),
            defn,
            families,
        ),
    }
    layout = Layout(
        placements=PlacementCollection(
            {
                cid_a: _place(cid_a, 0.5, 0.5),
                cid_b: _place(cid_b, 3.5, 3.5),
            }
        )
    )
    net = Net(
        id=uuid.uuid4(),
        name="N1",
        pins={
            PinRef(component_id=cid_a, pin_number=2),
            PinRef(component_id=cid_b, pin_number=1),
        },
    )
    schematic = Schematic(nets={net.id: net})
    return RouteRequest.from_session(
        board=board,
        layout=layout,
        schematic=schematic,
        components={"COMP": defn},
        instances=insts,
        routing_settings=RoutingSettings(wire_mode=wire_mode),
    )


def _canon(wires):
    """Order-independent canonical form for wire comparisons."""
    return sorted(
        (tuple(sorted((p.x, p.y) for p in w.path)), w.side, w.insulated) for w in wires
    )


# ---------------------------------------------------------------------------
# Protocol & registry conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_satisfies_router_protocol(self) -> None:
        assert isinstance(EnhancedAStarRouter(), Router)

    def test_registered_at_import(self) -> None:
        import protoboard.services.routing.builtin_enhanced  # noqa: F401

        assert "astar_enhanced" in registry.list_available()

    def test_default_router_is_still_astar(self) -> None:
        """Adding the enhanced router must not change registry.default()."""
        import protoboard.services.routing.builtin  # noqa: F401
        import protoboard.services.routing.builtin_enhanced  # noqa: F401

        assert registry.default().name == "astar"

    def test_capabilities_advertise_experimental_notes(self) -> None:
        caps = EnhancedAStarRouter().capabilities()
        assert isinstance(caps, RouterCapabilities)
        assert caps.supports_layer_change
        assert caps.supports_bare_wires
        assert caps.supports_push_and_shove
        assert "experimental" in caps.notes.lower() or "enhanced" in caps.notes.lower()


# ---------------------------------------------------------------------------
# Parity with default astar (defaults state)
# ---------------------------------------------------------------------------


class TestParityWithDefaultAStar:
    def test_two_pin_insulated_matches_astar(self) -> None:
        request = _two_pin_project(wire_mode="insulated")
        a = AStarRouter().route(request, ProgressSink())
        e = EnhancedAStarRouter().route(request, ProgressSink())
        assert _canon(a.wires) == _canon(e.wires)
        assert a.unrouted_nets == e.unrouted_nets

    def test_two_pin_bare_matches_astar(self) -> None:
        request = _two_pin_project(wire_mode="bare")
        a = AStarRouter().route(request, ProgressSink())
        e = EnhancedAStarRouter().route(request, ProgressSink())
        assert _canon(a.wires) == _canon(e.wires)
        assert a.unrouted_nets == e.unrouted_nets

    def test_two_pin_bare_diagonal_matches_astar(self) -> None:
        request = _two_pin_project(wire_mode="bare_diagonal")
        a = AStarRouter().route(request, ProgressSink())
        e = EnhancedAStarRouter().route(request, ProgressSink())
        assert _canon(a.wires) == _canon(e.wires)
        assert a.unrouted_nets == e.unrouted_nets
