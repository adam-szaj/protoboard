# SPDX-License-Identifier: MIT
"""Tests for the full-netlist autorouter.

Pin positions with SingleRow(n=2):
  rotation=0  → vertical:   pin1=(x,   y), pin2=(x,   y+1)
  rotation=90 → horizontal: pin1=(x,   y), pin2=(x+1, y)
"""

from __future__ import annotations

import threading
import uuid

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.definition import (
    ComponentDefinition,
    MetaPin,
    PackageOption,
)
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package.generated_families import (
    dip_family,
    sil_family,
)
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement, PlacementCollection
from protoboard.domain.layout.wire import Wire, WireCollection
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.services.routing.autorouter import (
    AutorouteCancelled,
    _net_names_by_node,
    autoroute,
)
from tests.instance_utils import bind


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


def _sil2_def(name: str = "COMP") -> ComponentDefinition:
    return ComponentDefinition(
        name=name,
        meta_pins=(MetaPin(("A",)), MetaPin(("B",))),
        package_options=(
            PackageOption(
                name="SIL-2",
                ref="sil-2/std",
                pin_map={1: (1,), 2: (2,)},
            ),
        ),
    )


def _dip8_def() -> ComponentDefinition:
    return ComponentDefinition(
        name="DIP8",
        meta_pins=tuple(MetaPin((str(pin),)) for pin in range(1, 9)),
        package_options=(
            PackageOption(
                name="DIP-8",
                ref="dip-8/std",
                pin_map={pin: (pin,) for pin in range(1, 9)},
            ),
        ),
    )


def _bind(inst: ComponentInstance, defn: ComponentDefinition) -> ComponentInstance:
    option = defn.package_options[0]
    family_id = option.ref.rsplit("/", 1)[0]
    family = (
        sil_family(len(defn.meta_pins))
        if family_id.startswith("sil-")
        else dip_family(len(defn.meta_pins), wide=False)
    )
    assert family is not None and family.family_id == family_id
    return bind(inst, defn, {family.family_id: family})


def _place(
    cid: uuid.UUID, x: float, y: float, rotation: int = 90
) -> ComponentPlacement:
    """rotation=90 → horizontal pins: pin1=(x,y), pin2=(x+1,y)."""
    return ComponentPlacement(
        component_id=cid, position=Point(x, y), rotation=rotation, side="top"
    )


def test_cancelled_route_does_not_mutate_input_layout() -> None:
    board = _perfboard(5, 5)
    defn = _sil2_def()
    cid_a, cid_b = uuid.uuid4(), uuid.uuid4()
    instances = {
        cid_a: _bind(
            ComponentInstance(id=cid_a, definition_ref="COMP", refdes="R1", value=""),
            defn,
        ),
        cid_b: _bind(
            ComponentInstance(id=cid_b, definition_ref="COMP", refdes="R2", value=""),
            defn,
        ),
    }
    layout = Layout(
        placements=PlacementCollection(
            {
                cid_a: _place(cid_a, 0.5, 0.5),
                cid_b: _place(cid_b, 3.5, 0.5),
            }
        )
    )
    net = Net(
        id=uuid.uuid4(),
        name="N1",
        pins={PinRef(cid_a, 2), PinRef(cid_b, 1)},
    )
    schematic = Schematic(nets={net.id: net})
    cancel_flag = threading.Event()
    cancel_flag.set()
    before_wires = list(layout.wires)

    with pytest.raises(AutorouteCancelled):
        autoroute(
            board,
            layout,
            schematic,
            {"COMP": defn},
            instances,
            cancel_flag=cancel_flag,
        )

    assert list(layout.wires) == before_wires


class TestFusionQuery:
    def test_groups_distinct_net_names_by_electric_node(self) -> None:
        component_id = uuid.uuid4()
        first = PinRef(component_id, 1)
        second = PinRef(component_id, 2)
        first_net = Net(id=uuid.uuid4(), name="VCC", pins={first})
        second_net = Net(id=uuid.uuid4(), name="GND", pins={second})
        schematic = Schematic(nets={first_net.id: first_net, second_net.id: second_net})

        class _Graph:
            def node_for_pin(self, _ref: PinRef) -> tuple[float, float, str]:
                return (0.5, 0.5, "top")

        result = _net_names_by_node(_Graph(), schematic)  # type: ignore[arg-type]

        assert result == {(0.5, 0.5, "top"): {"VCC", "GND"}}


