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
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from protoboard.gui.dialogs.kind_variant_dialog import KindVariantDialog

if TYPE_CHECKING:
    from protoboard.gui.footprint_choice import (
        FootprintChoiceProvider,
        FootprintOptionView,
    )

_VIEW_ROLE = Qt.ItemDataRole.UserRole


class FootprintPickerDialog(QDialog):
    """Choose a footprint for a component; parameterise it when it is a kind."""

    def __init__(
        self,
        provider: FootprintChoiceProvider,
        recent: list[FootprintOptionView],
        options: list[FootprintOptionView],
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

        self._list = QListWidget()
        # Recent first (★), then the full offered list — both carry their view.
        for view in recent:
            item = QListWidgetItem(f"★ {view.label}")
            item.setData(_VIEW_ROLE, view)
            self._list.addItem(item)
        for view in options:
            item = QListWidgetItem(view.label)
            item.setData(_VIEW_ROLE, view)
            self._list.addItem(item)
        if self._list.count():
            self._list.setCurrentRow(0)  # first row pre-selected
        self._list.itemActivated.connect(self._confirm_item)
        self._list.itemDoubleClicked.connect(self._confirm_item)
        layout.addWidget(self._list)

    # -- interaction ---------------------------------------------------------

    def _on_search(self, text: str) -> None:
        q = text.strip().lower()
        first_visible: int | None = None
        for i in range(self._list.count()):
            item = self._list.item(i)
            hidden = bool(q) and q not in item.text().lower()
            item.setHidden(hidden)
            if not hidden and first_visible is None:
                first_visible = i
        if first_visible is not None:
            self._list.setCurrentRow(first_visible)

    def keyPressEvent(self, event: object) -> None:  # noqa: N802 (Qt override)
        # Space confirms the current row (Enter is delivered as itemActivated).
        key = event.key()  # type: ignore[attr-defined]
        if key == Qt.Key.Key_Space and self._list.currentItem() is not None:
            self._confirm_item(self._list.currentItem())
            return
        super().keyPressEvent(event)  # type: ignore[arg-type]

    def _confirm_item(self, item: QListWidgetItem) -> None:
        view: FootprintOptionView = item.data(_VIEW_ROLE)
        alt = bool(
            QApplication.keyboardModifiers() & Qt.KeyboardModifier.AltModifier
        )
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
