# SPDX-License-Identifier: MIT
"""MainWindow — top-level application window."""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable
from pathlib import Path

import structlog

from protoboard.gui.app_settings import AppSettings
from protoboard.gui.window_state import load_window_state, save_window_state

from PySide6.QtCore import Qt, QTimer, QFileSystemWatcher, Signal
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QKeyEvent, QKeySequence
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
from protoboard.gui.action_builder import build_actions
from protoboard.gui.actions import ALL_ACTIONS
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


def _wire_signature(wire: "object") -> "tuple[object, ...]":
    """Return a hashable identity tuple for *wire* used by duplicate detection.

    Two wires are considered duplicates of the same *connection* when
    they share:

    * the unordered endpoint pair (``{a.id, b.id}``) — identity captured
      as ``a.id.int XOR b.id.int`` so direction A→B and B→A collapse
      naturally without an explicit canonical-sort step,
    * the side (top/bottom — a top-layer wire and a bottom-layer wire
      between the same pads are two physically distinct conductors,
      typically meeting at a via, not a duplicate),
    * the insulated flag (bare vs insulated for the same pin pair are
      again two distinct conductors).

    Wires without resolved knot refs fall back to a path-based
    signature: that catches identical-path duplicates from test
    fixtures or partial-migration paths where ``Wire.a/b`` are still
    None.  We rarely hit this branch in production because
    ``AddWireCommand.apply`` resolves both knot refs before commit.
    """
    a = getattr(wire, "a", None)
    b = getattr(wire, "b", None)
    side = getattr(wire, "side", None)
    insulated = getattr(wire, "insulated", None)
    if a is not None and b is not None:
        # XOR on .int makes the endpoint pair direction-insensitive
        # without a separate sort.  Two distinct wires get the same XOR
        # iff they have the same endpoint pair.
        endpoint_xor = a.id.int ^ b.id.int
        return ("knots", endpoint_xor, side, insulated)
    # Fallback for unresolved wires: keep the old path-based signature so
    # test fixtures and freshly-constructed wires still get a stable id.
    path = getattr(wire, "path", None) or []
    if not path:
        return ("path", side, insulated, ())
    rounded = tuple((round(p.x, 6), round(p.y, 6)) for p in path)
    reversed_rounded = tuple(reversed(rounded))
    canonical = (
        rounded if rounded <= reversed_rounded else reversed_rounded
    )
    return ("path", side, insulated, canonical)


