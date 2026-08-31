# SPDX-License-Identifier: MIT
"""``TransformOverlaysPort`` — the structural seam for the conflict-corner overlay.

The Qt-free ``Protocol`` for the two methods ``SelectTool``/``ArmedTransformTool``
call on the shared conflict-corner + selection-outline overlay.  The concrete
implementation (``TransformOverlays``, drawing the ``QGraphicsItem``s and
recolouring the selection outline) lives in
:mod:`protoboard.gui.tools.transform_overlays`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from protoboard.domain.layout.drag_analysis import DragAnalysis


class TransformOverlaysPort(Protocol):
    """The conflict corner label + selection-outline override for one view."""

    def show_conflict(self, decision: DragAnalysis | None) -> None:
        """Show/clear the corner + outline for a live preview *decision*."""
        ...

    def clear(self) -> None:
        """Hide the corner and restore the cyan selection outline."""
        ...
