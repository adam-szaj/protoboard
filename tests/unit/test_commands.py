# SPDX-License-Identifier: MIT
"""Tests for CommandStack (undo/redo) and core commands."""

import uuid

import pytest

from protoboard.domain.commands.base import Command, CommandStack
from protoboard.domain.commands.component_cmds import (
    PlaceComponentCommand,
    RemoveComponentCommand,
)
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement, PlacementCollection
from protoboard.domain.layout.wire import WireCollection
from protoboard.geometry import Point


class _FakeSession:
    """Minimal ComponentState stub for command tests (no Qt dependency)."""

    def __init__(self) -> None:
        from protoboard.domain.board.board import Board
        from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
        from protoboard.domain.board.pads import PadLayers

        self.board = Board(
            root=BoardGrid(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                cell=PadAtom(
                    width_policy=SizePolicy.HUG,
                    height_policy=SizePolicy.HUG,
                    layers=PadLayers(),
                ),
                rows=1,
                cols=1,
                row_gap=0.0,
                col_gap=0.0,
            )
        )
        self.layout = Layout(placements=PlacementCollection({}), wires=WireCollection())
        self.instances: dict[uuid.UUID, ComponentInstance] = {}
        from protoboard.domain.components.definition import ComponentDefinition

        self.components: dict[str, ComponentDefinition] = {}
        from protoboard.domain.schematic.schematic import Schematic

        self.schematic = Schematic(nets={})


class _Counter:
    """Shared mutable state used as a simple project stub."""

    def __init__(self) -> None:
        self.value = 0


class _IncrCmd(Command[_Counter]):
    description = "increment"

    def apply(self, state: _Counter) -> None:
        state.value += 1

    def revert(self, state: _Counter) -> None:
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
        inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        cmd = PlaceComponentCommand(component_id=comp_id, placement=pl, instance=inst)
        sess = _FakeSession()

        cmd.apply(sess)
        assert comp_id in sess.layout.placements
        assert comp_id in sess.instances

        cmd.revert(sess)
        assert comp_id not in sess.layout.placements
        assert comp_id not in sess.instances

    def test_description(self) -> None:
        comp_id = uuid.uuid4()
        pl = ComponentPlacement(
            component_id=comp_id,
            position=Point(0.0, 0.0),
            rotation=0,
            side="top",
        )
        inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        cmd = PlaceComponentCommand(component_id=comp_id, placement=pl, instance=inst)
        assert "Place" in cmd.description or "place" in cmd.description


class TestMoveComponentNoOpRevert:
    """MoveComponentCommand's _applied guard (now ConditionalCommand).

    A locked move declines to commit: apply() leaves the placement alone
    and revert() must be a no-op (not blindly restore old_placement, which
    on a redo-after-failed-apply could clobber a position set elsewhere).
    The happy path commits and revert restores the old placement.
    """

    @staticmethod
    def _placement(
        comp_id: uuid.UUID, pos: Point, *, locked: bool = False
    ) -> ComponentPlacement:
        return ComponentPlacement(
            component_id=comp_id,
            position=pos,
            rotation=0,
            side="top",
            locked=locked,
        )

    def test_locked_move_is_noop_apply_and_noop_revert(self) -> None:
        comp_id = uuid.uuid4()
        locked_pl = self._placement(comp_id, Point(2.0, 3.0), locked=True)
        sess = _FakeSession()
        sess.layout.placements[comp_id] = locked_pl

        from protoboard.domain.commands.component_cmds import MoveComponentCommand

        cmd = MoveComponentCommand(
            component_id=comp_id,
            new_placement=self._placement(comp_id, Point(9.0, 9.0)),
            old_placement=locked_pl,
            skip_conflict_check=True,
            placement_only=True,
        )
        cmd.apply(sess)
        # apply declined: placement unchanged.
        assert sess.layout.placements[comp_id].position == Point(2.0, 3.0)

        # Mutate the placement after the no-op apply, then revert: a guarded
        # revert must NOT touch it (the bug a naive `placements[id]=old` reintroduces).
        sess.layout.placements[comp_id] = self._placement(comp_id, Point(5.0, 5.0))
        cmd.revert(sess)
        assert sess.layout.placements[comp_id].position == Point(5.0, 5.0)

    def test_unlocked_move_commits_and_reverts(self) -> None:
        comp_id = uuid.uuid4()
        old_pl = self._placement(comp_id, Point(2.0, 3.0))
        new_pl = self._placement(comp_id, Point(7.0, 8.0))
        sess = _FakeSession()
        sess.layout.placements[comp_id] = old_pl

        from protoboard.domain.commands.component_cmds import MoveComponentCommand

        cmd = MoveComponentCommand(
            component_id=comp_id,
            new_placement=new_pl,
            old_placement=old_pl,
            skip_conflict_check=True,
            placement_only=True,
        )
        cmd.apply(sess)
        assert sess.layout.placements[comp_id].position == Point(7.0, 8.0)
        cmd.revert(sess)
        assert sess.layout.placements[comp_id].position == Point(2.0, 3.0)


