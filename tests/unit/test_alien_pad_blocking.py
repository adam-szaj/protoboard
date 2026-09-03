# SPDX-License-Identifier: MIT
"""Regression: A* must block pads that already belong to an alien electric set.

A pad with no component pin on it is *still* electrically claimed once a
prior wire of some net has touched it (the wire fused the pad into that
net's electric set).  A subsequent bare wire of a different net that
passes through the same pad shorts the two nets together — which used to
happen because `occupied_pads` only listed component pins, not every pad
in every alien electric set.
"""

from __future__ import annotations

import uuid

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.definition import (
    ComponentDefinition,
    MetaPin,
    PackageOption,
)
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package.generated_families import sil_family
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement, PlacementCollection
from protoboard.domain.layout.wire import Wire, WireCollection
from protoboard.domain.routing_settings import RoutingSettings
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.services.routing.autorouter import autoroute
from protoboard.types import Side
from tests.instance_utils import bind


def _perfboard(rows: int, cols: int) -> Board:
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


_SIL2_FAMILY = sil_family(2)
assert _SIL2_FAMILY is not None
_FAMILIES = {_SIL2_FAMILY.family_id: _SIL2_FAMILY}


def _sil2() -> ComponentDefinition:
    return ComponentDefinition(
        name="COMP",
        meta_pins=(MetaPin(("A",)), MetaPin(("B",))),
        package_options=(
            PackageOption(
                name="SIL-2",
                ref="sil-2/std",
                pin_map={1: (1,), 2: (2,)},
            ),
        ),
    )


def _place(cid, x, y, rotation=90):
    return ComponentPlacement(
        component_id=cid, position=Point(x, y), rotation=rotation, side="top"
    )


def _bare_wire(*pts: tuple[float, float], side: Side = "top") -> Wire:
    return Wire(
        id=uuid.uuid4(),
        path=[Point(*p) for p in pts],
        insulated=False,
        side=side,
    )


def _net_of(graph, schematic, pt: Point, side: str) -> set[str]:
    """Names of every schematic net whose canonical key matches (pt, side)."""
    ck = graph.node_for_pad(pt, side)
    if ck is None:
        return set()
    names = set()
    for net in schematic.nets.values():
        for ref in net.pins:
            if graph.node_for_pin(ref) == ck:
                names.add(net.name)
                break
    return names


class TestAlienPadBlocking:
    def test_existing_net_pad_chain_blocks_alien_route(self) -> None:
        """Pre-seed an 8-pad bare chain claiming NET_A through pads (1.5,0.5)..(8.5,0.5).

        Then ask the router to route NET_B between pins that bracket the chain
        on the same row.  The straight ortho path would steal NET_A's pads
        and short the two nets; the router must detour or fail."""  # noqa: E501
        board = _perfboard(rows=3, cols=12)
        defn = _sil2()
        # NET_A pins at (0.5,0.5) and (9.5,0.5) — both ends of an 8-pad chain.
        cid_a1, cid_a2 = uuid.uuid4(), uuid.uuid4()
        inst_a1 = bind(
            ComponentInstance(id=cid_a1, definition_ref="COMP", refdes="A1", value=""),
            defn,
            _FAMILIES,
        )
        inst_a2 = bind(
            ComponentInstance(id=cid_a2, definition_ref="COMP", refdes="A2", value=""),
            defn,
            _FAMILIES,
        )
        # NET_B pins outside the chain — (10.5,0.5) and (11.5,0.5) — and a
        # second NET_B pin at (10.5, 2.5) to force routing through row 0.
        cid_b1, cid_b2 = uuid.uuid4(), uuid.uuid4()
        inst_b1 = bind(
            ComponentInstance(id=cid_b1, definition_ref="COMP", refdes="B1", value=""),
            defn,
            _FAMILIES,
        )
        inst_b2 = bind(
            ComponentInstance(id=cid_b2, definition_ref="COMP", refdes="B2", value=""),
            defn,
            _FAMILIES,
        )

        layout = Layout(
            placements=PlacementCollection(
                {
                    cid_a1: _place(cid_a1, 0.5, 0.5),  # pin1=(0.5,0.5) pin2=(1.5,0.5)
                    cid_a2: _place(cid_a2, 8.5, 0.5),  # pin1=(8.5,0.5) pin2=(9.5,0.5)
                    cid_b1: _place(cid_b1, 0.5, 2.5),  # pin1=(0.5,2.5) pin2=(1.5,2.5)
                    cid_b2: _place(cid_b2, 9.5, 2.5),  # pin1=(9.5,2.5) pin2=(10.5,2.5)
                }
            ),
            wires=WireCollection(
                [
                    # NET_A chain on bottom: (1.5,0.5) → (8.5,0.5)
                    _bare_wire((1.5, 0.5), (2.5, 0.5), side="bottom"),
                    _bare_wire((2.5, 0.5), (3.5, 0.5), side="bottom"),
                    _bare_wire((3.5, 0.5), (4.5, 0.5), side="bottom"),
                    _bare_wire((4.5, 0.5), (5.5, 0.5), side="bottom"),
                    _bare_wire((5.5, 0.5), (6.5, 0.5), side="bottom"),
                    _bare_wire((6.5, 0.5), (7.5, 0.5), side="bottom"),
                    _bare_wire((7.5, 0.5), (8.5, 0.5), side="bottom"),
                ]
            ),
        )

        net_a = Net(
            id=uuid.uuid4(),
            name="NET_A",
            pins={
                PinRef(component_id=cid_a1, pin_number=2),  # (1.5, 0.5)
                PinRef(component_id=cid_a2, pin_number=1),  # (8.5, 0.5)
            },
        )
        net_b = Net(
            id=uuid.uuid4(),
            name="NET_B",
            pins={
                PinRef(component_id=cid_b1, pin_number=2),  # (1.5, 2.5)
                PinRef(component_id=cid_b2, pin_number=1),  # (9.5, 2.5)
            },
        )
        schematic = Schematic(nets={net_a.id: net_a, net_b.id: net_b})

        rs = RoutingSettings(wire_mode="bare")
        result = autoroute(
            board,
            layout,
            schematic,
            {"COMP": defn},
            {cid_a1: inst_a1, cid_a2: inst_a2, cid_b1: inst_b1, cid_b2: inst_b2},
            cost_model=rs.cost_model(),
            wire_mode=rs.wire_mode,
        )

        # After autoroute, NET_A and NET_B must remain distinct electric sets.
        # Build a fresh graph from the resulting layout.
        from dataclasses import replace

        final_layout = replace(layout, wires=list(layout.wires) + list(result.wires))
        from protoboard.domain.connectivity.incremental import ConnectivityGraph

        graph = ConnectivityGraph(
            board=board,
            layout=final_layout,
            components={"COMP": defn},
            instances={
                cid_a1: inst_a1,
                cid_a2: inst_a2,
                cid_b1: inst_b1,
                cid_b2: inst_b2,
            },
        )
        a_nets = _net_of(graph, schematic, Point(1.5, 0.5), "bottom")
        b_nets = _net_of(graph, schematic, Point(1.5, 2.5), "bottom")
        # No fusion: each pad sees only its own net.
        assert "NET_B" not in a_nets, f"NET_A pad ended up fused with NET_B: {a_nets}"
        assert "NET_A" not in b_nets, f"NET_B pad ended up fused with NET_A: {b_nets}"
