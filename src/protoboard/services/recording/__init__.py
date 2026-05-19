# SPDX-License-Identifier: MIT
"""Command session recording & replay.

The recorder appends every command executed/undone/redone through the
session's :class:`CommandStack` to a YAML log alongside the project
(``<project>.commands.yaml``).  The same log can then be replayed in a
headless test harness via :func:`replay_commands`.
"""

from protoboard.services.recording.headless import ReplayState
from protoboard.services.recording.recorder import CommandRecorder
from protoboard.services.recording.replay import (
    ReplayResult,
    load_recording,
    replay_commands,
)
from protoboard.services.recording.serialization import (
    UnknownCommandError,
    deserialize_command,
    serialize_command,
)

__all__ = [
    "CommandRecorder",
    "ReplayResult",
    "ReplayState",
    "UnknownCommandError",
    "deserialize_command",
    "load_recording",
    "replay_commands",
    "serialize_command",
]
