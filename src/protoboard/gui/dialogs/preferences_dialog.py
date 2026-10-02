# SPDX-License-Identifier: MIT
"""Preferences dialog — keyboard shortcuts, colours/alphas, library paths."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, cast

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QKeySequence
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from protoboard.controller.app_settings import AppSettings, save_app_settings
from protoboard.controller.view_options.theme import Theme
from protoboard.gui.dialogs.routing_preferences_page import (
    RoutingPreferencesPage as _RoutingPage,
)
from protoboard.gui.keybindings import DEFAULTS, DISPLAY_NAMES, KeyBindings
from protoboard.gui.keymap import AbstractKeyMap, FinalShortcutError, KeyMap
from protoboard.services.preferences.theme_yaml import save_theme

if TYPE_CHECKING:
    from protoboard.gui.session import EditorSession


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
        r, g, b, a = (
            self._color.red(),
            self._color.green(),
            self._color.blue(),
            self._color.alpha(),
        )
        self.setStyleSheet(
            f"background-color: rgba({r},{g},{b},{a}); border: 1px solid #666;"
        )

    def _pick(self) -> None:
        c = QColorDialog.getColor(
            self._color,
            self,
            "Choose colour",
            QColorDialog.ColorDialogOption.ShowAlphaChannel,
        )
        if c.isValid():
            self._color = c
            self._update_swatch()


# ---------------------------------------------------------------------------
# Page — Shortcuts
# ---------------------------------------------------------------------------


class _AppShortcutsTab(QWidget):
    """The legacy flat KeyBindings table (application/menu shortcuts)."""

    def __init__(self, kb: KeyBindings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._kb = kb
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self._table = QTableWidget(0, 2)
        self._table.setHorizontalHeaderLabels(["Action", "Shortcut"])
        header = self._table.horizontalHeader()
        # Action column auto-fits the longest label; Shortcut column
        # consumes the rest of the row width.  Without ResizeToContents
        # the default ~100px crops long action names like
        # "Component → Toggle Footprint Side".
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
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
            if new_value:
                self._steal(action_id, new_value)

    def _steal(self, winner: str, shortcut: str) -> None:
        """Unbind *shortcut* from every other action in this table.

        Two actions sharing one QShortcut would both be ambiguous (Qt beeps
        and fires neither), so a user assigning an already-taken shortcut
        means "move it here" — the previous owner is cleared, visibly.
        """
        for action_id, widget in self._seq_widgets.items():
            if action_id == winner:
                continue
            current = widget.keySequence()
            value = current.toString() if not current.isEmpty() else ""
            if value != shortcut:
                continue
            self._kb.set(action_id, "")
            widget.setKeySequence(QKeySequence())
            self._initial_values[action_id] = ""

    def _reset(self) -> None:
        for action_id, widget in self._seq_widgets.items():
            default = DEFAULTS.get(action_id, "")
            widget.setKeySequence(QKeySequence(default))


def _valid_trigger_text(text: str) -> bool:
    """Every ``|``-separated alternative parses as a key or mouse chord."""
    from protoboard.gui.keymap import is_mouse_trigger

    for alt in (a.strip() for a in text.split("|")):
        if not alt:
            continue
        if is_mouse_trigger(alt):
            continue
        # QKeySequence never rejects a string — garbage parses to a sequence
        # whose canonical text is empty.  Round-trip is the validity signal.
        if not QKeySequence(alt).toString():
            return False
    return True


def _normalize_trigger_text(text: str) -> str:
    return "|".join(a.strip() for a in text.split("|") if a.strip())


class _KeyMapTab(QWidget):
    """One KeyMap layer (global / view / one tool) as an editable table.

    Bindings are edited as text — ``"R"``, ``"Up|W"`` alternatives and mouse
    chords (``"WheelUp"``, ``"RMB"``, ``"Ctrl+LMB"``) all round-trip.  A
    ``final`` action's row is read-only.  Assigning a trigger already owned
    by another action *on the same map* steals it: the previous owner's row
    loses that alternative (state-gated overlaps that ship as defaults are
    untouched — only a user edit steals).
    """

    def __init__(
        self, keymap: object, action_ids: list[str], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        assert isinstance(keymap, KeyMap)
        self._km = keymap
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self._table = QTableWidget(0, 2)
        self._table.setHorizontalHeaderLabels(["Action", "Binding"])
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._table.verticalHeader().setVisible(False)
        layout.addWidget(self._table)

        hint = QLabel(
            "Text bindings: alternatives with “|” (e.g. Up|W); mouse chords "
            "WheelUp / WheelDown / LMB / RMB / MMB with Ctrl/Alt/Shift "
            "prefixes.  Empty = unbound."
        )
        hint.setWordWrap(True)
        font = hint.font()
        font.setItalic(True)
        hint.setFont(font)
        layout.addWidget(hint)

        self._edits: dict[str, QLineEdit] = {}
        self._initial: dict[str, str] = {}
        for action_id in action_ids:
            row = self._table.rowCount()
            self._table.insertRow(row)
            name_item = QTableWidgetItem(self._km.display_of(action_id))
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, 0, name_item)
            value = self._km.resolve(action_id)
            edit = QLineEdit(value)
            if self._km.is_final(action_id):
                edit.setEnabled(False)
                edit.setToolTip(
                    "Final shortcut — cannot be rebound.  A tool may add "
                    "extra triggers to this action, never remove these."
                )
            self._table.setCellWidget(row, 1, edit)
            self._edits[action_id] = edit
            self._initial[action_id] = value

    def apply(self) -> None:
        for action_id, edit in self._edits.items():
            if not edit.isEnabled():
                continue  # final — read-only
            text = _normalize_trigger_text(edit.text())
            if text == self._initial.get(action_id, ""):
                continue  # untouched
            if not _valid_trigger_text(text):
                QMessageBox.warning(
                    self,
                    "Invalid binding",
                    f"{text!r} is not a valid key or mouse chord.",
                )
                edit.setText(self._initial.get(action_id, ""))
                continue
            try:
                self._km.set_override(action_id, text)
            except FinalShortcutError as exc:
                QMessageBox.warning(self, "Reserved shortcut", str(exc))
                edit.setText(self._initial.get(action_id, ""))
                continue
            self._initial[action_id] = text
            if text:
                self._steal(action_id, text)

    def _steal(self, winner: str, text: str) -> None:
        """Remove the newly-assigned alternatives from every other action here."""
        wanted = {a for a in text.split("|") if a}
        for action_id, edit in self._edits.items():
            if action_id == winner or not edit.isEnabled():
                continue
            current = self._km.resolve(action_id)
            kept = [a for a in current.split("|") if a and a not in wanted]
            if len(kept) == len([a for a in current.split("|") if a]):
                continue  # no overlap
            remaining = "|".join(kept)
            try:
                self._km.set_override(action_id, remaining)
            except FinalShortcutError:  # pragma: no cover — final rows skipped
                continue
            edit.setText(remaining)
            self._initial[action_id] = remaining


class _ShortcutsPage(QWidget):
    """Shortcuts — sub-tabs: Application (legacy table), Global, View, tools."""

    def __init__(
        self,
        kb: KeyBindings,
        keymap: object | None = None,
        keymap_tools: tuple[object, ...] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        tabs = QTabWidget()
        layout.addWidget(tabs)

        self._app_tab = _AppShortcutsTab(kb)
        tabs.addTab(self._app_tab, "Application")

        self._keymap = None
        self._km_tabs: list[_KeyMapTab] = []
        if keymap is not None:
            from protoboard.gui.keymap import (
                GLOBAL_ACTIONS,
                VIEW_ACTIONS,
                KeyMap,
            )
            from protoboard.gui.shortcuts_help import category_label

            assert isinstance(keymap, KeyMap)
            self._keymap = keymap
            self._add_km_tab(tabs, keymap, [a.id for a in GLOBAL_ACTIONS], "Global")
            self._add_km_tab(tabs, keymap, [a.id for a in VIEW_ACTIONS], "View")
            for tool in keymap_tools:
                # A composed production tool (e.g. SelectTool = (_SelectTool,
                # ViewToolMixin)) is NOT a nominal ToolSurface subclass, so a
                # `isinstance(tool, ToolSurface)` assert here crashed when MainWindow
                # passed a real tool. Guard structurally on the one member we
                # use — its key-map layer builder — instead.
                make_key_map = getattr(tool, "make_key_map", None)
                if not callable(make_key_map):
                    continue
                layer = cast(AbstractKeyMap, make_key_map(keymap))
                ids = [a.id for a in layer.known_actions()]
                if not ids:
                    continue
                self._add_km_tab(tabs, layer, ids, category_label(ids[0]))

    def _add_km_tab(self, tabs: object, km: object, ids: list[str], label: str) -> None:
        assert isinstance(tabs, QTabWidget)
        tab = _KeyMapTab(km, ids)
        tabs.addTab(tab, label)
        self._km_tabs.append(tab)

    def apply(self) -> None:
        self._app_tab.apply()
        for tab in self._km_tabs:
            tab.apply()
        if self._keymap is not None and self._km_tabs:
            self._keymap.save_overrides()


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


def _alpha_row(
    label: str, initial: float, is_int: bool = False
) -> tuple[QHBoxLayout, QDoubleSpinBox | QSpinBox]:
    if is_int:
        sp: QSpinBox = QSpinBox()
        sp.setRange(0, 255)
        sp.setValue(int(initial))
        sp.setFixedWidth(80)
        return _row(label, sp), sp
    spf = QDoubleSpinBox()
    spf.setRange(0.0, 1.0)
    spf.setSingleStep(0.05)
    spf.setDecimals(2)
    spf.setValue(float(initial))
    spf.setFixedWidth(80)
    return _row(label, spf), spf


def _float_row(
    label: str,
    initial: float,
    lo: float,
    hi: float,
    step: float = 0.5,
    decimals: int = 1,
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

        self._widgets: dict[str, _ColorButton | QDoubleSpinBox | QSpinBox | QSlider] = (
            {}
        )

        self._add_board_and_wire_sections(vbox)
        self._add_component_sections(vbox)
        self._add_group_and_selection_sections(vbox)
        self._add_tool_feedback_sections(vbox)
        self._add_body_sections(vbox)

        reset_btn = QPushButton("Restore Defaults")
        reset_btn.clicked.connect(self._reset)
        vbox.addWidget(reset_btn)
        vbox.addStretch()

    def _add_section(
        self,
        vbox: QVBoxLayout,
        title: str,
        rows: list[
            tuple[str, str, object] | tuple[str, str, object, tuple[float, float]]
        ],
    ) -> None:
        """rows: (field_name, label, initial[, (lo, hi)]) — 4th element enables free range."""  # noqa: E501
        grp = QGroupBox(title)
        gl = QVBoxLayout(grp)
        for entry in rows:
            field_name, label, init = entry[0], entry[1], entry[2]
            range_hint: tuple[float, float] | None = (
                entry[3] if len(entry) > 3 else None
            )
            control: _ColorButton | QDoubleSpinBox | QSpinBox
            if isinstance(init, str):
                row_layout, color_button = _color_row(label, init)
                control = color_button
            elif isinstance(init, int):
                row_layout, alpha_spin = _alpha_row(label, init, is_int=True)
                control = alpha_spin
            elif range_hint is not None:
                row_layout, float_spin = _float_row(label, float(init), range_hint[0], range_hint[1])  # type: ignore[arg-type]  # noqa: E501
                control = float_spin
            else:
                row_layout, alpha_spin = _alpha_row(label, float(init))  # type: ignore[arg-type]
                control = alpha_spin
            gl.addLayout(row_layout)
            self._widgets[field_name] = control
        vbox.addWidget(grp)

    def _add_opacity_section(
        self, vbox: QVBoxLayout, title: str, rows: list[tuple[str, str, float]]
    ) -> None:
        """rows: (field_name, label, initial_float_0_1) — rendered as sliders."""
        grp = QGroupBox(title)
        gl = QVBoxLayout(grp)
        for field_name, label, init in rows:
            row_layout, slider = _opacity_row(label, init)
            gl.addLayout(row_layout)
            self._widgets[field_name] = slider
        vbox.addWidget(grp)

    def _add_board_and_wire_sections(self, vbox: QVBoxLayout) -> None:
        theme = self._theme
        self._add_section(
            vbox,
            "Board & Pads",
            [
                ("board_bg", "Board background", theme.board_bg),
                ("pad_color", "Pad colour", theme.pad_color),
                ("strip_color", "Strip colour", theme.strip_color),
                ("hole_color", "Hole colour", theme.hole_color),
                ("isolated_x_color", "Strip cut X colour", theme.isolated_x_color),
                ("axis_label_color", "Axis label colour", theme.axis_label_color),
            ],
        )

        self._add_section(
            vbox,
            "Wires",
            [
                (
                    "bare_outline_color",
                    "Bare wire outline colour",
                    theme.bare_outline_color,
                ),
                ("bare_width", "Bare wire fill width", theme.bare_width),
                (
                    "bare_outline_width",
                    "Bare wire outline width",
                    theme.bare_outline_width,
                ),
                (
                    "ins_outline_color",
                    "Insulated wire outline colour",
                    theme.ins_outline_color,
                ),
                ("ins_width", "Insulated wire fill width", theme.ins_width),
                (
                    "ins_outline_width",
                    "Insulated wire outline width",
                    theme.ins_outline_width,
                ),
                ("ratsnest_color", "Ratsnest colour", theme.ratsnest_color),
                (
                    "ratsnest_islands_color",
                    "Ratsnest colour (multi-island warning)",
                    theme.ratsnest_islands_color,
                ),
            ],
        )

        # net_colors palette (8 entries)
        net_grp = QGroupBox("Net colour palette")
        net_gl = QVBoxLayout(net_grp)
        self._net_color_btns: list[_ColorButton] = []
        for i, c in enumerate(theme.net_colors):
            row_layout, btn = _color_row(f"Net colour {i + 1}", c)
            net_gl.addLayout(row_layout)
            self._net_color_btns.append(btn)
        vbox.addWidget(net_grp)

    def _add_component_sections(self, vbox: QVBoxLayout) -> None:
        theme = self._theme
        self._add_section(
            vbox,
            "Components",
            [
                ("comp_fill_color", "Component fill colour", theme.comp_fill_color),
                ("comp_border", "Component border colour", theme.comp_border),
                ("refdes_color", "Label text colour", theme.refdes_color),
                ("label_bg_color", "Label background colour", theme.label_bg_color),
                (
                    "overlay_font_size",
                    "Label font size (pt)",
                    theme.overlay_font_size,
                    (2.0, 32.0),
                ),
                ("lead_color", "Component lead colour", theme.lead_color),
            ],
        )

        self._add_opacity_section(
            vbox,
            "Layer opacity",
            [
                ("back_alpha", "Back-side opacity", theme.back_alpha),
                ("board_ghost_alpha", "Board ghost opacity", theme.board_ghost_alpha),
                ("ghost_comp_alpha", "Component ghost opacity", theme.ghost_comp_alpha),
            ],
        )

        self._add_opacity_section(
            vbox,
            "Net / group highlight dim",
            [
                (
                    "highlight_dim_components",
                    "Dimmed components opacity",
                    theme.highlight_dim_components,
                ),
                (
                    "highlight_dim_wires",
                    "Dimmed wires opacity",
                    theme.highlight_dim_wires,
                ),
            ],
        )

    def _add_group_and_selection_sections(self, vbox: QVBoxLayout) -> None:
        theme = self._theme
        self._add_section(
            vbox,
            "Groups",
            [
                ("group_pen_color", "Group outline colour", theme.group_pen_color),
                ("group_pen_width", "Group outline width", theme.group_pen_width),
                ("group_fill_color", "Group fill colour", theme.group_fill_color),
                ("group_name_color", "Group label colour", theme.group_name_color),
            ],
        )

        self._add_section(
            vbox,
            "Selection",
            [
                ("select_color", "Selection outline colour", theme.select_color),
                ("select_width", "Selection outline width", theme.select_width),
                (
                    "multi_select_color",
                    "Multi-selection outline colour",
                    theme.multi_select_color,
                ),
                (
                    "multi_select_width",
                    "Multi-selection outline width",
                    theme.multi_select_width,
                ),
            ],
        )

        self._add_section(
            vbox,
            "Active group",
            [
                (
                    "group_active_color",
                    "Active group outline colour",
                    theme.group_active_color,
                ),
                (
                    "group_active_fill_color",
                    "Active group fill colour",
                    theme.group_active_fill_color,
                ),
            ],
        )

    def _add_tool_feedback_sections(self, vbox: QVBoxLayout) -> None:
        theme = self._theme
        self._add_section(
            vbox,
            "Tool feedback",
            [
                (
                    "placement_reach_color",
                    "Reachable pad colour",
                    theme.placement_reach_color,
                ),
                (
                    "placement_reach_line_color",
                    "Reach line colour",
                    theme.placement_reach_line_color,
                ),
                (
                    "routing_preview_color",
                    "Route preview colour",
                    theme.routing_preview_color,
                ),
            ],
        )

        self._add_section(
            vbox,
            "Component pins & markers",
            [
                (
                    "pin_width_complete",
                    "Pin width (with net)",
                    theme.pin_width_complete,
                ),
                (
                    "pin_width_incomplete",
                    "Pin width (no net)",
                    theme.pin_width_incomplete,
                ),
                (
                    "pin_label_fallback_color",
                    "Pin label fallback colour",
                    theme.pin_label_fallback_color,
                ),
                ("pin1_marker_color", "Pin 1 marker colour", theme.pin1_marker_color),
                (
                    "component_origin_cross_color",
                    "Origin cross colour",
                    theme.component_origin_cross_color,
                ),
                (
                    "component_origin_cross_width",
                    "Origin cross width",
                    theme.component_origin_cross_width,
                ),
            ],
        )

    def _add_body_sections(self, vbox: QVBoxLayout) -> None:
        theme = self._theme
        self._add_section(
            vbox,
            "Component body",
            [
                (
                    "body_led_stripe_color",
                    "LED stripe colour",
                    theme.body_led_stripe_color,
                ),
                (
                    "body_diode_cathode_color",
                    "Diode cathode colour",
                    theme.body_diode_cathode_color,
                ),
                (
                    "body_diode_anode_color",
                    "Diode anode colour",
                    theme.body_diode_anode_color,
                ),
                ("exported_pin_color", "Exported pin colour", theme.exported_pin_color),
                (
                    "exported_pin_pen_color",
                    "Exported pin pen colour",
                    theme.exported_pin_pen_color,
                ),
                (
                    "exported_pin_pen_width",
                    "Exported pin pen width",
                    theme.exported_pin_pen_width,
                ),
            ],
        )

        self._add_opacity_section(
            vbox,
            "Component body opacity",
            [
                ("body_tab_opacity", "Tab opacity", theme.body_tab_opacity),
                ("body_slot_opacity", "Slot opacity", theme.body_slot_opacity),
            ],
        )

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
            str(settings.default_projects_dir)
            if settings.default_projects_dir
            else "(not set)"
        )
        self._proj_edit.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
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
        self._selection_bbox_cb = QCheckBox("Selection bounding box (dashed)")
        self._selection_bbox_cb.setChecked(settings.show_selection_bbox)
        self._selection_bbox_cb.setToolTip(
            "Draw a thin dashed bounding box around each selected component, "
            "in addition to the bright body outline.  Default on."
        )
        render_layout.addWidget(self._selection_bbox_cb)
        vbox.addWidget(render_grp)

        # Connections layer (ratsnest) — line-mode selector.  Full draws
        # every pair within an unrouted net (K_n graph); Closest draws
        # only each pin → nearest other pin.  Multi-island nets always
        # use the "ratsnest_islands_color" warning colour from the theme
        # regardless of this setting.
        conn_grp = QGroupBox("Connections layer (ratsnest)")
        conn_layout = QVBoxLayout(conn_grp)
        self._ratsnest_mode_group = QButtonGroup(conn_grp)
        self._ratsnest_full_rb = QRadioButton(
            "Full — all pin pairs in a net (K_n graph)"
        )
        self._ratsnest_full_rb.setToolTip(
            "Draw a line between every pair of pins in a net.  Maximum "
            "information; can look busy on dense designs."
        )
        self._ratsnest_closest_rb = QRadioButton(
            "Closest — each pin to its nearest neighbour"
        )
        self._ratsnest_closest_rb.setToolTip(
            "Draw one line per pin to its closest other pin in the same "
            "net.  Less clutter; doesn't show every missing pair."
        )
        self._ratsnest_mode_group.addButton(self._ratsnest_full_rb)
        self._ratsnest_mode_group.addButton(self._ratsnest_closest_rb)
        if settings.ratsnest_mode == "closest":
            self._ratsnest_closest_rb.setChecked(True)
        else:
            self._ratsnest_full_rb.setChecked(True)
        conn_layout.addWidget(self._ratsnest_full_rb)
        conn_layout.addWidget(self._ratsnest_closest_rb)
        vbox.addWidget(conn_grp)

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
        self._settings.show_selection_bbox = self._selection_bbox_cb.isChecked()
        self._settings.ratsnest_mode = (
            "closest" if self._ratsnest_closest_rb.isChecked() else "full"
        )
        save_app_settings(self._settings)

    def _pick_projects_dir(self) -> None:
        start = (
            str(self._default_projects_dir)
            if self._default_projects_dir
            else str(Path.home())
        )
        folder = QFileDialog.getExistingDirectory(
            self, "Choose default projects directory", start
        )
        if folder:
            self._default_projects_dir = Path(folder)
            self._proj_edit.setText(folder)

    def _clear_projects_dir(self) -> None:
        self._default_projects_dir = None
        self._proj_edit.setText("(not set)")


# ---------------------------------------------------------------------------
# Helper — Board form layout
# ---------------------------------------------------------------------------


def _RoutingFormLayout(parent: QWidget) -> QFormLayout:
    form = QFormLayout()
    form.setSpacing(6)
    form.setContentsMargins(8, 8, 8, 8)
    return form


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
        ("Disabled", "disabled"),
        ("0, 1, 2, …", "numeric_0"),
        ("1, 2, 3, …", "numeric_1"),
        ("a, b, c, …", "alphabetic"),
    ]

    def __init__(self, session: object, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._session: EditorSession = session  # type: ignore[assignment]

        vbox = QVBoxLayout(self)
        vbox.setSpacing(10)
        vbox.setContentsMargins(8, 8, 8, 8)

        grp = QGroupBox("Axis index labels")
        form = _RoutingFormLayout(grp)

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
            'be toggled separately from the Layers panel ("Axis Labels").'
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


class _DragPage(QWidget):
    """Preferences page for drag-time soft-conflict resolution.

    Three combo-boxes (one per category) + lock-internal-wires default.
    Shares the parent dialog's AppSettings instance so Apply / OK paths
    in PreferencesDialog persist via save_app_settings(self._settings).
    """

    # (value, label) pairs.  Values MUST stay in sync with
    # ``protoboard.controller.editing.drag_preferences.DRAG_CONFLICT_VALUES`` — the
    # assertion below catches drift.  Labels are UI-facing only.
    _CONFLICT_CHOICES: list[tuple[str, str]] = [
        ("auto-remove", "Auto-remove (silent)"),
        ("ask-remove", "Ask before removing"),
        ("auto-reroute", "Auto-reroute affected nets"),
        ("ask-reroute", "Ask: reroute / drop / cancel"),
        ("block", "Block drag (HARD)"),
    ]

    def __init__(
        self,
        settings: AppSettings,
        parent: QWidget | None = None,
    ) -> None:
        from protoboard.controller.editing.drag_preferences import DRAG_CONFLICT_VALUES

        assert {
            v for v, _l in self._CONFLICT_CHOICES
        } == DRAG_CONFLICT_VALUES, (
            "DragPage choices drifted from DRAG_CONFLICT_VALUES — keep them in sync"
        )

        super().__init__(parent)
        self._settings = settings
        vbox = QVBoxLayout(self)
        vbox.setSpacing(12)

        grp = QGroupBox("Soft conflict resolution")
        form = QFormLayout(grp)
        note = QLabel(
            "How the commit pipeline reacts when a drag would touch wires.\n"
            "Defaults preserve existing wires unless the user opts in."
        )
        note.setStyleSheet("color: #888; font-size: 11px;")
        note.setWordWrap(True)
        form.addRow(note)

        self._free_combo = self._make_combo(settings.drag_free_wire_conflict)
        self._free_combo.setToolTip(
            "Free wires (not owned by any group) crossed by a moved pin.",
        )
        form.addRow("Free wire on path", self._free_combo)

        self._sgroup_combo = self._make_combo(
            settings.drag_stationary_group_wire_conflict,
        )
        self._sgroup_combo.setToolTip(
            "Wires owned by a stationary (non-dragged) group.\n"
            "Modifying them crosses group boundaries — confirm by default.",
        )
        form.addRow("Stationary group wire", self._sgroup_combo)

        self._disconnect_combo = self._make_combo(
            settings.drag_cross_group_disconnect,
        )
        self._disconnect_combo.setToolTip(
            "Wires between a drag-set member and the rest of the board.\n"
            "Drag would disconnect them; default lets the user choose.",
        )
        form.addRow("Cross-group disconnect", self._disconnect_combo)

        vbox.addWidget(grp)

        grp2 = QGroupBox("Group defaults")
        v2 = QVBoxLayout(grp2)
        self._lock_default_cb = QCheckBox(
            "Lock internal wires by default for new groups",
        )
        self._lock_default_cb.setChecked(settings.drag_default_lock_internal_wires)
        self._lock_default_cb.setToolTip(
            "When set, new groups treat their internal wires as HARD\n"
            "obstacles to foreign drags (no auto-remove).  Existing\n"
            "groups are unaffected — toggle per-group in Properties.",
        )
        v2.addWidget(self._lock_default_cb)
        vbox.addWidget(grp2)

        vbox.addStretch()

    def _make_combo(self, current: str) -> QComboBox:
        combo = QComboBox()
        for value, label in self._CONFLICT_CHOICES:
            combo.addItem(label, value)
        # Set selection to current preference.
        for i in range(combo.count()):
            if combo.itemData(i) == current:
                combo.setCurrentIndex(i)
                break
        return combo

    def apply(self) -> None:
        self._settings.drag_free_wire_conflict = (
            self._free_combo.currentData() or "auto-remove"
        )
        self._settings.drag_stationary_group_wire_conflict = (
            self._sgroup_combo.currentData() or "ask-remove"
        )
        self._settings.drag_cross_group_disconnect = (
            self._disconnect_combo.currentData() or "ask-reroute"
        )
        self._settings.drag_default_lock_internal_wires = (
            self._lock_default_cb.isChecked()
        )
        save_app_settings(self._settings)


class PreferencesDialog(QDialog):
    """Application preferences — Shortcuts, Colours, Paths, Routing, Drag, Board."""

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
        keymap: object | None = None,
        keymap_tools: tuple[object, ...] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.setMinimumSize(680, 520)
        # Top-level window with full WM decorations (title bar, minimise,
        # maximise, close).  ``Qt.Window`` decouples the dialog from the
        # parent's geometry so the user can freely move it around the
        # desktop — without it some window managers (Wayland/GNOME)
        # anchor the dialog to the main window.  The context-help "?"
        # button is dropped because we don't wire help into preferences.
        self.setWindowFlags(
            (self.windowFlags() | Qt.WindowType.Window)
            & ~Qt.WindowType.WindowContextHelpButtonHint
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
        for label in ("Shortcuts", "Colours", "Paths", "Routing", "Drag", "Board"):
            item = QListWidgetItem(label)
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
            self._nav.addItem(item)
        outer.addWidget(self._nav)

        # Right stacked pages
        self._stack = QStackedWidget()
        self._shortcuts_page = _ShortcutsPage(
            keybindings, keymap=keymap, keymap_tools=keymap_tools
        )
        self._colors_page = _ColorsPage(theme)
        self._paths_page = _PathsPage(app_settings)
        self._routing_page: _RoutingPage | None = (
            _RoutingPage(session, app_settings, self) if session is not None else None
        )
        self._drag_page = _DragPage(app_settings, self)
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
        self._stack.addWidget(self._drag_page)
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
        btn_box.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(
            self._apply_current
        )
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
        elif idx == 4:
            self._drag_page.apply()
        elif idx == 5 and self._board_page is not None:
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
        self._drag_page.apply()
        if self._board_page is not None:
            self._board_page.apply()
            if self._on_axis_labels_applied:
                self._on_axis_labels_applied()
        self.accept()
