# SPDX-License-Identifier: MIT
"""NetlistPanel — tree view of schematic instances with placement status."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QComboBox,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from protoboard.gui.panels.tree_panel_mixin import TreePanelMixin

if TYPE_CHECKING:
    from protoboard.gui.session import EditorSession

_PLACED_COLOR = QColor("#1A8C1A")
_UNPLACED_COLOR = QColor("#AAAAAA")
_PLACED_MARK = "✓"
_UNPLACED_MARK = "✗"

_MODES = [
    "Symbol → Instances",
    "Net → Symbol → Instances",
    "Flat (by RefDes)",
]


class NetlistPanel(TreePanelMixin, QWidget):
    """Browsable tree of component instances from the loaded schematic."""

    instance_selected = Signal(uuid.UUID)

    def __init__(self, parent: object = None) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        layout.addWidget(QLabel("Netlist / BOM"))

        self._mode_combo = QComboBox()
        self._mode_combo.addItems(_MODES)
        layout.addWidget(self._mode_combo)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Component", "Status", "Value"])
        self._tree.setColumnWidth(0, 120)
        self._tree.setColumnWidth(1, 30)
        self._tree.setAlternatingRowColors(True)
        layout.addWidget(self._tree)

        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self._tree.itemClicked.connect(self._on_item_clicked)

    def _rebuild(self) -> None:
        self._tree.clear()
        if self._session is None:
            return
        session = self._session
        mode = self._mode_combo.currentIndex()
        if mode == 0:
            self._build_symbol_mode(session)
        elif mode == 1:
            self._build_net_mode(session)
        else:
            self._build_flat_mode(session)
        self._tree.expandAll()

    def _on_mode_changed(self) -> None:
        self._rebuild()

    def _on_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        if self._syncing:
            return
        comp_id_str = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(comp_id_str, str):
            try:
                self.instance_selected.emit(uuid.UUID(comp_id_str))
            except ValueError:
                pass

    # ------------------------------------------------------------------
    # Tree builders
    # ------------------------------------------------------------------

    def _build_symbol_mode(self, session: EditorSession) -> None:
        """Symbol → Instances: group by definition_ref."""
        groups: dict[str, list[uuid.UUID]] = {}
        for inst_id, inst in sorted(
            session.instances.items(), key=lambda kv: kv[1].definition_ref
        ):
            groups.setdefault(inst.definition_ref, []).append(inst_id)

        for def_ref, inst_ids in sorted(groups.items()):
            count = len(inst_ids)
            cat = self._category(session, def_ref)
            label = f"{def_ref}" + (f"  [{cat}]" if cat else "")
            group_item = QTreeWidgetItem([f"{label}  ({count})", "", ""])
            group_item.setFlags(group_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self._tree.addTopLevelItem(group_item)
            for inst_id in sorted(inst_ids, key=lambda i: session.instances[i].refdes):
                group_item.addChild(self._make_instance_leaf(session, inst_id))

    def _build_net_mode(self, session: EditorSession) -> None:
        """Net → Symbol → Instances."""
        for net in sorted(session.schematic.nets.values(), key=lambda n: n.name):
            net_item = QTreeWidgetItem([net.name, "", ""])
            net_item.setFlags(net_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self._tree.addTopLevelItem(net_item)

            # Group pins by definition_ref within this net.
            by_sym: dict[str, list[uuid.UUID]] = {}
            for ref in net.pins:
                inst = session.instances.get(ref.component_id)
                if inst is None:
                    continue
                by_sym.setdefault(inst.definition_ref, []).append(ref.component_id)

            for def_ref, comp_ids in sorted(by_sym.items()):
                sym_item = QTreeWidgetItem([def_ref, "", ""])
                sym_item.setFlags(sym_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                net_item.addChild(sym_item)
                seen: set[uuid.UUID] = set()
                for comp_id in comp_ids:
                    if comp_id in seen:
                        continue
                    seen.add(comp_id)
                    sym_item.addChild(self._make_instance_leaf(session, comp_id))

        # Instances not in any net.
        unassigned = [
            inst_id
            for inst_id in session.instances
            if not any(
                any(ref.component_id == inst_id for ref in net.pins)
                for net in session.schematic.nets.values()
            )
        ]
        if unassigned:
            other = QTreeWidgetItem(["(no net)", "", ""])
            other.setFlags(other.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self._tree.addTopLevelItem(other)
            for inst_id in sorted(
                unassigned, key=lambda i: session.instances[i].refdes
            ):
                other.addChild(self._make_instance_leaf(session, inst_id))

    def _build_flat_mode(self, session: EditorSession) -> None:
        """Flat alphabetical list by refdes."""
        for inst_id, inst in sorted(
            session.instances.items(), key=lambda kv: kv[1].refdes
        ):
            leaf = self._make_instance_leaf(session, inst_id)
            self._tree.addTopLevelItem(leaf)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_instance_leaf(
        self, session: EditorSession, inst_id: uuid.UUID
    ) -> QTreeWidgetItem:
        inst = session.instances[inst_id]
        placed = inst_id in session.layout.placements
        mark = _PLACED_MARK if placed else _UNPLACED_MARK
        color = _PLACED_COLOR if placed else _UNPLACED_COLOR

        item = QTreeWidgetItem([inst.refdes, mark, inst.value])
        item.setForeground(0, QBrush(color))
        item.setForeground(1, QBrush(color))
        item.setForeground(2, QBrush(color))
        item.setTextAlignment(1, Qt.AlignmentFlag.AlignCenter)
        # Store UUID for click handler.
        item.setData(0, Qt.ItemDataRole.UserRole, str(inst_id))
        return item

    def _category(self, session: EditorSession, def_ref: str) -> str:
        defn = session.components.get(def_ref)
        return defn.component_class if defn is not None else ""
