# SPDX-License-Identifier: MIT
"""Save must resync the session with the canonicalised layout.

``save_project`` canonicalises the LIVE layout in place (endpoint
split/merge passes) before writing — wires that meet end-to-end with
matching insulation get fused into one Wire.  The save handler must then
emit ``layout_changed`` so the session caches (wire_spatial_index,
wire_by_id, wire_net_map) resync; otherwise hit-test and the hover
highlight keep resolving the PRE-save wire ids, and only the fused wire's
winning-id section stays selectable (the user-reported bug: three
bare/insulated/bare wires merged on save, yellow hover worked on one of
the three sections).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from protoboard.controller.app_settings import AppSettings
from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.geometry import Point
from protoboard.gui.main_window import MainWindow
from protoboard.gui.session import EditorSession
from protoboard.services.library.loader import load_library

_LIBRARY_DIR = Path(__file__).parent.parent / "fixtures" / "library"


def _make_perfboard(rows: int = 8, cols: int = 8) -> Board:
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


class TestSaveResyncsCaches:
    def test_save_merge_resyncs_wire_spatial_index(
        self, qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        library = load_library([_LIBRARY_DIR])
        session = EditorSession(board=_make_perfboard(), library=library)
        mw = MainWindow(session, AppSettings())
        qtbot.addWidget(mw)

        # The user's exact shape: bare | insulated | bare in series.  The
        # insulation mismatch blocks AddWireCommand's add-time collapse, and
        # setting the middle bare afterwards does NOT auto-fuse — three
        # same-insulation touching wires persist until the save-time
        # canonicalise pass merges them.
        w1 = session.edit_controller.add_wire(
            [Point(1.5, 1.5), Point(2.5, 1.5)],
            insulated=False,
            side="top",
        )
        w2 = session.edit_controller.add_wire(
            [Point(2.5, 1.5), Point(3.5, 1.5)],
            insulated=True,
            side="top",
        )
        w3 = session.edit_controller.add_wire(
            [Point(3.5, 1.5), Point(4.5, 1.5)],
            insulated=False,
            side="top",
        )
        assert w1 is not None and w2 is not None and w3 is not None
        session.edit_controller.set_wires_insulated([w2], insulated=False)
        assert len(session.layout.wires) == 3  # still three separate wires
        session.wire_spatial_index()  # warm the cache pre-save

        # Run the worker synchronously; skip git commit + recent-projects IO.
        monkeypatch.setattr(
            "protoboard.gui.workers.operation_worker.run_with_progress",
            lambda parent, title, fn, label: fn(),
        )
        monkeypatch.setattr(mw, "_commit_save", lambda path: "")
        monkeypatch.setattr(
            "protoboard.gui.main_window.recent_projects",
            type("R", (), {"add": staticmethod(lambda path: None)})(),
        )

        mw._do_save(tmp_path / "proj")

        live_ids = {w.id for w in session.layout.wires}
        assert len(live_ids) == 1, (
            f"expected the save canonicalise pass to fuse into 1 wire, "
            f"got {len(live_ids)}"
        )
        indexed = {wid for ids in session.wire_spatial_index().values() for wid in ids}
        assert indexed == live_ids, (
            f"session caches were not resynced after save mutated the "
            f"layout: index={indexed} live={live_ids}"
        )
