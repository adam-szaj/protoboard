# SPDX-License-Identifier: MIT
"""Regression: ``_update_wires_for_move`` must not drag foreign wires.

User-reported 2026-05-31: moving a component caused unrelated wires
whose path happened to pass through the *old* pin position to snap to
the new pin position, "sticking" themselves to the dragged component.

Root cause: ``_update_wires_for_move`` translated every wire vertex
through a global pin_map keyed by old pin positions, without checking
whether the wire was actually anchored to the moved component's pin
(via a PinKnot endpoint ref). The fix gates per-wire on
``_wire_anchored_at_comp_pin``: only wires with a strong (PinKnot ref)
or weak (no knot refs at all + endpoint coincidence) anchor get
translated.
"""

from __future__ import annotations

import uuid

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.commands.component_cmds import (
    _update_wires_for_move,
)
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.knot import PinKnot
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import KnotRef, Wire
from protoboard.domain.schematic.net import PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point


def _perfboard(rows: int = 6, cols: int = 6) -> Board:
    return Board(root=BoardGrid(
        width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=rows, cols=cols, row_gap=0.0, col_gap=0.0,
    ))


class _State:
    def __init__(self) -> None:
        self.board = _perfboard()
        self.layout = Layout()
        self.layout.ensure_root_group()
        self.instances: dict[uuid.UUID, ComponentInstance] = {}
        self.components: dict[str, ComponentDefinition] = {}
        self.schematic = Schematic(nets={})


def _place(
    state: _State, refdes: str, pos: Point,
) -> tuple[ComponentInstance, ComponentPlacement]:
    defn_name = f"_J_{refdes}"
    defn = ComponentDefinition(name=defn_name, footprint=SIL(1), pin_names={1: "P"})
    state.components[defn_name] = defn
    inst = ComponentInstance.new(definition_ref=defn_name, refdes=refdes, value="")
    state.instances[inst.id] = inst
    pl = ComponentPlacement(
        component_id=inst.id, position=pos, rotation=0, side="top",
    )
    state.layout.placements[inst.id] = pl
    return inst, pl


