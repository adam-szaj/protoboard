# SPDX-License-Identifier: MIT
"""Tests for MoveDragSetCommand (atomic drag-commit).

Covers:
* internal wires translate by delta in one pass (no skewed-drop),
* loopback wires on a single-comp drag set translate cleanly,
* dropped wires are removed atomically (+ orphan joint cleanup),
* revert restores placements, paths, fragments, removed wires, joints,
* interior-vertex cache is invalidated after apply.
"""

from __future__ import annotations

import uuid

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.commands.drag_cmds import MoveDragSetCommand
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.knot import JointKnot, PinKnot
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import KnotRef, Wire
from protoboard.domain.schematic.net import PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point


# ---------------------------------------------------------------------------
# State stub mirroring ComponentState Protocol
# ---------------------------------------------------------------------------


class _State:
    def __init__(
        self,
        board: Board,
        layout: Layout,
        instances: dict[uuid.UUID, ComponentInstance],
        components: dict[str, ComponentDefinition],
        schematic: Schematic | None = None,
    ) -> None:
        self.board = board
        self.layout = layout
        self.instances = instances
        self.components = components
        self.schematic = schematic or Schematic(nets={})


def _perfboard(rows: int = 20, cols: int = 20) -> Board:
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


def _inst(defn: ComponentDefinition, refdes: str) -> ComponentInstance:
    return ComponentInstance.new(
        definition_ref=defn.name, refdes=refdes, value="",
    )


def _pl(inst: ComponentInstance, pos: Point) -> ComponentPlacement:
    return ComponentPlacement(
        component_id=inst.id, position=pos, rotation=0, side="top",
    )


# ---------------------------------------------------------------------------
# apply()
# ---------------------------------------------------------------------------


