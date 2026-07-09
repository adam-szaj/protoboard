# SPDX-License-Identifier: MIT
"""Corner-anchored text item for live drag feedback.

Renders a short, view-anchored status line (zoom-invariant) in the top-left
of the board view while a drag is in flight or the selection is in the
"floating" placement state.  Re-positioned on view scroll/resize so it stays
glued to the corner.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtGui import QBrush, QColor, QFont, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsRectItem, QGraphicsTextItem

if TYPE_CHECKING:
    from PySide6.QtWidgets import QGraphicsScene

    from protoboard.gui.board_view_tool import AbstractBoardView


_Z = 60.0  # above DragConflictOverlay (50.0)
_MARGIN_PX = 12  # px gap from the top-left corner
_PADDING_PX = 6  # px around the text
_FONT_POINT = 10


class DragCornerText:
    """Manages a small corner-anchored text + background panel.

    Reuses the underlying QGraphicsItems across show() calls — only the text,
    color and position update.  After ``scene.clear()`` the items are gone:
    call :meth:`rerender` from your tool's ``on_scene_cleared`` hook.
    """

    def __init__(self) -> None:
        self._text_item: QGraphicsTextItem | None = None
        self._bg_item: QGraphicsRectItem | None = None
        self._last_text: str = ""
        self._last_color: QColor | None = None

    # ------------------------------------------------------------------

    def show(
        self,
        scene: QGraphicsScene,
        view: AbstractBoardView,
        text: str,
        color: QColor,
    ) -> None:
        """Show *text* (multiline allowed) in the top-left corner of *view*."""
        self._last_text = text
        self._last_color = color

        if self._text_item is None or self._text_item.scene() is not scene:
            self._build(scene)

        assert self._text_item is not None
        assert self._bg_item is not None

        font = QFont()
        font.setPointSize(_FONT_POINT)
        self._text_item.setFont(font)
        self._text_item.setDefaultTextColor(QColor(255, 255, 255))
        self._text_item.setPlainText(text)

        self._bg_item.setBrush(QBrush(color))
        pen = QPen(color.darker(150), 0)
        self._bg_item.setPen(pen)

        self.update_position(view)

    def update_position(self, view: AbstractBoardView) -> None:
        """Re-anchor the text + background to the view's top-left corner."""
        if self._text_item is None or self._bg_item is None:
            return
        from protoboard.geometry import Point

        port = view.viewport_port
        top_left_scene = port.map_view_to_scene(
            Point(float(_MARGIN_PX), float(_MARGIN_PX))
        )
        self._text_item.setPos(top_left_scene.x, top_left_scene.y)

        # Background panel: text bounding rect + padding, also in scene coords.
        # The text item ignores transforms, but its boundingRect is in *its*
        # local coordinate system (pixels). Convert padding via the port.
        text_rect = self._text_item.boundingRect()
        origin_scene = port.map_view_to_scene(Point(0.0, 0.0))
        pad = port.map_view_to_scene(
            Point(float(_PADDING_PX), float(_PADDING_PX))
        )
        pad_x, pad_y = pad.x - origin_scene.x, pad.y - origin_scene.y
        size = port.map_view_to_scene(Point(
            float(round(text_rect.width())), float(round(text_rect.height()))
        ))
        size_x, size_y = size.x - origin_scene.x, size.y - origin_scene.y
        self._bg_item.setRect(
            top_left_scene.x - pad_x,
            top_left_scene.y - pad_y,
            size_x + 2 * pad_x,
            size_y + 2 * pad_y,
        )

    def hide(self, scene: QGraphicsScene) -> None:
        """Remove the corner text from *scene*."""
        for item in (self._text_item, self._bg_item):
            if item is None:
                continue
            try:
                scene.removeItem(item)
            except RuntimeError:
                pass  # C++ object already gone via scene.clear()
        self._text_item = None
        self._bg_item = None
        self._last_text = ""
        self._last_color = None

    def rerender(
        self, scene: QGraphicsScene | None, view: AbstractBoardView | None
    ) -> None:
        """Re-add items after a scene rebuild.  No-op if nothing to show."""
        if not self._last_text or scene is None or view is None:
            return
        self._text_item = None
        self._bg_item = None
        color = self._last_color or QColor(80, 80, 80, 200)
        self.show(scene, view, self._last_text, color)

    @property
    def is_visible(self) -> bool:
        return self._text_item is not None

    # ------------------------------------------------------------------

    def _build(self, scene: QGraphicsScene) -> None:
        bg = QGraphicsRectItem()
        bg.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        bg.setZValue(_Z)
        bg.setOpacity(0.85)
        scene.addItem(bg)
        self._bg_item = bg

        txt = QGraphicsTextItem()
        txt.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        txt.setZValue(_Z + 0.1)
        scene.addItem(txt)
        self._text_item = txt


__all__ = ["DragCornerText"]
