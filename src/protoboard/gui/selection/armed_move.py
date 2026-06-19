# SPDX-License-Identifier: MIT
"""``ArmedMove`` — keyboard float-move as an :class:`ArmedSubTool`.

A thin adapter over :class:`DragController`: the controller already floats a
selection, classifies the drop, and holds a HARD conflict without committing —
exactly the armed-sub-tool contract.  This wraps it for keyboard driving
(arrows / WASD step the anchor one grid cell via ``nudge_to``) and maps the
protocol's ``accept`` / ``cancel`` onto the controller's CONFIRM / CANCEL.

``accept()`` is conditional by construction: the controller's
``confirm_floating`` only commits when the drop is committable; an un-committable
drop leaves the controller floating, so the sub-tool stays armed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt

from protoboard.geometry import Point

if TYPE_CHECKING:
    from PySide6.QtGui import QKeyEvent, QWheelEvent

    from protoboard.gui.selection.drag_controller import DragController

# Per-key anchor nudge in grid cells (arrows + WASD).
_STEP: dict[int, tuple[float, float]] = {
    int(Qt.Key.Key_Left): (-1.0, 0.0), int(Qt.Key.Key_A): (-1.0, 0.0),
    int(Qt.Key.Key_Right): (1.0, 0.0), int(Qt.Key.Key_D): (1.0, 0.0),
    int(Qt.Key.Key_Up): (0.0, -1.0), int(Qt.Key.Key_W): (0.0, -1.0),
    int(Qt.Key.Key_Down): (0.0, 1.0), int(Qt.Key.Key_S): (0.0, 1.0),
}


class ArmedMove:
    """Keyboard float-move sub-tool wrapping a live :class:`DragController`."""

    def __init__(self, drag: DragController, *, anchor: Point) -> None:
        self._drag = drag
        self._anchor = anchor

    @property
    def finished(self) -> bool:
        return self._drag.finished

    def status_hint(self) -> str:
        return (
            "Move armed  ·  arrows / WASD step one cell  ·  "
            "[Enter] drop  ·  [Esc] cancel"
        )

    def on_key(self, event: QKeyEvent) -> bool:
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.accept()
            return True
        if key == Qt.Key.Key_Escape:
            self.cancel()
            return True
        nudge = _STEP.get(int(key))
        if nudge is not None:
            self._anchor = Point(self._anchor.x + nudge[0], self._anchor.y + nudge[1])
            self._drag.nudge_to(self._anchor)
            return True
        return False

    def wants_wheel(self, event: QWheelEvent) -> bool:
        return False  # keyboard-move leaves the wheel to zoom

    def on_wheel(self, event: QWheelEvent) -> bool:
        return False

    def on_click(self, scene_pt: Point) -> bool:
        # Keyboard-move ignores clicks; the press falls through to selection.
        return False

    def can_commit(self) -> bool:
        decision = self._drag.last_decision
        return decision is None or decision.can_commit

    def accept(self) -> None:
        # Conditional disarm: commit only when the drop is committable (a HARD
        # overlap leaves the controller floating, so the sub-tool stays armed).
        # ``confirm_floating`` applies the move regardless of phase from the last
        # settled decision — keyboard nudges never enter the release-only
        # FLOATING phase, so route through it directly rather than CONFIRM.
        if self.can_commit():
            self._drag.confirm_floating()

    def cancel(self) -> None:
        self._drag.cancel()
