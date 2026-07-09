# SPDX-License-Identifier: MIT
"""``SelectPreview`` — SelectTool's transient overlays as semantic objects.

The qt-ports §3 pattern, applied to the five overlay concepts SelectTool
draws (armed-rotate pivot markers, the rubber band, the re-drag landing
overlay, the wire-edit fragment overlay, the reroute waypoint preview).
Each concept is a small class owning its own ``QGraphicsItem``s; the
``QtSelectPreview`` facade is the single seam the tool talks through, so a
test can inject one recorder.

Policy stays in the tool (which pads are feasible, which vertices are
marked, where the anchor is); only the item mechanism lives here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPen

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from PySide6.QtWidgets import QGraphicsItem, QGraphicsRectItem, QGraphicsScene

    from protoboard.geometry import Point

# -- pens / brushes (mechanism constants, moved verbatim from select_tool) ----

_BAND_PEN_LR = QPen(QColor(0, 200, 255, 180), 0.06)
_BAND_PEN_RL = QPen(QColor(0, 200, 255, 180), 0.06)
_BAND_PEN_RL.setStyle(Qt.PenStyle.DashLine)
_BAND_FILL_LR = QColor(0, 200, 255, 20)
_BAND_FILL_RL = QColor(0, 200, 255, 10)

_WIRE_EDIT_PEN = QPen(QColor(0, 204, 255, 220), 0.42)
_WIRE_EDIT_PEN.setCapStyle(Qt.PenCapStyle.RoundCap)
_WIRE_EDIT_PEN.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
_NODE_EDIT_PEN = QPen(QColor(0, 204, 255, 255), 0.05)
_NODE_EDIT_BRUSH = QColor(0, 204, 255, 220)
_NODE_SELECTED_PEN = QPen(Qt.GlobalColor.red, 0.06)
_NODE_SELECTED_BRUSH = QColor(255, 80, 0, 230)
_EDIT_PREVIEW_PEN = QPen(QColor(255, 120, 0, 200), 0.12)
_EDIT_PREVIEW_PEN.setStyle(Qt.PenStyle.DashLine)
_NODE_RADIUS = 0.13

_REDRAG_PAD_RADIUS = 0.18
_REDRAG_GREEN = QColor(0, 200, 80, 120)


class SelectPreview(Protocol):
    """The transient overlays SelectTool shows (semantic surface)."""

    # armed-rotate pivot markers
    def show_pivot(self, pos: Point, *, shared: bool) -> None:
        """Add one ⊕ pivot marker (shared = bigger/brighter)."""
        ...

    def clear_pivots(self) -> None: ...

    # rubber band
    def show_band(self, a: Point, b: Point, ltr: bool) -> None:
        """Show/update the rubber band (ltr picks the solid-vs-dash style)."""
        ...

    def clear_band(self) -> None: ...

    # re-drag landing overlay
    def show_redrag_pad(self, pt: Point) -> None:
        """Add one green landing-pad dot."""
        ...

    def show_reach_disc(self, centre: Point, radius: float) -> None:
        """Outline the grabbed pin's reach disc."""
        ...

    def show_redrag_live(self, pos: Point, ok: bool) -> None:
        """Validity tint under the cursor: green ring, or red ring + X cross
        (the cross is the non-colour accessibility cue).  Replaces the
        previous marker."""
        ...

    def clear_redrag_live(self) -> None: ...

    def clear_redrag(self) -> None:
        """Drop the pads, the reach disc and the live marker."""
        ...

    # wire-edit fragment overlay
    def show_wire_edit_wire(self, path: list[Point], marked: Collection[int]) -> None:
        """Add one wire's cyan segments + vertex dots (*marked* indices red)."""
        ...

    def clear_wire_edit(self) -> None: ...

    # reroute waypoint preview
    def show_reroute_path(self, points: list[Point]) -> None:
        """Dashed orange polyline through *points* (replaces the previous,
        including the live segment)."""
        ...

    def show_reroute_live(self, a: Point, b: Point) -> None:
        """Live anchor→cursor segment (replaces the previous)."""
        ...

    def clear_reroute_live(self) -> None: ...

    def clear_reroute(self) -> None: ...

    def on_scene_cleared(self) -> None:
        """Drop stale item refs after ``scene.clear()`` (C++ objects gone)."""
        ...


