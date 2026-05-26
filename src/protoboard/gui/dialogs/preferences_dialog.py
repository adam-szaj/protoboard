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
        # Initial values captured per-action so apply() can detect which
        # widgets the user actually edited.  Without this we would call
        # KeyBindings.set() on every row — including untouched empty ones —
        # which silently turns "use default" into "disabled" overrides.
        self._initial_values: dict[str, str] = {}
        for action_id in DEFAULTS:
            display = DISPLAY_NAMES.get(action_id, action_id)
            row = self._table.rowCount()
            self._table.insertRow(row)
            name_item = QTableWidgetItem(display)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, 0, name_item)
            initial = kb.get(action_id) or ""
            seq_edit = QKeySequenceEdit(QKeySequence(initial))
            self._table.setCellWidget(row, 1, seq_edit)
            self._seq_widgets[action_id] = seq_edit
            self._initial_values[action_id] = initial

        reset_btn = QPushButton("Restore Defaults")
        reset_btn.clicked.connect(self._reset)
        layout.addWidget(reset_btn)

    def apply(self) -> None:
        # Force pending key sequences to be committed.  QKeySequenceEdit only
        # finalises its value on a timeout (default ~1s) or when it loses
        # focus; clicking OK/Apply immediately after typing a shortcut may
        # otherwise leave the entry empty.
        focused = self.focusWidget()
        if isinstance(focused, QKeySequenceEdit):
            focused.clearFocus()

        for action_id, widget in self._seq_widgets.items():
            seq = widget.keySequence()
            new_value = seq.toString() if not seq.isEmpty() else ""
            if new_value == self._initial_values.get(action_id, ""):
                continue  # Untouched — don't rewrite "use default" into "".
            self._kb.set(action_id, new_value)
            self._initial_values[action_id] = new_value

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
            ("axis_label_color", "Axis label colour",   theme.axis_label_color),
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

        # Render throttles — surfaced here because they live on AppSettings
        # and ride the same Apply path as the rest of the page.  Defaults
        # (33 / 33 / 80) are fine on local X11; remote SSH sessions
        # benefit from raising them so we send fewer X11 round-trips per
        # second.
        from PySide6.QtWidgets import QFormLayout
        perf_grp = QGroupBox("Render throttles (ms)")
        perf_form = QFormLayout(perf_grp)
        perf_note = QLabel(
            "Higher values reduce GUI traffic at the cost of a more\n"
            "delayed update — useful over slow remote sessions.\n"
            "0 disables the throttle (no debouncing)."
        )
        perf_note.setStyleSheet("color: #888; font-size: 11px;")
        perf_note.setWordWrap(True)
        perf_form.addRow(perf_note)

        def _ms_spin(value: int) -> QSpinBox:
            sp = QSpinBox()
            sp.setRange(0, 2000)
            sp.setSingleStep(10)
            sp.setSuffix(" ms")
            sp.setValue(int(value))
            return sp

        self._cursor_ms = _ms_spin(settings.cursor_label_throttle_ms)
        self._cursor_ms.setToolTip(
            "Status-bar cursor coordinates.  Default 33 (≈30 fps).  "
            "Try 100–150 over SSH if stale positions trail the cursor."
        )
        perf_form.addRow("Cursor label", self._cursor_ms)

        self._rubber_ms = _ms_spin(settings.rubber_band_throttle_ms)
        self._rubber_ms.setToolTip(
            "Rubber-band selection rectangle.  Default 33 (≈30 fps)."
        )
        perf_form.addRow("Rubber-band", self._rubber_ms)

        self._zoom_ms = _ms_spin(settings.zoom_refresh_ms)
        self._zoom_ms.setToolTip(
            "Post-zoom refresh debounce.  Default 80.  Higher values let "
            "a longer wheel burst settle before the post-zoom refresh."
        )
        perf_form.addRow("Zoom refresh", self._zoom_ms)
        vbox.addWidget(perf_grp)

        # Rendering — anti-aliasing toggle.  Default on; turn off to claw
        # back paint time on very large boards or weak GPUs at the cost
        # of jagged pad / wire edges.
        render_grp = QGroupBox("Rendering")
        render_layout = QVBoxLayout(render_grp)
        self._antialiasing_cb = QCheckBox("Anti-aliasing (smooth edges)")
        self._antialiasing_cb.setChecked(settings.antialiasing)
        self._antialiasing_cb.setToolTip(
            "Smooth pad/wire/text edges.  Default on.  Turn off to reduce "
            "paint time on very large boards or weak GPUs."
        )
        render_layout.addWidget(self._antialiasing_cb)
        vbox.addWidget(render_grp)

        vbox.addStretch()

        self._default_projects_dir: Path | None = settings.default_projects_dir

    def apply(self) -> None:
        self._settings.default_projects_dir = self._default_projects_dir
        self._settings.component_library_dirs = self._lib_list.paths()
        self._settings.module_dirs = self._mod_list.paths()
        self._settings.auto_accept_placement = self._auto_accept_cb.isChecked()
        self._settings.cursor_label_throttle_ms = self._cursor_ms.value()
        self._settings.rubber_band_throttle_ms = self._rubber_ms.value()
        self._settings.zoom_refresh_ms = self._zoom_ms.value()
        self._settings.antialiasing = self._antialiasing_cb.isChecked()
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

    def __init__(
        self,
        session: object,
        app_settings: AppSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # Import here to avoid circular dependency with domain layer.
        from protoboard.domain.routing_settings import ROUTING_DEFAULTS, RoutingSettings
        from protoboard.gui.session import EditorSession

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
        self._wire_mode.addItems(["auto", "bare", "bare_diagonal", "insulated"])
        gen_form.addRow("Wire mode", self._wire_mode)

        self._allow_jumpers = QCheckBox()
        gen_form.addRow("Allow jumpers", self._allow_jumpers)

        self._push_and_shove = QCheckBox()
        gen_form.addRow("Push-and-shove", self._push_and_shove)

        self._enable_simplify = QCheckBox()
        gen_form.addRow("Simplify after routing", self._enable_simplify)

        from PySide6.QtWidgets import QComboBox as _QComboBox
        self._simplify_mode = _QComboBox()
        self._simplify_mode.addItem("Chebyshev (shortcut via diag)", "chebyshev")
        self._simplify_mode.addItem(
            "Bare-parity (respect anti-short rule)", "bare_parity"
        )
        gen_form.addRow("Simplify mode", self._simplify_mode)

        self._max_detour = _cost_spinbox(0.0, 20.0, 0.5, 1)
        self._max_detour.setSpecialValueText("unlimited")
        gen_form.addRow("Max detour factor", self._max_detour)

        gen_grp.setLayout(gen_form)
        vbox.addWidget(gen_grp)

        # --- Cost preset group ---
        from protoboard.services.routing.cost_presets import builtin_preset_names
        preset_grp = QGroupBox("Cost preset")
        preset_form = _RoutingFormLayout(preset_grp)

        from PySide6.QtWidgets import QComboBox as _PresetCombo
        self._cost_preset = _PresetCombo()
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

        from PySide6.QtWidgets import QPushButton as _PresetEditBtn
        edit_presets_btn = _PresetEditBtn("Edit presets…")
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

    def _build_overrides_group(self) -> "QGroupBox":
        """Dynamic spinbox grid for every user-tunable cost knob.

        Knobs are grouped by the prefix of their key (crossing_, bend_,
        diagonal_, at_pad_, etc.).  Each group becomes a labelled
        sub-section; spinboxes within a group use the standard
        sentinel-aware behaviour (-1 → ∞).  Changes are written to
        ``self._pending_overrides`` and merged into ``cost_overrides``
        when ``apply()`` runs.
        """
        from PySide6.QtWidgets import (
            QFormLayout, QGroupBox, QLabel, QScrollArea, QVBoxLayout, QWidget,
        )
        from protoboard.services.routing.cost_model import (
            merged_cost_key_docs, merged_user_tunable_cost_keys,
        )
        # Defensive imports — ensure built-in routers register themselves
        # before we ask for their supported / user-tunable cost keys.
        import protoboard.services.routing.builtin  # noqa: F401
        import protoboard.services.routing.builtin_enhanced  # noqa: F401
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
        rs = self._session.routing_settings  # type: ignore[attr-defined]
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
        font = hint.font(); font.setItalic(True); hint.setFont(font)
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
                spin.setToolTip(
                    COST_KEY_DOCS.get(key, "")
                    + "\n\nTip: enter -1 for ∞."
                )
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
            "crossing":     "Crossings",
            "at_pad":       "At-pad penalties",
            "bend":         "Bends",
            "diagonal":     "Diagonals",
            "foreign / via": "Foreign-pad / via avoidance",
            "other":        "Other",
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
        from PySide6.QtWidgets import QInputDialog, QMessageBox
        from protoboard.gui.app_settings import save_app_settings
        from protoboard.services.routing.cost_presets import builtin_preset_names

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
                self, "Save as preset", "Preset name:",
            )
            if not ok:
                return
            name = name.strip()
            if not name:
                return
            if name in builtin_preset_names():
                QMessageBox.warning(
                    self, "Reserved name",
                    f"{name!r} is a built-in preset name and cannot be used.",
                )
                continue
            if name in self._app_settings.cost_presets:
                if QMessageBox.question(
                    self, "Overwrite preset",
                    f"A user preset named {name!r} already exists.  Overwrite?",
                ) != QMessageBox.StandardButton.Yes:
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
        from protoboard.domain.routing_settings import RoutingSettings
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

    def _restore_defaults(self) -> None:
        self._load_from_settings(self._defaults)

    def apply(self) -> None:
        from protoboard.domain.routing_settings import RoutingSettings
        rs_old = self._session.routing_settings  # type: ignore[attr-defined]
        new_settings = RoutingSettings(
            preferred_side=self._preferred_side.currentText(),  # type: ignore[arg-type]
            allow_layer_change=self._allow_layer_change.isChecked(),
            max_rip_up_passes=self._max_rip_up.value(),
            max_optimize_iterations=self._max_optimize.value(),
            wire_mode=self._wire_mode.currentText(),  # type: ignore[arg-type]
            allow_jumpers=self._allow_jumpers.isChecked(),
            push_and_shove=self._push_and_shove.isChecked(),
            enable_simplify_pass=self._enable_simplify.isChecked(),
            simplify_mode=_resolve_simplify_mode(self._simplify_mode.currentData()),
            max_detour_factor=self._max_detour.value(),
            router_name=rs_old.router_name,
            skip_autoroute_dialog=rs_old.skip_autoroute_dialog,
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


def _resolve_simplify_mode(raw: object) -> object:
    """Translate the QComboBox data role back into a SimplifyMode enum."""
    from protoboard.services.routing.postprocess import SimplifyMode
    if isinstance(raw, str):
        try:
            return SimplifyMode(raw)
        except ValueError:
            return SimplifyMode.BARE_PARITY
    return SimplifyMode.BARE_PARITY


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

# ---------------------------------------------------------------------------
# Page — Board (axis labels)
# ---------------------------------------------------------------------------


class _BoardPage(QWidget):
    """Per-project board-display settings — column/row index labels."""

    # Display string ↔ AxisLabelFormat value
    _OPTIONS: list[tuple[str, str]] = [
        ("Disabled",      "disabled"),
        ("0, 1, 2, …",    "numeric_0"),
        ("1, 2, 3, …",    "numeric_1"),
        ("a, b, c, …",    "alphabetic"),
    ]

    def __init__(self, session: object, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from protoboard.gui.session import EditorSession

        self._session: EditorSession = session  # type: ignore[assignment]

        vbox = QVBoxLayout(self)
        vbox.setSpacing(10)
        vbox.setContentsMargins(8, 8, 8, 8)

        grp = QGroupBox("Axis index labels")
        form = _RoutingFormLayout(grp)

        from PySide6.QtWidgets import QComboBox

        self._columns = QComboBox()
        for label, data in self._OPTIONS:
            self._columns.addItem(label, data)
        form.addRow("Columns format", self._columns)

        self._rows = QComboBox()
        for label, data in self._OPTIONS:
            self._rows.addItem(label, data)
        form.addRow("Rows format", self._rows)

        grp.setLayout(form)
        vbox.addWidget(grp)

        help_label = QLabel(
            "Labels are drawn on all four sides of the board.  Visibility can\n"
            "be toggled separately from the Layers panel (\"Axis Labels\")."
        )
        help_label.setStyleSheet("color: #888;")
        vbox.addWidget(help_label)
        vbox.addStretch()

        self._load_from_settings()

    def _load_from_settings(self) -> None:
        s = self._session.axis_label_settings
        self._set_combo(self._columns, s.columns.value)
        self._set_combo(self._rows, s.rows.value)

    @staticmethod
    def _set_combo(combo: object, value: str) -> None:
        for i in range(combo.count()):  # type: ignore[attr-defined]
            if combo.itemData(i) == value:  # type: ignore[attr-defined]
                combo.setCurrentIndex(i)  # type: ignore[attr-defined]
                return

    def apply(self) -> None:
        from protoboard.domain.axis_labels_settings import (
            AxisLabelFormat,
            AxisLabelSettings,
        )
        new_settings = AxisLabelSettings(
            columns=AxisLabelFormat(self._columns.currentData()),
            rows=AxisLabelFormat(self._rows.currentData()),
        )
        if new_settings == self._session.axis_label_settings:
            return
        self._session.axis_label_settings = new_settings
        self._session.mark_dirty()


# ---------------------------------------------------------------------------
# PreferencesDialog
# ---------------------------------------------------------------------------


class PreferencesDialog(QDialog):
    """Application preferences — Shortcuts, Colours, Paths, Routing, Board."""

    def __init__(
        self,
        keybindings: KeyBindings,
        theme: Theme,
        app_settings: AppSettings,
        on_keybindings_applied: Callable[[], None] | None = None,
        on_theme_applied: Callable[[Theme], None] | None = None,
        on_paths_applied: Callable[[AppSettings], None] | None = None,
        on_axis_labels_applied: Callable[[], None] | None = None,
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
        self._on_axis_labels_applied = on_axis_labels_applied
        self._theme = theme

        outer = QHBoxLayout(self)

        # Left navigation list
        self._nav = QListWidget()
        self._nav.setFixedWidth(140)
        self._nav.setSpacing(2)
        for label in ("Shortcuts", "Colours", "Paths", "Routing", "Board"):
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
            _RoutingPage(session, app_settings, self) if session is not None else None
        )
        self._board_page: _BoardPage | None = (
            _BoardPage(session, self) if session is not None else None
        )
        self._stack.addWidget(self._shortcuts_page)
        self._stack.addWidget(self._colors_page)
        self._stack.addWidget(self._paths_page)
        if self._routing_page is not None:
            self._stack.addWidget(self._routing_page)
        else:
            placeholder = QLabel("Open a project to configure routing settings.")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._stack.addWidget(placeholder)
        if self._board_page is not None:
            self._stack.addWidget(self._board_page)
        else:
            placeholder_b = QLabel("Open a project to configure board settings.")
            placeholder_b.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._stack.addWidget(placeholder_b)
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
        elif idx == 4 and self._board_page is not None:
            self._board_page.apply()
            if self._on_axis_labels_applied:
                self._on_axis_labels_applied()

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
        if self._board_page is not None:
            self._board_page.apply()
            if self._on_axis_labels_applied:
                self._on_axis_labels_applied()
        self.accept()
