# SPDX-License-Identifier: MIT
"""NetInspectorPanel — read-only net statistics with click-to-highlight."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
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
    from protoboard.gui.board_view import BoardView
    from protoboard.gui.session import EditorSession

_COL_NAME = 0
_COL_PINS = 1
_COL_WIRES = 2
_COL_LENGTH = 3
_ROLE = Qt.ItemDataRole.UserRole


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
    """Read-only panel listing nets with pin count, wire count, and total length."""

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
        self._tree.setHeaderLabels(["Name", "Pins", "Wires", "Length (r.u.)"])
        self._tree.setColumnWidth(_COL_PINS, 38)
        self._tree.setColumnWidth(_COL_WIRES, 42)
        self._tree.setColumnWidth(_COL_LENGTH, 76)
        self._tree.setSortingEnabled(True)
        self._tree.header().setSectionResizeMode(
            _COL_NAME, QHeaderView.ResizeMode.Stretch
        )
        self._tree.setAlternatingRowColors(True)
        self._tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        root_layout.addWidget(self._tree)

        self._tree.itemClicked.connect(self._on_item_clicked)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def refresh(self, session: "EditorSession") -> None:
        self._session = session
        self._rebuild()

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

        from protoboard.gui.board_view import _build_wire_net_map

        wire_net_map = _build_wire_net_map(session)
        net_wires: dict[uuid.UUID, list[WireSegment]] = {}
        for wire in session.layout.wires:
            nid = wire_net_map.get(wire.id)
            if nid is not None:
                net_wires.setdefault(nid, []).append(wire)  # type: ignore[arg-type]

        for net in sorted(session.schematic.nets.values(), key=lambda n: n.name):
            wires = net_wires.get(net.id, [])
            length = _manhattan_length(wires)
            pin_count = len(net.pins)
            wire_count = len(wires)

            item = _SortableItem()
            item.setText(_COL_NAME, net.name)
            item.setText(_COL_PINS, str(pin_count))
            item.setText(_COL_WIRES, str(wire_count))
            item.setText(_COL_LENGTH, f"{length:.1f}")
            item.setData(_COL_NAME, _ROLE, str(net.id))
            item.setData(_COL_PINS, _ROLE, pin_count)
            item.setData(_COL_WIRES, _ROLE, wire_count)
            item.setData(_COL_LENGTH, _ROLE, length)
            self._tree.addTopLevelItem(item)

    def _on_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        raw = item.data(_COL_NAME, _ROLE)
        if not isinstance(raw, str):
            return
        try:
            net_id = uuid.UUID(raw)
        except ValueError:
            return
        self._board_view.set_highlighted_net(net_id)


def _manhattan_length(wires: list["WireSegment"]) -> float:
    total = 0.0
    for wire in wires:
        pts = wire.path
        for i in range(len(pts) - 1):
            total += abs(pts[i + 1].x - pts[i].x) + abs(pts[i + 1].y - pts[i].y)
    return total
