# SPDX-License-Identifier: MIT
"""EditorSession selection() / select() — the GUI bridge to the ItemId API.

The GUI selection lives in a per-session ``Hit``-based ``SelectionService``
(parked by ``SelectTool.activate``). These verify that ``EditorSession`` presents
it through the controller's ``ItemId`` shape, so ``EditController.rotate()`` with
no ids operates on the GUI selection — the same contract the headless session
already satisfies.
"""

from __future__ import annotations

import uuid

from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.layout import ROOT_GROUP_ID
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.geometry import Point
from protoboard.gui.board_view import BoardView
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.select_tool import SelectTool
from protoboard.services.editing.item_handle import ItemId


def _activate_select(board_view: BoardView) -> SelectTool:
    """Activate a SelectTool so the session's SelectionService is parked."""
    board_view.set_tool(SelectTool())
    tool = board_view._active_tool
    assert isinstance(tool, SelectTool)
    return tool


def _place(session: EditorSession, x: float, y: float, refdes: str) -> uuid.UUID:
    defn = session.components["R"]
    inst = ComponentInstance.new(
        definition_ref="R", refdes=refdes, value="",
    ).bind(defn, session.package_families)
    session.instances[inst.id] = inst
    session.layout.placements[inst.id] = ComponentPlacement(
        component_id=inst.id, position=Point(x, y), rotation=0, side="top",
    )
    session.invalidate_graph()
    return inst.id


def _make_group(session: EditorSession, member_ids: list[uuid.UUID]) -> uuid.UUID:
    grp_id = uuid.uuid4()
    session.layout.groups[grp_id] = ComponentGroup(
        id=grp_id, name="G1",
        component_ids=frozenset(member_ids),
        child_group_ids=frozenset(),
        exported_pins=frozenset(),
    )
    root = session.layout.groups[ROOT_GROUP_ID]
    session.layout.groups[ROOT_GROUP_ID] = ComponentGroup(
        id=ROOT_GROUP_ID, name=root.name,
        component_ids=root.component_ids - frozenset(member_ids),
        child_group_ids=root.child_group_ids | {grp_id},
        exported_pins=root.exported_pins,
    )
    session.invalidate_graph()
    return grp_id


def test_selection_empty_before_any_tool_activates(session: EditorSession) -> None:
    """No SelectTool has activated → no service → empty selection, no crash."""
    assert session.selection_service is None
    assert session.selection() == frozenset()


def test_select_then_selection_roundtrips(
    board_view: BoardView, session: EditorSession
) -> None:
    """select(ids) writes the service; selection() reads the same ItemIds back."""
    _activate_select(board_view)
    c1 = _place(session, 2.5, 2.5, "R1")
    c2 = _place(session, 4.5, 4.5, "R2")

    session.select([ItemId.component(c1), ItemId.component(c2)])
    assert session.selection() == frozenset(
        {ItemId.component(c1), ItemId.component(c2)}
    )


def test_select_group_itemid_roundtrips(
    board_view: BoardView, session: EditorSession
) -> None:
    """A group ItemId round-trips through the Hit-based service."""
    _activate_select(board_view)
    cid = _place(session, 2.5, 2.5, "R1")
    gid = _make_group(session, [cid])

    session.select([ItemId.group(gid)])
    assert session.selection() == frozenset({ItemId.group(gid)})


def test_rotate_no_args_operates_on_gui_selection(
    board_view: BoardView, session: EditorSession
) -> None:
    """EditController.rotate() with no ids rotates the GUI session's selection."""
    _activate_select(board_view)
    c1 = _place(session, 2.5, 2.5, "R1")
    c2 = _place(session, 4.5, 4.5, "R2")

    session.select([ItemId.component(c1), ItemId.component(c2)])
    session.edit_controller.rotate()

    assert session.layout.placements[c1].rotation == 90
    assert session.layout.placements[c2].rotation == 90


def test_select_emits_selection_changed(
    board_view: BoardView, session: EditorSession
) -> None:
    """select() broadcasts to panels via selection_changed (comp ids payload)."""
    _activate_select(board_view)
    c1 = _place(session, 2.5, 2.5, "R1")
    seen: list[frozenset[uuid.UUID]] = []
    session.selection_changed.connect(lambda comp, wire, net: seen.append(comp))

    session.select([ItemId.component(c1)])

    assert seen and c1 in seen[-1]
