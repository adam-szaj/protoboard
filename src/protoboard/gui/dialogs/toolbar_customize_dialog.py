# SPDX-License-Identifier: MIT
"""Customize Toolbars dialog — edits ``~/.config/protoboard/toolbars.yaml``.

The dialog is the only GUI affordance the user has for shaping the
toolbar layout.  Direct YAML edits also work (a power-user fast path)
because the file is plain.

UI shape:

* One tab per :data:`protoboard.gui.actions.ToolbarGroup`.
* Each tab has two QListWidgets side by side:
  - **Available**: every spec id NOT currently on this toolbar.
    Read from :data:`ALL_ACTIONS` so removed-but-still-in-spec actions
    can be added back at any time.
  - **Current**: spec ids on this toolbar, in display order.  The user
    drags entries between lists, reorders with Up/Down buttons, and
    confirms with OK.

* Bottom buttons: Reset to Defaults, Cancel, OK.  Reset reloads the
  shipped ``default_toolbars.yaml`` and discards user edits.  OK
  writes the per-tab state back to the user file via
  ``save_toolbar_config``.

The dialog never touches the live QToolBars itself — the host
``MainWindow`` rebuilds them after the dialog accepts (see
``MainWindow._reload_toolbar_layout``).  Pulling that separation apart
keeps the dialog testable without a fully wired MainWindow.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast, get_args

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from protoboard.gui.action_builder import ActionRegistry
from protoboard.gui.actions import ALL_ACTIONS, ActionSpec, ToolbarGroup, strip_mnemonic
from protoboard.gui.toolbar_config import (
    load_default_toolbar_config,
    load_toolbar_config,
    save_toolbar_config,
)


def _spec_label(spec: ActionSpec) -> str:
    """Plain-text label for list display (mnemonic ``&`` stripped)."""
    return strip_mnemonic(spec.label)


class ToolbarCustomizeDialog(QDialog):
    """Modal dialog that edits the toolbar layout YAML.

    Args:
        main_window: Used as parent and as object source for action
            label/icon lookups.
        registry: The active :class:`ActionRegistry`.  Used as a fallback
            source of action labels (in case the spec list rotated).
    """

    def __init__(
        self,
        *,
        main_window: QMainWindow,  # kept for API symmetry; future hooks may need it
        registry: ActionRegistry,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        _ = main_window  # currently unused; reserved for icon-source extension
        self.setWindowTitle("Customize Toolbars")
        self.setModal(True)
        self.resize(720, 480)

        self._registry = registry
        self._spec_by_id: dict[str, ActionSpec] = {s.id: s for s in ALL_ACTIONS}
        self._groups: tuple[ToolbarGroup, ...] = tuple(
            cast(ToolbarGroup, g) for g in get_args(ToolbarGroup)
        )
        # Whether the user actually committed a change; the host MainWindow
        # only rebuilds toolbars when this is True.
        self.changed: bool = False

        layout = load_toolbar_config()

        root = QVBoxLayout(self)
        intro = QLabel(
            "Drag actions between Available and Current to customise each "
            "toolbar.  Use Up/Down to reorder.  Empty toolbars are hidden.\n"
            "Reset restores the layout shipped with the application."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        self._tabs = QTabWidget(self)
        # Per-tab pair of (current, available) list widgets — populated below.
        self._tab_widgets: dict[ToolbarGroup, tuple[QListWidget, QListWidget]] = {}
        for group in self._groups:
            tab = self._build_tab(group, layout.get(group, []))
            self._tabs.addTab(tab, group.capitalize())
        root.addWidget(self._tabs, stretch=1)

        button_row = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self._reset_btn = QPushButton("Reset to Defaults", self)
        button_row.addButton(
            self._reset_btn,
            QDialogButtonBox.ButtonRole.ResetRole,
        )
        button_row.accepted.connect(self._on_accept)
        button_row.rejected.connect(self.reject)
        self._reset_btn.clicked.connect(self._on_reset)
        root.addWidget(button_row)

    # ------------------------------------------------------------------
    # Tab construction
    # ------------------------------------------------------------------

    def _build_tab(
        self,
        group: ToolbarGroup,
        current_ids: Sequence[str],
    ) -> QWidget:
        """Build the two-pane editor for one toolbar group."""
        tab = QWidget(self)
        outer = QHBoxLayout(tab)

        # ---- Left: Available actions (not currently on this toolbar) ----
        left_box = QVBoxLayout()
        left_box.addWidget(QLabel("Available"))
        available = QListWidget(tab)
        available.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        left_box.addWidget(available)

        # ---- Middle: add/remove arrows ----
        mid_box = QVBoxLayout()
        mid_box.addStretch(1)
        add_btn = QPushButton("→", tab)
        remove_btn = QPushButton("←", tab)
        add_btn.setMaximumWidth(40)
        remove_btn.setMaximumWidth(40)
        mid_box.addWidget(add_btn)
        mid_box.addWidget(remove_btn)
        mid_box.addStretch(1)

        # ---- Right: Current items on this toolbar (ordered) ----
        right_box = QVBoxLayout()
        right_box.addWidget(QLabel("On this toolbar (top → bottom = left → right)"))
        current = QListWidget(tab)
        current.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        right_box.addWidget(current)

        # ---- Reorder buttons under Current ----
        reorder_row = QHBoxLayout()
        up_btn = QPushButton("Up", tab)
        down_btn = QPushButton("Down", tab)
        reorder_row.addWidget(up_btn)
        reorder_row.addWidget(down_btn)
        reorder_row.addStretch(1)
        right_box.addLayout(reorder_row)

        outer.addLayout(left_box, stretch=2)
        outer.addLayout(mid_box, stretch=0)
        outer.addLayout(right_box, stretch=2)

        # ---- Populate lists from layout / spec list ----
        current_set: set[str] = set()
        for aid in current_ids:
            spec = self._spec_by_id.get(aid)
            if spec is None:
                continue  # unknown id — skip; warning already logged by loader
            item = QListWidgetItem(_spec_label(spec))
            item.setData(Qt.ItemDataRole.UserRole, aid)
            current.addItem(item)
            current_set.add(aid)
        for spec in ALL_ACTIONS:
            if spec.id in current_set:
                continue
            item = QListWidgetItem(_spec_label(spec))
            item.setData(Qt.ItemDataRole.UserRole, spec.id)
            available.addItem(item)

        # ---- Wire buttons ----
        add_btn.clicked.connect(lambda: self._move_item(available, current))
        remove_btn.clicked.connect(lambda: self._move_item(current, available))
        up_btn.clicked.connect(lambda: self._reorder(current, -1))
        down_btn.clicked.connect(lambda: self._reorder(current, +1))

        self._tab_widgets[group] = (current, available)
        return tab

    # ------------------------------------------------------------------
    # List manipulation
    # ------------------------------------------------------------------

    @staticmethod
    def _move_item(src: QListWidget, dst: QListWidget) -> None:
        """Take the currently selected row from ``src`` and append it
        at the end of ``dst``.  No-op when nothing is selected."""
        row = src.currentRow()
        if row < 0:
            return
        item = src.takeItem(row)
        if item is None:
            return
        dst.addItem(item)
        dst.setCurrentRow(dst.count() - 1)

    @staticmethod
    def _reorder(widget: QListWidget, delta: int) -> None:
        """Swap the selected row with its ``delta`` neighbour."""
        row = widget.currentRow()
        if row < 0:
            return
        new_row = row + delta
        if not (0 <= new_row < widget.count()):
            return
        item = widget.takeItem(row)
        if item is None:
            return
        widget.insertItem(new_row, item)
        widget.setCurrentRow(new_row)

    # ------------------------------------------------------------------
    # Reset / Accept
    # ------------------------------------------------------------------

    def _on_reset(self) -> None:
        """Reload the shipped defaults into every tab.  Discards any
        unsaved edits the user made in this dialog session."""
        confirm = QMessageBox.question(
            self,
            "Reset Toolbars",
            "Restore the default toolbar layout?  All your toolbar "
            "customisations will be lost.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        defaults = load_default_toolbar_config()
        for group, (current, available) in self._tab_widgets.items():
            current.clear()
            available.clear()
            current_ids = defaults.get(group, [])
            current_set: set[str] = set()
            for aid in current_ids:
                spec = self._spec_by_id.get(aid)
                if spec is None:
                    continue
                item = QListWidgetItem(_spec_label(spec))
                item.setData(Qt.ItemDataRole.UserRole, aid)
                current.addItem(item)
                current_set.add(aid)
            for spec in ALL_ACTIONS:
                if spec.id in current_set:
                    continue
                item = QListWidgetItem(_spec_label(spec))
                item.setData(Qt.ItemDataRole.UserRole, spec.id)
                available.addItem(item)

    def _on_accept(self) -> None:
        """Collect per-tab current lists and persist via save_toolbar_config."""
        layout: dict[ToolbarGroup, list[str]] = {}
        for group, (current, _available) in self._tab_widgets.items():
            ids: list[str] = []
            for row in range(current.count()):
                item = current.item(row)
                if item is None:
                    continue
                aid = item.data(Qt.ItemDataRole.UserRole)
                if isinstance(aid, str):
                    ids.append(aid)
            layout[group] = ids
        save_toolbar_config(layout)
        self.changed = True
        self.accept()


__all__ = ["ToolbarCustomizeDialog"]
