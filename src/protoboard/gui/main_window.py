# SPDX-License-Identifier: MIT
"""MainWindow — top-level application window."""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path

import structlog

from protoboard.gui.app_settings import AppSettings
from protoboard.gui.window_state import load_window_state, save_window_state

from PySide6.QtCore import Qt, QTimer, QFileSystemWatcher, Signal
from PySide6.QtGui import QAction, QCloseEvent, QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QDockWidget,
    QFileDialog,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMenuBar,
    QMessageBox,
    QStatusBar,
    QTabWidget,
    QToolBar,
)

from protoboard.geometry import Point
from protoboard.gui import recent_projects
from protoboard.gui.board_view import BoardView
from protoboard.gui import keybindings as KB
from protoboard.gui.panels.components_panel import ComponentsPanel
from protoboard.gui.panels.groups_panel import GroupsPanel
from protoboard.gui.panels.layers_panel import LayersPanel
from protoboard.gui.panels.library_panel import LibraryPanel
from protoboard.gui.panels.netlist_panel import NetlistPanel
from protoboard.gui.panels.nets_editor_panel import NetsEditorPanel
from protoboard.gui.panels.performance_panel import PerformancePanel
from protoboard.gui.panels.properties_panel import PropertiesPanel
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.cut_tool import CutTool
from protoboard.gui.tools.place_tool import PlaceTool
from protoboard.gui.tools.route_tool import RouteTool
from protoboard.gui.tools.select_tool import SelectTool
from protoboard.gui.tools.simplify_tool import SimplifyTool
from protoboard.services.export.bom import export_bom_csv
from protoboard.services.export.pdf import export_pdf
from protoboard.services.export.png import export_png
from protoboard.services.export.svg import export_svg
from protoboard.services.kicad.exporter import KiCadNetlistExporter
from protoboard.services.kicad.importer import ImportWarning, KiCadNetlistImporter
from protoboard.services.placement.auto_placer import auto_place
from protoboard.services.placement.spreader import spread_components
from protoboard.services.persistence import git_repo
from protoboard.services.persistence.project_yaml import (
    load_project,
    pop_last_normalize_report,
    save_project,
)

_log = structlog.get_logger(__name__)

import os as _os
import time as _time
_click_log = structlog.get_logger("protoboard.gui.main_window.click")
_CLICK_PROFILE = _os.environ.get("PROTOBOARD_CLICK_PROFILE") not in (None, "", "0")