# -- one small class per overlay concept ---------------------------------------


class _PivotMarkers:
    """Armed-rotate ⊕ markers (one per object, or one on the shared pad)."""

    def __init__(self, scene: Callable[[], QGraphicsScene]) -> None:
        self._scene = scene
        self.items: list[QGraphicsItem] = []

    def show(self, pos: Point, *, shared: bool) -> None:
        from PySide6.QtWidgets import QGraphicsEllipseItem, QGraphicsLineItem

        # Shared pivot is bigger + brighter (it governs the whole selection);
        # per-object markers are small.  Orange = "transforming" (distinct from
        # the cyan static-selection outline).
        orange = QColor(255, 150, 0, 230)
        r = 0.34 if shared else 0.18
        pen = QPen(orange, 0.06 if shared else 0.04)
        ring = QGraphicsEllipseItem(pos.x - r, pos.y - r, 2 * r, 2 * r)
        ring.setPen(pen)
        ring.setZValue(21.0)
        self._scene().addItem(ring)
        self.items.append(ring)
        for dx, dy in ((-r, 0.0), (r, 0.0), (0.0, -r), (0.0, r)):
            cross = QGraphicsLineItem(pos.x, pos.y, pos.x + dx, pos.y + dy)
            cross.setPen(pen)
            cross.setZValue(21.0)
            self._scene().addItem(cross)
            self.items.append(cross)

    def clear(self) -> None:
        for item in self.items:
            try:
                self._scene().removeItem(item)
            except RuntimeError:
                pass
        self.items = []


class _RubberBandRect:
    """The area-select rectangle (solid LR, dashed RL)."""

    def __init__(self, scene: Callable[[], QGraphicsScene]) -> None:
        self._scene = scene
        self.item: QGraphicsRectItem | None = None

    def show(self, a: Point, b: Point, ltr: bool) -> None:
        from PySide6.QtCore import QRectF
        from PySide6.QtWidgets import QGraphicsRectItem

        x0, y0 = min(a.x, b.x), min(a.y, b.y)
        rect = QRectF(x0, y0, abs(b.x - a.x), abs(b.y - a.y))
        if self.item is None:
            item = QGraphicsRectItem(rect)
            item.setPen(_BAND_PEN_LR if ltr else _BAND_PEN_RL)
            item.setBrush(_BAND_FILL_LR if ltr else _BAND_FILL_RL)
            self._scene().addItem(item)
            self.item = item
        else:
            self.item.setRect(rect)
            self.item.setPen(_BAND_PEN_LR if ltr else _BAND_PEN_RL)
            self.item.setBrush(_BAND_FILL_LR if ltr else _BAND_FILL_RL)

    def clear(self) -> None:
        if self.item is not None:
            try:
                self._scene().removeItem(self.item)
            except RuntimeError:
                pass
            self.item = None


