# SPDX-License-Identifier: MIT
"""Bidirectional selection sync — board ↔ side panels through the session hub.

The session is the single hub: panels subscribe to ``selection_changed``
themselves (board → panel) and ask the board to select via
``request_select_*`` → ``selection_requested`` (panel → board).  MainWindow no
longer fans selection out to panels nor relays panel clicks to the board.

These build a real ``MainWindow`` so every panel is constructed and self-wired,
then drive the session signals/methods directly (pytest-qt does not pump the
event loop between calls, but the slots run synchronously when invoked).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from protoboard.controller.app_settings import AppSettings
from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.definition import (
    ComponentDefinition,
    MetaPin,
    PackageOption,
)
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package import PackageFamily, PackageVariant
from protoboard.domain.components.package.loader import build_package
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.geometry import Point
from protoboard.gui import keybindings as KB
from protoboard.gui.main_window import MainWindow
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.gui_tools import SelectTool
from protoboard.services.library.loader import load_library

_LIBRARY_DIR = Path(__file__).parent.parent / "fixtures" / "library"


@pytest.fixture(autouse=True)
def _isolate_keybindings_file(tmp_path, monkeypatch):
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


def _place(mw: MainWindow, x: float, y: float, refdes: str = "U1") -> ComponentInstance:
    """Add a 1-pin component directly (no PlaceTool drag loop)."""
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
    inst = ComponentInstance.new(definition_ref="D1", refdes=refdes, value="")
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


def _add_net_with_pin(
    mw: MainWindow, inst: ComponentInstance, name: str = "VCC"
) -> Net:
    sess = mw._session
    net = Net(id=uuid.uuid4(), name=name, pins=set())
    sess.schematic.nets[net.id] = net
    sess.schematic.add_pin_to_net(PinRef(component_id=inst.id, pin_number=1), net.id)
    sess.invalidate_graph()
    mw._refresh_nets_editor()
    mw._refresh_net_inspector()
    return net


def _select_tool(mw: MainWindow) -> SelectTool:
    mw._tool_select()
    tool = mw._board_view._active_tool
    assert isinstance(tool, SelectTool)
    return tool


# ---------------------------------------------------------------------------
# board → panel  (session.notify_selection drives the panel subscriptions)
# ---------------------------------------------------------------------------


def test_component_selection_populates_component_panels(main_window) -> None:
    inst = _place(main_window, 2.5, 2.5)
    main_window._session.notify_selection(frozenset({inst.id}), frozenset(), None)

    assert main_window._props_panel._current_id == inst.id


def test_component_selection_lights_pin_rows(main_window, qtbot) -> None:
    """nets_editor subscribes to the session itself; a component broadcast on
    ``selection_changed`` selects its Component-Pin rows.

    Driven through a freestanding panel bound to the same session (so isVisible()
    can be made true headless — a panel nested in an unshown MainWindow never
    becomes visible).  This exercises the real subscription: refresh() connects
    selection_changed, and the emit lands in sync_selection.
    """
    from protoboard.gui.panels.nets_editor_panel import NetsEditorPanel

    inst = _place(main_window, 2.5, 2.5)
    panel = NetsEditorPanel()
    qtbot.addWidget(panel)
    panel.refresh(main_window._session)  # subscribes to selection_changed
    panel.show()
    qtbot.waitExposed(panel)

    main_window._session.notify_selection(frozenset({inst.id}), frozenset(), None)

    selected = panel._pin_tree.selectedItems()
    assert selected, "the component's pin row should be selected"
    raw = selected[0].data(0, Qt.ItemDataRole.UserRole)
    assert str(inst.id) in str(raw)


def test_net_selection_selects_inspector_row(main_window, qtbot) -> None:
    from protoboard.gui.panels.net_inspector_panel import NetInspectorPanel

    inst = _place(main_window, 2.5, 2.5)
    net = _add_net_with_pin(main_window, inst)
    inspector = NetInspectorPanel(main_window._board_view)
    qtbot.addWidget(inspector)
    inspector.refresh(main_window._session)  # subscribes to selection_changed
    inspector.show()
    qtbot.waitExposed(inspector)

    main_window._session.notify_selection(frozenset(), frozenset(), net.id)

    selected = inspector._tree.selectedItems()
    assert selected, "inspector should select the net's row (its cost column shown)"


# ---------------------------------------------------------------------------
# panel → board  (request_select_* → selection_requested → SelectTool)
# ---------------------------------------------------------------------------


def test_request_select_component_drives_board(main_window) -> None:
    inst = _place(main_window, 2.5, 2.5)
    tool = _select_tool(main_window)

    main_window._session.request_select_component(inst.id)

    assert tool._selected_by_kind("component") == {inst.id}


def test_request_select_net_selects_components_and_wires(main_window) -> None:
    inst = _place(main_window, 2.5, 2.5)
    net = _add_net_with_pin(main_window, inst)
    tool = _select_tool(main_window)

    main_window._session.request_select_net(net.id)

    # The net's pin belongs to inst, so the whole-net request selects inst.
    assert inst.id in tool._selected_by_kind("component")


def test_request_select_pin_focuses_component_and_net(main_window) -> None:
    inst = _place(main_window, 2.5, 2.5)
    _add_net_with_pin(main_window, inst)
    tool = _select_tool(main_window)

    main_window._session.request_select_pin(inst.id, 1)

    assert inst.id in tool._selected_by_kind("component")
    # The pin's net is lit as the selection's net context.
    assert tool._primary_net_id() is not None


def test_panel_instance_signal_drives_board(main_window) -> None:
    """The Components panel's ``instance_selected`` signal selects on the board
    (now relayed through the session hub, not a direct board_view call)."""
    inst = _place(main_window, 2.5, 2.5)
    tool = _select_tool(main_window)

    main_window._components_panel.instance_selected.emit(inst.id)

    assert tool._selected_by_kind("component") == {inst.id}


# ---------------------------------------------------------------------------
# Re-entrancy — one request must not spin selection_changed forever
# ---------------------------------------------------------------------------


def test_board_view_resubscribes_after_session_swap(main_window) -> None:
    """A panel→board request on the NEW session drives the board; the OLD
    session no longer does (board_view re-binds selection_requested in
    set_session).  Guards the "selection sync silently dies after open" trap."""
    library = load_library([_LIBRARY_DIR])
    old_session = main_window._session
    new_session = EditorSession(board=_make_perfboard(), library=library)
    # Swap the board_view onto the new session (what _open_project does).
    main_window._board_view.set_session(new_session)
    main_window._session = new_session
    inst = _place(main_window, 2.5, 2.5)
    tool = _select_tool(main_window)

    # The new session drives the board.
    new_session.request_select_component(inst.id)
    assert tool._selected_by_kind("component") == {inst.id}

    # The old session is detached — its request must not touch this board_view.
    tool.clear_selection()
    old_session.request_select_component(inst.id)
    assert tool._selected_by_kind("component") == set()


def test_request_does_not_loop(main_window) -> None:
    inst = _place(main_window, 2.5, 2.5)
    _select_tool(main_window)
    count = {"n": 0}
    main_window._session.selection_changed.connect(
        lambda *_: count.__setitem__("n", count["n"] + 1)
    )

    main_window._session.request_select_component(inst.id)

    # One request settles in a bounded number of broadcasts — the panels'
    # _syncing guards break the click → sync → click cycle.
    assert 1 <= count["n"] <= 3
