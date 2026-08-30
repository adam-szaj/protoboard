# SPDX-License-Identifier: MIT
"""NetInspectorPanel — read-only net statistics with click-to-highlight."""

from __future__ import annotations

import math
import uuid
from typing import TYPE_CHECKING, NamedTuple

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

from protoboard.gui.panels.tree_panel_mixin import TreePanelMixin

if TYPE_CHECKING:
    from collections.abc import Callable

    from protoboard.domain.board.pad_state import PadIndex
    from protoboard.domain.layout.wire import Wire
    from protoboard.domain.schematic.net import Net
    from protoboard.geometry import Point
    from protoboard.gui.board_view import BoardView
    from protoboard.services.routing.cost_model import RoutingCostModel

_COL_NAME = 0
_COL_PINS = 1
_COL_WIRES = 2
_COL_LENGTH = 3
_COL_RATIO = 4  # wire length / MST manhattan
_COL_HULL = 5  # convex hull area (wires) / convex hull area (pins)
_COL_COST = 6  # total routing cost under current cost_model
_ROLE = Qt.ItemDataRole.UserRole

_UNROUTED_COLOR = QColor("#E07B00")
_NA = "—"
_NA_SORT = -1.0  # sentinel for numeric sort of N/A cells


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


class NetInspectorPanel(TreePanelMixin, QWidget):
    """Read-only panel listing nets with statistics and routing quality metrics."""

    def __init__(
        self,
        board_view: BoardView,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._board_view = board_view
        # Set when `_rebuild` was called while hidden — `showEvent` picks
        # it up so the tree catches up the moment the user reveals the
        # panel.  Avoids spending O(nets × wires) on a tree no one sees.
        self._refresh_pending = False

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(4, 4, 4, 4)
        root_layout.setSpacing(4)
        root_layout.addWidget(QLabel("Net Inspector"))

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(
            ["Name", "Pins", "Wires", "Length (r.u.)", "Wire/MST", "Hull", "Cost"]
        )
        self._tree.setColumnWidth(_COL_PINS, 38)
        self._tree.setColumnWidth(_COL_WIRES, 42)
        self._tree.setColumnWidth(_COL_LENGTH, 76)
        self._tree.setColumnWidth(_COL_RATIO, 68)
        self._tree.setColumnWidth(_COL_HULL, 58)
        self._tree.setColumnWidth(_COL_COST, 72)
        self._tree.setSortingEnabled(True)
        self._tree.header().setSectionResizeMode(
            _COL_NAME, QHeaderView.ResizeMode.Stretch
        )
        self._tree.setAlternatingRowColors(True)
        self._tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        root_layout.addWidget(self._tree)

        self._tree.itemClicked.connect(self._on_item_clicked)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def sync_selection(
        self,
        comp_ids: object,
        wire_ids: object,
        net_id: object,
    ) -> None:
        """Highlight the row matching net_id. Called by MainWindow on board selection."""  # noqa: E501
        del comp_ids, wire_ids  # unused: this panel keys off net_id only
        if not self.isVisible():
            return
        self._syncing = True
        try:
            self._tree.clearSelection()
            if net_id is not None:
                item = self._find_item(str(net_id))
                if item is not None:
                    item.setSelected(True)
                    self._tree.scrollToItem(item)
        finally:
            self._syncing = False

    def clear_highlight(self) -> None:
        self._board_view.set_highlighted_net(None)
        self._tree.clearSelection()

    def showEvent(self, event: object) -> None:  # type: ignore[override]
        super().showEvent(event)  # type: ignore[arg-type]
        if self._refresh_pending and self._session is not None:
            self._refresh_pending = False
            self._rebuild()

    def selected_net_ids(self) -> set[uuid.UUID]:
        """Return UUIDs of all nets currently selected in the panel."""
        result: set[uuid.UUID] = set()
        for item in self._tree.selectedItems():
            raw = item.data(_COL_NAME, _ROLE)
            if isinstance(raw, str):
                try:
                    result.add(uuid.UUID(raw))
                except ValueError:
                    pass
        return result

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _rebuild(self) -> None:
        self._tree.clear()
        session = self._session
        if session is None or not session.schematic.nets:
            return
        # Inspector populates synchronously on the main thread; skip the
        # whole walk when the user can't see the result.  Refresh is
        # already debounced via the parent's `_panels_timer` (100 ms), so
        # marking pending state here is cheap.
        if not self.isVisible():
            self._refresh_pending = True
            return
        self._refresh_pending = False

        from protoboard.services.routing.cost_analyzer import analyze_wires
        from protoboard.services.routing.net_status import routed_net_ids

        graph = session.connectivity_graph()
        routed = routed_net_ids(session.schematic, graph)

        wire_net_map = session.cached_wire_net_map()
        net_wires: dict[uuid.UUID, list[Wire]] = {}
        for wire in session.layout.wires:
            nid = wire_net_map.get(wire.id)
            if nid is not None:
                net_wires.setdefault(nid, []).append(wire)  # type: ignore[arg-type]

        cm = session.routing_settings.cost_model()
        # Snapshot the full wire list once so each per-net `analyze_wires`
        # call sees crossings against wires from other nets.
        all_wires = list(session.layout.wires)
        pad_idx = session.pad_index()
        # Pad-walk sets come from the session cache — `_walk_atoms()` only
        # runs the very first time after a layout change, not per refresh
        # and definitely not per net.
        cost_under_pads = (
            session.cached_under_component_pads()
            if cm.get("under_component_top", 0.0) > 0.0
            else set()
        )
        cost_plated_pads = (
            session.cached_plated_through_pads()
            if cm.get("layer_change", 0.0) > 0.0
            else set()
        )

        unrouted_brush = QBrush(_UNROUTED_COLOR)

        for net in sorted(session.schematic.nets.values(), key=lambda n: n.name):
            wires = net_wires.get(net.id, [])
            pin_count = len(net.pins)
            wire_count = len(wires)

            # Collect placed pin positions for this net.
            pin_pts: list[Point] = []
            for ref in net.pins:
                pos = session.get_pin_position(ref.component_id, ref.pin_number)
                if pos is not None:
                    pin_pts.append(pos)

            metrics = _compute_net_metrics(
                wires,
                pin_pts,
                cost_model=cm,
                under_component_pads=cost_under_pads,
                plated_through_pads=cost_plated_pads,
                all_board_wires=all_wires,
                pad_index=pad_idx,
                analyze_wires=analyze_wires,
            )

            item = self._make_net_item(
                net, pin_count, wire_count, metrics, unrouted_brush, routed
            )
            self._tree.addTopLevelItem(item)

    def _make_net_item(
        self,
        net: Net,
        pin_count: int,
        wire_count: int,
        metrics: _NetMetrics,
        unrouted_brush: QBrush,
        routed: set[uuid.UUID],
    ) -> _SortableItem:
        """Build the sortable tree row for one net from its computed metrics."""
        item = _SortableItem()
        item.setText(_COL_NAME, net.name)
        item.setText(_COL_PINS, str(pin_count))
        item.setText(_COL_WIRES, str(wire_count))
        item.setText(_COL_LENGTH, f"{metrics.length:.1f}")
        item.setText(_COL_RATIO, metrics.ratio_txt)
        item.setText(_COL_HULL, metrics.hull_txt)
        item.setText(_COL_COST, metrics.cost_txt)

        item.setData(_COL_NAME, _ROLE, str(net.id))
        item.setData(_COL_PINS, _ROLE, pin_count)
        item.setData(_COL_WIRES, _ROLE, wire_count)
        item.setData(_COL_LENGTH, _ROLE, metrics.length)
        item.setData(_COL_RATIO, _ROLE, metrics.ratio_key)
        item.setData(_COL_HULL, _ROLE, metrics.hull_key)
        item.setData(_COL_COST, _ROLE, metrics.cost_key)

        if net.id not in routed:
            item.setForeground(_COL_NAME, unrouted_brush)
        return item

    def _on_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        if self._syncing or self._session is None:
            return
        raw = item.data(_COL_NAME, _ROLE)
        if not isinstance(raw, str):
            return
        try:
            net_id = uuid.UUID(raw)
        except ValueError:
            return
        # Panel -> board through the session hub (not a direct board_view call):
        # selecting the whole net lights its pads/wires AND, looping back through
        # selection_changed, re-selects this row and the Nets-panel net row.
        self._session.request_select_net(net_id)


# ---------------------------------------------------------------------------
# Metric aggregation
# ---------------------------------------------------------------------------


class _NetMetrics(NamedTuple):
    """Computed per-net display metrics: cell text plus numeric sort keys."""

    length: float
    ratio_txt: str
    ratio_key: float
    hull_txt: str
    hull_key: float
    cost_txt: str
    cost_key: float


def _compute_net_metrics(
    wires: list[Wire],
    pin_pts: list[Point],
    *,
    cost_model: RoutingCostModel,
    under_component_pads: set[Point],
    plated_through_pads: set[Point],
    all_board_wires: list[Wire],
    pad_index: PadIndex,
    analyze_wires: Callable[..., object],
) -> _NetMetrics:
    """Compute length / MST ratio / hull ratio / routing cost for one net.

    Returns the formatted cell text and the numeric sort keys; the formulas
    and formatting are identical to the inline loop they replaced.
    """
    length = _manhattan_length(wires)

    # Wire/MST ratio.
    mst = _mst_manhattan(pin_pts)
    if mst > 0.0:
        ratio = length / mst
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
        hull_ratio = hull_w / hull_p
        hull_txt = f"{hull_ratio:.2f}"
        hull_key: float = hull_ratio
    else:
        hull_txt = _NA
        hull_key = _NA_SORT

    # Routing cost for this net's wires under the current cost model.
    # Cheap (O(steps)); precomputed pad sets above keep this fast
    # even with hundreds of nets.  ``all_board_wires`` is the full
    # board's wire list so crossings with wires of *other* nets
    # are charged against this net too.
    if wires:
        cost = analyze_wires(
            wires,
            cost_model=cost_model,
            under_component_pads=under_component_pads,
            plated_through_pads=plated_through_pads,
            all_board_wires=all_board_wires,
            pad_index=pad_index,
        ).total  # type: ignore[attr-defined]
        cost_txt = f"{cost:.1f}"
        cost_key: float = cost
    else:
        cost_txt = _NA
        cost_key = _NA_SORT

    return _NetMetrics(
        length=length,
        ratio_txt=ratio_txt,
        ratio_key=ratio_key,
        hull_txt=hull_txt,
        hull_key=hull_key,
        cost_txt=cost_txt,
        cost_key=cost_key,
    )


# ---------------------------------------------------------------------------
# Pure geometry helpers
# ---------------------------------------------------------------------------


def _manhattan_length(wires: list[Wire]) -> float:
    total = 0.0
    for wire in wires:
        pts = wire.path
        for i in range(len(pts) - 1):
            total += abs(pts[i + 1].x - pts[i].x) + abs(pts[i + 1].y - pts[i].y)
    return total


def _mst_manhattan(points: list[Point]) -> float:
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
            d = abs(points[next_j].x - points[j].x) + abs(
                points[next_j].y - points[j].y
            )
            if d < min_dist[j]:
                min_dist[j] = d
                nearest[j] = next_j
    return total


def _convex_hull_area(points: list[Point]) -> float:
    """Convex hull area via Graham scan + shoelace formula. Returns 0 for <3 pts."""
    if len(points) < 3:
        return 0.0
    pivot = min(points, key=lambda p: (p.y, p.x))

    def _angle(p: Point) -> float:
        return (p - pivot).angle()

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


def _pin_hull_area(points: list[Point]) -> float:
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
        return abs(points[0].x - points[1].x) + abs(points[0].y - points[1].y)
    return _convex_hull_area(points)