class TestForeignWireNotDraggedByMove:
    """Reproduction of the user-reported "wire glued to moved pin" bug."""

    def test_foreign_wire_with_pinknot_anchored_elsewhere_stays_put(self) -> None:
        """Foreign wire anchored to R_OTHER's pin (not to J1) must NOT
        be translated when J1 moves, even when its path passes through
        J1's old pin position."""
        state = _State()
        # J1 at (2.5, 2.5); user will move it to (0.5, 0.5).
        j1, j1_old_pl = _place(state, "J1", Point(2.5, 2.5))
        # R_OTHER at (5.5, 5.5) — its pin owns the foreign wire.
        r_other, _ = _place(state, "R_OTHER", Point(5.5, 5.5))
        # Foreign wire: endpoint a at R_OTHER's pin (5.5, 5.5) anchored
        # via PinKnot; endpoint b drawn through J1's old pin position
        # (2.5, 2.5) — so the path *passes through* J1's old pin but
        # is logically owned by R_OTHER.
        pinknot_a = PinKnot(
            id=uuid.uuid4(),
            pin=PinRef(component_id=r_other.id, pin_number=1),
        )
        state.layout.knots[pinknot_a.id] = pinknot_a
        foreign_wire = Wire(
            id=uuid.uuid4(),
            path=[Point(5.5, 5.5), Point(2.5, 2.5)],
            insulated=False, side="top",
            a=KnotRef(id=pinknot_a.id),
        )
        state.layout.wires.append(foreign_wire)
        # J1 moves.
        j1_new_pl = ComponentPlacement(
            component_id=j1.id, position=Point(0.5, 0.5),
            rotation=0, side="top",
        )
        state.layout.placements[j1.id] = j1_new_pl

        _update_wires_for_move(
            state, j1.id, j1_old_pl, j1_new_pl, frozenset(),
        )

        # Foreign wire path unchanged — (2.5, 2.5) endpoint stays put.
        assert [(p.x, p.y) for p in foreign_wire.path] == [(5.5, 5.5), (2.5, 2.5)]

    def test_foreign_wire_mid_segment_through_old_pin_stays_put(self) -> None:
        """Even more subtle: foreign wire whose *interior vertex* (not
        endpoint) sits on the old pin position must not get translated."""
        state = _State()
        j1, j1_old_pl = _place(state, "J1", Point(2.5, 2.5))
        # Wire (1.5, 2.5)→(2.5, 2.5)→(3.5, 2.5) — middle vertex on
        # old pin. No knot refs (orphan in this scenario).
        foreign_wire = Wire(
            id=uuid.uuid4(),
            path=[Point(1.5, 2.5), Point(2.5, 2.5), Point(3.5, 2.5)],
            insulated=False, side="top",
        )
        state.layout.wires.append(foreign_wire)
        j1_new_pl = ComponentPlacement(
            component_id=j1.id, position=Point(0.5, 0.5),
            rotation=0, side="top",
        )
        state.layout.placements[j1.id] = j1_new_pl

        _update_wires_for_move(
            state, j1.id, j1_old_pl, j1_new_pl, frozenset(),
        )

        # Path unchanged — even though path[1] == old_pin, the wire's
        # endpoints (path[0], path[-1]) don't coincide with it, so the
        # weak-fallback anchor check returns False.
        assert [(p.x, p.y) for p in foreign_wire.path] == [
            (1.5, 2.5), (2.5, 2.5), (3.5, 2.5)
        ]

    def test_own_wire_with_pinknot_anchor_does_translate(self) -> None:
        """Sanity inverse: a wire properly anchored to the moved
        component's pin via PinKnot still translates with the move."""
        state = _State()
        j1, j1_old_pl = _place(state, "J1", Point(2.5, 2.5))
        pinknot = PinKnot(
            id=uuid.uuid4(),
            pin=PinRef(component_id=j1.id, pin_number=1),
        )
        state.layout.knots[pinknot.id] = pinknot
        own_wire = Wire(
            id=uuid.uuid4(),
            path=[Point(2.5, 2.5), Point(4.5, 4.5)],
            insulated=False, side="top",
            a=KnotRef(id=pinknot.id),
        )
        state.layout.wires.append(own_wire)
        j1_new_pl = ComponentPlacement(
            component_id=j1.id, position=Point(0.5, 0.5),
            rotation=0, side="top",
        )
        state.layout.placements[j1.id] = j1_new_pl

        _update_wires_for_move(
            state, j1.id, j1_old_pl, j1_new_pl, frozenset(),
        )

        # Wire endpoint followed the pin from (2.5,2.5) to (0.5,0.5).
        assert own_wire.path[0] == Point(0.5, 0.5)
        assert own_wire.path[-1] == Point(4.5, 4.5)

    def test_orphan_wire_endpoint_on_old_pin_does_translate(self) -> None:
        """Weak-fallback: wire with no knot refs whose endpoint sits
        on old_pin_pos is treated as anchored (historic behaviour)."""
        state = _State()
        j1, j1_old_pl = _place(state, "J1", Point(2.5, 2.5))
        orphan_wire = Wire(
            id=uuid.uuid4(),
            path=[Point(2.5, 2.5), Point(4.5, 4.5)],
            insulated=False, side="top",
            # no a/b refs
        )
        state.layout.wires.append(orphan_wire)
        j1_new_pl = ComponentPlacement(
            component_id=j1.id, position=Point(0.5, 0.5),
            rotation=0, side="top",
        )
        state.layout.placements[j1.id] = j1_new_pl

        _update_wires_for_move(
            state, j1.id, j1_old_pl, j1_new_pl, frozenset(),
        )

        assert orphan_wire.path[0] == Point(0.5, 0.5)
