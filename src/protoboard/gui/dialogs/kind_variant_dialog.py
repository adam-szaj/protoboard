# SPDX-License-Identifier: MIT
"""Kind-variant pop-up — the second stage of footprint selection.

Opened from the ``FootprintPickerDialog`` when the chosen footprint is a *kind*
(a parameterised package). Two sections:

* **Recent variants** — a list of already-built variants of this kind, most
  recent first, the top one pre-selected. Confirming one (double-click / Enter /
  Space) returns its address with no further input — the one-confirm path.
* **New parameters** — an input box per required parameter; ``[OK]`` builds a
  fresh variant via the provider.

A section is hidden when the provider offers nothing for it. ``Esc`` cancels.
After ``exec()`` read :attr:`chosen_address` (``None`` on cancel).

The dialog is pure UI: it renders whatever the provider returns and asks the
provider to build the final address. It never touches the package model.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from protoboard.gui.footprint_choice import (
        FootprintChoiceProvider,
        KindVariantView,
        ParamSpec,
    )

_VIEW_ROLE = Qt.ItemDataRole.UserRole


class KindVariantDialog(QDialog):
    """Pick a recent variant of a kind, or enter parameters for a new one."""

    def __init__(
        self,
        provider: FootprintChoiceProvider,
        address: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Choose variant")
        self._provider = provider
        self._address = address
        self.chosen_address: str | None = None

        self._params: list[ParamSpec] = list(provider.kind_params(address))
        variants: list[KindVariantView] = list(provider.recent_variants(address))

        layout = QVBoxLayout(self)

        # --- Section 1: recently-used variants (one-confirm path) ---
        self._recent_list = QListWidget()
        if variants:
            layout.addWidget(QLabel("Recently used:"))
            for v in variants:
                item = QListWidgetItem(v.label)
                item.setData(_VIEW_ROLE, v)
                self._recent_list.addItem(item)
            self._recent_list.setCurrentRow(0)  # top pre-selected
            self._recent_list.itemActivated.connect(self._confirm_recent)
            self._recent_list.itemDoubleClicked.connect(self._confirm_recent)
            layout.addWidget(self._recent_list)
        else:
            self._recent_list.setVisible(False)

        # --- Section 2: new-variant parameter inputs ---
        self._inputs: dict[str, QLineEdit] = {}
        if self._params:
            layout.addWidget(QLabel("New variant:"))
            form = QFormLayout()
            for spec in self._params:
                edit = QLineEdit()
                edit.setPlaceholderText(spec.type)
                self._inputs[spec.name] = edit
                form.addRow(f"{spec.name}:", edit)
            layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # -- confirm paths -------------------------------------------------------

    def _confirm_recent(self, item: QListWidgetItem) -> None:
        view: KindVariantView = item.data(_VIEW_ROLE)
        self.chosen_address = view.address
        self.accept()

    def _on_ok(self) -> None:
        # A focused/selected recent variant wins over entering new params, so the
        # top-pre-selected one-confirm path also works via the OK button.
        if (
            self._recent_list.isVisible()
            and self._recent_list.currentItem() is not None
            and not self._has_input()
        ):
            self._confirm_recent(self._recent_list.currentItem())
            return
        if self._params:
            values = {name: e.text().strip() for name, e in self._inputs.items()}
            if not self._validate(values):
                return
            built = self._provider.build_variant(self._address, values)
            if built is None:
                return
            self.chosen_address = built
            self.accept()
            return
        # No params and no recent selection → the kind resolves as-is.
        self.chosen_address = self._provider.build_variant(self._address, {})
        self.accept()

    # -- helpers -------------------------------------------------------------

    def _has_input(self) -> bool:
        return any(e.text().strip() for e in self._inputs.values())

    def _validate(self, values: dict[str, str]) -> bool:
        for spec in self._params:
            raw = values.get(spec.name, "")
            if not raw:
                return False
            if spec.type == "float":
                try:
                    float(raw)
                except ValueError:
                    return False
            elif spec.type == "int":
                try:
                    int(raw)
                except ValueError:
                    return False
        return True