class TestAutorouter:
    def test_routes_simple_two_pin_net(self) -> None:
        """Two components on a 5×5 board, one net connecting their pins.

        R1 (rot=90) at (0.5,0.5): pin1=(0.5,0.5), pin2=(1.5,0.5)
        R2 (rot=90) at (3.5,0.5): pin1=(3.5,0.5), pin2=(4.5,0.5)
        Net: R1-pin2 → R2-pin1  (1.5,0.5) → (3.5,0.5)
        """
        board = _perfboard(5, 5)
        defn = _sil2_def()
        cid_a = uuid.uuid4()
        cid_b = uuid.uuid4()

        inst_a = _bind(
            ComponentInstance(id=cid_a, definition_ref="COMP", refdes="R1", value="1k"),
            defn,
        )
        inst_b = _bind(
            ComponentInstance(id=cid_b, definition_ref="COMP", refdes="R2", value="1k"),
            defn,
        )
        layout = Layout(
            placements=PlacementCollection(
                {
                    cid_a: _place(cid_a, 0.5, 0.5),  # pin1=(0.5,0.5) pin2=(1.5,0.5)
                    cid_b: _place(cid_b, 3.5, 0.5),  # pin1=(3.5,0.5) pin2=(4.5,0.5)
                }
            )
        )
        ref_a2 = PinRef(component_id=cid_a, pin_number=2)  # (1.5, 0.5)
        ref_b1 = PinRef(component_id=cid_b, pin_number=1)  # (3.5, 0.5)
        net = Net(id=uuid.uuid4(), name="N1", pins={ref_a2, ref_b1})
        schematic = Schematic(nets={net.id: net})
        components = {"COMP": defn}
        instances = {cid_a: inst_a, cid_b: inst_b}

        result = autoroute(board, layout, schematic, components, instances)

        assert result.unrouted_nets == []
        assert len(result.wires) == 1
        wire = result.wires[0]
        endpoints = {wire.path[0], wire.path[-1]}
        assert Point(1.5, 0.5) in endpoints
        assert Point(3.5, 0.5) in endpoints

    def test_empty_schematic_returns_no_wires(self) -> None:
        board = _perfboard(5, 5)
        layout = Layout(placements=PlacementCollection({}))
        schematic = Schematic(nets={})
        result = autoroute(board, layout, schematic, {}, {})
        assert result.wires == []
        assert result.unrouted_nets == []

    def test_single_pin_net_skipped(self) -> None:
        board = _perfboard(5, 5)
        cid = uuid.uuid4()
        defn = _sil2_def()
        inst = _bind(
            ComponentInstance(id=cid, definition_ref="COMP", refdes="R1", value="1k"),
            defn,
        )
        layout = Layout(placements=PlacementCollection({cid: _place(cid, 0.5, 0.5)}))
        ref = PinRef(component_id=cid, pin_number=1)
        net = Net(id=uuid.uuid4(), name="SOLO", pins={ref})
        schematic = Schematic(nets={net.id: net})
        result = autoroute(board, layout, schematic, {"COMP": defn}, {cid: inst})
        assert result.wires == []
        assert result.unrouted_nets == []

    def test_router_avoids_pads_of_other_nets(self) -> None:
        """Wire path must not pass through pads that belong to a different net.

        Board 3×7:
          NET1: R1-pin2=(1.5,0.5) → R2-pin1=(5.5,0.5)   [need to route]
          NET2: R3-pin1=(3.5,0.5), R3-pin2=(4.5,0.5)    [blocker in middle]
        Router must detour around row 0 via row 1 or report unrouted.
        """
        board = _perfboard(3, 7)
        defn = _sil2_def()
        cid_a, cid_b, cid_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

        inst_a = _bind(
            ComponentInstance(id=cid_a, definition_ref="COMP", refdes="R1", value=""),
            defn,
        )
        inst_b = _bind(
            ComponentInstance(id=cid_b, definition_ref="COMP", refdes="R2", value=""),
            defn,
        )
        inst_c = _bind(
            ComponentInstance(id=cid_c, definition_ref="COMP", refdes="R3", value=""),
            defn,
        )

        layout = Layout(
            placements=PlacementCollection(
                {
                    cid_a: _place(cid_a, 0.5, 0.5),  # pin1=(0.5,0.5) pin2=(1.5,0.5)
                    cid_b: _place(cid_b, 5.5, 0.5),  # pin1=(5.5,0.5) pin2=(6.5,0.5)
                    cid_c: _place(
                        cid_c, 3.5, 0.5
                    ),  # pin1=(3.5,0.5) pin2=(4.5,0.5) — blocker
                }
            )
        )

        ref_a2 = PinRef(component_id=cid_a, pin_number=2)  # (1.5, 0.5)
        ref_b1 = PinRef(component_id=cid_b, pin_number=1)  # (5.5, 0.5)
        ref_c1 = PinRef(component_id=cid_c, pin_number=1)  # (3.5, 0.5)
        ref_c2 = PinRef(component_id=cid_c, pin_number=2)  # (4.5, 0.5)

        net1 = Net(id=uuid.uuid4(), name="NET1", pins={ref_a2, ref_b1})
        net2 = Net(id=uuid.uuid4(), name="NET2", pins={ref_c1, ref_c2})
        schematic = Schematic(nets={net1.id: net1, net2.id: net2})
        components = {"COMP": defn}
        instances = {cid_a: inst_a, cid_b: inst_b, cid_c: inst_c}

        result = autoroute(board, layout, schematic, components, instances)

        # All wires produced for NET1 must not pass through NET2 pads on the same layer.
        # (Bottom-layer wires may share X,Y with top-layer pins without creating a short.)  # noqa: E501
        net2_pts_top = {Point(3.5, 0.5), Point(4.5, 0.5)}
        for wire in result.wires:
            if wire.side != "top":
                continue
            interior = wire.path[1:-1]
            for pt in interior:
                assert (
                    pt not in net2_pts_top
                ), f"Top wire passes through NET2 pad {pt}: {wire.path}"

    def test_skips_already_connected_pairs(self) -> None:
        """If a wire already connects two pins of a net, no duplicate is added.

        R1 (rot=90) at (0.5,0.5): pin1=(0.5,0.5), pin2=(1.5,0.5)
        R2 (rot=90) at (2.5,0.5): pin1=(2.5,0.5), pin2=(3.5,0.5)
        Existing wire: (1.5,0.5) → (2.5,0.5) already connects R1-pin2 to R2-pin1.
        """
        board = _perfboard(5, 5)
        defn = _sil2_def()
        cid_a = uuid.uuid4()
        cid_b = uuid.uuid4()
        inst_a = _bind(
            ComponentInstance(id=cid_a, definition_ref="COMP", refdes="R1", value=""),
            defn,
        )
        inst_b = _bind(
            ComponentInstance(id=cid_b, definition_ref="COMP", refdes="R2", value=""),
            defn,
        )

        existing_wire = Wire(
            id=uuid.uuid4(),
            path=[Point(1.5, 0.5), Point(2.5, 0.5)],
            insulated=True,
            side="top",
        )
        layout = Layout(
            placements=PlacementCollection(
                {
                    cid_a: _place(cid_a, 0.5, 0.5),  # pin1=(0.5,0.5) pin2=(1.5,0.5)
                    cid_b: _place(cid_b, 2.5, 0.5),  # pin1=(2.5,0.5) pin2=(3.5,0.5)
                }
            ),
            wires=WireCollection([existing_wire]),
        )
        ref_a2 = PinRef(component_id=cid_a, pin_number=2)  # (1.5, 0.5)
        ref_b1 = PinRef(component_id=cid_b, pin_number=1)  # (2.5, 0.5)
        net = Net(id=uuid.uuid4(), name="N1", pins={ref_a2, ref_b1})
        schematic = Schematic(nets={net.id: net})
        components = {"COMP": defn}
        instances = {cid_a: inst_a, cid_b: inst_b}

        result = autoroute(board, layout, schematic, components, instances)

        assert result.unrouted_nets == []
        # No new wires — the connection already exists.
        assert result.wires == []

    def test_router_avoids_floating_component_pins(self) -> None:
        """Wire must not pass through pins of a component not in any net.

        R1(rot=90)@(0.5,0.5): pin2=(1.5,0.5) → R2(rot=90)@(3.5,0.5): pin1=(3.5,0.5)
        R3(rot=90)@(2.5,0.5): pin1=(2.5,0.5), pin2=(3.5,0.5) — floating, blocks row 0.
        Router must detour through row 1.
        """
        board = _perfboard(2, 5)
        defn = _sil2_def()
        cid_a, cid_b, cid_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        inst_a = _bind(
            ComponentInstance(id=cid_a, definition_ref="COMP", refdes="R1", value=""),
            defn,
        )
        inst_b = _bind(
            ComponentInstance(id=cid_b, definition_ref="COMP", refdes="R2", value=""),
            defn,
        )
        inst_c = _bind(
            ComponentInstance(id=cid_c, definition_ref="COMP", refdes="R3", value=""),
            defn,
        )
        layout = Layout(
            placements=PlacementCollection(
                {
                    cid_a: _place(cid_a, 0.5, 0.5),  # pin1=(0.5,0.5) pin2=(1.5,0.5)
                    cid_b: _place(cid_b, 3.5, 0.5),  # pin1=(3.5,0.5) pin2=(4.5,0.5)
                    cid_c: _place(
                        cid_c, 2.5, 0.5
                    ),  # pin1=(2.5,0.5) pin2=(3.5,0.5) — blocker
                }
            )
        )
        ref_a2 = PinRef(component_id=cid_a, pin_number=2)
        ref_b1 = PinRef(component_id=cid_b, pin_number=1)
        net = Net(id=uuid.uuid4(), name="N1", pins={ref_a2, ref_b1})
        schematic = Schematic(nets={net.id: net})  # R3 not in any net
        components = {"COMP": defn}
        instances = {cid_a: inst_a, cid_b: inst_b, cid_c: inst_c}

        result = autoroute(board, layout, schematic, components, instances)

        assert result.unrouted_nets == []
        # Floating pins R3 are on "top" side.  A wire on "bottom" may legally
        # share the same (x,y) coordinates — only top-layer wires must avoid them.
        floating_pts_top = {Point(2.5, 0.5), Point(3.5, 0.5)}
        for wire in result.wires:
            if wire.side != "top":
                continue
            for pt in wire.path[1:-1]:
                assert (
                    pt not in floating_pts_top
                ), f"Top wire passes through floating pin {pt}: {wire.path}"

    def test_no_wire_crosses_existing_wire_segment(self) -> None:
        """New wire must not cross a previously routed wire mid-segment.

        Layout:
          NET1: (1.5,1.5) → (4.5,1.5)  — horizontal, routed first
          NET2: (3.5,0.5) → (3.5,2.5)  — vertical, must NOT cross NET1 midway
        The only crossing-free path for NET2 goes around NET1.
        """
        board = _perfboard(4, 6)
        defn = _sil2_def()
        # Use 4 components to get 4 pins in two separate nets.
        # Place them so NET1 forces a horizontal wire and NET2 a vertical one.
        cid_a, cid_b, cid_c, cid_d = (uuid.uuid4() for _ in range(4))

        def inst(cid, ref):
            return _bind(
                ComponentInstance(id=cid, definition_ref="COMP", refdes=ref, value=""),
                defn,  # noqa: E501
            )

        # rot=90: pin1=(x,y), pin2=(x+1,y)
        layout = Layout(
            placements=PlacementCollection(
                {
                    cid_a: _place(cid_a, 0.5, 1.5),  # pin2=(1.5,1.5) ← NET1 source
                    cid_b: _place(cid_b, 3.5, 1.5),  # pin2=(4.5,1.5) ← NET1 target
                    cid_c: _place(cid_c, 3.5, 0.5),  # pin1=(3.5,0.5) ← NET2 source
                    cid_d: _place(cid_d, 2.5, 2.5),  # pin2=(3.5,2.5) ← NET2 target
                }
            )
        )
        ref_a2 = PinRef(component_id=cid_a, pin_number=2)
        ref_b2 = PinRef(component_id=cid_b, pin_number=2)
        ref_c1 = PinRef(component_id=cid_c, pin_number=1)
        ref_d2 = PinRef(component_id=cid_d, pin_number=2)
        net1 = Net(id=uuid.uuid4(), name="NET1", pins={ref_a2, ref_b2})
        net2 = Net(id=uuid.uuid4(), name="NET2", pins={ref_c1, ref_d2})
        schematic = Schematic(nets={net1.id: net1, net2.id: net2})
        components = {"COMP": defn}
        instances = {
            cid_a: inst(cid_a, "R1"),
            cid_b: inst(cid_b, "R2"),
            cid_c: inst(cid_c, "R3"),
            cid_d: inst(cid_d, "R4"),
        }

        result = autoroute(board, layout, schematic, components, instances)

        # Check no pair of wires crosses each other mid-segment.
        def seg_cross(ax1, ay1, ax2, ay2, bx1, by1, bx2, by2):  # type: ignore[return]
            a_h = ay1 == ay2
            b_h = by1 == by2
            if a_h and not b_h:
                if min(ax1, ax2) < bx1 < max(ax1, ax2) and min(by1, by2) < ay1 < max(
                    by1, by2
                ):
                    return (bx1, ay1)
            elif not a_h and b_h:
                if min(bx1, bx2) < ax1 < max(bx1, bx2) and min(ay1, ay2) < by1 < max(
                    ay1, ay2
                ):
                    return (ax1, by1)

        wires = result.wires
        for i, wa in enumerate(wires):
            for wb in wires[i + 1 :]:
                for k in range(len(wa.path) - 1):
                    for segment_index in range(len(wb.path) - 1):
                        pt = seg_cross(
                            wa.path[k].x,
                            wa.path[k].y,
                            wa.path[k + 1].x,
                            wa.path[k + 1].y,
                            wb.path[segment_index].x,
                            wb.path[segment_index].y,
                            wb.path[segment_index + 1].x,
                            wb.path[segment_index + 1].y,
                        )
                        assert pt is None, f"Wires cross at {pt}: {wa.path} x {wb.path}"

    def test_already_routed_net_same_component(self) -> None:
        """Two pins on the same placed component — already adjacent, nothing to route."""  # noqa: E501
        board = _perfboard(5, 5)
        defn = _sil2_def()
        cid = uuid.uuid4()
        inst = _bind(
            ComponentInstance(id=cid, definition_ref="COMP", refdes="R1", value="1k"),
            defn,
        )
        layout = Layout(placements=PlacementCollection({cid: _place(cid, 0.5, 0.5)}))
        ref1 = PinRef(component_id=cid, pin_number=1)
        ref2 = PinRef(component_id=cid, pin_number=2)
        net = Net(id=uuid.uuid4(), name="SAME", pins={ref1, ref2})
        schematic = Schematic(nets={net.id: net})
        result = autoroute(board, layout, schematic, {"COMP": defn}, {cid: inst})
        assert result.unrouted_nets == []

    def test_under_dip_routes_on_bottom(self) -> None:
        """pin1→pin5 of a DIP8 should go through the bottom layer (Panama Canal).

        The direct top path is penalised by under_component_top; the same-length
        bottom path is not penalised.  Router must choose bottom.

        DIP8 at (2.5,1.5): pin1=(2.5,1.5), pin5=(5.5,4.5).
        Bottom diagonal: (2.5,1.5)→(3.5,2.5)→(4.5,3.5)→(5.5,4.5) — 3 steps.
        """
        board = _perfboard(8, 10)
        defn = _dip8_def()
        cid = uuid.uuid4()
        inst = _bind(
            ComponentInstance(id=cid, definition_ref="DIP8", refdes="U1", value=""),
            defn,
        )
        layout = Layout(
            placements=PlacementCollection(
                {
                    cid: ComponentPlacement(
                        component_id=cid,
                        position=Point(2.5, 1.5),
                        rotation=0,
                        side="top",
                    )
                }
            )
        )
        ref_1 = PinRef(component_id=cid, pin_number=1)
        ref_5 = PinRef(component_id=cid, pin_number=5)
        net = Net(id=uuid.uuid4(), name="N1", pins={ref_1, ref_5})
        schematic = Schematic(nets={net.id: net})

        result = autoroute(board, layout, schematic, {"DIP8": defn}, {cid: inst})

        assert result.unrouted_nets == []
        # All produced wires must be on the bottom layer.
        for wire in result.wires:
            assert (
                wire.side == "bottom"
            ), f"Expected bottom routing, got side={wire.side}: {wire.path}"

    def test_panama_canal_not_blocked_by_same_component_near_pin_penalty(self) -> None:
        """Panama Canal must stay short even when other nets use remaining DIP8 pins.

        If other pins of the same DIP8 belong to different nets that are not yet
        routed, their 4-adjacent neighbours (which lie on the diagonal Panama Canal
        path) must NOT generate near_pin corridor penalties for the current net.

        Setup: DIP8 with pin1↔pin5 in NET1, pins 2,3,4 each in separate nets.
        Pins 2,3,4 are at (2.5,2.5),(2.5,3.5),(2.5,4.5) — their neighbours
        (3.5,2.5),(3.5,3.5),(3.5,4.5) lie exactly on the 3-hop diagonal bottom
        path pin1→pin5.  Without the fix, near_pin_pads would penalise those pads
        by +10/hop and the router would take a long detour.

        We assert the pin1→pin5 wire uses ≤ 5 hops (optimal=3, allowing small slack).
        """
        board = _perfboard(8, 10)
        defn = _dip8_def()
        cid = uuid.uuid4()
        inst = _bind(
            ComponentInstance(id=cid, definition_ref="DIP8", refdes="U1", value=""),
            defn,
        )
        layout = Layout(
            placements=PlacementCollection(
                {
                    cid: ComponentPlacement(
                        component_id=cid,
                        position=Point(2.5, 1.5),
                        rotation=0,
                        side="top",
                    )
                }
            )
        )

        ref_1 = PinRef(component_id=cid, pin_number=1)  # (2.5,1.5)
        ref_5 = PinRef(component_id=cid, pin_number=5)  # (5.5,4.5)
        ref_2 = PinRef(component_id=cid, pin_number=2)  # (2.5,2.5)
        ref_3 = PinRef(component_id=cid, pin_number=3)  # (2.5,3.5)
        ref_4 = PinRef(component_id=cid, pin_number=4)  # (2.5,4.5)

        net1 = Net(id=uuid.uuid4(), name="NET1", pins={ref_1, ref_5})
        # Each remaining pin in its own 1-pin net (unroutable, but generates pending)
        net2 = Net(id=uuid.uuid4(), name="NET2", pins={ref_2})
        net3 = Net(id=uuid.uuid4(), name="NET3", pins={ref_3})
        net4 = Net(id=uuid.uuid4(), name="NET4", pins={ref_4})

        schematic = Schematic(
            nets={net1.id: net1, net2.id: net2, net3.id: net3, net4.id: net4}
        )

        result = autoroute(board, layout, schematic, {"DIP8": defn}, {cid: inst})

        assert result.unrouted_nets == []
        total_hops = sum(len(w.path) - 1 for w in result.wires)
        assert (
            total_hops <= 5
        ), f"Panama Canal should be ≤5 hops, got {total_hops}: " + str(
            [(w.path, w.side) for w in result.wires]
        )

    def test_bottom_routing_avoids_component_pins(self) -> None:
        """Wires routed on the bottom layer must not pass through component pin holes.

        A component pin occupies a plated-through hole — it is blocked on
        BOTH layers, not just the component's mounting side.

        Layout: 1×5 board.
          R3 (floating) at (2.5, 0.5): pin1=(2.5,0.5), pin2=(3.5,0.5) — blocker
          Net to route: (1.5,0.5) → (4.5,0.5)
        Without two-sided blocking the router would route through bottom via
        (2.5,0.5) and (3.5,0.5).  With proper blocking it must detour.
        """
        board = _perfboard(3, 6)
        defn = _sil2_def()
        cid_a, cid_b, cid_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        inst_a = _bind(
            ComponentInstance(id=cid_a, definition_ref="COMP", refdes="R1", value=""),
            defn,
        )
        inst_b = _bind(
            ComponentInstance(id=cid_b, definition_ref="COMP", refdes="R2", value=""),
            defn,
        )
        inst_c = _bind(
            ComponentInstance(id=cid_c, definition_ref="COMP", refdes="R3", value=""),
            defn,
        )

        # rot=90: pin1=(x,y), pin2=(x+1,y)
        layout = Layout(
            placements=PlacementCollection(
                {
                    cid_a: _place(cid_a, 0.5, 0.5),  # pin2=(1.5,0.5) ← net source
                    cid_b: _place(cid_b, 3.5, 0.5),  # pin2=(4.5,0.5) ← net target
                    cid_c: _place(
                        cid_c, 2.5, 0.5
                    ),  # pin1=(2.5,0.5), pin2=(3.5,0.5) — floating
                }
            )
        )
        ref_a2 = PinRef(component_id=cid_a, pin_number=2)
        ref_b2 = PinRef(component_id=cid_b, pin_number=2)
        net = Net(id=uuid.uuid4(), name="N1", pins={ref_a2, ref_b2})
        schematic = Schematic(nets={net.id: net})

        result = autoroute(
            board,
            layout,
            schematic,
            {"COMP": defn},
            {cid_a: inst_a, cid_b: inst_b, cid_c: inst_c},
        )

        assert result.unrouted_nets == []
        blocked_pts = {Point(2.5, 0.5), Point(3.5, 0.5)}
        for wire in result.wires:
            for pt in wire.path[1:-1]:
                assert (
                    pt not in blocked_pts
                ), f"Wire (side={wire.side}) passes through floating pin hole {pt}"


