# SPDX-License-Identifier: MIT
"""MainWindow — top-level application window."""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path

from protoboard.gui.app_settings import AppSettings
from protoboard.gui.window_state import load_window_state, save_window_state

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
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
from protoboard.gui.panels.properties_panel import PropertiesPanel
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.cut_tool import CutTool
from protoboard.gui.tools.place_tool import PlaceTool
from protoboard.gui.tools.route_tool import RouteTool
from protoboard.gui.tools.select_tool import SelectTool
from protoboard.services.export.bom import export_bom_csv
from protoboard.services.export.pdf import export_pdf
from protoboard.services.export.png import export_png
from protoboard.services.export.svg import export_svg
from protoboard.services.kicad.exporter import KiCadNetlistExporter
from protoboard.services.kicad.importer import ImportWarning, KiCadNetlistImporter
from protoboard.services.placement.auto_placer import auto_place
from protoboard.services.placement.spreader import spread_components
from protoboard.services.persistence.project_yaml import load_project, save_project


class MainWindow(QMainWindow):
    # Emitted when user chooses File > Close Project (return to welcome screen).
    return_to_welcome = Signal()

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
        from protoboard.gui.theme import load_theme, Theme as _Theme
        self._theme: _Theme = load_theme()
        self._update_title()
        self.resize(1280, 800)

        # Central widget — board view.
        self._board_view = BoardView(session, self)
        self._board_view.set_theme(self._theme)
        self._board_view.setMinimumSize(200, 200)
        self.setCentralWidget(self._board_view)
        # Single persistent SelectTool instance — preserves group navigation state.
        self._select_tool = SelectTool()
        self._board_view._permanent_tools.append(self._select_tool)

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
        self._build_menu()

        # Wire routing state (persisted across tool switches).
        self._wire_insulated: bool = True
        self._wire_side: str = "top"

        # Per-layer manual visibility overrides.
        from protoboard.gui.layer_state import LayerOverrides
        self._layer_overrides = LayerOverrides()

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

        # ── Toolbar: Tools ──────────────────────────────────────────────
        tb_tools = QToolBar("Tools", self)
        tb_tools.setMovable(False)
        tb_tools.setObjectName("tb_tools")
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb_tools)

        act_select = tb_tools.addAction("Select [S]")
        act_route  = tb_tools.addAction("Route Wire [W]")
        act_cut    = tb_tools.addAction("Cut Strip [C]")
        act_select.triggered.connect(self._tool_select)
        act_route.triggered.connect(self._tool_route)
        act_cut.triggered.connect(self._tool_cut)

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
        act_zoom_in_tb.triggered.connect(lambda: self._board_view.scale(1.3, 1.3))
        act_zoom_out_tb.triggered.connect(lambda: self._board_view.scale(1 / 1.3, 1 / 1.3))
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
        act_spread_tb    = tb_analysis.addAction("Spread")
        act_hug_tb       = tb_analysis.addAction("Hug")
        act_drc.triggered.connect(self._run_drc)
        act_autoroute_tb.triggered.connect(self._autoroute)
        act_spread_tb.triggered.connect(self._spread_components)
        act_hug_tb.triggered.connect(self._hug_components)

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
        from protoboard.gui.panels.selection_filter_widget import SelectionFilterWidget
        self._sel_filter_widget = SelectionFilterWidget(self)
        self._sel_filter_widget.set_tool(self._select_tool)
        self._status.addPermanentWidget(self._sel_filter_widget)

        # Auto-DRC timer — fires 600 ms after the last layout_changed.
        self._drc_timer = QTimer(self)
        self._drc_timer.setSingleShot(True)
        self._drc_timer.setInterval(600)
        self._drc_timer.timeout.connect(self._run_drc_silent)

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
        session.dirty_changed.connect(self._update_title)
        session.drc_changed.connect(self._props_panel.update_issues)
        self._props_panel.component_changed.connect(self._on_component_changed)
        self._props_panel.pin_offsets_changed.connect(self._on_pin_offsets_changed)
        self._props_panel.pin_variant_changed.connect(self._on_pin_variant_changed)

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

    def _build_menu(self) -> None:
        mb: QMenuBar = self.menuBar()

        # ── File ──────────────────────────────────────────────────────────
        file_menu = mb.addMenu("&File")

        act_save = file_menu.addAction("&Save")
        act_save.setShortcut(self._ks(KB.ACTION_SAVE))
        act_save.triggered.connect(self._save)

        act_save_as = file_menu.addAction("Save &As…")
        act_save_as.setShortcut(self._ks(KB.ACTION_SAVE_AS))
        act_save_as.triggered.connect(self._save_as)

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

        act_quit = file_menu.addAction("&Quit")
        act_quit.setShortcut(self._ks(KB.ACTION_QUIT))
        act_quit.triggered.connect(self.close)

        # ── Edit ──────────────────────────────────────────────────────────
        edit_menu = mb.addMenu("&Edit")

        self._act_undo: QAction = edit_menu.addAction("&Undo")
        self._act_undo.setShortcut(self._ks(KB.ACTION_UNDO))
        self._act_undo.setEnabled(False)
        self._act_undo.triggered.connect(self._undo)

        self._act_redo: QAction = edit_menu.addAction("&Redo")
        self._act_redo.setShortcut(self._ks(KB.ACTION_REDO))
        self._act_redo.setEnabled(False)
        self._act_redo.triggered.connect(self._redo)

        edit_menu.addSeparator()

        act_delete = edit_menu.addAction("&Delete")
        act_delete.setShortcut(self._ks(KB.ACTION_DELETE))
        act_delete.triggered.connect(self._delete_selection)

        edit_menu.addSeparator()

        act_rotate_cw = edit_menu.addAction("Rotate &CW")
        act_rotate_cw.setShortcut(self._ks(KB.ACTION_ROTATE_CW))
        act_rotate_cw.triggered.connect(self._rotate_cw)

        act_rotate_ccw = edit_menu.addAction("Rotate CC&W")
        act_rotate_ccw.setShortcut(self._ks(KB.ACTION_ROTATE_CCW))
        act_rotate_ccw.triggered.connect(self._rotate_ccw)

        act_flip = edit_menu.addAction("&Flip")
        act_flip.setShortcut(self._ks(KB.ACTION_FLIP))
        act_flip.triggered.connect(self._flip_selection)

        edit_menu.addSeparator()

        act_group = edit_menu.addAction("&Group Selection")
        act_group.setShortcut(self._ks(KB.ACTION_GROUP))
        act_group.triggered.connect(self._group_selection)

        act_ungroup = edit_menu.addAction("&Ungroup")
        act_ungroup.setShortcut(self._ks(KB.ACTION_UNGROUP))
        act_ungroup.triggered.connect(self._ungroup_selection)

        act_exit_group = edit_menu.addAction("&Exit Group")
        act_exit_group.setShortcut(self._ks(KB.ACTION_EXIT_GROUP))
        act_exit_group.triggered.connect(self._exit_group)

        edit_menu.addSeparator()

        act_prefs = edit_menu.addAction("&Preferences…")
        act_prefs.setShortcut(QKeySequence("Ctrl+,"))
        act_prefs.triggered.connect(self._open_preferences)

        edit_menu.addSeparator()

        generate_menu = edit_menu.addMenu("&Generate")
        act_gen_shortcuts = generate_menu.addAction("Shortcut Config &File")
        act_gen_shortcuts.triggered.connect(self._generate_shortcut_config)

        # ── Select ────────────────────────────────────────────────────────
        select_menu = mb.addMenu("&Select")

        act_sel_all = select_menu.addAction("Select &All")
        act_sel_all.setShortcut(self._ks(KB.ACTION_SELECT_ALL))
        act_sel_all.triggered.connect(self._select_all)

        act_desel_all = select_menu.addAction("&Deselect All")
        act_desel_all.setShortcut(self._ks(KB.ACTION_DESELECT_ALL))
        act_desel_all.triggered.connect(self._deselect_all)

        select_menu.addSeparator()

        act_sel_comps = select_menu.addAction("Select All &Components")
        act_sel_comps.setShortcut(self._ks(KB.ACTION_SELECT_ALL_COMPONENTS))
        act_sel_comps.triggered.connect(self._select_all_components)

        act_desel_comps = select_menu.addAction("Deselect All C&omponents")
        act_desel_comps.setShortcut(self._ks(KB.ACTION_DESELECT_ALL_COMPONENTS))
        act_desel_comps.triggered.connect(self._deselect_all_components)

        select_menu.addSeparator()

        act_sel_wires = select_menu.addAction("Select All &Wires")
        act_sel_wires.setShortcut(self._ks(KB.ACTION_SELECT_ALL_WIRES))
        act_sel_wires.triggered.connect(self._select_all_wires)

        act_desel_wires = select_menu.addAction("Deselect All Wi&res")
        act_desel_wires.setShortcut(self._ks(KB.ACTION_DESELECT_ALL_WIRES))
        act_desel_wires.triggered.connect(self._deselect_all_wires)

        # ── View ──────────────────────────────────────────────────────────
        view_menu = mb.addMenu("&View")

        # Camera side (which physical side is in front).
        self._act_camera_top: QAction = view_menu.addAction("Camera: &Top")
        self._act_camera_top.setCheckable(True)
        self._act_camera_top.setChecked(True)
        self._act_camera_top.triggered.connect(lambda: self._set_camera("top"))

        self._act_camera_bottom: QAction = view_menu.addAction("Camera: &Bottom")
        self._act_camera_bottom.setCheckable(True)
        self._act_camera_bottom.triggered.connect(lambda: self._set_camera("bottom"))

        self._act_camera_toggle: QAction = view_menu.addAction("Camera: To&ggle")
        self._act_camera_toggle.setShortcut(self._ks(KB.ACTION_CAMERA_TOGGLE))
        self._act_camera_toggle.triggered.connect(lambda: self._set_camera("toggle"))

        view_menu.addSeparator()

        # Visual transform (independent of camera).
        self._act_mirror: QAction = view_menu.addAction("Mirror &Horizontal")
        self._act_mirror.setCheckable(True)
        self._act_mirror.setShortcut(self._ks(KB.ACTION_MIRROR_H))
        self._act_mirror.triggered.connect(self._mirror_view)

        self._act_mirror_v: QAction = view_menu.addAction("Mirror &Vertical")
        self._act_mirror_v.setCheckable(True)
        self._act_mirror_v.setShortcut(self._ks(KB.ACTION_MIRROR_V))
        self._act_mirror_v.triggered.connect(self._mirror_view_v)

        view_menu.addSeparator()

        # ── Layers submenu ────────────────────────────────────────────
        layers_menu = view_menu.addMenu("&Layers")
        _LAYER_ENTRIES = [
            ("components_top",    "Components Top",    KB.ACTION_LAYER_1),
            ("components_bottom", "Components Bottom", KB.ACTION_LAYER_2),
            ("wires_top",         "Wires Top",         KB.ACTION_LAYER_3),
            ("wires_bottom",      "Wires Bottom",      KB.ACTION_LAYER_4),
            ("labels_top",        "Labels Top",        KB.ACTION_LAYER_5),
            ("labels_bottom",     "Labels Bottom",     KB.ACTION_LAYER_6),
            ("board",             "Board",             KB.ACTION_LAYER_7),
        ]
        self._layer_field_order: list[str] = []
        self._layer_actions: dict[str, QAction] = {}
        for field_name, display_name, action_id in _LAYER_ENTRIES:
            act = layers_menu.addAction(display_name)
            act.setShortcut(self._ks(action_id))
            act.triggered.connect(
                lambda _checked, fn=field_name: self._cycle_layer(fn)
            )
            self._layer_actions[field_name] = act
            self._layer_field_order.append(field_name)
        layers_menu.addSeparator()
        act_reset_layers = layers_menu.addAction("Reset All Layers")
        act_reset_layers.triggered.connect(self._reset_layers)

        view_menu.addSeparator()

        act_zoom_in = view_menu.addAction("Zoom &In")
        act_zoom_in.setShortcut(self._ks(KB.ACTION_ZOOM_IN))
        act_zoom_in.triggered.connect(lambda: self._board_view.scale(1.3, 1.3))

        act_zoom_out = view_menu.addAction("Zoom &Out")
        act_zoom_out.setShortcut(self._ks(KB.ACTION_ZOOM_OUT))
        act_zoom_out.triggered.connect(lambda: self._board_view.scale(1 / 1.3, 1 / 1.3))

        act_fit = view_menu.addAction("&Fit Board")
        act_fit.setShortcut(self._ks(KB.ACTION_FIT_BOARD))
        act_fit.triggered.connect(self._fit_board)

        act_fit_area = view_menu.addAction("Fit &Area")
        act_fit_area.setShortcut(self._ks(KB.ACTION_FIT_AREA))
        act_fit_area.triggered.connect(self._fit_area)

        act_fit_sel = view_menu.addAction("Fit &Selection")
        act_fit_sel.setShortcut(self._ks(KB.ACTION_FIT_SELECTION))
        act_fit_sel.triggered.connect(self._fit_selection)

        # ── Tools ─────────────────────────────────────────────────────────
        tools_menu = mb.addMenu("&Tools")

        act_tool_select = tools_menu.addAction("&Select")
        act_tool_select.setShortcut(self._ks(KB.ACTION_TOOL_SELECT))
        act_tool_select.triggered.connect(self._tool_select)

        act_tool_route = tools_menu.addAction("Route &Wire")
        act_tool_route.setShortcut(self._ks(KB.ACTION_TOOL_ROUTE))
        act_tool_route.triggered.connect(self._tool_route)

        wire_sub = tools_menu.addMenu("  Wire options")

        self._act_wire_insulated: QAction = wire_sub.addAction("&Insulated")
        # self._act_wire_insulated.setCheckable(True)
        # self._act_wire_insulated.setChecked(True)
        self._act_wire_insulated.setShortcut(self._ks(KB.ACTION_WIRE_TOGGLE_INSULATED))
        self._act_wire_insulated.triggered.connect(
            lambda checked: self._set_wire_insulated(checked)
        )

        wire_side_group_top: QAction = wire_sub.addAction("Side: &Top")
        # wire_side_group_top.setCheckable(True)
        # wire_side_group_top.setChecked(True)
        wire_side_group_top.setShortcut(self._ks(KB.ACTION_WIRE_SIDE_TOP))
        wire_side_group_top.triggered.connect(lambda: self._set_wire_side("top"))
        self._act_wire_top = wire_side_group_top

        wire_side_group_bot: QAction = wire_sub.addAction("Side: &Bottom")
        # wire_side_group_bot.setCheckable(True)
        wire_side_group_bot.setShortcut(self._ks(KB.ACTION_WIRE_SIDE_BOTTOM))
        wire_side_group_bot.triggered.connect(lambda: self._set_wire_side("bottom"))
        self._act_wire_bottom = wire_side_group_bot

        act_tool_cut = tools_menu.addAction("&Cut Strip")
        act_tool_cut.setShortcut(self._ks(KB.ACTION_TOOL_CUT))
        act_tool_cut.triggered.connect(self._tool_cut)

        tools_menu.addSeparator()

        act_redrag = tools_menu.addAction("Re-drag &Pins")
        act_redrag.setShortcut(self._ks(KB.ACTION_REDRAG_PINS))
        act_redrag.triggered.connect(self._redrag_pins)

        tools_menu.addSeparator()

        act_wire_set_ins = tools_menu.addAction("Wire: Set &Insulated")
        act_wire_set_ins.setShortcut(self._ks(KB.ACTION_WIRE_SET_INSULATED))
        act_wire_set_ins.triggered.connect(lambda: self._set_selected_wire_insulated(True))

        act_wire_set_bare = tools_menu.addAction("Wire: Set &Bare")
        act_wire_set_bare.setShortcut(self._ks(KB.ACTION_WIRE_SET_BARE))
        act_wire_set_bare.triggered.connect(lambda: self._set_selected_wire_insulated(False))

        act_wire_to_top = tools_menu.addAction("Wire: Move to &Top Layer")
        act_wire_to_top.setShortcut(self._ks(KB.ACTION_WIRE_MOVE_TO_TOP))
        act_wire_to_top.triggered.connect(lambda: self._move_selected_wires_to_side("top"))

        act_wire_to_bot = tools_menu.addAction("Wire: Move to &Bottom Layer")
        act_wire_to_bot.setShortcut(self._ks(KB.ACTION_WIRE_MOVE_TO_BOTTOM))
        act_wire_to_bot.triggered.connect(lambda: self._move_selected_wires_to_side("bottom"))

        tools_menu.addSeparator()

        act_disconnect = tools_menu.addAction("&Disconnect Pin from Net…")
        act_disconnect.setShortcut(self._ks(KB.ACTION_DISCONNECT_PIN))
        act_disconnect.triggered.connect(self._disconnect_pin)

        tools_menu.addSeparator()

        act_resize_board = tools_menu.addAction("&Resize Board…")
        act_resize_board.triggered.connect(self._resize_board)

        tools_menu.addSeparator()

        act_autoroute = tools_menu.addAction("&Autoroute All Nets…")
        act_autoroute.setShortcut(self._ks(KB.ACTION_AUTOROUTE))
        act_autoroute.triggered.connect(self._autoroute)

        act_spread = tools_menu.addAction("S&pread Components")
        act_spread.triggered.connect(self._spread_components)

        tools_menu.addSeparator()

        act_drc = tools_menu.addAction("Run &DRC")
        act_drc.setShortcut(self._ks(KB.ACTION_RUN_DRC))
        act_drc.triggered.connect(self._run_drc)

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
            board, layout, schematic, instances, routing_settings = load_project(path)
        except Exception as exc:
            QMessageBox.critical(self, "Open error", str(exc))
            return
        session = EditorSession(board=board, library=self._session.library)
        session.layout = layout
        session.schematic = schematic
        session.instances = instances
        session.routing_settings = routing_settings
        session.project_path = path
        recent_projects.add(path)
        # Reconnect signals from the old session.
        self._session.layout_changed.disconnect(self._session.invalidate_graph)
        self._session.layout_changed.disconnect(self._panels_timer.start)
        self._session.layout_changed.disconnect(self._drc_timer.start)
        self._session.layout_changed.disconnect(self._update_undo_redo)
        self._session.layout_changed.disconnect(self._session.mark_dirty)
        self._session.dirty_changed.disconnect(self._update_title)
        self._session.drc_changed.disconnect(self._props_panel.update_issues)
        self._session = session
        session.layout_changed.connect(session.invalidate_graph)
        session.layout_changed.connect(self._panels_timer.start)
        session.layout_changed.connect(self._drc_timer.start)
        session.layout_changed.connect(self._update_undo_redo)
        session.layout_changed.connect(session.mark_dirty)
        session.dirty_changed.connect(self._update_title)
        session.drc_changed.connect(self._props_panel.update_issues)
        # Reset select tool state (group stack etc.) for the new session.
        self._select_tool = SelectTool()
        self._board_view._permanent_tools = [self._select_tool]
        self._sel_filter_widget.set_tool(self._select_tool)
        session.selection_changed.connect(self._on_selection_changed)
        self._board_view.set_session(session)
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
        self._board_view._layer_overrides = self._layer_overrides
        self._refresh_layer_menu()
        self._layers_panel.sync()
        self._board_view.refresh()

    def _reset_layers(self) -> None:
        self._layer_overrides.reset()
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
            "components_top":    "Components Top",
            "components_bottom": "Components Bottom",
            "wires_top":         "Wires Top",
            "wires_bottom":      "Wires Bottom",
            "labels_top":        "Labels Top",
            "labels_bottom":     "Labels Bottom",
            "board":             "Board",
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
        self.setWindowTitle(f"{name}[*]")
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
        self._netlist_panel.sync_selection(comp_ids, wire_ids, net_id)
        self._components_panel.sync_selection(comp_ids, wire_ids, net_id)
        self._nets_editor_panel.sync_selection(comp_ids, wire_ids, net_id)
        self._net_inspector_panel.sync_selection(comp_ids, wire_ids, net_id)

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

    def _hug_components(self) -> None:
        """Pull selected components and their net-neighbours closer together."""
        from typing import cast

        from protoboard.domain.commands.base import Command, MacroCommand
        from protoboard.domain.commands.component_cmds import (
            ComponentState,
            MoveComponentCommand,
        )
        from protoboard.gui.workers.operation_worker import run_with_progress
        from protoboard.services.placement.hugger import hug_components

        tool = self._get_select_tool()
        selected: set[uuid.UUID] = set()
        if tool is not None:
            selected.update(tool._selected_ids)
            if tool._selected_id is not None:
                selected.add(tool._selected_id)

        if not selected:
            self._status.showMessage("Select components to hug first.", 3000)
            return

        # Snapshot session state for the worker thread.
        board = self._session.board
        layout = self._session.layout
        schematic = self._session.schematic
        components = self._session.components
        instances = self._session.instances
        selected_frozen = frozenset(selected)

        class _State:
            pass
        state = _State()
        state.board = board          # type: ignore[attr-defined]
        state.layout = layout        # type: ignore[attr-defined]
        state.schematic = schematic  # type: ignore[attr-defined]
        state.components = components  # type: ignore[attr-defined]
        state.instances = instances  # type: ignore[attr-defined]

        r = run_with_progress(
            parent=self,
            title="Hug",
            fn=lambda cb: hug_components(set(selected_frozen), state, progress_callback=cb),  # type: ignore[arg-type]
            label="Hugging components…",
            with_progress=True,
        )
        if r is None:
            return

        if not r.moved_ids:
            self._status.showMessage("Hug: nothing to move.", 2000)
            return

        cmds: list[Command[ComponentState]] = []
        for cid in r.moved_ids:
            old_pl = self._session.layout.placements.get(cid)
            new_pl = r.new_placements.get(cid)
            if old_pl is None or new_pl is None:
                continue
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

        if not cmds:
            self._status.showMessage("Hug: nothing to move.", 2000)
            return

        n = len(cmds)
        macro: Command[ComponentState] = (
            cmds[0]
            if n == 1
            else cast(
                Command[ComponentState],
                MacroCommand(
                    commands=cmds,
                    description=f"Hug {n} component{'s' if n != 1 else ''}",
                ),
            )
        )

        self._session.stack.execute(macro, self._session)
        self._session.layout_changed.emit()
        self._board_view.refresh()
        self._update_stats()
        self._update_undo_redo()

        msg = f"Hug: moved {n} component{'s' if n != 1 else ''}."
        n_unchanged = len(r.unchanged_ids)
        if n_unchanged:
            msg += f" {n_unchanged} could not move (no free slot or locked)."
        self._status.showMessage(msg, 4000)

    def _tool_route(self) -> None:
        self._board_view.set_tool(
            RouteTool(insulated=self._wire_insulated, side=self._wire_side)
        )
        self._update_route_status(self._wire_insulated, self._wire_side)

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

    def _update_route_status(self, insulated: bool, side: str) -> None:
        style = "Insulated" if insulated else "Bare"
        self._mode_label.setText(
            f"Route Wire  {style} [{side}]  [I] toggle  [T/B] side  [Enter] commit  [Esc] cancel"
        )

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
        self._session.project_path = path
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
            )
            recent_projects.add(path)
            self._session.mark_clean()
            self._status.showMessage(f"Saved to {path}", 4000)
        except Exception as exc:
            QMessageBox.critical(self, "Save error", str(exc))

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
            self._session.mark_dirty()
            self._board_view.refresh()
            self._update_stats()
            self._update_undo_redo()

    def _redo(self) -> None:
        if self._session.stack.can_redo():
            self._session.stack.redo(self._session)
            self._session.mark_dirty()
            self._board_view.refresh()
            self._update_stats()
            self._update_undo_redo()

    def _update_undo_redo(self) -> None:
        """Sync Undo/Redo menu item enabled state and label."""
        stack = self._session.stack
        can_undo = stack.can_undo()
        can_redo = stack.can_redo()
        self._act_undo.setEnabled(can_undo)
        self._act_redo.setEnabled(can_redo)
        if can_undo:
            desc = getattr(stack._undo[-1], "description", "")
            self._act_undo.setText(f"&Undo  {desc}" if desc else "&Undo")
        else:
            self._act_undo.setText("&Undo")
        if can_redo:
            desc = getattr(stack._redo[-1], "description", "")
            self._act_redo.setText(f"&Redo  {desc}" if desc else "&Redo")
        else:
            self._act_redo.setText("&Redo")

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

    def _move_selected_wires_to_side(self, side: str) -> None:
        from protoboard.domain.commands.base import Command, MacroCommand
        from protoboard.domain.commands.component_cmds import (
            ComponentState,
            SetWireSideCommand,
        )
        from typing import cast

        wire_ids = self._get_selected_wire_ids()
        if not wire_ids:
            return
        cmds = [
            cast(Command[ComponentState], SetWireSideCommand(wire_id=wid, side=side))
            for wid in wire_ids
        ]
        cmd: Command[ComponentState] = (
            cmds[0] if len(cmds) == 1
            else MacroCommand(cmds, description=f"Move {len(cmds)} wires to {side}")
        )
        self._session.stack.execute(cmd, self._session)
        self._session.layout_changed.emit()

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
        # If we're inside a group (navigated via double-click), disband that group.
        group_id: uuid.UUID | None = None
        if tool._group_stack:
            group_id = tool._group_stack[-1]
        else:
            # Otherwise find a group that wholly contains the selected components.
            selected = tool._selected_ids
            for gid, grp in self._session.layout.groups.items():
                if grp.component_ids == frozenset(selected):
                    group_id = gid
                    break
        if group_id is None:
            self._status.showMessage("No group selected", 2000)
            return
        cmd = DisbandGroupCommand(group_id=group_id)
        self._session.stack.execute(cmd, self._session)
        # Exit the group if we were inside it.
        if tool._group_stack and tool._group_stack[-1] == group_id:
            tool._group_stack.pop()
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

        # Show pre-flight dialog — user can adjust all parameters.
        cfg_dlg = AutorouteDialog(self._session.routing_settings, parent=self)
        if cfg_dlg.exec() != QDialog.DialogCode.Accepted:
            return

        run_settings = cfg_dlg.routing_settings()
        if cfg_dlg.save_as_default():
            self._session.routing_settings = run_settings

        dlg = QProgressDialog("Initialising autorouter…", "Cancel", 0, 100, self)
        dlg.setWindowTitle("Autoroute")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(300)
        dlg.setValue(0)

        worker = AutorouteWorker(
            board=self._session.board,
            layout=self._session.layout,
            schematic=self._session.schematic,
            components=self._session.components,
            instances=self._session.instances,
            routing_settings=run_settings,
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
            dlg.setValue(int(done * 100 / total))

        worker.progress.connect(_on_progress)
        worker.optimize_progress.connect(_on_optimize_progress)
        worker.finished.connect(self._on_autoroute_done)
        worker.finished.connect(dlg.close)
        worker.error.connect(
            lambda msg: QMessageBox.critical(self, "Autoroute error", msg)
        )
        worker.error.connect(dlg.close)
        dlg.canceled.connect(worker.cancel)

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

        if not result.wires:
            # Cancelled — no message
            return

        msg = f"Autorouted {len(result.wires)} wire(s)."
        if result.unrouted_nets:
            msg += f"\nUnrouted nets: {', '.join(result.unrouted_nets)}"
        QMessageBox.information(self, "Autoroute", msg)

    def _run_drc(self) -> None:
        issues = self._session.run_drc()
        n = len(issues)
        self._status.showMessage(f"DRC: {n} issue{'s' if n != 1 else ''}", 4000)
        self._update_stats()

    def _run_drc_silent(self) -> None:
        """Auto-DRC triggered by layout_changed — no status bar message."""
        self._session.run_drc()
        self._update_stats()

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
            session=self._session,
            parent=self,
        )
        dlg.exec()

    def _apply_all_keybindings(self) -> None:
        """Re-apply QAction shortcuts from the current KeyBindings table."""
        self._act_undo.setShortcut(self._ks(KB.ACTION_UNDO))
        self._act_redo.setShortcut(self._ks(KB.ACTION_REDO))

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
        self._board_view.refresh()

    def _on_cursor_moved(self, x: float, y: float) -> None:
        self._cursor_label.setText(f"({x:.2f}, {y:.2f})")

    # ------------------------------------------------------------------
    # Window close
    # ------------------------------------------------------------------

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
        super().resizeEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._closing_to_welcome:
            self._closing_to_welcome = False
            save_window_state(self)
            super().closeEvent(event)
            self.return_to_welcome.emit()
            return
        # × button or Alt+F4 — prompt if dirty, then quit.
        if not self._check_unsaved_changes("quitting"):
            event.ignore()
            return
        save_window_state(self)
        super().closeEvent(event)
        from PySide6.QtWidgets import QApplication

        QApplication.quit()

    # ------------------------------------------------------------------
    # Keyboard shortcuts
    # ------------------------------------------------------------------

    def _matches(self, event: QKeyEvent, action_id: str) -> bool:
        """Return True if *event* matches the shortcut assigned to *action_id*."""
        shortcut = self._keybindings.get(action_id)
        if not shortcut:
            return False
        return QKeySequence(event.keyCombination()) == QKeySequence(shortcut)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        # QActions with setShortcut() are handled automatically by Qt.
        # This method only handles actions that lack a dedicated QAction
        # (tool switching, mirror, camera) which are shown in menus as
        # hint text only, and layer cycling which is dynamically indexed.
        if self._matches(event, KB.ACTION_TOOL_SELECT):
            self._tool_select()
        elif self._matches(event, KB.ACTION_TOOL_ROUTE):
            self._tool_route()
        elif self._matches(event, KB.ACTION_TOOL_CUT):
            self._tool_cut()
        elif self._matches(event, KB.ACTION_MIRROR_H):
            self._mirror_view()
        elif self._matches(event, KB.ACTION_MIRROR_V):
            self._mirror_view_v()
        elif self._matches(event, KB.ACTION_CAMERA_TOGGLE):
            self._set_camera("toggle")
        elif self._matches(event, KB.ACTION_WIRE_TOGGLE_INSULATED):
            self._set_wire_insulated(not self._wire_insulated)
        elif self._matches(event, KB.ACTION_WIRE_SIDE_TOP):
            self._set_wire_side("top")
        elif self._matches(event, KB.ACTION_WIRE_SIDE_BOTTOM):
            self._set_wire_side("bottom")
        else:
            # Layer shortcuts: dynamically check ACTION_LAYER_1..7.
            _layer_actions = [
                KB.ACTION_LAYER_1, KB.ACTION_LAYER_2, KB.ACTION_LAYER_3,
                KB.ACTION_LAYER_4, KB.ACTION_LAYER_5, KB.ACTION_LAYER_6,
                KB.ACTION_LAYER_7,
            ]
            for idx, layer_action_id in enumerate(_layer_actions):
                if self._matches(event, layer_action_id):
                    if idx < len(self._layer_field_order):
                        self._cycle_layer(self._layer_field_order[idx])
                    return
            super().keyPressEvent(event)