class TestRemoveComponentCommand:
    def test_remove_and_undo(self) -> None:
        comp_id = uuid.uuid4()
        pl = ComponentPlacement(
            component_id=comp_id,
            position=Point(1.0, 1.0),
            rotation=0,
            side="top",
        )
        inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        sess = _FakeSession()
        sess.layout.placements[comp_id] = pl
        sess.instances[comp_id] = inst

        cmd = RemoveComponentCommand(component_id=comp_id)
        cmd.apply(sess)
        assert comp_id not in sess.layout.placements
        assert comp_id not in sess.instances

        cmd.revert(sess)
        assert comp_id in sess.layout.placements
        assert comp_id in sess.instances

    def test_remove_nonexistent_raises(self) -> None:
        sess = _FakeSession()
        cmd = RemoveComponentCommand(component_id=uuid.uuid4())
        with pytest.raises(KeyError):
            cmd.apply(sess)

    def test_removes_pins_from_nets(self) -> None:
        """Removing a component also removes its pins from all nets."""
        from protoboard.domain.schematic.net import Net, PinRef

        comp_id = uuid.uuid4()
        other_id = uuid.uuid4()
        pl = ComponentPlacement(
            component_id=comp_id, position=Point(1.0, 1.0), rotation=0, side="top"
        )
        inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="")
        ref_a = PinRef(component_id=comp_id, pin_number=1)
        ref_b = PinRef(component_id=other_id, pin_number=1)
        net = Net(id=uuid.uuid4(), name="N1", pins={ref_a, ref_b})
        sess = _FakeSession()
        sess.layout.placements[comp_id] = pl
        sess.instances[comp_id] = inst
        sess.schematic.nets[net.id] = net

        cmd = RemoveComponentCommand(component_id=comp_id)
        cmd.apply(sess)

        # Net should still exist but without comp_id's pin.
        assert net.id in sess.schematic.nets
        assert ref_a not in sess.schematic.nets[net.id].pins
        assert ref_b in sess.schematic.nets[net.id].pins

        cmd.revert(sess)
        assert ref_a in sess.schematic.nets[net.id].pins

    def test_removes_empty_net(self) -> None:
        """Net with only one component's pins is deleted entirely on remove."""
        from protoboard.domain.schematic.net import Net, PinRef

        comp_id = uuid.uuid4()
        pl = ComponentPlacement(
            component_id=comp_id, position=Point(1.0, 1.0), rotation=0, side="top"
        )
        inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="")
        ref = PinRef(component_id=comp_id, pin_number=1)
        net = Net(id=uuid.uuid4(), name="SOLO", pins={ref})
        sess = _FakeSession()
        sess.layout.placements[comp_id] = pl
        sess.instances[comp_id] = inst
        sess.schematic.nets[net.id] = net

        cmd = RemoveComponentCommand(component_id=comp_id)
        cmd.apply(sess)
        assert net.id not in sess.schematic.nets

        cmd.revert(sess)
        assert net.id in sess.schematic.nets
        assert ref in sess.schematic.nets[net.id].pins