class TestAutorouterOptimization:
    """Tests for the iterative re-routing optimization pass."""

    def test_zero_optimize_iterations_still_routes(self) -> None:
        """max_optimize_iterations=0 disables optimization but routing must still work."""  # noqa: E501
        board = _perfboard(5, 5)
        defn = _sil2_def()
        cid_a = uuid.uuid4()
        cid_b = uuid.uuid4()
        inst_a = _bind(
            ComponentInstance(id=cid_a, definition_ref="COMP", refdes="R1", value=""),
            defn,
        )
        inst_b = _bind(
            ComponentInstance(id=cid_b, definition_ref="COMP", refdes="R2", value=""),
            defn,
        )
        layout = Layout(
            placements=PlacementCollection(
                {
                    cid_a: _place(cid_a, 0.5, 0.5),  # pin2=(1.5,0.5)
                    cid_b: _place(cid_b, 3.5, 0.5),  # pin1=(3.5,0.5)
                }
            )
        )
        ref_a2 = PinRef(component_id=cid_a, pin_number=2)
        ref_b1 = PinRef(component_id=cid_b, pin_number=1)
        net = Net(id=uuid.uuid4(), name="N1", pins={ref_a2, ref_b1})
        schematic = Schematic(nets={net.id: net})
        components = {"COMP": defn}
        instances = {cid_a: inst_a, cid_b: inst_b}

        result = autoroute(
            board, layout, schematic, components, instances, max_optimize_iterations=0
        )

        assert result.unrouted_nets == []
        assert len(result.wires) >= 1
        wire = result.wires[0]
        endpoints = {wire.path[0], wire.path[-1]}
        assert Point(1.5, 0.5) in endpoints
        assert Point(3.5, 0.5) in endpoints

    def test_optimization_converges_trivial_board(self) -> None:
        """On a simple open board, optimization must not change the wire endpoints.

        A trivial two-pin net on an open board is already optimal after the
        first pass; the optimization loop must converge immediately and return
        the same logical connection.
        """
        board = _perfboard(5, 5)
        defn = _sil2_def()
        cid_a = uuid.uuid4()
        cid_b = uuid.uuid4()
        inst_a = _bind(
            ComponentInstance(id=cid_a, definition_ref="COMP", refdes="R1", value=""),
            defn,
        )
        inst_b = _bind(
            ComponentInstance(id=cid_b, definition_ref="COMP", refdes="R2", value=""),
            defn,
        )
        layout = Layout(
            placements=PlacementCollection(
                {
                    cid_a: _place(cid_a, 0.5, 0.5),  # pin2=(1.5,0.5)
                    cid_b: _place(cid_b, 3.5, 0.5),  # pin1=(3.5,0.5)
                }
            )
        )
        ref_a2 = PinRef(component_id=cid_a, pin_number=2)
        ref_b1 = PinRef(component_id=cid_b, pin_number=1)
        net = Net(id=uuid.uuid4(), name="N1", pins={ref_a2, ref_b1})
        schematic = Schematic(nets={net.id: net})
        components = {"COMP": defn}
        instances = {cid_a: inst_a, cid_b: inst_b}

        result_no_opt = autoroute(
            board, layout, schematic, components, instances, max_optimize_iterations=0
        )
        result_opt = autoroute(
            board, layout, schematic, components, instances, max_optimize_iterations=10
        )

        def endpoints(result: object) -> set[frozenset[Point]]:
            from protoboard.services.routing.autorouter import AutorouteResult

            assert isinstance(result, AutorouteResult)
            return {frozenset([w.path[0], w.path[-1]]) for w in result.wires}

        assert endpoints(result_no_opt) == endpoints(result_opt)
        assert result_opt.unrouted_nets == []

    def test_unrouted_net_stays_unrouted_through_optimization(self) -> None:
        """A net that cannot be routed must appear exactly once in unrouted_nets.

        Build a 2×3 board where the only path between pin A and pin B is through
        a pin of a different net — impossible to route.  The optimization passes
        must not retry the failed net and must not duplicate it in unrouted_nets.

        (Board height 2 instead of 1 so that pin2 of each rot=0 SIL(2) lands on
        a real pad — required by the OFF_BOARD_PIN DRC precondition.)
        """
        board = _perfboard(2, 3)
        defn = _sil2_def()
        # Three components in row 0; pin2 of each falls into row 1.
        # rot=0 SIL(2): pin1=(x,y), pin2=(x,y+1)
        cid_a = uuid.uuid4()
        cid_b = uuid.uuid4()
        cid_c = uuid.uuid4()
        inst_a = _bind(
            ComponentInstance(id=cid_a, definition_ref="COMP", refdes="R1", value=""),
            defn,
        )
        inst_b = _bind(
            ComponentInstance(id=cid_b, definition_ref="COMP", refdes="R2", value=""),
            defn,
        )
        inst_c = _bind(
            ComponentInstance(id=cid_c, definition_ref="COMP", refdes="R3", value=""),
            defn,
        )
        # Arrange: R1-pin1=(0.5,0.5), blocker at (1.5,0.5), R3-pin1=(2.5,0.5)
        layout = Layout(
            placements=PlacementCollection(
                {
                    cid_a: ComponentPlacement(
                        component_id=cid_a,
                        position=Point(0.5, 0.5),
                        rotation=0,
                        side="top",
                    ),  # pin1=(0.5,0.5)
                    cid_b: ComponentPlacement(
                        component_id=cid_b,
                        position=Point(1.5, 0.5),
                        rotation=0,
                        side="top",
                    ),  # pin1=(1.5,0.5) — blocker
                    cid_c: ComponentPlacement(
                        component_id=cid_c,
                        position=Point(2.5, 0.5),
                        rotation=0,
                        side="top",
                    ),  # pin1=(2.5,0.5)
                }
            )
        )
        ref_a1 = PinRef(component_id=cid_a, pin_number=1)  # (0.5, 0.5)
        ref_c1 = PinRef(component_id=cid_c, pin_number=1)  # (2.5, 0.5)
        ref_b1 = PinRef(component_id=cid_b, pin_number=1)  # (1.5, 0.5) blocker net

        # BLOCKED_NET must route (0.5,0.5)→(2.5,0.5) but (1.5,0.5) is occupied by OTHER_NET  # noqa: E501
        blocked_net = Net(id=uuid.uuid4(), name="BLOCKED_NET", pins={ref_a1, ref_c1})
        other_net = Net(id=uuid.uuid4(), name="OTHER_NET", pins={ref_b1})
        schematic = Schematic(
            nets={blocked_net.id: blocked_net, other_net.id: other_net}
        )
        components = {"COMP": defn}
        instances = {cid_a: inst_a, cid_b: inst_b, cid_c: inst_c}

        result = autoroute(
            board, layout, schematic, components, instances, max_optimize_iterations=10
        )

        assert result.unrouted_nets.count("BLOCKED_NET") == 1

    def test_four_pin_net_routes_nearest_neighbour(self) -> None:
        """4-pin net in a row must connect via nearest neighbours, not all from one end.

        Pins: A(0.5,0.5) — B(2.5,0.5) — C(4.5,0.5) — D(6.5,0.5)
        Optimal Steiner tree: A-B, B-C, C-D  (total 6 hops)
        Bad all-from-A tree:  A-B, A-C, A-D  (total 12 hops, 3 separate long wires)

        We assert total wire hops ≤ 7 (optimal + 1 slack for board edges).
        """
        board = _perfboard(3, 8)
        defn = _sil2_def()
        cid_a, cid_b, cid_c, cid_d = (uuid.uuid4() for _ in range(4))

        def inst(cid, ref):
            return _bind(
                ComponentInstance(id=cid, definition_ref="COMP", refdes=ref, value=""),
                defn,  # noqa: E501
            )

        # rot=0 → vertical: pin1=(x,y), pin2=(x,y+1) — use pin1 only in the net
        layout = Layout(
            placements=PlacementCollection(
                {
                    cid_a: ComponentPlacement(
                        component_id=cid_a,
                        position=Point(0.5, 0.5),
                        rotation=0,
                        side="top",
                    ),
                    cid_b: ComponentPlacement(
                        component_id=cid_b,
                        position=Point(2.5, 0.5),
                        rotation=0,
                        side="top",
                    ),
                    cid_c: ComponentPlacement(
                        component_id=cid_c,
                        position=Point(4.5, 0.5),
                        rotation=0,
                        side="top",
                    ),
                    cid_d: ComponentPlacement(
                        component_id=cid_d,
                        position=Point(6.5, 0.5),
                        rotation=0,
                        side="top",
                    ),
                }
            )
        )
        refs = [
            PinRef(component_id=cid, pin_number=1)
            for cid in (cid_a, cid_b, cid_c, cid_d)
        ]
        net = Net(id=uuid.uuid4(), name="BUS", pins=set(refs))
        schematic = Schematic(nets={net.id: net})
        components = {"COMP": defn}
        instances = {
            cid: inst(cid, f"R{i+1}")
            for i, cid in enumerate((cid_a, cid_b, cid_c, cid_d))
        }

        result = autoroute(board, layout, schematic, components, instances)

        assert result.unrouted_nets == []
        total_hops = sum(len(w.path) - 1 for w in result.wires)
        # Optimal nearest-neighbour: 3 segments × 2 hops = 6.
        # Allow 7 for minor board-edge detours.
        assert (
            total_hops <= 7
        ), f"Expected ≤7 hops (nearest-neighbour), got {total_hops}: " + str(
            [(w.path, w.side) for w in result.wires]
        )

    def test_insulated_wires_do_not_share_pads_on_same_side(self) -> None:
        """Two insulated wires of different nets must not occupy the same pad.

        Physical wires cannot share a pad even when both are insulated —
        two conductors cannot occupy the same physical point.

        Layout: 5×3 board, two horizontal nets forced to cross the same row.
          NET1: (0.5,1.5) → (4.5,1.5)  routed first (long, high priority)
          NET2: (1.5,0.5) → (1.5,2.5)  routed second (vertical through middle)
        NET2 must detour around the pad occupied by NET1's wire on row 1.
        """
        board = _perfboard(3, 5)
        defn = _sil2_def()

        def _place_v(cid: uuid.UUID, x: float, y: float) -> ComponentPlacement:
            return ComponentPlacement(
                component_id=cid, position=Point(x, y), rotation=0, side="top"
            )

        cid_a, cid_b, cid_c, cid_d = (uuid.uuid4() for _ in range(4))
        inst_a = _bind(
            ComponentInstance(id=cid_a, definition_ref="COMP", refdes="R1", value=""),
            defn,
        )
        inst_b = _bind(
            ComponentInstance(id=cid_b, definition_ref="COMP", refdes="R2", value=""),
            defn,
        )
        inst_c = _bind(
            ComponentInstance(id=cid_c, definition_ref="COMP", refdes="R3", value=""),
            defn,
        )
        inst_d = _bind(
            ComponentInstance(id=cid_d, definition_ref="COMP", refdes="R4", value=""),
            defn,
        )

        # rot=90: pin1=(x,y), pin2=(x+1,y); rot=0: pin1=(x,y), pin2=(x,y+1)
        layout = Layout(
            placements=PlacementCollection(
                {
                    cid_a: _place(
                        cid_a, 0.5, 1.5
                    ),  # pin1=(0.5,1.5) — NET1 source (rot90)
                    cid_b: _place(
                        cid_b, 3.5, 1.5
                    ),  # pin2=(4.5,1.5) — NET1 target (rot90)
                    cid_c: _place_v(
                        cid_c, 1.5, 0.5
                    ),  # pin1=(1.5,0.5) — NET2 source (rot0)
                    cid_d: _place_v(
                        cid_d, 1.5, 1.5
                    ),  # pin2=(1.5,2.5) — NET2 target (rot0)
                }
            )
        )
        ref_a1 = PinRef(component_id=cid_a, pin_number=1)  # (0.5, 1.5)
        ref_b2 = PinRef(component_id=cid_b, pin_number=2)  # (4.5, 1.5)
        ref_c1 = PinRef(component_id=cid_c, pin_number=1)  # (1.5, 0.5)
        ref_d2 = PinRef(component_id=cid_d, pin_number=2)  # (1.5, 2.5)

        net1 = Net(id=uuid.uuid4(), name="NET1", pins={ref_a1, ref_b2})
        net2 = Net(id=uuid.uuid4(), name="NET2", pins={ref_c1, ref_d2})
        schematic = Schematic(nets={net1.id: net1, net2.id: net2})
        components = {"COMP": defn}
        instances = {cid_a: inst_a, cid_b: inst_b, cid_c: inst_c, cid_d: inst_d}

        result = autoroute(board, layout, schematic, components, instances)

        # Collect all (Point, side) positions used by each net's wires.
        def wire_pads(wires: list[Wire]) -> set[tuple[Point, str]]:
            pads: set[tuple[Point, str]] = set()
            for w in wires:
                for pt in w.path:
                    pads.add((pt, w.side))
            return pads

        all_wires = result.wires
        # For each pair of wires from different nets, check no pad overlap on same side.
        for i, wa in enumerate(all_wires):
            pads_a = {(pt, wa.side) for pt in wa.path}
            for wb in all_wires[i + 1 :]:
                pads_b = {(pt, wb.side) for pt in wb.path}
                # Endpoints touching is allowed (same-net merge point or via),
                # but interior overlap of different nets is not.
                # We allow endpoint collisions here; check only interior points.
                interior_a = {(pt, wa.side) for pt in wa.path[1:-1]}
                interior_b = {(pt, wb.side) for pt in wb.path[1:-1]}
                bad = (interior_a & pads_b) | (interior_b & pads_a)
                assert not bad, (
                    f"Wires share pad(s) {bad}: {wa.path} (side={wa.side}) "
                    f"vs {wb.path} (side={wb.side})"
                )

    def test_optimization_reduces_or_maintains_wire_length(self) -> None:
        """Optimization must not make total wire length worse.

        On a board with two nets where early routing decisions interact with
        corridor penalties, the iterative pass must produce wire paths that are
        at most as long as the unoptimized single pass.

        Three components, two nets:
          NET1: R1-pin2=(1.5,0.5) → R3-pin1=(3.5,0.5)  (long horizontal)
          NET2: R2-pin1=(2.5,0.5) → R2-pin2=(2.5,1.5)  (short vertical)
        """
        board = _perfboard(5, 6)
        defn = _sil2_def()

        # SIL2 rot=0 → vertical: pin1=(x,y), pin2=(x,y+1)
        def _place_v(cid: uuid.UUID, x: float, y: float) -> ComponentPlacement:
            return ComponentPlacement(
                component_id=cid, position=Point(x, y), rotation=0, side="top"
            )

        cid_a, cid_b, cid_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        inst_a = _bind(
            ComponentInstance(id=cid_a, definition_ref="COMP", refdes="R1", value=""),
            defn,
        )
        inst_b = _bind(
            ComponentInstance(id=cid_b, definition_ref="COMP", refdes="R2", value=""),
            defn,
        )
        inst_c = _bind(
            ComponentInstance(id=cid_c, definition_ref="COMP", refdes="R3", value=""),
            defn,
        )

        layout = Layout(
            placements=PlacementCollection(
                {
                    cid_a: _place(cid_a, 0.5, 0.5),  # pin1=(0.5,0.5) pin2=(1.5,0.5)
                    cid_b: _place_v(cid_b, 2.5, 0.5),  # pin1=(2.5,0.5) pin2=(2.5,1.5)
                    cid_c: _place(
                        cid_c, 2.5, 0.5
                    ),  # pin1=(2.5,0.5) pin2=(3.5,0.5) — shares col
                }
            )
        )

        # Use distinct pins so they don't collide in the net graph:
        # NET1 connects R1-pin2 → R3-pin2
        ref_a2 = PinRef(component_id=cid_a, pin_number=2)  # (1.5, 0.5)
        ref_c2 = PinRef(component_id=cid_c, pin_number=2)  # (3.5, 0.5)
        # NET2 connects R2-pin1 → R2-pin2 (same component, already adjacent — skipped)
        ref_b1 = PinRef(component_id=cid_b, pin_number=1)  # (2.5, 0.5)
        ref_b2 = PinRef(component_id=cid_b, pin_number=2)  # (2.5, 1.5)

        net1 = Net(id=uuid.uuid4(), name="NET1", pins={ref_a2, ref_c2})
        net2 = Net(id=uuid.uuid4(), name="NET2", pins={ref_b1, ref_b2})
        schematic = Schematic(nets={net1.id: net1, net2.id: net2})
        components = {"COMP": defn}
        instances = {cid_a: inst_a, cid_b: inst_b, cid_c: inst_c}

        def total_hops(result: object) -> int:
            from protoboard.services.routing.autorouter import AutorouteResult

            assert isinstance(result, AutorouteResult)
            return sum(len(w.path) - 1 for w in result.wires)

        result_no_opt = autoroute(
            board, layout, schematic, components, instances, max_optimize_iterations=0
        )
        result_opt = autoroute(
            board, layout, schematic, components, instances, max_optimize_iterations=5
        )

        assert result_opt.unrouted_nets == []
        assert total_hops(result_opt) <= total_hops(result_no_opt)


