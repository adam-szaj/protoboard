# SPDX-License-Identifier: MIT
"""Replay a recorded command log against a session-shaped state object.

The replay is intentionally minimal: it pushes every recorded ``execute``
through a fresh CommandStack and replays ``undo`` / ``redo`` events
against that same stack.  No GUI or signals are involved — the harness is
suitable for headless tests and CLI tooling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from ruamel.yaml import YAML

from protoboard.domain.commands.base import Command, CommandStack
from protoboard.errors import SerializationError
from protoboard.services.recording.serialization import deserialize_command

_log = structlog.get_logger(__name__)

_yaml = YAML(typ="safe")


@dataclass
class ReplayResult:
    """Outcome of a replay run.

    ``executed`` counts ``execute`` events that were successfully applied;
    ``undone`` / ``redone`` count the corresponding navigation events; and
    ``skipped`` records any events that failed (with their reason) so the
    caller can surface them without the whole replay aborting.
    """

    executed: int = 0
    undone: int = 0
    redone: int = 0
    skipped: list[tuple[int, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.executed + self.undone + self.redone


def load_recording(path: Path) -> list[dict[str, Any]]:
    """Read a ``.commands.yaml`` file and return its raw event list."""
    text = Path(path).read_text(encoding="utf-8")
    data = _yaml.load(text) or {}
    if not isinstance(data, dict):
        raise SerializationError(f"{path}: top-level YAML must be a mapping")
    events = data.get("events") or []
    if not isinstance(events, list):
        raise SerializationError(f"{path}: 'events' must be a list")
    return events


def replay_commands(
    state: Any,
    events: list[dict[str, Any]] | Path,
    *,
    stop_on_error: bool = False,
) -> ReplayResult:
    """Apply *events* (or load them from *path*) to *state*.

    *state* must implement the union of state protocols used by the recorded
    commands — in practice, an :class:`EditorSession` always satisfies this.
    A fresh :class:`CommandStack` is created internally; the session's own
    stack is left untouched.  When ``stop_on_error=True`` the first failing
    event raises; otherwise the failure is appended to
    :attr:`ReplayResult.skipped` and the replay continues.
    """
    if isinstance(events, (str, Path)):
        events = load_recording(Path(events))

    stack: CommandStack[Any] = CommandStack()
    result = ReplayResult()

    for idx, evt in enumerate(events):
        kind = evt.get("kind")
        try:
            if kind == "execute":
                cmd_record = evt.get("command")
                if not isinstance(cmd_record, dict):
                    raise SerializationError(
                        f"event {idx}: 'command' missing or not a mapping"
                    )
                cmd: Command[Any] = deserialize_command(cmd_record)
                stack.execute(cmd, state)
                result.executed += 1
            elif kind == "undo":
                stack.undo(state)
                result.undone += 1
            elif kind == "redo":
                stack.redo(state)
                result.redone += 1
            else:
                raise SerializationError(f"event {idx}: unknown kind {kind!r}")
        except Exception as exc:  # noqa: BLE001
            msg = f"{type(exc).__name__}: {exc}"
            _log.warning("replay.event_failed", idx=idx, kind=kind, error=msg)
            if stop_on_error:
                raise
            result.skipped.append((idx, msg))

    _log.info(
        "replay.done",
        executed=result.executed,
        undone=result.undone,
        redone=result.redone,
        skipped=len(result.skipped),
    )
    return result


__all__ = ["ReplayResult", "load_recording", "replay_commands"]
