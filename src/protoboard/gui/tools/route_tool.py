# SPDX-License-Identifier: MIT
"""Route wire tool — click-click-...-Enter/Escape to finish a multi-segment wire."""

from __future__ import annotations

import uuid

import structlog
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPen
from PySide6.QtWidgets import QGraphicsLineItem

from protoboard.domain.commands.component_cmds import AddWireWithNetsCommand
from protoboard.domain.connectivity.graph import ConnectivityGraph
from protoboard.domain.layout.placement import pin_world_position
from protoboard.domain.layout.wire import WireSegment
from protoboard.geometry import Point
from protoboard.gui.board_view import _Tool
from protoboard.gui.renderers.routing_overlay import render_routing_hints
from protoboard.services.routing.astar import find_paths
from protoboard.services.routing.cost_model import RoutingCostModel

_log = structlog.get_logger(__name__)
_PREVIEW_COLOR = QColor(255, 100, 0, 180)
_PREVIEW_WIDTH = 0.12


class RouteTool(_Tool):
    """Multi-segment wire routing: click to add points, Enter/Esc to commit."""

    def __init__(
        self,
        insulated: bool = True,
        side: str = "top",
    ) -> None:
        self._insulated = insulated
        self._side = side
        self._points: list[Point] = []
        # Committed segments (between clicked points).
        self._committed_lines: list[QGraphicsLineItem] = []
        # Single live segment from last point to cursor — None when not active.
        self._live_line: QGraphicsLineItem | None = None
        self._hint_items: list[QGraphicsLineItem] = []

    def deactivate(self) -> None:
        self._cancel()

    def on_press(self, event: QMouseEvent, scene_pt: QPointF) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        if self._view.is_locked_pin(scene_pt):
            sb = self._view.window().statusBar()
            if sb is not None:
                sb.showMessage("Pad należy do grupy — wyeksportuj go najpierw (prawy klik → Export pin)", 3000)
            return True  # consume event, block routing
        snapped = self._view.snap(scene_pt)
        if len(self._points) == 1:
            self._show_routing_hints(snapped)
        # Turn the live segment into a committed segment.
        if len(self._points) >= 1:
            self._remove_live()
            self._add_committed_line(self._points[-1], snapped)
        self._points.append(snapped)
        if len(self._points) == 1:
            _log.debug("route.started", start=(snapped.x, snapped.y), insulated=self._insulated, side=self._side)
        # Immediately show live segment from new point to current cursor.
        self._add_live_line(snapped, snapped)
        return True

    def on_move(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        if not self._points:
            return
        self._remove_live()
        snapped = self._view.snap(scene_pt)
        self._add_live_line(self._points[-1], snapped)

    def on_key(self, event: QKeyEvent) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._commit()
        elif key == Qt.Key.Key_Escape:
            self._cancel()
        elif key == Qt.Key.Key_I:
            self._insulated = not self._insulated
            self._notify_status()
        elif key == Qt.Key.Key_T:
            self._side = "top"
            self._notify_status()
        elif key == Qt.Key.Key_B:
            self._side = "bottom"
            self._notify_status()

    def _notify_status(self) -> None:
        """Update the main window status bar with current wire options."""
        win = self._view.window()
        updater = getattr(win, "_update_route_status", None)
        if updater is not None:
            updater(self._insulated, self._side)

    # ------------------------------------------------------------------

    def _commit(self) -> None:
        if len(self._points) < 2:
            self._cancel()
            return
        wire = WireSegment(
            id=uuid.uuid4(),
            path=list(self._points),
            insulated=self._insulated,
            side=self._side,  # type: ignore[arg-type]
        )
        from protoboard.gui.wire_net_resolver import resolve_wire_nets

        cmd = AddWireWithNetsCommand(wire=wire)
        if not resolve_wire_nets(
            self._session, wire, cmd, self._view.window()
        ):
            _log.debug("route.cancelled", reason="net_merge_rejected")
            self._cancel()
            return
        self._points.clear()
        self._clear_committed()
        self._remove_live()
        self._clear_hints()
        self._session.stack.execute(cmd, self._session)
        _log.info("route.committed", wire_id=str(wire.id), points=len(wire.path), insulated=wire.insulated)
        self._session.layout_changed.emit()

    def _cancel(self) -> None:
        self._clear_committed()
        self._remove_live()
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
        _log.debug("route.hints", paths_found=len(results), targets=len(targets))
        self._hint_items = render_routing_hints(self._view.scene(), results)

    def on_scene_cleared(self) -> None:
        # scene.clear() already destroyed all items — just drop references.
        self._committed_lines.clear()
        self._live_line = None
        self._hint_items.clear()

    def _clear_hints(self) -> None:
        for item in self._hint_items:
            self._view.scene().removeItem(item)
        self._hint_items.clear()

    def _add_committed_line(self, a: Point, b: Point) -> None:
        pen = QPen(_PREVIEW_COLOR, _PREVIEW_WIDTH)
        line = QGraphicsLineItem(a.x, a.y, b.x, b.y)
        line.setPen(pen)
        self._view.scene().addItem(line)
        self._committed_lines.append(line)

    def _add_live_line(self, a: Point, b: Point) -> None:
        pen = QPen(_PREVIEW_COLOR, _PREVIEW_WIDTH)
        pen.setStyle(Qt.PenStyle.DashLine)
        line = QGraphicsLineItem(a.x, a.y, b.x, b.y)
        line.setPen(pen)
        self._view.scene().addItem(line)
        self._live_line = line

    def _remove_live(self) -> None:
        if self._live_line is not None:
            self._view.scene().removeItem(self._live_line)
            self._live_line = None

    def _clear_committed(self) -> None:
        for item in self._committed_lines:
            self._view.scene().removeItem(item)
        self._committed_lines.clear()
