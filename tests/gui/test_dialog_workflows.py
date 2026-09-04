# SPDX-License-Identifier: MIT
"""Public outcome tests for application dialogs."""

from __future__ import annotations

import uuid
from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox,
    QDialogButtonBox,
    QListWidget,
    QPushButton,
    QSpinBox,
)

from protoboard.controller.app_settings import AppSettings
from protoboard.controller.editing.placement_conflict import WireConflictAction
from protoboard.controller.view_options.theme import Theme
from protoboard.domain.layout.pad_conflict import ConflictKind, PadConflict
from protoboard.geometry import Point
from protoboard.gui.dialogs.new_project_dialog import NewProjectDialog
from protoboard.gui.dialogs.preferences_dialog import PreferencesDialog
from protoboard.gui.dialogs.resize_board_dialog import ResizeBoardDialog
from protoboard.gui.dialogs.wire_conflict_dialog import WireConflictDialog
from protoboard.gui.keybindings import KeyBindings


def _buttons(dialog: object) -> QDialogButtonBox:
    boxes = dialog.findChildren(QDialogButtonBox)  # type: ignore[attr-defined]
    assert len(boxes) == 1
    return boxes[0]


def test_new_project_accept_returns_selected_board_and_path(
    qtbot, tmp_path: Path
) -> None:
    dialog = NewProjectDialog(default_dir=tmp_path)
    qtbot.addWidget(dialog)
    combo = dialog.findChildren(QComboBox)[0]
    combo.setCurrentText("Stripboard")
    spins = dialog.findChildren(QSpinBox)
    spins[0].setValue(4)
    spins[1].setValue(7)
    dialog._path_edit.setText(str(tmp_path / "new-project"))

    _buttons(dialog).button(QDialogButtonBox.StandardButton.Ok).click()

    assert dialog.result() == dialog.DialogCode.Accepted
    assert dialog.project_path() == tmp_path / "new-project"
    assert dialog.board().bounds.lx > 0
    assert dialog.board().bounds.ly > 0


def test_new_project_cancel_does_not_accept(qtbot, tmp_path: Path) -> None:
    dialog = NewProjectDialog(default_dir=tmp_path)
    qtbot.addWidget(dialog)
    _buttons(dialog).button(QDialogButtonBox.StandardButton.Cancel).click()
    assert dialog.result() == dialog.DialogCode.Rejected


def test_resize_board_accept_produces_requested_dimensions(qtbot, session) -> None:
    dialog = ResizeBoardDialog(
        session.board, session.layout, session.instances, session.components
    )
    qtbot.addWidget(dialog)
    spins = dialog.findChildren(QSpinBox)
    spins[0].setValue(6)
    spins[1].setValue(9)

    _buttons(dialog).button(QDialogButtonBox.StandardButton.Ok).click()

    assert dialog.result() == dialog.DialogCode.Accepted
    assert dialog.new_board().bounds.lx > session.board.bounds.lx
    assert dialog.new_board().bounds.ly > session.board.bounds.ly


def test_resize_board_cancel_does_not_mutate_session_board(qtbot, session) -> None:
    before = session.board
    dialog = ResizeBoardDialog(
        session.board, session.layout, session.instances, session.components
    )
    qtbot.addWidget(dialog)
    _buttons(dialog).button(QDialogButtonBox.StandardButton.Cancel).click()
    assert dialog.result() == dialog.DialogCode.Rejected
    assert session.board is before


def _wire_conflict(kind: ConflictKind) -> PadConflict:
    return PadConflict(
        incoming_pin=1,
        existing_comp_id=uuid.uuid4(),
        existing_pin=0,
        position=Point(1, 1),
        kind=kind,
        existing_net_id=None,
        existing_net_name=None,
        incoming_net_id=None,
        incoming_net_name=None,
        wire_id=uuid.uuid4(),
    )


def test_wire_conflict_dialog_returns_create_net_decision(qtbot) -> None:
    dialog = WireConflictDialog(
        [_wire_conflict(ConflictKind.PIN_ON_WIRE_UNCONNECTED)], "R1"
    )
    qtbot.addWidget(dialog)
    buttons = dialog.findChildren(QPushButton)
    create = next(button for button in buttons if button.text() == "Create net")
    create.click()
    assert dialog.result() == dialog.DialogCode.Accepted
    assert dialog.chosen_action() is WireConflictAction.CREATE_NET


def test_wire_conflict_dialog_returns_cancel_decision(qtbot) -> None:
    dialog = WireConflictDialog(
        [_wire_conflict(ConflictKind.PIN_ON_WIRE_DIFFERENT_NET)], "R1"
    )
    qtbot.addWidget(dialog)
    cancel = next(
        button
        for button in dialog.findChildren(QPushButton)
        if button.text() == "Cancel"
    )
    cancel.click()
    assert dialog.result() == dialog.DialogCode.Rejected
    assert dialog.chosen_action() is WireConflictAction.CANCEL


def test_preferences_apply_invokes_callback(qtbot) -> None:
    calls: list[AppSettings] = []
    settings = AppSettings()
    dialog = PreferencesDialog(
        KeyBindings(),
        Theme(),
        settings,
        on_paths_applied=calls.append,
    )
    qtbot.addWidget(dialog)
    dialog.findChildren(QListWidget)[0].setCurrentRow(2)
    apply = _buttons(dialog).button(QDialogButtonBox.StandardButton.Apply)
    apply.click()
    assert calls == [settings]


def test_preferences_cancel_does_not_invoke_callback(qtbot) -> None:
    calls: list[AppSettings] = []
    dialog = PreferencesDialog(
        KeyBindings(),
        Theme(),
        AppSettings(),
        on_paths_applied=calls.append,
    )
    qtbot.addWidget(dialog)
    _buttons(dialog).button(QDialogButtonBox.StandardButton.Cancel).click()
    assert dialog.result() == dialog.DialogCode.Rejected
    assert calls == []
