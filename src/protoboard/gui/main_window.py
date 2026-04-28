# SPDX-License-Identifier: MIT
"""MainWindow — top-level application window."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QDockWidget,
    QLabel,
    QMainWindow,
    QStatusBar,
    QToolBar,
)

from protoboard.gui.board_view import BoardView
from protoboard.gui.panels.library_panel import LibraryPanel
from protoboard.gui.panels.properties_panel import DRCPanel
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.place_tool import PlaceTool
from protoboard.gui.tools.route_tool import RouteTool
from protoboard.gui.tools.select_tool import SelectTool


class MainWindow(QMainWindow):
    def __init__(self, session: EditorSession) -> None:
        super().__init__()
        self._session = session
        self.setWindowTitle("Protoboard Designer")
        self.resize(1200, 800)

        # Central widget — board view.
        self._board_view = BoardView(session, self)
        self.setCentralWidget(self._board_view)

        # Library dock (left).
        self._library_panel = LibraryPanel(session.library, self)
        lib_dock = QDockWidget("Library", self)
        lib_dock.setWidget(self._library_panel)
        lib_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, lib_dock)

        # DRC dock (right).
        self._drc_panel = DRCPanel(self)
        drc_dock = QDockWidget("DRC", self)
        drc_dock.setWidget(self._drc_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, drc_dock)

        # Toolbar.
        toolbar = QToolBar("Tools", self)
        toolbar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)

        act_select = toolbar.addAction("Select [S]")
        act_route = toolbar.addAction("Route Wire [W]")
        act_drc = toolbar.addAction("Run DRC [F5]")

        act_select.triggered.connect(self._tool_select)
        act_route.triggered.connect(self._tool_route)
        act_drc.triggered.connect(self._run_drc)

        # Status bar.
        self._status = QStatusBar(self)
        self.setStatusBar(self._status)
        self._cursor_label = QLabel("(—, —)")
        self._status.addPermanentWidget(self._cursor_label)
        self._mode_label = QLabel("Select")
        self._status.addWidget(self._mode_label)

        # Signals.
        self._board_view.cursor_moved.connect(self._on_cursor_moved)
        self._library_panel.component_selected.connect(self._tool_place)
        session.drc_changed.connect(self._drc_panel.update_issues)

        # Default tool.
        self._tool_select()

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

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _run_drc(self) -> None:
        issues = self._session.run_drc()
        n = len(issues)
        self._status.showMessage(f"DRC: {n} issue{'s' if n != 1 else ''}", 4000)

    def _on_cursor_moved(self, x: float, y: float) -> None:
        self._cursor_label.setText(f"({x:.2f}, {y:.2f})")

    # ------------------------------------------------------------------
    # Keyboard shortcuts
    # ------------------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key.Key_S:
            self._tool_select()
        elif key == Qt.Key.Key_W:
            self._tool_route()
        elif key == Qt.Key.Key_F5:
            self._run_drc()
        elif (
            key == Qt.Key.Key_Z
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            if self._session.stack.can_undo():
                self._session.stack.undo(self._session.layout)
                self._board_view.refresh()
        elif (
            key == Qt.Key.Key_Y
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            if self._session.stack.can_redo():
                self._session.stack.redo(self._session.layout)
                self._board_view.refresh()
        else:
            super().keyPressEvent(event)
