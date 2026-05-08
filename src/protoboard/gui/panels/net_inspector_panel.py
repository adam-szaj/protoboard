# SPDX-License-Identifier: MIT
"""NetInspectorPanel — read-only net statistics with click-to-highlight."""

from __future__ import annotations

import math
import uuid
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QHeaderView,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from protoboard.domain.layout.wire import WireSegment
    from protoboard.geometry import Point
    from protoboard.gui.board_view import BoardView
    from protoboard.gui.session import EditorSession

_COL_NAME   = 0
_COL_PINS   = 1
_COL_WIRES  = 2
_COL_LENGTH = 3
_COL_RATIO  = 4   # wire length / MST manhattan
_COL_HULL   = 5   # convex hull area (wires) / convex hull area (pins)
_ROLE = Qt.ItemDataRole.UserRole

_UNROUTED_COLOR = QColor("#E07B00")
_NA = "—"
_NA_SORT = -1.0   # sentinel for numeric sort of N/A cells


class _SortableItem(QTreeWidgetItem):
    """QTreeWidgetItem with numeric-aware column sorting."""

    def __lt__(self, other: QTreeWidgetItem) -> bool:
        tree = self.treeWidget()
        col = tree.sortColumn() if tree is not None else 0
        v_self = self.data(col, _ROLE)
        v_other = other.data(col, _ROLE)
        if isinstance(v_self, (int, float)) and isinstance(v_other, (int, float)):
            return v_self < v_other
        return self.text(col) < other.text(col)


