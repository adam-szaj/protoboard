# SPDX-License-Identifier: MIT
"""Qt-free ports of the board-canvas seam (docs/qt-ports.md §2).

Pure Python + ``protoboard.geometry`` imports only — the same mechanical
rule that protects ``domain/`` protects this package.
"""

from protoboard.gui.ports.status_event import StatusEvent

__all__ = ["StatusEvent"]