class MainWindow(QMainWindow):
    # Emitted when user chooses File > Close Project (return to welcome screen).
    return_to_welcome = Signal()

    # ------------------------------------------------------------------
    # Action metadata — declarative table consumed by _register_actions().
    # Each entry holds a menu label (with mnemonics) and optional flags.
    # Actions not listed here fall back to KB.DISPLAY_NAMES for their label
    # and use neutral defaults (enabled, not checkable).
    # ------------------------------------------------------------------
    _ACTION_METADATA: dict[str, dict[str, object]] = {
        # File menu
        KB.ACTION_SAVE:    {"label": "&Save"},
        KB.ACTION_SAVE_AS: {"label": "Save &As…"},
        KB.ACTION_QUIT:    {"label": "&Quit"},
        # Edit menu
        KB.ACTION_UNDO:    {"label": "&Undo", "enabled": False},
        KB.ACTION_REDO:    {"label": "&Redo", "enabled": False},
        KB.ACTION_DELETE:  {"label": "&Delete"},
        KB.ACTION_CLEAR_HIGHLIGHT: {"label": "Clear &Highlight", "enabled": False},
        KB.ACTION_ROTATE_CW:  {"label": "Rotate &CW"},
        KB.ACTION_ROTATE_CCW: {"label": "Rotate CC&W"},
        KB.ACTION_FLIP:       {"label": "&Flip"},
        KB.ACTION_GROUP:      {"label": "&Group Selection"},
        KB.ACTION_UNGROUP:    {"label": "&Ungroup"},
        KB.ACTION_EXIT_GROUP: {"label": "&Exit Group"},
        KB.ACTION_PREFERENCES: {"label": "&Preferences…"},
        # Select menu
        KB.ACTION_SELECT_ALL:               {"label": "Select &All"},
        KB.ACTION_DESELECT_ALL:             {"label": "&Deselect All"},
        KB.ACTION_SELECT_ALL_COMPONENTS:    {"label": "Select All &Components"},
        KB.ACTION_DESELECT_ALL_COMPONENTS:  {"label": "Deselect All C&omponents"},
        KB.ACTION_SELECT_ALL_WIRES:         {"label": "Select All &Wires"},
        KB.ACTION_DESELECT_ALL_WIRES:       {"label": "Deselect All Wi&res"},
        # View menu
        KB.ACTION_CAMERA_TOGGLE: {"label": "Camera: To&ggle"},
        KB.ACTION_MIRROR_H:      {"label": "Mirror &Horizontal", "checkable": True},
        KB.ACTION_MIRROR_V:      {"label": "Mirror &Vertical",   "checkable": True},
        KB.ACTION_FULLSCREEN:    {"label": "&Full Screen"},
        KB.ACTION_ZOOM_IN:       {"label": "Zoom &In"},
        KB.ACTION_ZOOM_OUT:      {"label": "Zoom &Out"},
        KB.ACTION_FIT_BOARD:     {"label": "&Fit Board"},
        KB.ACTION_FIT_AREA:      {"label": "Fit &Area"},
        KB.ACTION_FIT_SELECTION: {"label": "Fit &Selection"},
        # Tools menu
        KB.ACTION_TOOL_SELECT:          {"label": "&Select"},
        KB.ACTION_TOOL_ROUTE:           {"label": "Route &Wire"},
        KB.ACTION_TOOL_ROUTE_NET_AWARE: {"label": "Route Wire [Net-aware]"},
        KB.ACTION_TOOL_CUT:             {"label": "&Cut Strip"},
        KB.ACTION_TOOL_SIMPLIFY:        {"label": "Simplify &Wire Tool"},
        KB.ACTION_WIRE_TOGGLE_INSULATED: {"label": "&Insulated"},
        KB.ACTION_WIRE_SIDE_TOP:         {"label": "Side: &Top"},
        KB.ACTION_WIRE_SIDE_BOTTOM:      {"label": "Side: &Bottom"},
        KB.ACTION_REDRAG_PINS:           {"label": "Re-drag &Pins"},
        KB.ACTION_WIRE_SET_INSULATED:    {"label": "Wire: Set &Insulated"},
        KB.ACTION_WIRE_SET_BARE:         {"label": "Wire: Set &Bare"},
        KB.ACTION_WIRE_MOVE_TO_TOP:      {"label": "Wire: Move to &Top Layer"},
        KB.ACTION_WIRE_MOVE_TO_BOTTOM:   {"label": "Wire: Move to &Bottom Layer"},
        KB.ACTION_WIRE_FLIP:             {"label": "Wire: &Flip to Opposite Layer"},
        KB.ACTION_DISCONNECT_PIN:        {"label": "&Disconnect Pin from Net…"},
        KB.ACTION_AUTOROUTE:             {"label": "&Autoroute Nets… [Inspector selection or all]"},
        KB.ACTION_RUN_DRC:               {"label": "Run &DRC"},
        # Layer cycling
        KB.ACTION_LAYER_1: {"label": "Board"},
        KB.ACTION_LAYER_2: {"label": "Components Top"},
        KB.ACTION_LAYER_3: {"label": "Components Bottom"},
        KB.ACTION_LAYER_4: {"label": "Wires Top"},
        KB.ACTION_LAYER_5: {"label": "Wires Bottom"},
        KB.ACTION_LAYER_6: {"label": "Labels"},
        KB.ACTION_LAYER_7: {"label": "Pin Names"},
        KB.ACTION_LAYER_8: {"label": "Connections"},
        # Orphan actions (no menu entry — only keyboard shortcut)
        KB.ACTION_FILTER_TOGGLE_COMPONENTS: {"label": "Filter: Toggle Components"},
        KB.ACTION_FILTER_TOGGLE_WIRES:      {"label": "Filter: Toggle Wires"},
        KB.ACTION_FILTER_TOGGLE_GROUPS:     {"label": "Filter: Toggle Groups"},
        KB.ACTION_HIGHLIGHT_CYCLE:          {"label": "Cycle Relation Class"},
        KB.ACTION_RELATION_CYCLE_BACK:      {"label": "Cycle Relation Class (Back)"},
        KB.ACTION_RELATION_NONE:            {"label": "Relation: None"},
        KB.ACTION_RELATION_LOGICAL_NET:     {"label": "Relation: Same Logical Net"},
        KB.ACTION_RELATION_PHYSICAL_NET:    {"label": "Relation: Same Physical Net"},
        KB.ACTION_RELATION_UNCONNECTED:     {"label": "Relation: Unconnected Pins"},
        KB.ACTION_RELATION_NEIGHBORS:       {"label": "Relation: Neighbors"},
        KB.ACTION_SIMPLIFY_WIRE:            {"label": "Simplify Selected Wires"},
        KB.ACTION_UNWEAVE_WIRES:            {"label": "Unweave Selected Wires"},
        KB.ACTION_HIGHLIGHT_TOGGLE:         {"label": "Highlight Toggle"},
    }

    def __init__(
        self,
        session: EditorSession,
        app_settings: AppSettings | None = None,
    ) -> None:
        super().__init__()
        self._session = session
        self._app_settings = app_settings if app_settings is not None else AppSettings()
        self._closing_to_welcome = False
        self._keybindings = KB.KeyBindings.load()
        self._watched_net_path: Path | None = None
        self._kicad_watcher = QFileSystemWatcher(self)
        self._kicad_watcher.fileChanged.connect(self._on_kicad_net_changed)
        from protoboard.gui.theme import load_theme, Theme as _Theme
        self._theme: _Theme = load_theme()
        self._update_title()
        self.resize(1280, 800)

        self._pre_fullscreen_maximized: bool = False

        # Central widget — board view.
        self._board_view = BoardView(session, self)
        self._board_view.set_theme(self._theme)
        self._board_view.setMinimumSize(200, 200)
        self.setCentralWidget(self._board_view)
        # Single persistent SelectTool instance — preserves group navigation state.
        self._select_tool = SelectTool()
        self._board_view._permanent_tools.append(self._select_tool)
        self._apply_selection_settings_to_tool(self._select_tool)

        # Left dock — tabbed: Library | Netlist/BOM | Groups | Nets | Components
        self._library_panel = LibraryPanel(session.library, self)
        self._netlist_panel = NetlistPanel(self)
        self._groups_panel = GroupsPanel(self)
        self._nets_editor_panel = NetsEditorPanel(self)
        self._components_panel = ComponentsPanel(self)
        from protoboard.gui.panels.net_inspector_panel import NetInspectorPanel
        self._net_inspector_panel = NetInspectorPanel(self._board_view, self)
        left_tabs = QTabWidget()
        left_tabs.addTab(self._library_panel, "Library")
        left_tabs.addTab(self._netlist_panel, "Netlist")
        left_tabs.addTab(self._groups_panel, "Groups")
        left_tabs.addTab(self._nets_editor_panel, "Nets")
        left_tabs.addTab(self._components_panel, "Components")
        left_tabs.addTab(self._net_inspector_panel, "Inspector")
        left_dock = QDockWidget("", self)
        left_dock.setObjectName("left_dock")
        left_dock.setWidget(left_tabs)
        left_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        left_dock.setMinimumWidth(150)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, left_dock)

        # Properties + DRC dock (right).
        self._props_panel = PropertiesPanel(self)
        props_dock = QDockWidget("Properties / DRC", self)
        props_dock.setObjectName("props_dock")
        props_dock.setWidget(self._props_panel)
        props_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        props_dock.setMinimumWidth(150)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, props_dock)

        # Menu bar — File menu.
        # Build the action registry first so _build_menu() can pull QActions
        # by id from self._actions.
        self._register_actions()
        self._build_menu()

        # Wire routing state (persisted across tool switches).
        self._wire_insulated: bool = True
        self._wire_side: str = "top"

        # Per-layer manual visibility overrides — restored from session if available.
        self._layer_overrides = session.layer_overrides
        self._board_view._layer_overrides = self._layer_overrides

        # Layers dock (right, tabified with Properties).
        self._layers_panel = LayersPanel(self._layer_overrides, self)
        layers_dock = QDockWidget("Layers", self)
        layers_dock.setObjectName("layers_dock")
        layers_dock.setWidget(self._layers_panel)
        layers_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, layers_dock)
        self.tabifyDockWidget(props_dock, layers_dock)

        # Performance dock (right, tabified).
        self._perf_panel = PerformancePanel(session, self)
        perf_dock = QDockWidget("Performance", self)
        perf_dock.setObjectName("perf_dock")
        perf_dock.setWidget(self._perf_panel)
        perf_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, perf_dock)
        self.tabifyDockWidget(layers_dock, perf_dock)

        # ── Toolbar: Tools ──────────────────────────────────────────────
        tb_tools = QToolBar("Tools", self)
        tb_tools.setMovable(False)
        tb_tools.setObjectName("tb_tools")
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb_tools)

        # Reuse the QActions registered in _register_actions() so the
        # toolbar buttons share one identity with menu entries and global
        # WindowShortcut handlers.  Creating fresh toolbar QActions
        # (the old code) led to handler duplication and at least one
        # Qt platform (Wayland/X11 mix) refusing to fire the shortcut.
        tb_tools.addAction(self._actions[KB.ACTION_TOOL_SELECT])
        tb_tools.addAction(self._actions[KB.ACTION_TOOL_ROUTE])
        tb_tools.addAction(self._actions[KB.ACTION_TOOL_CUT])
        tb_tools.addAction(self._actions[KB.ACTION_TOOL_SIMPLIFY])

        # ── Toolbar: Selection (filter + highlight relation) ─────────────
        tb_selection = QToolBar("Selection", self)
        tb_selection.setMovable(True)
        tb_selection.setObjectName("tb_selection")
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb_selection)

        from protoboard.gui.panels.selection_filter_widget import SelectionFilterWidget
        from protoboard.gui.panels.highlight_mode_widget import HighlightModeWidget
        self._sel_filter_widget = SelectionFilterWidget(self)
        self._sel_filter_widget.set_tool(self._select_tool)
        tb_selection.addWidget(self._sel_filter_widget)
        tb_selection.addSeparator()
        self._hl_mode_widget = HighlightModeWidget(self)
        self._hl_mode_widget.set_tool(self._select_tool)
        tb_selection.addWidget(self._hl_mode_widget)

        # ── Toolbar: View ────────────────────────────────────────────────
        tb_view = QToolBar("View", self)
        tb_view.setMovable(False)
        tb_view.setObjectName("tb_view")
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb_view)

        self._act_camera_tb = tb_view.addAction("⊙ Bottom [X]")
        self._act_camera_tb.setCheckable(True)
        self._act_mirror_tb = tb_view.addAction("⟺H [H]")
        self._act_mirror_tb.setCheckable(True)
        self._act_mirror_v_tb = tb_view.addAction("⟺V [V]")
        self._act_mirror_v_tb.setCheckable(True)
        self._act_camera_tb.triggered.connect(lambda: self._set_camera("toggle"))
        self._act_mirror_tb.triggered.connect(self._mirror_view)
        self._act_mirror_v_tb.triggered.connect(self._mirror_view_v)

        # ── Toolbar: Zoom ────────────────────────────────────────────────
        tb_zoom = QToolBar("Zoom", self)
        tb_zoom.setMovable(False)
        tb_zoom.setObjectName("tb_zoom")
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb_zoom)

        act_zoom_in_tb  = tb_zoom.addAction("+ Zoom In")
        act_zoom_out_tb = tb_zoom.addAction("− Zoom Out")
        tb_zoom.addSeparator()
        act_fit_area_tb  = tb_zoom.addAction("⊡ Fit Area")
        act_fit_board_tb = tb_zoom.addAction("⊞ Fit Board")
        act_fit_sel_tb   = tb_zoom.addAction("⊟ Fit Sel")
        act_zoom_in_tb.triggered.connect(lambda: self._board_view.zoom_by(1.3))
        act_zoom_out_tb.triggered.connect(lambda: self._board_view.zoom_by(1 / 1.3))
        act_fit_area_tb.triggered.connect(self._fit_area)
        act_fit_board_tb.triggered.connect(self._fit_board)
        act_fit_sel_tb.triggered.connect(self._fit_selection)

        # ── Toolbar: Wire ────────────────────────────────────────────────
        tb_wire = QToolBar("Wire", self)
        tb_wire.setMovable(True)
        tb_wire.setObjectName("tb_wire")
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb_wire)

        self._act_tb_insulated = tb_wire.addAction("Ins [I]")
        self._act_tb_insulated.setCheckable(True)
        self._act_tb_insulated.setChecked(self._wire_insulated)
        self._act_tb_top = tb_wire.addAction("Top [T]")
        self._act_tb_top.setCheckable(True)
        self._act_tb_top.setChecked(self._wire_side == "top")
        self._act_tb_bottom = tb_wire.addAction("Bottom [B]")
        self._act_tb_bottom.setCheckable(True)
        self._act_tb_bottom.setChecked(self._wire_side == "bottom")
        self._act_tb_insulated.triggered.connect(
            lambda checked: self._set_wire_insulated(checked)
        )
        self._act_tb_top.triggered.connect(lambda: self._set_wire_side("top"))
        self._act_tb_bottom.triggered.connect(lambda: self._set_wire_side("bottom"))

        # ── Toolbar: Component ───────────────────────────────────────────
        tb_comp = QToolBar("Component", self)
        tb_comp.setMovable(True)
        tb_comp.setObjectName("tb_comp")
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb_comp)

        act_comp_add = tb_comp.addAction("+ Add")
        act_comp_del = tb_comp.addAction("− Del")
        act_comp_cw  = tb_comp.addAction("↻ CW")
        act_comp_ccw = tb_comp.addAction("↺ CCW")
        act_comp_add.triggered.connect(self._focus_library_for_add)
        act_comp_del.triggered.connect(self._delete_selection)
        act_comp_cw.triggered.connect(self._rotate_cw)
        act_comp_ccw.triggered.connect(self._rotate_ccw)

        # ── Toolbar: Analysis ────────────────────────────────────────────
        tb_analysis = QToolBar("Analysis", self)
        tb_analysis.setMovable(False)
        tb_analysis.setObjectName("tb_analysis")
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb_analysis)

        act_drc          = tb_analysis.addAction("DRC [F5]")
        act_autoroute_tb = tb_analysis.addAction("Autoroute [F6]")
        act_autoroute_tb.setToolTip(
            "Autoroute all nets (F6).\n"
            "Select nets in the Inspector panel first to route only those nets."
        )
        act_spread_tb    = tb_analysis.addAction("Spread")
        act_drc.triggered.connect(self._run_drc)
        act_autoroute_tb.triggered.connect(self._autoroute)
        act_spread_tb.triggered.connect(self._spread_components)

        # Status bar.
        self._status = QStatusBar(self)
        self.setStatusBar(self._status)
        self._mode_label = QLabel("Select")
        self._status.addWidget(self._mode_label)
        self._stats_label = QLabel("")
        self._status.addPermanentWidget(self._stats_label)
        self._view_label = QLabel("")
        self._status.addPermanentWidget(self._view_label)
        self._cursor_label = QLabel("(—, —)")
        self._status.addPermanentWidget(self._cursor_label)
        # Cursor-label throttle: ``cursor_moved`` fires per mouseMoveEvent
        # (up to ~120 Hz).  Updating a QLabel forces a status-bar repaint,
        # which is an X11 round-trip — over SSH this queues hundreds of
        # stale "intermediate position" repaints after the cursor stops.
        # Park the latest coords on ``_pending_cursor_pt`` and let the
        # timer batch updates to ~30 fps.
        self._cursor_timer = QTimer(self)
        self._cursor_timer.setSingleShot(True)
        self._cursor_timer.setInterval(self._app_settings.cursor_label_throttle_ms)
        self._cursor_timer.timeout.connect(self._flush_cursor_label)
        self._pending_cursor_pt: tuple[float, float] | None = None
        # Push the rubber-band / post-zoom throttles into the view too.
        self._board_view.apply_throttle_settings(
            rubber_band_ms=self._app_settings.rubber_band_throttle_ms,
            zoom_refresh_ms=self._app_settings.zoom_refresh_ms,
        )
        self._board_view.set_antialiasing(self._app_settings.antialiasing)

        # Auto-DRC timer — fires 600 ms after the last layout_changed.
        self._drc_timer = QTimer(self)
        self._drc_timer.setSingleShot(True)
        self._drc_timer.setInterval(600)
        self._drc_timer.timeout.connect(self._run_drc_silent)
        # In-flight DRC worker (if any).  Set when ``_dispatch_drc``
        # kicks off a background run; cleared in ``_on_drc_finished``.
        # Subsequent layout edits cancel the previous worker so its
        # result is dropped before it can overwrite the panel.
        self._drc_worker: object | None = None

        # Panel-refresh timer — debounces sidebar rebuilds to avoid blocking UI
        # on every drag-commit.  100 ms is fast enough to feel instant after a
        # move, but prevents rebuilding all trees on every keystroke/undo/redo.
        self._panels_timer = QTimer(self)
        self._panels_timer.setSingleShot(True)
        self._panels_timer.setInterval(100)
        self._panels_timer.timeout.connect(self._refresh_panels)

        # Signals.
        self._board_view.cursor_moved.connect(self._on_cursor_moved)
        self._board_view.escape_tool.connect(self._tool_select)
        self._board_view.spread_requested.connect(self._spread_components)
        self._library_panel.component_selected.connect(self._tool_place)
        self._netlist_panel.instance_selected.connect(self._board_view.select_component)
        self._components_panel.instance_selected.connect(self._board_view.select_component)
        self._components_panel.place_requested.connect(self._tool_place_instance)
        self._components_panel.auto_place_selected_requested.connect(
            self._auto_place_instances
        )
        self._components_panel.auto_place_all_requested.connect(
            self._auto_place_instances
        )
        session.layout_changed.connect(session.invalidate_graph)
        session.layout_changed.connect(self._panels_timer.start)
        session.layout_changed.connect(self._drc_timer.start)
        session.layout_changed.connect(self._update_undo_redo)
        session.layout_changed.connect(session.mark_dirty)
        session.autoroute_active_changed.connect(self._apply_autoroute_ui_lock)
        session.highlight_stack.changed.connect(self._update_highlight_action)
        session.dirty_changed.connect(self._update_title)
        session.drc_changed.connect(self._props_panel.update_issues)
        self._props_panel.component_changed.connect(self._on_component_changed)
        self._props_panel.pin_offsets_changed.connect(self._on_pin_offsets_changed)
        self._props_panel.pin_variant_changed.connect(self._on_pin_variant_changed)
        self._props_panel.drc_issue_clicked.connect(self._on_drc_issue_clicked)

        self._groups_panel.group_disbanded.connect(lambda _: self._board_view.refresh())
        self._groups_panel.group_disbanded.connect(lambda _: self._update_undo_redo())
        self._groups_panel.group_disbanded.connect(lambda _: self._session.mark_dirty())
        self._groups_panel.group_renamed.connect(lambda _: self._board_view.refresh())
        self._groups_panel.group_renamed.connect(lambda _: self._update_undo_redo())
        self._groups_panel.group_renamed.connect(lambda _: self._session.mark_dirty())

        self._nets_editor_panel.nets_changed.connect(self._on_nets_editor_changed)
        self._nets_editor_panel.net_selected.connect(self._board_view.set_highlighted_net)
        self._layers_panel.overrides_changed.connect(self._on_layer_panel_changed)
        self._session.selection_changed.connect(self._on_selection_changed)
        self._session.highlight_mode_changed.connect(self._hl_mode_widget.on_highlight_mode_changed)
        # Persist selection axes globally whenever the user changes them.
        self._sel_filter_widget.filter_changed.connect(self._on_filter_changed)
        self._hl_mode_widget.relation_changed.connect(self._on_relation_changed)

        # Populate panels if project already loaded.
        self._refresh_netlist()
        self._refresh_groups()
        self._refresh_nets_editor()
        self._refresh_components()
        self._refresh_net_inspector()
        self._update_stats()
        self._update_view_label()

        # Default tool.
        self._tool_select()

        # Restore window geometry and dock layout from previous session.
        load_window_state(self)

    # ------------------------------------------------------------------
    # Menu
    # ------------------------------------------------------------------

    def _ks(self, action_id: str) -> QKeySequence:
        """Return a QKeySequence for *action_id* (empty if no shortcut set)."""
        s = self._keybindings.get(action_id)
        return QKeySequence(s) if s else QKeySequence()

    # Layer cycling — field name + display label + ACTION id, in menu order.
    # Class-level so _register_actions() can read it before _build_menu() runs.
    _LAYER_ENTRIES: list[tuple[str, str, str]] = [
        ("board",             "Board",              KB.ACTION_LAYER_1),
        ("components_top",    "Components Top",     KB.ACTION_LAYER_2),
        ("components_bottom", "Components Bottom",  KB.ACTION_LAYER_3),
        ("wires_top",         "Wires Top",          KB.ACTION_LAYER_4),
        ("wires_bottom",      "Wires Bottom",       KB.ACTION_LAYER_5),
        ("labels",            "Labels",             KB.ACTION_LAYER_6),
        ("pin_names",         "Pin Names",          KB.ACTION_LAYER_7),
        ("ratsnest",          "Connections",        KB.ACTION_LAYER_8),
    ]

    def _register_actions(self) -> None:
        """Create one QAction per entry in the handler map.

        All QActions are added to ``self`` (QMainWindow) with the default
        ``Qt.ShortcutContext.WindowShortcut`` so Qt routes their shortcut
        regardless of whether they are subsequently put in a menu.  Menu
        items consume them via ``self._actions[KB.ACTION_X]``; orphan
        entries (no menu) still fire their handler on the bound shortcut.
        """
        self._layer_field_order: list[str] = [
            field_name for field_name, _label, _aid in self._LAYER_ENTRIES
        ]

        # Per-instance handler map — built here because handlers need bound
        # ``self``.  Order does not matter — Qt does the routing.
        self._action_handlers: dict[str, "Callable[[], None]"] = {
            # File
            KB.ACTION_SAVE:    self._save,
            KB.ACTION_SAVE_AS: self._save_as,
            KB.ACTION_QUIT:    self.close,
            # Edit
            KB.ACTION_UNDO:    self._undo,
            KB.ACTION_REDO:    self._redo,
            KB.ACTION_DELETE:  self._delete_selection,
            KB.ACTION_CLEAR_HIGHLIGHT: self._clear_highlight,
            KB.ACTION_ROTATE_CW:  self._rotate_cw,
            KB.ACTION_ROTATE_CCW: self._rotate_ccw,
            KB.ACTION_FLIP:       self._flip_selection,
            KB.ACTION_GROUP:      self._group_selection,
            KB.ACTION_UNGROUP:    self._ungroup_selection,
            KB.ACTION_EXIT_GROUP: self._exit_group,
            KB.ACTION_PREFERENCES: self._open_preferences,
            # Select
            KB.ACTION_SELECT_ALL:               self._select_all,
            KB.ACTION_DESELECT_ALL:             self._deselect_all,
            KB.ACTION_SELECT_ALL_COMPONENTS:    self._select_all_components,
            KB.ACTION_DESELECT_ALL_COMPONENTS:  self._deselect_all_components,
            KB.ACTION_SELECT_ALL_WIRES:         self._select_all_wires,
            KB.ACTION_DESELECT_ALL_WIRES:       self._deselect_all_wires,
            # View
            KB.ACTION_CAMERA_TOGGLE: lambda: self._set_camera("toggle"),
            KB.ACTION_MIRROR_H:      self._mirror_view,
            KB.ACTION_MIRROR_V:      self._mirror_view_v,
            KB.ACTION_FULLSCREEN:    self._toggle_fullscreen,
            KB.ACTION_ZOOM_IN:       lambda: self._board_view.zoom_by(1.3),
            KB.ACTION_ZOOM_OUT:      lambda: self._board_view.zoom_by(1 / 1.3),
            KB.ACTION_FIT_BOARD:     self._fit_board,
            KB.ACTION_FIT_AREA:      self._fit_area,
            KB.ACTION_FIT_SELECTION: self._fit_selection,
            # Tools
            KB.ACTION_TOOL_SELECT:          self._tool_select,
            KB.ACTION_TOOL_ROUTE:           self._tool_route,
            KB.ACTION_TOOL_ROUTE_NET_AWARE: lambda: self._tool_route(net_aware=True),
            KB.ACTION_TOOL_CUT:             self._tool_cut,
            KB.ACTION_TOOL_SIMPLIFY:        self._tool_simplify,
            KB.ACTION_WIRE_TOGGLE_INSULATED: lambda: self._set_wire_insulated(not self._wire_insulated),
            KB.ACTION_WIRE_SIDE_TOP:         lambda: self._set_wire_side("top"),
            KB.ACTION_WIRE_SIDE_BOTTOM:      lambda: self._set_wire_side("bottom"),
            KB.ACTION_REDRAG_PINS:           self._redrag_pins,
            KB.ACTION_WIRE_SET_INSULATED:    lambda: self._set_selected_wire_insulated(True),
            KB.ACTION_WIRE_SET_BARE:         lambda: self._set_selected_wire_insulated(False),
            KB.ACTION_WIRE_MOVE_TO_TOP:      lambda: self._move_selected_wires_to_side("top"),
            KB.ACTION_WIRE_MOVE_TO_BOTTOM:   lambda: self._move_selected_wires_to_side("bottom"),
            KB.ACTION_WIRE_FLIP:             lambda: self._move_selected_wires_to_side(None),
            KB.ACTION_DISCONNECT_PIN:        self._disconnect_pin,
            KB.ACTION_AUTOROUTE:             self._autoroute,
            KB.ACTION_RUN_DRC:               self._run_drc,
            # Orphan — no menu entry, only keyboard shortcut.
            KB.ACTION_FILTER_TOGGLE_COMPONENTS: lambda: self._toggle_filter("components"),
            KB.ACTION_FILTER_TOGGLE_WIRES:      lambda: self._toggle_filter("wires"),
            KB.ACTION_FILTER_TOGGLE_GROUPS:     lambda: self._toggle_filter("groups"),
            KB.ACTION_HIGHLIGHT_CYCLE:          lambda: self._cycle_relation(forward=True),
            KB.ACTION_RELATION_CYCLE_BACK:      lambda: self._cycle_relation(forward=False),
            KB.ACTION_RELATION_NONE:            lambda: self._set_relation_by_name("none"),
            KB.ACTION_RELATION_LOGICAL_NET:     lambda: self._set_relation_by_name("same_logical_net"),
            KB.ACTION_RELATION_PHYSICAL_NET:    lambda: self._set_relation_by_name("same_physical_net"),
            KB.ACTION_RELATION_UNCONNECTED:     lambda: self._set_relation_by_name("unconnected_pins"),
            KB.ACTION_RELATION_NEIGHBORS:       lambda: self._set_relation_by_name("neighbors"),
            KB.ACTION_SIMPLIFY_WIRE:            self._simplify_selected_wires,
            KB.ACTION_UNWEAVE_WIRES:            self._unweave_selected_wires,
            # ACTION_HIGHLIGHT_TOGGLE: kept as an alias of clear-highlight
            # for now — the historic intent was "drop the persistent stack
            # entry like Esc".  Wire it to _clear_highlight so the action
            # has a working handler.
            KB.ACTION_HIGHLIGHT_TOGGLE:         self._clear_highlight,
        }
        # Layer 1..8 — bind layer-index via default-arg lambda to avoid the
        # late-binding closure pitfall.  The first positional arg swallows
        # Qt's QAction.triggered(checked=False) so it doesn't bleed into the
        # default-arg index slot.
        for idx, (_field, _label, layer_action_id) in enumerate(self._LAYER_ENTRIES):
            self._action_handlers[layer_action_id] = (
                lambda *_args, i=idx: self._cycle_layer(self._layer_field_order[i])
            )

        self._actions: dict[str, QAction] = {}
        for action_id, handler in self._action_handlers.items():
            meta = self._ACTION_METADATA.get(action_id, {})
            label = meta.get("label") or KB.DISPLAY_NAMES.get(action_id, action_id)
            act = QAction(label, self)
            act.setShortcut(self._ks(action_id))
            act.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            if meta.get("checkable", False):
                act.setCheckable(True)
            if not meta.get("enabled", True):
                act.setEnabled(False)
            act.triggered.connect(handler)
            self._actions[action_id] = act
            self.addAction(act)

        # Derived view: layer field-name → QAction, for _refresh_layer_menu().
        self._layer_actions: dict[str, QAction] = {
            field_name: self._actions[layer_action_id]
            for field_name, _label, layer_action_id in self._LAYER_ENTRIES
        }

    # ------------------------------------------------------------------
    # Backward-compat aliases — historic per-attribute names kept as
    # properties so existing callsites (_update_undo_redo, _sync_view_actions,
    # _set_wire_*, etc.) keep working without a sweep.
    # ------------------------------------------------------------------
    @property
    def _act_undo(self) -> QAction: return self._actions[KB.ACTION_UNDO]
    @property
    def _act_redo(self) -> QAction: return self._actions[KB.ACTION_REDO]
    @property
    def _act_clear_highlight(self) -> QAction: return self._actions[KB.ACTION_CLEAR_HIGHLIGHT]
    @property
    def _act_mirror(self) -> QAction: return self._actions[KB.ACTION_MIRROR_H]
    @property
    def _act_mirror_v(self) -> QAction: return self._actions[KB.ACTION_MIRROR_V]
    @property
    def _act_wire_insulated(self) -> QAction: return self._actions[KB.ACTION_WIRE_TOGGLE_INSULATED]
    @property
    def _act_wire_top(self) -> QAction: return self._actions[KB.ACTION_WIRE_SIDE_TOP]
    @property
    def _act_wire_bottom(self) -> QAction: return self._actions[KB.ACTION_WIRE_SIDE_BOTTOM]

    def _build_menu(self) -> None:
        mb: QMenuBar = self.menuBar()

        # ── File ──────────────────────────────────────────────────────────
        file_menu = mb.addMenu("&File")

        file_menu.addAction(self._actions[KB.ACTION_SAVE])
        file_menu.addAction(self._actions[KB.ACTION_SAVE_AS])

        file_menu.addSeparator()

        self._recent_menu: QMenu = file_menu.addMenu("Open &Recent")
        self._populate_recent_menu()

        file_menu.addSeparator()

        act_import_net = file_menu.addAction("&Import KiCAD .net…")
        act_import_net.triggered.connect(self._import_kicad_net)

        act_export_net = file_menu.addAction("Export KiCAD .net…")
        act_export_net.triggered.connect(self._export_kicad_net)

        file_menu.addSeparator()

        act_export_svg = file_menu.addAction("Export SVG…")
        act_export_svg.triggered.connect(self._export_svg)

        act_export_pdf = file_menu.addAction("Export PDF…")
        act_export_pdf.triggered.connect(self._export_pdf)

        act_export_png = file_menu.addAction("Export PNG…")
        act_export_png.triggered.connect(self._export_png)

        act_export_bom = file_menu.addAction("Export BOM CSV…")
        act_export_bom.triggered.connect(self._export_bom)

        file_menu.addSeparator()

        act_close = file_menu.addAction("&Close Project")
        act_close.triggered.connect(self._close_project)

        file_menu.addSeparator()

        file_menu.addAction(self._actions[KB.ACTION_QUIT])

        # ── Edit ──────────────────────────────────────────────────────────
        edit_menu = mb.addMenu("&Edit")

        edit_menu.addAction(self._actions[KB.ACTION_UNDO])
        edit_menu.addAction(self._actions[KB.ACTION_REDO])

        edit_menu.addSeparator()

        edit_menu.addAction(self._actions[KB.ACTION_DELETE])

        edit_menu.addSeparator()

        edit_menu.addAction(self._actions[KB.ACTION_CLEAR_HIGHLIGHT])

        edit_menu.addSeparator()

        edit_menu.addAction(self._actions[KB.ACTION_ROTATE_CW])
        edit_menu.addAction(self._actions[KB.ACTION_ROTATE_CCW])
        edit_menu.addAction(self._actions[KB.ACTION_FLIP])

        edit_menu.addSeparator()

        edit_menu.addAction(self._actions[KB.ACTION_GROUP])
        edit_menu.addAction(self._actions[KB.ACTION_UNGROUP])
        edit_menu.addAction(self._actions[KB.ACTION_EXIT_GROUP])

        edit_menu.addSeparator()

        edit_menu.addAction(self._actions[KB.ACTION_PREFERENCES])

        edit_menu.addSeparator()

        generate_menu = edit_menu.addMenu("&Generate")
        act_gen_shortcuts = generate_menu.addAction("Shortcut Config &File")
        act_gen_shortcuts.triggered.connect(self._generate_shortcut_config)

        # ── Select ────────────────────────────────────────────────────────
        select_menu = mb.addMenu("&Select")

        select_menu.addAction(self._actions[KB.ACTION_SELECT_ALL])
        select_menu.addAction(self._actions[KB.ACTION_DESELECT_ALL])

        select_menu.addSeparator()

        select_menu.addAction(self._actions[KB.ACTION_SELECT_ALL_COMPONENTS])
        select_menu.addAction(self._actions[KB.ACTION_DESELECT_ALL_COMPONENTS])

        select_menu.addSeparator()

        select_menu.addAction(self._actions[KB.ACTION_SELECT_ALL_WIRES])
        select_menu.addAction(self._actions[KB.ACTION_DESELECT_ALL_WIRES])

        # ── View ──────────────────────────────────────────────────────────
        view_menu = mb.addMenu("&View")

        # Camera side (which physical side is in front).  Two checkable items
        # without shortcut, plus the configurable toggle that lives in the
        # action registry.
        self._act_camera_top: QAction = view_menu.addAction("Camera: &Top")
        self._act_camera_top.setCheckable(True)
        self._act_camera_top.setChecked(True)
        self._act_camera_top.triggered.connect(lambda: self._set_camera("top"))

        self._act_camera_bottom: QAction = view_menu.addAction("Camera: &Bottom")
        self._act_camera_bottom.setCheckable(True)
        self._act_camera_bottom.triggered.connect(lambda: self._set_camera("bottom"))

        view_menu.addAction(self._actions[KB.ACTION_CAMERA_TOGGLE])

        view_menu.addSeparator()

        # Visual transform (independent of camera).
        view_menu.addAction(self._actions[KB.ACTION_MIRROR_H])
        view_menu.addAction(self._actions[KB.ACTION_MIRROR_V])

        view_menu.addSeparator()

        # ── Layers submenu ────────────────────────────────────────────
        layers_menu = view_menu.addMenu("&Layers")
        for _field_name, _display_name, action_id in self._LAYER_ENTRIES:
            layers_menu.addAction(self._actions[action_id])
        layers_menu.addSeparator()
        act_reset_layers = layers_menu.addAction("Reset All Layers")
        act_reset_layers.triggered.connect(self._reset_layers)

        view_menu.addSeparator()

        view_menu.addAction(self._actions[KB.ACTION_ZOOM_IN])
        view_menu.addAction(self._actions[KB.ACTION_ZOOM_OUT])
        view_menu.addAction(self._actions[KB.ACTION_FIT_BOARD])
        view_menu.addAction(self._actions[KB.ACTION_FIT_AREA])
        view_menu.addAction(self._actions[KB.ACTION_FIT_SELECTION])

        view_menu.addSeparator()

        view_menu.addAction(self._actions[KB.ACTION_FULLSCREEN])

        # ── Tools ─────────────────────────────────────────────────────────
        tools_menu = mb.addMenu("&Tools")

        tools_menu.addAction(self._actions[KB.ACTION_TOOL_SELECT])
        tools_menu.addAction(self._actions[KB.ACTION_TOOL_ROUTE])
        tools_menu.addAction(self._actions[KB.ACTION_TOOL_ROUTE_NET_AWARE])

        wire_sub = tools_menu.addMenu("  Wire options")
        wire_sub.addAction(self._actions[KB.ACTION_WIRE_TOGGLE_INSULATED])
        wire_sub.addAction(self._actions[KB.ACTION_WIRE_SIDE_TOP])
        wire_sub.addAction(self._actions[KB.ACTION_WIRE_SIDE_BOTTOM])

        tools_menu.addAction(self._actions[KB.ACTION_TOOL_CUT])
        tools_menu.addAction(self._actions[KB.ACTION_TOOL_SIMPLIFY])

        tools_menu.addSeparator()

        tools_menu.addAction(self._actions[KB.ACTION_REDRAG_PINS])

        tools_menu.addSeparator()

        tools_menu.addAction(self._actions[KB.ACTION_WIRE_SET_INSULATED])
        tools_menu.addAction(self._actions[KB.ACTION_WIRE_SET_BARE])
        tools_menu.addAction(self._actions[KB.ACTION_WIRE_MOVE_TO_TOP])
        tools_menu.addAction(self._actions[KB.ACTION_WIRE_MOVE_TO_BOTTOM])
        tools_menu.addAction(self._actions[KB.ACTION_WIRE_FLIP])

        tools_menu.addSeparator()

        tools_menu.addAction(self._actions[KB.ACTION_DISCONNECT_PIN])

        tools_menu.addSeparator()

        act_resize_board = tools_menu.addAction("&Resize Board…")
        act_resize_board.triggered.connect(self._resize_board)

        tools_menu.addSeparator()

        tools_menu.addAction(self._actions[KB.ACTION_AUTOROUTE])

        act_spread = tools_menu.addAction("S&pread Components")
        act_spread.triggered.connect(self._spread_components)

        tools_menu.addSeparator()

        tools_menu.addAction(self._actions[KB.ACTION_RUN_DRC])


    # ------------------------------------------------------------------
    # Recent projects
    # ------------------------------------------------------------------

    def _populate_recent_menu(self) -> None:
        """Rebuild the Open Recent submenu from the persisted list."""
        self._recent_menu.clear()
        recents = recent_projects.load()
        if not recents:
            placeholder = self._recent_menu.addAction("(no recent projects)")
            placeholder.setEnabled(False)
            return
        for path in recents:
            act = self._recent_menu.addAction(str(path))
            act.triggered.connect(
                lambda _checked, p=path: self._open_recent_path(p)
            )

    def _open_recent_path(self, path: Path) -> None:
        """Open a recent project, prompting to save unsaved changes first."""
        if not path.exists():
            QMessageBox.warning(self, "Not found", f"Project not found:\n{path}")
            self._populate_recent_menu()
            return
        if not self._check_unsaved_changes("opening another project"):
            return
        try:
            (board, layout, schematic, instances,
             routing_settings, layer_overrides, axis_label_settings) = load_project(path)
        except Exception as exc:
            QMessageBox.critical(self, "Open error", str(exc))
            return
        # Stop the recorder on the outgoing session before swapping it in.
        self._session.stop_recording()
        session = EditorSession(board=board, library=self._session.library)
        session.layout = layout
        session.schematic = schematic
        session.instances = instances
        session.routing_settings = routing_settings
        session.axis_label_settings = axis_label_settings
        session.project_path = path
        recent_projects.add(path)
        session.start_recording()
        # Reconnect signals from the old session.
        self._session.layout_changed.disconnect(self._session.invalidate_graph)
        self._session.layout_changed.disconnect(self._panels_timer.start)
        self._session.layout_changed.disconnect(self._drc_timer.start)
        self._session.layout_changed.disconnect(self._update_undo_redo)
        self._session.layout_changed.disconnect(self._session.mark_dirty)
        self._session.dirty_changed.disconnect(self._update_title)
        self._session.drc_changed.disconnect(self._props_panel.update_issues)
        try:
            self._props_panel.drc_issue_clicked.disconnect(self._on_drc_issue_clicked)
        except (TypeError, RuntimeError):
            pass  # not connected yet — first-time path
        self._session = session
        session.layout_changed.connect(session.invalidate_graph)
        session.layout_changed.connect(self._panels_timer.start)
        session.layout_changed.connect(self._drc_timer.start)
        session.layout_changed.connect(self._update_undo_redo)
        session.layout_changed.connect(session.mark_dirty)
        session.autoroute_active_changed.connect(self._apply_autoroute_ui_lock)
        session.dirty_changed.connect(self._update_title)
        session.drc_changed.connect(self._props_panel.update_issues)
        self._props_panel.drc_issue_clicked.connect(self._on_drc_issue_clicked)
        # Reset select tool state (group stack etc.) for the new session.
        self._select_tool = SelectTool()
        self._board_view._permanent_tools = [self._select_tool]
        self._apply_selection_settings_to_tool(self._select_tool)
        self._sel_filter_widget.set_tool(self._select_tool)
        self._hl_mode_widget.set_tool(self._select_tool)
        session.selection_changed.connect(self._on_selection_changed)
        session.highlight_mode_changed.connect(self._hl_mode_widget.on_highlight_mode_changed)
        session.layer_overrides = layer_overrides
        self._layer_overrides = layer_overrides
        self._board_view._layer_overrides = layer_overrides
        self._refresh_layer_menu()
        self._layers_panel.sync()
        self._board_view.set_session(session)
        self._perf_panel.set_session(session)
        self._tool_select()
        self._refresh_netlist()
        self._refresh_groups()
        self._refresh_nets_editor()
        self._refresh_components()
        self._refresh_net_inspector()
        self._update_stats()
        self._update_undo_redo()
        self._update_title()
        self._populate_recent_menu()
        # Frame the freshly-loaded board.  The viewport is already at its
        # final size here (the window is fully laid out by the time the user
        # picks "Open Recent"), so a single synchronous fit is enough.
        self._board_view.fit_board()
        # Notify the user if legacy fractional positions were snapped on load.
        normalize_report = pop_last_normalize_report(path)
        if normalize_report is not None and normalize_report.any_corrections:
            n_pl = len(normalize_report.placements)
            n_wp = len(normalize_report.wires)
            parts = []
            if n_pl:
                parts.append(f"{n_pl} pozycji komponentów")
            if n_wp:
                parts.append(f"{n_wp} punkt(ów) przewodów")
            self.statusBar().showMessage(
                "Naprawiono offsety legacy: " + " + ".join(parts)
                + " (zapisz projekt, aby utrwalić)",
                8000,
            )
            self._session.mark_dirty()

    # ------------------------------------------------------------------
    # Title helper
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Layer visibility
    # ------------------------------------------------------------------

    def _cycle_layer(self, field_name: str) -> None:
        """Cycle the named layer override: AUTO → FULL → GHOST → HIDDEN → AUTO."""
        current = getattr(self._layer_overrides, field_name)
        setattr(self._layer_overrides, field_name, current.cycle())
        self._session.layer_overrides = self._layer_overrides
        self._board_view._layer_overrides = self._layer_overrides
        self._refresh_layer_menu()
        self._layers_panel.sync()
        self._board_view.refresh()

    def _reset_layers(self) -> None:
        self._layer_overrides.reset()
        self._session.layer_overrides = self._layer_overrides
        self._board_view._layer_overrides = self._layer_overrides
        self._refresh_layer_menu()
        self._layers_panel.sync()
        self._board_view.refresh()

    def _on_layer_panel_changed(self) -> None:
        """Called when user clicks a button in the Layers panel."""
        self._board_view._layer_overrides = self._layer_overrides
        self._refresh_layer_menu()
        self._board_view.refresh()

    def _refresh_layer_menu(self) -> None:
        """Update layer menu action labels to reflect current override states."""
        _NAMES = {
            "board":             "Board",
            "components_top":    "Components Top",
            "components_bottom": "Components Bottom",
            "wires_top":         "Wires Top",
            "wires_bottom":      "Wires Bottom",
            "labels":            "Labels",
            "pin_names":         "Pin Names",
            "ratsnest":          "Connections",
        }
        for field_name, act in self._layer_actions.items():
            vis = getattr(self._layer_overrides, field_name)
            suffix = vis.label()
            base = _NAMES[field_name]
            act.setText(f"{base}  {suffix}" if suffix else base)

    # ------------------------------------------------------------------

    def _update_title(self, _dirty: bool = False) -> None:
        p = self._session.project_path
        name = p.name if p is not None else "Protoboard Designer"
        suffix = " — autoroute…" if self._session.autoroute_active else ""
        self.setWindowTitle(f"{name}{suffix}[*]")
        self.setWindowModified(self._session.is_dirty)

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def _refresh_netlist(self) -> None:
        self._netlist_panel.refresh(self._session)

    def _refresh_groups(self) -> None:
        self._groups_panel.refresh(self._session)

    def _refresh_nets_editor(self) -> None:
        self._nets_editor_panel.refresh(self._session)

    def _refresh_components(self) -> None:
        self._components_panel.refresh(self._session)

    def _refresh_net_inspector(self) -> None:
        self._net_inspector_panel.refresh(self._session)

    def _on_selection_changed(
        self, comp_ids: object, wire_ids: object, net_id: object
    ) -> None:
        """Broadcast board selection state to all panels."""
        _t0 = _time.monotonic() if _CLICK_PROFILE else 0.0
        self._netlist_panel.sync_selection(comp_ids, wire_ids, net_id)
        _t1 = _time.monotonic() if _CLICK_PROFILE else 0.0
        self._components_panel.sync_selection(comp_ids, wire_ids, net_id)
        _t2 = _time.monotonic() if _CLICK_PROFILE else 0.0
        self._nets_editor_panel.sync_selection(comp_ids, wire_ids, net_id)
        _t3 = _time.monotonic() if _CLICK_PROFILE else 0.0
        self._net_inspector_panel.sync_selection(comp_ids, wire_ids, net_id)
        if _CLICK_PROFILE:
            _click_log.info(
                "click_profile.main_window.on_selection_changed",
                netlist_ms=round((_t1 - _t0) * 1000, 1),
                components_ms=round((_t2 - _t1) * 1000, 1),
                nets_editor_ms=round((_t3 - _t2) * 1000, 1),
                net_inspector_ms=round((_time.monotonic() - _t3) * 1000, 1),
            )

    def _refresh_panels(self) -> None:
        """Debounced refresh of all sidebar panels — called via _panels_timer."""
        self._refresh_netlist()
        self._refresh_groups()
        self._refresh_nets_editor()
        self._refresh_components()
        self._refresh_net_inspector()

    def _on_nets_editor_changed(self) -> None:
        self._session.mark_dirty()
        self._refresh_netlist()
        self._update_undo_redo()
        self._update_stats()
        self._board_view.refresh()
        self._drc_timer.start()

    # ------------------------------------------------------------------
    # Tool switches
    # ------------------------------------------------------------------

    def _tool_select(self) -> None:
        self._board_view.set_tool(self._select_tool)
        self._mode_label.setText("Select")

    def _tool_place(self, definition_ref: str, pin_variant: object = None) -> None:
        variant_str = str(pin_variant) if pin_variant is not None else None
        self._board_view.set_tool(PlaceTool(definition_ref, pin_variant=variant_str))
        label = definition_ref
        if variant_str:
            label = f"{definition_ref} [{variant_str}]"
        self._mode_label.setText(f"Place {label}  [R] rotate")

    def _tool_place_instance(self, instance_id: uuid.UUID) -> None:
        """Activate PlaceTool for a specific netlist-imported instance."""
        inst = self._session.instances.get(instance_id)
        if inst is None:
            return
        if instance_id in self._session.layout.placements:
            self._status.showMessage(
                f"{inst.refdes} is already placed on the board.", 3000
            )
            return
        tool = PlaceTool(inst.definition_ref, existing_instance=inst)
        self._board_view.set_tool(tool)
        self._mode_label.setText(
            f"Place {inst.refdes} ({inst.value})  [R] rotate"
        )

    def _auto_place_instances(self, instance_ids: list[uuid.UUID]) -> None:
        """Auto-place a list of component instances using net-gravity heuristic."""
        from protoboard.domain.commands.base import Command, MacroCommand
        from protoboard.domain.commands.component_cmds import (
            ComponentState,
            PlaceComponentCommand,
        )
        from protoboard.gui.workers.operation_worker import run_with_progress
        from typing import cast

        to_place = [
            inst
            for iid in instance_ids
            if (inst := self._session.instances.get(iid)) is not None
            and iid not in self._session.layout.placements
        ]
        if not to_place:
            self._status.showMessage("No unplaced components to place.", 3000)
            return

        result = run_with_progress(
            parent=self,
            title="Auto-place",
            fn=lambda: auto_place(to_place, self._session, side="top"),
            label=f"Placing {len(to_place)} component{'s' if len(to_place) != 1 else ''}…",
        )
        if result is None:
            return

        if not result.placements:
            self._status.showMessage(
                "Auto-place: no free positions found on the board.", 4000
            )
            return

        cmds: list[Command[ComponentState]] = []
        for pl in result.placements:
            inst = self._session.instances.get(pl.component_id)
            if inst is None:
                continue
            cmds.append(
                cast(
                    Command[ComponentState],
                    PlaceComponentCommand(
                        component_id=pl.component_id,
                        placement=pl,
                        instance=inst,
                    ),
                )
            )

        if not cmds:
            return

        n = len(cmds)
        macro: Command[ComponentState] = (
            cmds[0]
            if n == 1
            else cast(
                Command[ComponentState],
                MacroCommand(
                    commands=cmds,
                    description=f"Auto-place {n} component{'s' if n != 1 else ''}",
                ),
            )
        )

        self._session.stack.execute(macro, self._session)
        self._session.layout_changed.emit()
        self._board_view.refresh()
        self._update_stats()
        self._update_undo_redo()

        n_outside = len(result.outside_board)
        n_skipped = len(result.skipped)
        msg = f"Auto-placed {n} component{'s' if n != 1 else ''}."
        if n_outside:
            n_inst = {pl.component_id for pl in result.placements}
            outside_refs = [
                self._session.instances[iid].refdes
                for iid in result.outside_board
                if iid in self._session.instances
            ]
            msg += (
                f" {n_outside} placed outside board bounds"
                f" ({', '.join(outside_refs[:5])}{'…' if n_outside > 5 else ''})"
                " — consider resizing the board."
            )
        if n_skipped:
            msg += f" {n_skipped} could not be placed (unknown footprint or no space)."
        self._status.showMessage(msg, 8000)
        if n_outside:
            QMessageBox.information(self, "Auto-place", msg)

    def _spread_components(self) -> None:
        """Spread placed components more evenly using force-directed relaxation."""
        from typing import cast

        from protoboard.domain.commands.base import Command, MacroCommand
        from protoboard.domain.commands.component_cmds import (
            ComponentState,
            MoveComponentCommand,
        )
        from protoboard.gui.workers.operation_worker import run_with_progress

        tool = self._get_select_tool()
        selected: set[uuid.UUID] = set()
        if tool is not None:
            selected.update(tool._selected_ids)
            if tool._selected_id is not None:
                selected.add(tool._selected_id)

        if selected:
            comp_ids = [
                cid for cid in selected if cid in self._session.layout.placements
            ]
            scope_label = f"{len(comp_ids)} selected component"
        else:
            comp_ids = list(self._session.layout.placements.keys())
            scope_label = f"all {len(comp_ids)} component"

        if len(comp_ids) < 2:
            self._status.showMessage(
                "Need at least 2 placed components to spread.", 3000
            )
            return

        result = run_with_progress(
            parent=self,
            title="Spread",
            fn=lambda: spread_components(comp_ids, self._session),
            label=f"Spreading {len(comp_ids)} component{'s' if len(comp_ids) != 1 else ''}…",
        )
        if result is None:
            return

        if not result.moved_ids:
            self._status.showMessage(
                "Spread: components are already well-distributed.", 4000
            )
            return

        cmds: list[Command[ComponentState]] = []
        for cid in result.moved_ids:
            old_pl = self._session.layout.placements[cid]
            new_pl = result.new_placements[cid]
            cmds.append(
                cast(
                    Command[ComponentState],
                    MoveComponentCommand(
                        component_id=cid,
                        new_placement=new_pl,
                        old_placement=old_pl.copy(),
                        skip_conflict_check=True,
                    ),
                )
            )

        n = len(cmds)
        macro: Command[ComponentState] = (
            cmds[0]
            if n == 1
            else cast(
                Command[ComponentState],
                MacroCommand(
                    commands=cmds,
                    description=f"Spread {scope_label}{'s' if n != 1 else ''}",
                ),
            )
        )

        self._session.stack.execute(macro, self._session)
        self._session.layout_changed.emit()
        self._board_view.refresh()
        self._update_stats()
        self._update_undo_redo()

        msg = f"Spread {n} component{'s' if n != 1 else ''}."
        n_unchanged = len(result.unchanged_ids)
        if n_unchanged:
            msg += f" {n_unchanged} could not move (no free slot)."
        msg += " Consider re-routing wires."
        self._status.showMessage(msg, 6000)

    def _tool_route(self, net_aware: bool = False) -> None:
        tool = self._board_view._active_tool
        if isinstance(tool, RouteTool):
            # Already routing — just toggle net_aware mode, keep current state.
            tool._net_aware = net_aware
            tool._notify_status()
            tool._redraw_committed_lines()
        else:
            self._board_view.set_tool(
                RouteTool(insulated=self._wire_insulated, side=self._wire_side, net_aware=net_aware)
            )
        self._update_route_status(self._wire_insulated, self._wire_side, net_aware)

    def _set_wire_insulated(self, insulated: bool) -> None:
        self._wire_insulated = insulated
        self._act_wire_insulated.setChecked(insulated)
        self._act_tb_insulated.setChecked(insulated)
        tool = self._board_view._active_tool
        if isinstance(tool, RouteTool):
            tool._insulated = insulated
            self._update_route_status(insulated, self._wire_side)

    def _set_wire_side(self, side: str) -> None:
        self._wire_side = side
        self._act_wire_top.setChecked(side == "top")
        self._act_wire_bottom.setChecked(side == "bottom")
        self._act_tb_top.setChecked(side == "top")
        self._act_tb_bottom.setChecked(side == "bottom")
        tool = self._board_view._active_tool
        if isinstance(tool, RouteTool):
            tool._side = side
            self._update_route_status(self._wire_insulated, side)

    def _focus_library_for_add(self) -> None:
        """Switch to library tab and focus search, so user can pick a component."""
        # Find the left dock tab widget and switch to Library tab.
        for child in self.findChildren(QTabWidget):
            for i in range(child.count()):
                if child.tabText(i) == "Library":
                    child.setCurrentIndex(i)
                    self._library_panel.setFocus()
                    break

    def _update_route_status(self, insulated: bool, side: str, net_aware: bool = False) -> None:
        style = "Insulated" if insulated else "Bare"
        mode = "[NET] " if net_aware else ""
        hint = "[W] basic" if net_aware else "[Shift+W] net-aware"
        self._mode_label.setText(
            f"Route Wire {mode} {style} [{side}]  [I] toggle  [T/B] side  {hint}  [Enter] commit  [Esc] cancel"
        )

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            if self._pre_fullscreen_maximized:
                self.showMaximized()
            else:
                self.showNormal()
        else:
            self._pre_fullscreen_maximized = self.isMaximized()
            self.showFullScreen()

    def _mirror_view(self) -> None:
        self._board_view.toggle_mirror_h()
        self._sync_view_actions()

    def _mirror_view_v(self) -> None:
        self._board_view.toggle_mirror_v()
        self._sync_view_actions()

    def _set_camera(self, side: str) -> None:
        if side == "toggle":
            current_side = self._board_view.camera_side
            side = "top" if current_side == "bottom" else "bottom"

        self._board_view.set_camera_side(side)
        self._sync_view_actions()
        # Keep wire side in sync.
        self._set_wire_side(side)

    def _sync_view_actions(self) -> None:
        """Synchronise all view-related checkable actions with BoardView state."""
        mh = self._board_view.view_transform == "mirror_h"
        mv = self._board_view.view_transform == "mirror_v"
        bottom = self._board_view.camera_side == "bottom"
        self._act_mirror.setChecked(mh)
        self._act_mirror_tb.setChecked(mh)
        self._act_mirror_v.setChecked(mv)
        self._act_mirror_v_tb.setChecked(mv)
        self._act_camera_top.setChecked(not bottom)
        self._act_camera_bottom.setChecked(bottom)
        self._act_camera_tb.setChecked(bottom)
        self._update_view_label()

    def _update_view_label(self) -> None:
        camera = self._board_view.camera_side.capitalize()   # "Top" | "Bottom"
        transform = self._board_view.view_transform          # "normal"|"mirror_h"|"mirror_v"
        mirror_tag = {"normal": "N", "mirror_h": "H", "mirror_v": "V"}[transform]
        self._view_label.setText(f"{camera}  Mirror:{mirror_tag}   ")

    def _resize_board(self) -> None:
        from protoboard.domain.commands.board_cmds import ResizeBoardCommand
        from protoboard.gui.dialogs.resize_board_dialog import ResizeBoardDialog

        dlg = ResizeBoardDialog(
            board=self._session.board,
            layout=self._session.layout,
            instances=self._session.instances,
            components=self._session.components,
            parent=self,
        )
        if dlg.exec() != ResizeBoardDialog.DialogCode.Accepted:
            return

        new_board = dlg.new_board()
        cmd = ResizeBoardCommand(new_board=new_board)
        self._session.stack.execute(cmd, self._session)
        self._session.invalidate_graph()
        # The board geometry itself changed — force the pads/strips layer to
        # be rebuilt; ``layout_changed`` alone only re-renders
        # components/wires, since the board layer is cached on a separate
        # ``_board_layer_dirty`` flag for performance.
        self._board_view._board_layer_dirty = True
        self._session.layout_changed.emit()
        self._board_view.fit_board()
        self._update_undo_redo()

    def _fit_board(self) -> None:
        self._board_view.fit_board()

    def _fit_area(self) -> None:
        self._board_view.fit_area()

    def _fit_selection(self) -> None:
        self._board_view.fit_selection()

    def _tool_cut(self) -> None:
        self._board_view.set_tool(CutTool())
        self._mode_label.setText("Cut Strip — click near a strip")

    def _tool_simplify(self) -> None:
        self._board_view.set_tool(SimplifyTool())
        self._mode_label.setText(
            "Simplify Wire — LPM: select (as Select), Shift+LPM on wire: apply simplify"
        )

    # ------------------------------------------------------------------
    # File actions
    # ------------------------------------------------------------------

    def _save(self) -> None:
        if self._session.project_path is None:
            self._save_as()
            return
        self._do_save(self._session.project_path)

    def _save_as(self) -> None:
        path_str = QFileDialog.getExistingDirectory(self, "Save project folder")
        if not path_str:
            return
        path = Path(path_str)
        self._do_save(path)
        # Re-anchor the command recorder to the new project location.
        self._session.stop_recording()
        self._session.project_path = path
        self._session.start_recording()
        self._update_title()

    def _do_save(self, path: Path) -> None:
        try:
            save_project(
                directory=path,
                board=self._session.board,
                layout=self._session.layout,
                schematic=self._session.schematic,
                instances=self._session.instances,
                routing_settings=self._session.routing_settings,
                layer_overrides=self._session.layer_overrides,
                axis_label_settings=self._session.axis_label_settings,
            )
            recent_projects.add(path)
        except Exception as exc:
            QMessageBox.critical(self, "Save error", str(exc))
            return

        self._session.mark_clean()
        commit_status = self._commit_save(path)
        self._status.showMessage(f"Saved to {path}{commit_status}", 4000)

    def _commit_save(self, path: Path) -> str:
        """Commit the session log and the saved project as two separate commits.

        The session log (``session_commands.yaml``) is committed first so
        that the history reads as "commands performed → snapshot reached".
        The recorder is then reset to a fresh empty log, which is captured
        by the second commit alongside the rest of the project files.
        Git failures are surfaced as a status suffix but do not roll back
        the save.
        """
        log_filename = self._session.DEFAULT_RECORDING_FILENAME
        n_cmds = len(self._session.recorder.events)
        suffix_parts: list[str] = []

        # Commit 1 — session log only (skipped if the log file isn't present
        # or hasn't changed since the last commit).
        try:
            log_paths: list[str] = (
                [log_filename] if (path / log_filename).exists() else []
            )
            cmd_word = "command" if n_cmds == 1 else "commands"
            log_msg = f"Session log: {n_cmds} {cmd_word}"
            log_result = git_repo.add_and_commit(path, log_msg, paths=log_paths)
        except git_repo.GitError as exc:
            _log.warning("project.commit_failed", phase="log", error=str(exc))
            return f" — git: {exc}"

        if log_result.created_repo:
            suffix_parts.append("git init")
        if log_result.commit_sha is not None:
            suffix_parts.append(f"log {log_result.commit_sha}")
            # Reset before the project commit so the second commit captures
            # the now-empty log file.
            self._session.reset_recording()

        # Commit 2 — everything else (project YAMLs + the reset log).
        try:
            project_msg = f"Save: {path.name}"
            proj_result = git_repo.add_and_commit(path, project_msg)
        except git_repo.GitError as exc:
            _log.warning("project.commit_failed", phase="project", error=str(exc))
            return " — git: " + str(exc) if not suffix_parts else (
                f" [{', '.join(suffix_parts)}] — git: {exc}"
            )

        if proj_result.commit_sha is not None:
            suffix_parts.append(f"save {proj_result.commit_sha}")

        return f" [{', '.join(suffix_parts)}]" if suffix_parts else ""

    def _check_unsaved_changes(self, action: str = "continuing") -> bool:
        """Return True if safe to proceed; prompts to save when dirty."""
        if not self._session.is_dirty:
            return True
        reply = QMessageBox.question(
            self,
            "Unsaved changes",
            f"The project has unsaved changes. Save before {action}?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Cancel:
            return False
        if reply == QMessageBox.StandardButton.Save:
            self._save()
            if self._session.is_dirty:
                return False
        return True

    def _close_project(self) -> None:
        """Return to the welcome screen."""
        if not self._check_unsaved_changes("closing"):
            return
        self._closing_to_welcome = True
        self.close()

    # ------------------------------------------------------------------
    # Other actions
    # ------------------------------------------------------------------

    def _undo(self) -> None:
        if self._session.stack.can_undo():
            self._session.stack.undo(self._session)
            self._session.invalidate_graph()
            self._session.mark_dirty()
            self._board_view.refresh()
            self._update_stats()
            self._update_undo_redo()

    def _redo(self) -> None:
        if self._session.stack.can_redo():
            self._session.stack.redo(self._session)
            self._session.invalidate_graph()
            self._session.mark_dirty()
            self._board_view.refresh()
            self._update_stats()
            self._update_undo_redo()

    def _update_undo_redo(self) -> None:
        """Sync Undo/Redo menu item enabled state and label.

        Undo/Redo is also force-disabled while autoroute is running so a
        Ctrl+Z mid-route cannot rewind state the worker still depends on.
        """
        stack = self._session.stack
        gate_open = not self._session.autoroute_active
        can_undo = stack.can_undo() and gate_open
        can_redo = stack.can_redo() and gate_open
        self._act_undo.setEnabled(can_undo)
        self._act_redo.setEnabled(can_redo)
        if stack.can_undo():
            desc = getattr(stack._undo[-1], "description", "")
            self._act_undo.setText(f"&Undo  {desc}" if desc else "&Undo")
        else:
            self._act_undo.setText("&Undo")
        if stack.can_redo():
            desc = getattr(stack._redo[-1], "description", "")
            self._act_redo.setText(f"&Redo  {desc}" if desc else "&Redo")
        else:
            self._act_redo.setText("&Redo")

    # ------------------------------------------------------------------
    # Autoroute UI lock
    # ------------------------------------------------------------------

    # QActions that mutate the layout/schematic snapshot the in-flight
    # autoroute worker depends on.  Disabled while autoroute is running;
    # everything else (view ops, layer toggles, save, preferences, net
    # color/name) stays interactive.  Undo/Redo gating is handled inside
    # _update_undo_redo so it also respects the live can_undo/can_redo
    # state once autoroute finishes.
    _AUTOROUTE_LOCK_ACTIONS = frozenset({
        KB.ACTION_DELETE,
        KB.ACTION_ROTATE_CW,
        KB.ACTION_ROTATE_CCW,
        KB.ACTION_FLIP,
        KB.ACTION_GROUP,
        KB.ACTION_UNGROUP,
        KB.ACTION_EXIT_GROUP,
        KB.ACTION_REDRAG_PINS,
        KB.ACTION_WIRE_SET_INSULATED,
        KB.ACTION_WIRE_SET_BARE,
        KB.ACTION_WIRE_MOVE_TO_TOP,
        KB.ACTION_WIRE_MOVE_TO_BOTTOM,
        KB.ACTION_WIRE_FLIP,
        KB.ACTION_WIRE_TOGGLE_INSULATED,
        KB.ACTION_WIRE_SIDE_TOP,
        KB.ACTION_WIRE_SIDE_BOTTOM,
        KB.ACTION_DISCONNECT_PIN,
        KB.ACTION_TOOL_ROUTE,
        KB.ACTION_TOOL_ROUTE_NET_AWARE,
        KB.ACTION_TOOL_CUT,
        KB.ACTION_AUTOROUTE,
        KB.ACTION_SIMPLIFY_WIRE,
        KB.ACTION_UNWEAVE_WIRES,
        KB.ACTION_RUN_DRC,
    })

    def _apply_autoroute_ui_lock(self, active: bool) -> None:
        """Enable/disable hard-mutate QActions when autoroute starts/ends.

        Updates the window title and status bar so the user has visible
        feedback that some inputs are throttled.  Undo/Redo state is
        refreshed through ``_update_undo_redo`` (it gates on the same
        flag).
        """
        enabled = not active
        for aid in self._AUTOROUTE_LOCK_ACTIONS:
            action = self._actions.get(aid)
            if action is not None:
                action.setEnabled(enabled)
        self._update_undo_redo()
        self._update_title()
        if active:
            self.statusBar().showMessage(
                "Autoroute running — only inspection and display actions are available", 0
            )
        else:
            self.statusBar().clearMessage()

    def _clear_highlight(self) -> None:
        """Clear all persistent highlight entries from the stack."""
        self._session.highlight_stack.clear()

    def _update_highlight_action(self) -> None:
        """Enable 'Clear Highlight' only when there are persistent entries."""
        self._act_clear_highlight.setEnabled(
            self._session.highlight_stack.has_persistent()
        )

    def _ensure_select_tool(self) -> SelectTool | None:
        """Switch to SelectTool if not already active and return it."""
        if not isinstance(self._board_view._active_tool, SelectTool):
            self._board_view.set_tool(self._select_tool)
        tool = self._board_view._active_tool
        return tool if isinstance(tool, SelectTool) else None

    def _get_select_tool(self) -> SelectTool | None:
        """Return current SelectTool without switching, or None."""
        tool = self._board_view._active_tool
        return tool if isinstance(tool, SelectTool) else None

    # ------------------------------------------------------------------
    # Edit actions — delete, rotate, flip, exit group
    # ------------------------------------------------------------------

    def _delete_selection(self) -> None:
        tool = self._get_select_tool()
        if tool is None:
            return
        from PySide6.QtGui import QKeyEvent
        from PySide6.QtCore import QEvent
        # Simulate Del key through SelectTool's handler.
        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Delete, Qt.KeyboardModifier.NoModifier)
        tool.on_key(event)

    def _rotate_cw(self) -> None:
        tool = self._get_select_tool()
        if tool is None:
            return
        if tool._selected_ids:
            tool._rotate_multi(delta=90)
        elif tool._selected_id is not None:
            tool._rotate_selected(delta=90)

    def _rotate_ccw(self) -> None:
        tool = self._get_select_tool()
        if tool is None:
            return
        if tool._selected_ids:
            tool._rotate_multi(delta=-90)
        elif tool._selected_id is not None:
            tool._rotate_selected(delta=-90)

    def _flip_selection(self) -> None:
        tool = self._get_select_tool()
        if tool is None:
            return
        if tool._selected_ids:
            tool._flip_multi()
        elif tool._selected_id is not None:
            tool._flip_selected()

    def _exit_group(self) -> None:
        tool = self._get_select_tool()
        if tool is None:
            return
        if tool._group_stack:
            tool.exit_group()

    # ------------------------------------------------------------------
    # Tools actions — re-drag, wire properties, disconnect pin
    # ------------------------------------------------------------------

    def _redrag_pins(self) -> None:
        tool = self._get_select_tool()
        if tool is None or tool._selected_id is None:
            return
        tool._start_redrag(tool._selected_id)

    def _get_selected_wire_ids(self) -> set[uuid.UUID]:
        """Collect effective wire selection (multi + primary single)."""
        tool = self._get_select_tool()
        if tool is None:
            return set()
        ids: set[uuid.UUID] = set(tool._selected_wire_ids)
        if tool._selected_wire_id is not None:
            ids.add(tool._selected_wire_id)
        return ids

    def _set_selected_wire_insulated(self, insulated: bool) -> None:
        from protoboard.domain.commands.base import Command, MacroCommand
        from protoboard.domain.commands.component_cmds import (
            ComponentState,
            SetWireInsulatedCommand,
        )
        from typing import cast

        wire_ids = self._get_selected_wire_ids()
        if not wire_ids:
            return
        cmds = [
            cast(Command[ComponentState], SetWireInsulatedCommand(wire_id=wid, insulated=insulated))
            for wid in wire_ids
        ]
        cmd: Command[ComponentState] = (
            cmds[0] if len(cmds) == 1
            else MacroCommand(cmds, description=f"Set {len(cmds)} wires {'insulated' if insulated else 'bare'}")
        )
        self._session.stack.execute(cmd, self._session)
        self._session.layout_changed.emit()

    def _move_selected_wires_to_side(self, side: str | None) -> None:
        """Move wires to a layer; ``side=None`` toggles (flip).

        If a fragment is being edited, operates on the fragment (via
        FlipWireFragmentCommand). Otherwise operates on all selected wires
        (toggle requires same current side).  Delegates to SelectTool which
        owns validation and status messages.
        """
        tool = self._get_select_tool()
        if tool is None:
            return
        if tool._is_editing_wire():
            tool._flip_wire_fragment(target_side=side)
        else:
            tool._move_selected_wires_to_side(side)

    def _disconnect_pin(self) -> None:
        """Disconnect pin — shows submenu if SelectTool has a component selected."""
        tool = self._get_select_tool()
        comp_id = tool._selected_id if tool is not None else None
        if comp_id is None:
            self._status.showMessage("Select a component first", 2000)
            return
        inst = self._session.instances.get(comp_id)
        defn = self._session.components.get(inst.definition_ref) if inst else None
        if inst is None or defn is None:
            return

        from protoboard.domain.commands.component_cmds import (
            ComponentState,
            DisconnectPinCommand,
        )
        from protoboard.domain.commands.base import Command
        from typing import cast

        # Build list of netted pins for this component.
        netted: list[tuple[int, str]] = []
        for net in self._session.schematic.nets.values():
            for pr in net.pins:
                if pr.component_id == comp_id:
                    netted.append((pr.pin_number, net.name))

        if not netted:
            self._status.showMessage("No netted pins on selected component", 2000)
            return

        if len(netted) == 1:
            pin_num, net_name = netted[0]
        else:
            from PySide6.QtWidgets import QInputDialog
            items = [f"Pin {p} — {n}" for p, n in netted]
            item, ok = QInputDialog.getItem(
                self, "Disconnect Pin", "Choose pin to disconnect:", items, 0, False
            )
            if not ok:
                return
            idx = items.index(item)
            pin_num, net_name = netted[idx]

        cmd = cast(
            Command[ComponentState],
            DisconnectPinCommand(component_id=comp_id, pin_number=pin_num),
        )
        self._session.stack.execute(cmd, self._session)
        self._session.layout_changed.emit()
        self._update_undo_redo()

    def _select_all(self) -> None:
        """Select all components and wires."""
        tool = self._ensure_select_tool()
        if tool is None:
            return
        session = self._session
        tool._selected_ids = set(session.layout.placements.keys())
        tool._selected_wire_ids = {w.id for w in session.layout.wires}
        tool._redraw_multi_highlights()
        tool._redraw_multi_wire_highlights()

    def _deselect_all(self) -> None:
        """Deselect all components and wires."""
        tool = self._ensure_select_tool()
        if tool is None:
            return
        tool._clear_multi_selection()
        tool._deselect()
        tool._deselect_wire()

    def _select_all_components(self) -> None:
        """Select all components, keep wire selection unchanged."""
        tool = self._ensure_select_tool()
        if tool is None:
            return
        tool._selected_ids = set(self._session.layout.placements.keys())
        tool._redraw_multi_highlights()

    def _deselect_all_components(self) -> None:
        """Deselect all components, keep wire selection unchanged."""
        tool = self._ensure_select_tool()
        if tool is None:
            return
        tool._clear_multi_selection()
        tool._deselect()

    def _select_all_wires(self) -> None:
        """Select all wires, keep component selection unchanged."""
        tool = self._ensure_select_tool()
        if tool is None:
            return
        tool._selected_wire_ids = {w.id for w in self._session.layout.wires}
        tool._redraw_multi_wire_highlights()

    def _deselect_all_wires(self) -> None:
        """Deselect all wires, keep component selection unchanged."""
        tool = self._ensure_select_tool()
        if tool is None:
            return
        tool._deselect_wire()

    def _group_selection(self) -> None:
        """Group the current selection via Ctrl+G — delegates to SelectTool dialog."""
        from protoboard.gui.tools.select_tool import SelectTool
        tool = self._board_view._active_tool
        if not isinstance(tool, SelectTool):
            return
        # Require at least one component selected (single or multi).
        has_selection = bool(tool._selected_ids) or tool._selected_id is not None
        if not has_selection:
            self._status.showMessage("Select at least one component to group", 2000)
            return
        tool._group_for_selection()

    def _ungroup_selection(self) -> None:
        """Disband the group currently entered, or the group matching the selection."""
        from protoboard.domain.commands.group_cmds import DisbandGroupCommand
        from protoboard.gui.tools.select_tool import SelectTool

        tool = self._board_view._active_tool
        if not isinstance(tool, SelectTool):
            return
        # Priority: outline-selected group > navigated-into group > inferred from selection.
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        group_id: uuid.UUID | None = None
        if tool._selected_group_id is not None and tool._selected_group_id in self._session.layout.groups:
            # User clicked a group outline — disband that group.
            group_id = tool._selected_group_id
        elif len(tool._group_stack) > 1:
            # User double-clicked into a subgroup — disband the active subgroup.
            group_id = tool._current_group_id
        else:
            # Fall back: find a group that wholly contains the selected components.
            selected = tool._selected_ids
            for gid, grp in self._session.layout.groups.items():
                if gid == ROOT_GROUP_ID:
                    continue
                if grp.component_ids == frozenset(selected):
                    group_id = gid
                    break
        if group_id is None:
            self._status.showMessage("No group selected", 2000)
            return
        cmd = DisbandGroupCommand(group_id=group_id)
        self._session.stack.execute(cmd, self._session)
        # Exit the group if we were inside it (never pop root).
        if len(tool._group_stack) > 1 and tool._group_stack[-1] == group_id:
            tool._group_stack.pop()
        # Clear selected_group_id if it pointed at the disbanded group.
        if tool._selected_group_id == group_id:
            tool._selected_group_id = None
        self._session.mark_dirty()
        self._board_view.refresh()
        self._update_undo_redo()

    def _autoroute(self) -> None:
        """Open the autoroute dialog, then start the router in a background thread."""
        from protoboard.gui.dialogs.autoroute_dialog import AutorouteDialog
        from protoboard.gui.workers.autoroute_worker import AutorouteWorker
        from PySide6.QtWidgets import QProgressDialog

        if not self._session.schematic.nets:
            QMessageBox.information(self, "Autoroute", "No nets to route.")
            return

        # Collect nets to route: board selection → net inspector → all.
        from protoboard.gui.tools.select_tool import SelectTool
        from protoboard.domain.schematic.net import PinRef
        net_ids: set[uuid.UUID] | None = None
        tool = self._board_view._active_tool
        board_selected_ids: set[uuid.UUID] = set()
        if isinstance(tool, SelectTool):
            board_selected_ids = (tool._selected_ids or
                                  ({tool._selected_id} if tool._selected_id else set()))
        if board_selected_ids:
            # Collect nets for all pins of selected components.
            net_ids = set()
            for comp_id in board_selected_ids:
                inst = self._session.instances.get(comp_id)
                defn = self._session.components.get(inst.definition_ref) if inst else None
                if defn is None:
                    continue
                for pin_num in range(1, defn.footprint.pin_count() + 1):
                    ref = PinRef(component_id=comp_id, pin_number=pin_num)
                    net = self._session.schematic.net_for_pin(ref)
                    if net is not None:
                        net_ids.add(net.id)
            if not net_ids:
                net_ids = None  # selected components have no nets — fall through
        if net_ids is None:
            # Fall back to Net Inspector selection, then all nets.
            inspector_ids = self._net_inspector_panel.selected_net_ids()
            net_ids = inspector_ids if inspector_ids else None
        title_suffix = f" ({len(net_ids)} nets)" if net_ids else " (all nets)"

        # Show pre-flight dialog unless user opted to skip it.
        if self._session.routing_settings.skip_autoroute_dialog:
            run_settings = self._session.routing_settings
        else:
            cfg_dlg = AutorouteDialog(self._session.routing_settings, parent=self)
            if cfg_dlg.exec() != QDialog.DialogCode.Accepted:
                return
            run_settings = cfg_dlg.routing_settings()
            # ``run_settings.router_name`` carries the user's selection
            # (set inside ``routing_settings()``) — persisted with the
            # rest of the settings so the next dialog open restores it.
            self._session.routing_settings = run_settings
            self._session.mark_dirty()
        # The optimizer chain lives on RoutingSettings — single source of
        # truth for both UI and persistence.  None means "no plugins ever
        # registered", which preserves the legacy code path that lets
        # autoroute() run its embedded simplify pass.
        optimizer_names: list[str] | None = (
            list(run_settings.optimizer_chain)
            if run_settings.optimizer_chain
            else None
        )

        dlg = QProgressDialog("Initialising autorouter…", "Cancel", 0, 100, self)
        dlg.setWindowTitle(f"Autoroute{title_suffix}")
        # Non-modal: the main window stays interactive.  Hard-mutate
        # actions (place / route / undo / etc.) are gated by
        # ``session.autoroute_active`` below, but inspection, view ops,
        # and pure display tweaks (net colour, refdes, preferences)
        # remain available so the user isn't frozen for minutes.
        dlg.setWindowModality(Qt.WindowModality.NonModal)
        dlg.setWindowFlag(Qt.WindowType.Tool, True)
        dlg.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        dlg.setMinimumDuration(300)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setValue(0)

        worker = AutorouteWorker(
            board=self._session.board,
            layout=self._session.layout,
            schematic=self._session.schematic,
            components=self._session.components,
            instances=self._session.instances,
            routing_settings=run_settings,
            net_ids=net_ids,
            project_path=self._session.project_path,
            router_name=run_settings.router_name,
            optimizer_names=optimizer_names,
        )
        # Keep a reference so the worker isn't garbage-collected before finishing.
        self._autoroute_worker = worker

        def _on_progress(pass_num: int, net_name: str, pct: int) -> None:
            if dlg.wasCanceled():
                return
            label = f"Pass {pass_num + 1} — routing {net_name}…"
            dlg.setLabelText(label)
            dlg.setValue(pct)

        def _on_optimize_progress(done: int, total: int) -> None:
            if dlg.wasCanceled():
                return
            dlg.setLabelText(f"Optimising… ({done}/{total})")
            dlg.setValue(int(done * 100 / total) if total else 0)

        # IMPORTANT: connect the gate-release BEFORE ``_on_autoroute_done``
        # so it runs first.  Qt invokes ``finished`` slots in connection
        # order, and ``_on_autoroute_done`` issues a MacroCommand of
        # AddWireCommand — those are hard-mutate, gated by
        # ``session.autoroute_active``.  Without releasing first, the
        # CommandStack guard rejects our own apply with
        # ``CommandRejectedError``.
        def _release_gate(*_args: object) -> None:
            self._session.set_autoroute_active(False)
        worker.finished.connect(_release_gate)
        worker.error.connect(_release_gate)

        worker.progress.connect(_on_progress)
        worker.optimize_progress.connect(_on_optimize_progress)
        import time as _time
        _route_t0 = _time.monotonic()
        worker.finished.connect(
            lambda _: self._session.perf.record_route((_time.monotonic() - _route_t0) * 1000)
        )
        worker.finished.connect(self._on_autoroute_done)
        worker.finished.connect(dlg.close)
        worker.error.connect(
            lambda msg: QMessageBox.critical(self, "Autoroute error", msg)
        )
        worker.error.connect(dlg.close)
        dlg.canceled.connect(worker.cancel)

        # Gate hard-mutate UI before kicking off the worker so the user
        # cannot e.g. trigger a second autoroute or undo mid-launch.
        self._session.set_autoroute_active(True)
        worker.start()

    def _on_autoroute_done(self, result: object) -> None:
        """Apply autoroute result to the session (called on the main thread)."""
        from protoboard.services.routing.autorouter import AutorouteResult
        from protoboard.domain.commands.base import Command, MacroCommand
        from protoboard.domain.commands.component_cmds import AddWireCommand, ComponentState
        from typing import cast

        if not isinstance(result, AutorouteResult):
            return

        if not result.wires and not result.unrouted_nets:
            QMessageBox.information(
                self, "Autoroute", "Nothing to route (all nets already connected)."
            )
            return

        if result.wires:
            cmds = [cast(object, AddWireCommand(wire=w)) for w in result.wires]
            macro: MacroCommand[ComponentState] = MacroCommand(
                commands=[cast(Command[ComponentState], c) for c in cmds],
                description=f"Autoroute ({len(result.wires)} wires)",
            )
            self._session.stack.execute(macro, self._session)
            self._session.mark_dirty()
            self._board_view.refresh()
            self._update_undo_redo()
            self._session.layout_changed.emit()

        if not result.wires:
            # Cancelled — no message
            return

        msg = f"Autorouted {len(result.wires)} wire(s)."
        if result.unrouted_nets:
            msg += f"\nUnrouted nets: {', '.join(result.unrouted_nets)}"
        QMessageBox.information(self, "Autoroute", msg)

    def _run_drc(self) -> None:
        """Menu-action DRC run.

        Same async dispatch as ``_run_drc_silent`` plus a "DRC running…"
        status bar message that is replaced by the result when the
        worker finishes.  Never blocks the GUI thread.
        """
        self._dispatch_drc(status_msg="DRC running…")

    def _run_drc_silent(self) -> None:
        """Auto-DRC triggered by ``layout_changed`` — no status banner."""
        self._dispatch_drc(status_msg=None)

    def _dispatch_drc(self, status_msg: str | None) -> None:
        """Kick off (or supersede) a background DRC run.

        If a previous worker is still alive, its cancel flag is set
        so it bails out at the next checkpoint and its ``finished``
        signal is suppressed.  We do *not* ``wait()`` on it — that
        would block the GUI and defeat the whole purpose; Python
        keeps the QThread alive until ``run()`` returns naturally.
        """
        from protoboard.gui.workers.drc_worker import DRCWorker
        from protoboard.services.routing.snapshot import (
            LayoutSnapshot,
            SchematicSnapshot,
        )

        prev = self._drc_worker
        if prev is not None and prev.isRunning():  # type: ignore[attr-defined]
            prev.cancel()  # type: ignore[attr-defined]

        worker = DRCWorker(
            board=self._session.board,
            layout_snap=LayoutSnapshot.freeze(self._session.layout),
            schematic_snap=SchematicSnapshot.freeze(self._session.schematic),
            components=dict(self._session.components),
            instances=dict(self._session.instances),
            axis_labels=self._session.axis_label_settings,
        )
        worker.finished.connect(self._on_drc_finished)
        worker.error.connect(self._on_drc_error)
        self._drc_worker = worker
        if status_msg:
            self._status.showMessage(status_msg)
        worker.start()

    def _on_drc_finished(self, issues: list) -> None:
        """Apply a DRC result to the session — but only if the worker
        that emitted it is still the active one.  Superseded workers
        whose cancel flag is set typically don't fire this signal, but
        guard defensively in case the cancel arrived too late."""
        sender = self.sender()
        if sender is not self._drc_worker:
            return
        self._drc_worker = None
        self._session.set_drc_issues(issues)
        n = len(issues)
        self._status.showMessage(
            f"DRC: {n} issue{'s' if n != 1 else ''}", 4000,
        )
        self._update_stats()

    def _on_drc_error(self, msg: str) -> None:
        sender = self.sender()
        if sender is not self._drc_worker:
            return
        self._drc_worker = None
        _log.error("drc.worker_error", msg=msg)
        self._status.showMessage(f"DRC failed: {msg}", 4000)

    # ------------------------------------------------------------------
    # DRC interactivity — translate panel clicks into highlight + viewport.
    # ------------------------------------------------------------------

    _DRC_HIGHLIGHT_SOURCE = "drc_panel"

    def _on_drc_issue_clicked(self, issue: object) -> None:
        """Push (or pop) an auto highlight entry derived from the clicked
        DRC issue and centre the viewport on the first location.

        ``issue`` is either a :class:`DRCIssue` (first click) or ``None``
        (toggled off / list refreshed).  The highlight is pushed into the
        *auto* tier keyed by ``_DRC_HIGHLIGHT_SOURCE`` so subsequent
        clicks replace (not stack) the entry, and a tool's transient
        highlight still wins over it.
        """
        from protoboard.gui.highlight_state import HighlightEntry
        from protoboard.services.drc.engine import DRCIssue as _DRCIssue

        stack = self._session.highlight_stack
        stack.clear_auto(self._DRC_HIGHLIGHT_SOURCE)

        if not isinstance(issue, _DRCIssue):
            return

        comp_ids = frozenset(issue.affected_components)
        wire_ids = frozenset(issue.affected_wires)
        net_id = issue.affected_nets[0] if issue.affected_nets else None
        stack.push_auto(
            HighlightEntry(
                comp_ids=comp_ids,
                wire_ids=wire_ids,
                net_id=net_id,
                label=f"drc:{issue.code}",
            ),
            source_id=self._DRC_HIGHLIGHT_SOURCE,
        )

        if issue.locations:
            from PySide6.QtCore import QPointF
            p = issue.locations[0]
            self._board_view.center_on_scene_point(QPointF(p.x, p.y))

    def _update_stats(self) -> None:
        """Update the permanent status-bar stats label."""
        n_comp = len(self._session.layout.placements)
        n_nets = len(self._session.schematic.nets)
        n_issues = len(self._session.drc_issues)
        parts = [f"{n_comp} component{'s' if n_comp != 1 else ''}"]
        if n_nets:
            parts.append(f"{n_nets} net{'s' if n_nets != 1 else ''}")
        if n_issues:
            parts.append(f"⚠ {n_issues} DRC")
        self._stats_label.setText("  |  ".join(parts) + "   ")

    def _on_component_changed(
        self,
        comp_id: uuid.UUID,
        refdes: str,
        value: str,
        rotation: int,
        label_offset_x: float,
        label_offset_y: float,
    ) -> None:
        inst = self._session.instances.get(comp_id)
        pl = self._session.layout.placements.get(comp_id)
        if inst is None or pl is None:
            return
        self._session.instances[comp_id] = dataclasses.replace(
            inst, refdes=refdes, value=value
        )
        pl.rotation = rotation
        pl.label_offset = Point(label_offset_x, label_offset_y)
        self._session.mark_dirty()
        self._board_view.refresh()

    def _on_pin_offsets_changed(self, comp_id: uuid.UUID, offsets: object) -> None:
        pl = self._session.layout.placements.get(comp_id)
        if pl is None:
            return
        pl.pin_offsets = offsets  # type: ignore[assignment]
        self._session.mark_dirty()
        self._board_view.refresh()

    def _on_pin_variant_changed(self, comp_id: uuid.UUID, pin_variant: object) -> None:
        from typing import cast
        from protoboard.domain.commands.component_cmds import (
            ComponentState,
            SetPinVariantCommand,
        )
        variant_str = str(pin_variant) if pin_variant is not None else None
        cmd = SetPinVariantCommand(component_id=comp_id, new_variant=variant_str)
        self._session.stack.execute(cmd, cast(ComponentState, self._session))
        self._session.mark_dirty()
        self._board_view.refresh()

    def _export_svg(self) -> None:
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Export SVG", "board.svg", "SVG files (*.svg)"
        )
        if not path_str:
            return
        try:
            svg_str = export_svg(
                self._session.board,
                self._session.layout,
                components=self._session.components,
                instances=self._session.instances,
            )
            Path(path_str).write_text(svg_str, encoding="utf-8")
            self._status.showMessage(f"SVG exported to {path_str}", 4000)
        except Exception as exc:
            QMessageBox.critical(self, "Export error", str(exc))

    def _export_pdf(self) -> None:
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Export PDF (print-ready 1:1)", "board.pdf", "PDF files (*.pdf)"
        )
        if not path_str:
            return
        try:
            export_pdf(
                Path(path_str),
                self._session.board,
                self._session.layout,
                components=self._session.components,
                instances=self._session.instances,
                raster_pitch_mm=self._session.board.raster_pitch_mm,
            )
            self._status.showMessage(f"PDF exported to {path_str}", 4000)
        except Exception as exc:
            QMessageBox.critical(self, "Export error", str(exc))

    def _export_png(self) -> None:
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Export PNG", "board.png", "PNG files (*.png)"
        )
        if not path_str:
            return
        try:
            export_png(
                Path(path_str),
                self._session.board,
                self._session.layout,
                components=self._session.components,
                instances=self._session.instances,
            )
            self._status.showMessage(f"PNG exported to {path_str}", 4000)
        except Exception as exc:
            QMessageBox.critical(self, "Export error", str(exc))

    def _export_bom(self) -> None:
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Export BOM", "bom.csv", "CSV files (*.csv)"
        )
        if not path_str:
            return
        try:
            csv_str = export_bom_csv(
                self._session.layout,
                components=self._session.components,
                instances=self._session.instances,
            )
            Path(path_str).write_text(csv_str, encoding="utf-8")
            self._status.showMessage(f"BOM exported to {path_str}", 4000)
        except Exception as exc:
            QMessageBox.critical(self, "Export error", str(exc))

    def _import_kicad_net(self) -> None:
        import warnings
        from protoboard.services.kicad.footprint_map import FootprintMap

        path_str, _ = QFileDialog.getOpenFileName(
            self, "Import KiCAD Netlist", "", "Netlist files (*.net);;All files (*)"
        )
        if not path_str:
            return
        net_path = Path(path_str)
        try:
            imp = KiCadNetlistImporter()

            # Load existing footprint_map.yaml from project dir if available.
            user_map: FootprintMap | None = None
            if self._session.project_path:
                map_path = self._session.project_path / "footprint_map.yaml"
                if map_path.exists():
                    try:
                        user_map = FootprintMap.load(map_path)
                    except Exception:
                        pass

            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                result = imp.import_full(net_path, footprint_map=user_map)

            # Update session state.
            self._session.schematic = result.schematic
            # Merge imported instances: keep existing placed ones, add/update rest.
            placed_ids = set(self._session.layout.placements.keys())
            for refdes, inst in result.instances.items():
                if inst.id not in placed_ids:
                    self._session.instances[inst.id] = inst

            # Save footprint_map.yaml next to the project.
            if self._session.project_path:
                map_path = self._session.project_path / "footprint_map.yaml"
                result.footprint_map.save(map_path)

            self._session.layout_changed.emit()
            self._watch_net_file(net_path)

            # Status message.
            warn_count = sum(1 for w in caught if issubclass(w.category, ImportWarning))
            unplaced = sum(
                1 for iid in result.instances
                if iid not in self._session.layout.placements
            )
            msg = (
                f"Imported {len(result.schematic.nets)} nets, "
                f"{len(result.instances)} components "
                f"({unplaced} unplaced) from {net_path.name}"
            )
            if result.unresolved_fps:
                msg += f" — {len(result.unresolved_fps)} unknown footprints"
                if self._session.project_path:
                    msg += ", see footprint_map.yaml"
            if warn_count and not result.unresolved_fps:
                msg += f" ({warn_count} warnings)"
            self._status.showMessage(msg, 8000)

            if result.unresolved_fps and self._session.project_path:
                QMessageBox.information(
                    self,
                    "Unknown footprints",
                    f"{len(result.unresolved_fps)} footprint(s) could not be resolved "
                    f"automatically:\n\n"
                    + "\n".join(f"  • {fp}" for fp in result.unresolved_fps[:10])
                    + ("\n  …" if len(result.unresolved_fps) > 10 else "")
                    + f"\n\nA footprint_map.yaml has been written to:\n"
                    f"{self._session.project_path}\n\n"
                    "Fill in the missing definition_ref and pin_count fields, "
                    "then re-import the netlist.",
                )
        except Exception as exc:
            QMessageBox.critical(self, "Import error", str(exc))

    def _watch_net_file(self, net_path: Path) -> None:
        """Start watching *net_path* for external changes (Live KiCAD sync)."""
        if self._watched_net_path is not None:
            self._kicad_watcher.removePath(str(self._watched_net_path))
        self._watched_net_path = net_path
        self._kicad_watcher.addPath(str(net_path))

    def _on_kicad_net_changed(self, path: str) -> None:
        """Called by QFileSystemWatcher when the watched .net file is modified."""
        net_path = Path(path)
        # Re-add path: some editors replace the file (delete + create) which
        # removes it from the watcher.
        if not self._kicad_watcher.files():
            self._kicad_watcher.addPath(str(net_path))

        reply = QMessageBox.question(
            self,
            "KiCAD Netlist Changed",
            f"The netlist file changed on disk:\n{net_path.name}\n\nReload it now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._reload_kicad_net(net_path)

    def _reload_kicad_net(self, net_path: Path) -> None:
        """Re-import *net_path* without user interaction (used by live sync)."""
        import warnings
        from protoboard.services.kicad.footprint_map import FootprintMap

        try:
            imp = KiCadNetlistImporter()
            user_map: FootprintMap | None = None
            if self._session.project_path:
                map_path = self._session.project_path / "footprint_map.yaml"
                if map_path.exists():
                    try:
                        user_map = FootprintMap.load(map_path)
                    except Exception:
                        pass

            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                result = imp.import_full(net_path, footprint_map=user_map)

            self._session.schematic = result.schematic
            placed_ids = set(self._session.layout.placements.keys())
            for refdes, inst in result.instances.items():
                if inst.id not in placed_ids:
                    self._session.instances[inst.id] = inst

            if self._session.project_path:
                map_path = self._session.project_path / "footprint_map.yaml"
                result.footprint_map.save(map_path)

            self._session.layout_changed.emit()
            self._status.showMessage(
                f"Reloaded {net_path.name}: "
                f"{len(result.schematic.nets)} nets, "
                f"{len(result.instances)} components",
                6000,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Reload error", str(exc))

    def _export_kicad_net(self) -> None:
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Export KiCAD Netlist", "netlist.net", "Netlist files (*.net)"
        )
        if not path_str:
            return
        try:
            refdes_map = {
                inst.id: inst.refdes for inst in self._session.instances.values()
            }
            exp = KiCadNetlistExporter()
            exp.export_to_file(
                self._session.schematic, Path(path_str), refdes_map=refdes_map
            )
            self._status.showMessage(f"Netlist exported to {path_str}", 4000)
        except Exception as exc:
            QMessageBox.critical(self, "Export error", str(exc))

    def _generate_shortcut_config(self) -> None:
        """Write a full keybindings.yaml with all actions and their current shortcuts."""
        try:
            self._keybindings.generate_full_config()
            path = str(KB._KEYBINDINGS_PATH)
            self._status.showMessage(f"Shortcut config written to {path}", 6000)
            QMessageBox.information(
                self,
                "Shortcut Config",
                f"Shortcut configuration written to:\n{path}\n\n"
                "Edit the file and restart the application to apply changes.",
            )
        except Exception as exc:
            QMessageBox.critical(self, "Error", str(exc))

    def _open_preferences(self) -> None:
        from protoboard.gui.dialogs.preferences_dialog import PreferencesDialog
        dlg = PreferencesDialog(
            keybindings=self._keybindings,
            theme=self._theme,
            app_settings=self._app_settings,
            on_keybindings_applied=self._apply_all_keybindings,
            on_theme_applied=self._on_theme_applied,
            on_paths_applied=self._on_paths_applied,
            on_axis_labels_applied=self._on_axis_labels_applied,
            session=self._session,
            parent=self,
        )
        dlg.exec()

    def _on_axis_labels_applied(self) -> None:
        """Re-render after the user changes the axis-label format."""
        self._board_view.refresh()

    def _apply_all_keybindings(self) -> None:
        """Re-apply QAction shortcuts from the current KeyBindings table.

        Called by PreferencesDialog after the user edits any binding — every
        registered action refreshes its shortcut from disk-backed state, so
        changes take effect without an app restart.
        """
        for action_id, act in self._actions.items():
            act.setShortcut(self._ks(action_id))

    def _on_theme_applied(self, theme: object) -> None:
        from protoboard.gui.theme import Theme
        if isinstance(theme, Theme):
            self._theme = theme
            self._board_view.set_theme(theme)

    def _on_paths_applied(self, settings: object) -> None:
        from protoboard.gui.app_settings import AppSettings
        from protoboard.paths import BUILTIN_LIBRARY_DIR
        from protoboard.services.library.loader import load_library
        if not isinstance(settings, AppSettings):
            return
        self._app_settings = settings
        extra_dirs = [d for d in settings.component_library_dirs if d.exists()]
        library_dirs = ([BUILTIN_LIBRARY_DIR] if BUILTIN_LIBRARY_DIR.exists() else []) + extra_dirs
        new_library = load_library(library_dirs)
        self._session.library = new_library
        self._session.components = {
            name: new_library.get(name) for name in new_library.names()
        }
        self._library_panel.set_library(new_library)
        # Re-apply render throttles in case the user adjusted them in
        # PreferencesDialog → Performance.  Cheap (just QTimer.setInterval).
        self._cursor_timer.setInterval(settings.cursor_label_throttle_ms)
        self._board_view.apply_throttle_settings(
            rubber_band_ms=settings.rubber_band_throttle_ms,
            zoom_refresh_ms=settings.zoom_refresh_ms,
        )
        self._board_view.set_antialiasing(settings.antialiasing)
        self._board_view.refresh()

    def _on_cursor_moved(self, x: float, y: float) -> None:
        """Park the latest cursor pt; the throttle timer renders it.

        Calling ``setText`` directly on every event forces a status-bar
        repaint per event — over SSH that means hundreds of stale
        intermediate coordinates trickle in after the cursor stops.
        """
        self._pending_cursor_pt = (x, y)
        if not self._cursor_timer.isActive():
            self._cursor_timer.start()

    def _flush_cursor_label(self) -> None:
        pt = self._pending_cursor_pt
        self._pending_cursor_pt = None
        if pt is None:
            return
        self._cursor_label.setText(f"({pt[0]:.2f}, {pt[1]:.2f})")

    # ------------------------------------------------------------------
    # Window close
    # ------------------------------------------------------------------

    def showEvent(self, event: object) -> None:  # type: ignore[override]
        super().showEvent(event)  # type: ignore[arg-type]
        # The window-manager often delivers show() with a placeholder
        # geometry and then resizes the window to its final (maximized /
        # restored) size a frame or two later.  We arm a pending fit and
        # re-run fit_board on every subsequent resize, then settle once
        # the resize stream goes quiet — that way fitInView measures the
        # *final* viewport regardless of how many intermediate resizes the
        # WM produces.
        if not getattr(self, "_initial_fit_done", False):
            self._initial_fit_done = True
            self._initial_fit_pending = True
            self._initial_fit_timer = QTimer(self)
            self._initial_fit_timer.setSingleShot(True)
            self._initial_fit_timer.setInterval(250)
            self._initial_fit_timer.timeout.connect(self._finalise_initial_fit)
            self._board_view.fit_board()
            self._initial_fit_timer.start()

    def _finalise_initial_fit(self) -> None:
        """Run the last fit_board after the resize stream stops."""
        if not getattr(self, "_initial_fit_pending", False):
            return
        self._initial_fit_pending = False
        self._board_view.fit_board()

    def resizeEvent(self, event: "QResizeEvent") -> None:  # type: ignore[override]
        old = event.oldSize()
        new = event.size()
        if abs(new.width() - old.width()) >= 10 or abs(new.height() - old.height()) >= 10:
            import structlog
            _log = structlog.get_logger()
            bv = self._board_view
            _log.info(
                "main_window.resize",
                window=(new.width(), new.height()),
                board_view=(bv.width(), bv.height()),
                board_view_min=(bv.minimumWidth(), bv.minimumHeight()),
                hbar=(bv.horizontalScrollBar().minimum(),
                      bv.horizontalScrollBar().value(),
                      bv.horizontalScrollBar().maximum()),
                vbar=(bv.verticalScrollBar().minimum(),
                      bv.verticalScrollBar().value(),
                      bv.verticalScrollBar().maximum()),
                scene_rect=(round(bv.sceneRect().x(), 2), round(bv.sceneRect().y(), 2),
                            round(bv.sceneRect().width(), 2), round(bv.sceneRect().height(), 2)),
            )
        # While the WM is still pushing post-show resizes, refit on each one
        # and reset the quiet-period timer.  When the resize stream stops we
        # fit one more time from _finalise_initial_fit().
        if getattr(self, "_initial_fit_pending", False):
            self._board_view.fit_board()
            timer = getattr(self, "_initial_fit_timer", None)
            if timer is not None:
                timer.start()
        super().resizeEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._closing_to_welcome:
            self._closing_to_welcome = False
            save_window_state(self)
            self._session.stop_recording()
            super().closeEvent(event)
            self.return_to_welcome.emit()
            return
        # × button or Alt+F4 — prompt if dirty, then quit.
        if not self._check_unsaved_changes("quitting"):
            event.ignore()
            return
        save_window_state(self)
        self._session.stop_recording()
        super().closeEvent(event)
        from PySide6.QtWidgets import QApplication

        QApplication.quit()

    # ------------------------------------------------------------------
    # Keyboard shortcuts
    # ------------------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:
        # All shortcuts route through QActions registered in
        # _register_actions() with Qt.ShortcutContext.WindowShortcut — Qt
        # dispatches them automatically when the main window is active.
        super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # Selection axes — load from app_settings and persist on change.
    # ------------------------------------------------------------------

    def _apply_selection_settings_to_tool(self, tool: SelectTool) -> None:
        """Push filter+relation from app_settings into a freshly built SelectTool."""
        from protoboard.gui.selection_axes import (
            filter_from_tokens,
            relation_from_str,
        )
        tool.set_filter(filter_from_tokens(self._app_settings.selection_filter_tokens))
        tool.set_relation(relation_from_str(self._app_settings.selection_relation))

    def _on_filter_changed(self, new_filter: object) -> None:
        from protoboard.gui.app_settings import save_app_settings
        from protoboard.gui.selection_axes import SelectionFilter, filter_to_tokens
        if not isinstance(new_filter, SelectionFilter):
            return
        self._app_settings.selection_filter_tokens = filter_to_tokens(new_filter)
        save_app_settings(self._app_settings)

    def _on_relation_changed(self, new_relation: object) -> None:
        from protoboard.gui.app_settings import save_app_settings
        from protoboard.gui.selection_axes import RelationClass
        if not isinstance(new_relation, RelationClass):
            return
        self._app_settings.selection_relation = new_relation.value
        save_app_settings(self._app_settings)

    def _toggle_filter(self, which: str) -> None:
        """Toggle one bit of the SelectTool's selection filter from a shortcut.

        Pushes the new value back through SelectionFilterWidget so the
        status-bar buttons stay in sync and the change is persisted.
        """
        from protoboard.gui.selection_axes import SelectionFilter
        current = self._select_tool.filter
        flag = {
            "components": SelectionFilter.COMPONENTS,
            "wires": SelectionFilter.WIRES,
            "groups": SelectionFilter.GROUPS,
        }.get(which)
        if flag is None:
            return
        new = current ^ flag  # bit toggle
        # Apply to tool, update widget buttons, persist.
        self._select_tool.set_filter(new)
        self._sel_filter_widget.set_tool(self._select_tool)
        self._sel_filter_widget.filter_changed.emit(new)

    def _set_relation_by_name(self, value: str) -> None:
        """Set the SelectTool's relation class from a string identifier."""
        from protoboard.gui.selection_axes import relation_from_str
        relation = relation_from_str(value)
        self._select_tool.set_relation(relation)
        self._hl_mode_widget.set_tool(self._select_tool)
        self._hl_mode_widget.relation_changed.emit(relation)

    def _cycle_relation(self, *, forward: bool) -> None:
        """Cycle through RelationClass values forward or backward."""
        from protoboard.gui.selection_axes import RelationClass
        relations = list(RelationClass)
        idx = relations.index(self._select_tool.relation)
        step = 1 if forward else -1
        new = relations[(idx + step) % len(relations)]
        self._select_tool.set_relation(new)
        self._hl_mode_widget.set_tool(self._select_tool)
        self._hl_mode_widget.relation_changed.emit(new)

    def _simplify_selected_wires(self) -> None:
        """Run generic_simplify_wire on every currently-selected wire."""
        tool = self._select_tool
        wire_ids: set[uuid.UUID] = set(tool._selected_wire_ids)
        if tool._selected_wire_id is not None:
            wire_ids.add(tool._selected_wire_id)
        if not wire_ids:
            self._status.showMessage("Simplify: select a wire first.", 3000)
            return
        tool._simplify_wires(wire_ids)

    def _unweave_selected_wires(self) -> None:
        """Topologically un-weave mutual crossings of the selected wires."""
        tool = self._select_tool
        wire_ids: set[uuid.UUID] = set(tool._selected_wire_ids)
        if tool._selected_wire_id is not None:
            wire_ids.add(tool._selected_wire_id)
        if len(wire_ids) < 2:
            self._status.showMessage("Unweave: select at least 2 wires.", 3000)
            return
        tool._unweave_wires(wire_ids)
