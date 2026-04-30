# SPDX-License-Identifier: MIT
"""Place component tool — click to place the selected component."""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QGraphicsItemGroup

from protoboard.domain.commands.component_cmds import PlaceComponentCommand
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.gui.board_view import _Tool
from protoboard.gui.renderers.component_renderer import make_ghost_item


class PlaceTool(_Tool):
    """Places a component at the snapped cursor position on left-click."""

    def __init__(self, definition_ref: str) -> None:
        self._definition_ref = definition_ref
        self._rotation = 0
        self._ghost: QGraphicsItemGroup | None = None

    def activate(self, session: object, view: object) -> None:
        super().activate(session, view)  # type: ignore[arg-type]
        self._rebuild_ghost()

    def deactivate(self) -> None:
        self._remove_ghost()

    def on_press(self, event: QMouseEvent, scene_pt: QPointF) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        snapped = self._view.snap(scene_pt)
        inst = ComponentInstance.new(
            definition_ref=self._definition_ref,
            refdes=self._next_refdes(),
            value=self._definition_ref,
        )
        placement = ComponentPlacement(
            component_id=inst.id,
            position=snapped,
            rotation=self._rotation,
            side="top",
        )
        cmd = PlaceComponentCommand(
            component_id=inst.id, placement=placement, instance=inst
        )
        self._session.stack.execute(cmd, self._session)
        self._session.layout_changed.emit()
        return True

    def on_move(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        snapped = self._view.snap(scene_pt)
        self._move_ghost(snapped.x, snapped.y)

    def on_key(self, event: object) -> None:
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtGui import QKeyEvent

        if not isinstance(event, QKeyEvent):
            return
        if event.key() == _Qt.Key.Key_R:
            self._rotation = (self._rotation + 90) % 360
            self._rebuild_ghost()

    def on_scene_cleared(self) -> None:
        self._ghost = None  # scene wiped — item is gone

    # ------------------------------------------------------------------
    # Ghost helpers
    # ------------------------------------------------------------------

    def _rebuild_ghost(self) -> None:
        """Remove old ghost and create a new one with current rotation."""
        self._remove_ghost()
        defn = self._session.components.get(self._definition_ref)
        if defn is None:
            return
        ghost = make_ghost_item(defn, rotation=self._rotation)
        self._view.scene().addItem(ghost)
        self._ghost = ghost

    def _remove_ghost(self) -> None:
        if self._ghost is not None:
            self._view.scene().removeItem(self._ghost)
            self._ghost = None

    def _move_ghost(self, x: float, y: float) -> None:
        if self._ghost is not None:
            self._ghost.setPos(x, y)

    # ------------------------------------------------------------------

    def _next_refdes(self) -> str:
        defn = self._session.components.get(self._definition_ref)
        prefix = (
            defn.refdes_prefix if defn is not None else self._definition_ref[0].upper()
        )
        existing = {
            inst.refdes
            for inst in self._session.instances.values()
            if inst.refdes.startswith(prefix)
        }
        n = 1
        while f"{prefix}{n}" in existing:
            n += 1
        return f"{prefix}{n}"
