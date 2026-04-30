# SPDX-License-Identifier: MIT
"""Command base class and generic CommandStack."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Generic, TypeVar

T = TypeVar("T")


class Command(ABC, Generic[T]):
    description: str = ""

    @abstractmethod
    def apply(self, state: T) -> None: ...

    @abstractmethod
    def revert(self, state: T) -> None: ...


class MacroCommand(Command[T], Generic[T]):
    """Composite command — applies/reverts a list of sub-commands atomically."""

    def __init__(
        self, commands: Sequence[Command[T]], description: str = "Macro"
    ) -> None:
        self._commands = list(commands)
        self.description = description

    def apply(self, state: T) -> None:
        for cmd in self._commands:
            cmd.apply(state)

    def revert(self, state: T) -> None:
        for cmd in reversed(self._commands):
            cmd.revert(state)


class CommandStack(Generic[T]):
    def __init__(self) -> None:
        self._undo: list[Command[T]] = []
        self._redo: list[Command[T]] = []

    def execute(self, cmd: Command[T], state: T) -> None:
        cmd.apply(state)
        self._undo.append(cmd)
        self._redo.clear()

    def undo(self, state: T) -> None:
        if not self._undo:
            raise IndexError("nothing to undo")
        cmd = self._undo.pop()
        cmd.revert(state)
        self._redo.append(cmd)

    def redo(self, state: T) -> None:
        if not self._redo:
            raise IndexError("nothing to redo")
        cmd = self._redo.pop()
        cmd.apply(state)
        self._undo.append(cmd)

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)
