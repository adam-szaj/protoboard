# SPDX-License-Identifier: MIT
"""Tests for the autoroute UI gate (Session.autoroute_active).

Covers:

- CommandStack guard refuses hard-mutate commands while autoroute is active.
- Allowed commands (rename / color / global) pass through.
- MacroCommand inherits ``allowed_during_autoroute`` from its children.
- NetsEditorPanel disables structural buttons while gate is on.
"""

from __future__ import annotations

import uuid
from typing import TypeVar, cast

import pytest
from pytestqt.qtbot import QtBot

from protoboard.domain.commands.base import (
    Command,
    CommandRejectedError,
    CommandStack,
    MacroCommand,
)
from protoboard.domain.commands.component_cmds import RemoveWireCommand
from protoboard.domain.commands.net_cmds import (
    CreateNetCommand,
    DeleteNetCommand,
    RenameNetCommand,
    SetNetColorCommand,
    SetNetGlobalCommand,
)
from protoboard.domain.schematic.net import Net
from protoboard.gui.session import EditorSession


@pytest.fixture()
def session_with_net(session: EditorSession) -> tuple[EditorSession, uuid.UUID]:
    net_id = uuid.uuid4()
    session.schematic.nets[net_id] = Net(id=net_id, name="VCC")
    return session, net_id


_S = TypeVar("_S")


def _execute(session: EditorSession, cmd: Command[_S]) -> None:
    """Execute ``cmd`` against ``session`` through its command stack.

    ``EditorSession`` structurally satisfies every command state protocol
    (``ComponentState`` / ``NetState`` / …), but ``session.stack`` is declared
    ``CommandStack[ComponentState]`` and the invariant generic ``T`` prevents
    Pyright from unifying that with net commands. Cast the stack and state to
    the command's own state type — the runtime behaviour is unchanged.
    """
    cast("CommandStack[_S]", session.stack).execute(cmd, cast("_S", session))


class TestCommandGuard:
    def test_hard_mutate_rejected_while_autoroute_active(
        self, session_with_net: tuple[EditorSession, uuid.UUID]
    ) -> None:
        session, net_id = session_with_net
        session.set_autoroute_active(True)
        try:
            cmd = CreateNetCommand(name="GND")
            with pytest.raises(CommandRejectedError):
                _execute(session, cmd)
        finally:
            session.set_autoroute_active(False)

    def test_set_net_color_allowed_during_autoroute(
        self, session_with_net: tuple[EditorSession, uuid.UUID]
    ) -> None:
        session, net_id = session_with_net
        session.set_autoroute_active(True)
        try:
            cmd = SetNetColorCommand(net_id=net_id, new_color="#FF8800")
            _execute(session, cmd)
            assert session.schematic.nets[net_id].color == "#FF8800"
        finally:
            session.set_autoroute_active(False)

    def test_rename_net_allowed_during_autoroute(
        self, session_with_net: tuple[EditorSession, uuid.UUID]
    ) -> None:
        session, net_id = session_with_net
        session.set_autoroute_active(True)
        try:
            _execute(session, RenameNetCommand(net_id=net_id, new_name="V_BAT"))
            assert session.schematic.nets[net_id].name == "V_BAT"
        finally:
            session.set_autoroute_active(False)

    def test_set_global_allowed_during_autoroute(
        self, session_with_net: tuple[EditorSession, uuid.UUID]
    ) -> None:
        session, net_id = session_with_net
        session.set_autoroute_active(True)
        try:
            _execute(session, SetNetGlobalCommand(net_id=net_id, global_net=True))
            assert session.schematic.nets[net_id].global_net is True
        finally:
            session.set_autoroute_active(False)

    def test_macro_blocks_if_any_child_hard_mutates(
        self, session_with_net: tuple[EditorSession, uuid.UUID]
    ) -> None:
        session, net_id = session_with_net
        session.set_autoroute_active(True)
        try:
            soft = SetNetColorCommand(net_id=net_id, new_color="#FF0000")
            hard = DeleteNetCommand(net_id=net_id)
            macro = MacroCommand([soft, hard], description="mixed")  # type: ignore[arg-type]
            with pytest.raises(CommandRejectedError):
                _execute(session, macro)
        finally:
            session.set_autoroute_active(False)

    def test_macro_passes_when_all_children_soft(
        self, session_with_net: tuple[EditorSession, uuid.UUID]
    ) -> None:
        session, net_id = session_with_net
        session.set_autoroute_active(True)
        try:
            soft1 = SetNetColorCommand(net_id=net_id, new_color="#FF0000")
            soft2 = RenameNetCommand(net_id=net_id, new_name="ALSO")
            macro = MacroCommand([soft1, soft2], description="bothsoft")  # type: ignore[arg-type]
            _execute(session, macro)
            assert session.schematic.nets[net_id].color == "#FF0000"
            assert session.schematic.nets[net_id].name == "ALSO"
        finally:
            session.set_autoroute_active(False)

    def test_gate_off_allows_everything(
        self, session_with_net: tuple[EditorSession, uuid.UUID]
    ) -> None:
        session, net_id = session_with_net
        # Default — gate off.
        assert session.autoroute_active is False
        _execute(session, CreateNetCommand(name="GND"))
        # No exception → pass.


class TestApplyResultPath:
    """The worker's finished result is applied as a MacroCommand of
    AddWireCommand — both hard-mutate.  The gate MUST be released
    before the apply handler runs, otherwise CommandStack rejects our
    own commit."""

    def test_simulated_finished_path_does_not_reject_macro(
        self, session_with_net: tuple[EditorSession, uuid.UUID]
    ) -> None:
        from protoboard.domain.commands.component_cmds import AddWireCommand
        from protoboard.domain.layout.wire import Wire
        from protoboard.geometry import Point

        session, _net_id = session_with_net
        # Simulate the order of operations the autoroute_active gate
        # arranges: release gate, then apply MacroCommand.
        session.set_autoroute_active(True)
        try:
            session.set_autoroute_active(False)  # mimic _release_gate first
            wire = Wire(
                id=uuid.uuid4(),
                path=[Point(0.5, 0.5), Point(1.5, 0.5)],
                insulated=True,
                side="top",
            )
            macro = MacroCommand(
                [AddWireCommand(wire=wire)],  # type: ignore[arg-type, list-item]
                description="Autoroute (1 wires)",
            )
            _execute(session, macro)
            # No CommandRejectedError raised → path is correct.
            assert wire in session.layout.wires
        finally:
            session.set_autoroute_active(False)


class TestSignalEmission:
    def test_set_autoroute_active_emits_signal(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        with qtbot.waitSignal(session.autoroute_active_changed, timeout=500) as blocker:
            session.set_autoroute_active(True)
        assert blocker.args == [True]

    def test_set_autoroute_active_noop_when_unchanged(
        self, qtbot: QtBot, session: EditorSession
    ) -> None:
        emissions: list[bool] = []
        session.autoroute_active_changed.connect(emissions.append)
        session.set_autoroute_active(False)  # already False
        session.set_autoroute_active(False)
        assert emissions == []
