# SPDX-License-Identifier: MIT
"""Preferences dialog — keyboard shortcuts, colours/alphas, library paths."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from protoboard.gui.app_settings import AppSettings, save_app_settings
from protoboard.gui.keybindings import DEFAULTS, DISPLAY_NAMES, KeyBindings
from protoboard.gui.theme import Theme, save_theme


# ---------------------------------------------------------------------------
# Helper — colour button
# ---------------------------------------------------------------------------

class _ColorButton(QPushButton):
    """Push button that shows a colour swatch and opens QColorDialog on click."""

    def __init__(self, color: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = QColor(color)
        self._update_swatch()
        self.setFixedWidth(56)
        self.clicked.connect(self._pick)

    def color_hex(self) -> str:
        return self._color.name(QColor.NameFormat.HexArgb)

    def set_color(self, color: str) -> None:
        self._color = QColor(color)
        self._update_swatch()

    def _update_swatch(self) -> None:
        r, g, b, a = self._color.red(), self._color.green(), self._color.blue(), self._color.alpha()
        self.setStyleSheet(
            f"background-color: rgba({r},{g},{b},{a}); border: 1px solid #666;"
        )

    def _pick(self) -> None:
        c = QColorDialog.getColor(
            self._color, self, "Choose colour",
            QColorDialog.ColorDialogOption.ShowAlphaChannel,
        )
        if c.isValid():
            self._color = c
            self._update_swatch()


# ---------------------------------------------------------------------------
# Page — Shortcuts
# ---------------------------------------------------------------------------

class _ShortcutsPage(QWidget):
    def __init__(self, kb: KeyBindings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._kb = kb
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self._table = QTableWidget(0, 2)
        self._table.setHorizontalHeaderLabels(["Action", "Shortcut"])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._table.verticalHeader().setVisible(False)
        layout.addWidget(self._table)

        self._seq_widgets: dict[str, QKeySequenceEdit] = {}
        for action_id in DEFAULTS:
            display = DISPLAY_NAMES.get(action_id, action_id)
            row = self._table.rowCount()
            self._table.insertRow(row)
            name_item = QTableWidgetItem(display)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, 0, name_item)
            seq_edit = QKeySequenceEdit(QKeySequence(kb.get(action_id) or ""))
            self._table.setCellWidget(row, 1, seq_edit)
            self._seq_widgets[action_id] = seq_edit

        reset_btn = QPushButton("Restore Defaults")
        reset_btn.clicked.connect(self._reset)
        layout.addWidget(reset_btn)

    def apply(self) -> None:
        for action_id, widget in self._seq_widgets.items():
            seq = widget.keySequence()
            self._kb.set(action_id, seq.toString() if not seq.isEmpty() else "")

    def _reset(self) -> None:
        for action_id, widget in self._seq_widgets.items():
            default = DEFAULTS.get(action_id, "")
            widget.setKeySequence(QKeySequence(default))


# ---------------------------------------------------------------------------
# Page — Colours & Alphas
# ---------------------------------------------------------------------------

def _row(label_text: str, widget: QWidget) -> QHBoxLayout:
    row = QHBoxLayout()
    lbl = QLabel(label_text)
    lbl.setMinimumWidth(220)
    row.addWidget(lbl)
    row.addWidget(widget)
    row.addStretch()
    return row


def _color_row(label: str, initial: str) -> tuple[QHBoxLayout, _ColorButton]:
    btn = _ColorButton(initial)
    return _row(label, btn), btn


def _opacity_row(label: str, initial: float) -> tuple[QHBoxLayout, QSlider]:
    """Slider 0–100 representing opacity 0.0–1.0, with a live numeric label."""
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setRange(0, 100)
    slider.setValue(int(round(initial * 100)))
    slider.setFixedWidth(120)
    value_label = QLabel(f"{initial:.2f}")
    value_label.setFixedWidth(35)
    slider.valueChanged.connect(lambda v, lbl=value_label: lbl.setText(f"{v/100:.2f}"))
    row = QHBoxLayout()
    row.addWidget(QLabel(label))
    row.addStretch()
    row.addWidget(slider)
    row.addWidget(value_label)
    return row, slider


def _alpha_row(label: str, initial: float, is_int: bool = False
               ) -> tuple[QHBoxLayout, QDoubleSpinBox | QSpinBox]:
    if is_int:
        sp: QSpinBox = QSpinBox()
        sp.setRange(0, 255)
        sp.setValue(int(initial))
        sp.setFixedWidth(80)
        return _row(label, sp), sp  # type: ignore[return-value]
    spf = QDoubleSpinBox()
    spf.setRange(0.0, 1.0)
    spf.setSingleStep(0.05)
    spf.setDecimals(2)
    spf.setValue(float(initial))
    spf.setFixedWidth(80)
    return _row(label, spf), spf  # type: ignore[return-value]


def _float_row(
    label: str, initial: float, lo: float, hi: float, step: float = 0.5, decimals: int = 1,
) -> tuple[QHBoxLayout, QDoubleSpinBox]:
    spf = QDoubleSpinBox()
    spf.setRange(lo, hi)
    spf.setSingleStep(step)
    spf.setDecimals(decimals)
    spf.setValue(float(initial))
    spf.setFixedWidth(80)
    return _row(label, spf), spf


class _ColorsPage(QWidget):
    def __init__(self, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = theme

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        scroll.setWidget(inner)
        main = QVBoxLayout(self)
        main.setContentsMargins(0, 0, 0, 0)
        main.addWidget(scroll)
        vbox = QVBoxLayout(inner)
        vbox.setSpacing(12)

        self._widgets: dict[str, _ColorButton | QDoubleSpinBox | QSpinBox | QSlider] = {}

        def add_section(title: str, rows: list[tuple[str, str, object]]) -> None:  # type: ignore[misc]
            """rows: (field_name, label, initial[, (lo, hi)]) — 4th element enables free range."""
            grp = QGroupBox(title)
            gl = QVBoxLayout(grp)
            for entry in rows:  # type: ignore[assignment]
                field_name, label, init = entry[0], entry[1], entry[2]
                range_hint: tuple[float, float] | None = entry[3] if len(entry) > 3 else None  # type: ignore[misc]
                if isinstance(init, str):
                    row_layout, w = _color_row(label, init)
                elif isinstance(init, int):
                    row_layout, w = _alpha_row(label, init, is_int=True)
                elif range_hint is not None:
                    row_layout, w = _float_row(label, float(init), range_hint[0], range_hint[1])  # type: ignore[arg-type]
                else:
                    row_layout, w = _alpha_row(label, float(init))  # type: ignore[arg-type]
                gl.addLayout(row_layout)
                self._widgets[field_name] = w
            vbox.addWidget(grp)

        def add_opacity_section(title: str, rows: list[tuple[str, str, float]]) -> None:
            """rows: (field_name, label, initial_float_0_1) — rendered as sliders."""
            grp = QGroupBox(title)
            gl = QVBoxLayout(grp)
            for field_name, label, init in rows:
                row_layout, slider = _opacity_row(label, init)
                gl.addLayout(row_layout)
                self._widgets[field_name] = slider
            vbox.addWidget(grp)

        add_section("Board & Pads", [
            ("board_bg",         "Board background",    theme.board_bg),
            ("pad_color",        "Pad colour",          theme.pad_color),
            ("strip_color",      "Strip colour",        theme.strip_color),
            ("hole_color",       "Hole colour",         theme.hole_color),
            ("isolated_x_color", "Strip cut X colour",  theme.isolated_x_color),
        ])

        add_section("Wires", [
            ("bare_outline_color",  "Bare wire outline colour",      theme.bare_outline_color),
            ("bare_width",          "Bare wire fill width",           theme.bare_width),
            ("bare_outline_width",  "Bare wire outline width",        theme.bare_outline_width),
            ("ins_outline_color",   "Insulated wire outline colour",  theme.ins_outline_color),
            ("ins_width",           "Insulated wire fill width",      theme.ins_width),
            ("ins_outline_width",   "Insulated wire outline width",   theme.ins_outline_width),
            ("ratsnest_color",      "Ratsnest colour",                theme.ratsnest_color),
        ])

        # net_colors palette (8 entries)
        net_grp = QGroupBox("Net colour palette")
        net_gl = QVBoxLayout(net_grp)
        self._net_color_btns: list[_ColorButton] = []
        for i, c in enumerate(theme.net_colors):
            row_layout, btn = _color_row(f"Net colour {i + 1}", c)
            net_gl.addLayout(row_layout)
            self._net_color_btns.append(btn)
        vbox.addWidget(net_grp)

        add_section("Components", [
            ("comp_fill_color",   "Component fill colour",    theme.comp_fill_color),
            ("comp_border",       "Component border colour",  theme.comp_border),
            ("refdes_color",      "Label text colour",        theme.refdes_color),
            ("label_bg_color",    "Label background colour",  theme.label_bg_color),
            ("overlay_font_size", "Label font size (pt)",     theme.overlay_font_size, (2.0, 32.0)),
            ("lead_color",        "Component lead colour",    theme.lead_color),
        ])

        add_opacity_section("Layer opacity", [
            ("back_alpha",        "Back-side opacity",       theme.back_alpha),
            ("board_ghost_alpha", "Board ghost opacity",     theme.board_ghost_alpha),
            ("ghost_comp_alpha",  "Component ghost opacity", theme.ghost_comp_alpha),
        ])

        add_opacity_section("Net / group highlight dim", [
            ("highlight_dim_components", "Dimmed components opacity", theme.highlight_dim_components),
            ("highlight_dim_wires",      "Dimmed wires opacity",      theme.highlight_dim_wires),
        ])

        add_section("Groups", [
            ("group_pen_color",  "Group outline colour", theme.group_pen_color),
            ("group_pen_width",  "Group outline width",  theme.group_pen_width),
            ("group_fill_color", "Group fill colour",    theme.group_fill_color),
            ("group_name_color", "Group label colour",   theme.group_name_color),
        ])

        add_section("Selection", [
            ("select_color",       "Selection outline colour",       theme.select_color),
            ("select_width",       "Selection outline width",        theme.select_width),
            ("multi_select_color", "Multi-selection outline colour", theme.multi_select_color),
            ("multi_select_width", "Multi-selection outline width",  theme.multi_select_width),
        ])

        add_section("Active group", [
            ("group_active_color",      "Active group outline colour", theme.group_active_color),
            ("group_active_fill_color", "Active group fill colour",    theme.group_active_fill_color),
        ])

        add_section("Tool feedback", [
            ("placement_reach_color",      "Reachable pad colour",    theme.placement_reach_color),
            ("placement_reach_line_color", "Reach line colour",       theme.placement_reach_line_color),
            ("routing_preview_color",      "Route preview colour",    theme.routing_preview_color),
        ])

        add_section("Component pins & markers", [
            ("pin_width_complete",           "Pin width (with net)",      theme.pin_width_complete),
            ("pin_width_incomplete",         "Pin width (no net)",        theme.pin_width_incomplete),
            ("pin_label_fallback_color",     "Pin label fallback colour", theme.pin_label_fallback_color),
            ("pin1_marker_color",            "Pin 1 marker colour",       theme.pin1_marker_color),
            ("component_origin_cross_color", "Origin cross colour",       theme.component_origin_cross_color),
            ("component_origin_cross_width", "Origin cross width",        theme.component_origin_cross_width),
        ])

        add_section("Component body", [
            ("body_led_stripe_color",    "LED stripe colour",       theme.body_led_stripe_color),
            ("body_diode_cathode_color", "Diode cathode colour",    theme.body_diode_cathode_color),
            ("body_diode_anode_color",   "Diode anode colour",      theme.body_diode_anode_color),
            ("exported_pin_color",       "Exported pin colour",     theme.exported_pin_color),
            ("exported_pin_pen_color",   "Exported pin pen colour", theme.exported_pin_pen_color),
            ("exported_pin_pen_width",   "Exported pin pen width",  theme.exported_pin_pen_width),
        ])

        add_opacity_section("Component body opacity", [
            ("body_tab_opacity",  "Tab opacity",  theme.body_tab_opacity),
            ("body_slot_opacity", "Slot opacity", theme.body_slot_opacity),
        ])

        reset_btn = QPushButton("Restore Defaults")
        reset_btn.clicked.connect(self._reset)
        vbox.addWidget(reset_btn)
        vbox.addStretch()

    def apply(self) -> None:
        for field_name, widget in self._widgets.items():
            if isinstance(widget, _ColorButton):
                setattr(self._theme, field_name, widget.color_hex())
            elif isinstance(widget, QSlider):
                setattr(self._theme, field_name, widget.value() / 100.0)
            elif isinstance(widget, QSpinBox):
                setattr(self._theme, field_name, widget.value())
            else:
                setattr(self._theme, field_name, widget.value())
        # net_colors palette
        self._theme.net_colors = [btn.color_hex() for btn in self._net_color_btns]
        save_theme(self._theme)

    def _reset(self) -> None:
        defaults = Theme()
        for field_name, widget in self._widgets.items():
            default_val = getattr(defaults, field_name)
            if isinstance(widget, _ColorButton):
                widget.set_color(str(default_val))
            elif isinstance(widget, QSlider):
                widget.setValue(int(round(float(default_val) * 100)))
            elif isinstance(widget, QSpinBox):
                widget.setValue(int(default_val))
            else:
                widget.setValue(float(default_val))
        for i, btn in enumerate(self._net_color_btns):
            btn.set_color(defaults.net_colors[i])


# ---------------------------------------------------------------------------
# Page — Paths
# ---------------------------------------------------------------------------

class _PathListWidget(QWidget):
    """Editable list of directory paths with + / − / ↑ / ↓ buttons."""

    def __init__(self, paths: list[Path], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)

        self._list = QListWidget()
        for p in paths:
            self._list.addItem(str(p))
        h.addWidget(self._list)

        btn_col = QVBoxLayout()
        add_btn = QPushButton("+")
        add_btn.setFixedWidth(28)
        add_btn.clicked.connect(self._add)
        remove_btn = QPushButton("−")
        remove_btn.setFixedWidth(28)
        remove_btn.clicked.connect(self._remove)
        up_btn = QPushButton("↑")
        up_btn.setFixedWidth(28)
        up_btn.clicked.connect(self._move_up)
        down_btn = QPushButton("↓")
        down_btn.setFixedWidth(28)
        down_btn.clicked.connect(self._move_down)
        for b in (add_btn, remove_btn, up_btn, down_btn):
            btn_col.addWidget(b)
        btn_col.addStretch()
        h.addLayout(btn_col)

    def paths(self) -> list[Path]:
        return [Path(self._list.item(i).text()) for i in range(self._list.count())]

    def _add(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose directory")
        if folder:
            self._list.addItem(folder)

    def _remove(self) -> None:
        for item in self._list.selectedItems():
            self._list.takeItem(self._list.row(item))

    def _move_up(self) -> None:
        row = self._list.currentRow()
        if row > 0:
            item = self._list.takeItem(row)
            self._list.insertItem(row - 1, item)
            self._list.setCurrentRow(row - 1)

    def _move_down(self) -> None:
        row = self._list.currentRow()
        if row < self._list.count() - 1:
            item = self._list.takeItem(row)
            self._list.insertItem(row + 1, item)
            self._list.setCurrentRow(row + 1)


class _PathsPage(QWidget):
    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        vbox = QVBoxLayout(self)
        vbox.setSpacing(12)

        # Default projects directory
        proj_grp = QGroupBox("Default projects directory")
        proj_layout = QHBoxLayout(proj_grp)
        self._proj_edit = QPushButton(
            str(settings.default_projects_dir) if settings.default_projects_dir else "(not set)"
        )
        self._proj_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._proj_edit.clicked.connect(self._pick_projects_dir)
        clear_btn = QPushButton("Clear")
        clear_btn.setFixedWidth(56)
        clear_btn.clicked.connect(self._clear_projects_dir)
        proj_layout.addWidget(self._proj_edit)
        proj_layout.addWidget(clear_btn)
        vbox.addWidget(proj_grp)

        # Component library directories
        lib_grp = QGroupBox("Additional component library directories")
        lib_layout = QVBoxLayout(lib_grp)
        lib_note = QLabel(
            "YAML files in these directories are merged with the built-in library.\n"
            "Earlier entries take priority (override built-in definitions)."
        )
        lib_note.setStyleSheet("color: #888; font-size: 11px;")
        lib_note.setWordWrap(True)
        lib_layout.addWidget(lib_note)
        self._lib_list = _PathListWidget(settings.component_library_dirs)
        lib_layout.addWidget(self._lib_list)
        vbox.addWidget(lib_grp)

        # Module directories
        mod_grp = QGroupBox("Module directories")
        mod_layout = QVBoxLayout(mod_grp)
        mod_note = QLabel(
            "Directories containing reusable circuit module definitions (YAML).\n"
            "Modules are pre-wired groups of components that can be placed as a unit."
        )
        mod_note.setStyleSheet("color: #888; font-size: 11px;")
        mod_note.setWordWrap(True)
        mod_layout.addWidget(mod_note)
        self._mod_list = _PathListWidget(settings.module_dirs)
        mod_layout.addWidget(self._mod_list)
        vbox.addWidget(mod_grp)

        # Placement behaviour
        place_grp = QGroupBox("Placement")
        place_layout = QVBoxLayout(place_grp)
        self._auto_accept_cb = QCheckBox(
            "Auto-accept when no conflict (no Enter required)"
        )
        self._auto_accept_cb.setChecked(settings.auto_accept_placement)
        place_layout.addWidget(self._auto_accept_cb)
        vbox.addWidget(place_grp)

        vbox.addStretch()

        self._default_projects_dir: Path | None = settings.default_projects_dir

    def apply(self) -> None:
        self._settings.default_projects_dir = self._default_projects_dir
        self._settings.component_library_dirs = self._lib_list.paths()
        self._settings.module_dirs = self._mod_list.paths()
        self._settings.auto_accept_placement = self._auto_accept_cb.isChecked()
        save_app_settings(self._settings)

    def _pick_projects_dir(self) -> None:
        start = str(self._default_projects_dir) if self._default_projects_dir else str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Choose default projects directory", start)
        if folder:
            self._default_projects_dir = Path(folder)
            self._proj_edit.setText(folder)

    def _clear_projects_dir(self) -> None:
        self._default_projects_dir = None
        self._proj_edit.setText("(not set)")


# ---------------------------------------------------------------------------
# Page — Routing
# ---------------------------------------------------------------------------


class _RoutingPage(QWidget):
    """Routing settings page — cost model and general routing parameters."""

    def __init__(self, session: object, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Import here to avoid circular dependency with domain layer.
        from protoboard.domain.routing_settings import ROUTING_DEFAULTS, RoutingSettings
        from protoboard.gui.session import EditorSession

        self._session: EditorSession = session  # type: ignore[assignment]
        self._defaults = ROUTING_DEFAULTS

        vbox = QVBoxLayout(self)
        vbox.setSpacing(10)
        vbox.setContentsMargins(8, 8, 8, 8)

        # --- General group ---
        gen_grp = QGroupBox("General")
        gen_form = _RoutingFormLayout(gen_grp)

        from PySide6.QtWidgets import QComboBox
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
        self._wire_mode.addItems(["auto", "bare", "insulated"])
        gen_form.addRow("Wire mode", self._wire_mode)

        self._allow_jumpers = QCheckBox()
        gen_form.addRow("Allow jumpers", self._allow_jumpers)

        self._max_detour = _cost_spinbox(0.0, 20.0, 0.5, 1)
        self._max_detour.setSpecialValueText("unlimited")
        gen_form.addRow("Max detour factor", self._max_detour)

        gen_grp.setLayout(gen_form)
        vbox.addWidget(gen_grp)

        # --- Cost model group ---
        cost_grp = QGroupBox("Cost model")
        cost_form = _RoutingFormLayout(cost_grp)

        self._direction_change = _cost_spinbox(0.0, 20.0, 0.1, 2)
        cost_form.addRow("Direction change", self._direction_change)

        self._crossing_insulated_bare = _cost_spinbox(0.0, 50.0, 0.5, 1)
        cost_form.addRow("Insulated × bare wire", self._crossing_insulated_bare)

        self._layer_change = _cost_spinbox(0.0, 20.0, 0.5, 1)
        cost_form.addRow("Layer change (via)", self._layer_change)

        self._under_component_top = _cost_spinbox(0.0, 20.0, 0.5, 1)
        cost_form.addRow("Under component (top)", self._under_component_top)

        cost_grp.setLayout(cost_form)
        vbox.addWidget(cost_grp)

        # --- Restore defaults button ---
        restore_row = QHBoxLayout()
        restore_row.addStretch()
        restore_btn = QPushButton("Restore defaults")
        restore_btn.clicked.connect(self._restore_defaults)
        restore_row.addWidget(restore_btn)
        vbox.addLayout(restore_row)

        vbox.addStretch()

        self._load_from_settings(self._session.routing_settings)

    def _load_from_settings(self, s: object) -> None:
        from protoboard.domain.routing_settings import RoutingSettings
        rs: RoutingSettings = s  # type: ignore[assignment]
        idx = self._preferred_side.findText(rs.preferred_side)
        self._preferred_side.setCurrentIndex(max(idx, 0))
        self._allow_layer_change.setChecked(rs.allow_layer_change)
        self._max_rip_up.setValue(rs.max_rip_up_passes)
        self._max_optimize.setValue(rs.max_optimize_iterations)
        self._direction_change.setValue(rs.direction_change)
        self._crossing_insulated_bare.setValue(rs.crossing_insulated_bare)
        self._layer_change.setValue(rs.layer_change)
        self._under_component_top.setValue(rs.under_component_top)
        idx = self._wire_mode.findText(rs.wire_mode)
        self._wire_mode.setCurrentIndex(max(idx, 0))
        self._allow_jumpers.setChecked(rs.allow_jumpers)
        self._max_detour.setValue(rs.max_detour_factor)

    def _restore_defaults(self) -> None:
        self._load_from_settings(self._defaults)

    def apply(self) -> None:
        from protoboard.domain.routing_settings import RoutingSettings
        new_settings = RoutingSettings(
            preferred_side=self._preferred_side.currentText(),  # type: ignore[arg-type]
            allow_layer_change=self._allow_layer_change.isChecked(),
            max_rip_up_passes=self._max_rip_up.value(),
            max_optimize_iterations=self._max_optimize.value(),
            direction_change=self._direction_change.value(),
            crossing_insulated_bare=self._crossing_insulated_bare.value(),
            layer_change=self._layer_change.value(),
            under_component_top=self._under_component_top.value(),
            wire_mode=self._wire_mode.currentText(),  # type: ignore[arg-type]
            allow_jumpers=self._allow_jumpers.isChecked(),
            max_detour_factor=self._max_detour.value(),
        )
        self._session.routing_settings = new_settings
        self._session.mark_dirty()


def _RoutingFormLayout(parent: QWidget) -> "QFormLayout":
    from PySide6.QtWidgets import QFormLayout
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


# ---------------------------------------------------------------------------
# Main dialog
# ---------------------------------------------------------------------------

class PreferencesDialog(QDialog):
    """Application preferences — Shortcuts, Colours, Paths, Routing."""

    def __init__(
        self,
        keybindings: KeyBindings,
        theme: Theme,
        app_settings: AppSettings,
        on_keybindings_applied: Callable[[], None] | None = None,
        on_theme_applied: Callable[[Theme], None] | None = None,
        on_paths_applied: Callable[[AppSettings], None] | None = None,
        session: object | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.setMinimumSize(680, 520)
        self.setWindowFlags(
            self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint
        )

        self._on_keybindings_applied = on_keybindings_applied
        self._on_theme_applied = on_theme_applied
        self._on_paths_applied = on_paths_applied
        self._theme = theme

        outer = QHBoxLayout(self)

        # Left navigation list
        self._nav = QListWidget()
        self._nav.setFixedWidth(140)
        self._nav.setSpacing(2)
        for label in ("Shortcuts", "Colours", "Paths", "Routing"):
            item = QListWidgetItem(label)
            item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self._nav.addItem(item)
        outer.addWidget(self._nav)

        # Right stacked pages
        self._stack = QStackedWidget()
        self._shortcuts_page = _ShortcutsPage(keybindings)
        self._colors_page = _ColorsPage(theme)
        self._paths_page = _PathsPage(app_settings)
        self._routing_page: _RoutingPage | None = (
            _RoutingPage(session, self) if session is not None else None
        )
        self._stack.addWidget(self._shortcuts_page)
        self._stack.addWidget(self._colors_page)
        self._stack.addWidget(self._paths_page)
        if self._routing_page is not None:
            self._stack.addWidget(self._routing_page)
        else:
            # Placeholder when no session (e.g. no project open)
            placeholder = QLabel("Open a project to configure routing settings.")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._stack.addWidget(placeholder)
        outer.addWidget(self._stack)

        self._nav.currentRowChanged.connect(self._stack.setCurrentIndex)
        self._nav.setCurrentRow(0)

        # Button box
        right_col = QVBoxLayout()
        right_col.addWidget(self._stack)
        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.accepted.connect(self._on_ok)
        btn_box.rejected.connect(self.reject)
        btn_box.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._apply_current)
        right_col.addWidget(btn_box)

        # Re-layout: nav on left, (stack + buttons) on right
        outer.takeAt(1)  # remove stack added earlier
        outer.addLayout(right_col)

    def _apply_current(self) -> None:
        idx = self._nav.currentRow()
        if idx == 0:
            self._shortcuts_page.apply()
            if self._on_keybindings_applied:
                self._on_keybindings_applied()
        elif idx == 1:
            self._colors_page.apply()
            if self._on_theme_applied:
                self._on_theme_applied(self._theme)
        elif idx == 2:
            self._paths_page.apply()
            if self._on_paths_applied:
                self._on_paths_applied(self._paths_page._settings)
        elif idx == 3 and self._routing_page is not None:
            self._routing_page.apply()

    def _on_ok(self) -> None:
        # Apply all pages
        self._shortcuts_page.apply()
        if self._on_keybindings_applied:
            self._on_keybindings_applied()
        self._colors_page.apply()
        if self._on_theme_applied:
            self._on_theme_applied(self._theme)
        self._paths_page.apply()
        if self._on_paths_applied:
            self._on_paths_applied(self._paths_page._settings)
        if self._routing_page is not None:
            self._routing_page.apply()
        self.accept()
