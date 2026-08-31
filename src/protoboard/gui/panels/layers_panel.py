# SPDX-License-Identifier: MIT
"""Layers panel — visual overview of layer visibility with click-to-cycle buttons."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from protoboard.controller.view_options.layers import LayerOverrides, LayerVisibility

# (field_name, display_name, key_hint).  "ratsnest" keeps its internal
# name for backwards compatibility but is displayed as "Connections".
_LAYER_DEFS = [
    ("board", "Board", "1"),
    ("components_top", "Components Top", "2"),
    ("components_bottom", "Components Bottom", "3"),
    ("wires_top", "Wires Top", "4"),
    ("wires_bottom", "Wires Bottom", "5"),
    ("labels", "Labels", "6"),
    ("meta_pin_labels", "Pin Names", "7"),
    ("ratsnest", "Connections", "8"),
    ("pin_pads", "Pin Pads", "9"),
    ("pin_modifiers", "Pin Modifiers", "0"),
]

# Background colours per visibility state.
_STATE_BG: dict[LayerVisibility, str] = {
    LayerVisibility.FULL: "#2e7d32",  # green — always visible
    LayerVisibility.GHOST: "#e65100",  # amber — dimmed
    LayerVisibility.HIDDEN: "#424242",  # dark grey — invisible
}
_STATE_FG = "#ffffff"


class LayersPanel(QWidget):
    """Compact panel showing all 7 layers as click-to-cycle buttons."""

    # Emitted whenever the user changes a layer override.
    overrides_changed = Signal()

    def __init__(
        self, overrides: LayerOverrides, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._overrides = overrides
        self._buttons: dict[str, QPushButton] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        for field_name, display_name, key_hint in _LAYER_DEFS:
            row = _LayerRow(field_name, display_name, key_hint, self)
            row.clicked.connect(self._on_row_clicked)
            self._buttons[field_name] = row.button
            root.addWidget(row)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #555;")
        root.addWidget(sep)

        reset_btn = QPushButton("Reset All")
        reset_btn.setStyleSheet("font-size: 10px;")
        reset_btn.clicked.connect(self._reset)
        root.addWidget(reset_btn)

        root.addStretch()
        self._sync()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def set_overrides(self, overrides: LayerOverrides) -> None:
        """Replace the overrides object (e.g. after opening a new project)."""
        self._overrides = overrides
        self._sync()

    def sync(self) -> None:
        """Refresh button labels/colours from current overrides (call after external change)."""  # noqa: E501
        self._sync()

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _on_row_clicked(self, field_name: str) -> None:
        current = getattr(self._overrides, field_name)
        setattr(self._overrides, field_name, current.cycle())
        self._sync()
        self.overrides_changed.emit()

    def _reset(self) -> None:
        self._overrides.reset()
        self._sync()
        self.overrides_changed.emit()

    def _sync(self) -> None:
        for field_name, btn in self._buttons.items():
            vis: LayerVisibility = getattr(self._overrides, field_name)
            bg = _STATE_BG[vis]
            label = vis.value.upper()
            btn.setText(label)
            btn.setStyleSheet(
                f"background-color: {bg}; color: {_STATE_FG}; "
                "font-size: 10px; font-weight: bold; "
                "border: none; border-radius: 3px; padding: 1px 4px;"
            )


class _LayerRow(QWidget):
    """One row: layer name label + state button."""

    clicked = Signal(str)  # emits field_name

    def __init__(
        self,
        field_name: str,
        display_name: str,
        key_hint: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._field_name = field_name

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        key_label = QLabel(key_hint)
        key_label.setFixedWidth(12)
        key_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        key_label.setStyleSheet("color: #888; font-size: 10px;")

        name_label = QLabel(display_name)
        name_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        name_label.setStyleSheet("font-size: 11px;")

        self.button = QPushButton("AUTO")
        self.button.setFixedWidth(58)
        self.button.setFixedHeight(20)
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.clicked.connect(lambda: self.clicked.emit(self._field_name))

        layout.addWidget(key_label)
        layout.addWidget(name_label)
        layout.addWidget(self.button)