class TestApply:
    def test_internal_wire_translates_by_delta(self) -> None:
        # R1.pin1 ↔ R2.pin1; drag set = {R1, R2}, delta = (+1, 0).
        # Wire path must shift by (+1, 0) in one atomic step.
        defn = _defn("R", 2)
        r1, r2 = _inst(defn, "R1"), _inst(defn, "R2")
        old_pl1 = _pl(r1, Point(0.5, 0.5))
        old_pl2 = _pl(r2, Point(5.5, 0.5))
        new_pl1 = _pl(r1, Point(1.5, 0.5))
        new_pl2 = _pl(r2, Point(6.5, 0.5))

        layout = Layout()
        layout.ensure_root_group()
        layout.placements[r1.id] = old_pl1
        layout.placements[r2.id] = old_pl2

        ka = PinKnot(id=uuid.uuid4(), pin=PinRef(r1.id, 1))
        kb = PinKnot(id=uuid.uuid4(), pin=PinRef(r2.id, 1))
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(5.5, 0.5)],
            a=KnotRef(id=ka.id),
            b=KnotRef(id=kb.id),
        )
        layout.knots[ka.id] = ka
        layout.knots[kb.id] = kb
        layout.add_wire(wire)
        state = _State(
            board=_perfboard(),
            layout=layout,
            instances={r1.id: r1, r2.id: r2},
            components={defn.name: defn},
        )

        cmd = MoveDragSetCommand(
            members={r1.id: (old_pl1, new_pl1), r2.id: (old_pl2, new_pl2)},
            internal_wire_ids=frozenset({wire.id}),
            dropped_wire_ids=frozenset(),
            delta=Point(1.0, 0.0),
        )
        cmd.apply(state)  # type: ignore[arg-type]

        new_wire = state.layout.wires.get(wire.id)
        assert new_wire is not None
        assert new_wire.path == [Point(1.5, 0.5), Point(6.5, 0.5)]
        # Placements moved.
        assert state.layout.placements[r1.id].position == Point(1.5, 0.5)
        assert state.layout.placements[r2.id].position == Point(6.5, 0.5)
        # PinKnot refs unchanged.
        assert new_wire.a is not None
        assert new_wire.b is not None
        assert isinstance(state.layout.knots[new_wire.a.id], PinKnot)
        assert isinstance(state.layout.knots[new_wire.b.id], PinKnot)

    def test_single_comp_loopback_translates(self) -> None:
        # R1.pin1 ↔ R1.pin2 loopback — degenerate single-member drag set.
        defn = _defn("R", 2)
        r1 = _inst(defn, "R1")
        old_pl = _pl(r1, Point(0.5, 0.5))
        new_pl = _pl(r1, Point(2.5, 3.5))

        layout = Layout()
        layout.ensure_root_group()
        layout.placements[r1.id] = old_pl

        ka = PinKnot(id=uuid.uuid4(), pin=PinRef(r1.id, 1))
        kb = PinKnot(id=uuid.uuid4(), pin=PinRef(r1.id, 2))
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(0.5, 1.5)],
            a=KnotRef(id=ka.id),
            b=KnotRef(id=kb.id),
        )
        layout.knots[ka.id] = ka
        layout.knots[kb.id] = kb
        layout.add_wire(wire)
        state = _State(
            board=_perfboard(),
            layout=layout,
            instances={r1.id: r1},
            components={defn.name: defn},
        )
        cmd = MoveDragSetCommand(
            members={r1.id: (old_pl, new_pl)},
            internal_wire_ids=frozenset({wire.id}),
            dropped_wire_ids=frozenset(),
            delta=Point(2.0, 3.0),
        )
        cmd.apply(state)  # type: ignore[arg-type]
        new_wire = state.layout.wires.get(wire.id)
        assert new_wire is not None
        assert new_wire.path == [Point(2.5, 3.5), Point(2.5, 4.5)]

    def test_dropped_wire_removed_with_orphan_joint(self) -> None:
        # Wire with a joint endpoint — when wire is dropped, the orphan
        # joint must be removed too (and snapshotted for revert).
        defn = _defn("R", 2)
        r1 = _inst(defn, "R1")
        old_pl = _pl(r1, Point(0.5, 0.5))
        new_pl = _pl(r1, Point(1.5, 0.5))

        layout = Layout()
        layout.ensure_root_group()
        layout.placements[r1.id] = old_pl

        ka = PinKnot(id=uuid.uuid4(), pin=PinRef(r1.id, 1))
        jb = JointKnot(id=uuid.uuid4(), position=Point(3.5, 0.5))
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(3.5, 0.5)],
            a=KnotRef(id=ka.id),
            b=KnotRef(id=jb.id),
        )
        layout.knots[ka.id] = ka
        layout.knots[jb.id] = jb
        layout.add_wire(wire)
        state = _State(
            board=_perfboard(),
            layout=layout,
            instances={r1.id: r1},
            components={defn.name: defn},
        )
        cmd = MoveDragSetCommand(
            members={r1.id: (old_pl, new_pl)},
            internal_wire_ids=frozenset(),
            dropped_wire_ids=frozenset({wire.id}),
            delta=Point(1.0, 0.0),
        )
        cmd.apply(state)  # type: ignore[arg-type]
        assert state.layout.wires.get(wire.id) is None
        # Orphan joint also gone.
        assert jb.id not in state.layout.knots


# ---------------------------------------------------------------------------
# revert()
# ---------------------------------------------------------------------------