# ---------------------------------------------------------------------------
# MacroCommand atomic rollback on partial-failure
# ---------------------------------------------------------------------------


class _FailingCmd(Command[_Counter]):
    """Sub-command whose apply() unconditionally raises."""

    description = "always fails"

    def apply(self, state: _Counter) -> None:
        raise RuntimeError("intentional failure")

    def revert(self, state: _Counter) -> None:
        # Should never be called; if it is, the rollback machinery is
        # double-counting.  Mark with sentinel.
        state.value += 1000


class _FailingRevertCmd(Command[_Counter]):
    """Sub-command whose revert() raises (used to test rollback-error logging)."""

    description = "revert fails"

    def __init__(self) -> None:
        self.applied = False

    def apply(self, state: _Counter) -> None:
        self.applied = True
        state.value += 1

    def revert(self, state: _Counter) -> None:
        raise RuntimeError("rollback failure")


class TestMacroCommandAtomic:
    def test_failure_rolls_back_already_applied(self) -> None:
        from protoboard.domain.commands.base import MacroCommand

        counter = _Counter()
        # Two increments + one failing → after raise, counter should be 0.
        macro = MacroCommand(
            [_IncrCmd(), _IncrCmd(), _FailingCmd()],
            description="test",
        )
        with pytest.raises(RuntimeError, match="intentional failure"):
            macro.apply(counter)
        assert counter.value == 0

    def test_failure_at_first_command_no_rollback_needed(self) -> None:
        from protoboard.domain.commands.base import MacroCommand

        counter = _Counter()
        macro = MacroCommand(
            [_FailingCmd(), _IncrCmd()],
            description="test",
        )
        with pytest.raises(RuntimeError):
            macro.apply(counter)
        # First command failed before any state change; nothing to revert.
        assert counter.value == 0

    def test_success_path_unchanged(self) -> None:
        from protoboard.domain.commands.base import MacroCommand

        counter = _Counter()
        macro = MacroCommand(
            [_IncrCmd(), _IncrCmd(), _IncrCmd()],
            description="test",
        )
        macro.apply(counter)
        assert counter.value == 3

    def test_rollback_error_does_not_swallow_original_exception(self) -> None:
        from protoboard.domain.commands.base import MacroCommand

        counter = _Counter()
        # One applied-but-revert-fails + one failing.  Rollback for the
        # first cmd will raise; the original RuntimeError must still
        # surface.
        macro = MacroCommand(
            [_FailingRevertCmd(), _FailingCmd()],
            description="test",
        )
        with pytest.raises(RuntimeError, match="intentional failure"):
            macro.apply(counter)
        # The applied-then-failed-to-revert command's apply ran (value=1),
        # revert raised (value untouched), so counter stays at 1 — best
        # effort.  The contract is: "do not silently lose the original
        # exception"; full state restoration is best-effort.
        assert counter.value == 1

    def test_stack_does_not_record_failed_macro(self) -> None:
        from protoboard.domain.commands.base import CommandStack, MacroCommand

        counter = _Counter()
        stack: CommandStack[_Counter] = CommandStack()
        # Pre-load with a successful command so the undo stack is non-empty.
        stack.execute(_IncrCmd(), counter)
        assert counter.value == 1
        macro = MacroCommand(
            [_IncrCmd(), _FailingCmd()],
            description="failing macro",
        )
        with pytest.raises(RuntimeError):
            stack.execute(macro, counter)
        # State must be back to the post-first-execute snapshot
        # (rolled back the inner _IncrCmd), and undo stack must still
        # contain only the first command.
        assert counter.value == 1
        # Undoing one step should revert the first IncrCmd, NOT some
        # leftover macro entry.
        stack.undo(counter)
        assert counter.value == 0
