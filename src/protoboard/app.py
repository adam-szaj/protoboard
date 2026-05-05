# SPDX-License-Identifier: MIT
"""Entry point for the Protoboard Designer application."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import structlog as _structlog
from PySide6.QtWidgets import QApplication, QMessageBox

from protoboard.gui import recent_projects
from protoboard.gui.dialogs.welcome_dialog import WelcomeDialog
from protoboard.gui.main_window import MainWindow
from protoboard.gui.session import EditorSession
from protoboard.logging_config import configure_logging
from protoboard.services.library.loader import load_library
from protoboard.services.persistence.project_yaml import load_project, save_project

_log = _structlog.get_logger(__name__)

_LIBRARY_DIR = Path(__file__).parent.parent.parent / "library"


def _open_session(
    action: str,
    dlg: WelcomeDialog,
    library: object,
) -> EditorSession | None:
    """Build an EditorSession from a completed WelcomeDialog result.

    Returns None if setup fails (error shown to user, caller should retry).
    """
    from protoboard.services.library.loader import ComponentLibrary

    assert isinstance(library, ComponentLibrary)

    project_path = dlg.project_path()

    if action == "new":
        board = dlg.board()
        if board is None or project_path is None:
            return None
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
            _log.warning("project.open_failed", path=str(project_path), error=str(exc))
            QMessageBox.critical(None, "Error", f"Cannot create project:\n{exc}")
            return None
        _log.info("project.created", path=str(project_path))
        session = EditorSession(board=board, library=library)
        session.project_path = project_path
        recent_projects.add(project_path)
        return session

    if action == "open":
        if project_path is None:
            return None
        try:
            board, layout, schematic, instances = load_project(project_path)
        except Exception as exc:
            _log.warning("project.open_failed", path=str(project_path), error=str(exc))
            QMessageBox.critical(None, "Open error", str(exc))
            return None
        _log.info("project.opened", path=str(project_path))
        session = EditorSession(board=board, library=library)
        session.layout = layout
        session.schematic = schematic
        session.instances = instances
        session.project_path = project_path
        recent_projects.add(project_path)
        return session

    return None


def main() -> None:
    parser = argparse.ArgumentParser(prog="protoboard", add_help=False)
    parser.add_argument("--verbose", "-v", action="store_true", default=False)
    args, remaining = parser.parse_known_args(sys.argv[1:])
    configure_logging(verbose=args.verbose)
    _log.info("protoboard.starting", verbose=args.verbose)

    app = QApplication([sys.argv[0]] + remaining)
    app.setApplicationName("Protoboard Designer")
    # Prevent automatic quit when the last dialog closes before the main
    # window appears (welcome screen → new project dialog chain).
    app.setQuitOnLastWindowClosed(False)

    library_dirs = [_LIBRARY_DIR] if _LIBRARY_DIR.exists() else []
    library = load_library(library_dirs)

    window: MainWindow | None = None

    def show_welcome() -> None:
        nonlocal window
        dlg = WelcomeDialog()
        if dlg.exec() != WelcomeDialog.DialogCode.Accepted:
            # User clicked Quit on the welcome screen.
            app.quit()
            return

        session = _open_session(dlg.result_action, dlg, library)
        if session is None:
            # Setup failed; show welcome again.
            show_welcome()
            return

        window = MainWindow(session)
        # When the window is hidden by "Close Project", loop back to welcome.
        window.return_to_welcome.connect(show_welcome)
        window.show()

    show_welcome()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
