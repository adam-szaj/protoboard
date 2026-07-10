# SPDX-License-Identifier: MIT
"""Qt-free ports of the board-canvas seam (docs/qt-ports.md §2).

Pure Python + ``protoboard.geometry`` imports only — the same mechanical
rule that protects ``domain/`` protects this package.
"""

from protoboard.gui.ports.inputs import (
    LEFT_BUTTON,
    MIDDLE_BUTTON,
    RIGHT_BUTTON,
    Intent,
    KeyInput,
    Mods,
    PointerInput,
    WheelInput,
)
from protoboard.gui.ports.executor import BlockingJob, ExecutorPort
from protoboard.gui.ports.help_overlay import HelpOverlayPort
from protoboard.gui.ports.hover import HoverEvent
from protoboard.gui.ports.interaction_context import (
    ActionOption,
    InteractionContext,
)
from protoboard.gui.ports.status_event import StatusEvent
from protoboard.gui.ports.tool_controller import ToolController
from protoboard.gui.ports.viewport import (
    CursorKind,
    ScreenMetrics,
    ViewportPort,
)

__all__ = [
    "ActionOption",
    "BlockingJob",
    "CursorKind",
    "ExecutorPort",
    "HelpOverlayPort",
    "HoverEvent",
    "InteractionContext",
    "ToolController",
    "ScreenMetrics",
    "ViewportPort",
    "Intent",
    "KeyInput",
    "LEFT_BUTTON",
    "MIDDLE_BUTTON",
    "Mods",
    "PointerInput",
    "RIGHT_BUTTON",
    "StatusEvent",
    "WheelInput",
]
