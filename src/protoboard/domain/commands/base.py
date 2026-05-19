# SPDX-License-Identifier: MIT
"""Command base class and generic CommandStack."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any, Generic, Protocol, TypeVar

import structlog

_log = structlog.get_logger(__name__)

T = TypeVar("T")


class _RecorderProtocol(Protocol):
    """Subset of CommandRecorder used by CommandStack — kept tiny to avoid a
    hard import dependency on the services layer from domain code."""

    def record_execute(self, cmd: "Command[Any]") -> None: ...
    def record_undo(self) -> None: ...
    def record_redo(self) -> None: ...


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
        self._recorder: _RecorderProtocol | None = None

    def set_recorder(self, recorder: "_RecorderProtocol | None") -> None:
        """Attach (or clear) a recorder that observes every execute/undo/redo."""
        self._recorder = recorder

    def execute(self, cmd: Command[T], state: T) -> None:
        cmd.apply(state)
        _log.info("cmd.execute", cmd=type(cmd).__name__, desc=cmd.description)
        self._undo.append(cmd)
        self._redo.clear()
        if self._recorder is not None:
            self._recorder.record_execute(cmd)

    def undo(self, state: T) -> None:
        if not self._undo:
            raise IndexError("nothing to undo")
        cmd = self._undo.pop()
        cmd.revert(state)
        _log.info("cmd.undo", cmd=type(cmd).__name__, desc=cmd.description)
        self._redo.append(cmd)
        if self._recorder is not None:
            self._recorder.record_undo()

    def redo(self, state: T) -> None:
        if not self._redo:
            raise IndexError("nothing to redo")
        cmd = self._redo.pop()
        cmd.apply(state)
        _log.info("cmd.redo", cmd=type(cmd).__name__, desc=cmd.description)
        self._undo.append(cmd)
        if self._recorder is not None:
            self._recorder.record_redo()

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)
