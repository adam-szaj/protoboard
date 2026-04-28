# SPDX-License-Identifier: MIT
"""Route wire tool — click-click-...-Enter/Escape to finish a multi-segment wire."""

from __future__ import annotations

import uuid

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPen
from PySide6.QtWidgets import QGraphicsLineItem

from protoboard.domain.connectivity.graph import ConnectivityGraph
from protoboard.domain.layout.placement import pin_world_position
from protoboard.domain.layout.wire import WireSegment
from protoboard.geometry import Point
from protoboard.gui.board_view import _Tool
from protoboard.gui.renderers.routing_overlay import render_routing_hints
from protoboard.services.routing.astar import find_paths
from protoboard.services.routing.cost_model import RoutingCostModel

_PREVIEW_COLOR = QColor(255, 100, 0, 180)
_PREVIEW_WIDTH = 0.12


class RouteTool(_Tool):
    """Multi-segment wire routing: click to add points, Enter/Esc to commit."""

    def __init__(self, insulated: bool = True) -> None:
        self._insulated = insulated
        self._points: list[Point] = []
        self._preview_lines: list[QGraphicsLineItem] = []
        self._hint_items: list[QGraphicsLineItem] = []

    def deactivate(self) -> None:
        self._cancel()

    def on_press(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        snapped = self._view.snap(scene_pt)
        self._points.append(snapped)
        if len(self._points) == 1:
            self._show_routing_hints(snapped)
        self._update_preview()

    def on_move(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        if not self._points:
            return
        # Draw a live preview from the last committed point to the cursor.
        self._clear_preview_last()
        snapped = self._view.snap(scene_pt)
        last = self._points[-1]
        self._add_preview_line(last, snapped)

    def on_key(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._commit()
        elif event.key() == Qt.Key.Key_Escape:
            self._cancel()

    # ------------------------------------------------------------------

    def _commit(self) -> None:
        if len(self._points) < 2:
            self._cancel()
            return
        wire = WireSegment(
            id=uuid.uuid4(),
            path=list(self._points),
            insulated=self._insulated,
            side="top",
        )
        self._points.clear()
        self._clear_all_preview()
        self._clear_hints()
        self._session.layout.wires.append(wire)
        self._session.layout_changed.emit()

    def _cancel(self) -> None:
        self._clear_all_preview()
        self._clear_hints()
        self._points.clear()

    def _show_routing_hints(self, source: Point) -> None:
        """Compute and render top-3 routing hint paths from source."""
        self._clear_hints()
        session = self._session
        graph = ConnectivityGraph(
            board=session.board,
            layout=session.layout,
            components=session.components,
            instances=session.instances,
        )
        source_node = graph.node_for_pad(source, "top")
        if source_node is None:
            return
        targets: list[Point] = []
        for net in session.schematic.nets.values():
            for pin_ref in net.pins:
                inst = session.instances.get(pin_ref.component_id)
                if inst is None:
                    continue
                placement = session.layout.placements.get(pin_ref.component_id)
                if placement is None:
                    continue
                defn = session.components.get(inst.definition_ref)
                if defn is None:
                    continue
                pin_pos = pin_world_position(
                    defn.footprint, placement, pin_ref.pin_number
                )
                pin_node = graph.node_for_pad(pin_pos, placement.side)
                if pin_node is not None and pin_node == source_node:
                    # This net contains our source — collect the other pads.
                    for other_ref in net.pins:
                        if other_ref == pin_ref:
                            continue
                        other_inst = session.instances.get(other_ref.component_id)
                        if other_inst is None:
                            continue
                        other_pl = session.layout.placements.get(other_ref.component_id)
                        if other_pl is None:
                            continue
                        other_defn = session.components.get(other_inst.definition_ref)
                        if other_defn is None:
                            continue
                        targets.append(
                            pin_world_position(
                                other_defn.footprint, other_pl, other_ref.pin_number
                            )
                        )
                    break
        if not targets:
            return
        results = find_paths(
            graph=graph,
            source=source,
            targets=targets,
            side="top",
            cost_model=RoutingCostModel(),
            k=3,
        )
        self._hint_items = render_routing_hints(self._view.scene(), results)

    def on_scene_cleared(self) -> None:
        # scene.clear() already destroyed all items — just drop the references.
        self._preview_lines.clear()
        self._hint_items.clear()

    def _clear_hints(self) -> None:
        for item in self._hint_items:
            self._view.scene().removeItem(item)
        self._hint_items.clear()

    def _update_preview(self) -> None:
        self._clear_all_preview()
        pts = self._points
        for i in range(len(pts) - 1):
            self._add_preview_line(pts[i], pts[i + 1])

    def _add_preview_line(self, a: Point, b: Point) -> None:
        pen = QPen(_PREVIEW_COLOR, _PREVIEW_WIDTH)
        line = QGraphicsLineItem(a.x, a.y, b.x, b.y)
        line.setPen(pen)
        self._view.scene().addItem(line)
        self._preview_lines.append(line)

    def _clear_preview_last(self) -> None:
        if self._preview_lines:
            item = self._preview_lines.pop()
            self._view.scene().removeItem(item)

    def _clear_all_preview(self) -> None:
        for item in self._preview_lines:
            self._view.scene().removeItem(item)
        self._preview_lines.clear()
