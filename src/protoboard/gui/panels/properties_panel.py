# SPDX-License-Identifier: MIT
"""Properties panel — selected component editor + DRC list."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from protoboard.domain.components.appearance import Appearance, parse_style
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package.bend import PinBendSpec
from protoboard.domain.components.package.family import PackageFamily
from protoboard.domain.layout.placement import ComponentPlacement, pin_world_position
from protoboard.geometry import Vector
from protoboard.services.debug_trace import traced
from protoboard.services.drc.engine import DRCIssue

if TYPE_CHECKING:
    from protoboard.gui.session import EditorSession

_SEVERITY_COLORS = {
    "error": QColor("#CC2222"),
    "warning": QColor("#CC8800"),
    "info": QColor("#2255AA"),
}


class PropertiesPanel(QWidget):
    """Shows editable fields for the selected component and DRC issues."""

    # id, refdes, value, tolerance, rotation, label_offset_x, label_offset_y
    component_changed = Signal(uuid.UUID, str, str, str, int, float, float)
    # id, {pin_number: Vector} — emitted when Apply is clicked and offsets changed
    pin_offsets_changed = Signal(uuid.UUID, object)
    # id, pin_variant (str or None)
    pin_variant_changed = Signal(uuid.UUID, object)
    # id, package_variant (str "family/variant_id" or None for default)
    package_variant_changed = Signal(uuid.UUID, object)
    # id, Appearance or None (None = clear the per-instance style override).
    # Emitted from Apply, only when the fields differ from the shown value.
    appearance_changed = Signal(uuid.UUID, object)
    # DRCIssue or None (None = the user clicked away or no issue selected).
    # MainWindow translates this into HighlightStack pushes + viewport centring.
    drc_issue_clicked = Signal(object)

    @traced()
    def __init__(self, parent: object = None) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # Component properties group.
        grp = QGroupBox("Component")
        form = QFormLayout(grp)
        self._refdes_edit = QLineEdit()
        self._value_edit = QLineEdit()
        self._tolerance_edit = QLineEdit()
        self._tolerance_edit.setPlaceholderText("e.g. 5% — empty = unspecified")
        self._tolerance_edit.setToolTip(
            "Manufacturing tolerance of the value (e.g. 5%, 1%). Drives the "
            "tolerance colour band on axial resistors/inductors; 20% renders "
            "as no band (its IEC encoding).",
        )
        self._rotation_combo = QComboBox()
        self._rotation_combo.addItems(["0°", "90°", "180°", "270°"])

        self._label_x_spin = QDoubleSpinBox()
        self._label_x_spin.setRange(-20.0, 20.0)
        self._label_x_spin.setSingleStep(0.5)
        self._label_x_spin.setDecimals(2)
        self._label_x_spin.setToolTip("Label X offset relative to component bbox")

        self._label_y_spin = QDoubleSpinBox()
        self._label_y_spin.setRange(-20.0, 20.0)
        self._label_y_spin.setSingleStep(0.5)
        self._label_y_spin.setDecimals(2)
        self._label_y_spin.setToolTip("Label Y offset relative to component bbox")

        self._reset_label_btn = QPushButton("↺")
        self._reset_label_btn.setFixedWidth(28)
        self._reset_label_btn.setToolTip("Reset label offset to default (0.05, 0.05)")
        self._reset_label_btn.clicked.connect(self._on_reset_label)

        label_x_row = QHBoxLayout()
        label_x_row.addWidget(self._label_x_spin)
        label_x_row.addWidget(self._reset_label_btn)

        self._apply_btn = QPushButton("Apply")

        self._footprint_label = QLabel()
        self._footprint_label.setWordWrap(True)
        self._footprint_label.setStyleSheet("color: #888; font-size: 10px;")

        self._pin_variant_combo = QComboBox()
        self._pin_variant_label = QLabel("Pinout:")
        self._pin_variant_row_visible = False

        self._body_variant_combo = QComboBox()
        self._body_variant_combo.setToolTip(
            "Physical body package (e.g. DO-35, DIN-0207).  Picking a "
            "variant overrides the library default for this instance only.",
        )

        # Per-instance visual style override (Appearance): body fill colour +
        # corner radius.  Text fields, committed via Apply like refdes/value;
        # an EMPTY field means "no override" (falls back to the option /
        # definition style, then the package / theme defaults).
        self._style_color_edit = QLineEdit()
        self._style_color_edit.setPlaceholderText("#RRGGBB[AA] — empty = default")
        self._style_color_edit.setToolTip(
            "Body fill colour override for this instance (CSS hex, alpha "
            "last).  Clear the field to fall back to the package colour.",
        )
        self._style_radius_edit = QLineEdit()
        self._style_radius_edit.setPlaceholderText("mm (1–4 values) — empty = default")
        self._style_radius_edit.setToolTip(
            "Body corner radius override in millimetres — 1 to 4 values in "
            "CSS border-radius order (0 = sharp corners).  Clear the field "
            "to fall back to the package edge style.",
        )

        form.addRow("RefDes:", self._refdes_edit)
        form.addRow("Value:", self._value_edit)
        form.addRow("Tolerance:", self._tolerance_edit)
        form.addRow("Rotation:", self._rotation_combo)
        form.addRow("Label X:", label_x_row)
        form.addRow("Label Y:", self._label_y_spin)
        form.addRow("Footprint:", self._footprint_label)
        self._pin_variant_form_row_label = QLabel("Pinout:")
        form.addRow(self._pin_variant_form_row_label, self._pin_variant_combo)
        self._body_variant_form_row_label = QLabel("Body:")
        form.addRow(self._body_variant_form_row_label, self._body_variant_combo)
        form.addRow("Body color:", self._style_color_edit)
        form.addRow("Corner radius:", self._style_radius_edit)
        form.addRow(self._apply_btn)
        layout.addWidget(grp)

        self._pin_variant_form_row_label.setVisible(False)
        self._pin_variant_combo.setVisible(False)
        self._body_variant_form_row_label.setVisible(False)
        self._body_variant_combo.setVisible(False)

        self._apply_btn.clicked.connect(self._on_apply)
        self._pin_variant_combo.currentIndexChanged.connect(
            self._on_pin_variant_changed
        )
        self._body_variant_combo.currentIndexChanged.connect(
            self._on_package_variant_changed,
        )
        self._current_id: uuid.UUID | None = None
        # The appearance the style fields were last populated with — Apply
        # compares against it so an unchanged form emits no no-op command.
        self._shown_appearance: Appearance | None = None
        # Set via set_session(); the panel fetches inst/placement/defn itself now
        # that it subscribes to selection directly (no MainWindow fan-out).
        self._session: "EditorSession | None" = None
        self._pin_variant_ids: list[str | None] = []
        # Parallel to _pin_variant_ids: address strings or None for default.
        self._body_variant_addresses: list[str | None] = []

        # Pin offsets group — dynamically populated, hidden when empty.
        self._pin_offsets_grp = QGroupBox("Pin Offsets (bent leads)")
        self._pin_offsets_form = QFormLayout(self._pin_offsets_grp)
        self._pin_offset_spins: dict[int, tuple[QDoubleSpinBox, QDoubleSpinBox]] = {}
        self._pin_bend_widgets: dict[int, _BendWidget] = {}
        layout.addWidget(self._pin_offsets_grp)
        self._pin_offsets_grp.setVisible(False)

        # Package parameters — schema-driven, shown when the selected component
        # has a package carrying a ParamSchema. Editability comes from the
        # schema's fields_view (a hole/null field is RW; a set/default/auto value
        # is read-only). Read-only is signalled non-colour (a "(set)"/"(auto)"
        # suffix + a non-editable field) — the user can have trouble with colour.
        self._params_grp = QGroupBox("Parameters")
        self._params_form = QFormLayout(self._params_grp)
        # path -> editor widget, rebuilt per selection.
        self._param_widgets: dict[str, QLineEdit] = {}
        layout.addWidget(self._params_grp)
        self._params_grp.setVisible(False)

        # Geometry debug table — read-only, shown when component selected.
        self._geo_grp = QGroupBox("Geometry")
        geo_layout = QVBoxLayout(self._geo_grp)
        geo_layout.setContentsMargins(2, 2, 2, 2)
        self._geo_table = QTableWidget(0, 2)
        self._geo_table.setHorizontalHeaderLabels(["Parameter", "Value"])
        self._geo_table.horizontalHeader().setStretchLastSection(True)
        self._geo_table.verticalHeader().setVisible(False)
        self._geo_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._geo_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._geo_table.setAlternatingRowColors(True)
        self._geo_table.setMinimumHeight(120)
        geo_layout.addWidget(self._geo_table)
        layout.addWidget(self._geo_grp)
        self._geo_grp.setVisible(False)

        # DRC group.
        drc_grp = QGroupBox("DRC")
        drc_layout = QVBoxLayout(drc_grp)
        self._drc_list = QListWidget()
        self._drc_list.setToolTip(
            "Click an issue to highlight the affected nets / wires / "
            "components and centre the view on the problem.  Click again "
            "to clear the highlight."
        )
        self._drc_list.itemClicked.connect(self._on_drc_item_clicked)
        # Tracks which issue (by row index) is currently "active" so a
        # second click on the same row toggles the highlight off.
        self._active_drc_row: int = -1
        drc_layout.addWidget(self._drc_list)
        layout.addWidget(drc_grp)
        layout.addStretch()

    # ------------------------------------------------------------------

    @traced()
    def set_session(self, session: "EditorSession") -> None:
        """Bind the session and subscribe to its selection broadcasts.

        The panel owns its own board -> panel sync (no MainWindow fan-out): it
        listens to ``selection_changed`` and pulls the selected instance's
        placement/definition straight off the session.  Re-binding on a session
        swap moves the subscription off the dead session.
        """
        if self._session is session:
            return
        if self._session is not None:
            try:
                self._session.selection_changed.disconnect(self._on_session_selection)
            except (TypeError, RuntimeError):
                pass
        self._session = session
        try:
            session.selection_changed.connect(self._on_session_selection)
        except (TypeError, RuntimeError):
            pass

    @traced()
    def _on_session_selection(
        self, comp_ids: object, _wire_ids: object, _net_id: object
    ) -> None:
        """Board -> panel: show the single selected component, else clear.

        Mirrors the old ``MainWindow._sync_properties_panel`` — moved here so the
        panel drives itself.  Only a lone component selection populates the
        editor; anything else (multi-select, wire, group, empty) clears it.
        """
        ids = (
            list(comp_ids)
            if isinstance(comp_ids, (set, frozenset, list, tuple))
            else []
        )
        if len(ids) != 1 or self._session is None:
            self.clear_component()
            return
        cid = ids[0]
        inst = self._session.instances.get(cid)
        if inst is None:
            self.clear_component()
            return
        placement = self._session.layout.placements.get(cid)
        defn = self._session.components.get(inst.definition_ref)
        self.show_component(
            inst,
            placement,
            defn,
            package_families=self._session.package_families,
        )

    @traced()
    def show_component(
        self,
        inst: ComponentInstance,
        placement: ComponentPlacement | None = None,
        defn: ComponentDefinition | None = None,
        package_families: "dict | None" = None,
    ) -> None:
        self._current_id = inst.id
        self._populate_basic_fields(inst, placement)
        self._rebuild_pin_offsets(
            defn,
            placement.pin_offsets if placement is not None else {},
            inst,
            package_families or {},
        )
        self._update_geometry_table(placement, defn, inst, package_families or {})
        self._rebuild_pin_variant_combo(defn, inst.pin_variant)
        self._rebuild_body_variant_combo(
            defn,
            inst.package_variant,
            package_families or {},
        )
        self._rebuild_parameters(defn, inst, package_families or {})

    @traced()
    def _populate_basic_fields(
        self,
        inst: ComponentInstance,
        placement: ComponentPlacement | None,
    ) -> None:
        """Write the refdes/value/footprint and (if placed) rotation/label fields."""
        self._refdes_edit.setText(inst.refdes)
        self._value_edit.setText(inst.value)
        self._tolerance_edit.setText(inst.tolerance or "")
        self._footprint_label.setText(inst.definition_ref)
        self._populate_appearance_fields(inst.appearance)
        if placement is not None:
            idx = placement.rotation // 90
            self._rotation_combo.setCurrentIndex(idx)
            self._label_x_spin.setValue(placement.label_offset.dx)
            self._label_y_spin.setValue(placement.label_offset.dy)

    @traced()
    def clear_component(self) -> None:
        self._current_id = None
        self._refdes_edit.clear()
        self._value_edit.clear()
        self._tolerance_edit.clear()
        self._footprint_label.clear()
        self._populate_appearance_fields(None)
        self._rebuild_pin_offsets(None, {})
        self._rebuild_pin_variant_combo(None, None)
        self._rebuild_body_variant_combo(None, None, {})
        self._rebuild_parameters(None, None, {})
        self._geo_table.setRowCount(0)
        self._geo_grp.setVisible(False)

    @traced()
    def update_issues(self, issues: list[DRCIssue]) -> None:
        # Refreshing the list invalidates any previously selected row;
        # tell the listener to clear its highlight so we don't leave a
        # stale entry on the HighlightStack.
        if self._active_drc_row >= 0:
            self.drc_issue_clicked.emit(None)
            self._active_drc_row = -1
        self._drc_list.clear()
        if not issues:
            item = QListWidgetItem("✓ No issues")
            item.setForeground(QColor("#228822"))
            self._drc_list.addItem(item)
            return
        for issue in issues:
            item = QListWidgetItem(f"[{issue.code}] {issue.message}")
            item.setForeground(_SEVERITY_COLORS.get(issue.severity, QColor()))
            # Stash the issue on the item so click handlers can recover
            # affected_nets / affected_wires / affected_components without
            # re-indexing.
            item.setData(Qt.ItemDataRole.UserRole, issue)
            self._drc_list.addItem(item)

    @traced()
    def _on_drc_item_clicked(self, item: QListWidgetItem) -> None:
        """Toggle highlight for the clicked DRC issue.

        First click on a row → emit ``drc_issue_clicked(issue)``; second
        click on the same row → emit ``drc_issue_clicked(None)`` (the
        listener treats None as "clear my transient highlight").
        """
        issue = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(issue, DRCIssue):
            return  # the "✓ No issues" sentinel item
        clicked_row = self._drc_list.row(item)
        if clicked_row == self._active_drc_row:
            self._active_drc_row = -1
            self._drc_list.clearSelection()
            self.drc_issue_clicked.emit(None)
        else:
            self._active_drc_row = clicked_row
            self.drc_issue_clicked.emit(issue)

    # ------------------------------------------------------------------
    # Pin offsets section
    # ------------------------------------------------------------------

    @traced()
    def _rebuild_pin_offsets(
        self,
        defn: ComponentDefinition | None,
        current_offsets: dict[int, Vector],
        instance: ComponentInstance | None = None,
        package_families: "dict | None" = None,
    ) -> None:
        while self._pin_offsets_form.rowCount():
            self._pin_offsets_form.removeRow(0)
        self._pin_offset_spins.clear()
        self._pin_bend_widgets = {}

        # Package-aware specs: the chosen footprint variant (incl. an SMD chip's
        # discrete mount points) drives bending. We pass the placed instance +
        # the catalogue so a part whose bodies live in a ``footprints:`` list
        # (e.g. ``R`` as ``SMD-0805``) shows the SMD mount picker, matching the
        # place tool — not the legacy axial ``bendable_pins``.
        from protoboard.domain.components.package import effective_bend_specs

        specs = (
            effective_bend_specs(defn, instance, package_families)
            if defn is not None
            else {}
        )
        if defn is None or not specs:
            self._pin_offsets_grp.setVisible(False)
            return

        for pin_num in sorted(specs.keys()):
            pin_name = defn.pin_name(pin_num)
            spec: PinBendSpec = specs[pin_num]
            current = current_offsets.get(pin_num, spec.default_offset)

            if spec.is_free():
                widget: _BendWidget = _FreeBendWidget(spec, current)
            else:
                continue

            widget.value_changed.connect(self._on_bend_changed)
            self._pin_bend_widgets[pin_num] = widget
            self._pin_offsets_form.addRow(f"Pin {pin_num} ({pin_name}):", widget)

        self._pin_offsets_grp.setVisible(bool(self._pin_bend_widgets))

    @traced()
    def _rebuild_parameters(
        self,
        defn: ComponentDefinition | None,
        inst: ComponentInstance | None,
        packages: "dict[str, Any]",
    ) -> None:
        """Show the resolved package's schema-declared parameters.

        Resolves the selected component's package (instance override → definition
        default → inline) and renders one row per ``schema`` field via
        ``fields_view``. Editability is read off each field's status: a null /
        ``REQUIRED_EMPTY`` field is editable (a hole the author left for the
        user); a concrete value, a filled default, or ``auto`` is read-only.
        Read-only is signalled *without colour* — a status suffix on the label
        (``(set)`` / ``(auto)``) and a non-editable field — because the user can
        have trouble distinguishing colours. Hidden when the package has no
        schema (synthetic bodies, or parts not yet on the schema format).
        """
        self._clear_params_form()
        if defn is None:
            self._params_grp.setVisible(False)
            return
        from protoboard.domain.components.package.component_package import (
            resolve_component_package,
        )

        package = resolve_component_package(defn, inst, packages)
        schema = getattr(package, "schema", None)
        raw_values = getattr(package, "raw_values", None)
        if schema is None or raw_values is None:
            self._params_grp.setVisible(False)
            return
        views = schema.fields_view(raw_values)
        if not views:
            self._params_grp.setVisible(False)
            return

        for view in views:
            self._add_param_row(view)
        self._params_grp.setVisible(True)

    @traced()
    def _clear_params_form(self) -> None:
        self._param_widgets = {}
        while self._params_form.rowCount():
            self._params_form.removeRow(0)

    @traced()
    def _add_param_row(self, view: "Any") -> None:
        """One label+editor row for a FieldView (read-only signalled non-colour)."""
        from protoboard.domain.components.package.schema import FieldStatus

        leaf = view.path.rsplit(".", 1)[-1]
        suffix = {
            FieldStatus.REQUIRED_EMPTY: "",  # editable → no suffix
            FieldStatus.SET: "  (set)",
            FieldStatus.AUTO: "  (auto)",
        }[view.status]
        label = QLabel(f"{leaf}{suffix}")
        edit = QLineEdit()
        edit.setText("" if view.value is None else str(view.value))
        edit.setReadOnly(not view.editable)
        if not view.editable:
            # Non-colour read-only cue: a clear-button-less, non-editable field
            # plus the label suffix. A tooltip explains why it cannot be edited.
            reason = (
                "computed by the package generator"
                if view.status is FieldStatus.AUTO
                else "fixed by the package definition"
            )
            edit.setToolTip(f"Read-only — {reason}.")
        else:
            edit.setToolTip("Editable — left open by the package definition.")
        self._params_form.addRow(label, edit)
        self._param_widgets[view.path] = edit

    @traced()
    def _update_geometry_table(
        self,
        placement: ComponentPlacement | None,
        defn: ComponentDefinition | None,
        inst: ComponentInstance | None = None,
        package_families: dict[str, PackageFamily] | None = None,
    ) -> None:
        self._geo_table.setRowCount(0)
        if placement is None:
            self._geo_grp.setVisible(False)
            return

        rows: list[tuple[str, str]] = []

        # Placement fields.
        rows.append(("origin.x", f"{placement.position.x:.4f}"))
        rows.append(("origin.y", f"{placement.position.y:.4f}"))
        rows.append(("rotation", f"{placement.rotation}°"))
        rows.append(("side", placement.side))
        rows.append(("vertical_flipped", str(placement.vertical_flipped)))

        # Per-pin geometry — resolved against the instance's chosen variant.
        if defn is not None:
            from protoboard.domain.components.package import resolved_footprint

            fp = resolved_footprint(defn, inst, package_families)
            fp_positions = fp.pin_positions()
            for i, fp_pos in enumerate(fp_positions):
                pin_num = i + 1
                off = placement.pin_offsets.get(pin_num, Vector(0.0, 0.0))
                world = pin_world_position(fp, placement, pin_num)
                rows.append(
                    (f"── pin {pin_num} fp_pos", f"({fp_pos.x:g}, {fp_pos.y:g})")
                )
                if off.dx != 0.0 or off.dy != 0.0:
                    rows.append(
                        (f"   pin {pin_num} offset", f"({off.dx:g}, {off.dy:g})")
                    )
                rows.append(
                    (f"   pin {pin_num} world", f"({world.x:.3f}, {world.y:.3f})")
                )

        self._geo_table.setRowCount(len(rows))
        for row_idx, (param, value) in enumerate(rows):
            param_item = QTableWidgetItem(param)
            param_item.setFont(self._geo_table.font())
            value_item = QTableWidgetItem(value)
            self._geo_table.setItem(row_idx, 0, param_item)
            self._geo_table.setItem(row_idx, 1, value_item)

        self._geo_table.resizeColumnToContents(0)
        self._geo_table.resizeRowsToContents()
        # Resize table height to fit content (capped at 400px).
        row_h = sum(
            self._geo_table.rowHeight(r) for r in range(self._geo_table.rowCount())
        )
        header_h = self._geo_table.horizontalHeader().height()
        self._geo_table.setMaximumHeight(min(row_h + header_h + 4, 400))
        self._geo_grp.setVisible(True)

    @traced()
    def _on_bend_changed(self) -> None:
        if self._current_id is not None:
            self.pin_offsets_changed.emit(self._current_id, self._collect_pin_offsets())

    @traced()
    def _collect_pin_offsets(self) -> dict[int, Vector]:
        result: dict[int, Vector] = {}
        for pin_num, widget in self._pin_bend_widgets.items():
            pt = widget.current_value()
            if pt.dx != 0.0 or pt.dy != 0.0:
                result[pin_num] = pt
        # Also collect legacy spin-box values (harmless if dict is empty)
        for pin_num, (sx, sy) in self._pin_offset_spins.items():
            x, y = sx.value(), sy.value()
            if x != 0.0 or y != 0.0:
                result[pin_num] = Vector(x, y)
        return result

    # ------------------------------------------------------------------

    @traced()
    def _rebuild_pin_variant_combo(
        self,
        defn: ComponentDefinition | None,
        current_variant: str | None,
    ) -> None:
        self._pin_variant_combo.blockSignals(True)
        self._pin_variant_combo.clear()
        self._pin_variant_ids = []
        has_variants = defn is not None and bool(defn.variants)
        self._pin_variant_form_row_label.setVisible(has_variants)
        self._pin_variant_combo.setVisible(has_variants)
        if has_variants and defn is not None:
            self._pin_variant_combo.addItem("Default")
            self._pin_variant_ids.append(None)
            for v in defn.variants:
                self._pin_variant_combo.addItem(v.label)
                self._pin_variant_ids.append(v.id)
            # Select the current variant.
            if current_variant is None:
                self._pin_variant_combo.setCurrentIndex(0)
            else:
                for i, vid in enumerate(self._pin_variant_ids):
                    if vid == current_variant:
                        self._pin_variant_combo.setCurrentIndex(i)
                        break
        self._pin_variant_combo.blockSignals(False)

    @traced()
    def _on_pin_variant_changed(self, index: int) -> None:
        if self._current_id is None or index < 0 or index >= len(self._pin_variant_ids):
            return
        self.pin_variant_changed.emit(self._current_id, self._pin_variant_ids[index])

    @traced()
    def _rebuild_body_variant_combo(
        self,
        defn: ComponentDefinition | None,
        current_variant: str | None,
        packages: "dict",
    ) -> None:
        """Repopulate the body-variant dropdown for the current instance.

        ``packages`` is the package-family catalogue (family_id →
        :class:`PackageFamily`).  Shown whenever the footprint has body choices
        (two-pin catalogue bodies, or the generated ``dip-N`` / ``wdip-N`` /
        ``sil-N`` for multi-pin in-line parts) and a catalogue is available.
        The list is:

        * ``Default`` — fall back to the definition's default package option
          (or the inline package when neither default nor instance override
          resolves).
        * The variants of the **definition's default family** first.
        * The remaining offered families' variants, so the user can swap a
          DIP-16 between narrow / wide (or a resistor's body to a brick)
          without leaving the panel.
        """
        from protoboard.domain.components.package.generated_families import (
            offered_body_families,
        )

        self._body_variant_combo.blockSignals(True)
        self._body_variant_combo.clear()
        self._body_variant_addresses = []

        offered = (
            offered_body_families(defn, packages)
            if defn is not None and packages
            else []
        )
        show = bool(offered)
        self._body_variant_form_row_label.setVisible(show)
        self._body_variant_combo.setVisible(show)
        if not show or defn is None:
            self._body_variant_combo.blockSignals(False)
            return

        # Default entry first.
        self._body_variant_combo.addItem("Default (library)")
        self._body_variant_addresses.append(None)

        # Definition's default family (if resolvable) sits at the top.
        primary_pkg_id: str | None = None
        default_ref = defn.default_package_ref()
        if default_ref:
            parts = default_ref.split("/", 1)
            if len(parts) == 2:
                primary_pkg_id = parts[0]

        # Offer order: the definition's default family first, then the rest.
        ordered = sorted(offered, key=lambda f: f.family_id != primary_pkg_id)

        for pkg in ordered:
            pkg_id = pkg.family_id
            for variant in pkg.variants:
                addr = f"{pkg_id}/{variant.variant_id}"
                # Display: "dip-16 / std — DIP-16 (narrow 0.3")" etc.
                label = f"{pkg_id} / {variant.variant_id}"
                if variant.label and variant.label != variant.variant_id:
                    label = f"{label} — {variant.label}"
                self._body_variant_combo.addItem(label)
                self._body_variant_addresses.append(addr)

        # Select the current value.
        if current_variant is None:
            self._body_variant_combo.setCurrentIndex(0)
        else:
            for i, addr in enumerate(self._body_variant_addresses):
                if addr == current_variant:
                    self._body_variant_combo.setCurrentIndex(i)
                    break
        self._body_variant_combo.blockSignals(False)

    @traced()
    def _on_package_variant_changed(self, index: int) -> None:
        if (
            self._current_id is None
            or index < 0
            or index >= len(self._body_variant_addresses)
        ):
            return
        self.package_variant_changed.emit(
            self._current_id,
            self._body_variant_addresses[index],
        )

    @traced()
    def _on_reset_label(self) -> None:
        self._label_x_spin.setValue(0.05)
        self._label_y_spin.setValue(0.05)

    @traced()
    def _populate_appearance_fields(self, appearance: Appearance | None) -> None:
        """Write *appearance* into the style fields (empty field = no override)."""
        self._shown_appearance = appearance
        color = appearance.color if appearance is not None else None
        self._style_color_edit.setText(color or "")
        radii = appearance.corner_radius_mm if appearance is not None else None
        self._style_radius_edit.setText(
            " ".join(f"{v:g}" for v in radii) if radii is not None else ""
        )

    @traced()
    def _collect_appearance(self) -> tuple[bool, Appearance | None]:
        """``(valid, appearance)`` from the style fields.

        Empty fields mean "no override" → ``(True, None)``.  Validation is the
        domain's (``parse_style``); a bad colour / radius yields
        ``(False, None)`` and the caller must not emit.
        """
        parts: list[str] = []
        color = self._style_color_edit.text().strip()
        radius = self._style_radius_edit.text().strip()
        if color:
            parts.append(f"color: {color}")
        if radius:
            parts.append(f"corner-radius: {radius}")
        if not parts:
            return True, None
        try:
            return True, parse_style("; ".join(parts), ctx="properties panel")
        except ValueError:
            return False, None

    @traced()
    def _on_apply(self) -> None:
        if self._current_id is None:
            return
        rotation = self._rotation_combo.currentIndex() * 90
        self.component_changed.emit(
            self._current_id,
            self._refdes_edit.text(),
            self._value_edit.text(),
            self._tolerance_edit.text(),
            rotation,
            self._label_x_spin.value(),
            self._label_y_spin.value(),
        )
        if self._pin_bend_widgets or self._pin_offset_spins:
            self.pin_offsets_changed.emit(
                self._current_id,
                self._collect_pin_offsets(),
            )
        valid, new_appearance = self._collect_appearance()
        if valid and new_appearance != self._shown_appearance:
            self._shown_appearance = new_appearance
            self.appearance_changed.emit(self._current_id, new_appearance)


class DRCPanel(QWidget):
    """Standalone DRC panel (kept for backward compat with session.drc_changed)."""

    @traced()
    def __init__(self, parent: object = None) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(QLabel("DRC"))
        self._list = QListWidget()
        layout.addWidget(self._list)

    @traced()
    def update_issues(self, issues: list[DRCIssue]) -> None:
        self._list.clear()
        if not issues:
            item = QListWidgetItem("✓ No issues")
            item.setForeground(QColor("#228822"))
            self._list.addItem(item)
            return
        for issue in issues:
            item = QListWidgetItem(f"[{issue.code}] {issue.message}")
            item.setForeground(_SEVERITY_COLORS.get(issue.severity, QColor()))
            self._list.addItem(item)


# ---------------------------------------------------------------------------
# Bend widgets — inline controls for PinBendSpec
# ---------------------------------------------------------------------------


class _BendWidget(QWidget):
    """Abstract base for bend-spec controls."""

    value_changed = Signal()

    @traced()
    def current_value(self) -> Vector:
        raise NotImplementedError


class _FreeBendWidget(_BendWidget):
    """Read-only display for a free-range (2D) PinBendSpec.

    The actual offset is set interactively via Ctrl+R drag on the canvas.
    This widget just shows the current (dx, dy) and the allowed reach range.
    """

    @traced()
    def __init__(
        self, spec: PinBendSpec, current: Vector, parent: object = None
    ) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        self._spec = spec
        self._current = current
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self._val_label = QLabel(self._fmt(current))
        self._val_label.setStyleSheet("font-family: monospace;")
        reach_label = QLabel(f"reach: {spec.min_reach:g}–{spec.max_reach:g} cells")
        reach_label.setStyleSheet("color: #888; font-size: 10px;")
        hint = QLabel("Edit: Ctrl+R on board")
        hint.setStyleSheet("color: #666; font-size: 10px;")
        layout.addWidget(self._val_label)
        layout.addWidget(reach_label)
        layout.addWidget(hint)

    @traced()
    def set_current(self, pt: Vector) -> None:
        self._current = pt
        self._val_label.setText(self._fmt(pt))

    @traced()
    def current_value(self) -> Vector:
        return self._current

    @staticmethod
    @traced()
    def _fmt(pt: Vector) -> str:
        import math as _math

        d = _math.hypot(pt.dx, pt.dy)
        return f"dx={pt.dx:g}  dy={pt.dy:g}  |d|={d:.2f}"