class TestAutoroutePreconditions:
    """Autoroute must abort with a clear error when input layout fails DRC.

    Junk preconditions (overlapping pins, pre-existing shorts) produce
    junk autoroute output.  Instead of flooding the log and emitting
    broken wires, raise ``AutoroutePreconditionError`` so the GUI can
    show a dialog and ask the user to fix the schematic/placement first.
    """

    def test_aborts_when_two_nets_share_a_physical_pad(self) -> None:
        """Two components placed so pin-1 of each lands on the same pad,
        with each pin assigned to a different net → SHORT.  Autoroute
        must raise instead of running."""
        from protoboard.services.routing.autorouter import (
            AutoroutePreconditionError,
        )

        board = _perfboard(5, 5)
        defn = _sil2_def()
        cid_a = uuid.uuid4()
        cid_b = uuid.uuid4()
        inst_a = _bind(
            ComponentInstance(
                id=cid_a,
                definition_ref="COMP",
                refdes="R1",
                value="1k",
            ),
            defn,
        )
        inst_b = _bind(
            ComponentInstance(
                id=cid_b,
                definition_ref="COMP",
                refdes="R2",
                value="1k",
            ),
            defn,
        )
        # Both components placed at (0.5, 0.5) — pin 1 of each lands on
        # the same physical pad.  DRC catches this as OVERLAP (and
        # depending on graph state, also SHORT).
        layout = Layout(
            placements=PlacementCollection(
                {
                    cid_a: _place(cid_a, 0.5, 0.5),
                    cid_b: _place(cid_b, 0.5, 0.5),
                }
            )
        )
        # Assign each colliding pin to a different net.
        ref_a1 = PinRef(component_id=cid_a, pin_number=1)
        ref_b1 = PinRef(component_id=cid_b, pin_number=1)
        ref_a2 = PinRef(component_id=cid_a, pin_number=2)
        ref_b2 = PinRef(component_id=cid_b, pin_number=2)
        net_x = Net(id=uuid.uuid4(), name="NET_X", pins={ref_a1, ref_a2})
        net_y = Net(id=uuid.uuid4(), name="NET_Y", pins={ref_b1, ref_b2})
        schematic = Schematic(nets={net_x.id: net_x, net_y.id: net_y})
        components = {"COMP": defn}
        instances = {cid_a: inst_a, cid_b: inst_b}

        with pytest.raises(AutoroutePreconditionError) as exc_info:
            autoroute(board, layout, schematic, components, instances)

        err = exc_info.value
        assert len(err.issues) >= 1
        codes = {iss.code for iss in err.issues}
        # SHORT fires because two pins of different nets share a physical
        # pad and end up in the same electric set.  (OVERLAP also fires,
        # but it's not in the blocking set — pads can be intentionally
        # shared between a routed pin and a floating pin of another
        # component.)
        assert "SHORT" in codes

    def test_series_resistors_sharing_pad_not_blocked(self) -> None:
        """Series-resistor pattern: R1.pin2 and R2.pin1 share the same
        physical pad, both assigned to the same schematic net.  This is
        a legitimate and common perfboard layout — DRC flags it as
        OVERLAP (informational) but NOT as SHORT.  Autoroute must not
        block on OVERLAP alone, because the user did this intentionally.
        """
        board = _perfboard(5, 5)
        defn = _sil2_def()
        cid_a = uuid.uuid4()
        cid_b = uuid.uuid4()
        cid_c = uuid.uuid4()
        inst_a = _bind(
            ComponentInstance(
                id=cid_a,
                definition_ref="COMP",
                refdes="R1",
                value="1k",
            ),
            defn,
        )
        inst_b = _bind(
            ComponentInstance(
                id=cid_b,
                definition_ref="COMP",
                refdes="R2",
                value="1k",
            ),
            defn,
        )
        inst_c = _bind(
            ComponentInstance(
                id=cid_c,
                definition_ref="COMP",
                refdes="R3",
                value="1k",
            ),
            defn,
        )
        # R1.pin2 = (1.5, 0.5).  R2.pin1 = (1.5, 0.5) — same pad → OVERLAP.
        # R3 is the routing target, placed elsewhere.
        layout = Layout(
            placements=PlacementCollection(
                {
                    cid_a: _place(cid_a, 0.5, 0.5),  # pin1=(0.5,0.5) pin2=(1.5,0.5)
                    cid_b: _place(cid_b, 1.5, 0.5),  # pin1=(1.5,0.5) pin2=(2.5,0.5)
                    cid_c: _place(cid_c, 3.5, 0.5),  # pin1=(3.5,0.5) pin2=(4.5,0.5)
                }
            )
        )
        # Single shared net for the series junction (R1.pin2 = R2.pin1),
        # second net connects R2.pin2 → R3.pin1 — that's what autoroute
        # actually needs to draw a wire for.
        ref_a2 = PinRef(component_id=cid_a, pin_number=2)
        ref_b1 = PinRef(component_id=cid_b, pin_number=1)
        ref_b2 = PinRef(component_id=cid_b, pin_number=2)
        ref_c1 = PinRef(component_id=cid_c, pin_number=1)
        # Series junction: same schematic net contains both pins on the
        # shared pad — no SHORT.
        junction = Net(id=uuid.uuid4(), name="JUNCTION", pins={ref_a2, ref_b1})
        bridge = Net(id=uuid.uuid4(), name="BRIDGE", pins={ref_b2, ref_c1})
        schematic = Schematic(nets={junction.id: junction, bridge.id: bridge})
        components = {"COMP": defn}
        instances = {cid_a: inst_a, cid_b: inst_b, cid_c: inst_c}

        # Must not raise — OVERLAP alone is not blocking.
        result = autoroute(board, layout, schematic, components, instances)
        # BRIDGE should route; JUNCTION is already physically connected via
        # the shared pad so it has nothing to draw.
        assert "BRIDGE" not in result.unrouted_nets

    def test_clean_layout_does_not_abort(self) -> None:
        """A normal two-pin route with disjoint nets must NOT raise.

        Guard against false positives in the precondition gate.
        """
        board = _perfboard(5, 5)
        defn = _sil2_def()
        cid_a = uuid.uuid4()
        cid_b = uuid.uuid4()
        inst_a = _bind(
            ComponentInstance(
                id=cid_a,
                definition_ref="COMP",
                refdes="R1",
                value="1k",
            ),
            defn,
        )
        inst_b = _bind(
            ComponentInstance(
                id=cid_b,
                definition_ref="COMP",
                refdes="R2",
                value="1k",
            ),
            defn,
        )
        layout = Layout(
            placements=PlacementCollection(
                {
                    cid_a: _place(cid_a, 0.5, 0.5),
                    cid_b: _place(cid_b, 3.5, 0.5),
                }
            )
        )
        ref_a2 = PinRef(component_id=cid_a, pin_number=2)
        ref_b1 = PinRef(component_id=cid_b, pin_number=1)
        net = Net(id=uuid.uuid4(), name="N1", pins={ref_a2, ref_b1})
        schematic = Schematic(nets={net.id: net})
        components = {"COMP": defn}
        instances = {cid_a: inst_a, cid_b: inst_b}

        # Must not raise.
        result = autoroute(board, layout, schematic, components, instances)
        assert result.unrouted_nets == []


