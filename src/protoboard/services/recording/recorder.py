# SPDX-License-Identifier: MIT
"""CommandRecorder — append every command event to a YAML log.

Lifecycle is owned by the editor session: ``start(path)`` opens (or creates)
a log file alongside the project; subsequent ``record_execute`` /
``record_undo`` / ``record_redo`` append events.  ``stop()`` flushes and
detaches.

The file uses a simple, append-friendly structure:

    version: 1
    project: my_project_dir_name        # informational only
    events:
      - kind: execute
        command: {type: place_component, ...}
      - kind: undo
      - kind: redo
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import structlog
from ruamel.yaml import YAML

from protoboard.domain.commands.base import Command
from protoboard.services.recording.serialization import (
    UnknownCommandError,
    serialize_command,
)

_log = structlog.get_logger(__name__)

_FORMAT_VERSION = 1


class CommandRecorder:
    """Appends recorded command events to a YAML file.

    The recorder is *opt-in lossy*: if a command's type is not registered for
    serialization the event is dropped (and a warning logged), so an
    incomplete registry never breaks the editor.  Every successful append
    writes the entire file atomically — cheap for the kinds of session
    lengths users actually produce (≤ a few thousand commands) and keeps the
    log readable as a single YAML document.
    """

    def __init__(self) -> None:
        self._path: Path | None = None
        self._events: list[dict[str, Any]] = []
        self._project_label: str = ""
        self._yaml = YAML(typ="safe")
        self._yaml.default_flow_style = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, path: Path, project_label: str = "") -> None:
        """Begin (or resume) recording to *path*.

        Existing logs are not preserved — :func:`start` always begins a fresh
        session.  Callers that wish to keep historical logs should rotate the
        file themselves before calling.
        """
        self._path = Path(path)
        self._events = []
        self._project_label = project_label
        self._flush()
        _log.info("recorder.start", path=str(self._path))

    def stop(self) -> None:
        """Flush and detach.  No-op if not active."""
        if self._path is None:
            return
        self._flush()
        _log.info("recorder.stop", path=str(self._path), events=len(self._events))
        self._path = None
        self._events = []

    @property
    def active(self) -> bool:
        return self._path is not None

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def events(self) -> list[dict[str, Any]]:
        """Read-only view of the in-memory event list (for tests)."""
        return list(self._events)

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_execute(self, cmd: Command[Any]) -> None:
        if not self.active:
            return
        try:
            payload = serialize_command(cmd)
        except UnknownCommandError as exc:
            _log.warning("recorder.skip_unknown", cmd=type(cmd).__name__, error=str(exc))
            return
        self._events.append({"kind": "execute", "command": payload})
        self._flush()

    def record_undo(self) -> None:
        if not self.active:
            return
        self._events.append({"kind": "undo"})
        self._flush()

    def record_redo(self) -> None:
        if not self.active:
            return
        self._events.append({"kind": "redo"})
        self._flush()

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    def _flush(self) -> None:
        if self._path is None:
            return
        data: dict[str, Any] = {
            "version": _FORMAT_VERSION,
            "events": self._events,
        }
        if self._project_label:
            data["project"] = self._project_label
        buf = io.StringIO()
        self._yaml.dump(data, buf)
        self._path.write_text(buf.getvalue(), encoding="utf-8")


__all__ = ["CommandRecorder"]
