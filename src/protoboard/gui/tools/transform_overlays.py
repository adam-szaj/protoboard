# SPDX-License-Identifier: MIT
"""Shared transform-preview overlays: the conflict corner + selection outline.

Both the live drag (in :class:`~protoboard.gui.tools.gui_tools.SelectTool`)
and the armed transform (in
:class:`~protoboard.gui.tools.gui_tools.ArmedTransformTool`) show the
same SOFT-amber / HARD-red corner label and recolour the selection outline to
match while a preview decision conflicts.  The logic is identical, so it lives
here once — a small view-bound helper — instead of being duplicated (and drifting)
across the two tools.

Wire-ghosting (dimming the specific wires a drop would drop/disconnect) stays in
SelectTool: it is drag-only and reads drag-specific buckets off the decision.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtGui import QColor

if TYPE_CHECKING:
    from protoboard.domain.layout.drag_analysis import DragAnalysis
    from protoboard.gui.board_view_tool import AbstractBoardView


class TransformOverlays:
    """The conflict corner label + selection-outline override for one view."""

    def __init__(self, view: AbstractBoardView) -> None:
        self._view = view
        self._corner: object | None = None  # lazily-built DragCornerText

    def show_conflict(self, decision: DragAnalysis | None) -> None:
        """Show/clear the corner + outline for a live preview *decision*.

        SOFT amber / HARD red while conflicting; hidden the instant the state is
        CLEAR (or ``None``) so the frame never lingers.
        """
        from protoboard.domain.layout.drag_analysis import DragSeverity

        if decision is None or decision.severity is DragSeverity.CLEAR:
            self.clear()
            return
        amber = QColor(180, 130, 0, 220)
        red = QColor(180, 40, 25, 230)
        color = red if decision.severity is DragSeverity.HARD else amber
        from protoboard.gui.tools.drag_messages import format_drag_reasons

        text = format_drag_reasons(decision.reasons) or (
            "Cannot drop here — overlap  ·  [Esc] cancel"
            if decision.severity is DragSeverity.HARD
            else "Drops/edits some wires on release"
        )
        self._show_corner(text, color)
        # Flip the selection-outline colour to match the conflict severity so the
        # transformed bodies' own highlight signals the drop's consequence.
        self._set_outline(color.name(QColor.NameFormat.HexArgb))

    def clear(self) -> None:
        """Hide the corner and restore the cyan selection outline."""
        from protoboard.gui.tools.drag_corner_text import DragCornerText

        if isinstance(self._corner, DragCornerText):
            self._corner.hide(self._view.scene())
        self._set_outline(None)

    # -- internals ----------------------------------------------------------

    def _show_corner(self, text: str, color: QColor) -> None:
        from protoboard.gui.tools.drag_corner_text import DragCornerText

        if not isinstance(self._corner, DragCornerText):
            self._corner = DragCornerText()
        self._corner.show(self._view.scene(), self._view, text, color)

    def _set_outline(self, color: str | None) -> None:
        """Drive BoardView's selection-outline override (None → back to cyan).

        *color* is hex ``#AARRGGBB`` — the seam's colour format (qt-ports §2.7).
        No-op when the view lacks the setter (headless test stub).
        """
        setter = getattr(self._view, "set_selection_outline_override", None)
        if setter is not None:
            setter(color)
