# SPDX-License-Identifier: MIT
"""``QtPlacePreview`` — PlaceTool's ghost + reach overlay as a semantic object.

The qt-ports §3 pattern: the tool states *what the user sees* — the ghost
body under the cursor, the green reachable-pad dots, the dashed pin→cursor
feedback line — and this object owns the ``QGraphicsItem`` mechanism.  The
ghost's *content* (definition, rotation, offsets, variant, theme) is tool
policy and travels through :meth:`show_ghost`'s arguments; only the baking
into items (``make_ghost_item``) is mechanism.

The ``PlacePreview`` Protocol PlaceTool depends on lives in
:mod:`protoboard.controller.tools.place_preview` (Qt-free); re-exported here
for existing GUI importers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from protoboard.controller.tools.place_preview import PlacePreview as PlacePreview

if TYPE_CHECKING:
    from collections.abc import Callable

    from PySide6.QtWidgets import (
        QGraphicsItem,
        QGraphicsItemGroup,
        QGraphicsScene,
    )

    from protoboard.controller.view_options.theme import Theme
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.geometry import Point, Vector

_REACH_PAD_RADIUS = 0.18


class QtPlacePreview:
    """PlacePreview over ``QGraphicsScene`` items (built by PlaceTool)."""

    def __init__(
        self,
        scene: Callable[[], QGraphicsScene],
        theme: Callable[[], Theme],
    ) -> None:
        self._scene = scene
        self._theme = theme
        self._ghost: QGraphicsItemGroup | None = None
        self._reach_dots: list[QGraphicsItem] = []
        self._reach_line: QGraphicsItem | None = None

    @property
    def ghost_item(self) -> QGraphicsItemGroup | None:
        """The live ghost item group (geometry assertions in tests)."""
        return self._ghost

    # -- ghost -----------------------------------------------------------------

    def show_ghost(
        self,
        defn: ComponentDefinition,
        *,
        rotation: int,
        pin_offsets: dict[int, Vector],
        package_variant: str | None,
        theme: Theme,
    ) -> None:
        from protoboard.services.rendering.component_renderer import make_ghost_item

        self.clear_ghost()
        ghost = make_ghost_item(
            defn,
            rotation=rotation,
            theme=theme,
            pin_offsets=pin_offsets,
            package_variant=package_variant,
        )
        # Z=20 puts the ghost above all back-side scene items (Z<Z_BOARD=3.0)
        # so BoardView._ensure_back_cache doesn't bake it into the back-side
        # pixmap and hide it.  Without this the ghost stays "stuck" at (0,0)
        # because setVisible(False) is applied to the back-cached item and
        # setPos() updates pose on an invisible object.
        ghost.setZValue(20.0)
        self._scene().addItem(ghost)
        self._ghost = ghost

    def move_ghost(self, pos: Point) -> None:
        if self._ghost is not None:
            self._ghost.setPos(pos.x, pos.y)
            # setPos already schedules a repaint via Qt's
            # FullViewportUpdate mode; no explicit update() needed once
            # the back-cache filter no longer hides the ghost's children.

    def clear_ghost(self) -> None:
        if self._ghost is not None:
            self._scene().removeItem(self._ghost)
            self._ghost = None

    @property
    def has_ghost(self) -> bool:
        return self._ghost is not None

    # -- reach overlay ------------------------------------------------------------

    def show_reach_dots(self, points: list[Point]) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QBrush, QColor, QPen
        from PySide6.QtWidgets import QGraphicsEllipseItem

        self._clear_reach_dots()
        r = _REACH_PAD_RADIUS
        color = QColor(self._theme().placement_reach_color)
        for pt in points:
            item = QGraphicsEllipseItem(pt.x - r, pt.y - r, 2 * r, 2 * r)
            item.setBrush(QBrush(color))
            item.setPen(QPen(Qt.GlobalColor.transparent, 0))
            item.setZValue(20.0)
            self._scene().addItem(item)
            self._reach_dots.append(item)

    def show_reach_line(self, a: Point, b: Point) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor, QPen
        from PySide6.QtWidgets import QGraphicsLineItem

        self.clear_reach_line()
        line = QGraphicsLineItem(a.x, a.y, b.x, b.y)
        pen = QPen(QColor(self._theme().placement_reach_line_color), 0.05)
        pen.setStyle(Qt.PenStyle.DashLine)
        line.setPen(pen)
        line.setZValue(20.0)
        self._scene().addItem(line)
        self._reach_line = line

    def clear_reach_line(self) -> None:
        if self._reach_line is not None:
            self._scene().removeItem(self._reach_line)
            self._reach_line = None

    def clear_reach(self) -> None:
        self._clear_reach_dots()
        self.clear_reach_line()

    def _clear_reach_dots(self) -> None:
        for item in self._reach_dots:
            self._scene().removeItem(item)
        self._reach_dots = []

    # -- lifecycle ---------------------------------------------------------------

    def on_scene_cleared(self) -> None:
        self._ghost = None
        self._reach_dots = []
        self._reach_line = None
