# SPDX-License-Identifier: MIT
"""``QtViewport`` — the Qt adapter of :class:`ViewportPort` (qt-ports §2.1).

The **only** place that converts between the port's geometry vocabulary and
``QPointF`` / ``QTransform`` / scrollbars / cursors.  Everything above it
(ViewCamera policy, tools) speaks ``protoboard.geometry`` types; the
Qt-vs-fake property test pins this adapter to the pure
:class:`~protoboard.gui.ports.viewport_details.CameraMath` model.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QPoint, Qt

from protoboard.geometry import Point, Rect, Size, Transform2D, Vector
from protoboard.gui.ports.viewport import CursorKind, ScreenMetrics
from protoboard.gui.qt_geometry import (
    from_qpointf,
    from_qtransform,
    to_qpointf,
    to_qrectf,
    to_qtransform,
)

if TYPE_CHECKING:
    from PySide6.QtWidgets import QGraphicsView

_CURSORS = {
    CursorKind.ARROW: Qt.CursorShape.ArrowCursor,
    CursorKind.CLOSED_HAND: Qt.CursorShape.ClosedHandCursor,
}


class QtViewport:
    """ViewportPort over a live ``QGraphicsView``."""

    def __init__(self, view: QGraphicsView) -> None:
        self._view = view

    # -- mapping -------------------------------------------------------------

    def map_view_to_scene(self, view_pt: Point) -> Point:
        # Viewport coordinates are integer pixels in Qt's overload set.
        qp = QPoint(round(view_pt.x), round(view_pt.y))
        return from_qpointf(self._view.mapToScene(qp))

    def map_scene_to_view(self, scene_pt: Point) -> Point:
        qp = self._view.mapFromScene(to_qpointf(scene_pt))
        return Point(float(qp.x()), float(qp.y()))

    def view_scale(self) -> float:
        return abs(self._view.transform().m11())

    def view_transform(self) -> Transform2D:
        return from_qtransform(self._view.transform())

    def set_view_transform(self, t: Transform2D) -> None:
        self._view.setTransform(to_qtransform(t))

    # -- framing / scroll ------------------------------------------------------

    def viewport_rect(self) -> Rect:
        vp = self._view.viewport().rect()
        return Rect(0.0, 0.0, float(vp.width()), float(vp.height()))

    def visible_scene_rect(self) -> Rect:
        polygon = self._view.mapToScene(self._view.viewport().rect())
        r = polygon.boundingRect()
        return Rect(r.x(), r.y(), r.width(), r.height())

    def scroll_position(self) -> Vector:
        return Vector(
            float(self._view.horizontalScrollBar().value()),
            float(self._view.verticalScrollBar().value()),
        )

    def set_scroll_position(self, v: Vector) -> None:
        self._view.horizontalScrollBar().setValue(round(v.dx))
        self._view.verticalScrollBar().setValue(round(v.dy))

    def pan_by(self, view_delta: Vector) -> None:
        hbar = self._view.horizontalScrollBar()
        vbar = self._view.verticalScrollBar()
        hbar.setValue(hbar.value() + round(view_delta.dx))
        vbar.setValue(vbar.value() + round(view_delta.dy))

    def center_on(self, scene_pt: Point) -> None:
        self._view.centerOn(to_qpointf(scene_pt))

    def fit_in_view(self, scene_rect: Rect) -> None:
        self._view.fitInView(
            to_qrectf(scene_rect), Qt.AspectRatioMode.KeepAspectRatio
        )

    # -- output --------------------------------------------------------------

    def request_repaint(self) -> None:
        self._view.viewport().update()

    def set_cursor(self, cursor: CursorKind) -> None:
        self._view.setCursor(_CURSORS[cursor])

    def screen_metrics(self) -> ScreenMetrics | None:
        screen = self._view.screen() or (
            self._view.window().screen() if self._view.window() else None
        )
        if screen is None:
            return None
        size = screen.size()
        return ScreenMetrics(
            size_px=Size(float(size.width()), float(size.height())),
            dpi=float(screen.physicalDotsPerInch() or 96.0),
        )
