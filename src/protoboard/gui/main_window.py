# SPDX-License-Identifier: MIT
"""MainWindow — top-level application window."""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenuBar,
    QMessageBox,
    QStatusBar,
    QTabWidget,
    QToolBar,
)

from protoboard.geometry import Point
from protoboard.gui.board_view import BoardView
from protoboard.gui.panels.groups_panel import GroupsPanel
from protoboard.gui.panels.library_panel import LibraryPanel
from protoboard.gui.panels.netlist_panel import NetlistPanel
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
from protoboard.services.persistence.project_yaml import save_project


class MainWindow(QMainWindow):
    # Emitted when user chooses File > Close Project (return to welcome screen).
    return_to_welcome = Signal()

    def __init__(self, session: EditorSession) -> None:
        super().__init__()
        self._session = session
        self._closing_to_welcome = False
        self._update_title()
        self.resize(1280, 800)

        # Central widget — board view.
        self._board_view = BoardView(session, self)
        self.setCentralWidget(self._board_view)

        # Left dock — tabbed: Library | Netlist/BOM | Groups
        self._library_panel = LibraryPanel(session.library, self)
        self._netlist_panel = NetlistPanel(self)
        self._groups_panel = GroupsPanel(self)
        left_tabs = QTabWidget()
        left_tabs.addTab(self._library_panel, "Library")
        left_tabs.addTab(self._netlist_panel, "Netlist")
        left_tabs.addTab(self._groups_panel, "Groups")
        left_dock = QDockWidget("", self)
        left_dock.setWidget(left_tabs)
        left_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, left_dock)

        # Properties + DRC dock (right).
        self._props_panel = PropertiesPanel(self)
        props_dock = QDockWidget("Properties / DRC", self)
        props_dock.setWidget(self._props_panel)
        props_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, props_dock)

        # Menu bar — File menu.
        self._build_menu()

        # Toolbar.
        toolbar = QToolBar("Tools", self)
        toolbar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)

        act_select = toolbar.addAction("Select [S]")
        act_route = toolbar.addAction("Route Wire [W]")
        act_cut = toolbar.addAction("Cut Strip [C]")
        toolbar.addSeparator()
        act_drc = toolbar.addAction("Run DRC [F5]")
        toolbar.addSeparator()
        act_export_svg = toolbar.addAction("Export SVG")
        act_export_pdf = toolbar.addAction("Export PDF")
        act_export_bom = toolbar.addAction("Export BOM")
        act_import_net = toolbar.addAction("Import KiCAD .net")
        act_export_net = toolbar.addAction("Export KiCAD .net")

        act_select.triggered.connect(self._tool_select)
        act_route.triggered.connect(self._tool_route)
        act_cut.triggered.connect(self._tool_cut)
        act_drc.triggered.connect(self._run_drc)
        act_export_svg.triggered.connect(self._export_svg)
        act_export_pdf.triggered.connect(self._export_pdf)
        act_export_bom.triggered.connect(self._export_bom)
        act_import_net.triggered.connect(self._import_kicad_net)
        act_export_net.triggered.connect(self._export_kicad_net)

        # Status bar.
        self._status = QStatusBar(self)
        self.setStatusBar(self._status)
        self._mode_label = QLabel("Select")
        self._status.addWidget(self._mode_label)
        self._stats_label = QLabel("")
        self._status.addPermanentWidget(self._stats_label)
        self._cursor_label = QLabel("(—, —)")
        self._status.addPermanentWidget(self._cursor_label)

        # Auto-DRC timer — fires 600 ms after the last layout_changed.
        self._drc_timer = QTimer(self)
        self._drc_timer.setSingleShot(True)
        self._drc_timer.setInterval(600)
        self._drc_timer.timeout.connect(self._run_drc_silent)

        # Signals.
        self._board_view.cursor_moved.connect(self._on_cursor_moved)
        self._board_view.escape_tool.connect(self._tool_select)
        self._library_panel.component_selected.connect(self._tool_place)
        self._netlist_panel.instance_selected.connect(self._board_view.select_component)
        session.layout_changed.connect(self._refresh_netlist)
        session.layout_changed.connect(self._refresh_groups)
        session.layout_changed.connect(self._drc_timer.start)
        session.layout_changed.connect(self._update_undo_redo)
        session.drc_changed.connect(self._props_panel.update_issues)
        self._props_panel.component_changed.connect(self._on_component_changed)
        self._props_panel.pin_offsets_changed.connect(self._on_pin_offsets_changed)

        self._groups_panel.group_disbanded.connect(lambda _: self._board_view.refresh())
        self._groups_panel.group_disbanded.connect(lambda _: self._update_undo_redo())
        self._groups_panel.group_renamed.connect(lambda _: self._board_view.refresh())
        self._groups_panel.group_renamed.connect(lambda _: self._update_undo_redo())

        # Populate panels if project already loaded.
        self._refresh_netlist()
        self._refresh_groups()
        self._update_stats()

        # Default tool.
        self._tool_select()

    # ------------------------------------------------------------------
    # Menu
    # ------------------------------------------------------------------

    def _build_menu(self) -> None:
        mb: QMenuBar = self.menuBar()

        # ── File ──────────────────────────────────────────────────────────
        file_menu = mb.addMenu("&File")

        act_save = file_menu.addAction("&Save")
        act_save.setShortcut(QKeySequence.StandardKey.Save)
        act_save.triggered.connect(self._save)

        act_save_as = file_menu.addAction("Save &As…")
        act_save_as.setShortcut(QKeySequence.StandardKey.SaveAs)
        act_save_as.triggered.connect(self._save_as)

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
        act_quit.setShortcut(QKeySequence.StandardKey.Quit)
        act_quit.triggered.connect(self.close)

        # ── Edit ──────────────────────────────────────────────────────────
        edit_menu = mb.addMenu("&Edit")

        self._act_undo: QAction = edit_menu.addAction("&Undo")
        self._act_undo.setShortcut(QKeySequence.StandardKey.Undo)
        self._act_undo.setEnabled(False)
        self._act_undo.triggered.connect(self._undo)

        self._act_redo: QAction = edit_menu.addAction("&Redo")
        self._act_redo.setShortcut(QKeySequence.StandardKey.Redo)
        self._act_redo.setEnabled(False)
        self._act_redo.triggered.connect(self._redo)

        edit_menu.addSeparator()

        act_select_all = edit_menu.addAction("Select &All\tCtrl+A")
        act_select_all.setShortcut(QKeySequence.StandardKey.SelectAll)
        act_select_all.triggered.connect(self._select_all)

        edit_menu.addSeparator()

        act_group = edit_menu.addAction("&Group Selection\tCtrl+G")
        act_group.setShortcut(QKeySequence("Ctrl+G"))
        act_group.triggered.connect(self._group_selection)

        act_ungroup = edit_menu.addAction("&Ungroup")
        act_ungroup.triggered.connect(self._ungroup_selection)

        # ── Tools ─────────────────────────────────────────────────────────
        tools_menu = mb.addMenu("&Tools")

        act_tool_select = tools_menu.addAction("&Select\tS")
        act_tool_select.triggered.connect(self._tool_select)

        act_tool_route = tools_menu.addAction("Route &Wire\tW")
        act_tool_route.triggered.connect(self._tool_route)

        act_tool_cut = tools_menu.addAction("&Cut Strip\tC")
        act_tool_cut.triggered.connect(self._tool_cut)

        tools_menu.addSeparator()

        act_autoroute = tools_menu.addAction("&Autoroute All Nets…\tF6")
        act_autoroute.triggered.connect(self._autoroute)

        tools_menu.addSeparator()

        act_drc = tools_menu.addAction("Run &DRC\tF5")
        act_drc.triggered.connect(self._run_drc)

    # ------------------------------------------------------------------
    # Title helper
    # ------------------------------------------------------------------

    def _update_title(self) -> None:
        p = self._session.project_path
        if p is not None:
            self.setWindowTitle(f"Protoboard Designer — {p.name}")
        else:
            self.setWindowTitle("Protoboard Designer")

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def _refresh_netlist(self) -> None:
        self._netlist_panel.refresh(self._session)

    def _refresh_groups(self) -> None:
        self._groups_panel.refresh(self._session)

    # ------------------------------------------------------------------
    # Tool switches
    # ------------------------------------------------------------------

    def _tool_select(self) -> None:
        self._board_view.set_tool(SelectTool())
        self._mode_label.setText("Select")

    def _tool_place(self, definition_ref: str) -> None:
        self._board_view.set_tool(PlaceTool(definition_ref))
        self._mode_label.setText(f"Place {definition_ref}  [R] rotate")

    def _tool_route(self) -> None:
        self._board_view.set_tool(RouteTool(insulated=True))
        self._mode_label.setText("Route Wire  [Enter] commit  [Esc] cancel")

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
            )
            self._status.showMessage(f"Saved to {path}", 4000)
        except Exception as exc:
            QMessageBox.critical(self, "Save error", str(exc))

    def _close_project(self) -> None:
        """Return to the welcome screen."""
        reply = QMessageBox.question(
            self,
            "Close project",
            "Save before closing?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Cancel:
            return
        if reply == QMessageBox.StandardButton.Save:
            self._save()
        self._closing_to_welcome = True
        self.close()

    # ------------------------------------------------------------------
    # Other actions
    # ------------------------------------------------------------------

    def _undo(self) -> None:
        if self._session.stack.can_undo():
            self._session.stack.undo(self._session)
            self._board_view.refresh()
            self._update_stats()
            self._update_undo_redo()

    def _redo(self) -> None:
        if self._session.stack.can_redo():
            self._session.stack.redo(self._session)
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

    def _select_all(self) -> None:
        """Select all components and wires with rubber-band across entire board."""
        from protoboard.gui.tools.select_tool import SelectTool

        if not isinstance(self._board_view._active_tool, SelectTool):
            self._board_view.set_tool(SelectTool())
        tool = self._board_view._active_tool
        if not isinstance(tool, SelectTool):
            return
        session = self._session
        tool._selected_ids = set(session.layout.placements.keys())
        tool._selected_wire_ids = {w.id for w in session.layout.wires}
        tool._redraw_multi_highlights()
        tool._redraw_multi_wire_highlights()

    def _group_selection(self) -> None:
        """Group the current multi-selection via Ctrl+G."""
        from protoboard.domain.commands.group_cmds import CreateGroupCommand
        from protoboard.domain.layout.group import ComponentGroup
        from protoboard.gui.tools.select_tool import SelectTool

        tool = self._board_view._active_tool
        if not isinstance(tool, SelectTool):
            return
        selected = tool._selected_ids.copy()
        if len(selected) < 2:
            self._status.showMessage("Select 2+ components to group", 2000)
            return
        name, ok = QInputDialog.getText(
            self, "Group Name", "Group name:", text="Group"
        )
        if not ok or not name.strip():
            return
        group = ComponentGroup(
            id=uuid.uuid4(),
            name=name.strip(),
            component_ids=frozenset(selected),
            child_group_ids=frozenset(),
        )
        cmd = CreateGroupCommand(group=group)
        self._session.stack.execute(cmd, self._session)
        self._board_view.refresh()
        self._update_undo_redo()

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
        self._board_view.refresh()
        self._update_undo_redo()

    def _autoroute(self) -> None:
        """Run full-netlist autorouter and add resulting wires to the layout."""
        from protoboard.domain.commands.base import MacroCommand
        from protoboard.domain.commands.component_cmds import AddWireCommand
        from protoboard.services.routing.autorouter import autoroute

        if not self._session.schematic.nets:
            QMessageBox.information(self, "Autoroute", "No nets to route.")
            return

        result = autoroute(
            board=self._session.board,
            layout=self._session.layout,
            schematic=self._session.schematic,
            components=self._session.components,
            instances=self._session.instances,
        )

        if not result.wires and not result.unrouted_nets:
            QMessageBox.information(
                self, "Autoroute", "Nothing to route (all nets already connected)."
            )
            return

        if result.wires:
            from typing import cast
            cmds = [cast(object, AddWireCommand(wire=w)) for w in result.wires]
            from protoboard.domain.commands.base import Command
            from protoboard.domain.commands.component_cmds import ComponentState
            macro: MacroCommand[ComponentState] = MacroCommand(
                commands=[cast(Command[ComponentState], c) for c in cmds],
                description=f"Autoroute ({len(result.wires)} wires)",
            )
            self._session.stack.execute(macro, self._session)
            self._board_view.refresh()
            self._update_undo_redo()

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
        self._session.layout.placements[comp_id] = dataclasses.replace(
            pl, rotation=rotation, label_offset=Point(label_offset_x, label_offset_y)
        )
        self._board_view.refresh()

    def _on_pin_offsets_changed(self, comp_id: uuid.UUID, offsets: object) -> None:
        pl = self._session.layout.placements.get(comp_id)
        if pl is None:
            return
        self._session.layout.placements[comp_id] = dataclasses.replace(
            pl, pin_offsets=offsets  # type: ignore[arg-type]
        )
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

        path_str, _ = QFileDialog.getOpenFileName(
            self, "Import KiCAD Netlist", "", "Netlist files (*.net);;All files (*)"
        )
        if not path_str:
            return
        try:
            imp = KiCadNetlistImporter()
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                schematic = imp.import_(Path(path_str))
            self._session.schematic = schematic
            self._session.layout_changed.emit()
            self._netlist_panel.refresh(self._session)
            warn_count = sum(1 for w in caught if issubclass(w.category, ImportWarning))
            msg = f"Imported {len(schematic.nets)} nets from {Path(path_str).name}"
            if warn_count:
                msg += f" ({warn_count} unknown footprints)"
            self._status.showMessage(msg, 6000)
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

    def _on_cursor_moved(self, x: float, y: float) -> None:
        self._cursor_label.setText(f"({x:.2f}, {y:.2f})")

    # ------------------------------------------------------------------
    # Window close
    # ------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        super().closeEvent(event)
        if self._closing_to_welcome:
            self._closing_to_welcome = False
            self.return_to_welcome.emit()
        else:
            # × button or Alt+F4 — quit the whole application.
            from PySide6.QtWidgets import QApplication

            QApplication.quit()

    # ------------------------------------------------------------------
    # Keyboard shortcuts
    # ------------------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        if key == Qt.Key.Key_S and not ctrl:
            self._tool_select()
        elif key == Qt.Key.Key_W:
            self._tool_route()
        elif key == Qt.Key.Key_C and not ctrl:
            self._tool_cut()
        elif key == Qt.Key.Key_F5:
            self._run_drc()
        elif key == Qt.Key.Key_S and ctrl:
            self._save()
        elif key == Qt.Key.Key_Q and ctrl:
            self.close()
        elif key == Qt.Key.Key_Z and ctrl:
            self._undo()
        elif key == Qt.Key.Key_Y and ctrl:
            self._redo()
        else:
            super().keyPressEvent(event)
