# SPDX-License-Identifier: MIT
"""GroupsPanel — tree view of component groups with rename and disband actions."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from protoboard.domain.layout.group import ComponentGroup
    from protoboard.gui.session import EditorSession


class GroupsPanel(QWidget):
    """Lists all component groups in a tree; supports rename and disband."""

    group_selected = Signal(uuid.UUID)  # emitted when user clicks a group row
    group_disbanded = Signal(uuid.UUID)  # emitted after DisbandGroupCommand executes
    group_renamed = Signal(uuid.UUID)  # emitted after RenameGroupCommand executes

    def __init__(self, parent: object = None) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        layout.addWidget(QLabel("Groups"))

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Name", "Components"])
        self._tree.setColumnWidth(0, 140)
        self._tree.setAlternatingRowColors(True)
        layout.addWidget(self._tree)

        btn_row = QHBoxLayout()
        self._btn_rename = QPushButton("Rename")
        self._btn_disband = QPushButton("Disband")
        btn_row.addWidget(self._btn_rename)
        btn_row.addWidget(self._btn_disband)
        layout.addLayout(btn_row)

        self._tree.itemClicked.connect(self._on_item_clicked)
        self._btn_rename.clicked.connect(self._on_rename)
        self._btn_disband.clicked.connect(self._on_disband)

        self._session: EditorSession | None = None

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def refresh(self, session: EditorSession) -> None:
        """Rebuild tree from *session.layout.groups*."""
        from protoboard.domain.layout.layout import ROOT_GROUP_ID

        self._session = session
        self._tree.clear()

        groups = session.layout.groups
        # Exclude root group from display.
        visible = {gid: g for gid, g in groups.items() if gid != ROOT_GROUP_ID}
        if not visible:
            return

        # Build a map: group_id → item, for wiring parent↔child.
        items: dict[uuid.UUID, QTreeWidgetItem] = {}
        child_ids: set[uuid.UUID] = set()
        for g in visible.values():
            child_ids.update(g.child_group_ids)

        for gid, grp in visible.items():
            items[gid] = self._make_item(grp, session)

        # Wire children under parents.
        for gid, grp in visible.items():
            for cid in grp.child_group_ids:
                if cid in items:
                    items[gid].addChild(items[cid])

        # Add top-level groups (those not referenced as children of visible groups).
        for gid in visible:
            if gid not in child_ids:
                self._tree.addTopLevelItem(items[gid])

        self._tree.expandAll()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _make_item(
        self, grp: ComponentGroup, session: EditorSession
    ) -> QTreeWidgetItem:
        total = len(session.group_tree().all_component_ids(grp.id))
        exported = len(grp.exported_pins)
        col1 = f"{total} ({exported} exp.)" if exported else str(total)
        item = QTreeWidgetItem([grp.name or "(unnamed)", col1])
        item.setData(0, Qt.ItemDataRole.UserRole, grp.id)
        return item

    def _selected_group_id(self) -> uuid.UUID | None:
        items = self._tree.selectedItems()
        if not items:
            return None
        val = items[0].data(0, Qt.ItemDataRole.UserRole)
        return uuid.UUID(str(val)) if val is not None else None

    def _on_item_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        gid = item.data(0, Qt.ItemDataRole.UserRole)
        if gid is not None:
            self.group_selected.emit(gid)

    def _on_rename(self) -> None:
        from protoboard.domain.commands.group_cmds import RenameGroupCommand

        gid = self._selected_group_id()
        if gid is None or self._session is None:
            return
        old_name = self._session.layout.groups[gid].name
        new_name, ok = QInputDialog.getText(
            self, "Rename Group", "New name:", text=old_name
        )
        if not ok or not new_name.strip() or new_name.strip() == old_name:
            return
        cmd = RenameGroupCommand(group_id=gid, new_name=new_name.strip())
        self._session.edit_controller.execute_command(cmd)
        self.refresh(self._session)
        self.group_renamed.emit(gid)

    def _on_disband(self) -> None:
        gid = self._selected_group_id()
        if gid is None or self._session is None:
            return
        grp = self._session.layout.groups.get(gid)
        if grp is None:
            return
        reply = QMessageBox.question(
            self,
            "Disband Group",
            f"Disband group '{grp.name}'? Components remain placed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._session.edit_controller.get_group(gid).delete()
        self.refresh(self._session)
        self.group_disbanded.emit(gid)