class MainWindow(QMainWindow):
    # Emitted when user chooses File > Close Project (return to welcome screen).
    return_to_welcome = Signal()

    # ------------------------------------------------------------------
    # Action metadata moved to ``src/protoboard/gui/actions.py`` —
    # ``ALL_ACTIONS`` is the single source of truth.  This class still
    # owns slot methods; ``_register_actions`` calls ``build_actions``.
    # ------------------------------------------------------------------

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
        # Load the user's namespaced keybinding overrides (Stage 1) into the
        # view's KeyMap so tool shortcut customisation persists.  The legacy
        # flat ``_keybindings`` still drives the global QActions for now.
        self._board_view.keymap.load_overrides()
        self._board_view.set_theme(self._theme)
        self._board_view.setMinimumSize(200, 200)
        self.setCentralWidget(self._board_view)
        # Single persistent SelectTool instance — preserves group navigation state.
        self._select_tool = SelectTool()
        self._board_view._permanent_tools.append(self._select_tool)
        self._apply_selection_settings_to_tool(self._select_tool)

        # Left dock — tabbed: Library | Netlist/BOM | Groups | Nets | Components
        self._library_panel = LibraryPanel(session.library, self)
        # Wire the parametric body catalogue so the placement picker
        # can offer per-package variant overrides.
        self._library_panel.set_package_families(session.package_families)
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

        # Highlight toggle: snapshot of persistent entries when the
        # user hits the toggle to hide them, restored on next press.
        self._highlight_snapshot: list = []

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

        # ── Toolbars ─────────────────────────────────────────────────────
        # Layout is loaded from ~/.config/protoboard/toolbars.yaml — the
        # builder already realised every QAction onto its toolbar during
        # _register_actions().  Here we only attach the live widget
        # toolbars (Selection) that cannot live in the spec list.
        self._install_widget_toolbars()

        # Refresh checkable state so toolbar buttons match the current
        # wire route mode after toolbars have been populated.
        if (insulated_act := self._actions.get(KB.ACTION_WIRE_TOGGLE_INSULATED)) is not None:
            insulated_act.setChecked(self._wire_insulated)
        if (top_act := self._actions.get(KB.ACTION_WIRE_SIDE_TOP)) is not None:
            top_act.setChecked(self._wire_side == "top")
        if (bot_act := self._actions.get(KB.ACTION_WIRE_SIDE_BOTTOM)) is not None:
            bot_act.setChecked(self._wire_side == "bottom")

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
        self._board_view.set_ratsnest_mode(self._app_settings.ratsnest_mode)

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
        # Cancelled DRC workers we still need to keep alive until their
        # QThread.run() returns naturally — otherwise Python may GC them
        # mid-flight and Qt aborts with "QThread destroyed while running".
        self._drc_workers_draining: list[object] = []

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
        self._props_panel.package_variant_changed.connect(
            self._on_package_variant_changed,
        )
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
        ("pin_pads",          "Pin Pads",           KB.ACTION_LAYER_9),
        ("pin_modifiers",     "Pin Modifiers",      KB.ACTION_LAYER_10),
    ]

    def _register_actions(self) -> None:
        """Build QActions, menus and action groups via the declarative spec list.

        See :mod:`protoboard.gui.actions` for the ALL_ACTIONS list and
        :mod:`protoboard.gui.action_builder` for the builder.  Top-level
        menus are wired here too — the spec ``menu_path`` drives placement
        automatically.  Dynamic items (Recent files, Camera Top/Bottom)
        are still added by ``_build_menu`` after this method returns.
        """
        self._layer_field_order: list[str] = [
            field_name for field_name, _label, _aid in self._LAYER_ENTRIES
        ]

        # Toolbar layout lives in YAML — load once and pass to the
        # builder, which realises each group's QToolBar in declaration
        # order.  ``ensure_user_toolbars_file_exists`` copies the
        # shipped default file into the user's config dir on first run.
        from protoboard.gui.toolbar_config import (
            ensure_user_toolbars_file_exists, load_toolbar_config,
        )
        ensure_user_toolbars_file_exists()
        toolbar_layout = load_toolbar_config()

        self._registry = build_actions(
            self,
            ALL_ACTIONS,
            self._keybindings,
            slot_owners={"mw": self},
            toolbar_layout=toolbar_layout,
        )
        # Preserve the historical attribute names so the rest of the
        # codebase (PPM handlers, tests, _apply_all_keybindings, etc.)
        # keeps reading the same dict — these are live views, not copies.
        self._actions: dict[str, QAction] = self._registry.actions
        self._action_handlers: dict[str, Callable[[], None]] = self._registry.handlers

        # Force WindowShortcut on every QAction — builder leaves the
        # default (ShortcutContext is set on the QAction by Qt itself).
        for act in self._actions.values():
            act.setShortcutContext(Qt.ShortcutContext.WindowShortcut)

        # Derived view: layer field-name → QAction, for _refresh_layer_menu().
        self._layer_actions: dict[str, QAction] = {
            field_name: self._actions[layer_action_id]
            for field_name, _label, layer_action_id in self._LAYER_ENTRIES
        }

        # Stronger exclusion policy on the two declared groups — the
        # builder uses default Exclusive (1-and-only-1) which matches.
        # Re-asserted here for clarity and to keep the symbol stable for
        # callsites that reference ``self._tool_action_group``.
        self._tool_action_group: QActionGroup = self._registry.action_groups["tool"]
        self._tool_action_group.setExclusionPolicy(
            QActionGroup.ExclusionPolicy.Exclusive
        )
        self._wire_side_group: QActionGroup = self._registry.action_groups["wire_side"]
        self._wire_side_group.setExclusionPolicy(
            QActionGroup.ExclusionPolicy.Exclusive
        )

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
        """Splice the dynamic menu items (Recent files, Camera Top/Bottom)
        into the QMenus the builder created from ``ALL_ACTIONS``.

        Most menu wiring is data-driven now — see
        :data:`protoboard.gui.actions.ALL_ACTIONS`.  Only two things stay
        hand-built here:

        * ``File ► Open Recent`` — submenu populated from RecentProjects
          at runtime, not a static QAction.
        * ``View ► Camera ► Top / Bottom`` — exclusive radio pair whose
          checked state mirrors :attr:`_camera_bottom`; managed alongside
          the configurable ``ACTION_CAMERA_TOGGLE`` action.
        """
        file_menu = self._registry.menus[("File",)]
        view_menu = self._registry.menus[("View",)]

        # ── Recent files submenu ── insert between Save As… and the
        # Import/Export separator block.  ``insertMenu`` requires a
        # ``before`` QAction — pick the Import KiCAD entry as the anchor
        # so Recent lands right above it (matches the legacy layout).
        import_act = self._actions[KB.ACTION_IMPORT_KICAD_NET]
        self._recent_menu = QMenu("Open &Recent", self)
        file_menu.insertMenu(import_act, self._recent_menu)
        self._populate_recent_menu()

        # ── Camera Top/Bottom radio pair ── insert before the toggle
        # entry.  These QActions live outside the spec list because Qt
        # exclusion grouping for the camera pair pre-dates the registry
        # and ``ACTION_CAMERA_TOGGLE`` already provides the keyboard-
        # configurable side flip.
        camera_toggle_act = self._actions[KB.ACTION_CAMERA_TOGGLE]
        self._act_camera_top = QAction("Camera: &Top", self)
        self._act_camera_top.setCheckable(True)
        self._act_camera_top.setChecked(True)
        self._act_camera_top.triggered.connect(lambda: self._set_camera("top"))
        view_menu.insertAction(camera_toggle_act, self._act_camera_top)

        self._act_camera_bottom = QAction("Camera: &Bottom", self)
        self._act_camera_bottom.setCheckable(True)
        self._act_camera_bottom.triggered.connect(lambda: self._set_camera("bottom"))
        view_menu.insertAction(camera_toggle_act, self._act_camera_bottom)


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
            from protoboard.services.persistence.project_yaml import (
                set_migration_library,
            )
            set_migration_library(self._session.library)
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
        session.rebuild_all_pin_identities()
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
                parts.append(f"{n_pl} component placement(s)")
            if n_wp:
                parts.append(f"{n_wp} wire point(s)")
            self.statusBar().showMessage(
                "Snapped legacy offsets: " + " + ".join(parts)
                + " (save the project to persist)",
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
            "pin_pads":          "Pin Pads",
            "pin_modifiers":     "Pin Modifiers",
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

        # Mode label gets the SelectTool selection summary appended so
        # the status bar always shows the current primary + multi
        # counts.  Only applies when SelectTool is the active tool —
        # other tools (Route / Place / Cut) keep their own label.
        from protoboard.gui.tools.select_tool import SelectTool
        active = getattr(self._board_view, "_active_tool", None)
        if isinstance(active, SelectTool):
            self._mode_label.setText(f"Select | {active.selection_summary()}")
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
        # Net membership changed without a layout change, so layout_changed
        # (which normally invalidates the graph/PadIndex) did not fire. PadIndex
        # is the source of per-pad net colour + highlight, so invalidate it
        # explicitly — otherwise newly-assigned pads (and same-name siblings)
        # keep the stale, uncoloured state until the next layout edit.
        self._session.invalidate_graph()
        self._refresh_netlist()
        self._update_undo_redo()
        self._update_stats()
        self._board_view.refresh()
        self._drc_timer.start()

    # ------------------------------------------------------------------
    # Tool switches
    # ------------------------------------------------------------------

    def _sync_tool_check(self, active_action_id: str | None) -> None:
        """Reflect active tool in the tool QActionGroup checkmark.

        Pass *None* when a tool without an associated ACTION_TOOL_* is
        active (e.g. PlaceTool); the exclusive-optional group then shows
        nothing checked.
        """
        for aid in (
            KB.ACTION_TOOL_SELECT,
            KB.ACTION_TOOL_ROUTE,
            KB.ACTION_TOOL_CUT,
            KB.ACTION_TOOL_SIMPLIFY,
        ):
            self._actions[aid].setChecked(aid == active_action_id)

    def _tool_select(self) -> None:
        self._board_view.set_tool(self._select_tool)
        # Mode label gets a selection summary suffix while SelectTool
        # is active.  `_on_selection_changed` keeps it up-to-date for
        # every subsequent mutation; this initial paint covers the
        # case where the user enters Select with a non-empty selection
        # (e.g. after PlaceTool exits with the placed component still
        # primary).
        self._mode_label.setText(
            f"Select | {self._select_tool.selection_summary()}"
        )
        self._sync_tool_check(KB.ACTION_TOOL_SELECT)

    def _tool_place(
        self,
        definition_ref: str,
        pin_variant: object = None,
        package_variant: object = None,
    ) -> None:
        variant_str = str(pin_variant) if pin_variant is not None else None
        package_variant_str = (
            str(package_variant) if package_variant is not None else None
        )
        self._board_view.set_tool(PlaceTool(
            definition_ref,
            pin_variant=variant_str,
            package_variant=package_variant_str,
        ))
        label = definition_ref
        tags = []
        if variant_str:
            tags.append(variant_str)
        if package_variant_str:
            tags.append(package_variant_str)
        if tags:
            label = f"{definition_ref} [{', '.join(tags)}]"
        self._mode_label.setText(f"Place {label}  [R] rotate")
        # PlaceTool has no ACTION_TOOL_* — clear the radio.
        self._sync_tool_check(None)

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
        self._sync_tool_check(None)

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

    def _tool_route(self) -> None:
        tool = self._board_view._active_tool
        if isinstance(tool, RouteTool):
            # Already routing — keep current state.  RouteTool is
            # always net-aware; the only commit-time dialog is the
            # NetMergeDialog for two-different-nets merges.
            tool._notify_status()
        else:
            self._board_view.set_tool(
                RouteTool(insulated=self._wire_insulated, side=self._wire_side)
            )
        self._update_route_status(self._wire_insulated, self._wire_side)
        self._sync_tool_check(KB.ACTION_TOOL_ROUTE)

    def _set_wire_insulated(self, insulated: bool) -> None:
        self._wire_insulated = insulated
        # One QAction lives in both menu and toolbar — setChecked updates both.
        self._act_wire_insulated.setChecked(insulated)
        tool = self._board_view._active_tool
        if isinstance(tool, RouteTool):
            tool._insulated = insulated
            self._update_route_status(insulated, self._wire_side)

    def _set_wire_side(self, side: str) -> None:
        self._wire_side = side
        self._act_wire_top.setChecked(side == "top")
        self._act_wire_bottom.setChecked(side == "bottom")
        tool = self._board_view._active_tool
        if isinstance(tool, RouteTool):
            tool._side = side
            self._update_route_status(self._wire_insulated, side)

    def _toggle_wire_insulated(self) -> None:
        self._set_wire_insulated(not self._wire_insulated)

    def _wire_flip_action(self) -> None:
        self._move_selected_wires_to_side(None)

    def _zoom_in(self) -> None:
        self._board_view.zoom_by(1.3)

    def _zoom_out(self) -> None:
        self._board_view.zoom_by(1 / 1.3)

    def _install_widget_toolbars(self) -> None:
        """Attach the Selection toolbar which hosts live QWidgets
        (filter combobox, highlight mode dropdown).  These cannot ride
        the spec list because they are not QActions; they live in their
        own non-customisable toolbar so the user dialog never has to
        deal with widget-shaped toolbar contents."""
        from protoboard.gui.panels.selection_filter_widget import SelectionFilterWidget
        from protoboard.gui.panels.highlight_mode_widget import HighlightModeWidget

        tb_selection = QToolBar("Selection", self)
        tb_selection.setMovable(True)
        tb_selection.setFloatable(True)
        tb_selection.setObjectName("tb_selection")
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb_selection)

        self._sel_filter_widget = SelectionFilterWidget(self)
        self._sel_filter_widget.set_tool(self._select_tool)
        tb_selection.addWidget(self._sel_filter_widget)
        tb_selection.addSeparator()
        self._hl_mode_widget = HighlightModeWidget(self)
        self._hl_mode_widget.set_tool(self._select_tool)
        tb_selection.addWidget(self._hl_mode_widget)

    def _open_toolbar_customize(self) -> None:
        """Open the Customize Toolbars dialog.  On accept, reloads the
        toolbar layout from disk and rebuilds every QToolBar in place."""
        from protoboard.gui.dialogs.toolbar_customize_dialog import (
            ToolbarCustomizeDialog,
        )
        dlg = ToolbarCustomizeDialog(
            main_window=self,
            registry=self._registry,
            parent=self,
        )
        dlg.exec()
        if dlg.changed:
            self._reload_toolbar_layout()

    def _reload_toolbar_layout(self) -> None:
        """Re-read ``toolbars.yaml`` and rebuild every spec-driven toolbar
        in place.  Widget-shaped toolbars (Selection) are left alone —
        they are installed by ``_install_widget_toolbars``."""
        from protoboard.gui.toolbar_config import load_toolbar_config
        layout = load_toolbar_config()
        for group, toolbar in list(self._registry.toolbars.items()):
            toolbar.clear()
            for aid in layout.get(group, []):
                act = self._actions.get(aid)
                if act is not None:
                    toolbar.addAction(act)
        # If the YAML mentions a group whose toolbar didn't exist yet
        # (first time the user populates an empty group), create it.
        from protoboard.gui.action_builder import _ensure_toolbar
        for group, action_ids in layout.items():
            if group in self._registry.toolbars:
                continue
            if not action_ids:
                continue
            toolbar = _ensure_toolbar(self, group, self._registry)
            for aid in action_ids:
                act = self._actions.get(aid)
                if act is not None:
                    toolbar.addAction(act)

    def _convert_endpoint_to_wire_pin_action(self) -> None:
        """Promote the half-orphan endpoint of the singly-selected wire to a wire-pin.

        Context-sensitive: when no wire is selected (or multiple are),
        falls back to a status-bar hint pointing at the right-click flow.
        Endpoint side preference: ``b`` (last placed) when both endpoints
        are eligible, ``a`` otherwise.
        """
        sel = list(self._select_tool._selected_wire_ids)
        if self._select_tool._selected_wire_id is not None:
            sel.append(self._select_tool._selected_wire_id)
        sel = list(dict.fromkeys(sel))
        if len(sel) != 1:
            self.statusBar().showMessage(
                "Convert endpoint to wire-pin: select one wire (right-click its endpoint for context menu).",
                3500,
            )
            return
        from protoboard.domain.commands.component_cmds import (
            ConvertEndpointToWirePinCommand,
        )
        from protoboard.domain.layout.knot import JointKnot
        from typing import cast as _cast
        wire_id = sel[0]
        wire = self._session.layout.wires.get(wire_id)
        if wire is None:
            return
        eligible: str | None = None
        for side in ("b", "a"):
            ref = wire.b if side == "b" else wire.a
            if ref is None:
                continue
            knot = self._session.layout.knots.get(ref.id)
            if isinstance(knot, JointKnot) and knot.wire_ids == frozenset({wire.id}):
                if self._select_tool._endpoint_eligible_for_wire_pin(knot.position):
                    eligible = side
                    break
        if eligible is None:
            self.statusBar().showMessage(
                "Convert endpoint to wire-pin: no eligible half-orphan endpoint on a plated-through pad.",
                3500,
            )
            return
        wire_net_id = self._session.cached_wire_net_map().get(wire.id)
        from protoboard.domain.commands.base import Command, ComponentState
        cmd = _cast(
            "Command[ComponentState]",
            ConvertEndpointToWirePinCommand(
                wire_id=wire.id, endpoint=eligible, net_id=wire_net_id,
            ),
        )
        self._session.stack.execute(cmd, self._session)
        self._session.layout_changed.emit()

    def _convert_wire_pin_to_endpoint_action(self) -> None:
        """Demote every WirePinKnot owned by the selected wires back to a free endpoint."""
        sel = list(self._select_tool._selected_wire_ids)
        if self._select_tool._selected_wire_id is not None:
            sel.append(self._select_tool._selected_wire_id)
        sel = list(dict.fromkeys(sel))
        if not sel:
            self.statusBar().showMessage(
                "Convert wire-pin to endpoint: select one or more wires that own a wire-pin endpoint.",
                3500,
            )
            return
        from protoboard.domain.commands.component_cmds import (
            ConvertWirePinToEndpointCommand,
        )
        from protoboard.domain.layout.knot import WirePinKnot
        from typing import cast as _cast
        from protoboard.domain.commands.base import (
            Command,
            ComponentState,
            MacroCommand,
        )
        targets: list[uuid.UUID] = []
        for wire_id in sel:
            wire = self._session.layout.wires.get(wire_id)
            if wire is None:
                continue
            for ref in (wire.a, wire.b):
                if ref is None:
                    continue
                knot = self._session.layout.knots.get(ref.id)
                if isinstance(knot, WirePinKnot) and knot.id not in targets:
                    targets.append(knot.id)
        if not targets:
            self.statusBar().showMessage(
                "Convert wire-pin to endpoint: selected wires have no wire-pin endpoints.",
                3500,
            )
            return
        cmds = [
            _cast(
                "Command[ComponentState]",
                ConvertWirePinToEndpointCommand(wire_pin_id=wp_id),
            )
            for wp_id in targets
        ]
        if len(cmds) == 1:
            self._session.stack.execute(cmds[0], self._session)
        else:
            self._session.stack.execute(
                MacroCommand(cmds, description="Convert wire-pins to endpoints"),
                self._session,
            )
        self._session.layout_changed.emit()

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
            f"Route Wire {style} [{side}]  [I] toggle  [T/B] side  [Enter] commit  [Esc] cancel"
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
        transform = self._board_view.view_transform
        mh = transform in ("mirror_h", "mirror_hv")
        mv = transform in ("mirror_v", "mirror_hv")
        bottom = self._board_view.camera_side == "bottom"
        # Each axis QAction reflects its own flag — both can be checked
        # simultaneously when the user combines Mirror H + Mirror V.
        self._act_mirror.setChecked(mh)
        self._act_mirror_v.setChecked(mv)
        self._act_camera_top.setChecked(not bottom)
        self._act_camera_bottom.setChecked(bottom)
        self._update_view_label()

    def _update_view_label(self) -> None:
        camera = self._board_view.camera_side.capitalize()   # "Top" | "Bottom"
        transform = self._board_view.view_transform
        mirror_tag = {
            "normal":    "N",
            "mirror_h":  "H",
            "mirror_v":  "V",
            "mirror_hv": "HV",
        }.get(transform, "?")
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
        self._sync_tool_check(KB.ACTION_TOOL_CUT)

    def _tool_simplify(self) -> None:
        self._board_view.set_tool(SimplifyTool())
        self._mode_label.setText(
            "Simplify Wire — LPM: select (as Select), Shift+LPM on wire: apply simplify"
        )
        self._sync_tool_check(KB.ACTION_TOOL_SIMPLIFY)

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
        # save_project canonicalises wires (split/merge pad-collision
        # passes) and writes 4-6 YAML files; on big projects the I/O
        # alone is enough to freeze the GUI for hundreds of ms.  Run
        # it in a background worker with a busy-bar dialog so the
        # window stays responsive.
        from protoboard.gui.workers.operation_worker import run_with_progress

        board = self._session.board
        layout = self._session.layout
        schematic = self._session.schematic
        instances = self._session.instances
        routing_settings = self._session.routing_settings
        layer_overrides = self._session.layer_overrides
        axis_label_settings = self._session.axis_label_settings

        def _save_in_worker() -> object:
            save_project(
                directory=path,
                board=board,
                layout=layout,
                schematic=schematic,
                instances=instances,
                routing_settings=routing_settings,
                layer_overrides=layer_overrides,
                axis_label_settings=axis_label_settings,
            )
            return True

        try:
            ok = run_with_progress(
                parent=self,
                title="Save",
                fn=_save_in_worker,
                label=f"Saving to {path.name}…",
            )
        except Exception as exc:
            QMessageBox.critical(self, "Save error", str(exc))
            return
        if ok is None:
            return  # cancelled or worker error already logged
        recent_projects.add(path)

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
        KB.ACTION_TOOL_CUT,
        KB.ACTION_AUTOROUTE,
        KB.ACTION_SIMPLIFY_WIRE,
        KB.ACTION_UNWEAVE_WIRES,
        KB.ACTION_MERGE_WIRES,
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

    def _highlight_toggle(self) -> None:
        """Toggle highlight: hide if visible, restore last hidden if not.

        - If there's a persistent highlight on the stack, snapshot it
          and clear (so the user can momentarily see the board "raw").
        - If there isn't and we have a snapshot, restore it.
        - If neither — no-op.

        This makes the keybinding behave like a true on/off switch
        instead of the legacy alias-for-clear behaviour.
        """
        stack = self._session.highlight_stack
        if stack.has_persistent():
            # Snapshot every persistent entry, in stack order, so the
            # restore re-pushes them with the same ordering.
            self._highlight_snapshot = [
                e for e in stack._stack  # type: ignore[attr-defined]
                if not e.transient and not e.auto
            ]
            stack.clear()
        elif self._highlight_snapshot:
            for entry in self._highlight_snapshot:
                stack.push(entry)
            self._highlight_snapshot = []

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

    def _focus_nets_panel(self) -> None:
        """Switch the left dock to the Nets tab and give it focus.

        Same idiom as _focus_library_for_add — top-menu Net actions
        delegate to NetsEditorPanel slots which work on its currently
        selected net, so the panel needs to be visible first.
        """
        for child in self.findChildren(QTabWidget):
            for i in range(child.count()):
                if child.tabText(i) == "Nets":
                    child.setCurrentIndex(i)
                    self._nets_editor_panel.setFocus()
                    return

    def _net_new(self) -> None:
        self._focus_nets_panel()
        self._nets_editor_panel._on_add_net()

    def _net_rename(self) -> None:
        self._focus_nets_panel()
        self._nets_editor_panel._on_rename_net()

    def _net_set_color(self) -> None:
        self._focus_nets_panel()
        self._nets_editor_panel._on_set_color()

    def _net_clear_color(self) -> None:
        self._focus_nets_panel()
        self._nets_editor_panel._on_clear_color()

    def _net_toggle_global(self) -> None:
        self._focus_nets_panel()
        self._nets_editor_panel._on_toggle_global()

    def _net_delete(self) -> None:
        self._focus_nets_panel()
        self._nets_editor_panel._on_delete_net()

    def _pin_assign_net(self) -> None:
        self._focus_nets_panel()
        self._nets_editor_panel._on_assign_pin()

    def _pin_assign_net_for(self, comp_id: uuid.UUID, pin_num: int) -> None:
        """Assign a specific pin (clicked on the board) to a net.

        Routes the board-click pin into the Nets panel's parameterised assign
        dialog — the same path as the panel's own "Assign to net…", so there
        is one assign flow, not two.
        """
        self._focus_nets_panel()
        self._nets_editor_panel._show_assign_dialog(comp_id, pin_num)

    # ------------------------------------------------------------------
    # Group operations on the active (entered) group
    # ------------------------------------------------------------------

    def _active_group_or_warn(self) -> uuid.UUID | None:
        """Return the active group ID, or show a status message and
        return None when the user is at root."""
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        from protoboard.gui.tools.select_tool import SelectTool

        tool = self._board_view._active_tool
        if not isinstance(tool, SelectTool):
            self._status.showMessage(
                "Group action requires the Select tool.", 3000
            )
            return None
        gid = tool._current_group_id
        if gid == ROOT_GROUP_ID:
            self._status.showMessage(
                "Enter a group first (long-press its outline or double-click a member).",
                4000,
            )
            return None
        return gid

    def _group_rename(self) -> None:
        from PySide6.QtWidgets import QInputDialog
        from protoboard.domain.commands.group_cmds import RenameGroupCommand

        gid = self._active_group_or_warn()
        if gid is None:
            return
        group = self._session.layout.groups.get(gid)
        if group is None:
            return
        name, ok = QInputDialog.getText(
            self, "Rename group", "New name:", text=group.name
        )
        if not ok:
            return
        name = name.strip()
        if not name or name == group.name:
            return
        self._session.stack.execute(
            RenameGroupCommand(group_id=gid, new_name=name), self._session
        )
        self._session.layout_changed.emit()
        self._update_undo_redo()

    def _group_set_exported(self, new_exported: frozenset) -> None:  # type: ignore[type-arg]
        """Common back-end for the three Export menu entries."""
        from protoboard.domain.commands.group_cmds import SetExportedPinsCommand

        gid = self._active_group_or_warn()
        if gid is None:
            return
        group = self._session.layout.groups.get(gid)
        if group is None:
            return
        if new_exported == group.exported_pins:
            self._status.showMessage("No change to exported pins.", 2000)
            return
        self._session.stack.execute(
            SetExportedPinsCommand(group_id=gid, new_exported=new_exported),
            self._session,
        )
        self._session.layout_changed.emit()
        self._update_undo_redo()

    def _group_export_auto(self) -> None:
        gid = self._active_group_or_warn()
        if gid is None:
            return
        new_exported = self._session.group_tree().external_pins_for_group(
            gid, self._session.schematic
        )
        self._group_set_exported(new_exported)

    def _group_export_all(self) -> None:
        gid = self._active_group_or_warn()
        if gid is None:
            return
        # Encapsulation: "all" = pins of direct members + each direct
        # child's re-exportable interface.  Grandchild pins surface only
        # via explicit re-exports at each intermediate level.
        new_exported = self._session.group_tree().exposable_surface(
            gid, self._session.instances, self._session.components,
        )
        self._group_set_exported(new_exported)

    def _group_export_clear(self) -> None:
        self._group_set_exported(frozenset())

    def _group_autoroute(self) -> None:
        """Autoroute connections that live inside the active group.

        Any net (including externals and globals) is routed when it has
        ≥2 pins on components in the group's recursive component set —
        but only between those in-group pins.  External pins are hidden
        from the router on this pass via a schematic projection; they
        remain in the real schematic and can be routed later from a
        parent context.
        """
        if not self._session.schematic.nets:
            QMessageBox.information(self, "Autoroute", "No nets to route.")
            return
        gid = self._active_group_or_warn()
        if gid is None:
            return
        tree = self._session.group_tree()
        merged_ids = frozenset(tree.all_component_ids(gid))
        projection = tree.schematic_projection_for_group(
            merged_ids, self._session.schematic
        )
        if not projection.nets:
            self._status.showMessage(
                "Group has no nets with internal connectivity to route.",
                3000,
            )
            return
        self._autoroute_with_net_ids(
            net_ids=None,  # route every net in the projection
            schematic_override=projection,
            title_prefix="Autoroute group",
        )

    def _lock_toggle(self) -> None:
        """Toggle lock on every component in the current selection.

        If every component in the selection is already locked → unlock
        them all.  Otherwise lock the whole selection (idempotent for
        already-locked entries).
        """
        from protoboard.domain.commands.base import MacroCommand
        from protoboard.domain.commands.component_cmds import LockComponentCommand

        tool = self._get_select_tool()
        if tool is None:
            return
        targets: set[uuid.UUID] = set()
        if tool._selected_ids:
            targets.update(tool._selected_ids)
        if tool._selected_id is not None:
            targets.add(tool._selected_id)
        if not targets:
            self._status.showMessage("Nothing selected to lock.", 3000)
            return

        placements = self._session.layout.placements
        valid = [cid for cid in targets if cid in placements]
        if not valid:
            return

        all_locked = all(placements[cid].locked for cid in valid)
        new_locked = not all_locked
        cmds = [
            LockComponentCommand(component_id=cid, locked=new_locked)
            for cid in valid
            if placements[cid].locked != new_locked
        ]
        if not cmds:
            return
        macro = MacroCommand(cmds) if len(cmds) > 1 else cmds[0]
        self._session.stack.execute(macro, self._session)
        self._session.layout_changed.emit()
        self._update_undo_redo()
        verb = "Locked" if new_locked else "Unlocked"
        self._status.showMessage(f"{verb} {len(cmds)} component(s).", 3000)

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

    def _rotate_around_point(self) -> None:
        tool = self._get_select_tool()
        if tool is None:
            return
        tool._begin_rotate_around_point()

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
            _log.info("select_all.no_tool")
            return
        session = self._session
        tool._selected_ids = set(session.layout.placements.keys())
        tool._selected_wire_ids = {w.id for w in session.layout.wires}
        tool._redraw_multi_highlights()
        tool._redraw_multi_wire_highlights()
        _log.info(
            "select_all.applied",
            n_components=len(tool._selected_ids),
            n_wires=len(tool._selected_wire_ids),
            n_multi_highlight_items=len(tool._multi_highlights),
        )

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

    def _autoroute(self, dialog_mode: str = "") -> None:
        """Open the autoroute dialog, then start the router in a background thread.

        ``dialog_mode`` overrides the persisted ``skip_autoroute_dialog``
        setting for this single run (bound to the F6 modifier variants):

        * ``""`` — honour the setting (plain F6).
        * ``"force"`` — always show the pre-flight dialog (Alt+F6).
        * ``"skip"`` — never show it (Shift+F6).
        """
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
        self._autoroute_with_net_ids(net_ids, dialog_mode=dialog_mode)

    def _autoroute_force_dialog(self) -> None:
        """Autoroute, always showing the pre-flight dialog (Alt+F6)."""
        self._autoroute("force")

    def _autoroute_skip_dialog(self) -> None:
        """Autoroute, never showing the pre-flight dialog (Shift+F6)."""
        self._autoroute("skip")

    def _autoroute_with_net_ids(
        self,
        net_ids: set[uuid.UUID] | None,
        *,
        schematic_override: object | None = None,
        title_prefix: str = "Autoroute",
        dialog_mode: str = "",
    ) -> None:
        """Launch the autoroute worker over an explicit net set.

        ``net_ids = None`` routes every net in the schematic.  Used by
        ``_autoroute`` (which derives the set from selection / Net
        Inspector / fallback) and by ``_group_autoroute`` (which passes
        a per-group schematic projection so external nets get routed
        only between their in-group pins).
        """
        from protoboard.gui.dialogs.autoroute_dialog import AutorouteDialog
        from protoboard.gui.workers.autoroute_worker import AutorouteWorker
        from PySide6.QtWidgets import QProgressDialog

        title_suffix = f" ({len(net_ids)} nets)" if net_ids else " (all nets)"

        # Show pre-flight dialog unless user opted to skip it.  A
        # per-run ``dialog_mode`` (F6 modifier variants) overrides the
        # persisted setting in either direction.
        if dialog_mode == "force":
            show_dialog = True
        elif dialog_mode == "skip":
            show_dialog = False
        else:
            show_dialog = not self._session.routing_settings.skip_autoroute_dialog
        if not show_dialog:
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
        dlg.setWindowTitle(f"{title_prefix}{title_suffix}")
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
            schematic=schematic_override or self._session.schematic,  # type: ignore[arg-type]
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

        def _on_simplify_progress(
            iter_num: int, max_iter: int, n_simplified: int,
        ) -> None:
            if dlg.wasCanceled():
                return
            # Show iteration countdown + how many wires the current pass
            # rewrote.  ``n_simplified == 0`` means the loop converged and
            # is about to break on the next check.
            tail = (
                f" — {n_simplified} wires simplified"
                if n_simplified > 0
                else " — converged"
            )
            dlg.setLabelText(
                f"Simplifying… iter {iter_num}/{max_iter}{tail}"
            )
            dlg.setValue(int(iter_num * 100 / max_iter) if max_iter else 0)

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
        worker.simplify_progress.connect(_on_simplify_progress)
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
        from protoboard.domain.commands.base import ComponentState
        from protoboard.domain.commands.component_cmds import AddWireCommand
        from typing import cast

        if not isinstance(result, AutorouteResult):
            return

        if not result.wires and not result.unrouted_nets:
            QMessageBox.information(
                self, "Autoroute", "Nothing to route (all nets already connected)."
            )
            return

        if result.wires:
            # Filter out wires whose path duplicates an already-existing
            # wire in the layout.  Second-autoroute runs can fall into
            # this trap when the connectivity graph rebuild missed a
            # pre-existing wire (e.g. a stale wire.path left behind by
            # rotate/drag macros) — the router then re-routes the same
            # connection and emits a wire with the identical path.  The
            # duplicate has its own UUID, so AddWireCommand would happily
            # commit both wires on top of each other (visual ghost,
            # double-counted in connectivity, mid-segment T-junction
            # logic confused).
            existing_signatures = {
                _wire_signature(w) for w in self._session.layout.wires
            }
            kept: list[object] = []
            duplicates: list[dict[str, object]] = []
            for w in result.wires:
                sig = _wire_signature(w)
                if sig in existing_signatures:
                    duplicates.append({
                        "new_id": str(w.id)[:8],
                        "side": w.side,
                        "insulated": w.insulated,
                        "n_points": len(w.path),
                        "endpoints": (
                            (w.path[0].x, w.path[0].y),
                            (w.path[-1].x, w.path[-1].y),
                        ) if w.path else None,
                    })
                    continue
                # Record the new signature so any sibling autoroute wires
                # in this same result that *also* duplicate it are also
                # filtered (defensive — happens when two parallel A* runs
                # produce the same path for different nets that share
                # padding).
                existing_signatures.add(sig)
                kept.append(w)
            if duplicates:
                _log.warning(
                    "autoroute.duplicate_wires_discarded",
                    n_discarded=len(duplicates),
                    n_kept=len(kept),
                    n_total=len(result.wires),
                    duplicates=duplicates[:10],
                )
            if not kept:
                _log.info("autoroute.all_results_were_duplicates")
                QMessageBox.information(
                    self, "Autoroute",
                    "Nothing new to route — all results duplicate existing "
                    "wires (probably a stale connectivity state).",
                )
                return
            cmds = [cast(object, AddWireCommand(wire=w)) for w in kept]
            macro: MacroCommand[ComponentState] = MacroCommand(
                commands=[cast(Command[ComponentState], c) for c in cmds],
                description=f"Autoroute ({len(kept)} wires)",
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
            # Keep a strong ref to the cancelled worker until its QThread
            # run() returns naturally — otherwise Python may GC it while
            # the C++ QThread is still finishing, and Qt aborts.  Worker
            # emits ``exited`` from run()'s finally block.
            self._park_for_drain(prev)

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
        # Don't drop the ref yet — finished is emitted from run() BEFORE
        # the QThread has fully unwound.  Releasing here would let Python
        # GC the QThread mid-flight and Qt aborts.  Hand the worker over
        # to the draining list; ``exited`` (from run()'s finally) calls
        # _drain_drc_worker, which releases the strong ref safely.
        self._park_for_drain(sender)
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
        self._park_for_drain(sender)
        self._drc_worker = None
        _log.error("drc.worker_error", msg=msg)
        self._status.showMessage(f"DRC failed: {msg}", 4000)

    def _park_for_drain(self, worker: object) -> None:
        """Move *worker* to the draining list and wire ``exited`` cleanup.

        Used both by the cancel path and the normal finished/error
        callbacks — in both cases the QThread.run() is still about to
        return naturally, and we must hold a strong ref until ``exited``
        fires from run()'s finally block.
        """
        if worker is None or worker in self._drc_workers_draining:
            return
        self._drc_workers_draining.append(worker)
        worker.exited.connect(  # type: ignore[attr-defined]
            lambda *_a, w=worker: self._drain_drc_worker(w)
        )

    def _drain_drc_worker(self, worker: object) -> None:
        """Release the strong ref on a cancelled DRC worker.

        Connected to ``DRCWorker.exited`` — fires once the QThread's
        ``run()`` has provably returned.  Removing the worker from
        ``_drc_workers_draining`` lets Python GC it safely.
        """
        try:
            self._drc_workers_draining.remove(worker)
        except ValueError:
            pass  # already removed

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

    def _on_package_variant_changed(
        self, comp_id: uuid.UUID, package_variant: object,
    ) -> None:
        from typing import cast
        from protoboard.domain.commands.component_cmds import (
            ComponentState,
            SetComponentPackageVariantCommand,
        )
        variant_str = str(package_variant) if package_variant is not None else None
        cmd = SetComponentPackageVariantCommand(
            component_id=comp_id, new_variant=variant_str,
        )
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
                schematic=self._session.schematic,
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
                schematic=self._session.schematic,
                raster_pitch_mm=self._session.board.raster_pitch_mm,
            )
            self._status.showMessage(f"PDF exported to {path_str}", 4000)
        except Exception as exc:
            QMessageBox.critical(self, "Export error", str(exc))

    def _export_pdf_template(self) -> None:
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Export Template PDF (A4, 4 panels)",
            "board_template.pdf", "PDF files (*.pdf)",
        )
        if not path_str:
            return
        try:
            from protoboard.services.export.pdf_template import export_template_pdf
            from protoboard.services.export.snapshot import BoardSnapshot
            snapshot = BoardSnapshot.from_session(self._session, canonical=True)
            export_template_pdf(Path(path_str), snapshot)
            self._status.showMessage(
                f"Template PDF exported to {path_str}", 4000,
            )
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
                schematic=self._session.schematic,
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
            # Imported instances carry no pin identities yet — build them so
            # same-name pins share a net.
            self._session.rebuild_all_pin_identities()

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
        # Pass ``parent=None`` so the dialog opens as an independent
        # top-level window that the user can freely position anywhere
        # on the desktop without dragging the main window with it.
        # Modality is enforced explicitly via setWindowModality below.
        dlg = PreferencesDialog(
            keybindings=self._keybindings,
            theme=self._theme,
            app_settings=self._app_settings,
            on_keybindings_applied=self._apply_all_keybindings,
            on_theme_applied=self._on_theme_applied,
            on_paths_applied=self._on_paths_applied,
            on_axis_labels_applied=self._on_axis_labels_applied,
            session=self._session,
            parent=None,
        )
        from PySide6.QtCore import Qt as _Qt
        dlg.setWindowModality(_Qt.WindowModality.ApplicationModal)
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
        self._board_view.set_ratsnest_mode(settings.ratsnest_mode)
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

    def _shutdown_cleanup(self) -> None:
        """Run shutdown side-effects, isolating each so one failure can't
        abort the close.

        An exception escaping ``closeEvent`` makes Qt keep the window (and
        the process) alive — e.g. a recording flush hitting a deleted
        project directory used to leave the app un-quittable.  Each step is
        therefore best-effort.
        """
        for label, step in (
            ("save_window_state", lambda: save_window_state(self)),
            ("stop_recording", self._session.stop_recording),
            ("wait_for_drc_workers", self._wait_for_drc_workers),
        ):
            try:
                step()
            except Exception as exc:
                _log.warning("main_window.shutdown_step_failed", step=label, error=str(exc))

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._closing_to_welcome:
            self._closing_to_welcome = False
            self._shutdown_cleanup()
            super().closeEvent(event)
            self.return_to_welcome.emit()
            return
        # × button or Alt+F4 — prompt if dirty, then quit.
        if not self._check_unsaved_changes("quitting"):
            event.ignore()
            return
        self._shutdown_cleanup()
        super().closeEvent(event)
        from PySide6.QtWidgets import QApplication

        QApplication.quit()

    def _wait_for_drc_workers(self) -> None:
        """Block until every DRC worker's QThread.run() has returned.

        Called from closeEvent before super() — otherwise Qt may deallocate
        the QThread instances while ``run()`` is mid-flight ("QThread
        destroyed while thread is still running").  We cancel each worker
        first so the wait is quick.
        """
        workers = [self._drc_worker] + list(self._drc_workers_draining)
        for w in workers:
            if w is None:
                continue
            try:
                w.cancel()  # type: ignore[attr-defined]
            except Exception:
                pass
        for w in workers:
            if w is None:
                continue
            try:
                # 2 s ceiling — DRC is fast; if it hangs we'd rather drop
                # the wait than freeze the app on exit.
                w.wait(2000)  # type: ignore[attr-defined]
            except Exception:
                pass

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

    def _cycle_relation(self, forward: bool) -> None:
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
        # Prefer the active tool — SimplifyTool / RouteTool are SelectTool
        # subclasses with their own selection state, and the user's wire
        # picks live there, not in the permanent _select_tool fallback.
        tool = self._get_select_tool() or self._select_tool
        wire_ids: set[uuid.UUID] = set(tool._selected_wire_ids)
        if tool._selected_wire_id is not None:
            wire_ids.add(tool._selected_wire_id)
        if not wire_ids:
            self._status.showMessage("Simplify: select a wire first.", 3000)
            return
        tool._simplify_wires(wire_ids)

    def _unweave_selected_wires(self) -> None:
        """Topologically un-weave mutual crossings of the selected wires."""
        tool = self._get_select_tool() or self._select_tool
        wire_ids: set[uuid.UUID] = set(tool._selected_wire_ids)
        if tool._selected_wire_id is not None:
            wire_ids.add(tool._selected_wire_id)
        if len(wire_ids) < 2:
            self._status.showMessage("Unweave: select at least 2 wires.", 3000)
            return
        tool._unweave_wires(wire_ids)

    def _merge_wires_action(self) -> None:
        """User-triggered merge: fuse touching wires + collapse same-side fragments.

        Scope:
        * If any wires are selected — restrict to that set.
        * Otherwise — operate on the entire layout.

        Two-phase: first collapse adjacent wires sharing an off-pin
        2-degree JointKnot (``FuseTouchingWiresCommand``), then run the
        per-wire fragment coalescer.  The wire-merge phase can produce
        a multi-fragment Wire (cross-side via collapse), so it has to
        run before the fragment pass to give it a chance to act on the
        merged result.
        """
        tool = self._get_select_tool() or self._select_tool
        wire_ids: set[uuid.UUID] = set(tool._selected_wire_ids)
        if tool._selected_wire_id is not None:
            wire_ids.add(tool._selected_wire_id)
        scope = frozenset(wire_ids) if wire_ids else None
        tool._merge_wires_and_fragments(scope)
