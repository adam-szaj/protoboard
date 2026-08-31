# SPDX-License-Identifier: MIT
"""Menu toggle and ctx explicit lock both reach EditController.set_locked.

Both ``main_window._lock_toggle`` (menu-bar slot, which decides toggle
direction: all-locked -> unlock else lock) and ``select_tool._menu_lock``
(ctx handler, explicit ``locked`` bool) must reduce to a call into
``EditController.set_locked`` — the unified builder for the "Lock /
Unlock" op (same-op-same-code rule). ``_lock_toggle`` keeps the
all-locked decision (it reads current placements); only the command
construction is delegated.

Mirrors ``tests/unit/test_wire_insulated_entrypoint_parity.py``: the ctx
handler is Qt-free (only touches ``self._session``), driven with a bare
``SelectTool()``; the menu-bar slot needs a real ``MainWindow``
(constructible headlessly via ``qtbot``).  Every test that drives a
``MainWindow`` ends with ``session.mark_clean()`` so teardown's
``closeEvent`` doesn't pop a blocking "unsaved changes" dialog.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from protoboard.controller.app_settings import AppSettings
from protoboard.controller.tools.select_tool import SelectTool
from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, PadLayers, SizePolicy
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.placement import ComponentPlacement
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


def _place(session: EditorSession, x: float, y: float, refdes: str) -> object:
    """Place a 2-pin "R": pin 1 at (x, y), pin 2 at (x, y+1)."""
    defn = session.components["R"]
    inst = ComponentInstance.new(
        definition_ref="R",
        refdes=refdes,
        value="",
    ).bind(defn, session.package_families)
    session.instances[inst.id] = inst
    session.layout.placements[inst.id] = ComponentPlacement(
        component_id=inst.id,
        position=Point(x, y),
        rotation=0,
        side="top",
    )
    session.invalidate_graph()
    return inst.id


def _session_with_two_components() -> tuple[EditorSession, object, object]:
    library = load_library([_LIBRARY_DIR])
    session = EditorSession(board=_perfboard(), library=library)
    c1 = _place(session, 1.0, 1.0, "R1")
    c2 = _place(session, 4.0, 1.0, "R2")
    return session, c1, c2


def test_ctx_lock_all() -> None:
    """select_tool._menu_lock locks every targeted component."""
    session, c1, c2 = _session_with_two_components()

    tool = SelectTool()
    tool._session = session  # type: ignore[assignment]

    tool._menu_lock({c1, c2}, True)  # type: ignore[arg-type]

    assert session.layout.placements[c1].locked is True  # type: ignore[index]
    assert session.layout.placements[c2].locked is True  # type: ignore[index]


def test_ctx_lock_empty_selection_is_noop() -> None:
    session, c1, c2 = _session_with_two_components()

    tool = SelectTool()
    tool._session = session  # type: ignore[assignment]

    tool._menu_lock(set(), True)

    assert session.layout.placements[c1].locked is False  # type: ignore[index]
    assert session.layout.placements[c2].locked is False  # type: ignore[index]


def test_menu_toggle_unlocks_when_all_locked(qtbot: QtBot) -> None:
    """main_window._lock_toggle unlocks when the whole selection is locked."""
    session, c1, c2 = _session_with_two_components()
    for c in (c1, c2):  # arrange: all locked
        session.layout.placements[c].locked = True  # type: ignore[index]

    mw = MainWindow(session, AppSettings())
    qtbot.addWidget(mw)
    try:
        tool = mw._get_select_tool()
        assert tool is not None
        tool.selected_component_ids = {c1, c2}  # type: ignore[assignment]

        mw._lock_toggle()

        assert session.layout.placements[c1].locked is False  # type: ignore[index]
        assert session.layout.placements[c2].locked is False  # type: ignore[index]
    finally:
        # A dirty session left behind makes qtbot's teardown closeEvent()
        # pop a blocking "unsaved changes" QMessageBox and hang the test.
        session.mark_clean()


def test_menu_toggle_locks_when_mixed_selection(qtbot: QtBot) -> None:
    """main_window._lock_toggle locks the whole selection when not all locked."""
    session, c1, c2 = _session_with_two_components()
    session.layout.placements[c1].locked = True  # type: ignore[index]
    session.layout.placements[c2].locked = False  # type: ignore[index]

    mw = MainWindow(session, AppSettings())
    qtbot.addWidget(mw)
    try:
        tool = mw._get_select_tool()
        assert tool is not None
        tool.selected_component_ids = {c1, c2}  # type: ignore[assignment]

        mw._lock_toggle()

        assert session.layout.placements[c1].locked is True  # type: ignore[index]
        assert session.layout.placements[c2].locked is True  # type: ignore[index]
    finally:
        session.mark_clean()


def test_menu_and_ctx_call_verb_with_same_args(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verb-boundary parity: both handlers call the same verb the same way."""
    session, c1, c2 = _session_with_two_components()

    calls: list[tuple[frozenset, bool]] = []
    original = session.edit_controller.set_locked

    def _recording(component_ids: object, *, locked: bool) -> None:
        calls.append((frozenset(component_ids), locked))  # type: ignore[arg-type]
        original(component_ids, locked=locked)  # type: ignore[arg-type]

    monkeypatch.setattr(session.edit_controller, "set_locked", _recording)

    ctx_tool = SelectTool()
    ctx_tool._session = session  # type: ignore[assignment]
    ctx_tool._menu_lock({c1, c2}, True)  # type: ignore[arg-type]

    mw = MainWindow(session, AppSettings())
    qtbot.addWidget(mw)
    try:
        menu_tool = mw._get_select_tool()
        assert menu_tool is not None
        menu_tool.selected_component_ids = {c1, c2}  # type: ignore[assignment]
        # Both already locked from the ctx call -> toggle should unlock.
        mw._lock_toggle()

        assert len(calls) == 2
        assert calls[0] == (frozenset({c1, c2}), True)
        assert calls[1] == (frozenset({c1, c2}), False)
        assert session.layout.placements[c1].locked is False  # type: ignore[index]
        assert session.layout.placements[c2].locked is False  # type: ignore[index]
    finally:
        session.mark_clean()
