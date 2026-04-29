# SPDX-License-Identifier: MIT
"""Entry point for the Protoboard Designer application."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from protoboard.gui.dialogs.welcome_dialog import WelcomeDialog
from protoboard.gui.main_window import MainWindow
from protoboard.gui.session import EditorSession
from protoboard.services.library.loader import load_library
from protoboard.services.persistence.project_yaml import load_project, save_project

_LIBRARY_DIR = Path(__file__).parent.parent.parent / "library"


def _run_welcome(app: QApplication, library: object) -> None:
    """Show welcome dialog in a loop; open MainWindow on success."""
    from protoboard.services.library.loader import ComponentLibrary

    assert isinstance(library, ComponentLibrary)

    while True:
        dlg = WelcomeDialog()
        if dlg.exec() != WelcomeDialog.DialogCode.Accepted:
            # User quit from welcome screen.
            return

        action = dlg.result_action
        project_path = dlg.project_path()

        if action == "new":
            board = dlg.board()
            if board is None or project_path is None:
                continue
            # Save initial empty project so the folder is a valid project.
            try:
                from protoboard.domain.layout.layout import Layout
                from protoboard.domain.schematic.schematic import Schematic

                project_path.mkdir(parents=True, exist_ok=True)
                save_project(
                    directory=project_path,
                    board=board,
                    layout=Layout(),
                    schematic=Schematic(nets={}),
                    instances={},
                )
            except Exception as exc:
                QMessageBox.critical(None, "Error", f"Cannot create project:\n{exc}")
                continue
            session = EditorSession(board=board, library=library)
            session.project_path = project_path

        elif action == "open":
            if project_path is None:
                continue
            try:
                board, layout, schematic, instances = load_project(project_path)
            except Exception as exc:
                QMessageBox.critical(None, "Open error", str(exc))
                continue
            session = EditorSession(board=board, library=library)
            session.layout = layout
            session.schematic = schematic
            session.instances = instances
            session.project_path = project_path

        else:
            continue

        window = MainWindow(session)
        window.show()

        # Run event loop until this window closes.
        # When MainWindow requests "return to welcome", it hides itself and
        # returns; we loop back to show the welcome dialog again.
        while window.isVisible():
            app.exec()
            if not window._return_to_welcome:
                # Normal quit (window closed via × button etc.)
                return

        # _return_to_welcome == True → loop back to welcome dialog.
        window.deleteLater()


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Protoboard Designer")
    # Don't quit when last modal dialog closes before main window appears.
    app.setQuitOnLastWindowClosed(False)

    library_dirs = [_LIBRARY_DIR] if _LIBRARY_DIR.exists() else []
    library = load_library(library_dirs)

    _run_welcome(app, library)


if __name__ == "__main__":
    main()
