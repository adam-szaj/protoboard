# SPDX-License-Identifier: MIT
"""Pre-flight dialog for configuring and launching the autorouter."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
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
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
)

if TYPE_CHECKING:
    from protoboard.domain.routing_settings import RoutingSettings
    from protoboard.services.routing.postprocess import SimplifyMode


def _simplify_mode_from_combo(raw: object) -> "SimplifyMode":
    """Translate combobox data-role into SimplifyMode (defaults to BARE_PARITY)."""
    from protoboard.services.routing.postprocess import SimplifyMode
    if isinstance(raw, str):
        try:
            return SimplifyMode(raw)
        except ValueError:
            return SimplifyMode.BARE_PARITY
    return SimplifyMode.BARE_PARITY


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

        # ── Router (plugin-aware) ──────────────────────────────────────
        # Side-effect import: registers built-in routers if app.main()
        # hasn't run yet (e.g. unit tests).
        import protoboard.services.routing.builtin  # noqa: F401
        import protoboard.services.routing.builtin_enhanced  # noqa: F401
        from protoboard.services.routing.protocol import registry as _router_registry
        router_row = QHBoxLayout()
        router_row.addWidget(QLabel("Router:"))
        self._combo_router = QComboBox()
        for name in _router_registry.list_available():
            r = _router_registry.get(name)
            self._combo_router.addItem(getattr(r, "display_name", name), name)
            caps = r.capabilities()
            if caps.notes:
                self._combo_router.setItemData(
                    self._combo_router.count() - 1, caps.notes, 3,  # Qt.ToolTipRole
                )
        # Default selection: load user's last choice from settings, with
        # graceful fallback when the saved router is no longer registered
        # (e.g. its plugin was uninstalled between sessions).
        default_idx = self._combo_router.findData(rs.router_name)
        if default_idx < 0:
            default_idx = self._combo_router.findData("astar")
        if default_idx >= 0:
            self._combo_router.setCurrentIndex(default_idx)
        router_row.addWidget(self._combo_router)
        router_row.addStretch()
        root.addLayout(router_row)

        # ── Wire type ──────────────────────────────────────────────────
        wire_group = QGroupBox("Wire type")
        wire_layout = QVBoxLayout(wire_group)

        self._rb_auto = QRadioButton("Auto — bare wire, insulated as fallback")
        self._rb_bare = QRadioButton("Always bare")
        self._rb_bare_diag = QRadioButton(
            "Bare diagonal — parity-restricted, crossings between pads"
        )
        self._rb_ins  = QRadioButton("Always insulated")

        self._wire_mode_group = QButtonGroup(self)
        self._wire_mode_group.addButton(self._rb_auto, 0)
        self._wire_mode_group.addButton(self._rb_bare, 1)
        self._wire_mode_group.addButton(self._rb_bare_diag, 2)
        self._wire_mode_group.addButton(self._rb_ins, 3)

        if rs.wire_mode == "bare":
            self._rb_bare.setChecked(True)
        elif rs.wire_mode == "bare_diagonal":
            self._rb_bare_diag.setChecked(True)
        elif rs.wire_mode == "insulated":
            self._rb_ins.setChecked(True)
        else:
            self._rb_auto.setChecked(True)

        self._chk_jumpers = QCheckBox("Allow jumpers (insulated wire over bare)")
        self._chk_jumpers.setChecked(rs.allow_jumpers)

        wire_layout.addWidget(self._rb_auto)
        wire_layout.addWidget(self._rb_bare)
        wire_layout.addWidget(self._rb_bare_diag)
        wire_layout.addWidget(self._rb_ins)
        wire_layout.addWidget(self._chk_jumpers)
        root.addWidget(wire_group)

        # Disable jumpers option when bare-family selected (bare can never cross bare).
        def _sync_jumpers_enabled() -> None:
            bare_family = self._rb_bare.isChecked() or self._rb_bare_diag.isChecked()
            self._chk_jumpers.setEnabled(not bare_family)
        self._rb_bare.toggled.connect(lambda _c: _sync_jumpers_enabled())
        self._rb_bare_diag.toggled.connect(lambda _c: _sync_jumpers_enabled())
        _sync_jumpers_enabled()

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

        self._chk_push_shove = QCheckBox()
        self._chk_push_shove.setChecked(rs.push_and_shove)
        strat_form.addRow("Push-and-shove:", self._chk_push_shove)

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

        self._spin_diag_cross = QDoubleSpinBox()
        self._spin_diag_cross.setRange(0.0, 100.0)
        self._spin_diag_cross.setSingleStep(0.5)
        self._spin_diag_cross.setDecimals(1)
        self._spin_diag_cross.setValue(rs.crossing_bare_bare_diagonal)
        cost_form.addRow("Diagonal cross cost (bare × bare):", self._spin_diag_cross)

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

        # Enhanced-router penalties (consumed only by ``astar_enhanced``;
        # the default ``astar`` router ignores them).  All default to 0 →
        # no behavioural change unless the user opts in.
        self._spin_at_pad_ii = QDoubleSpinBox()
        self._spin_at_pad_ii.setRange(0.0, 50.0)
        self._spin_at_pad_ii.setSingleStep(0.5)
        self._spin_at_pad_ii.setDecimals(1)
        self._spin_at_pad_ii.setValue(rs.at_pad_insulated_insulated_same_side)
        self._spin_at_pad_ii.setToolTip(
            "Enhanced router only.  Tuning starting point: 1.0.  0 = disabled."
        )
        cost_form.addRow(
            "At-pad insulated × insulated (same side):", self._spin_at_pad_ii
        )

        self._spin_at_pad_ib = QDoubleSpinBox()
        self._spin_at_pad_ib.setRange(0.0, 50.0)
        self._spin_at_pad_ib.setSingleStep(0.5)
        self._spin_at_pad_ib.setDecimals(1)
        self._spin_at_pad_ib.setValue(rs.at_pad_insulated_bare_same_side)
        self._spin_at_pad_ib.setToolTip(
            "Enhanced router only.  Tuning starting point: 3.0.  0 = disabled."
        )
        cost_form.addRow(
            "At-pad insulated × bare (same side):", self._spin_at_pad_ib
        )

        self._spin_parallel_opposite = QDoubleSpinBox()
        self._spin_parallel_opposite.setRange(0.0, 50.0)
        self._spin_parallel_opposite.setSingleStep(0.5)
        self._spin_parallel_opposite.setDecimals(1)
        self._spin_parallel_opposite.setValue(rs.parallel_opposite_side)
        self._spin_parallel_opposite.setToolTip(
            "Enhanced router only.  Tuning starting point: 0.5.  0 = disabled."
        )
        cost_form.addRow(
            "Parallel on opposite side:", self._spin_parallel_opposite
        )

        self._spin_max_reroute = QDoubleSpinBox()
        self._spin_max_reroute.setRange(0.0, 5.0)
        self._spin_max_reroute.setSingleStep(0.1)
        self._spin_max_reroute.setDecimals(2)
        self._spin_max_reroute.setValue(rs.max_reroute_factor)
        self._spin_max_reroute.setToolTip(
            "Enhanced router only.  Tuning starting point: 1.5.  0 = "
            "disabled (legacy push-and-shove only)."
        )
        cost_form.addRow(
            "Max reroute factor (push&shove):", self._spin_max_reroute
        )

        root.addWidget(cost_group)

        self._chk_simplify = QCheckBox("Simplify after routing (bare diagonal)")
        self._chk_simplify.setChecked(rs.enable_simplify_pass)
        root.addWidget(self._chk_simplify)

        simplify_mode_row = QHBoxLayout()
        simplify_mode_row.addWidget(QLabel("Simplify mode:"))
        self._combo_simplify_mode = QComboBox()
        self._combo_simplify_mode.addItem(
            "Chebyshev (shortcut via diag)", "chebyshev"
        )
        self._combo_simplify_mode.addItem(
            "Bare-parity (respect anti-short rule)", "bare_parity"
        )
        for i in range(self._combo_simplify_mode.count()):
            if self._combo_simplify_mode.itemData(i) == rs.simplify_mode.value:
                self._combo_simplify_mode.setCurrentIndex(i)
                break
        simplify_mode_row.addWidget(self._combo_simplify_mode)
        simplify_mode_row.addStretch()
        root.addLayout(simplify_mode_row)

        # ── Post-process plugins (optimizer chain) ─────────────────────
        # Each registered WireOptimizer becomes a row in a drag-reorderable
        # list with a checkbox.  Chain order = top-to-bottom of the list;
        # only checked rows are included.  The legacy "Simplify after
        # routing" checkbox above stays as the plain-vanilla path — when
        # no row here is checked, the worker falls back to the embedded
        # simplify pass per the routing settings.
        import protoboard.services.routing.builtin_optimizers  # noqa: F401
        from protoboard.services.routing.optimizer import (
            registry as _opt_registry,
        )
        self._optimizer_list: QListWidget | None = None
        opt_names = [n for n in _opt_registry.list_available() if n != "simplify"]
        if opt_names:
            opt_group = QGroupBox("Post-process (drag to reorder)")
            opt_layout = QVBoxLayout(opt_group)
            self._optimizer_list = QListWidget()
            # Enable both internal-only drag/drop and per-row check state.
            self._optimizer_list.setDragDropMode(
                QAbstractItemView.DragDropMode.InternalMove
            )
            self._optimizer_list.setSelectionMode(
                QAbstractItemView.SelectionMode.SingleSelection
            )
            # Compact height — 4 rows is plenty; users with many plugins
            # get a scrollbar instead of a giant dialog.
            self._optimizer_list.setMaximumHeight(120)

            # Build the row order: persisted chain first (preserving
            # user's saved sequence), then remaining unregistered names
            # alphabetically at the bottom, all unchecked.
            persisted_chain = list(rs.optimizer_chain)
            available = set(opt_names)
            ordered: list[str] = []
            for name in persisted_chain:
                if name in available:
                    ordered.append(name)
                    available.discard(name)
            ordered.extend(sorted(available))

            persisted_set = set(rs.optimizer_chain)
            from protoboard.services.routing.optimizer import (
                incompatibility_reasons,
            )
            for opt_name in ordered:
                opt = _opt_registry.get(opt_name)
                item = QListWidgetItem(getattr(opt, "display_name", opt_name))
                item.setData(Qt.ItemDataRole.UserRole, opt_name)
                caps = opt.capabilities()
                # Capability-vs-settings gating: if the current routing
                # settings make every declared capability a no-op, grey
                # the row out and force-uncheck it.  The tooltip combines
                # the optimizer's notes with each blocking reason so the
                # user understands why.
                reasons = incompatibility_reasons(caps, rs)
                compatible = not reasons

                base_flags = item.flags() | Qt.ItemFlag.ItemIsDragEnabled
                if compatible:
                    item.setFlags(base_flags | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(
                        Qt.CheckState.Checked
                        if opt_name in persisted_set
                        else Qt.CheckState.Unchecked
                    )
                else:
                    # ItemIsEnabled is on by default; drop it to grey out
                    # the row.  Drop ItemIsUserCheckable so the checkbox
                    # isn't clickable.  Force unchecked regardless of
                    # the persisted state — a saved-but-incompatible
                    # plugin must not silently fire.
                    item.setFlags(
                        (base_flags & ~Qt.ItemFlag.ItemIsEnabled)
                        & ~Qt.ItemFlag.ItemIsUserCheckable
                    )
                    item.setCheckState(Qt.CheckState.Unchecked)

                tooltip_parts: list[str] = []
                if caps.notes:
                    tooltip_parts.append(caps.notes)
                tooltip_parts.extend(reasons)
                if tooltip_parts:
                    item.setToolTip("\n\n".join(tooltip_parts))

                self._optimizer_list.addItem(item)
            opt_layout.addWidget(self._optimizer_list)
            root.addWidget(opt_group)

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
        elif self._rb_bare_diag.isChecked():
            wire_mode = "bare_diagonal"
        elif self._rb_ins.isChecked():
            wire_mode = "insulated"
        else:
            wire_mode = "auto"

        # Materialise the optimizer chain from the list — top-to-bottom
        # order, checked rows only.  Empty when no plugins are
        # registered or none are ticked.
        chain = tuple(self._chain_from_list())

        return RoutingSettings(
            preferred_side=self._combo_side.currentText(),  # type: ignore[arg-type]
            allow_layer_change=self._chk_layer_change.isChecked(),
            max_rip_up_passes=self._spin_ripup.value(),
            max_optimize_iterations=self._spin_opt.value(),
            direction_change=self._spin_dir.value(),
            crossing_insulated_bare=self._spin_jumper_cost.value(),
            crossing_bare_bare_diagonal=self._spin_diag_cross.value(),
            layer_change=self._spin_via.value(),
            under_component_top=self._spin_under.value(),
            at_pad_insulated_insulated_same_side=self._spin_at_pad_ii.value(),
            at_pad_insulated_bare_same_side=self._spin_at_pad_ib.value(),
            parallel_opposite_side=self._spin_parallel_opposite.value(),
            max_reroute_factor=self._spin_max_reroute.value(),
            wire_mode=wire_mode,  # type: ignore[arg-type]
            router_name=self.router_name(),
            allow_jumpers=self._chk_jumpers.isChecked(),
            push_and_shove=self._chk_push_shove.isChecked(),
            enable_simplify_pass=self._chk_simplify.isChecked(),
            simplify_mode=_simplify_mode_from_combo(self._combo_simplify_mode.currentData()),
            max_detour_factor=self._spin_detour.value(),
            skip_autoroute_dialog=self._chk_skip.isChecked(),
            optimizer_chain=chain,
        )

    def router_name(self) -> str:
        """Return the registry name of the router the user selected."""
        data = self._combo_router.currentData()
        return data if isinstance(data, str) else "astar"

    def selected_optimizers(self) -> list[str] | None:
        """Return the optimizer chain the user assembled, or None for legacy.

        Returns ``None`` when no plugin row exists at all in the dialog
        (i.e. only the built-in 'simplify' is registered).  ``None``
        keeps the legacy behaviour where ``autoroute()``'s embedded
        simplify pass fires based on ``RoutingSettings.enable_simplify_pass``.
        Returns a list (possibly empty) when at least one plugin row
        exists — the caller treats that as an explicit chain and
        suppresses the embedded pass.

        The "simplify" optimizer is not exposed here on purpose: it has
        a dedicated legacy checkbox above for backward compat.
        """
        if self._optimizer_list is None:
            # No plugins registered (only the built-in simplify) — keep
            # the legacy code path.
            return None
        return self._chain_from_list()

    def _chain_from_list(self) -> list[str]:
        """Read the optimizer chain in display order, taking checked rows only."""
        result: list[str] = []
        lst = self._optimizer_list
        if lst is None:
            return result
        for i in range(lst.count()):
            item = lst.item(i)
            if item is None:
                continue
            if item.checkState() != Qt.CheckState.Checked:
                continue
            name = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(name, str):
                result.append(name)
        return result

    def save_as_default(self) -> bool:
        """Return True if the user wants to persist these settings to the project."""
        return self._chk_save.isChecked()