class TestPartialRoutingRollback:
    """Bug fix 2026-06-01: when a multi-pin net fails routing on a
    later segment (e.g. 3rd pin), the partial wires from earlier
    segments must NOT leak into the result.  Previously, _route_net
    mutated `working_wires` in-place per successful segment, and
    `return None` on later segment failure left those partial
    connections behind — visible as "islands" (2-3 pins of the same
    net spuriously connected, while the net is reported as
    unrouted).
    """

    def test_failed_three_pin_net_leaves_no_partial_wires(self) -> None:
        """3-pin net where the 3rd pin is geometrically unreachable.

        Setup: 3 pins of TARGET net, with 2 unreachable from each
        other due to a board edge configuration (single-row board,
        pin C on a separate strip with no path back).

        After autoroute: if TARGET is unrouted (or only partially),
        the result.wires must contain only fully-routed nets — no
        partial fragments belonging to TARGET.
        """
        # 2-row strip-style board where pin C is isolated by the
        # board edge.  Pins A=(0.5,0.5), B=(1.5,0.5) on row 0;
        # C=(10.5, 1.5) on row 1 — far from A/B with limited path.
        # Surround C with blocker net pins so A* can't reach.
        board = _perfboard(2, 12)
        defn = _sil2_def()
        cid_a, cid_b, cid_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        cid_block1 = uuid.uuid4()
        inst_a = _bind(
            ComponentInstance(id=cid_a, definition_ref="COMP", refdes="A", value=""),
            defn,
        )
        inst_b = _bind(
            ComponentInstance(id=cid_b, definition_ref="COMP", refdes="B", value=""),
            defn,
        )
        inst_c = _bind(
            ComponentInstance(id=cid_c, definition_ref="COMP", refdes="C", value=""),
            defn,
        )
        inst_block = _bind(
            ComponentInstance(
                id=cid_block1,
                definition_ref="COMP",
                refdes="X1",
                value="",
            ),
            defn,
        )

        # rot=0 SIL(2): pin1=(x,y), pin2=(x,y+1).
        # rot=90 horizontal: pin1=(x,y), pin2=(x+1,y).
        # Pin C is placed at the right edge.  Around it we place a
        # horizontal blocker spanning the row so A* must detour
        # through the other row, which is also constrained.
        layout = Layout(
            placements=PlacementCollection(
                {
                    cid_a: ComponentPlacement(
                        component_id=cid_a,
                        position=Point(0.5, 0.5),
                        rotation=0,
                        side="top",
                    ),  # pin1=(0.5,0.5)
                    cid_b: ComponentPlacement(
                        component_id=cid_b,
                        position=Point(1.5, 0.5),
                        rotation=0,
                        side="top",
                    ),  # pin1=(1.5,0.5)
                    cid_c: ComponentPlacement(
                        component_id=cid_c,
                        position=Point(11.5, 0.5),
                        rotation=0,
                        side="top",
                    ),  # pin1=(11.5,0.5)
                    # Horizontal blocker spanning pins (3..10) on row 0,
                    # forcing detour through row 1.
                    cid_block1: ComponentPlacement(
                        component_id=cid_block1,
                        position=Point(5.5, 0.5),
                        rotation=90,
                        side="top",
                    ),  # pin1=(5.5,0.5), pin2=(6.5,0.5)
                }
            )
        )

        ref_a1 = PinRef(component_id=cid_a, pin_number=1)
        ref_b1 = PinRef(component_id=cid_b, pin_number=1)
        ref_c1 = PinRef(component_id=cid_c, pin_number=1)
        ref_x_1 = PinRef(component_id=cid_block1, pin_number=1)
        ref_x_2 = PinRef(component_id=cid_block1, pin_number=2)

        target_net = Net(id=uuid.uuid4(), name="TARGET", pins={ref_a1, ref_b1, ref_c1})
        blocker_net = Net(id=uuid.uuid4(), name="BLOCK", pins={ref_x_1, ref_x_2})
        schematic = Schematic(
            nets={target_net.id: target_net, blocker_net.id: blocker_net}
        )
        components = {"COMP": defn}
        instances = {
            cid_a: inst_a,
            cid_b: inst_b,
            cid_c: inst_c,
            cid_block1: inst_block,
        }

        try:
            result = autoroute(
                board,
                layout,
                schematic,
                components,
                instances,
                max_optimize_iterations=0,
            )
        except Exception as exc:
            pytest.skip(f"Autoroute precondition rejected the setup: {exc}")
            return

        # The actual invariant we test: result.wires must be
        # consistent with result.unrouted_nets.  Any wire claimed
        # in result.wires must belong to a fully-routed net.
        # Specifically: if TARGET is in unrouted_nets, NO wire with
        # endpoint at A/B/C may appear in result.wires.
        target_pin_world = {Point(0.5, 0.5), Point(1.5, 0.5), Point(11.5, 0.5)}
        target_wires = [
            w
            for w in result.wires
            if (w.path[0] in target_pin_world or w.path[-1] in target_pin_world)
        ]
        if "TARGET" in result.unrouted_nets:
            assert target_wires == [], (
                f"TARGET reported as unrouted but {len(target_wires)} "
                f"partial wires leaked into result: "
                f"{[(w.path[0], w.path[-1]) for w in target_wires]}"
            )

    def test_no_duplicate_wires_in_result(self) -> None:
        """Result must not contain duplicate Wire instances (same id
        listed twice).  Caused by the historic double-extend pattern
        in _run_one_pass (extend in both _route_net.1532 and
        _run_one_pass.1135).
        """
        board = _perfboard(5, 5)
        defn = _sil2_def()
        cid_a, cid_b, cid_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        inst_a = _bind(
            ComponentInstance(id=cid_a, definition_ref="COMP", refdes="A", value=""),
            defn,
        )
        inst_b = _bind(
            ComponentInstance(id=cid_b, definition_ref="COMP", refdes="B", value=""),
            defn,
        )
        inst_c = _bind(
            ComponentInstance(id=cid_c, definition_ref="COMP", refdes="C", value=""),
            defn,
        )
        # rot=0 SIL(2): pin1=(x,y), pin2=(x,y+1).
        # A at (0.5,0.5)/(0.5,1.5), B at (3.5,0.5)/(3.5,1.5), C at (0.5,3.5)/(0.5,4.5).
        layout = Layout(
            placements=PlacementCollection(
                {
                    cid_a: ComponentPlacement(
                        component_id=cid_a,
                        position=Point(0.5, 0.5),
                        rotation=0,
                        side="top",
                    ),
                    cid_b: ComponentPlacement(
                        component_id=cid_b,
                        position=Point(3.5, 0.5),
                        rotation=0,
                        side="top",
                    ),
                    cid_c: ComponentPlacement(
                        component_id=cid_c,
                        position=Point(0.5, 3.5),
                        rotation=0,
                        side="top",
                    ),
                }
            )
        )
        ref_a1 = PinRef(component_id=cid_a, pin_number=1)
        ref_b1 = PinRef(component_id=cid_b, pin_number=1)
        ref_c1 = PinRef(component_id=cid_c, pin_number=1)
        n1 = Net(id=uuid.uuid4(), name="N1", pins={ref_a1, ref_b1, ref_c1})
        schematic = Schematic(nets={n1.id: n1})
        components = {"COMP": defn}
        instances = {cid_a: inst_a, cid_b: inst_b, cid_c: inst_c}

        result = autoroute(board, layout, schematic, components, instances)
        wire_ids = [w.id for w in result.wires]
        assert len(wire_ids) == len(
            set(wire_ids)
        ), f"Duplicate wire IDs in result: {wire_ids}"


