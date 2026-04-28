# SPDX-License-Identifier: MIT
"""Tests for CommandStack (undo/redo) and core commands."""

import uuid

import pytest

from protoboard.domain.commands.base import Command, CommandStack
from protoboard.domain.commands.component_cmds import (
    PlaceComponentCommand,
    RemoveComponentCommand,
)
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.geometry import Point


class _Counter:
    """Shared mutable state used as a simple project stub."""

    def __init__(self) -> None:
        self.value = 0


class _IncrCmd(Command):
    description = "increment"

    def apply(self, state: object) -> None:
        assert isinstance(state, _Counter)
        state.value += 1

    def revert(self, state: object) -> None:
        assert isinstance(state, _Counter)
        state.value -= 1


class TestCommandStack:
    def test_execute_applies_command(self) -> None:
        counter = _Counter()
        stack: CommandStack[_Counter] = CommandStack()
        stack.execute(_IncrCmd(), counter)
        assert counter.value == 1

    def test_undo_reverts_command(self) -> None:
        counter = _Counter()
        stack: CommandStack[_Counter] = CommandStack()
        stack.execute(_IncrCmd(), counter)
        stack.undo(counter)
        assert counter.value == 0

    def test_redo_reapplies_command(self) -> None:
        counter = _Counter()
        stack: CommandStack[_Counter] = CommandStack()
        stack.execute(_IncrCmd(), counter)
        stack.undo(counter)
        stack.redo(counter)
        assert counter.value == 1

    def test_execute_clears_redo_stack(self) -> None:
        counter = _Counter()
        stack: CommandStack[_Counter] = CommandStack()
        stack.execute(_IncrCmd(), counter)
        stack.undo(counter)
        stack.execute(_IncrCmd(), counter)  # new command → clears redo
        assert not stack.can_redo()

    def test_can_undo_after_execute(self) -> None:
        stack: CommandStack[_Counter] = CommandStack()
        assert not stack.can_undo()
        stack.execute(_IncrCmd(), _Counter())
        assert stack.can_undo()

    def test_can_redo_after_undo(self) -> None:
        counter = _Counter()
        stack: CommandStack[_Counter] = CommandStack()
        stack.execute(_IncrCmd(), counter)
        assert not stack.can_redo()
        stack.undo(counter)
        assert stack.can_redo()

    def test_undo_empty_stack_raises(self) -> None:
        stack: CommandStack[_Counter] = CommandStack()
        with pytest.raises(IndexError):
            stack.undo(_Counter())

    def test_redo_empty_stack_raises(self) -> None:
        stack: CommandStack[_Counter] = CommandStack()
        with pytest.raises(IndexError):
            stack.redo(_Counter())

    def test_multiple_undo_redo(self) -> None:
        counter = _Counter()
        stack: CommandStack[_Counter] = CommandStack()
        for _ in range(3):
            stack.execute(_IncrCmd(), counter)
        assert counter.value == 3
        stack.undo(counter)
        stack.undo(counter)
        assert counter.value == 1
        stack.redo(counter)
        assert counter.value == 2


class TestPlaceComponentCommand:
    def test_place_and_undo(self) -> None:
        comp_id = uuid.uuid4()
        pl = ComponentPlacement(
            component_id=comp_id,
            position=Point(2.0, 3.0),
            rotation=0,
            side="top",
        )
        cmd = PlaceComponentCommand(component_id=comp_id, placement=pl)
        lay = Layout(placements={}, wires=[], cuts=[])

        cmd.apply(lay)
        assert comp_id in lay.placements

        cmd.revert(lay)
        assert comp_id not in lay.placements

    def test_description(self) -> None:
        comp_id = uuid.uuid4()
        pl = ComponentPlacement(
            component_id=comp_id,
            position=Point(0.0, 0.0),
            rotation=0,
            side="top",
        )
        cmd = PlaceComponentCommand(component_id=comp_id, placement=pl)
        assert "Place" in cmd.description or "place" in cmd.description


class TestRemoveComponentCommand:
    def test_remove_and_undo(self) -> None:
        comp_id = uuid.uuid4()
        pl = ComponentPlacement(
            component_id=comp_id,
            position=Point(1.0, 1.0),
            rotation=0,
            side="top",
        )
        lay = Layout(placements={comp_id: pl}, wires=[], cuts=[])

        cmd = RemoveComponentCommand(component_id=comp_id)
        cmd.apply(lay)
        assert comp_id not in lay.placements

        cmd.revert(lay)
        assert comp_id in lay.placements

    def test_remove_nonexistent_raises(self) -> None:
        lay = Layout(placements={}, wires=[], cuts=[])
        cmd = RemoveComponentCommand(component_id=uuid.uuid4())
        with pytest.raises(KeyError):
            cmd.apply(lay)
