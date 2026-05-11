# SPDX-License-Identifier: MIT
"""Shared fixtures for performance benchmarks.

Loads the m4 project once per session — it's the largest realistic project
available in the repo and exercises all the hot paths.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_M4_PATH = Path(__file__).parent.parent.parent / "m4"
_GOLD_PATH = Path(__file__).parent.parent / "gold_performance_test_check"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "benchmark: performance benchmark (use --benchmark-only to run)"
    )


@pytest.fixture(scope="session")
def qt_app():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture(scope="session")
def m4_project():
    """Load the m4 project once for all benchmarks in the session."""
    if not _M4_PATH.exists():
        pytest.skip("m4 project not found — run from repo root")
    from protoboard.paths import BUILTIN_LIBRARY_DIR
    from protoboard.services.library.loader import load_library
    from protoboard.services.persistence.project_yaml import load_project

    lib = load_library([BUILTIN_LIBRARY_DIR])
    board, layout, schematic, instances, routing, _ = load_project(_M4_PATH)
    components = {name: lib.get(name) for name in lib.names()}
    return board, layout, schematic, instances, routing, lib, components


@pytest.fixture(scope="session")
def m4_session(qt_app, m4_project):
    """EditorSession pre-loaded with the m4 project."""
    board, layout, schematic, instances, routing, lib, _ = m4_project
    from protoboard.gui.session import EditorSession
    session = EditorSession(board=board, library=lib)
    session.layout = layout
    session.schematic = schematic
    session.instances = instances
    session.routing_settings = routing
    return session


@pytest.fixture(scope="session")
def m4_board_view(qt_app, m4_session):
    """BoardView with m4 project, warmed up (cold refresh done)."""
    from protoboard.gui.board_view import BoardView
    view = BoardView(m4_session)
    view.refresh()  # cold — builds all groups
    return view


@pytest.fixture(scope="session")
def gold_project():
    """Load the gold_performance_test_check project (143 comps, 100 nets, 0 wires)."""
    if not _GOLD_PATH.exists():
        pytest.skip("gold project not found — run from repo root")
    from protoboard.paths import BUILTIN_LIBRARY_DIR
    from protoboard.services.library.loader import load_library
    from protoboard.services.persistence.project_yaml import load_project

    lib = load_library([BUILTIN_LIBRARY_DIR])
    board, layout, schematic, instances, routing, _ = load_project(_GOLD_PATH)
    components = {name: lib.get(name) for name in lib.names()}
    return board, layout, schematic, instances, routing, lib, components
