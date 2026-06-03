# SPDX-License-Identifier: MIT
"""Properties panel — selected component editor + DRC list."""

from __future__ import annotations

import uuid
from typing import Any

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

from protoboard.domain.components.definition import ComponentDefinition, PinBendSpec
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.placement import ComponentPlacement, pin_world_position
from protoboard.geometry import Point
from protoboard.services.drc.engine import DRCIssue

_SEVERITY_COLORS = {
    "error": QColor("#CC2222"),
    "warning": QColor("#CC8800"),
    "info": QColor("#2255AA"),
}


class PropertiesPanel(QWidget):
    """Shows editable fields for the selected component and DRC issues."""

    # id, refdes, value, rotation, label_offset_x, label_offset_y
    component_changed = Signal(uuid.UUID, str, str, int, float, float)
    # id, {pin_number: Point} — emitted when Apply is clicked and offsets changed
    pin_offsets_changed = Signal(uuid.UUID, object)
    # id, pin_variant (str or None)
    pin_variant_changed = Signal(uuid.UUID, object)
    # DRCIssue or None (None = the user clicked away or no issue selected).
    # MainWindow translates this into HighlightStack pushes + viewport centring.
    drc_issue_clicked = Signal(object)

    def __init__(self, parent: object = None) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # Component properties group.
        grp = QGroupBox("Component")
        form = QFormLayout(grp)
        self._refdes_edit = QLineEdit()
        self._value_edit = QLineEdit()
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

        form.addRow("RefDes:", self._refdes_edit)
        form.addRow("Value:", self._value_edit)
        form.addRow("Rotation:", self._rotation_combo)
        form.addRow("Label X:", label_x_row)
        form.addRow("Label Y:", self._label_y_spin)
        form.addRow("Footprint:", self._footprint_label)
        self._pin_variant_form_row_label = QLabel("Pinout:")
        form.addRow(self._pin_variant_form_row_label, self._pin_variant_combo)
        form.addRow(self._apply_btn)
        layout.addWidget(grp)

        self._pin_variant_form_row_label.setVisible(False)
        self._pin_variant_combo.setVisible(False)

        self._apply_btn.clicked.connect(self._on_apply)
        self._pin_variant_combo.currentIndexChanged.connect(self._on_pin_variant_changed)
        self._current_id: uuid.UUID | None = None
        self._pin_variant_ids: list[str | None] = []

        # Pin offsets group — dynamically populated, hidden when empty.
        self._pin_offsets_grp = QGroupBox("Pin Offsets (bent leads)")
        self._pin_offsets_form = QFormLayout(self._pin_offsets_grp)
        self._pin_offset_spins: dict[int, tuple[QDoubleSpinBox, QDoubleSpinBox]] = {}
        self._pin_bend_widgets: dict[int, _BendWidget] = {}
        layout.addWidget(self._pin_offsets_grp)
        self._pin_offsets_grp.setVisible(False)

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

    def show_component(
        self,
        inst: ComponentInstance,
        placement: ComponentPlacement | None = None,
        defn: ComponentDefinition | None = None,
    ) -> None:
        self._current_id = inst.id
        self._refdes_edit.setText(inst.refdes)
        self._value_edit.setText(inst.value)
        self._footprint_label.setText(inst.definition_ref)
        if placement is not None:
            idx = placement.rotation // 90
            self._rotation_combo.setCurrentIndex(idx)
            self._label_x_spin.setValue(placement.label_offset.x)
            self._label_y_spin.setValue(placement.label_offset.y)
        self._rebuild_pin_offsets(
            defn,
            placement.pin_offsets if placement is not None else {},
        )
        self._update_geometry_table(placement, defn)
        self._rebuild_pin_variant_combo(defn, inst.pin_variant)

    def clear_component(self) -> None:
        self._current_id = None
        self._refdes_edit.clear()
        self._value_edit.clear()
        self._footprint_label.clear()
        self._rebuild_pin_offsets(None, {})
        self._rebuild_pin_variant_combo(None, None)
        self._geo_table.setRowCount(0)
        self._geo_grp.setVisible(False)

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

    def _rebuild_pin_offsets(
        self,
        defn: ComponentDefinition | None,
        current_offsets: dict[int, Point],
    ) -> None:
        while self._pin_offsets_form.rowCount():
            self._pin_offsets_form.removeRow(0)
        self._pin_offset_spins.clear()
        self._pin_bend_widgets = {}

        if defn is None or not defn.bendable_pins:
            self._pin_offsets_grp.setVisible(False)
            return

        for pin_num in sorted(defn.bendable_pins.keys()):
            pin_name = defn.pin_name(pin_num)
            spec: PinBendSpec = defn.bendable_pins[pin_num]
            current = current_offsets.get(pin_num, spec.default_offset)

            if spec.is_free():
                widget: _BendWidget = _FreeBendWidget(spec, current)
            elif spec.discrete:
                widget = _DiscreteBendWidget(spec, current)
            else:
                continue

            widget.value_changed.connect(self._on_bend_changed)
            self._pin_bend_widgets[pin_num] = widget
            self._pin_offsets_form.addRow(f"Pin {pin_num} ({pin_name}):", widget)

        self._pin_offsets_grp.setVisible(bool(self._pin_bend_widgets))

    def _update_geometry_table(
        self,
        placement: ComponentPlacement | None,
        defn: ComponentDefinition | None,
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

        # Per-pin geometry.
        if defn is not None:
            fp_positions = defn.footprint.pin_positions()
            for i, fp_pos in enumerate(fp_positions):
                pin_num = i + 1
                off = placement.pin_offsets.get(pin_num, Point(0.0, 0.0))
                world = pin_world_position(defn.footprint, placement, pin_num)
                rows.append((f"── pin {pin_num} fp_pos", f"({fp_pos.x:g}, {fp_pos.y:g})"))
                if off.x != 0.0 or off.y != 0.0:
                    rows.append((f"   pin {pin_num} offset", f"({off.x:g}, {off.y:g})"))
                rows.append((f"   pin {pin_num} world", f"({world.x:.3f}, {world.y:.3f})"))

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

    def _on_bend_changed(self) -> None:
        if self._current_id is not None:
            self.pin_offsets_changed.emit(self._current_id, self._collect_pin_offsets())

    def _collect_pin_offsets(self) -> dict[int, Point]:
        result: dict[int, Point] = {}
        for pin_num, widget in self._pin_bend_widgets.items():
            pt = widget.current_value()
            if pt.x != 0.0 or pt.y != 0.0:
                result[pin_num] = pt
        # Also collect legacy spin-box values (harmless if dict is empty)
        for pin_num, (sx, sy) in self._pin_offset_spins.items():
            x, y = sx.value(), sy.value()
            if x != 0.0 or y != 0.0:
                result[pin_num] = Point(x, y)
        return result

    # ------------------------------------------------------------------

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

    def _on_pin_variant_changed(self, index: int) -> None:
        if self._current_id is None or index < 0 or index >= len(self._pin_variant_ids):
            return
        self.pin_variant_changed.emit(self._current_id, self._pin_variant_ids[index])

    def _on_reset_label(self) -> None:
        self._label_x_spin.setValue(0.05)
        self._label_y_spin.setValue(0.05)

    def _on_apply(self) -> None:
        if self._current_id is None:
            return
        rotation = self._rotation_combo.currentIndex() * 90
        self.component_changed.emit(
            self._current_id,
            self._refdes_edit.text(),
            self._value_edit.text(),
            rotation,
            self._label_x_spin.value(),
            self._label_y_spin.value(),
        )
        if self._pin_bend_widgets or self._pin_offset_spins:
            self.pin_offsets_changed.emit(
                self._current_id,
                self._collect_pin_offsets(),
            )


class DRCPanel(QWidget):
    """Standalone DRC panel (kept for backward compat with session.drc_changed)."""

    def __init__(self, parent: object = None) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(QLabel("DRC"))
        self._list = QListWidget()
        layout.addWidget(self._list)

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

    def current_value(self) -> Point:
        raise NotImplementedError


class _FreeBendWidget(_BendWidget):
    """Read-only display for a free-range (2D) PinBendSpec.

    The actual offset is set interactively via Ctrl+R drag on the canvas.
    This widget just shows the current (dx, dy) and the allowed reach range.
    """

    def __init__(self, spec: PinBendSpec, current: Point, parent: object = None) -> None:
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

    def set_current(self, pt: Point) -> None:
        self._current = pt
        self._val_label.setText(self._fmt(pt))

    def current_value(self) -> Point:
        return self._current

    @staticmethod
    def _fmt(pt: Point) -> str:
        import math as _math
        d = _math.hypot(pt.x, pt.y)
        return f"dx={pt.x:g}  dy={pt.y:g}  |d|={d:.2f}"


class _DiscreteBendWidget(_BendWidget):
    """Combo-box for a discrete PinBendSpec."""

    def __init__(self, spec: PinBendSpec, current: Point, parent: object = None) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        self._spec = spec
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._combo = QComboBox()
        for pt in spec.discrete:
            self._combo.addItem(f"({pt.x:g}, {pt.y:g})", userData=pt)
        # Select the currently active offset.
        for i, pt in enumerate(spec.discrete):
            if abs(pt.x - current.x) < 1e-6 and abs(pt.y - current.y) < 1e-6:
                self._combo.setCurrentIndex(i)
                break
        layout.addWidget(self._combo)
        self._combo.currentIndexChanged.connect(lambda _: self.value_changed.emit())

    def current_value(self) -> Point:
        pt = self._combo.currentData()
        return pt if isinstance(pt, Point) else Point(0.0, 0.0)
