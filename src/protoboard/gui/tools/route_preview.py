# SPDX-License-Identifier: MIT
"""``RoutePreview`` — RouteTool's transient overlay as a semantic object.

The qt-ports §3 pattern (like ``DragConflictOverlay`` / ``DragCornerText``):
the tool states *what the user sees* — committed segments, the live dashed
segment, routing hints, the background dim — and this object owns the
``QGraphicsItem`` mechanism.  A test injects a recorder and asserts the
semantic calls; the tool body stays scene-free.

``on_scene_cleared`` only drops the (already deleted) item references — the
*tool* re-renders from its own state, because the path points are tool
state, not preview state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

    from PySide6.QtWidgets import QGraphicsLineItem, QGraphicsScene

    from protoboard.geometry import Point
    from protoboard.gui.theme import Theme
    from protoboard.services.routing.astar import RoutingResult

_PREVIEW_WIDTH = 0.12


class RoutePreview(Protocol):
    """What RouteTool shows while a wire is being pulled."""

    def add_segment(self, a: Point, b: Point) -> None:
        """Append one committed (clicked) segment to the preview polyline."""
        ...

    def clear_segments(self) -> None: ...

    def show_live(self, a: Point, b: Point) -> None:
        """Show the dashed last-point→cursor segment (replaces the previous)."""
        ...

    def clear_live(self) -> None: ...

    def show_hints(self, results: list[RoutingResult]) -> None:
        """Render the top-K routing hint paths (replaces previous hints)."""
        ...

    def clear_hints(self) -> None: ...

    def show_dim(self) -> None:
        """Dim the whole scene below the routed items (route-active mode)."""
        ...

    def clear_dim(self) -> None: ...

    def on_scene_cleared(self) -> None:
        """Drop stale item refs after ``scene.clear()`` (C++ objects gone)."""
        ...


class QtRoutePreview:
    """RoutePreview over ``QGraphicsScene`` items (built by RouteTool)."""

    def __init__(
        self,
        scene: Callable[[], QGraphicsScene],
        theme: Callable[[], Theme],
    ) -> None:
        self._scene = scene
        self._theme = theme
        self._segments: list[QGraphicsLineItem] = []
        self._live: QGraphicsLineItem | None = None
        self._hints: list[QGraphicsLineItem] = []
        self._dim: object | None = None

    # -- committed segments ---------------------------------------------------

    def add_segment(self, a: Point, b: Point) -> None:
        self._segments.append(self._add_line(a, b, dashed=False))

    def clear_segments(self) -> None:
        for item in self._segments:
            self._scene().removeItem(item)
        self._segments.clear()

    # -- live segment -----------------------------------------------------------

    def show_live(self, a: Point, b: Point) -> None:
        self.clear_live()
        self._live = self._add_line(a, b, dashed=True)

    def clear_live(self) -> None:
        if self._live is not None:
            try:
                self._scene().removeItem(self._live)
            except RuntimeError:
                pass  # C++ object already deleted by scene.clear()
            self._live = None

    # -- routing hints -----------------------------------------------------------

    def show_hints(self, results: list[RoutingResult]) -> None:
        from protoboard.gui.renderers.routing_overlay import render_routing_hints

        self.clear_hints()
        self._hints = render_routing_hints(self._scene(), results, theme=self._theme())

    def clear_hints(self) -> None:
        for item in self._hints:
            self._scene().removeItem(item)
        self._hints = []

    # -- background dim ------------------------------------------------------------

    def show_dim(self) -> None:
        """Semi-transparent dark rectangle covering the whole scene.

        Sits below the components/wires but above the board background
        (z = -0.5 vs board z = -10), so unrelated items dim further while
        the routed items (higher z + bold highlight) remain visible.
        """
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QBrush, QColor
        from PySide6.QtWidgets import QGraphicsRectItem

        if self._dim is not None:
            return
        scene = self._scene()
        if scene is None:
            return
        rect_item = QGraphicsRectItem(scene.sceneRect())
        # 50% black overlay — strong enough to push the background away
        # while the bold routed pads (z >> 0) stay legible above it.
        rect_item.setBrush(QBrush(QColor(0, 0, 0, 128)))
        rect_item.setPen(Qt.PenStyle.NoPen)
        rect_item.setZValue(-0.5)
        scene.addItem(rect_item)
        self._dim = rect_item

    def clear_dim(self) -> None:
        if self._dim is None:
            return
        try:
            self._scene().removeItem(self._dim)  # type: ignore[arg-type]
        except RuntimeError:
            pass  # scene already cleared
        self._dim = None

    # -- lifecycle ---------------------------------------------------------------

    def on_scene_cleared(self) -> None:
        self._segments.clear()
        self._live = None
        self._hints.clear()
        self._dim = None

    # -- helpers -----------------------------------------------------------------

    def _add_line(self, a: Point, b: Point, *, dashed: bool) -> QGraphicsLineItem:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor, QPen
        from PySide6.QtWidgets import QGraphicsLineItem

        pen = QPen(QColor(self._theme().routing_preview_color), _PREVIEW_WIDTH)
        if dashed:
            pen.setStyle(Qt.PenStyle.DashLine)
        line = QGraphicsLineItem(a.x, a.y, b.x, b.y)
        line.setPen(pen)
        self._scene().addItem(line)
        return line
