# SPDX-License-Identifier: MIT
"""End-to-end tests for wire_mode='bare_diagonal' in the autorouter."""

from __future__ import annotations

import uuid

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SingleRow
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.routing_settings import RoutingSettings
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.services.routing.autorouter import autoroute


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


def _sil2() -> ComponentDefinition:
    return ComponentDefinition(
        name="COMP", footprint=SingleRow(n=2), pin_names={1: "A", 2: "B"}
    )


def _place(cid: uuid.UUID, x: float, y: float, rotation: int = 90) -> ComponentPlacement:
    return ComponentPlacement(
        component_id=cid, position=Point(x, y), rotation=rotation, side="top"
    )


def _all_bare(wires) -> bool:
    return all(not w.insulated for w in wires)


def _routed_path_kinds(wires) -> set[str]:
    """Return the set of step kinds present across all wires: ortho/diag_pos/diag_neg."""
    kinds: set[str] = set()
    for w in wires:
        for a, b in zip(w.path, w.path[1:]):
            dx, dy = b.x - a.x, b.y - a.y
            if dx == 0.0 or dy == 0.0:
                kinds.add("ortho")
            elif dx * dy > 0.0:
                kinds.add("diag_pos")
            else:
                kinds.add("diag_neg")
    return kinds


class TestBareDiagonalMode:
    def test_simple_diagonal_route(self) -> None:
        """Two SIL2 components placed diagonally — bare_diagonal mode should connect them
        without insulated wires, using diagonal A* steps."""
        board = _perfboard(5, 5)
        defn = _sil2()
        cid_a, cid_b = uuid.uuid4(), uuid.uuid4()
        inst_a = ComponentInstance(id=cid_a, definition_ref="COMP", refdes="R1", value="")
        inst_b = ComponentInstance(id=cid_b, definition_ref="COMP", refdes="R2", value="")
        # R1 horizontal at (0.5,0.5): pin2 = (1.5, 0.5)  → parity (1+0)=1 → "\"
        # R2 horizontal at (3.5,3.5): pin1 = (3.5, 3.5)  → parity (3+3)=0 → "/"
        layout = Layout(placements={
            cid_a: _place(cid_a, 0.5, 0.5),
            cid_b: _place(cid_b, 3.5, 3.5),
        })
        ref_a2 = PinRef(component_id=cid_a, pin_number=2)
        ref_b1 = PinRef(component_id=cid_b, pin_number=1)
        net = Net(id=uuid.uuid4(), name="N1", pins={ref_a2, ref_b1})
        schematic = Schematic(nets={net.id: net})

        rs = RoutingSettings(wire_mode="bare_diagonal")
        result = autoroute(
            board, layout, schematic, {"COMP": defn},
            {cid_a: inst_a, cid_b: inst_b},
            cost_model=rs.cost_model(),
            wire_mode=rs.wire_mode,
        )

        assert result.unrouted_nets == []
        assert len(result.wires) >= 1
        assert _all_bare(result.wires), "bare_diagonal must not produce insulated wires"

    def test_two_perpendicular_nets_can_share_mesh_cell(self) -> None:
        """Two nets routed in bare_diagonal mode — one even-parity, one odd-parity.
        The router should be able to lay both as bare diagonals that may cross
        at a mesh-cell centre (cost_model.crossing_bare_bare_diagonal applies)."""
        board = _perfboard(6, 6)
        defn = _sil2()
        ids = [uuid.uuid4() for _ in range(4)]
        # NET1: even-parity diagonal A→B from (0.5,0.5) to (3.5,3.5).
        # NET2: odd-parity diagonal C→D from (0.5,3.5) to (3.5,0.5).
        # These naturally want to cross at (2.0, 2.0).
        insts = {
            ids[0]: ComponentInstance(id=ids[0], definition_ref="COMP", refdes="R1", value=""),
            ids[1]: ComponentInstance(id=ids[1], definition_ref="COMP", refdes="R2", value=""),
            ids[2]: ComponentInstance(id=ids[2], definition_ref="COMP", refdes="R3", value=""),
            ids[3]: ComponentInstance(id=ids[3], definition_ref="COMP", refdes="R4", value=""),
        }
        # Place so that pin1 sits exactly at the desired endpoints.
        layout = Layout(placements={
            ids[0]: _place(ids[0], 0.5, 0.5),  # pin1=(0.5,0.5)
            ids[1]: _place(ids[1], 3.5, 3.5),  # pin1=(3.5,3.5)
            ids[2]: _place(ids[2], 0.5, 3.5),  # pin1=(0.5,3.5)
            ids[3]: _place(ids[3], 3.5, 0.5),  # pin1=(3.5,0.5)
        })
        net1 = Net(
            id=uuid.uuid4(), name="N1",
            pins={PinRef(component_id=ids[0], pin_number=1),
                  PinRef(component_id=ids[1], pin_number=1)},
        )
        net2 = Net(
            id=uuid.uuid4(), name="N2",
            pins={PinRef(component_id=ids[2], pin_number=1),
                  PinRef(component_id=ids[3], pin_number=1)},
        )
        schematic = Schematic(nets={net1.id: net1, net2.id: net2})

        rs = RoutingSettings(wire_mode="bare_diagonal")
        result = autoroute(
            board, layout, schematic, {"COMP": defn}, insts,
            cost_model=rs.cost_model(),
            wire_mode=rs.wire_mode,
        )

        assert result.unrouted_nets == [], (
            f"bare_diagonal failed to route both crossing nets; "
            f"unrouted={result.unrouted_nets}"
        )
        assert _all_bare(result.wires), "bare_diagonal must produce only bare wires"
        kinds = _routed_path_kinds(result.wires)
        # At least one diagonal step should appear — straight ortho between
        # the diagonal endpoints would defeat the whole point.
        assert kinds & {"diag_pos", "diag_neg"}, (
            f"expected diagonal steps, got {kinds}"
        )
