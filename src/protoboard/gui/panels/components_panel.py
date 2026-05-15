# SPDX-License-Identifier: MIT
"""ComponentsPanel — list of all component instances imported from a netlist.

Shows every ComponentInstance in the session with:
  - RefDes (e.g. R1, U3)
  - Placement status  ✓ placed / ✗ not yet on board
  - Value  (e.g. 10k, LM358)
  - Definition ref / footprint  (e.g. R_0603_1608Metric)

Double-clicking an unplaced row emits ``place_requested(uuid.UUID)`` so that
MainWindow can activate PlaceTool for that specific instance.
Clicking any row emits ``instance_selected(uuid.UUID)`` for board highlight.

"Place selected" / "Place all" buttons emit ``auto_place_selected_requested``
/ ``auto_place_all_requested`` with a list of UUIDs for auto-placement.

Search box filters by RefDes, value, or definition_ref.
Sort columns by clicking the header.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from protoboard.gui.panels.tree_panel_mixin import TreePanelMixin

if TYPE_CHECKING:
    from protoboard.gui.session import EditorSession

_PLACED_COLOR = QColor("#1A8C1A")     # green
_UNPLACED_COLOR = QColor("#AAAAAA")   # grey
_PLACED_MARK = "✓"
_UNPLACED_MARK = "✗"

# Column indices
_COL_REFDES = 0
_COL_STATUS = 1
_COL_VALUE = 2
_COL_FOOTPRINT = 3

_HEADERS = ["RefDes", "", "Value", "Footprint / Definition"]


class ComponentsPanel(TreePanelMixin, QWidget):
    """Panel listing all netlist-imported component instances."""

    instance_selected = Signal(uuid.UUID)
    place_requested = Signal(uuid.UUID)
    auto_place_selected_requested = Signal(list)  # list[uuid.UUID]
    auto_place_all_requested = Signal(list)        # list[uuid.UUID]

    def __init__(self, parent: object = None) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Header row: title + counters
        hdr = QHBoxLayout()
        self._title = QLabel("Components")
        self._title.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        hdr.addWidget(self._title)
        layout.addLayout(hdr)

        # Search bar
        self._search = QLineEdit()
        self._search.setPlaceholderText("Filter by RefDes / value / footprint…")
        self._search.setClearButtonEnabled(True)
        layout.addWidget(self._search)

        # Tree — extended selection so "Place selected" works for multiple rows
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(_HEADERS)
        self._tree.setColumnWidth(_COL_REFDES, 60)
        self._tree.setColumnWidth(_COL_STATUS, 22)
        self._tree.setColumnWidth(_COL_VALUE, 70)
        self._tree.setAlternatingRowColors(True)
        self._tree.setSortingEnabled(True)
        self._tree.sortByColumn(_COL_REFDES, Qt.SortOrder.AscendingOrder)
        self._tree.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        layout.addWidget(self._tree)

        # Manual single-place button
        self._place_btn = QPushButton("Place on board")
        self._place_btn.setEnabled(False)
        self._place_btn.setToolTip(
            "Activate PlaceTool for the selected component (click to position)"
        )
        layout.addWidget(self._place_btn)

        # Auto-place buttons
        auto_row = QHBoxLayout()
        self._auto_selected_btn = QPushButton("Place selected")
        self._auto_selected_btn.setEnabled(False)
        self._auto_selected_btn.setToolTip(
            "Auto-place all selected unplaced components"
        )
        self._auto_all_btn = QPushButton("Place all")
        self._auto_all_btn.setEnabled(False)
        self._auto_all_btn.setToolTip(
            "Auto-place every unplaced component in the project"
        )
        auto_row.addWidget(self._auto_selected_btn)
        auto_row.addWidget(self._auto_all_btn)
        layout.addLayout(auto_row)

        self._search.textChanged.connect(self._on_filter)
        self._tree.itemSelectionChanged.connect(self._on_selection_changed)
        self._tree.itemDoubleClicked.connect(self._on_double_click)
        self._place_btn.clicked.connect(self._on_place_btn)
        self._auto_selected_btn.clicked.connect(self._on_auto_place_selected)
        self._auto_all_btn.clicked.connect(self._on_auto_place_all)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _rebuild(self) -> None:
        self._tree.setSortingEnabled(False)
        self._tree.clear()
        session = self._session
        if session is None:
            self._title.setText("Components")
            self._auto_all_btn.setEnabled(False)
            return

        placed_ids = set(session.layout.placements.keys())
        total = len(session.instances)
        placed = sum(1 for iid in session.instances if iid in placed_ids)
        unplaced_count = total - placed

        self._title.setText(f"Components  ({placed}/{total} placed)")
        self._auto_all_btn.setEnabled(unplaced_count > 0)

        filt = self._search.text().strip().lower()

        for inst in sorted(session.instances.values(), key=lambda i: i.refdes):
            fp = inst.definition_ref
            if filt and not (
                filt in inst.refdes.lower()
                or filt in inst.value.lower()
                or filt in fp.lower()
            ):
                continue

            is_placed = inst.id in placed_ids
            mark = _PLACED_MARK if is_placed else _UNPLACED_MARK
            color = _PLACED_COLOR if is_placed else _UNPLACED_COLOR

            item = QTreeWidgetItem([inst.refdes, mark, inst.value, fp])
            item.setTextAlignment(_COL_STATUS, Qt.AlignmentFlag.AlignCenter)
            brush = QBrush(color)
            for col in range(4):
                item.setForeground(col, brush)
            item.setData(_COL_REFDES, Qt.ItemDataRole.UserRole, str(inst.id))
            item.setData(_COL_STATUS, Qt.ItemDataRole.UserRole, is_placed)
            self._tree.addTopLevelItem(item)

        self._tree.setSortingEnabled(True)
        self._tree.sortByColumn(_COL_REFDES, Qt.SortOrder.AscendingOrder)
        self._place_btn.setEnabled(False)
        self._auto_selected_btn.setEnabled(False)

    def _on_filter(self) -> None:
        self._rebuild()

    def _on_selection_changed(self) -> None:
        if self._syncing:
            return
        items = self._tree.selectedItems()
        if not items:
            self._place_btn.setEnabled(False)
            self._auto_selected_btn.setEnabled(False)
            return

        # Single-place: only valid for exactly one unplaced item.
        unplaced_items = [
            it for it in items
            if not (it.data(_COL_STATUS, Qt.ItemDataRole.UserRole) or False)
        ]
        self._place_btn.setEnabled(
            len(unplaced_items) == 1
            and self._item_uuid(unplaced_items[0]) is not None
        )
        self._auto_selected_btn.setEnabled(len(unplaced_items) > 0)

        # Highlight the first selected on the board.
        comp_id = self._item_uuid(items[0])
        if comp_id is not None:
            self.instance_selected.emit(comp_id)

    def _on_double_click(self, item: QTreeWidgetItem, _column: int) -> None:
        is_placed: bool = item.data(_COL_STATUS, Qt.ItemDataRole.UserRole) or False
        if is_placed:
            return
        comp_id = self._item_uuid(item)
        if comp_id is not None:
            self.place_requested.emit(comp_id)

    def _on_place_btn(self) -> None:
        items = self._tree.selectedItems()
        if not items:
            return
        comp_id = self._item_uuid(items[0])
        if comp_id is not None:
            self.place_requested.emit(comp_id)

    def _on_auto_place_selected(self) -> None:
        ids: list[uuid.UUID] = []
        for item in self._tree.selectedItems():
            is_placed: bool = item.data(_COL_STATUS, Qt.ItemDataRole.UserRole) or False
            if not is_placed:
                comp_id = self._item_uuid(item)
                if comp_id is not None:
                    ids.append(comp_id)
        if ids:
            self.auto_place_selected_requested.emit(ids)

    def _on_auto_place_all(self) -> None:
        if self._session is None:
            return
        placed_ids = set(self._session.layout.placements.keys())
        ids = [
            inst.id
            for inst in self._session.instances.values()
            if inst.id not in placed_ids
        ]
        if ids:
            self.auto_place_all_requested.emit(ids)

    @staticmethod
    def _item_uuid(item: QTreeWidgetItem) -> uuid.UUID | None:
        raw = item.data(_COL_REFDES, Qt.ItemDataRole.UserRole)
        if isinstance(raw, str):
            try:
                return uuid.UUID(raw)
            except ValueError:
                pass
        return None
