# SPDX-License-Identifier: MIT
"""Placement-time parameter dialog for parametric (variable-pin-count) parts.

A library entry like ``PinHeader`` / ``JST_XH`` declares only its package
kind; this dialog collects the pin count plus the generator's open integer
parameters (e.g. a pin header's ``rows``) and validates them by attempting
the actual build — a combination the generator rejects (odd count on a 2×N
header) keeps the dialog open with the error message instead of accepting.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.package.family import PackageFamily
from protoboard.domain.components.package.generators.base import (
    PackageGenerator,
)
from protoboard.domain.components.package.loader import PackageDefError
from protoboard.services.library.parametric import concretize

#: Sanity bound for the pin-count spin box (a 2×32 header is 64 pins).
MAX_PIN_COUNT = 64


class ParametricPinsDialog(QDialog):
    """Collect pin count + open generator parameters, validated by building."""

    def __init__(
        self,
        defn: ComponentDefinition,
        generator: PackageGenerator,
        package_families: dict[str, PackageFamily],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._defn = defn
        self._package_families = package_families
        self._concrete: ComponentDefinition | None = None
        self.setWindowTitle(f"Place {defn.name}")

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self._count = QSpinBox(self)
        self._count.setRange(2, MAX_PIN_COUNT)
        self._count.setValue(generator.preview_pin_count)
        form.addRow("Pin count:", self._count)

        # One spin box per open integer parameter of the generator's schema
        # (``rows`` for a pin header). Non-integer parameters have no
        # placement-time UI yet — they keep their schema defaults.
        self._param_spins: dict[str, QSpinBox] = {}
        for field_name, spec in generator.params.fields.items():
            if "." in field_name:
                continue
            default = spec.default
            if isinstance(default, bool) or not isinstance(default, int):
                continue
            spin = QSpinBox(self)
            spin.setRange(1, MAX_PIN_COUNT)
            spin.setValue(default)
            self._param_spins[field_name] = spin
            form.addRow(f"{field_name.capitalize()}:", spin)
        layout.addLayout(form)

        self._error = QLabel(self)
        self._error.setWordWrap(True)
        self._error.setVisible(False)
        layout.addWidget(self._error)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self._try_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def chosen_values(self) -> dict[str, object]:
        """The generator parameter values currently selected."""
        return {name: spin.value() for name, spin in self._param_spins.items()}

    def concrete_definition(self) -> ComponentDefinition | None:
        """The definition built on accept, or ``None`` before/without one."""
        return self._concrete

    def _try_accept(self) -> None:
        try:
            self._concrete = concretize(
                self._defn,
                self._count.value(),
                self._package_families,
                self.chosen_values(),
            )
        except PackageDefError as exc:
            self._error.setText(str(exc))
            self._error.setVisible(True)
            return
        self.accept()
