# SPDX-License-Identifier: MIT
"""Entry point for the Protoboard Designer application."""

from __future__ import annotations

import argparse
import os
import sys

# QT_QPA_PLATFORM=xcb workaround is now applied at package import time
# (see protoboard/__init__.py).  Kept here too as defense-in-depth in
# case someone reorders imports above the package import.
os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

import structlog as _structlog
from PySide6.QtWidgets import QApplication, QMessageBox

from protoboard.gui import recent_projects
from protoboard.gui.app_settings import AppSettings, load_app_settings
from protoboard.gui.dialogs.welcome_dialog import WelcomeDialog
from protoboard.gui.main_window import MainWindow
from protoboard.gui.session import EditorSession
from protoboard.logging_config import configure_logging
from protoboard.paths import BUILTIN_LIBRARY_DIR
from protoboard.services.library.loader import load_library
from protoboard.services.persistence.project_yaml import load_project, save_project

_log = _structlog.get_logger(__name__)


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
        session.start_recording()
        return session

    if action == "open":
        if project_path is None:
            return None
        try:
            (board, layout, schematic, instances,
             routing_settings, layer_overrides, axis_label_settings) = load_project(project_path)
        except Exception as exc:
            _log.warning("project.open_failed", path=str(project_path), error=str(exc))
            QMessageBox.critical(None, "Open error", str(exc))
            return None
        _log.info("project.opened", path=str(project_path))
        session = EditorSession(board=board, library=library)
        session.layout = layout
        session.schematic = schematic
        session.instances = instances
        session.routing_settings = routing_settings
        session.axis_label_settings = axis_label_settings
        session.project_path = project_path
        session.layer_overrides = layer_overrides
        recent_projects.add(project_path)
        session.start_recording()
        return session

    return None


def main() -> None:
    parser = argparse.ArgumentParser(prog="protoboard", add_help=False)
    parser.add_argument("--verbose", "-v", action="store_true", default=False)
    parser.add_argument("--version", action="store_true", default=False)
    parser.add_argument("--help", "-h", action="store_true", default=False)
    args, remaining = parser.parse_known_args(sys.argv[1:])
    if args.help:
        # Minimal help text — Qt argv is appended after our flags, so we
        # use a compact summary rather than the full argparse usage to
        # avoid confusing the user with PySide6 internals.
        print(
            "Usage: protoboard [-v|--verbose] [--version] [--help] [project_path]"
        )
        return
    if args.version:
        from protoboard import __version__
        print(f"protoboard-designer {__version__}")
        return
    import logging as _logging
    configure_logging(
        verbose=args.verbose,
        module_overrides={"protoboard.gui": _logging.INFO},
    )
    _log.info("protoboard.starting", verbose=args.verbose)

    # Register built-in routers + optimizers and discover external plugins.
    # The imports are side-effecting: each `builtin*` module registers the
    # default implementations on import.  Discovery walks both entry-point
    # groups; broken plugins are logged but do not abort the host.
    import protoboard.services.routing.builtin  # noqa: F401
    import protoboard.services.routing.builtin_optimizers  # noqa: F401
    from protoboard.services.routing.discovery import discover_all_plugins
    _discovered = discover_all_plugins()
    if any(_discovered.values()):
        _log.info("routing.plugins_loaded", **_discovered)

    app = QApplication([sys.argv[0]] + remaining)
    app.setApplicationName("Protoboard Designer")
    # Prevent automatic quit when the last dialog closes before the main
    # window appears (welcome screen → new project dialog chain).
    app.setQuitOnLastWindowClosed(False)

    app_settings = load_app_settings()
    extra_dirs = [d for d in app_settings.component_library_dirs if d.exists()]
    library_dirs = ([BUILTIN_LIBRARY_DIR] if BUILTIN_LIBRARY_DIR.exists() else []) + extra_dirs
    library = load_library(library_dirs)

    window: MainWindow | None = None

    def show_welcome() -> bool:
        """Show the welcome dialog and open a project.

        Returns True if a MainWindow is now visible, False if the user
        chose Quit / closed the dialog.  Callers in signal handlers must
        also call ``app.quit()`` on False; the initial call site handles
        it by skipping ``app.exec()``.
        """
        nonlocal window
        # Loop instead of recursing so we never nest QDialog.exec() event loops.
        # Recursion caused app.quit() to only exit the innermost local loop,
        # leaving the process alive after the window was closed.
        while True:
            dlg = WelcomeDialog(default_dir=app_settings.default_projects_dir)
            if dlg.exec() != WelcomeDialog.DialogCode.Accepted:
                # User pressed Escape or clicked Quit on the welcome screen.
                return False

            session = _open_session(dlg.result_action, dlg, library)
            if session is None:
                # Setup failed — show welcome again (loop, not recurse).
                continue

            break

        # Destroy the old window before creating the new one so Qt doesn't
        # keep the closed window alive and raise it instead of the new one.
        if window is not None:
            window.deleteLater()

        window = MainWindow(session, app_settings=app_settings)
        # When the window is hidden by "Close Project", loop back to welcome.
        window.return_to_welcome.connect(_on_return_to_welcome)
        window.show()
        window.raise_()
        window.activateWindow()
        return True

    def _on_return_to_welcome() -> None:
        # Re-entry from a running main window's "Close Project".  Quit the
        # app if the user cancels the welcome screen this time.
        if not show_welcome():
            app.quit()

    def _shutdown_pools() -> None:
        from protoboard.services.routing.autorouter import _ASTAR_POOL
        _ASTAR_POOL.shutdown(wait=False, cancel_futures=True)

    app.aboutToQuit.connect(_shutdown_pools)

    if not show_welcome():
        # User cancelled the very first welcome dialog — exit before
        # starting the main event loop so the process terminates cleanly.
        _shutdown_pools()
        return
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
