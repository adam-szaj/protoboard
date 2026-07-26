# SPDX-License-Identifier: MIT
"""Tests for MacroCommand composite command."""

from __future__ import annotations

from dataclasses import dataclass, field

from protoboard.domain.commands.base import Command, CommandStack, MacroCommand

# ---------------------------------------------------------------------------
# Simple counter state + command for testing
# ---------------------------------------------------------------------------


@dataclass
class _CounterState:
    value: int = 0
    log: list[str] = field(default_factory=list)


class _IncrCmd(Command[_CounterState]):
    description = "incr"

    def apply(self, state: _CounterState) -> None:
        state.value += 1
        state.log.append("incr")

    def revert(self, state: _CounterState) -> None:
        state.value -= 1
        state.log.append("revert_incr")


class _DecrCmd(Command[_CounterState]):
    description = "decr"

    def apply(self, state: _CounterState) -> None:
        state.value -= 1
        state.log.append("decr")

    def revert(self, state: _CounterState) -> None:
        state.value += 1
        state.log.append("revert_decr")


# ---------------------------------------------------------------------------
# MacroCommand tests
# ---------------------------------------------------------------------------


class TestMacroCommand:
    def test_apply_runs_all_in_order(self) -> None:
        state = _CounterState()
        macro = MacroCommand([_IncrCmd(), _IncrCmd(), _DecrCmd()])
        macro.apply(state)
        assert state.value == 1
        assert state.log == ["incr", "incr", "decr"]

    def test_revert_runs_in_reverse(self) -> None:
        state = _CounterState()
        macro = MacroCommand([_IncrCmd(), _IncrCmd(), _DecrCmd()])
        macro.apply(state)
        macro.revert(state)
        assert state.value == 0
        assert state.log[-3:] == ["revert_decr", "revert_incr", "revert_incr"]

    def test_empty_macro(self) -> None:
        state = _CounterState()
        macro: MacroCommand[_CounterState] = MacroCommand([])
        macro.apply(state)
        macro.revert(state)
        assert state.value == 0

    def test_description(self) -> None:
        macro = MacroCommand([_IncrCmd()], description="Test macro")
        assert macro.description == "Test macro"

    def test_default_description(self) -> None:
        macro: MacroCommand[_CounterState] = MacroCommand([])
        assert "macro" in macro.description.lower()

    def test_commandstack_undo_redo_macro(self) -> None:
        state = _CounterState()
        stack: CommandStack[_CounterState] = CommandStack()
        macro = MacroCommand([_IncrCmd(), _IncrCmd()], description="two incr")
        stack.execute(macro, state)
        assert state.value == 2
        stack.undo(state)
        assert state.value == 0
        stack.redo(state)
        assert state.value == 2

    def test_nested_macro(self) -> None:
        state = _CounterState()
        inner = MacroCommand([_IncrCmd(), _IncrCmd()])
        outer = MacroCommand([inner, _DecrCmd()])
        outer.apply(state)
        assert state.value == 1
        outer.revert(state)
        assert state.value == 0