class _RedragOverlay:
    """Legal landing pads + reach disc + the live validity marker."""

    def __init__(self, scene: Callable[[], QGraphicsScene]) -> None:
        self._scene = scene
        self.items: list[QGraphicsItem] = []
        self.live: QGraphicsItem | None = None

    def show_pad(self, pt: Point) -> None:
        from PySide6.QtGui import QBrush
        from PySide6.QtWidgets import QGraphicsEllipseItem

        r = _REDRAG_PAD_RADIUS
        item = QGraphicsEllipseItem(pt.x - r, pt.y - r, 2 * r, 2 * r)
        item.setBrush(QBrush(_REDRAG_GREEN))
        item.setPen(QPen(Qt.GlobalColor.transparent, 0))
        item.setZValue(20.0)
        self._scene().addItem(item)
        self.items.append(item)

    def show_disc(self, centre: Point, radius: float) -> None:
        from PySide6.QtGui import QBrush
        from PySide6.QtWidgets import QGraphicsEllipseItem

        ring = QGraphicsEllipseItem(
            centre.x - radius, centre.y - radius, 2 * radius, 2 * radius
        )
        ring.setBrush(QBrush(Qt.GlobalColor.transparent))
        ring.setPen(QPen(QColor(0, 200, 80, 160), 0))
        ring.setZValue(19.5)
        self._scene().addItem(ring)
        self.items.append(ring)

    def show_live(self, pos: Point, ok: bool) -> None:
        from PySide6.QtGui import QBrush
        from PySide6.QtWidgets import (
            QGraphicsEllipseItem,
            QGraphicsItemGroup,
            QGraphicsLineItem,
        )

        self.clear_live()
        r = 0.28
        group = QGraphicsItemGroup()
        colour = QColor(0, 200, 80, 200) if ok else QColor(220, 40, 40, 200)
        ring = QGraphicsEllipseItem(pos.x - r, pos.y - r, 2 * r, 2 * r)
        ring.setBrush(QBrush(Qt.GlobalColor.transparent))
        ring.setPen(QPen(colour, 0))
        group.addToGroup(ring)
        if not ok:
            # Non-colour cue: an "X" through the ring marks "no drop here".
            c = r * 0.6
            for x1, y1, x2, y2 in (
                (pos.x - c, pos.y - c, pos.x + c, pos.y + c),
                (pos.x - c, pos.y + c, pos.x + c, pos.y - c),
            ):
                ln = QGraphicsLineItem(x1, y1, x2, y2)
                ln.setPen(QPen(colour, 0))
                group.addToGroup(ln)
        group.setZValue(21.0)
        self._scene().addItem(group)
        self.live = group

    def clear_live(self) -> None:
        if self.live is not None:
            try:
                self._scene().removeItem(self.live)
            except RuntimeError:
                pass
            self.live = None

    def clear(self) -> None:
        self.clear_live()
        for item in self.items:
            try:
                self._scene().removeItem(item)
            except RuntimeError:
                pass
        self.items = []


class _WireEditOverlay:
    """Cyan wire segments + vertex dots (marked range endpoints red)."""

    def __init__(self, scene: Callable[[], QGraphicsScene]) -> None:
        self._scene = scene
        self.items: list[QGraphicsItem] = []

    def show_wire(self, path: list[Point], marked: Collection[int]) -> None:
        from PySide6.QtGui import QBrush
        from PySide6.QtWidgets import QGraphicsEllipseItem, QGraphicsLineItem

        pts = path
        for i in range(len(pts) - 1):
            seg = QGraphicsLineItem(pts[i].x, pts[i].y, pts[i + 1].x, pts[i + 1].y)
            seg.setPen(_WIRE_EDIT_PEN)
            seg.setZValue(9.0)
            self._scene().addItem(seg)
            self.items.append(seg)
        for i, pt in enumerate(pts):
            r = _NODE_RADIUS
            dot = QGraphicsEllipseItem(pt.x - r, pt.y - r, 2 * r, 2 * r)
            if i in marked:
                dot.setPen(_NODE_SELECTED_PEN)
                dot.setBrush(QBrush(_NODE_SELECTED_BRUSH))
            else:
                dot.setPen(_NODE_EDIT_PEN)
                dot.setBrush(QBrush(_NODE_EDIT_BRUSH))
            dot.setZValue(10.0)
            self._scene().addItem(dot)
            self.items.append(dot)

    def clear(self) -> None:
        for item in self.items:
            try:
                self._scene().removeItem(item)
            except RuntimeError:
                pass
        self.items = []


