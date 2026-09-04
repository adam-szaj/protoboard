# SPDX-License-Identifier: MIT
"""Small GUI smoke tests asserting public action outcomes."""

from __future__ import annotations

import uuid

import pytest
from PySide6.QtGui import QAction
from pytestqt.qtbot import QtBot

from protoboard.controller.app_settings import AppSettings
from protoboard.domain.commands.component_cmds import AddWireCommand
from protoboard.domain.layout.wire import Wire
from protoboard.geometry import Point
from protoboard.gui.main_window import MainWindow
from protoboard.gui.session import EditorSession


@pytest.fixture
def main_window_with_session(
    session: EditorSession, qtbot: QtBot
) -> tuple[MainWindow, EditorSession]:
    window = MainWindow(session, AppSettings())
    qtbot.addWidget(window)
    return window, session


def _action(window: MainWindow, label: str) -> QAction:
    for action in window.findChildren(QAction):
        if action.text().replace("&", "").startswith(label):
            return action
    raise AssertionError(f"no public QAction labelled {label!r}")


def test_undo_and_redo_actions_change_session_state(
    main_window_with_session: tuple[MainWindow, EditorSession],
    qtbot: QtBot,
) -> None:
    window, session = main_window_with_session
    wire = Wire(
        id=uuid.uuid4(),
        path=[Point(0.5, 0.5), Point(1.5, 0.5)],
        side="top",
    )
    assert session.edit_controller.execute_command(AddWireCommand(wire=wire))
    assert len(session.layout.wires) == 1

    undo = _action(window, "Undo")
    qtbot.waitUntil(undo.isEnabled)
    undo.trigger()
    assert len(session.layout.wires) == 0

    redo = _action(window, "Redo")
    qtbot.waitUntil(redo.isEnabled)
    redo.trigger()
    assert len(session.layout.wires) == 1
    session.mark_clean()
