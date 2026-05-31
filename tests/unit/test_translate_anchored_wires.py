# SPDX-License-Identifier: MIT
"""Tests for TranslateAnchoredWiresCommand + DropWiresCommand.

These commands are the placement-only counterparts spun out of
MoveComponentCommand in the drag-pipeline refactor (Step 3). The
contract: TranslateAnchoredWires is observationally identical to
MoveComponent's old in-apply wire translation, but is a distinct,
undo-able command. DropWires is an atomic multi-wire remove with full
roundtrip support.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.commands.component_cmds import (
    DropWiresCommand,
    TranslateAnchoredWiresCommand,
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


@dataclass
class _State:
    """Minimal ComponentState — only the attrs the commands touch."""

    board: Board
    layout: Layout
    instances: dict[uuid.UUID, ComponentInstance]
    components: dict[str, ComponentDefinition]
    schematic: Schematic = field(default_factory=lambda: Schematic(nets={}))


def _perfboard(rows: int = 10, cols: int = 10) -> Board:
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
            row_gap=0,
            col_gap=0,
        )
    )


def _defn(name: str, n: int = 2) -> ComponentDefinition:
    return ComponentDefinition(
        name=name,
        footprint=SIL(n),
        pin_names={i: str(i) for i in range(1, n + 1)},
    )


def _inst(defn: ComponentDefinition, refdes: str = "R1") -> ComponentInstance:
    return ComponentInstance.new(
        definition_ref=defn.name, refdes=refdes, value="",
    )


def _state(
    placements: list[
        tuple[ComponentInstance, ComponentDefinition, ComponentPlacement]
    ],
) -> _State:
    layout = Layout()
    layout.ensure_root_group()
    instances: dict[uuid.UUID, ComponentInstance] = {}
    components: dict[str, ComponentDefinition] = {}
    for inst, defn, pl in placements:
        instances[inst.id] = inst
        components[defn.name] = defn
        layout.placements[inst.id] = pl
    return _State(
        board=_perfboard(),
        layout=layout,
        instances=instances,
        components=components,
    )


def _add_pin_anchored_wire(
    state: _State,
    comp_id: uuid.UUID,
    pin_num: int,
    end: Point,
) -> Wire:
    """Add a wire whose endpoint a is a PinKnot referencing (comp_id, pin_num)."""
    pin_ref = PinRef(component_id=comp_id, pin_number=pin_num)
    knot = PinKnot(id=uuid.uuid4(), pin=pin_ref)
    state.layout.knots[knot.id] = knot
    pl = state.layout.placements[comp_id]
    inst = state.instances[comp_id]
    defn = state.components[inst.definition_ref]
    from protoboard.domain.layout.placement import pin_world_position
    start = pin_world_position(defn.footprint, pl, pin_num)
    wire = Wire(
        id=uuid.uuid4(),
        path=[start, end],
        insulated=True,
        side="top",
        a=KnotRef(id=knot.id),
        b=None,
    )
    state.layout.wires.append(wire)
    return wire


class TestTranslateAnchoredWires:
    def test_translates_anchored_wire(self) -> None:
        defn = _defn("R", 2)
        inst = _inst(defn, "R1")
        old_pl = ComponentPlacement(
            component_id=inst.id, position=Point(0.5, 0.5),
            rotation=0, side="top",
        )
        state = _state([(inst, defn, old_pl)])
        wire = _add_pin_anchored_wire(
            state, inst.id, pin_num=1, end=Point(0.5, 4.5),
        )
        new_pl = ComponentPlacement(
            component_id=inst.id, position=Point(2.5, 0.5),
            rotation=0, side="top",
        )
        state.layout.placements[inst.id] = new_pl

        cmd = TranslateAnchoredWiresCommand(
            component_id=inst.id,
            new_placement=new_pl,
            old_placement=old_pl,
        )
        cmd.apply(state)
        # First vertex (anchored to pin 1) translated from (0.5, 0.5)
        # to (2.5, 0.5). Endpoint (0.5, 4.5) untouched.
        assert wire.path[0] == Point(2.5, 0.5)
        assert wire.path[1] == Point(0.5, 4.5)

    def test_does_not_translate_foreign_wire(self) -> None:
        defn = _defn("R", 2)
        inst_a = _inst(defn, "R1")
        inst_b = _inst(defn, "R2")
        old_pl_a = ComponentPlacement(
            component_id=inst_a.id, position=Point(0.5, 0.5),
            rotation=0, side="top",
        )
        pl_b = ComponentPlacement(
            component_id=inst_b.id, position=Point(4.5, 0.5),
            rotation=0, side="top",
        )
        state = _state([(inst_a, defn, old_pl_a), (inst_b, defn, pl_b)])
        # Wire is anchored to R2's pin 1, but its path happens to pass
        # through R1's pin 1 position (0.5, 0.5).
        foreign_wire = _add_pin_anchored_wire(
            state, inst_b.id, pin_num=1, end=Point(0.5, 0.5),
        )
        new_pl_a = ComponentPlacement(
            component_id=inst_a.id, position=Point(2.5, 0.5),
            rotation=0, side="top",
        )
        state.layout.placements[inst_a.id] = new_pl_a

        original_path = list(foreign_wire.path)
        cmd = TranslateAnchoredWiresCommand(
            component_id=inst_a.id,
            new_placement=new_pl_a,
            old_placement=old_pl_a,
        )
        cmd.apply(state)
        # Foreign wire still in original position despite endpoint at
        # R1's old pin 1.
        assert foreign_wire.path == original_path

    def test_undo_restores_wires(self) -> None:
        defn = _defn("R", 2)
        inst = _inst(defn, "R1")
        old_pl = ComponentPlacement(
            component_id=inst.id, position=Point(0.5, 0.5),
            rotation=0, side="top",
        )
        state = _state([(inst, defn, old_pl)])
        wire = _add_pin_anchored_wire(
            state, inst.id, pin_num=1, end=Point(0.5, 4.5),
        )
        new_pl = ComponentPlacement(
            component_id=inst.id, position=Point(2.5, 0.5),
            rotation=0, side="top",
        )
        state.layout.placements[inst.id] = new_pl
        original_path = list(wire.path)
        cmd = TranslateAnchoredWiresCommand(
            component_id=inst.id,
            new_placement=new_pl,
            old_placement=old_pl,
        )
        cmd.apply(state)
        cmd.revert(state)
        # Wires list restored to pre-apply snapshot.
        same_wire = next(w for w in state.layout.wires if w.id == wire.id)
        assert same_wire.path == original_path


class TestDropWires:
    def test_removes_wires(self) -> None:
        defn = _defn("R", 2)
        inst = _inst(defn, "R1")
        pl = ComponentPlacement(
            component_id=inst.id, position=Point(0.5, 0.5),
            rotation=0, side="top",
        )
        state = _state([(inst, defn, pl)])
        w1 = _add_pin_anchored_wire(
            state, inst.id, pin_num=1, end=Point(0.5, 4.5),
        )
        w2 = _add_pin_anchored_wire(
            state, inst.id, pin_num=2, end=Point(1.5, 4.5),
        )

        cmd = DropWiresCommand(wire_ids=frozenset({w1.id, w2.id}))
        cmd.apply(state)
        assert state.layout.wires.get(w1.id) is None
        assert state.layout.wires.get(w2.id) is None

    def test_undo_restores_wires(self) -> None:
        defn = _defn("R", 2)
        inst = _inst(defn, "R1")
        pl = ComponentPlacement(
            component_id=inst.id, position=Point(0.5, 0.5),
            rotation=0, side="top",
        )
        state = _state([(inst, defn, pl)])
        w1 = _add_pin_anchored_wire(
            state, inst.id, pin_num=1, end=Point(0.5, 4.5),
        )
        original = list(w1.path)

        cmd = DropWiresCommand(wire_ids=frozenset({w1.id}))
        cmd.apply(state)
        cmd.revert(state)
        restored = state.layout.wires.get(w1.id)
        assert restored is not None
        assert list(restored.path) == original

    def test_empty_id_set_is_noop(self) -> None:
        defn = _defn("R", 2)
        inst = _inst(defn, "R1")
        pl = ComponentPlacement(
            component_id=inst.id, position=Point(0.5, 0.5),
            rotation=0, side="top",
        )
        state = _state([(inst, defn, pl)])
        w1 = _add_pin_anchored_wire(
            state, inst.id, pin_num=1, end=Point(0.5, 4.5),
        )
        before = len(state.layout.wires)
        cmd = DropWiresCommand(wire_ids=frozenset())
        cmd.apply(state)
        cmd.revert(state)
        assert len(state.layout.wires) == before
        assert state.layout.wires.get(w1.id) is not None
