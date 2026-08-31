# SPDX-License-Identifier: MIT
"""NetsEditorPanel — full net management: add, delete, rename, assign/unassign pins."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, cast

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
    from protoboard.domain.commands.base import Command, CommandStack
    from protoboard.domain.commands.net_cmds import NetState
    from protoboard.domain.schematic.net import Net
    from protoboard.gui.session import EditorSession

# Column indices for the net list tree.
_NET_COL_NAME = 0
_NET_COL_PINS = 1
_NET_COL_COLOR = 2
_NET_COL_GLOBAL = 3

# Column indices for the pins tree.
_PIN_COL_INST = 0
_PIN_COL_PIN = 1
_PIN_COL_NET = 2

_ROLE = Qt.ItemDataRole.UserRole


class NetsEditorPanel(QWidget):
    """Panel for managing logical nets: add, delete, rename, and assign pins."""

    nets_changed = Signal()  # emitted after any command that mutates the schematic
    net_selected = Signal(object)  # uuid.UUID — emitted when user clicks a net row

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
        self._net_tree.setHeaderLabels(["Net name", "Pins", "Color", "Global"])
        self._net_tree.setColumnWidth(_NET_COL_NAME, 110)
        self._net_tree.setColumnWidth(_NET_COL_PINS, 35)
        self._net_tree.setColumnWidth(_NET_COL_COLOR, 50)
        self._net_tree.setColumnWidth(_NET_COL_GLOBAL, 50)
        self._net_tree.setAlternatingRowColors(True)
        self._net_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._net_tree.customContextMenuRequested.connect(self._on_net_context_menu)
        self._net_tree.itemDoubleClicked.connect(self._on_net_double_clicked)
        self._net_tree.itemClicked.connect(self._on_net_item_clicked)
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
        self._pin_tree.itemClicked.connect(self._on_pin_item_clicked)
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
        self._syncing: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sync_selection(
        self,
        comp_ids: object,
        _wire_ids: object,
        net_id: object,
    ) -> None:
        """Board -> panel: select the net row (by net_id) and the Component-Pin
        rows of every selected component (by comp_ids).

        A component click lights its pin rows; a wire click lights the net row;
        a pin click does both (the request carries the component id AND the
        pin's net).  Runs under ``_syncing`` so the resulting ``setSelected``
        does not echo back out as a panel->board click.
        """
        if not self.isVisible():
            return
        self._syncing = True
        try:
            self._net_tree.clearSelection()
            if isinstance(net_id, uuid.UUID):
                self._select_net_row(net_id)
            self._pin_tree.clearSelection()
            cid_strs = {
                str(c)
                for c in (comp_ids if isinstance(comp_ids, frozenset) else frozenset())
            }
            first: QTreeWidgetItem | None = None
            for i in range(self._pin_tree.topLevelItemCount()):
                parent = self._pin_tree.topLevelItem(i)
                for j in range(parent.childCount() if parent else 0):
                    assert parent is not None
                    child = parent.child(j)
                    raw = child.data(0, _ROLE) if child is not None else None
                    if (
                        child is not None
                        and isinstance(raw, str)
                        and raw.split(":", 1)[0] in cid_strs
                    ):
                        child.setSelected(True)
                        if first is None:
                            first = child
            if first is not None:
                self._pin_tree.scrollToItem(first)
        finally:
            self._syncing = False

    def _on_net_item_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        if self._syncing:
            return
        import uuid as _uuid

        raw = item.data(_NET_COL_NAME, _ROLE)
        if isinstance(raw, str):
            try:
                self.net_selected.emit(_uuid.UUID(raw))
            except ValueError:
                pass

    def _on_pin_item_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        """Panel -> board: a Component-Pin row click selects the pin on the board.

        Reads the clicked item directly (not ``currentItem``, which Qt updates
        only after this slot) and routes through the session hub, which lights
        the pin's net AND focuses its component.
        """
        if self._syncing or self._session is None:
            return
        raw = item.data(0, _ROLE)
        if not isinstance(raw, str):
            return
        try:
            comp_id_str, pin_str = raw.split(":", 1)
            comp_id, pin_num = uuid.UUID(comp_id_str), int(pin_str)
        except (ValueError, AttributeError):
            return
        self._session.request_select_pin(comp_id, pin_num)

    def refresh(self, session: EditorSession) -> None:
        if self._session is not session:
            # Hook the autoroute gate and the board->panel selection feed exactly
            # once per session — subsequent same-session refresh() calls no-op
            # here.  On a session swap, drop the old session's selection hook.
            if self._session is not None:
                try:
                    self._session.selection_changed.disconnect(
                        self._on_session_selection
                    )
                except (TypeError, RuntimeError):
                    pass
            try:
                session.autoroute_active_changed.connect(self._on_autoroute_active)
                session.selection_changed.connect(self._on_session_selection)
            except (TypeError, RuntimeError):
                pass
        self._session = session
        self._on_autoroute_active(session.autoroute_active)
        self._rebuild()

    def _on_session_selection(
        self, comp_ids: object, wire_ids: object, net_id: object
    ) -> None:
        """Board -> panel feed (this panel is not a TreePanelMixin)."""
        self.sync_selection(comp_ids, wire_ids, net_id)

    def _on_autoroute_active(self, active: bool) -> None:
        """Disable structural net edits while autoroute is running.

        Color and rename buttons stay enabled — those commands are
        marked ``allowed_during_autoroute = True`` on the command class
        so the user can keep tidying the netlist mid-route.
        """
        enabled = not active
        self._btn_add_net.setEnabled(enabled)
        self._btn_delete_net.setEnabled(enabled)
        self._btn_assign.setEnabled(enabled)
        self._btn_unassign.setEnabled(enabled)

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
        # Resolve every net to its effective colour (manual override, palette,
        # or golden-ratio extension) so the panel shows exactly what the
        # board renderer paints — not just the manual override.
        from protoboard.controller.view_options.theme import DEFAULT_THEME
        from protoboard.gui.renderers.net_color import build_net_color_map

        theme = getattr(self._session, "theme", DEFAULT_THEME) or DEFAULT_THEME
        effective = build_net_color_map(self._session.schematic.nets, theme=theme)
        for net in sorted(self._session.schematic.nets.values(), key=lambda n: n.name):
            eff_hex = effective.get(net.id)
            item = self._make_net_tree_item(net, eff_hex)
            self._net_tree.addTopLevelItem(item)

    def _make_net_tree_item(self, net: Net, eff_hex: str | None) -> QTreeWidgetItem:
        """Build the styled net row from its effective colour.

        ``eff_hex`` is the renderer's effective colour for ``net`` (palette or
        golden-ratio extension); a manual ``net.color`` override takes
        precedence for both the label and the background swatch.
        """
        if net.color:
            color_label = net.color
        elif eff_hex:
            color_label = f"{eff_hex} (auto)"
        else:
            color_label = "auto"
        global_label = "✓" if net.global_net else ""
        item = QTreeWidgetItem(
            [net.name, str(len(net.pins)), color_label, global_label]
        )
        item.setData(_NET_COL_NAME, _ROLE, str(net.id))
        bg_hex = net.color or eff_hex
        if bg_hex:
            bg = QColor(bg_hex)
            item.setBackground(_NET_COL_COLOR, QBrush(bg))
            # Use black or white text depending on luminance.
            lum = 0.299 * bg.redF() + 0.587 * bg.greenF() + 0.114 * bg.blueF()
            fg = QColor("#000000") if lum > 0.5 else QColor("#ffffff")
            item.setForeground(_NET_COL_COLOR, QBrush(fg))
        if net.global_net:
            item.setForeground(_NET_COL_GLOBAL, QBrush(QColor("#888888")))
        return item

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
            pin_count = len(defn.meta_pins) if defn else 1
            parent_item = QTreeWidgetItem([inst.refdes, "", inst.value])
            parent_item.setFlags(parent_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self._pin_tree.addTopLevelItem(parent_item)
            for pin_num in range(1, pin_count + 1):
                pin_name = (
                    " / ".join(defn.meta_pin(pin_num).labels) if defn else f"P{pin_num}"
                )
                net_name = pin_to_net.get((inst_id, pin_num), "—")
                pin_item = QTreeWidgetItem(["", pin_name, net_name])
                pin_item.setData(0, _ROLE, f"{inst_id}:{pin_num}")
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
    # Command dispatch
    # ------------------------------------------------------------------

    def _execute_net_command(self, cmd: Command[NetState]) -> None:
        """Run a net-management command against the active session's stack.

        ``session.stack`` is declared as ``CommandStack[ComponentState]`` while
        net commands are ``Command[NetState]``; the runtime ``EditorSession``
        satisfies both protocols, so viewing the stack as ``CommandStack[NetState]``
        is sound. Callers must hold the ``self._session is not None`` invariant.
        """
        assert self._session is not None
        stack = cast("CommandStack[NetState]", self._session.stack)
        stack.execute(cmd, cast("NetState", self._session))

    # ------------------------------------------------------------------
    # Net actions
    # ------------------------------------------------------------------

    def _on_add_net(self) -> None:
        if self._session is None:
            return
        name, ok = QInputDialog.getText(
            self,
            "Add net",
            "Net name:",
        )
        if not ok or not name.strip():
            return
        name = name.strip()
        if self._name_exists(name):
            QMessageBox.warning(
                self,
                "Duplicate name",
                f"A net named '{name}' already exists.",
            )
            return
        from protoboard.domain.commands.net_cmds import CreateNetCommand

        cmd = CreateNetCommand(name=name)
        self._execute_net_command(cmd)
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
            self,
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
                self,
                "Duplicate name",
                f"A net named '{name}' already exists.",
            )
            return
        from protoboard.domain.commands.net_cmds import RenameNetCommand

        cmd = RenameNetCommand(net_id=net_id, new_name=name)
        self._execute_net_command(cmd)
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
                self,
                "Delete net",
                f"Net '{net.name}' has {len(net.pins)} assigned pin(s).\n"
                "Deleting it will unassign all pins. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
        from protoboard.domain.commands.net_cmds import DeleteNetCommand

        cmd = DeleteNetCommand(net_id=net_id)
        self._execute_net_command(cmd)
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
            self,
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
        self._execute_net_command(cmd)
        self._rebuild()
        self.nets_changed.emit()

    def _on_toggle_global(self) -> None:
        net_id = self._current_net_id()
        if net_id is None or self._session is None:
            return
        net = self._session.schematic.nets.get(net_id)
        if net is None:
            return
        from protoboard.domain.commands.net_cmds import SetNetGlobalCommand

        cmd = SetNetGlobalCommand(net_id=net_id, global_net=not net.global_net)
        self._execute_net_command(cmd)
        self._rebuild()
        self.nets_changed.emit()

    def _on_net_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        if column == _NET_COL_COLOR:
            self._net_tree.setCurrentItem(item)
            self._on_set_color()
        elif column == _NET_COL_GLOBAL:
            self._net_tree.setCurrentItem(item)
            self._on_toggle_global()

    def _on_net_context_menu(self, pos: object) -> None:
        from PySide6.QtCore import QPoint

        assert isinstance(pos, QPoint)
        item = self._net_tree.itemAt(pos)
        if item is None:
            return
        self._net_tree.setCurrentItem(item)
        net_id = self._current_net_id()
        net = (
            self._session.schematic.nets.get(net_id)
            if (net_id and self._session)
            else None
        )
        global_label = (
            "Unmark as global" if (net and net.global_net) else "Mark as global"
        )

        menu = QMenu(self)
        menu.addAction("Rename", self._on_rename_net)
        menu.addAction("Set color…", self._on_set_color)
        menu.addAction("Clear color", self._on_clear_color)
        menu.addAction(global_label, self._on_toggle_global)
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
        from protoboard.domain.commands.net_cmds import UnassignPinCommand
        from protoboard.domain.schematic.net import PinRef

        ref = PinRef(component_id=comp_id, pin_number=pin_num)
        net = self._session.schematic.net_for_pin(ref)
        if net is None:
            return
        cmd = UnassignPinCommand(component_id=comp_id, pin_number=pin_num)
        self._execute_net_command(cmd)
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
        menu = QMenu(self)
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
            (
                n
                for n in self._session.schematic.nets.values()
                if current_net is None or n.id != current_net.id
            ),
            key=lambda n: n.name,
        )
        if not nets:
            # No net to assign to — offer to create one inline instead of a
            # dead end.  Common for a free pin in a project with no nets yet.
            self._create_net_then_assign(comp_id, pin_num)
            return
        dlg = _AssignNetDialog(
            nets=[(n.id, n.name) for n in nets],
            parent=self,
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
        self._execute_net_command(cmd)
        self._rebuild()
        self.nets_changed.emit()

    def _create_net_then_assign(self, comp_id: uuid.UUID, pin_num: int) -> None:
        """Prompt for a new net name, create it, then assign the pin to it.

        Used when there is no net to assign to — turns the previous dead end
        ("create a net first") into a one-step flow.
        """
        if self._session is None:
            return
        ans = QMessageBox.question(
            self,
            "No nets",
            "There are no nets yet. Create one now and assign this pin to it?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        name, ok = QInputDialog.getText(
            self,
            "New net",
            "Net name:",
        )
        if not ok or not name.strip():
            return
        name = name.strip()
        if self._name_exists(name):
            QMessageBox.warning(
                self,
                "Duplicate name",
                f"A net named '{name}' already exists.",
            )
            return
        from protoboard.domain.commands.net_cmds import (
            AssignPinToNetCommand,
            CreateNetCommand,
        )

        create = CreateNetCommand(name=name)
        self._execute_net_command(create)
        assign = AssignPinToNetCommand(
            component_id=comp_id,
            pin_number=pin_num,
            target_net_id=create.net_id,
        )
        self._execute_net_command(assign)
        self._rebuild()
        self.nets_changed.emit()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _name_exists(self, name: str) -> bool:
        if self._session is None:
            return False
        return any(n.name == name for n in self._session.schematic.nets.values())


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
