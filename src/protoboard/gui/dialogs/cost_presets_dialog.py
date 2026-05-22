# SPDX-License-Identifier: MIT
"""CostPresetsDialog — manage user-defined cost-model presets.

Built-in presets ("default", "clean_routing") are read-only and always
listed at the top of the combo box.  User presets are loaded from / saved
to AppSettings.  Clicking OK persists the user-preset dict back to
``app_settings.yaml``.
"""

from __future__ import annotations

from typing import Iterable

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from protoboard.gui.app_settings import AppSettings, save_app_settings
from protoboard.services.routing.cost_model import COST_KEY_DOCS, USER_TUNABLE_COST_KEYS
from protoboard.services.routing.cost_presets import (
    builtin_preset_names,
    get_builtin_preset,
)


class CostPresetsDialog(QDialog):
    """Edit user-defined cost presets stored in AppSettings."""

    def __init__(
        self,
        settings: AppSettings,
        allowed_keys: Iterable[str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Cost-model presets")
        self.setModal(True)
        self.resize(560, 520)

        self._settings = settings
        # Work on a deep copy so Cancel discards.
        self._user_presets: dict[str, dict[str, float]] = {
            name: dict(weights)
            for name, weights in settings.cost_presets.items()
        }
        # Filter visible keys to user-tunable subset, intersected with
        # the union of every router's supported_cost_keys (if provided).
        # Physical constants and hard electrical bans are excluded so the
        # UI doesn't tempt users to e.g. set crossing_bare_bare=0.0.
        if allowed_keys is None:
            self._allowed_keys: list[str] = sorted(USER_TUNABLE_COST_KEYS & set(COST_KEY_DOCS))
        else:
            self._allowed_keys = sorted(
                set(allowed_keys) & USER_TUNABLE_COST_KEYS & set(COST_KEY_DOCS)
            )

        root = QVBoxLayout(self)

        # --- Top row: preset combo + management buttons ---
        top = QHBoxLayout()
        top.addWidget(QLabel("Preset:"))
        self._combo = QComboBox()
        self._combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._combo.currentIndexChanged.connect(self._on_preset_changed)
        top.addWidget(self._combo, 1)

        self._btn_new = QPushButton("New…")
        self._btn_new.clicked.connect(self._new_preset)
        top.addWidget(self._btn_new)

        self._btn_dup = QPushButton("Duplicate…")
        self._btn_dup.clicked.connect(self._duplicate_preset)
        top.addWidget(self._btn_dup)

        self._btn_del = QPushButton("Delete")
        self._btn_del.clicked.connect(self._delete_preset)
        top.addWidget(self._btn_del)

        root.addLayout(top)

        # --- Hint label for built-in presets ---
        self._hint = QLabel("")
        self._hint.setWordWrap(True)
        font = QFont()
        font.setItalic(True)
        self._hint.setFont(font)
        root.addWidget(self._hint)

        # --- Weights table ---
        self._table = QTableWidget(0, 2, self)
        self._table.setHorizontalHeaderLabels(["Cost key", "Weight"])
        self._table.horizontalHeader().setStretchLastSection(False)
        self._table.setColumnWidth(0, 320)
        self._table.setColumnWidth(1, 140)
        self._table.verticalHeader().setVisible(False)
        root.addWidget(self._table, 1)

        # --- OK/Cancel ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._reload_combo()

    # ------------------------------------------------------------------
    # Helpers — combo / table population
    # ------------------------------------------------------------------

    def _reload_combo(self, select_name: str | None = None) -> None:
        self._combo.blockSignals(True)
        self._combo.clear()
        for name in sorted(builtin_preset_names()):
            self._combo.addItem(f"{name}  (built-in)", name)
        for name in sorted(self._user_presets):
            self._combo.addItem(name, name)
        if select_name:
            for i in range(self._combo.count()):
                if self._combo.itemData(i) == select_name:
                    self._combo.setCurrentIndex(i)
                    break
        else:
            self._combo.setCurrentIndex(0)
        self._combo.blockSignals(False)
        self._on_preset_changed()

    def _on_preset_changed(self) -> None:
        name = self._combo.currentData()
        is_builtin = name in builtin_preset_names()
        self._btn_del.setEnabled(not is_builtin)
        if is_builtin:
            self._hint.setText(
                "Built-in preset (read-only). Use Duplicate to create an editable copy."
            )
            weights = get_builtin_preset(name) or {}
        else:
            self._hint.setText("User-defined preset — edit weights directly below.")
            weights = self._user_presets.get(name, {})
        self._populate_table(weights, read_only=is_builtin)

    def _populate_table(self, weights: dict[str, float], read_only: bool) -> None:
        self._table.blockSignals(True)
        self._table.setRowCount(len(self._allowed_keys))
        for row, key in enumerate(self._allowed_keys):
            # Column 0: key + doc (tooltip)
            key_item = QTableWidgetItem(key)
            key_item.setFlags(key_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            doc = COST_KEY_DOCS.get(key, "")
            key_item.setToolTip(doc + "\n\nTip: enter -1 to mean ∞ (hard-disable this kind of step).")
            self._table.setItem(row, 0, key_item)
            # Column 1: spinbox with -1 sentinel for ∞.  range starts at -1
            # and uses setSpecialValueText so "-1" renders as "∞" in the UI
            # while still being a valid numeric value users can type.
            spin = QDoubleSpinBox()
            spin.setRange(-1.0, 1e6)
            spin.setDecimals(3)
            spin.setSingleStep(0.1)
            spin.setSpecialValueText("∞")  # shown when value == minimum (-1.0)
            raw = float(weights.get(key, 0.0))
            spin.setValue(-1.0 if (raw == float("inf") or raw != raw) else raw)
            spin.setReadOnly(read_only)
            spin.setEnabled(not read_only)
            # Persist on change for user presets.
            if not read_only:
                spin.valueChanged.connect(
                    lambda val, k=key: self._update_current_user_preset(k, val)
                )
            self._table.setCellWidget(row, 1, spin)
        self._table.blockSignals(False)

    def _update_current_user_preset(self, key: str, value: float) -> None:
        name = self._combo.currentData()
        if name in builtin_preset_names():
            return
        weights = self._user_presets.setdefault(name, {})
        # Sentinel: spinbox value -1 means "∞" — translate back to float("inf")
        # so resolved cost models behave identically to the historical inf
        # constants (hard-disable that kind of step).
        if value <= -1.0 + 1e-9:
            weights[key] = float("inf")
        elif value == 0.0:
            # Sparse storage — omit zero weights so the YAML stays minimal.
            weights.pop(key, None)
        else:
            weights[key] = float(value)

    # ------------------------------------------------------------------
    # New / Duplicate / Delete
    # ------------------------------------------------------------------

    def _ask_for_name(self, title: str, default: str = "") -> str | None:
        name, ok = QInputDialog.getText(self, title, "Preset name:", text=default)
        if not ok:
            return None
        name = name.strip()
        if not name:
            return None
        if name in builtin_preset_names():
            QMessageBox.warning(
                self, "Reserved name",
                f"{name!r} is a built-in preset name and cannot be used.",
            )
            return None
        if name in self._user_presets:
            QMessageBox.warning(
                self, "Already exists",
                f"A user preset named {name!r} already exists.",
            )
            return None
        return name

    def _new_preset(self) -> None:
        name = self._ask_for_name("New preset")
        if name is None:
            return
        self._user_presets[name] = {}
        self._reload_combo(select_name=name)

    def _duplicate_preset(self) -> None:
        src = self._combo.currentData()
        name = self._ask_for_name("Duplicate preset", default=f"{src}_copy")
        if name is None:
            return
        if src in builtin_preset_names():
            self._user_presets[name] = get_builtin_preset(src) or {}
        else:
            self._user_presets[name] = dict(self._user_presets.get(src, {}))
        self._reload_combo(select_name=name)

    def _delete_preset(self) -> None:
        name = self._combo.currentData()
        if name in builtin_preset_names():
            return
        if QMessageBox.question(
            self, "Delete preset",
            f"Delete user preset {name!r}?",
        ) != QMessageBox.StandardButton.Yes:
            return
        self._user_presets.pop(name, None)
        self._reload_combo()

    # ------------------------------------------------------------------
    # Accept — write back to AppSettings on disk.
    # ------------------------------------------------------------------

    def accept(self) -> None:
        self._settings.cost_presets = self._user_presets
        save_app_settings(self._settings)
        super().accept()


__all__ = ["CostPresetsDialog"]
