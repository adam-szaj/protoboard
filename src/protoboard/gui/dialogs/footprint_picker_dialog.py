# SPDX-License-Identifier: MIT
"""Footprint pop-up — the first stage of footprint selection.

Opened from the library panel when placing a component that offers a real choice
(more than one footprint, or a single *kind* that needs parameterising). A search
box filters a list of options: recently-used first (prefixed ``★``), then the
component's footprints in declared order. The first row is pre-selected, so the
common case — re-use the last footprint — is a single confirm (double-click /
Enter / Space).

Confirming a *package* row finishes immediately. Confirming a *kind* row (or any
row with **Alt** held) opens the ``KindVariantDialog`` to pick / build a variant;
if that returns an address the pick is complete, otherwise this dialog stays
open. ``Esc`` cancels placement.

After ``exec()`` read :attr:`chosen_address` (``None`` on cancel). The dialog is
pure UI driven by a ``FootprintChoiceProvider``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLineEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from protoboard.gui.dialogs.kind_variant_dialog import KindVariantDialog

if TYPE_CHECKING:
    from protoboard.gui.footprint_choice import (
        FootprintChoiceProvider,
        PackageOptionView,
    )

_VIEW_ROLE = Qt.ItemDataRole.UserRole


class FootprintPickerDialog(QDialog):
    """Choose a footprint for a component; parameterise it when it is a kind."""

    def __init__(
        self,
        provider: FootprintChoiceProvider,
        recent: list[PackageOptionView],
        options: list[PackageOptionView],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Choose footprint")
        self._provider = provider
        self.chosen_address: str | None = None

        layout = QVBoxLayout(self)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search footprints…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_search)
        layout.addWidget(self._search)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        groups: dict[tuple[str, ...], QTreeWidgetItem] = {}

        def add_view(
            view: PackageOptionView,
            *,
            namespace: tuple[str, ...] | None = None,
            prefix: str = "",
        ) -> None:
            parent: QTreeWidgetItem | QTreeWidget = self._tree
            path: tuple[str, ...] = ()
            for segment in view.namespace if namespace is None else namespace:
                path += (segment,)
                group = groups.get(path)
                if group is None:
                    group = QTreeWidgetItem(parent, [segment])
                    group.setFlags(group.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                    groups[path] = group
                parent = group
            item = QTreeWidgetItem(parent, [f"{prefix}{view.label}"])
            item.setData(0, _VIEW_ROLE, view)

        if recent:
            for view in recent:
                add_view(view, namespace=("Recently used",), prefix="★ ")
        for view in options:
            add_view(view)
        self._tree.expandAll()
        first = self._first_visible_leaf()
        if first is not None:
            self._tree.setCurrentItem(first)
        self._tree.itemActivated.connect(self._confirm_item)
        self._tree.itemDoubleClicked.connect(self._confirm_item)
        layout.addWidget(self._tree)

    # -- interaction ---------------------------------------------------------

    def _on_search(self, text: str) -> None:
        q = text.strip().lower()

        def filter_item(item: QTreeWidgetItem) -> bool:
            view = item.data(0, _VIEW_ROLE)
            if view is not None:
                visible = (
                    not q or q in item.text(0).lower() or q in view.address.lower()
                )
            else:
                child_visibility = [
                    filter_item(child)
                    for index in range(item.childCount())
                    if (child := item.child(index)) is not None
                ]
                visible = any(child_visibility)
            item.setHidden(not visible)
            return visible

        for index in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(index)
            if item is not None:
                filter_item(item)
        first = self._first_visible_leaf()
        if first is not None:
            self._tree.setCurrentItem(first)

    def _first_visible_leaf(self) -> QTreeWidgetItem | None:
        def find(item: QTreeWidgetItem) -> QTreeWidgetItem | None:
            if item.isHidden():
                return None
            if item.data(0, _VIEW_ROLE) is not None:
                return item
            for index in range(item.childCount()):
                child = item.child(index)
                if child is None:
                    continue
                found = find(child)
                if found is not None:
                    return found
            return None

        for index in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(index)
            if item is None:
                continue
            found = find(item)
            if found is not None:
                return found
        return None

    def keyPressEvent(self, event: object) -> None:  # noqa: N802 (Qt override)
        # Space confirms the current row (Enter is delivered as itemActivated).
        key = event.key()  # type: ignore[attr-defined]
        current = self._tree.currentItem()
        if key == Qt.Key.Key_Space and current is not None:
            self._confirm_item(current)
            return
        super().keyPressEvent(event)  # type: ignore[arg-type]

    def _confirm_item(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        view: PackageOptionView | None = item.data(0, _VIEW_ROLE)
        if view is None:
            return
        alt = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.AltModifier)
        # A kind always parameterises; Alt forces the variant pop-up even on a
        # recent (already-built) pick, to derive a new variant of the same kind.
        if view.is_kind or alt:
            built = self._run_variant_dialog(view.address)
            if built is None:
                return  # cancelled the second stage; stay open
            self.chosen_address = built
            self.accept()
            return
        self.chosen_address = view.address
        self.accept()

    def _run_variant_dialog(self, address: str) -> str | None:
        dialog = KindVariantDialog(self._provider, address, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            return dialog.chosen_address
        return None
