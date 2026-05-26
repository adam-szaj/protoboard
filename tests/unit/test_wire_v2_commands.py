# SPDX-License-Identifier: MIT
"""Tests for v2 wire commands (Add/Remove/ReplaceFragments)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.commands.component_cmds import (
    AddWireV2Command,
    RemoveWireV2Command,
    ReplaceWireFragmentsCommand,
)
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.wire import Fragment, KnotRef, Wire
from protoboard.domain.schematic.net import PinRef
from protoboard.geometry import Point


@dataclass
class _State:
    layout: Layout
    board: Board
    instances: dict = None  # type: ignore[assignment]
    components: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.instances is None:
            self.instances = {}
        if self.components is None:
            self.components = {}


def _board(rows: int = 1, cols: int = 6, plated: bool = False) -> Board:
    grid = BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=plated),
        ),
        rows=rows, cols=cols, row_gap=0.0, col_gap=0.0,
    )
    return Board(root=grid)


def _setup(positions: dict[PinRef, Point]) -> _State:
    layout = Layout()
    layout.set_pin_resolver(lambda p: positions[p])
    return _State(layout=layout, board=_board())


def _pin(component_int: int, pin_number: int = 1) -> PinRef:
    return PinRef(component_id=uuid.UUID(int=component_int), pin_number=pin_number)


class TestAddWireV2Command:
    def test_apply_then_revert_restores(self) -> None:
        positions = {_pin(1): Point(0.5, 0.5), _pin(2): Point(5.5, 0.5)}
        state = _setup(positions)
        ka = state.layout.get_or_create_pin_knot(_pin(1))
        kb = state.layout.get_or_create_pin_knot(_pin(2))
        wire = Wire(
            id=uuid.uuid4(),
            a=KnotRef(id=ka.id), b=KnotRef(id=kb.id),
            fragments=(Fragment(path=(Point(0.5, 0.5), Point(5.5, 0.5)), side="top"),),
            insulated=False,
        )
        cmd = AddWireV2Command(wire=wire)
        cmd.apply(state)
        assert wire.id in state.layout.wires_v2
        assert wire.id in state.layout.knots[ka.id].wire_ids

        cmd.revert(state)
        assert wire.id not in state.layout.wires_v2
        assert wire.id not in state.layout.knots[ka.id].wire_ids

    def test_extra_joint_knot_registered_and_cleaned_up(self) -> None:
        positions = {_pin(1): Point(0.5, 0.5)}
        state = _setup(positions)
        ka = state.layout.get_or_create_pin_knot(_pin(1))
        from protoboard.domain.layout.knot import JointKnot
        joint = JointKnot(id=uuid.uuid4(), position=Point(2.5, 0.5))
        wire = Wire(
            id=uuid.uuid4(),
            a=KnotRef(id=ka.id), b=KnotRef(id=joint.id),
            fragments=(Fragment(path=(Point(0.5, 0.5), Point(2.5, 0.5)), side="top"),),
            insulated=False,
        )
        cmd = AddWireV2Command(wire=wire, extra_knots=[joint])
        cmd.apply(state)
        assert joint.id in state.layout.knots
        cmd.revert(state)
        # Wire gone -> joint becomes orphan; revert drops it.
        assert joint.id not in state.layout.knots


class TestRemoveWireV2Command:
    def test_orphan_joint_restored_on_revert(self) -> None:
        positions = {_pin(1): Point(0.5, 0.5)}
        state = _setup(positions)
        ka = state.layout.get_or_create_pin_knot(_pin(1))
        joint = state.layout.create_joint_knot(Point(2.5, 0.5))
        wire = Wire(
            id=uuid.uuid4(),
            a=KnotRef(id=ka.id), b=KnotRef(id=joint.id),
            fragments=(Fragment(path=(Point(0.5, 0.5), Point(2.5, 0.5)), side="top"),),
            insulated=False,
        )
        state.layout.add_wire_v2(wire)

        cmd = RemoveWireV2Command(wire_id=wire.id)
        cmd.apply(state)
        assert wire.id not in state.layout.wires_v2
        assert joint.id not in state.layout.knots  # orphan joint dropped

        cmd.revert(state)
        assert wire.id in state.layout.wires_v2
        assert joint.id in state.layout.knots  # joint restored
        assert wire.id in state.layout.knots[ka.id].wire_ids

    def test_revert_noop_when_no_wire_was_present(self) -> None:
        state = _setup({})
        cmd = RemoveWireV2Command(wire_id=uuid.uuid4())
        cmd.apply(state)  # no wire to remove
        cmd.revert(state)  # must not crash


class TestReplaceWireFragmentsCommand:
    def test_wire_id_preserved(self) -> None:
        positions = {_pin(1): Point(0.5, 0.5), _pin(2): Point(5.5, 0.5)}
        state = _setup(positions)
        ka = state.layout.get_or_create_pin_knot(_pin(1))
        kb = state.layout.get_or_create_pin_knot(_pin(2))
        wire = Wire(
            id=uuid.uuid4(),
            a=KnotRef(id=ka.id), b=KnotRef(id=kb.id),
            fragments=(Fragment(path=(Point(0.5, 0.5), Point(5.5, 0.5)), side="top"),),
            insulated=False,
        )
        state.layout.add_wire_v2(wire)
        original_id = wire.id

        new_frags = (
            Fragment(path=(Point(0.5, 0.5), Point(2.5, 0.5), Point(5.5, 0.5)), side="top"),
        )
        cmd = ReplaceWireFragmentsCommand(wire_id=wire.id, new_fragments=new_frags)
        cmd.apply(state)
        # The headline invariant: id stays.
        assert state.layout.wires_v2[original_id].id == original_id
        assert len(state.layout.wires_v2[original_id].fragments[0].path) == 3

        cmd.revert(state)
        assert state.layout.wires_v2[original_id].id == original_id
        assert len(state.layout.wires_v2[original_id].fragments[0].path) == 2

    def test_apply_noop_for_unknown_wire(self) -> None:
        state = _setup({})
        cmd = ReplaceWireFragmentsCommand(
            wire_id=uuid.uuid4(),
            new_fragments=(Fragment(path=(Point(0, 0), Point(1, 0)), side="top"),),
        )
        cmd.apply(state)  # must not crash
        cmd.revert(state)  # must not crash
