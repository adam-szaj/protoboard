# SPDX-License-Identifier: MIT
"""``SimplifyPreview`` — the structural seam for SimplifyTool's draft overlay.

The Qt-free ``Protocol`` SimplifyTool depends on.  The Qt implementation
(``QtSimplifyPreview``, owning the dashed-polyline ``QGraphicsItem``
mechanism) lives in :mod:`protoboard.gui.tools.simplify_preview` — see the
qt-ports §3 pattern described there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from protoboard.geometry import Point


class SimplifyPreview(Protocol):
    """The dashed draft polyline SimplifyTool shows before applying."""

    def show_draft(self, path: list[Point]) -> None:
        """Draw the drafted path as a dashed overlay (replaces the previous)."""
        ...

    def clear_draft(self) -> None: ...

    def on_scene_cleared(self) -> None:
        """Drop stale item refs after ``scene.clear()`` (C++ objects gone)."""
        ...