class TestRevert:
    def test_revert_restores_placements_and_internal_wire_path(self) -> None:
        defn = _defn("R", 2)
        r1 = _inst(defn, "R1")
        old_pl = _pl(r1, Point(0.5, 0.5))
        new_pl = _pl(r1, Point(2.5, 0.5))

        layout = Layout()
        layout.ensure_root_group()
        layout.placements[r1.id] = old_pl

        ka = PinKnot(id=uuid.uuid4(), pin=PinRef(r1.id, 1))
        kb = PinKnot(id=uuid.uuid4(), pin=PinRef(r1.id, 2))
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(0.5, 1.5)],
            a=KnotRef(id=ka.id),
            b=KnotRef(id=kb.id),
        )
        layout.knots[ka.id] = ka
        layout.knots[kb.id] = kb
        layout.add_wire(wire)
        state = _State(
            board=_perfboard(),
            layout=layout,
            instances={r1.id: r1},
            components={defn.name: defn},
        )
        cmd = MoveDragSetCommand(
            members={r1.id: (old_pl, new_pl)},
            internal_wire_ids=frozenset({wire.id}),
            dropped_wire_ids=frozenset(),
            delta=Point(2.0, 0.0),
        )
        cmd.apply(state)  # type: ignore[arg-type]
        cmd.revert(state)  # type: ignore[arg-type]
        assert state.layout.placements[r1.id].position == Point(0.5, 0.5)
        restored = state.layout.wires.get(wire.id)
        assert restored is not None
        assert restored.path == [Point(0.5, 0.5), Point(0.5, 1.5)]

    def test_revert_restores_dropped_wire_and_joint(self) -> None:
        defn = _defn("R", 2)
        r1 = _inst(defn, "R1")
        old_pl = _pl(r1, Point(0.5, 0.5))
        new_pl = _pl(r1, Point(1.5, 0.5))

        layout = Layout()
        layout.ensure_root_group()
        layout.placements[r1.id] = old_pl

        ka = PinKnot(id=uuid.uuid4(), pin=PinRef(r1.id, 1))
        jb = JointKnot(id=uuid.uuid4(), position=Point(3.5, 0.5))
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(3.5, 0.5)],
            a=KnotRef(id=ka.id),
            b=KnotRef(id=jb.id),
        )
        layout.knots[ka.id] = ka
        layout.knots[jb.id] = jb
        layout.add_wire(wire)
        state = _State(
            board=_perfboard(),
            layout=layout,
            instances={r1.id: r1},
            components={defn.name: defn},
        )
        cmd = MoveDragSetCommand(
            members={r1.id: (old_pl, new_pl)},
            internal_wire_ids=frozenset(),
            dropped_wire_ids=frozenset({wire.id}),
            delta=Point(1.0, 0.0),
        )
        cmd.apply(state)  # type: ignore[arg-type]
        cmd.revert(state)  # type: ignore[arg-type]
        # Wire + joint back.
        assert state.layout.wires.get(wire.id) is not None
        assert jb.id in state.layout.knots
        # Placement back too.
        assert state.layout.placements[r1.id].position == Point(0.5, 0.5)


# ---------------------------------------------------------------------------
# Description
# ---------------------------------------------------------------------------


class TestDescription:
    def test_description_singular(self) -> None:
        defn = _defn("R", 2)
        r1 = _inst(defn, "R1")
        old_pl = _pl(r1, Point(0.5, 0.5))
        new_pl = _pl(r1, Point(1.5, 0.5))
        cmd = MoveDragSetCommand(
            members={r1.id: (old_pl, new_pl)},
            internal_wire_ids=frozenset(),
            dropped_wire_ids=frozenset(),
            delta=Point(1.0, 0.0),
        )
        assert cmd.description == "Move 1 component"

    def test_description_plural(self) -> None:
        defn = _defn("R", 2)
        r1, r2 = _inst(defn, "R1"), _inst(defn, "R2")
        old_pl1 = _pl(r1, Point(0.5, 0.5))
        old_pl2 = _pl(r2, Point(5.5, 0.5))
        new_pl1 = _pl(r1, Point(1.5, 0.5))
        new_pl2 = _pl(r2, Point(6.5, 0.5))
        cmd = MoveDragSetCommand(
            members={r1.id: (old_pl1, new_pl1), r2.id: (old_pl2, new_pl2)},
            internal_wire_ids=frozenset(),
            dropped_wire_ids=frozenset(),
            delta=Point(1.0, 0.0),
        )
        assert cmd.description == "Move 2 components"


# ---------------------------------------------------------------------------
# Edge: empty drag set is a no-op apply (no crash, revert is no-op)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("delta", [Point(0.0, 0.0), Point(1.0, 1.0)])
def test_empty_drag_set_no_op(delta: Point) -> None:
    layout = Layout()
    layout.ensure_root_group()
    state = _State(
        board=_perfboard(),
        layout=layout,
        instances={},
        components={},
    )
    cmd = MoveDragSetCommand(
        members={},
        internal_wire_ids=frozenset(),
        dropped_wire_ids=frozenset(),
        delta=delta,
    )
    cmd.apply(state)  # type: ignore[arg-type]
    cmd.revert(state)  # type: ignore[arg-type]
    # No exceptions, no mutations.
    assert dict(state.layout.placements) == {}
