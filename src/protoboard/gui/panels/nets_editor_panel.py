# SPDX-License-Identifier: MIT
"""NetsEditorPanel — full net management: add, delete, rename, assign/unassign pins."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from protoboard.gui.session import EditorSession

# Column indices for the net list tree.
_NET_COL_NAME = 0
_NET_COL_PINS = 1
_NET_COL_COLOR = 2

# Column indices for the pins tree.
_PIN_COL_INST = 0
_PIN_COL_PIN = 1
_PIN_COL_NET = 2

_ROLE = Qt.ItemDataRole.UserRole


class NetsEditorPanel(QWidget):
    """Panel for managing logical nets: add, delete, rename, and assign pins."""

    nets_changed = Signal()  # emitted after any command that mutates the schematic

    def __init__(self, parent: object = None) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        root.addWidget(QLabel("Nets"))

        splitter = QSplitter(Qt.Orientation.Vertical)
        root.addWidget(splitter)

        # ── Top: net list ────────────────────────────────────────────────
        net_widget = QWidget()
        net_layout = QVBoxLayout(net_widget)
        net_layout.setContentsMargins(0, 0, 0, 0)
        net_layout.setSpacing(2)

        self._net_tree = QTreeWidget()
        self._net_tree.setHeaderLabels(["Net name", "Pins", "Color"])
        self._net_tree.setColumnWidth(_NET_COL_NAME, 120)
        self._net_tree.setColumnWidth(_NET_COL_PINS, 35)
        self._net_tree.setColumnWidth(_NET_COL_COLOR, 50)
        self._net_tree.setAlternatingRowColors(True)
        self._net_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._net_tree.customContextMenuRequested.connect(self._on_net_context_menu)
        self._net_tree.itemDoubleClicked.connect(self._on_net_double_clicked)
        net_layout.addWidget(self._net_tree)

        net_btn_row = QHBoxLayout()
        self._btn_add_net = QPushButton("Add net")
        self._btn_rename_net = QPushButton("Rename")
        self._btn_delete_net = QPushButton("Delete")
        self._btn_set_color = QPushButton("Color…")
        net_btn_row.addWidget(self._btn_add_net)
        net_btn_row.addWidget(self._btn_rename_net)
        net_btn_row.addWidget(self._btn_delete_net)
        net_btn_row.addWidget(self._btn_set_color)
        net_layout.addLayout(net_btn_row)
        splitter.addWidget(net_widget)

        # ── Bottom: pins list (all component pins) ───────────────────────
        pin_widget = QWidget()
        pin_layout = QVBoxLayout(pin_widget)
        pin_layout.setContentsMargins(0, 0, 0, 0)
        pin_layout.setSpacing(2)
        pin_layout.addWidget(QLabel("Component pins"))

        self._pin_tree = QTreeWidget()
        self._pin_tree.setHeaderLabels(["RefDes / Pin", "Pin name", "Net"])
        self._pin_tree.setColumnWidth(_PIN_COL_INST, 90)
        self._pin_tree.setColumnWidth(_PIN_COL_PIN, 80)
        self._pin_tree.setAlternatingRowColors(True)
        self._pin_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._pin_tree.customContextMenuRequested.connect(self._on_pin_context_menu)
        pin_layout.addWidget(self._pin_tree)

        pin_btn_row = QHBoxLayout()
        self._btn_assign = QPushButton("Assign to net…")
        self._btn_unassign = QPushButton("Unassign")
        pin_btn_row.addWidget(self._btn_assign)
        pin_btn_row.addWidget(self._btn_unassign)
        pin_layout.addLayout(pin_btn_row)
        splitter.addWidget(pin_widget)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)

        # Wire up buttons.
        self._btn_add_net.clicked.connect(self._on_add_net)
        self._btn_rename_net.clicked.connect(self._on_rename_net)
        self._btn_delete_net.clicked.connect(self._on_delete_net)
        self._btn_set_color.clicked.connect(self._on_set_color)
        self._btn_assign.clicked.connect(self._on_assign_pin)
        self._btn_unassign.clicked.connect(self._on_unassign_pin)

        self._session: EditorSession | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self, session: EditorSession) -> None:
        self._session = session
        self._rebuild()

    # ------------------------------------------------------------------
    # Rebuild helpers
    # ------------------------------------------------------------------

    def _rebuild(self) -> None:
        selected_net_id = self._current_net_id()
        self._rebuild_nets()
        self._rebuild_pins()
        # Try to restore net selection.
        if selected_net_id is not None:
            self._select_net_row(selected_net_id)

    def _rebuild_nets(self) -> None:
        self._net_tree.clear()
        if self._session is None:
            return
        for net in sorted(self._session.schematic.nets.values(), key=lambda n: n.name):
            color_label = net.color if net.color else "auto"
            item = QTreeWidgetItem([net.name, str(len(net.pins)), color_label])
            item.setData(_NET_COL_NAME, _ROLE, str(net.id))
            if net.color:
                bg = QColor(net.color)
                item.setBackground(_NET_COL_COLOR, QBrush(bg))
                # Use black or white text depending on luminance.
                lum = 0.299 * bg.redF() + 0.587 * bg.greenF() + 0.114 * bg.blueF()
                fg = QColor("#000000") if lum > 0.5 else QColor("#ffffff")
                item.setForeground(_NET_COL_COLOR, QBrush(fg))
            self._net_tree.addTopLevelItem(item)

    def _rebuild_pins(self) -> None:
        self._pin_tree.clear()
        if self._session is None:
            return
        session = self._session

        # Build a pin→net lookup.
        pin_to_net: dict[tuple[uuid.UUID, int], str] = {}
        for net in session.schematic.nets.values():
            for ref in net.pins:
                pin_to_net[(ref.component_id, ref.pin_number)] = net.name

        for inst_id, inst in sorted(
            session.instances.items(), key=lambda kv: kv[1].refdes
        ):
            defn = session.components.get(inst.definition_ref)
            pin_count = defn.pin_count if defn else 1
            parent_item = QTreeWidgetItem([inst.refdes, "", inst.value])
            parent_item.setFlags(parent_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self._pin_tree.addTopLevelItem(parent_item)
            for pin_num in range(1, pin_count + 1):
                pin_name = defn.pin_name(pin_num) if defn else f"P{pin_num}"
                net_name = pin_to_net.get((inst_id, pin_num), "—")
                pin_item = QTreeWidgetItem(["", pin_name, net_name])
                pin_item.setData(
                    0, _ROLE, f"{inst_id}:{pin_num}"
                )
                parent_item.addChild(pin_item)
            parent_item.setExpanded(True)

    def _select_net_row(self, net_id: uuid.UUID) -> None:
        for i in range(self._net_tree.topLevelItemCount()):
            item = self._net_tree.topLevelItem(i)
            if item and item.data(_NET_COL_NAME, _ROLE) == str(net_id):
                self._net_tree.setCurrentItem(item)
                return

    # ------------------------------------------------------------------
    # Current selection helpers
    # ------------------------------------------------------------------

    def _current_net_id(self) -> uuid.UUID | None:
        item = self._net_tree.currentItem()
        if item is None:
            return None
        raw = item.data(_NET_COL_NAME, _ROLE)
        if not isinstance(raw, str):
            return None
        try:
            return uuid.UUID(raw)
        except ValueError:
            return None

    def _current_pin_ref(self) -> tuple[uuid.UUID, int] | None:
        item = self._pin_tree.currentItem()
        if item is None:
            return None
        raw = item.data(0, _ROLE)
        if not isinstance(raw, str):
            return None
        try:
            comp_id_str, pin_str = raw.split(":")
            return uuid.UUID(comp_id_str), int(pin_str)
        except (ValueError, AttributeError):
            return None

    # ------------------------------------------------------------------
    # Net actions
    # ------------------------------------------------------------------

    def _on_add_net(self) -> None:
        if self._session is None:
            return
        name, ok = QInputDialog.getText(
            self,  # type: ignore[arg-type]
            "Add net",
            "Net name:",
        )
        if not ok or not name.strip():
            return
        name = name.strip()
        if self._name_exists(name):
            QMessageBox.warning(
                self,  # type: ignore[arg-type]
                "Duplicate name",
                f"A net named '{name}' already exists.",
            )
            return
        from protoboard.domain.commands.net_cmds import CreateNetCommand

        cmd = CreateNetCommand(name=name)
        self._session.stack.execute(cmd, self._session)
        self._rebuild()
        self.nets_changed.emit()

    def _on_rename_net(self) -> None:
        net_id = self._current_net_id()
        if net_id is None or self._session is None:
            return
        net = self._session.schematic.nets.get(net_id)
        if net is None:
            return
        name, ok = QInputDialog.getText(
            self,  # type: ignore[arg-type]
            "Rename net",
            "New name:",
            text=net.name,
        )
        if not ok or not name.strip():
            return
        name = name.strip()
        if name == net.name:
            return
        if self._name_exists(name):
            QMessageBox.warning(
                self,  # type: ignore[arg-type]
                "Duplicate name",
                f"A net named '{name}' already exists.",
            )
            return
        from protoboard.domain.commands.net_cmds import RenameNetCommand

        cmd = RenameNetCommand(net_id=net_id, new_name=name)
        self._session.stack.execute(cmd, self._session)
        self._rebuild()
        self.nets_changed.emit()

    def _on_delete_net(self) -> None:
        net_id = self._current_net_id()
        if net_id is None or self._session is None:
            return
        net = self._session.schematic.nets.get(net_id)
        if net is None:
            return
        if net.pins:
            ans = QMessageBox.question(
                self,  # type: ignore[arg-type]
                "Delete net",
                f"Net '{net.name}' has {len(net.pins)} assigned pin(s).\n"
                "Deleting it will unassign all pins. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
        from protoboard.domain.commands.net_cmds import DeleteNetCommand

        cmd = DeleteNetCommand(net_id=net_id)
        self._session.stack.execute(cmd, self._session)
        self._rebuild()
        self.nets_changed.emit()

    def _on_set_color(self) -> None:
        net_id = self._current_net_id()
        if net_id is None or self._session is None:
            return
        net = self._session.schematic.nets.get(net_id)
        if net is None:
            return
        initial = QColor(net.color) if net.color else QColor("#E03030")
        color = QColorDialog.getColor(
            initial,
            self,  # type: ignore[arg-type]
            f"Color for net '{net.name}'",
            QColorDialog.ColorDialogOption.DontUseNativeDialog,
        )
        if not color.isValid():
            return
        self._apply_color(net_id, color.name())

    def _on_clear_color(self) -> None:
        net_id = self._current_net_id()
        if net_id is None or self._session is None:
            return
        self._apply_color(net_id, None)

    def _apply_color(self, net_id: uuid.UUID, color: str | None) -> None:
        from protoboard.domain.commands.net_cmds import SetNetColorCommand

        cmd = SetNetColorCommand(net_id=net_id, new_color=color)
        self._session.stack.execute(cmd, self._session)  # type: ignore[union-attr]
        self._rebuild()
        self.nets_changed.emit()

    def _on_net_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        if column == _NET_COL_COLOR:
            self._net_tree.setCurrentItem(item)
            self._on_set_color()

    def _on_net_context_menu(self, pos: object) -> None:
        from PySide6.QtCore import QPoint

        assert isinstance(pos, QPoint)
        item = self._net_tree.itemAt(pos)
        if item is None:
            return
        self._net_tree.setCurrentItem(item)
        menu = QMenu(self)  # type: ignore[arg-type]
        menu.addAction("Rename", self._on_rename_net)
        menu.addAction("Set color…", self._on_set_color)
        menu.addAction("Clear color", self._on_clear_color)
        menu.addSeparator()
        menu.addAction("Delete", self._on_delete_net)
        menu.exec(self._net_tree.mapToGlobal(pos))

    # ------------------------------------------------------------------
    # Pin actions
    # ------------------------------------------------------------------

    def _on_assign_pin(self) -> None:
        pin_ref = self._current_pin_ref()
        if pin_ref is None or self._session is None:
            return
        comp_id, pin_num = pin_ref
        self._show_assign_dialog(comp_id, pin_num)

    def _on_unassign_pin(self) -> None:
        pin_ref = self._current_pin_ref()
        if pin_ref is None or self._session is None:
            return
        comp_id, pin_num = pin_ref
        from protoboard.domain.schematic.net import PinRef
        from protoboard.domain.commands.net_cmds import UnassignPinCommand

        ref = PinRef(component_id=comp_id, pin_number=pin_num)
        net = self._session.schematic.net_for_pin(ref)
        if net is None:
            return
        cmd = UnassignPinCommand(component_id=comp_id, pin_number=pin_num)
        self._session.stack.execute(cmd, self._session)
        self._rebuild()
        self.nets_changed.emit()

    def _on_pin_context_menu(self, pos: object) -> None:
        from PySide6.QtCore import QPoint

        assert isinstance(pos, QPoint)
        item = self._pin_tree.itemAt(pos)
        if item is None:
            return
        raw = item.data(0, _ROLE)
        if not isinstance(raw, str):
            return
        self._pin_tree.setCurrentItem(item)
        menu = QMenu(self)  # type: ignore[arg-type]
        menu.addAction("Assign to net…", self._on_assign_pin)
        menu.addAction("Unassign from net", self._on_unassign_pin)
        menu.exec(self._pin_tree.mapToGlobal(pos))

    def _show_assign_dialog(self, comp_id: uuid.UUID, pin_num: int) -> None:
        if self._session is None:
            return
        from protoboard.domain.schematic.net import PinRef

        current_net = self._session.schematic.net_for_pin(
            PinRef(component_id=comp_id, pin_number=pin_num)
        )
        nets = sorted(
            (n for n in self._session.schematic.nets.values()
             if current_net is None or n.id != current_net.id),
            key=lambda n: n.name,
        )
        if not nets:
            QMessageBox.information(
                self,  # type: ignore[arg-type]
                "No nets",
                "Create a net first before assigning pins.",
            )
            return
        dlg = _AssignNetDialog(
            nets=[(n.id, n.name) for n in nets],
            parent=self,  # type: ignore[arg-type]
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        target_net_id = dlg.chosen_net_id
        if target_net_id is None:
            return
        from protoboard.domain.commands.net_cmds import AssignPinToNetCommand

        cmd = AssignPinToNetCommand(
            component_id=comp_id,
            pin_number=pin_num,
            target_net_id=target_net_id,
        )
        self._session.stack.execute(cmd, self._session)
        self._rebuild()
        self.nets_changed.emit()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _name_exists(self, name: str) -> bool:
        if self._session is None:
            return False
        return any(
            n.name == name for n in self._session.schematic.nets.values()
        )


# ---------------------------------------------------------------------------
# _AssignNetDialog
# ---------------------------------------------------------------------------


class _AssignNetDialog(QDialog):
    """Simple dialog to pick a target net from a combo-box."""

    def __init__(
        self,
        nets: list[tuple[uuid.UUID, str]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Assign pin to net")
        self._nets = nets
        self.chosen_net_id: uuid.UUID | None = None

        layout = QFormLayout(self)
        self._combo = QComboBox()
        for _, name in nets:
            self._combo.addItem(name)
        layout.addRow("Net:", self._combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _on_accept(self) -> None:
        idx = self._combo.currentIndex()
        if 0 <= idx < len(self._nets):
            self.chosen_net_id = self._nets[idx][0]
        self.accept()
