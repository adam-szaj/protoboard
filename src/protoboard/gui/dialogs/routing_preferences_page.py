# SPDX-License-Identifier: MIT
"""Routing preferences and the associated cost preset controls."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from protoboard.controller.app_settings import AppSettings
from protoboard.domain.routing.simplify_mode import simplify_mode_from_value
from protoboard.domain.routing_settings import ROUTING_DEFAULTS, RoutingSettings

if TYPE_CHECKING:
    from protoboard.gui.session import EditorSession


class RoutingPreferencesPage(QWidget):
    """Routing settings page — cost model and general routing parameters."""

    def __init__(
        self,
        session: object,
        app_settings: AppSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._session: EditorSession = session  # type: ignore[assignment]
        self._defaults = ROUTING_DEFAULTS
        # Shared AppSettings instance from the parent PreferencesDialog —
        # the CostPresetsDialog mutates this in place so PreferencesDialog's
        # final save_app_settings() picks up the new cost_presets.
        self._app_settings = app_settings

        vbox = QVBoxLayout(self)
        vbox.setSpacing(10)
        vbox.setContentsMargins(8, 8, 8, 8)

        # --- General group ---
        gen_grp = QGroupBox("General")
        gen_form = _routing_form_layout()

        self._preferred_side = QComboBox()
        self._preferred_side.addItems(["bottom", "top"])
        gen_form.addRow("Preferred layer", self._preferred_side)

        self._allow_layer_change = QCheckBox()
        gen_form.addRow("Allow layer change", self._allow_layer_change)

        self._max_rip_up = QSpinBox()
        self._max_rip_up.setRange(0, 20)
        gen_form.addRow("Rip-up passes", self._max_rip_up)

        self._max_optimize = QSpinBox()
        self._max_optimize.setRange(0, 50)
        gen_form.addRow("Optimize iterations", self._max_optimize)

        self._wire_mode = QComboBox()
        self._wire_mode.addItems(["auto", "bare", "bare_diagonal", "insulated"])
        gen_form.addRow("Wire mode", self._wire_mode)

        self._allow_jumpers = QCheckBox()
        gen_form.addRow("Allow jumpers", self._allow_jumpers)

        self._push_and_shove = QCheckBox()
        gen_form.addRow("Push-and-shove", self._push_and_shove)

        self._enable_simplify = QCheckBox()
        gen_form.addRow("Simplify after routing", self._enable_simplify)

        self._simplify_mode = QComboBox()
        self._simplify_mode.addItem("Chebyshev (shortcut via diag)", "chebyshev")
        self._simplify_mode.addItem(
            "Bare-parity (respect anti-short rule)", "bare_parity"
        )
        gen_form.addRow("Simplify mode", self._simplify_mode)

        self._max_detour = _cost_spinbox(0.0, 20.0, 0.5, 1)
        self._max_detour.setSpecialValueText("unlimited")
        gen_form.addRow("Max detour factor", self._max_detour)

        self._skip_autoroute_dialog = QCheckBox()
        self._skip_autoroute_dialog.setToolTip(
            "When checked, F6 / Analysis → Autoroute starts immediately\n"
            "using the saved settings instead of opening the pre-flight dialog."
        )
        gen_form.addRow("Skip autoroute dialog", self._skip_autoroute_dialog)

        self._visual_unweave_side_flip = QCheckBox()
        self._visual_unweave_side_flip.setToolTip(
            "When checked, unweave may flip a wire to the opposite side as a\n"
            "last-resort move to reduce visual tangling on the 2D projection.\n"
            "Useful for cross-side wires that weave through one another even\n"
            "though they don't physically conflict.  Accepts only when\n"
            "the move strictly improves crossings or shared pads without\n"
            "worsening either."
        )
        gen_form.addRow("Visual unweave (side flips)", self._visual_unweave_side_flip)

        gen_grp.setLayout(gen_form)
        vbox.addWidget(gen_grp)

        # --- Cost preset group ---
        from protoboard.domain.routing.cost_presets import builtin_preset_names

        preset_grp = QGroupBox("Cost preset")
        preset_form = _routing_form_layout()

        self._cost_preset = QComboBox()
        # Built-ins first (reserved names; cannot be edited).
        self._builtin_preset_names = sorted(builtin_preset_names())
        for name in self._builtin_preset_names:
            self._cost_preset.addItem(f"{name}  (built-in)", name)
        # User presets follow (read from shared AppSettings, not reloaded).
        for name in sorted(self._app_settings.cost_presets):
            self._cost_preset.addItem(name, name)
        self._cost_preset.setToolTip(
            "Named bundle of cost weights resolved at routing time. "
            "Per-project overrides (set elsewhere) layer on top."
        )
        preset_form.addRow("Active preset", self._cost_preset)

        edit_presets_btn = QPushButton("Edit presets…")
        edit_presets_btn.clicked.connect(self._open_preset_editor)
        preset_form.addRow("", edit_presets_btn)

        preset_grp.setLayout(preset_form)
        vbox.addWidget(preset_grp)

        # --- Per-project cost overrides ---
        # Dynamic spinboxes for every USER_TUNABLE cost key the active
        # routers actually consume.  Values default to the resolved
        # preset+overrides snapshot; user edits go into cost_overrides
        # and are persisted to routing.yaml via apply().
        vbox.addWidget(self._build_overrides_group())

        # --- Restore defaults button ---
        restore_row = QHBoxLayout()
        restore_row.addStretch()
        restore_btn = QPushButton("Restore defaults")
        restore_btn.clicked.connect(self._restore_defaults)
        restore_row.addWidget(restore_btn)
        vbox.addLayout(restore_row)

        vbox.addStretch()

        self._load_from_settings(self._session.routing_settings)

    def _build_overrides_group(self) -> QGroupBox:
        """Dynamic spinbox grid for every user-tunable cost knob.

        Knobs are grouped by the prefix of their key (crossing_, bend_,
        diagonal_, at_pad_, etc.).  Each group becomes a labelled
        sub-section; spinboxes within a group use the standard
        sentinel-aware behaviour (-1 → ∞).  Changes are written to
        ``self._pending_overrides`` and merged into ``cost_overrides``
        when ``apply()`` runs.
        """
        # Defensive imports — ensure built-in routers register themselves
        # before we ask for their supported / user-tunable cost keys.
        import protoboard.services.routing.builtin  # noqa: F401
        import protoboard.services.routing.builtin_enhanced  # noqa: F401
        from protoboard.services.routing.cost_metadata import (
            merged_cost_key_docs,
            merged_user_tunable_cost_keys,
        )
        from protoboard.services.routing.protocol import registry as _rrr

        # Union of supported_cost_keys across registered routers
        # intersected with the union of every router's user_tunable
        # advertisement (built-in OR plugin-supplied).
        supported: set[str] = set()
        for name in _rrr.list_available():
            try:
                supported |= _rrr.get(name).supported_cost_keys()
            except Exception:  # noqa: BLE001
                continue
        docs = merged_cost_key_docs()
        tunable = merged_user_tunable_cost_keys()
        keys = sorted(tunable & supported & set(docs))
        COST_KEY_DOCS = docs  # local rebind so the spinbox loop reads merged docs

        # Group by first segment of the key (everything up to first '_'
        # *unless* it's a multi-segment prefix like 'crossing_ib' or
        # 'at_pad'; collapse those to the leading bigram).
        groups: dict[str, list[str]] = {}
        for k in keys:
            prefix = self._group_prefix(k)
            groups.setdefault(prefix, []).append(k)

        # Resolved current effective weights (preset + overrides).
        rs = self._session.routing_settings
        effective = rs.cost_model(self._app_settings.cost_presets).weights

        self._override_spinboxes: dict[str, QDoubleSpinBox] = {}
        self._pending_overrides: dict[str, float] = dict(rs.cost_overrides)

        outer = QGroupBox("Cost overrides (per-project)")
        outer_layout = QVBoxLayout(outer)
        outer_layout.setContentsMargins(8, 8, 8, 8)

        hint = QLabel(
            "Each value layers on top of the active preset.  Type -1 to "
            "set ∞ (hard-disable the corresponding category)."
        )
        hint.setWordWrap(True)
        font = hint.font()
        font.setItalic(True)
        hint.setFont(font)
        outer_layout.addWidget(hint)

        # "Save as preset…" — promote the current effective weights
        # (preset + overrides) into a brand-new user preset.
        save_row = QHBoxLayout()
        save_row.addStretch()
        save_as_btn = QPushButton("Save as preset…")
        save_as_btn.setToolTip(
            "Capture the currently displayed weights as a new user preset. "
            "After saving, the new preset becomes the active one and "
            "per-project overrides are cleared."
        )
        save_as_btn.clicked.connect(self._save_overrides_as_preset)
        save_row.addWidget(save_as_btn)
        outer_layout.addLayout(save_row)

        scroll_body = QWidget()
        body_layout = QVBoxLayout(scroll_body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(8)

        for prefix in sorted(groups):
            sub = QGroupBox(self._human_prefix(prefix))
            sub_form = QFormLayout(sub)
            sub_form.setSpacing(4)
            for key in groups[prefix]:
                spin = QDoubleSpinBox()
                spin.setRange(-1.0, 1e6)
                spin.setDecimals(3)
                spin.setSingleStep(0.1)
                spin.setSpecialValueText("∞")
                raw = float(effective.get(key, 0.0))
                spin.setValue(-1.0 if (raw == float("inf") or raw != raw) else raw)
                spin.setToolTip(COST_KEY_DOCS.get(key, "") + "\n\nTip: enter -1 for ∞.")
                spin.valueChanged.connect(
                    lambda val, k=key: self._on_override_changed(k, val)
                )
                self._override_spinboxes[key] = spin
                sub_form.addRow(key, spin)
            body_layout.addWidget(sub)

        body_layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(scroll_body)
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(240)
        outer_layout.addWidget(scroll)
        return outer

    @staticmethod
    def _group_prefix(key: str) -> str:
        """Map a cost key to its grouping label.

        Bigram prefixes for the granular families:
        - crossing_ib_*  / crossing_ii_*  / crossing_bare_*  → "crossing"
        - at_pad_*                                            → "at_pad"
        - bend_*                                              → "bend"
        - diagonal_*                                          → "diagonal"
        - wire_over_* / via_on_* / redundant_*                → "foreign / via"
        - everything else                                     → "other"
        """
        if key.startswith("crossing_"):
            return "crossing"
        if key.startswith("at_pad_"):
            return "at_pad"
        if key.startswith("bend_"):
            return "bend"
        if key.startswith("diagonal_"):
            return "diagonal"
        if key.startswith(("wire_over_", "via_on_", "redundant_")):
            return "foreign / via"
        return "other"

    @staticmethod
    def _human_prefix(prefix: str) -> str:
        return {
            "crossing": "Crossings",
            "at_pad": "At-pad penalties",
            "bend": "Bends",
            "diagonal": "Diagonals",
            "foreign / via": "Foreign-pad / via avoidance",
            "other": "Other",
        }.get(prefix, prefix.title())

    def _on_override_changed(self, key: str, value: float) -> None:
        # Sentinel: -1 means ∞.  Anything else is stored literally.
        if value <= -1.0 + 1e-9:
            self._pending_overrides[key] = float("inf")
        else:
            self._pending_overrides[key] = float(value)

    def _save_overrides_as_preset(self) -> None:
        """Capture currently displayed weights as a new user preset.

        Builds a sparse weight dict from the spinbox grid (preset + pending
        overrides, after the ∞ sentinel translation), prompts the user for
        a name, validates it (non-empty, not built-in, not duplicate),
        writes it into the shared AppSettings, persists immediately, then
        switches the active preset to the new one and clears overrides so
        the user is not left with redundant deltas.
        """
        from protoboard.controller.app_settings import save_app_settings
        from protoboard.domain.routing.cost_presets import builtin_preset_names

        # Snapshot the current effective weights as the user sees them in
        # the grid.  Sentinel -1 → ∞.  Zero values are omitted (sparse).
        snapshot: dict[str, float] = {}
        for key, spin in self._override_spinboxes.items():
            v = spin.value()
            if v <= -1.0 + 1e-9:
                snapshot[key] = float("inf")
            elif v != 0.0:
                snapshot[key] = float(v)

        # Prompt for a name, mirroring CostPresetsDialog._ask_for_name.
        while True:
            name, ok = QInputDialog.getText(
                self,
                "Save as preset",
                "Preset name:",
            )
            if not ok:
                return
            name = name.strip()
            if not name:
                return
            if name in builtin_preset_names():
                QMessageBox.warning(
                    self,
                    "Reserved name",
                    f"{name!r} is a built-in preset name and cannot be used.",
                )
                continue
            if name in self._app_settings.cost_presets:
                if (
                    QMessageBox.question(
                        self,
                        "Overwrite preset",
                        f"A user preset named {name!r} already exists.  Overwrite?",
                    )
                    != QMessageBox.StandardButton.Yes
                ):
                    continue
            break

        # Write into the shared AppSettings and persist immediately so the
        # preset survives even if the user later cancels PreferencesDialog.
        self._app_settings.cost_presets[name] = snapshot
        save_app_settings(self._app_settings)

        # Refresh the preset combo so the new entry appears.
        self._cost_preset.blockSignals(True)
        self._cost_preset.clear()
        for bn in self._builtin_preset_names:
            self._cost_preset.addItem(f"{bn}  (built-in)", bn)
        for un in sorted(self._app_settings.cost_presets):
            self._cost_preset.addItem(un, un)
        # Select the freshly created preset.
        for i in range(self._cost_preset.count()):
            if self._cost_preset.itemData(i) == name:
                self._cost_preset.setCurrentIndex(i)
                break
        self._cost_preset.blockSignals(False)

        # The new preset already contains the user's edits, so clear the
        # per-project overrides — otherwise they would layer on top and
        # double-apply.  Reload spinboxes from the new effective weights.
        self._pending_overrides = {}
        for key, spin in self._override_spinboxes.items():
            raw = snapshot.get(key, 0.0)
            spin.blockSignals(True)
            spin.setValue(-1.0 if (raw == float("inf") or raw != raw) else raw)
            spin.blockSignals(False)

    def _open_preset_editor(self) -> None:
        from protoboard.gui.dialogs.cost_presets_dialog import CostPresetsDialog

        # Use the shared AppSettings instance — CostPresetsDialog mutates
        # it in place so the parent PreferencesDialog's final
        # save_app_settings() preserves preset edits.
        # Build the union of all routers' supported_cost_keys so the editor
        # shows only meaningful knobs.
        from protoboard.services.routing.protocol import registry

        all_keys: set[str] = set()
        for name in registry.list_available():
            try:
                all_keys |= registry.get(name).supported_cost_keys()
            except Exception:  # noqa: BLE001
                continue
        dlg = CostPresetsDialog(self._app_settings, allowed_keys=all_keys, parent=self)
        if dlg.exec():
            # Re-populate the preset combo with possibly added/removed names.
            current = self._cost_preset.currentData()
            self._cost_preset.blockSignals(True)
            self._cost_preset.clear()
            for name in self._builtin_preset_names:
                self._cost_preset.addItem(f"{name}  (built-in)", name)
            for name in sorted(self._app_settings.cost_presets):
                self._cost_preset.addItem(name, name)
            for i in range(self._cost_preset.count()):
                if self._cost_preset.itemData(i) == current:
                    self._cost_preset.setCurrentIndex(i)
                    break
            self._cost_preset.blockSignals(False)

    def _load_from_settings(self, s: object) -> None:
        rs: RoutingSettings = s  # type: ignore[assignment]
        # Preset selection — fall back to "default" if the saved name is
        # no longer available (built-in removed, or user preset deleted).
        for i in range(self._cost_preset.count()):
            if self._cost_preset.itemData(i) == rs.cost_preset:
                self._cost_preset.setCurrentIndex(i)
                break
        else:
            for i in range(self._cost_preset.count()):
                if self._cost_preset.itemData(i) == "default":
                    self._cost_preset.setCurrentIndex(i)
                    break
        idx = self._preferred_side.findText(rs.preferred_side)
        self._preferred_side.setCurrentIndex(max(idx, 0))
        self._allow_layer_change.setChecked(rs.allow_layer_change)
        self._max_rip_up.setValue(rs.max_rip_up_passes)
        self._max_optimize.setValue(rs.max_optimize_iterations)
        idx = self._wire_mode.findText(rs.wire_mode)
        self._wire_mode.setCurrentIndex(max(idx, 0))
        self._allow_jumpers.setChecked(rs.allow_jumpers)
        self._push_and_shove.setChecked(rs.push_and_shove)
        self._enable_simplify.setChecked(rs.enable_simplify_pass)
        for i in range(self._simplify_mode.count()):
            if self._simplify_mode.itemData(i) == rs.simplify_mode.value:
                self._simplify_mode.setCurrentIndex(i)
                break
        self._max_detour.setValue(rs.max_detour_factor)
        self._skip_autoroute_dialog.setChecked(rs.skip_autoroute_dialog)
        self._visual_unweave_side_flip.setChecked(rs.visual_unweave_side_flip)

    def _restore_defaults(self) -> None:
        self._load_from_settings(self._defaults)

    def apply(self) -> None:
        rs_old = self._session.routing_settings
        new_settings = RoutingSettings(
            preferred_side=self._preferred_side.currentText(),  # type: ignore[arg-type]
            allow_layer_change=self._allow_layer_change.isChecked(),
            max_rip_up_passes=self._max_rip_up.value(),
            max_optimize_iterations=self._max_optimize.value(),
            wire_mode=self._wire_mode.currentText(),  # type: ignore[arg-type]
            allow_jumpers=self._allow_jumpers.isChecked(),
            push_and_shove=self._push_and_shove.isChecked(),
            enable_simplify_pass=self._enable_simplify.isChecked(),
            simplify_mode=simplify_mode_from_value(self._simplify_mode.currentData()),
            max_detour_factor=self._max_detour.value(),
            router_name=rs_old.router_name,
            skip_autoroute_dialog=self._skip_autoroute_dialog.isChecked(),
            visual_unweave_side_flip=self._visual_unweave_side_flip.isChecked(),
            optimizer_chain=rs_old.optimizer_chain,
            cost_preset=self._cost_preset.currentData() or "default",
            # Per-project overrides come from the dynamic spinbox grid
            # (see _build_overrides_group).  Each spinbox value (or its
            # ∞ sentinel) is written into _pending_overrides as the user
            # edits, so we just hand it over here.
            cost_overrides=dict(self._pending_overrides),
        )
        self._session.routing_settings = new_settings
        self._session.mark_dirty()


def _routing_form_layout() -> QFormLayout:
    form = QFormLayout()
    form.setSpacing(6)
    form.setContentsMargins(8, 8, 8, 8)
    return form


def _cost_spinbox(
    min_val: float, max_val: float, step: float, decimals: int
) -> QDoubleSpinBox:
    sb = QDoubleSpinBox()
    sb.setRange(min_val, max_val)
    sb.setSingleStep(step)
    sb.setDecimals(decimals)
    sb.setFixedWidth(80)
    return sb
