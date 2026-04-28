# SPDX-License-Identifier: MIT
"""Place component tool — click to place the selected component."""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent

from protoboard.domain.commands.component_cmds import PlaceComponentCommand
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.gui.board_view import _Tool


class PlaceTool(_Tool):
    """Places a component at the snapped cursor position on left-click."""

    def __init__(self, definition_ref: str) -> None:
        self._definition_ref = definition_ref
        self._rotation = 0

    def on_press(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
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
        self._session.instances[inst.id] = inst
        cmd = PlaceComponentCommand(component_id=inst.id, placement=placement)
        self._session.stack.execute(cmd, self._session.layout)
        self._session.layout_changed.emit()
        self._view.refresh()

    def on_key(self, event: object) -> None:
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtGui import QKeyEvent

        if not isinstance(event, QKeyEvent):
            return
        if event.key() == _Qt.Key.Key_R:
            self._rotation = (self._rotation + 90) % 360

    def _next_refdes(self) -> str:
        prefix = self._definition_ref[0].upper()
        existing = [
            inst.refdes
            for inst in self._session.instances.values()
            if inst.refdes.startswith(prefix)
        ]
        return f"{prefix}{len(existing) + 1}"
