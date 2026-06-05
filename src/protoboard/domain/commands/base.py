# SPDX-License-Identifier: MIT
"""Command base class and generic CommandStack."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any, ClassVar, Generic, Protocol, TypeVar

import structlog

from protoboard.errors import ProtoboardError

_log = structlog.get_logger(__name__)

T = TypeVar("T")


class CommandRejectedError(ProtoboardError):
    """Raised when CommandStack refuses to execute a command.

    Currently used to reject layout-mutating commands while a background
    autoroute worker is running (the in-flight RouteRequest was built
    from a layout snapshot; applying a hard-mutate command mid-flight
    would silently make the autoroute result land on top of an unrelated
    edit and confuse the undo stack).
    """


class _RecorderProtocol(Protocol):
    """Subset of CommandRecorder used by CommandStack — kept tiny to avoid a
    hard import dependency on the services layer from domain code."""

    def record_execute(self, cmd: "Command[Any]") -> None: ...
    def record_undo(self) -> None: ...
    def record_redo(self) -> None: ...


class Command(ABC, Generic[T]):
    description: str = ""

    # When False, ``CommandStack.execute`` refuses to run this command
    # while ``session.autoroute_active`` is True.  Override on commands
    # that are safe to run mid-autoroute — those that don't mutate the
    # routing snapshot inputs (layout placements, wires, pin↔net
    # assignments, board geometry).  Examples: rename net, set net
    # colour, set net global flag.
    allowed_during_autoroute: ClassVar[bool] = False

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

    @property
    def allowed_during_autoroute(self) -> bool:  # type: ignore[override]
        # Macro is allowed mid-autoroute only when every sub-command is.
        return all(c.allowed_during_autoroute for c in self._commands)

    def apply(self, state: T) -> None:
        for i, cmd in enumerate(self._commands):
            try:
                cmd.apply(state)
            except Exception:
                # Surface where in the macro we died.  MacroCommand has
                # no rollback today, so the sub-commands that already
                # applied have *mutated state* — they are not in the
                # undo stack (the macro will not be appended), and the
                # user is left with a partially-committed layout.  Log
                # the index + already-applied summary so a bug-reporter
                # can correlate with the resulting orphan state.
                _log.error(
                    "macro.apply.failed",
                    macro_desc=self.description,
                    failed_at=i,
                    failed_cmd=type(cmd).__name__,
                    applied_count=i,
                    total=len(self._commands),
                    applied_summary=[
                        type(c).__name__ for c in self._commands[:i]
                    ],
                    pending_summary=[
                        type(c).__name__ for c in self._commands[i + 1:]
                    ],
                )
                raise

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
        # Gate hard-mutate commands while a background autoroute is running.
        # We duck-type ``state.autoroute_active`` (EditorSession exposes it
        # as a property) — for state objects that don't have the attribute
        # (e.g. replay harness) the check is a no-op.
        if getattr(state, "autoroute_active", False) and not cmd.allowed_during_autoroute:
            raise CommandRejectedError(
                f"Command {type(cmd).__name__} blocked while autoroute is running"
            )
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