class TestReplaceWireFragmentsRevertWithSiblingRestore:
    """Regression: a "Move selection" macro can interleave
    ``ReplaceWireFragmentsCommand`` and ``RemoveWireCommand`` such that
    on revert the order is Remove → Replace.  Reverting Remove
    re-attaches a sibling wire to a JointKnot that the Replace had
    earlier slid (because it was the joint's only incident wire at the
    time).  Without the saved-position restore in
    ``ReplaceWireFragmentsCommand.revert`` the slide path would refuse
    to fire — degree is now 2 — and the whole undo would raise.

    User report 2026-05-30: 'Move selection' macro crashed in
    ``replace_wire_fragments`` with
    ``new path[-1] disagrees with knot ] position`` on undo.
    """

    def test_revert_succeeds_when_sibling_wire_restored_first(self) -> None:
        from dataclasses import dataclass as _dc
        from typing import Any, cast
        import uuid as _uuid

        from protoboard.domain.board.board import Board
        from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
        from protoboard.domain.board.pads import PadLayers
        from protoboard.domain.commands.component_cmds import (
            RemoveWireCommand,
            ReplaceWireFragmentsCommand,
        )
        from protoboard.domain.layout.knot import JointKnot
        from protoboard.domain.layout.layout import Layout
        from protoboard.domain.layout.placement import PlacementCollection
        from protoboard.domain.layout.wire import (
            Fragment,
            KnotRef,
            Wire,
            WireCollection,
        )
        from protoboard.geometry import Point

        board = Board(root=BoardGrid(
            width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
            cell=PadAtom(
                width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
                layers=PadLayers(top=True, bottom=True, plated_through=True),
            ),
            rows=5, cols=5, row_gap=0.0, col_gap=0.0,
        ))
        layout = Layout(placements=PlacementCollection({}), wires=WireCollection([]))

        # Joint A and Joint B carry both wires together.  Joint A is
        # the endpoint we'll later slide via ReplaceWireFragments.
        ja = JointKnot(id=_uuid.uuid4(), position=Point(0.5, 0.5))
        jb = JointKnot(id=_uuid.uuid4(), position=Point(2.5, 0.5))
        layout.knots[ja.id] = ja
        layout.knots[jb.id] = jb

        # Sibling wire (the one Remove will pull out, then revert
        # re-attaches).  Lives on Joint A and Joint B together with the
        # primary wire — degree=2 at both ends.
        w_sibling = Wire(
            id=_uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(2.5, 0.5)],
            insulated=True, side="top",
            a=KnotRef(id=ja.id), b=KnotRef(id=jb.id),
            fragments=[Fragment(path=[Point(0.5, 0.5), Point(2.5, 0.5)], side="top")],
        )
        # Primary wire — the one we replace fragments on.
        w_primary = Wire(
            id=_uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(2.5, 0.5)],
            insulated=True, side="top",
            a=KnotRef(id=ja.id), b=KnotRef(id=jb.id),
            fragments=[Fragment(path=[Point(0.5, 0.5), Point(2.5, 0.5)], side="top")],
        )
        layout.add_wire(w_sibling)
        layout.add_wire(w_primary)
        # After both add_wires, ja/jb each carry both wires.
        assert layout.knots[ja.id].wire_ids == frozenset({w_sibling.id, w_primary.id})

        class _State:
            pass
        state: Any = _State()
        state.board = board
        state.layout = layout
        state.instances = {}
        state.components = {}

        # Step 1 — Remove sibling.  Joint A's wire_ids becomes just
        # {primary}, degree=1, so the next Replace is allowed to slide.
        rm_cmd = RemoveWireCommand(wire_id=w_sibling.id)
        rm_cmd.apply(state)
        assert layout.knots[ja.id].wire_ids == frozenset({w_primary.id})

        # Step 2 — Replace primary's fragments with a different endpoint
        # at A.  This slides Joint A from (0.5,0.5) → (1.5,0.5).
        new_frags = (
            Fragment(path=[Point(1.5, 0.5), Point(2.5, 0.5)], side="top"),
        )
        rp_cmd = ReplaceWireFragmentsCommand(
            wire_id=w_primary.id, new_fragments=new_frags,
            old_fragments=tuple(w_primary.fragments),
        )
        rp_cmd.apply(state)
        assert cast(JointKnot, layout.knots[ja.id]).position == Point(1.5, 0.5)

        # Step 3 — Revert in reverse order (as MacroCommand does):
        # FIRST revert the Remove (sibling comes back, joint A's
        # wire_ids grows to {sibling, primary}), THEN revert the
        # Replace.  Without the saved-position restore the Replace
        # revert would raise because the slide path requires degree=1.
        rm_cmd.revert(state)
        assert layout.knots[ja.id].wire_ids == frozenset({w_sibling.id, w_primary.id})

        rp_cmd.revert(state)  # must not raise

        # Joint A is back at the original position.
        assert cast(JointKnot, layout.knots[ja.id]).position == Point(0.5, 0.5)
        # Primary wire path is back to the original.
        primary_after = layout.wires.get(w_primary.id)
        assert primary_after is not None
        assert primary_after.path[0] == Point(0.5, 0.5)