class NetInspectorPanel(QWidget):
    """Read-only panel listing nets with statistics and routing quality metrics."""

    def __init__(
        self,
        board_view: "BoardView",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._board_view = board_view
        self._session: "EditorSession | None" = None

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(4, 4, 4, 4)
        root_layout.setSpacing(4)
        root_layout.addWidget(QLabel("Net Inspector"))

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(
            ["Name", "Pins", "Wires", "Length (r.u.)", "Wire/MST", "Hull"]
        )
        self._tree.setColumnWidth(_COL_PINS,   38)
        self._tree.setColumnWidth(_COL_WIRES,  42)
        self._tree.setColumnWidth(_COL_LENGTH, 76)
        self._tree.setColumnWidth(_COL_RATIO,  68)
        self._tree.setColumnWidth(_COL_HULL,   58)
        self._tree.setSortingEnabled(True)
        self._tree.header().setSectionResizeMode(
            _COL_NAME, QHeaderView.ResizeMode.Stretch
        )
        self._tree.setAlternatingRowColors(True)
        self._tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        root_layout.addWidget(self._tree)

        self._tree.itemClicked.connect(self._on_item_clicked)
        self._syncing: bool = False

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def refresh(self, session: "EditorSession") -> None:
        self._session = session
        self._rebuild()

    def sync_selection(
        self,
        _comp_ids: object,
        _wire_ids: object,
        net_id: object,
    ) -> None:
        """Highlight the row matching net_id. Called by MainWindow on board selection."""
        if not self.isVisible():
            return
        self._syncing = True
        try:
            self._tree.clearSelection()
            if net_id is not None:
                for i in range(self._tree.topLevelItemCount()):
                    item = self._tree.topLevelItem(i)
                    if item is None:
                        continue
                    if item.data(_COL_NAME, _ROLE) == str(net_id):
                        item.setSelected(True)
                        self._tree.scrollToItem(item)
                        break
        finally:
            self._syncing = False

    def clear_highlight(self) -> None:
        self._board_view.set_highlighted_net(None)
        self._tree.clearSelection()

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _rebuild(self) -> None:
        self._tree.clear()
        session = self._session
        if session is None or not session.schematic.nets:
            return

        from protoboard.domain.layout.placement import pin_world_position
        from protoboard.domain.schematic.net import PinRef
        from protoboard.gui.board_view import _build_wire_net_map
        from protoboard.services.routing.net_status import routed_net_ids

        graph = session.connectivity_graph()
        routed = routed_net_ids(session.schematic, graph)

        wire_net_map = _build_wire_net_map(session)
        net_wires: dict[uuid.UUID, list[WireSegment]] = {}
        for wire in session.layout.wires:
            nid = wire_net_map.get(wire.id)
            if nid is not None:
                net_wires.setdefault(nid, []).append(wire)  # type: ignore[arg-type]

        unrouted_brush = QBrush(_UNROUTED_COLOR)

        for net in sorted(session.schematic.nets.values(), key=lambda n: n.name):
            wires = net_wires.get(net.id, [])
            length     = _manhattan_length(wires)
            pin_count  = len(net.pins)
            wire_count = len(wires)

            # Collect placed pin positions for this net.
            pin_pts: list[Point] = []
            for ref in net.pins:
                pl   = session.layout.placements.get(ref.component_id)
                inst = session.instances.get(ref.component_id)
                if pl is None or inst is None:
                    continue
                defn = session.components.get(inst.definition_ref)
                if defn is None or ref.pin_number > defn.pin_count:
                    continue
                pin_pts.append(pin_world_position(defn.footprint, pl, ref.pin_number))

            # Wire/MST ratio.
            mst = _mst_manhattan(pin_pts)
            if mst > 0.0:
                ratio     = length / mst
                ratio_txt = f"{ratio:.2f}"
                ratio_key: float = ratio
            else:
                ratio_txt = _NA
                ratio_key = _NA_SORT

            # Hull ratio.
            wire_pts: list[Point] = []
            for w in wires:
                wire_pts.extend(w.path)  # type: ignore[arg-type]
            hull_w = _convex_hull_area(wire_pts)
            hull_p = _pin_hull_area(pin_pts)
            if hull_p > 0.0:
                hull_ratio     = hull_w / hull_p
                hull_txt       = f"{hull_ratio:.2f}"
                hull_key: float = hull_ratio
            else:
                hull_txt = _NA
                hull_key = _NA_SORT

            item = _SortableItem()
            item.setText(_COL_NAME,   net.name)
            item.setText(_COL_PINS,   str(pin_count))
            item.setText(_COL_WIRES,  str(wire_count))
            item.setText(_COL_LENGTH, f"{length:.1f}")
            item.setText(_COL_RATIO,  ratio_txt)
            item.setText(_COL_HULL,   hull_txt)

            item.setData(_COL_NAME,   _ROLE, str(net.id))
            item.setData(_COL_PINS,   _ROLE, pin_count)
            item.setData(_COL_WIRES,  _ROLE, wire_count)
            item.setData(_COL_LENGTH, _ROLE, length)
            item.setData(_COL_RATIO,  _ROLE, ratio_key)
            item.setData(_COL_HULL,   _ROLE, hull_key)

            if net.id not in routed:
                item.setForeground(_COL_NAME, unrouted_brush)

            self._tree.addTopLevelItem(item)

    def _on_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        if self._syncing:
            return
        raw = item.data(_COL_NAME, _ROLE)
        if not isinstance(raw, str):
            return
        try:
            net_id = uuid.UUID(raw)
        except ValueError:
            return
        self._board_view.set_highlighted_net(net_id)


# ---------------------------------------------------------------------------
# Pure geometry helpers
# ---------------------------------------------------------------------------

def _manhattan_length(wires: list["WireSegment"]) -> float:
    total = 0.0
    for wire in wires:
        pts = wire.path
        for i in range(len(pts) - 1):
            total += abs(pts[i + 1].x - pts[i].x) + abs(pts[i + 1].y - pts[i].y)
    return total


def _mst_manhattan(points: list["Point"]) -> float:
    """Minimum spanning tree length in Manhattan metric (Prim's algorithm)."""
    n = len(points)
    if n < 2:
        return 0.0
    in_tree = {0}
    total = 0.0
    min_dist = [
        abs(points[0].x - points[j].x) + abs(points[0].y - points[j].y)
        for j in range(n)
    ]
    nearest = [0] * n
    while len(in_tree) < n:
        best = math.inf
        next_j = -1
        for j in range(n):
            if j in in_tree:
                continue
            if min_dist[j] < best:
                best = min_dist[j]
                next_j = j
        in_tree.add(next_j)
        total += best
        # Update distances from the newly added node.
        for j in range(n):
            if j in in_tree:
                continue
            d = (abs(points[next_j].x - points[j].x)
                 + abs(points[next_j].y - points[j].y))
            if d < min_dist[j]:
                min_dist[j] = d
                nearest[j] = next_j
    return total


def _convex_hull_area(points: list["Point"]) -> float:
    """Convex hull area via Graham scan + shoelace formula. Returns 0 for <3 pts."""
    if len(points) < 3:
        return 0.0
    pivot = min(points, key=lambda p: (p.y, p.x))

    def _angle(p: "Point") -> float:
        return math.atan2(p.y - pivot.y, p.x - pivot.x)

    sorted_pts = sorted(points, key=_angle)
    hull: list[Point] = []
    for p in sorted_pts:
        while len(hull) >= 2:
            o, a = hull[-2], hull[-1]
            cross = (a.x - o.x) * (p.y - o.y) - (a.y - o.y) * (p.x - o.x)
            if cross <= 0:
                hull.pop()
            else:
                break
        hull.append(p)
    nh = len(hull)
    area = 0.0
    for i in range(nh):
        j = (i + 1) % nh
        area += hull[i].x * hull[j].y
        area -= hull[j].x * hull[i].y
    return abs(area) / 2.0


def _pin_hull_area(points: list["Point"]) -> float:
    """Hull area for the pin set, where each pad = 1 (r.u.)².

    - 0 pins  → 0
    - 1 pin   → 1
    - 2 pins  → Manhattan distance between them
    - ≥3 pins → convex hull area (shoelace)
    """
    n = len(points)
    if n == 0:
        return 0.0
    if n == 1:
        return 1.0
    if n == 2:
        return (abs(points[0].x - points[1].x)
                + abs(points[0].y - points[1].y))
    return _convex_hull_area(points)