class _RerouteOverlay:
    """Dashed orange waypoint polyline + the live cursor segment."""

    def __init__(self, scene: Callable[[], QGraphicsScene]) -> None:
        self._scene = scene
        self.items: list[QGraphicsItem] = []
        self.live: QGraphicsItem | None = None

    def show_path(self, points: list[Point]) -> None:
        from PySide6.QtWidgets import QGraphicsLineItem

        self.clear()
        for a, b in zip(points, points[1:]):
            line = QGraphicsLineItem(a.x, a.y, b.x, b.y)
            line.setPen(_EDIT_PREVIEW_PEN)
            line.setZValue(20.0)
            self._scene().addItem(line)
            self.items.append(line)

    def show_live(self, a: Point, b: Point) -> None:
        from PySide6.QtWidgets import QGraphicsLineItem

        self.clear_live()
        line = QGraphicsLineItem(a.x, a.y, b.x, b.y)
        line.setPen(_EDIT_PREVIEW_PEN)
        line.setZValue(20.0)
        self._scene().addItem(line)
        self.live = line

    def clear_live(self) -> None:
        if self.live is not None:
            try:
                self._scene().removeItem(self.live)
            except RuntimeError:
                pass
            self.live = None

    def clear(self) -> None:
        self.clear_live()
        for item in self.items:
            try:
                self._scene().removeItem(item)
            except RuntimeError:
                pass
        self.items = []


class QtSelectPreview:
    """SelectPreview facade over the five per-concept overlay objects."""

    def __init__(self, scene: Callable[[], QGraphicsScene]) -> None:
        self.pivots = _PivotMarkers(scene)
        self.band = _RubberBandRect(scene)
        self.redrag = _RedragOverlay(scene)
        self.wire_edit = _WireEditOverlay(scene)
        self.reroute = _RerouteOverlay(scene)

    # armed-rotate pivots
    def show_pivot(self, pos: Point, *, shared: bool) -> None:
        self.pivots.show(pos, shared=shared)

    def clear_pivots(self) -> None:
        self.pivots.clear()

    # rubber band
    def show_band(self, a: Point, b: Point, ltr: bool) -> None:
        self.band.show(a, b, ltr)

    def clear_band(self) -> None:
        self.band.clear()

    # re-drag
    def show_redrag_pad(self, pt: Point) -> None:
        self.redrag.show_pad(pt)

    def show_reach_disc(self, centre: Point, radius: float) -> None:
        self.redrag.show_disc(centre, radius)

    def show_redrag_live(self, pos: Point, ok: bool) -> None:
        self.redrag.show_live(pos, ok)

    def clear_redrag_live(self) -> None:
        self.redrag.clear_live()

    def clear_redrag(self) -> None:
        self.redrag.clear()

    # wire-edit
    def show_wire_edit_wire(self, path: list[Point], marked: Collection[int]) -> None:
        self.wire_edit.show_wire(path, marked)

    def clear_wire_edit(self) -> None:
        self.wire_edit.clear()

    # reroute
    def show_reroute_path(self, points: list[Point]) -> None:
        self.reroute.show_path(points)

    def show_reroute_live(self, a: Point, b: Point) -> None:
        self.reroute.show_live(a, b)

    def clear_reroute_live(self) -> None:
        self.reroute.clear_live()

    def clear_reroute(self) -> None:
        self.reroute.clear()

    def on_scene_cleared(self) -> None:
        """Drop refs the way the tool historically did.

        Parity note: the re-drag overlay refs are deliberately NOT dropped —
        the legacy tool kept them across ``scene.clear()`` and relied on the
        guarded ``removeItem`` (RuntimeError → pass) for the freed items.
        """
        self.band.item = None
        self.wire_edit.items = []
        self.reroute.items = []
        self.reroute.live = None
        self.pivots.items = []
