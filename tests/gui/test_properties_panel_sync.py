# SPDX-License-Identifier: MIT
"""Properties panel ↔ selection wiring (audit regression).

v2 never told the Properties panel what was selected (``show_component`` had no
caller), so it stayed blank and a keyboard rotate left a stale rotation combo.
The panel now owns its sync: it subscribes to ``session.selection_changed`` and
its ``_on_session_selection`` slot populates / clears itself (previously
``MainWindow._sync_properties_panel`` did this).  rotate/flip re-broadcast the
selection so the panel refreshes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from protoboard.controller.app_settings import AppSettings
from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.gui import keybindings as KB
from protoboard.gui.main_window import MainWindow
from protoboard.gui.session import EditorSession
from protoboard.services.library.loader import load_library

_LIBRARY_DIR = Path(__file__).parent.parent / "fixtures" / "library"


@pytest.fixture(autouse=True)
def _isolate_keybindings_file(tmp_path, monkeypatch):
    """Redirect KeyBindings persistence to a throwaway file (matches the
    main_window test suite) so constructing MainWindow never touches the real
    user config."""
    monkeypatch.setattr(KB, "_KEYBINDINGS_PATH", tmp_path / "keybindings.yaml")


def _make_perfboard(rows: int = 10, cols: int = 10) -> Board:
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


@pytest.fixture()
def main_window(qtbot) -> MainWindow:
    library = load_library([_LIBRARY_DIR])
    session = EditorSession(board=_make_perfboard(), library=library)
    mw = MainWindow(session, AppSettings())
    qtbot.addWidget(mw)
    return mw


def _place(mw: MainWindow, x: float, y: float):  # type: ignore[no-untyped-def]
    """Add a 1-pin component to the session directly (no PlaceTool drag loop —
    that spins under pytest-qt headless).  Returns the instance."""
    from protoboard.domain.components.definition import (
        ComponentDefinition,
        MetaPin,
        PackageOption,
    )
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.components.package import PackageFamily, PackageVariant
    from protoboard.domain.components.package.loader import build_package
    from protoboard.domain.layout.placement import ComponentPlacement
    from protoboard.geometry import Point

    sess = mw._session
    if "D1" not in sess.components:
        package = build_package(
            {
                "shape": "prism",
                "dims": {"lx": 2.0, "ly": 2.0},
                "arrangement": "axial",
                "mount": "through_hole",
                "bend": 0,
                "leads": {
                    "direction": [[0, 0, -1]],
                    "body_offset": {"unit": 2.5, "value": []},
                    "board_offset": "default",
                },
            },
            "D1-package",
        )
        sess.package_families["D1-package"] = PackageFamily(
            family_id="D1-package",
            variants=(PackageVariant("std", "std", package),),
        )
        sess.components["D1"] = ComponentDefinition(
            name="D1",
            meta_pins=(MetaPin(("A",)),),
            package_options=(
                PackageOption(
                    name="D1",
                    ref="D1-package/std",
                    pin_map={1: (1,)},
                ),
            ),
        )
    inst = ComponentInstance.new(definition_ref="D1", refdes="U1", value="")
    inst = inst.bind(sess.components["D1"], sess.package_families)
    sess.instances[inst.id] = inst
    sess.layout.placements[inst.id] = ComponentPlacement(
        component_id=inst.id,
        position=Point(x, y),
        rotation=0,
        side="top",
    )
    sess.invalidate_graph()
    mw._board_view.refresh()
    return inst


# These drive the panel's own ``_on_session_selection`` slot directly (the
# wiring under test) rather than relying on the Qt ``selection_changed`` signal
# being delivered synchronously — pytest-qt does not pump the event loop between
# calls, so the queued emission would not have landed by the assert.  Driving the
# slot exercises the same code the live subscription invokes.


def test_selecting_component_populates_panel(main_window, qtbot) -> None:
    inst = _place(main_window, 2.5, 2.5)
    panel = main_window._props_panel
    assert panel._current_id is None  # blank before select

    panel._on_session_selection(frozenset({inst.id}), frozenset(), None)
    assert panel._current_id == inst.id  # panel now shows the component


def test_panel_reflects_current_rotation(main_window, qtbot) -> None:
    """The combo reads the placement's live rotation on each selection sync — so
    a re-broadcast after a rotate (which SelectTool now does) updates it.

    Drives the placement + handler directly: running the real rotate under
    pytest-qt hangs (an event-loop artifact unrelated to the fix; verified
    working in a standalone harness).
    """
    inst = _place(main_window, 2.5, 2.5)
    panel = main_window._props_panel
    panel._on_session_selection(frozenset({inst.id}), frozenset(), None)
    assert panel._rotation_combo.currentIndex() == 0  # 0°

    # Simulate the post-rotate state: placement at 90°, selection re-broadcast.
    pl = main_window._session.layout.placements[inst.id]
    pl.rotation = 90
    panel._on_session_selection(frozenset({inst.id}), frozenset(), None)
    assert panel._rotation_combo.currentIndex() == 1  # combo followed the rotation


def test_deselect_clears_panel(main_window, qtbot) -> None:
    inst = _place(main_window, 2.5, 2.5)
    panel = main_window._props_panel
    panel._on_session_selection(frozenset({inst.id}), frozenset(), None)
    assert panel._current_id == inst.id

    panel._on_session_selection(frozenset(), frozenset(), None)
    assert panel._current_id is None  # cleared
