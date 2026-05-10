# SPDX-License-Identifier: MIT
"""Pre-flight dialog for configuring and launching the autorouter."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
)

if TYPE_CHECKING:
    from protoboard.domain.routing_settings import RoutingSettings


class AutorouteDialog(QDialog):
    """Modal dialog shown before starting the autorouter.

    Pre-fills all fields from *routing_settings*.  The caller reads
    :meth:`routing_settings` after ``exec()`` returns ``Accepted``.
    If :meth:`save_as_default` is True, the caller should persist the
    returned settings back to the session.
    """

    def __init__(
        self,
        routing_settings: RoutingSettings,
        parent: object = None,
    ) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        self.setWindowTitle("Autoroute")
        self.setMinimumWidth(380)

        rs = routing_settings
        root = QVBoxLayout(self)
        root.setSpacing(10)

        # ── Wire type ──────────────────────────────────────────────────
        wire_group = QGroupBox("Wire type")
        wire_layout = QVBoxLayout(wire_group)

        self._rb_auto = QRadioButton("Auto — bare wire, insulated as fallback")
        self._rb_bare = QRadioButton("Always bare")
        self._rb_ins  = QRadioButton("Always insulated")

        self._wire_mode_group = QButtonGroup(self)
        self._wire_mode_group.addButton(self._rb_auto, 0)
        self._wire_mode_group.addButton(self._rb_bare, 1)
        self._wire_mode_group.addButton(self._rb_ins,  2)

        if rs.wire_mode == "bare":
            self._rb_bare.setChecked(True)
        elif rs.wire_mode == "insulated":
            self._rb_ins.setChecked(True)
        else:
            self._rb_auto.setChecked(True)

        self._chk_jumpers = QCheckBox("Allow jumpers (insulated wire over bare)")
        self._chk_jumpers.setChecked(rs.allow_jumpers)

        wire_layout.addWidget(self._rb_auto)
        wire_layout.addWidget(self._rb_bare)
        wire_layout.addWidget(self._rb_ins)
        wire_layout.addWidget(self._chk_jumpers)
        root.addWidget(wire_group)

        # Disable jumpers option when "always bare" (bare can never cross bare).
        self._rb_bare.toggled.connect(
            lambda checked: self._chk_jumpers.setEnabled(not checked)
        )
        if rs.wire_mode == "bare":
            self._chk_jumpers.setEnabled(False)

        # ── Routing strategy ───────────────────────────────────────────
        strat_group = QGroupBox("Routing strategy")
        strat_form = QFormLayout(strat_group)
        strat_form.setLabelAlignment(strat_form.labelAlignment())

        self._spin_ripup = QSpinBox()
        self._spin_ripup.setRange(0, 20)
        self._spin_ripup.setValue(rs.max_rip_up_passes)
        strat_form.addRow("Rip-up passes:", self._spin_ripup)

        self._spin_opt = QSpinBox()
        self._spin_opt.setRange(0, 50)
        self._spin_opt.setValue(rs.max_optimize_iterations)
        strat_form.addRow("Optimize iterations:", self._spin_opt)

        self._combo_side = QComboBox()
        self._combo_side.addItems(["bottom", "top"])
        self._combo_side.setCurrentText(rs.preferred_side)
        strat_form.addRow("Preferred layer:", self._combo_side)

        self._chk_layer_change = QCheckBox()
        self._chk_layer_change.setChecked(rs.allow_layer_change)
        strat_form.addRow("Allow layer change:", self._chk_layer_change)

        root.addWidget(strat_group)

        # ── Limits & costs ─────────────────────────────────────────────
        cost_group = QGroupBox("Limits & costs")
        cost_form = QFormLayout(cost_group)

        self._spin_detour = QDoubleSpinBox()
        self._spin_detour.setRange(0.0, 20.0)
        self._spin_detour.setSingleStep(0.5)
        self._spin_detour.setDecimals(1)
        self._spin_detour.setSpecialValueText("unlimited")
        self._spin_detour.setValue(rs.max_detour_factor)
        cost_form.addRow("Max detour factor:", self._spin_detour)

        self._spin_dir = QDoubleSpinBox()
        self._spin_dir.setRange(0.0, 20.0)
        self._spin_dir.setSingleStep(0.1)
        self._spin_dir.setDecimals(2)
        self._spin_dir.setValue(rs.direction_change)
        cost_form.addRow("Direction change cost:", self._spin_dir)

        self._spin_jumper_cost = QDoubleSpinBox()
        self._spin_jumper_cost.setRange(0.0, 100.0)
        self._spin_jumper_cost.setSingleStep(0.5)
        self._spin_jumper_cost.setDecimals(1)
        self._spin_jumper_cost.setValue(rs.crossing_insulated_bare)
        cost_form.addRow("Jumper cost (insulated × bare):", self._spin_jumper_cost)

        self._spin_via = QDoubleSpinBox()
        self._spin_via.setRange(0.0, 20.0)
        self._spin_via.setSingleStep(0.5)
        self._spin_via.setDecimals(1)
        self._spin_via.setValue(rs.layer_change)
        cost_form.addRow("Layer change (via) cost:", self._spin_via)

        self._spin_under = QDoubleSpinBox()
        self._spin_under.setRange(0.0, 20.0)
        self._spin_under.setSingleStep(0.5)
        self._spin_under.setDecimals(1)
        self._spin_under.setValue(rs.under_component_top)
        cost_form.addRow("Under component (top) cost:", self._spin_under)

        root.addWidget(cost_group)

        # ── Footer ─────────────────────────────────────────────────────
        self._chk_save = QCheckBox("Save as project default")
        root.addWidget(self._chk_save)

        self._chk_skip = QCheckBox("Don't show this dialog next time")
        self._chk_skip.setChecked(rs.skip_autoroute_dialog)
        root.addWidget(self._chk_skip)

        buttons = QDialogButtonBox()
        self._route_btn = buttons.addButton("Route", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def routing_settings(self) -> RoutingSettings:
        """Return a RoutingSettings built from the current widget values."""
        from protoboard.domain.routing_settings import RoutingSettings

        if self._rb_bare.isChecked():
            wire_mode = "bare"
        elif self._rb_ins.isChecked():
            wire_mode = "insulated"
        else:
            wire_mode = "auto"

        return RoutingSettings(
            preferred_side=self._combo_side.currentText(),  # type: ignore[arg-type]
            allow_layer_change=self._chk_layer_change.isChecked(),
            max_rip_up_passes=self._spin_ripup.value(),
            max_optimize_iterations=self._spin_opt.value(),
            direction_change=self._spin_dir.value(),
            crossing_insulated_bare=self._spin_jumper_cost.value(),
            layer_change=self._spin_via.value(),
            under_component_top=self._spin_under.value(),
            wire_mode=wire_mode,  # type: ignore[arg-type]
            allow_jumpers=self._chk_jumpers.isChecked(),
            max_detour_factor=self._spin_detour.value(),
            skip_autoroute_dialog=self._chk_skip.isChecked(),
        )

    def save_as_default(self) -> bool:
        """Return True if the user wants to persist these settings to the project."""
        return self._chk_save.isChecked()