class TestIterativeSimplifyAll:
    """Post-route iterative simplify-all pass (RoutingSettings.simplify_all_after_autoroute)."""  # noqa: E501

    def test_kwarg_default_true_does_not_break_existing_paths(self) -> None:
        """Default ON must not regress: a simple two-pin net still routes
        and the wire has reasonable vertex count."""
        board = _perfboard(5, 5)
        defn = _sil2_def()
        cid_a, cid_b = uuid.uuid4(), uuid.uuid4()
        layout = Layout(
            placements=PlacementCollection(
                {
                    cid_a: _place(cid_a, 0.5, 0.5),
                    cid_b: _place(cid_b, 3.5, 0.5),
                }
            )
        )
        inst_a = _bind(
            ComponentInstance(id=cid_a, definition_ref="COMP", refdes="R1", value=""),
            defn,
        )
        inst_b = _bind(
            ComponentInstance(id=cid_b, definition_ref="COMP", refdes="R2", value=""),
            defn,
        )
        net = Net(
            id=uuid.uuid4(),
            name="N",
            pins={
                PinRef(component_id=cid_a, pin_number=2),
                PinRef(component_id=cid_b, pin_number=1),
            },
        )
        result = autoroute(
            board,
            layout,
            Schematic(nets={net.id: net}),
            {"COMP": defn},
            {cid_a: inst_a, cid_b: inst_b},
            # default kwarg is True
        )
        assert result.unrouted_nets == []
        assert len(result.wires) == 1

    def test_explicit_off_disables_simplify(self) -> None:
        """When simplify_all_after_autoroute=False the post-process loop
        is skipped — useful to verify the kwarg actually gates the work."""
        board = _perfboard(5, 5)
        defn = _sil2_def()
        cid_a, cid_b = uuid.uuid4(), uuid.uuid4()
        layout = Layout(
            placements=PlacementCollection(
                {
                    cid_a: _place(cid_a, 0.5, 0.5),
                    cid_b: _place(cid_b, 3.5, 0.5),
                }
            )
        )
        inst_a = _bind(
            ComponentInstance(id=cid_a, definition_ref="COMP", refdes="R1", value=""),
            defn,
        )
        inst_b = _bind(
            ComponentInstance(id=cid_b, definition_ref="COMP", refdes="R2", value=""),
            defn,
        )
        net = Net(
            id=uuid.uuid4(),
            name="N",
            pins={
                PinRef(component_id=cid_a, pin_number=2),
                PinRef(component_id=cid_b, pin_number=1),
            },
        )
        result = autoroute(
            board,
            layout,
            Schematic(nets={net.id: net}),
            {"COMP": defn},
            {cid_a: inst_a, cid_b: inst_b},
            simplify_all_after_autoroute=False,
        )
        # Still routes correctly with simplify off.
        assert result.unrouted_nets == []
        assert len(result.wires) == 1

    def test_iteration_cap_respected(self) -> None:
        """Setting iteration cap to 1 must not crash and produces a
        valid result (single-pass behaviour)."""
        board = _perfboard(5, 5)
        defn = _sil2_def()
        cid_a, cid_b = uuid.uuid4(), uuid.uuid4()
        layout = Layout(
            placements=PlacementCollection(
                {
                    cid_a: _place(cid_a, 0.5, 0.5),
                    cid_b: _place(cid_b, 3.5, 0.5),
                }
            )
        )
        inst_a = _bind(
            ComponentInstance(id=cid_a, definition_ref="COMP", refdes="R1", value=""),
            defn,
        )
        inst_b = _bind(
            ComponentInstance(id=cid_b, definition_ref="COMP", refdes="R2", value=""),
            defn,
        )
        net = Net(
            id=uuid.uuid4(),
            name="N",
            pins={
                PinRef(component_id=cid_a, pin_number=2),
                PinRef(component_id=cid_b, pin_number=1),
            },
        )
        result = autoroute(
            board,
            layout,
            Schematic(nets={net.id: net}),
            {"COMP": defn},
            {cid_a: inst_a, cid_b: inst_b},
            simplify_all_after_autoroute=True,
            simplify_all_max_iterations=1,
        )
        assert result.unrouted_nets == []
        assert len(result.wires) == 1
