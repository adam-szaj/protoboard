# SPDX-License-Identifier: MIT
"""Tests for TransformDragSetCommand (atomic full-pose change).

Snapshot-based: apply writes the *after* geometry, revert writes the
*before*.  Covers:
* move-only round-trip (position),
* rotate-only round-trip (position + rotation field),
* mixed rotate+move in one command,
* free-knot (JointKnot) position restore on revert,
* wire path + fragments restore on revert.
"""

from __future__ import annotations

import uuid

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.commands.drag_cmds import TransformDragSetCommand
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.knot import JointKnot
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import Fragment, Wire
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.types import Side


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


def _pl(
    inst: ComponentInstance, pos: Point, rotation: int = 0, side: Side = "top"
) -> ComponentPlacement:
    return ComponentPlacement(
        component_id=inst.id, position=pos, rotation=rotation, side=side,
    )


def _state_with(
    placements: dict[uuid.UUID, ComponentPlacement],
    *,
    wires: list[Wire] | None = None,
    knots: list[JointKnot] | None = None,
    instances: dict[uuid.UUID, ComponentInstance] | None = None,
    components: dict[str, ComponentDefinition] | None = None,
) -> _State:
    layout = Layout()
    layout.ensure_root_group()
    for cid, pl in placements.items():
        layout.placements[cid] = pl
    for w in wires or []:
        layout.wires.append(w)
    for k in knots or []:
        layout.knots[k.id] = k
    return _State(
        _perfboard(), layout, instances or {}, components or {},
    )


class TestMoveOnly:
    def test_position_round_trip(self) -> None:
        defn = _defn("R", 2)
        r1 = _inst(defn, "R1")
        old = _pl(r1, Point(2.5, 2.5))
        new = _pl(r1, Point(5.5, 2.5))
        st = _state_with({r1.id: old})

        cmd = TransformDragSetCommand(
            members={r1.id: (old, new)},
            wire_paths={}, wire_fragments={}, knot_positions={},
        )
        cmd.apply(st)
        assert st.layout.placements[r1.id].position == Point(5.5, 2.5)
        cmd.revert(st)
        assert st.layout.placements[r1.id].position == Point(2.5, 2.5)


class TestRotateOnly:
    def test_rotation_field_round_trip(self) -> None:
        defn = _defn("U", 4)
        u1 = _inst(defn, "U1")
        old = _pl(u1, Point(4.5, 4.5), rotation=0)
        new = _pl(u1, Point(4.5, 4.5), rotation=90)
        st = _state_with({u1.id: old})

        cmd = TransformDragSetCommand(
            members={u1.id: (old, new)},
            wire_paths={}, wire_fragments={}, knot_positions={},
        )
        cmd.apply(st)
        assert st.layout.placements[u1.id].rotation == 90
        cmd.revert(st)
        assert st.layout.placements[u1.id].rotation == 0


class TestMixed:
    def test_rotate_and_move_in_one_command(self) -> None:
        defn = _defn("U", 4)
        u1, u2 = _inst(defn, "U1"), _inst(defn, "U2")
        old1 = _pl(u1, Point(3.5, 3.5), rotation=0)
        new1 = _pl(u1, Point(6.5, 3.5), rotation=90)   # moved + rotated
        old2 = _pl(u2, Point(8.5, 8.5), rotation=0, side="top")
        new2 = _pl(u2, Point(8.5, 8.5), rotation=180, side="bottom")  # rot+flip
        st = _state_with({u1.id: old1, u2.id: old2})

        cmd = TransformDragSetCommand(
            members={u1.id: (old1, new1), u2.id: (old2, new2)},
            wire_paths={}, wire_fragments={}, knot_positions={},
        )
        cmd.apply(st)
        assert st.layout.placements[u1.id].position == Point(6.5, 3.5)
        assert st.layout.placements[u1.id].rotation == 90
        assert st.layout.placements[u2.id].rotation == 180
        assert st.layout.placements[u2.id].side == "bottom"
        cmd.revert(st)
        assert st.layout.placements[u1.id].position == Point(3.5, 3.5)
        assert st.layout.placements[u1.id].rotation == 0
        assert st.layout.placements[u2.id].rotation == 0
        assert st.layout.placements[u2.id].side == "top"


class TestWireAndKnotGeometry:
    def test_free_knot_position_round_trip(self) -> None:
        kid = uuid.uuid4()
        knot = JointKnot(id=kid, position=Point(5.5, 5.5))
        st = _state_with({}, knots=[knot])

        cmd = TransformDragSetCommand(
            members={}, wire_paths={}, wire_fragments={},
            knot_positions={kid: (Point(5.5, 5.5), Point(7.5, 5.5))},
        )
        cmd.apply(st)
        moved = st.layout.knots[kid]
        assert isinstance(moved, JointKnot) and moved.position == Point(7.5, 5.5)
        cmd.revert(st)
        back = st.layout.knots[kid]
        assert isinstance(back, JointKnot) and back.position == Point(5.5, 5.5)

    def test_wire_path_and_fragments_round_trip(self) -> None:
        wid = uuid.uuid4()
        old_path = [Point(1.5, 1.5), Point(3.5, 1.5)]
        new_path = [Point(1.5, 4.5), Point(3.5, 4.5)]
        old_frag = Fragment(path=list(old_path), side="top")
        new_frag = Fragment(path=list(new_path), side="top")
        wire = Wire(
            id=wid, path=list(old_path), insulated=False, side="top",
            fragments=[old_frag],
        )
        st = _state_with({}, wires=[wire])

        cmd = TransformDragSetCommand(
            members={},
            wire_paths={wid: (old_path, new_path)},
            wire_fragments={wid: ([old_frag], [new_frag])},
            knot_positions={},
        )
        cmd.apply(st)
        got = st.layout.wires.get(wid)
        assert got is not None and got.path == new_path
        assert got.fragments[0].path == new_path
        cmd.revert(st)
        got = st.layout.wires.get(wid)
        assert got is not None and got.path == old_path
        assert got.fragments[0].path == old_path

    def test_revert_before_apply_is_noop(self) -> None:
        defn = _defn("R", 2)
        r1 = _inst(defn, "R1")
        old = _pl(r1, Point(2.5, 2.5))
        new = _pl(r1, Point(5.5, 2.5))
        st = _state_with({r1.id: old})
        cmd = TransformDragSetCommand(
            members={r1.id: (old, new)},
            wire_paths={}, wire_fragments={}, knot_positions={},
        )
        cmd.revert(st)  # never applied
        assert st.layout.placements[r1.id].position == Point(2.5, 2.5)
