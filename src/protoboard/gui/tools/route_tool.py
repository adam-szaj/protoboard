# SPDX-License-Identifier: MIT
"""Route wire tool — click-click-...-Enter/Escape to finish a multi-segment wire."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import structlog
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPen
from PySide6.QtWidgets import QGraphicsLineItem

from protoboard.domain.commands.component_cmds import AddWireWithNetsCommand
from protoboard.domain.connectivity.graph import ConnectivityGraph
from protoboard.domain.layout.placement import pin_world_position
from protoboard.domain.layout.wire import Wire
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

    Net-aware throughout: every commit goes through
    ``resolve_wire_nets`` and the only dialog the user ever sees is the
    NetMergeDialog when two *different* nets would be merged.  Net
    creation (BOTH_UNCONNECTED) and net extension (ONE_UNCONNECTED) are
    silent — they only mutate the schematic when the user confirms the
    wire commit (Enter).  Per-click confirmations were a usability
    mistake — at the time of the first click neither the second
    endpoint nor the wire's full net implication is known.
    """

    def __init__(
        self,
        insulated: bool = True,
        side: str = "top",
    ) -> None:
        super().__init__()
        self._insulated = insulated
        self._side = side
        self._points: list[Point] = []
        # Committed segments (between clicked points).
        self._committed_lines: list[QGraphicsLineItem] = []
        # Single live segment from last point to cursor — None when not active.
        self._live_line: QGraphicsLineItem | None = None
        self._hint_items: list[QGraphicsLineItem] = []
        # Background dim overlay shown during active wire drag.  None
        # while idle; QGraphicsRectItem after _set_route_highlight().
        self._dim_overlay: object | None = None
        # Track whether the transient highlight + dim overlay are
        # currently pushed so commit/cancel can pop exactly once.
        self._route_highlight_active: bool = False

    def activate(self, session: object, view: object) -> None:
        super().activate(session, view)  # type: ignore[arg-type]
        # No transient highlight on bare activation — RouteTool only
        # dims when the user is actively pulling a wire (i.e. after the
        # first click).  Standby mode keeps the normal view so the user
        # can scan the layout for the starting pad.
        self._route_highlight_active = False

    def deactivate(self) -> None:
        self._clear_route_highlight()
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
            self._highlight_start_net(snapped)
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
            event.accept()
        elif key == Qt.Key.Key_Escape:
            self._cancel()
            event.accept()
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
        self._session.set_routing_net(None)
        self._clear_route_highlight()
        if len(self._points) < 2:
            self._cancel()
            return

        from protoboard.domain.layout.wire_utils import (
            board_pads_for_side,
            insert_pad_nodes,
        )
        raw_path = list(self._points)
        # Take pads from BOTH sides — a wire passing geometrically over a pad
        # that exists only on the opposite side still needs that node so
        # simplify/unweave/edit can grab it.  Single-side board: the other
        # side returns an empty set, no effect.
        pads = (
            board_pads_for_side(self._session.board, "top")
            | board_pads_for_side(self._session.board, "bottom")
        )
        enriched_path = insert_pad_nodes(raw_path, pads)

        wire = Wire(
            id=uuid.uuid4(),
            path=enriched_path,
            insulated=self._insulated,
            side=self._side,  # type: ignore[arg-type]
        )

        # Every commit is net-aware: resolve_wire_nets silently extends or
        # creates nets, shows the NetMergeDialog only when two different
        # nets would collapse.  Cancel from that dialog aborts the commit.
        from protoboard.gui.wire_net_resolver import resolve_wire_nets
        cmd = AddWireWithNetsCommand(wire=wire)
        if not resolve_wire_nets(
            self._session, wire, cmd, self._view.window()  # type: ignore[arg-type]
        ):
            _log.debug("route.cancelled", reason="net_merge_rejected")
            self._cancel()
            return

        self._points.clear()
        self._clear_committed()
        self._remove_live()
        self._clear_hints()
        self._session.stack.execute(cmd, self._session)
        _log.info("route.committed", wire_id=str(wire.id), points=len(wire.path),
                  insulated=wire.insulated)
        self._session.layout_changed.emit()

    def _cancel(self) -> None:
        # Clear state first so on_scene_cleared() (triggered by refresh below)
        # sees empty _points and does not try to redraw deleted C++ objects.
        self._points.clear()
        self._clear_committed()
        self._remove_live()
        self._clear_hints()
        self._clear_route_highlight()
        self._session.set_routing_net(None)

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
            active_net_id=self._session.routing_net_id,
        )
        _log.debug("route.hints", paths_found=len(results), targets=len(targets))
        self._hint_items = render_routing_hints(
            self._view.scene(), results, theme=self._theme(),
        )

    def _highlight_start_net(self, source: Point) -> None:
        """Mark the net of the starting pad as the active routing net.

        BoardView unions this with whatever HighlightStack is showing, so a
        user-pinned persistent highlight on a different net stays visible.
        Also pushes a transient highlight entry that adds the
        route-active visual treatment: stronger dim on unrelated items,
        semi-transparent routed components (so the user can see pin
        geometry hidden under SVG bodies), and a background dim overlay.
        """
        graph = self._session.connectivity_graph()
        source_node = graph.node_for_pad(source, self._side)
        if source_node is None:
            self._set_route_highlight(net_id=None)
            return
        for net in self._session.schematic.nets.values():
            for ref in net.pins:
                if graph.node_for_pin(ref) == source_node:
                    self._session.set_routing_net(net.id)
                    self._set_route_highlight(net_id=net.id)
                    return
        # Start pad isn't on any pin → orphan route.  Still want the
        # dim+overlay treatment, just without a netted highlight.
        self._set_route_highlight(net_id=None)

    # ------------------------------------------------------------------
    # Route-active visual treatment (Etap 2 + 3)
    # ------------------------------------------------------------------

    def _set_route_highlight(self, *, net_id: "uuid.UUID | None") -> None:
        """Push transient highlight entry + show background dim overlay.

        Idempotent — repeat calls within the same drag are no-ops.
        Cleared by :meth:`_clear_route_highlight`.
        """
        if self._route_highlight_active:
            return
        from protoboard.gui.highlight_state import (
            HighlightEntry,
            PROFILE_ROUTE_ACTIVE,
        )
        comp_ids: frozenset[uuid.UUID] = frozenset()
        if net_id is not None:
            net = self._session.schematic.nets.get(net_id)
            if net is not None:
                comp_ids = frozenset(ref.component_id for ref in net.pins)
        self._session.highlight_stack.push_transient(  # type: ignore[attr-defined]
            HighlightEntry(
                comp_ids=comp_ids,
                net_id=net_id,
                profile=PROFILE_ROUTE_ACTIVE,
                label="Route active",
                transient=True,
            )
        )
        self._add_dim_overlay()
        self._route_highlight_active = True

    def _clear_route_highlight(self) -> None:
        """Pop transient highlight + remove background dim overlay."""
        if not self._route_highlight_active:
            return
        self._session.highlight_stack.pop_transient()  # type: ignore[attr-defined]
        self._remove_dim_overlay()
        self._route_highlight_active = False

    def _add_dim_overlay(self) -> None:
        """Add a semi-transparent dark rectangle covering the whole scene.

        Sits below the components/wires (z = -10) but above the board
        background, so unrelated items dim further while the routed
        items (already at higher z + boosted by bold highlight) remain
        visible.  Removed by ``_remove_dim_overlay``.
        """
        from PySide6.QtWidgets import QGraphicsRectItem
        from PySide6.QtGui import QBrush, QColor
        from PySide6.QtCore import Qt as _Qt
        scene = self._view.scene()
        if scene is None:
            return
        rect_item = QGraphicsRectItem(scene.sceneRect())
        # 50% black overlay — strong enough to push background away
        # while the bold routed pads (z >> 0) stay legible above it.
        rect_item.setBrush(QBrush(QColor(0, 0, 0, 128)))
        rect_item.setPen(_Qt.PenStyle.NoPen)
        # Z chosen by inspection of board_view layering: comp groups
        # live around z=2, wires around z=1-3.  -0.5 puts the overlay
        # above the board background (board z = -10) but below all
        # interactive items.
        rect_item.setZValue(-0.5)
        scene.addItem(rect_item)
        self._dim_overlay = rect_item

    def _remove_dim_overlay(self) -> None:
        if self._dim_overlay is None:
            return
        try:
            self._view.scene().removeItem(self._dim_overlay)  # type: ignore[arg-type]
        except RuntimeError:
            pass  # scene already cleared
        self._dim_overlay = None

    def on_scene_cleared(self) -> None:
        """Called after every refresh() — redraw committed lines from _points."""
        self._committed_lines.clear()
        self._live_line = None   # C++ object deleted by scene.clear(); drop ref
        self._hint_items.clear()
        # Background dim overlay is also a scene item — clear the
        # reference so the next _add_dim_overlay() doesn't try to
        # remove a freed C++ object.
        self._dim_overlay = None

        # Redraw committed segments from stored point sequence.
        if len(self._points) >= 2:
            for i in range(len(self._points) - 1):
                self._add_committed_line(self._points[i], self._points[i + 1])
        # Live line is NOT redrawn here — it will be recreated on the next
        # mouseMoveEvent via on_move() → _add_live_line().
        # Re-add the background dim overlay if a route is in flight; the
        # scene clear wiped it.
        if self._route_highlight_active:
            self._add_dim_overlay()

    def _clear_hints(self) -> None:
        for item in self._hint_items:
            self._view.scene().removeItem(item)
        self._hint_items.clear()

    def _add_committed_line(self, a: Point, b: Point) -> None:
        pen = QPen(QColor(self._theme().routing_preview_color), _PREVIEW_WIDTH)
        line = QGraphicsLineItem(a.x, a.y, b.x, b.y)
        line.setPen(pen)
        self._view.scene().addItem(line)
        self._committed_lines.append(line)

    def _add_live_line(self, a: Point, b: Point) -> None:
        pen = QPen(QColor(self._theme().routing_preview_color), _PREVIEW_WIDTH)
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
