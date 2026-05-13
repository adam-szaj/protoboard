# SPDX-License-Identifier: MIT
"""Route wire tool — click-click-...-Enter/Escape to finish a multi-segment wire."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import structlog
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPen
from PySide6.QtWidgets import QApplication, QGraphicsLineItem, QMessageBox

from protoboard.domain.commands.component_cmds import AddWireCommand, AddWireWithNetsCommand
from protoboard.domain.connectivity.graph import ConnectivityGraph
from protoboard.domain.layout.placement import pin_world_position
from protoboard.domain.layout.wire import WireSegment
from protoboard.domain.schematic.wire_net_ops import find_pin_at
from protoboard.geometry import Point
from protoboard.gui.board_view import _Tool
from protoboard.gui.renderers.routing_overlay import render_routing_hints
from protoboard.services.routing.astar import find_paths
from protoboard.services.routing.cost_model import RoutingCostModel

if TYPE_CHECKING:
    pass

_log = structlog.get_logger(__name__)
_PREVIEW_WIDTH = 0.12


class RouteTool(_Tool):
    """Multi-segment wire routing: click to add points, Enter/Esc to commit.

    Two modes:
    - Basic [W]: physical routing only; clicking on a pin of a different/absent
      net triggers a Yes/Cancel dialog.  Intermediate pad collisions truncate
      the path to the last safe node.
    - Net-aware [Shift+W]: same as basic but net assignments are applied
      automatically without dialogs (auto-Yes).  Holding Shift during a click
      or at commit provides a one-shot net-aware override.
    """

    def __init__(
        self,
        insulated: bool = True,
        side: str = "top",
        net_aware: bool = False,
    ) -> None:
        self._insulated = insulated
        self._side = side
        self._net_aware = net_aware
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
        shift_held = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        effective_net_aware = self._net_aware or shift_held
        snapped = self._view.snap(scene_pt)

        # In basic mode, ask for consent when snapping to a pin that would
        # create or modify a net assignment.
        if not effective_net_aware and snapped is not None:
            if not self._consent_to_pin(snapped):
                return True  # blocked — do not add this point

        if len(self._points) == 1:
            self._show_routing_hints(snapped)
        # Turn the live segment into a committed segment.
        if len(self._points) >= 1:
            self._remove_live()
            self._add_committed_line(self._points[-1], snapped, net_aware=effective_net_aware)
        self._points.append(snapped)
        if len(self._points) == 1:
            _log.debug("route.started", start=(snapped.x, snapped.y), insulated=self._insulated, side=self._side)
            self._highlight_start_net(snapped)
        # Immediately show live segment from new point to current cursor.
        self._add_live_line(snapped, snapped, net_aware=effective_net_aware)
        return True

    def on_move(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        if not self._points:
            return
        shift_held = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)
        effective_net_aware = self._net_aware or shift_held
        self._remove_live()
        snapped = self._view.snap(scene_pt)
        self._add_live_line(self._points[-1], snapped, net_aware=effective_net_aware)

    def on_key(self, event: QKeyEvent) -> None:
        key = event.key()
        mods = event.modifiers()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            shift_held = bool(mods & Qt.KeyboardModifier.ShiftModifier)
            self._commit(shift_held=shift_held)
            event.accept()
        elif key == Qt.Key.Key_Escape:
            self._cancel()
            event.accept()
        elif key == Qt.Key.Key_W:
            if bool(mods & Qt.KeyboardModifier.ShiftModifier):
                self._net_aware = True
            else:
                self._net_aware = False
            self._notify_status()
            self._redraw_committed_lines()
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
            updater(self._insulated, self._side, self._net_aware)

    # ------------------------------------------------------------------

    def _consent_to_pin(self, pt: Point) -> bool:
        """Return True if routing may proceed to *pt*.

        If *pt* is on a pin that would require a net assignment change, shows
        a Yes/Cancel dialog.  Returns False if the user cancels.
        """
        pin = find_pin_at(pt, self._session)
        if pin is None:
            return True  # not on any pin — always OK

        from protoboard.domain.schematic.net import PinRef
        pin_ref = PinRef(component_id=pin[0], pin_number=pin[1])
        pin_net = self._session.schematic.net_for_pin(pin_ref)

        # Determine net already implied by points placed so far.
        wire_net = self._net_from_points()

        # none+none: no conflict, allow silently.
        if pin_net is None and wire_net is None:
            return True
        # Same net: no change needed.
        if pin_net is not None and wire_net is not None and pin_net.id == wire_net:
            return True

        # Describe what will happen.
        if pin_net is None and wire_net is not None:
            net_obj = self._session.schematic.nets.get(wire_net)
            net_name = net_obj.name if net_obj else "?"
            msg = f"Ten punkt trafi na pin bez sieci.\nCzy przypisac go do sieci '{net_name}'?"
        elif pin_net is not None and wire_net is None:
            msg = f"Ten punkt trafi na pin sieci '{pin_net.name}'.\nCzy przypisac te siec do drutu?"
        else:
            net_a_obj = self._session.schematic.nets.get(wire_net) if wire_net else None
            na = net_a_obj.name if net_a_obj else "?"
            nb = pin_net.name if pin_net else "?"  # type: ignore[union-attr]
            msg = f"Ten punkt polaczy sieci '{na}' i '{nb}'.\nCzy scalic sieci?"

        win = self._view.window()
        reply = QMessageBox.question(
            win,
            "Połączenie z siecią",
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        return reply == QMessageBox.StandardButton.Yes

    def _net_from_points(self) -> uuid.UUID | None:
        """Return the net UUID implied by already-placed points, or None."""
        from protoboard.domain.schematic.net import PinRef
        for pt in self._points:
            pin = find_pin_at(pt, self._session)
            if pin is None:
                continue
            ref = PinRef(component_id=pin[0], pin_number=pin[1])
            net = self._session.schematic.net_for_pin(ref)
            if net is not None:
                return net.id
        return None

    def _commit(self, shift_held: bool = False) -> None:
        self._view.set_highlighted_net_silent(None)
        if len(self._points) < 2:
            self._cancel()
            return
        effective_net_aware = self._net_aware or shift_held

        from protoboard.domain.layout.wire_utils import (
            board_pads_for_side,
            insert_pad_nodes,
        )
        raw_path = list(self._points)
        pads = board_pads_for_side(self._session.board, self._side)
        enriched_path = insert_pad_nodes(raw_path, pads)

        if not effective_net_aware:
            enriched_path = _trim_at_intermediate_pin_conflict(enriched_path, self._session)
            if len(enriched_path) < 2:
                _log.debug("route.cancelled", reason="path_collapsed_after_trim")
                self._cancel()
                return

        wire = WireSegment(
            id=uuid.uuid4(),
            path=enriched_path,
            insulated=self._insulated,
            side=self._side,  # type: ignore[arg-type]
        )

        if effective_net_aware:
            from protoboard.gui.wire_net_resolver import resolve_wire_nets
            cmd: AddWireWithNetsCommand | AddWireCommand = AddWireWithNetsCommand(wire=wire)
            if not resolve_wire_nets(
                self._session, wire, cmd, self._view.window()  # type: ignore[arg-type]
            ):
                _log.debug("route.cancelled", reason="net_merge_rejected")
                self._cancel()
                return
        else:
            cmd = AddWireCommand(wire=wire)

        self._points.clear()
        self._clear_committed()
        self._remove_live()
        self._clear_hints()
        self._session.stack.execute(cmd, self._session)
        _log.info("route.committed", wire_id=str(wire.id), points=len(wire.path),
                  insulated=wire.insulated, net_aware=effective_net_aware)
        self._session.layout_changed.emit()

    def _cancel(self) -> None:
        # Clear state first so on_scene_cleared() (triggered by refresh below)
        # sees empty _points and does not try to redraw deleted C++ objects.
        self._points.clear()
        self._clear_committed()
        self._remove_live()
        self._clear_hints()
        self._view.set_highlighted_net(None)

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

    def _highlight_start_net(self, source: Point) -> None:
        """Set dim for the net of the starting pad — silent (no refresh)."""
        graph = self._session.connectivity_graph()
        source_node = graph.node_for_pad(source, self._side)
        if source_node is None:
            return
        for net in self._session.schematic.nets.values():
            for ref in net.pins:
                if graph.node_for_pin(ref) == source_node:
                    self._view.set_highlighted_net_silent(net.id)
                    return

    def on_scene_cleared(self) -> None:
        """Called after every refresh() — redraw committed lines from _points."""
        self._committed_lines.clear()
        self._live_line = None   # C++ object deleted by scene.clear(); drop ref
        self._hint_items.clear()

        # Redraw committed segments from stored point sequence.
        if len(self._points) >= 2:
            for i in range(len(self._points) - 1):
                self._add_committed_line(self._points[i], self._points[i + 1])
        # Live line is NOT redrawn here — it will be recreated on the next
        # mouseMoveEvent via on_move() → _add_live_line().

    def _redraw_committed_lines(self) -> None:
        """Redraw committed lines with the current net_aware color."""
        self._clear_committed()
        if len(self._points) >= 2:
            for i in range(len(self._points) - 1):
                self._add_committed_line(self._points[i], self._points[i + 1])

    def _preview_color(self, net_aware: bool) -> str:
        t = self._theme()
        if net_aware:
            return t.routing_net_aware_color
        return t.routing_preview_color

    def _clear_hints(self) -> None:
        for item in self._hint_items:
            self._view.scene().removeItem(item)
        self._hint_items.clear()

    def _add_committed_line(self, a: Point, b: Point, *, net_aware: bool | None = None) -> None:
        if net_aware is None:
            net_aware = self._net_aware
        pen = QPen(QColor(self._preview_color(net_aware)), _PREVIEW_WIDTH)
        line = QGraphicsLineItem(a.x, a.y, b.x, b.y)
        line.setPen(pen)
        self._view.scene().addItem(line)
        self._committed_lines.append(line)

    def _add_live_line(self, a: Point, b: Point, *, net_aware: bool | None = None) -> None:
        if net_aware is None:
            net_aware = self._net_aware
        pen = QPen(QColor(self._preview_color(net_aware)), _PREVIEW_WIDTH)
        pen.setStyle(Qt.PenStyle.DashLine)
        line = QGraphicsLineItem(a.x, a.y, b.x, b.y)
        line.setPen(pen)
        self._view.scene().addItem(line)
        self._live_line = line

    def _remove_live(self) -> None:
        if self._live_line is not None:
            try:
                self._view.scene().removeItem(self._live_line)
            except RuntimeError:
                pass  # C++ object already deleted by scene.clear()
            self._live_line = None

    def _clear_committed(self) -> None:
        for item in self._committed_lines:
            self._view.scene().removeItem(item)
        self._committed_lines.clear()


def _trim_at_intermediate_pin_conflict(
    path: list[Point],
    session: object,
) -> list[Point]:
    """Trim *path* before the first intermediate node that conflicts with a net.

    Intermediate nodes (indices 1..n-2) that land on a pin are checked.  A
    conflict exists when the pin's net differs from the wire's implied net (the
    first net found on any endpoint or earlier node).  none+none is not a
    conflict.  Returns the (possibly shortened) path; always at least the
    original slice if no conflict is found.
    """
    if len(path) <= 2:
        return path

    # Determine the net implied by the two endpoints first.
    from protoboard.domain.schematic.net import PinRef

    def _net_at(pt: Point) -> uuid.UUID | None:
        pin = find_pin_at(pt, session)  # type: ignore[arg-type]
        if pin is None:
            return None
        ref = PinRef(component_id=pin[0], pin_number=pin[1])
        net = session.schematic.net_for_pin(ref)  # type: ignore[attr-defined]
        return net.id if net is not None else None

    # Build the "wire net" from endpoints (same logic as _net_from_points but
    # operating on a path list).
    wire_net: uuid.UUID | None = _net_at(path[0]) or _net_at(path[-1])

    for i in range(1, len(path) - 1):
        pt = path[i]
        pin = find_pin_at(pt, session)  # type: ignore[arg-type]
        if pin is None:
            continue
        ref = PinRef(component_id=pin[0], pin_number=pin[1])
        net_obj = session.schematic.net_for_pin(ref)  # type: ignore[attr-defined]
        pad_net = net_obj.id if net_obj is not None else None

        # none + none → no conflict
        if pad_net is None and wire_net is None:
            continue
        # Same net → no conflict; update wire_net if it was unknown
        if pad_net == wire_net:
            continue
        # wire has net, pad has none (or vice versa), or different nets → conflict
        return path[:i]

    return path
