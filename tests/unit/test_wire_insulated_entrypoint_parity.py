# SPDX-License-Identifier: MIT
"""Menu and context-menu 'set insulated' reach the same verb (parity).

Both ``main_window._set_selected_wire_insulated`` (menu-bar slot) and
``select_tool._menu_set_wire_insulated`` (context-menu handler) must reduce
to a single call into ``EditController.set_wires_insulated`` — the unified
builder for the "Wire: Set Insulated / Bare" op (same-op-same-code rule).

``select_tool._menu_set_wire_insulated`` is Qt-free (only touches
``self._session``), so it is driven fully behaviorally here: a bare
``SelectTool()`` with ``_session`` set directly (no ``activate()``/view
needed — the rewired method never touches ``self._view``).

``main_window._set_selected_wire_insulated`` needs a real ``MainWindow``
(it resolves the active tool via ``self._board_view``), which is
constructible headlessly via ``qtbot`` + the shared ``session`` fixture.
Driving it behaviorally too lets the core assertion (every targeted wire's
``insulated`` flag flips) be checked identically for both entry points.

Driving a real ``MainWindow`` dirties the session (``layout_changed`` ->
``session.mark_dirty``); ``qtbot``'s widget teardown calls ``closeEvent`` ->
``_check_unsaved_changes``, which pops a blocking ``QMessageBox`` when dirty
and hangs the test process waiting for a click that never comes. Every test
that drives a ``MainWindow`` therefore ends with ``session.mark_clean()`` so
teardown's ``closeEvent`` short-circuits.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from protoboard.controller.app_settings import AppSettings
from protoboard.controller.tools.select_tool import SelectTool
from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, PadLayers, SizePolicy
from protoboard.geometry import Point
from protoboard.gui.main_window import MainWindow
from protoboard.gui.session import EditorSession
from protoboard.services.library.loader import load_library

_LIBRARY_DIR = Path(__file__).parent.parent / "fixtures" / "library"


def _perfboard(rows: int = 8, cols: int = 8) -> Board:
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


def _session_with_two_wires() -> EditorSession:
    library = load_library([_LIBRARY_DIR])
    session = EditorSession(board=_perfboard(), library=library)
    w1 = session.edit_controller.add_wire(
        [Point(1.0, 1.0), Point(3.0, 1.0)],
        insulated=True,
        side="top",
    )
    w2 = session.edit_controller.add_wire(
        [Point(1.0, 3.0), Point(3.0, 3.0)],
        insulated=True,
        side="top",
    )
    assert w1 is not None and w2 is not None
    session._test_wire_ids = (w1, w2)  # type: ignore[attr-defined]
    return session


def test_ctx_handler_sets_insulated(qtbot: QtBot) -> None:
    """select_tool._menu_set_wire_insulated flips every targeted wire."""
    session = _session_with_two_wires()
    w1, w2 = session._test_wire_ids  # type: ignore[attr-defined]

    tool = SelectTool()
    tool._session = session  # type: ignore[assignment]
    signals: list[None] = []
    session.layout_changed.connect(lambda: signals.append(None))

    tool._menu_set_wire_insulated({w1, w2}, False)

    assert session.layout.wires.get_or_raise(w1).insulated is False
    assert session.layout.wires.get_or_raise(w2).insulated is False
    assert len(signals) == 1


def test_ctx_handler_empty_selection_is_noop(qtbot: QtBot) -> None:
    session = _session_with_two_wires()
    w1, w2 = session._test_wire_ids  # type: ignore[attr-defined]

    tool = SelectTool()
    tool._session = session  # type: ignore[assignment]

    tool._menu_set_wire_insulated(set(), False)

    assert session.layout.wires.get_or_raise(w1).insulated is True
    assert session.layout.wires.get_or_raise(w2).insulated is True


def test_menu_slot_sets_insulated(qtbot: QtBot) -> None:
    """main_window._set_selected_wire_insulated flips every selected wire."""
    session = _session_with_two_wires()
    w1, w2 = session._test_wire_ids  # type: ignore[attr-defined]

    mw = MainWindow(session, AppSettings())
    qtbot.addWidget(mw)
    try:
        tool = mw._get_select_tool()
        assert tool is not None
        tool._selected_wire_ids = {w1, w2}

        mw._set_selected_wire_insulated(False)

        assert session.layout.wires.get_or_raise(w1).insulated is False
        assert session.layout.wires.get_or_raise(w2).insulated is False
    finally:
        # Always run, even on assertion failure — a dirty session left behind
        # makes qtbot's teardown closeEvent() pop a blocking "unsaved
        # changes" QMessageBox and hang the test process.
        session.mark_clean()


def test_menu_and_ctx_call_verb_with_same_args(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verb-boundary parity: both handlers call the same verb the same way."""
    session = _session_with_two_wires()
    w1, w2 = session._test_wire_ids  # type: ignore[attr-defined]

    calls: list[tuple[frozenset, bool]] = []
    original = session.edit_controller.set_wires_insulated

    def _recording(wire_ids: object, *, insulated: bool) -> None:
        calls.append((frozenset(wire_ids), insulated))  # type: ignore[arg-type]
        original(wire_ids, insulated=insulated)  # type: ignore[arg-type]

    monkeypatch.setattr(session.edit_controller, "set_wires_insulated", _recording)

    ctx_tool = SelectTool()
    ctx_tool._session = session  # type: ignore[assignment]
    ctx_tool._menu_set_wire_insulated({w1, w2}, False)

    mw = MainWindow(session, AppSettings())
    qtbot.addWidget(mw)
    try:
        menu_tool = mw._get_select_tool()
        assert menu_tool is not None
        menu_tool._selected_wire_ids = {w1, w2}
        mw._set_selected_wire_insulated(False)

        assert len(calls) == 2
        assert calls[0] == calls[1] == (frozenset({w1, w2}), False)
        # Both handlers also left the wires in the same final state.
        assert session.layout.wires.get_or_raise(w1).insulated is False
        assert session.layout.wires.get_or_raise(w2).insulated is False
    finally:
        # Always run, even on assertion failure — a dirty session left behind
        # makes qtbot's teardown closeEvent() pop a blocking "unsaved
        # changes" QMessageBox and hang the test process.
        session.mark_clean()
