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
