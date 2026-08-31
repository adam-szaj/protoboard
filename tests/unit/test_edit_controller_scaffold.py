# SPDX-License-Identifier: MIT
"""Step-0 scaffold smoke: EditController + HeadlessSession, no Qt.

Proves the extracted state substrate (:class:`HeadlessSession`) and the
command-execution seam (:meth:`EditController._execute`) run a real command
end-to-end without a display server — the foundation every later edit verb
builds on. Also asserts the GUI ``EditorSession`` still structurally satisfies
the ``EditingContext`` protocol, so the same controller drives both.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from protoboard.controller.editing import EditingContext
from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, PadLayers, SizePolicy
from protoboard.domain.commands.component_cmds import (
    AddWireCommand,
    RemoveWireCommand,
)
from protoboard.domain.layout.wire import Wire
from protoboard.geometry import Point
from protoboard.headless_session import HeadlessSession
from protoboard.services.library.loader import load_library

_LIBRARY_DIR = Path(__file__).parent.parent / "fixtures" / "library"


def _perfboard(rows: int = 5, cols: int = 5) -> Board:
    grid = BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=rows,
        cols=cols,
        row_gap=0.0,
        col_gap=0.0,
    )
    return Board(root=grid)


def _session() -> HeadlessSession:
    library = load_library([_LIBRARY_DIR])
    return HeadlessSession(board=_perfboard(), library=library)


def test_headless_session_is_qt_free_and_builds_caches() -> None:
    """HeadlessSession stands up with no Qt and its read caches resolve."""
    session = _session()
    # Core command-state members present.
    assert session.board is not None
    assert session.layout is not None
    assert session.schematic is not None
    # Read caches (the pre-command reads the plan says were the real gap).
    assert session.connectivity_graph() is not None
    assert session.pad_index() is not None
    assert session.group_tree() is not None
    assert session.conflict_scanner() is not None


def test_controller_executes_and_reverts_wire_command() -> None:
    """EditController._execute runs a real command through the stack."""
    session = _session()
    controller = session.edit_controller

    wire = Wire(
        id=uuid.uuid4(),
        path=[Point(1.0, 1.0), Point(3.0, 1.0)],
        insulated=True,
        side="top",
    )
    assert controller._execute(AddWireCommand(wire=wire)) is True
    assert any(w.id == wire.id for w in session.layout.wires)

    assert controller._execute(RemoveWireCommand(wire_id=wire.id)) is True
    assert not any(w.id == wire.id for w in session.layout.wires)

    # The command went through the shared stack, so undo restores the wire.
    session.stack.undo(session)  # type: ignore[arg-type]
    assert any(w.id == wire.id for w in session.layout.wires)


def test_controller_notify_hook_fires_on_success() -> None:
    """_execute pings the state's change hook after a successful command."""

    class _CountingSession(HeadlessSession):
        def __init__(self) -> None:
            library = load_library([_LIBRARY_DIR])
            super().__init__(board=_perfboard(), library=library)
            self.changed = 0

        def notify_changed(self) -> None:
            self.changed += 1

    session = _CountingSession()
    controller = session.edit_controller
    wire = Wire(
        id=uuid.uuid4(),
        path=[Point(1.0, 1.0), Point(3.0, 1.0)],
        insulated=True,
        side="top",
    )
    assert controller._execute(AddWireCommand(wire=wire)) is True
    assert session.changed == 1


def test_editor_session_satisfies_edit_state() -> None:
    """The GUI EditorSession still structurally satisfies EditingContext.

    Imported lazily so this Qt-free module never pulls PySide6 unless a Qt
    session is actually constructed elsewhere; the isinstance check against the
    runtime-checkable protocol only needs the class, which does import Qt — so
    guard on availability and skip cleanly on a headless box without PySide6.
    """
    import pytest

    pytest.importorskip("PySide6")
    from protoboard.gui.session import EditorSession

    library = load_library([_LIBRARY_DIR])
    session = EditorSession(board=_perfboard(), library=library)
    assert isinstance(session, EditingContext)
    # The change hook the controller pings exists on the GUI session.
    assert callable(session.notify_changed)
