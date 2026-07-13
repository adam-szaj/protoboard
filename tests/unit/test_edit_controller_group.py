# SPDX-License-Identifier: MIT
"""EditController.create_group / add_to_group — promotion parity, headless (no Qt).

Exercises the verbs that absorb SelectTool's _prepare_new_group_promoted /
_execute_group_promotion and the add-to-existing branch, driven directly on a
:class:`HeadlessSession`. The GUI GroupTargetDialog and select-the-result side
effect stay in the tool; here the verbs are called with the choice already made.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, PadLayers, SizePolicy
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.group import all_component_ids
from protoboard.domain.layout.layout import ROOT_GROUP_ID
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.geometry import Point
from protoboard.controller.editing import HeadlessSession
from protoboard.controller.editing.item_handle import ItemId
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


def _session() -> HeadlessSession:
    library = load_library([_LIBRARY_DIR])
    return HeadlessSession(board=_perfboard(), library=library)


def _place(session: HeadlessSession, x: float, y: float, refdes: str) -> uuid.UUID:
    defn = session.components["R"]
    inst = ComponentInstance.new(
        definition_ref="R", refdes=refdes, value="",
    ).bind(defn, session.package_families)
    session.instances[inst.id] = inst
    session.layout.placements[inst.id] = ComponentPlacement(
        component_id=inst.id, position=Point(x, y), rotation=0, side="top",
    )
    # Root group tracks free components as direct members (the GUI seeds this).
    root = session.layout.groups[ROOT_GROUP_ID]
    session.layout.groups[ROOT_GROUP_ID] = root.__class__(
        id=ROOT_GROUP_ID, name=root.name,
        component_ids=root.component_ids | {inst.id},
        child_group_ids=root.child_group_ids,
        exported_pins=root.exported_pins,
    )
    session.invalidate_graph()
    return inst.id


def test_create_group_empty_selection_returns_none() -> None:
    session = _session()
    assert session.edit_controller.create_group(
        [], active_group_id=ROOT_GROUP_ID, name="X",
    ) is None


def test_create_group_promotes_and_detaches_from_root() -> None:
    session = _session()
    c1 = _place(session, 1.0, 1.0, "R1")
    c2 = _place(session, 3.0, 1.0, "R2")

    gid = session.edit_controller.create_group(
        [ItemId.component(c1), ItemId.component(c2)], active_group_id=ROOT_GROUP_ID, name="G1",
    )
    assert gid is not None
    grp = session.layout.groups[gid]
    # The two components are now direct members of the new group…
    assert grp.component_ids == frozenset({c1, c2})
    # …the new group is a child of ROOT, and ROOT no longer holds them directly.
    root = session.layout.groups[ROOT_GROUP_ID]
    assert gid in root.child_group_ids
    assert c1 not in root.component_ids and c2 not in root.component_ids
    # The recursive collector still reports both under the new group.
    assert all_component_ids(session.layout.groups, gid) == frozenset({c1, c2})


def test_create_group_undo_restores_flat_layout() -> None:
    session = _session()
    c1 = _place(session, 1.0, 1.0, "R1")
    c2 = _place(session, 3.0, 1.0, "R2")
    groups_before = len(session.layout.groups)

    gid = session.edit_controller.create_group(
        [ItemId.component(c1), ItemId.component(c2)], active_group_id=ROOT_GROUP_ID, name="G1",
    )
    assert gid is not None and len(session.layout.groups) == groups_before + 1

    # The promotion is a sequence of commands on the stack; undoing them all
    # restores the flat layout (create + reparent are individually reversible).
    while session.stack.can_undo():
        session.stack.undo(session)  # type: ignore[arg-type]
    assert gid not in session.layout.groups
    assert session.layout.groups[ROOT_GROUP_ID].component_ids >= frozenset({c1, c2})


def test_add_to_existing_group_moves_membership() -> None:
    session = _session()
    c1 = _place(session, 1.0, 1.0, "R1")
    c2 = _place(session, 3.0, 1.0, "R2")
    gid = session.edit_controller.create_group(
        [ItemId.component(c1)], active_group_id=ROOT_GROUP_ID, name="G1",
    )
    assert gid is not None

    session.edit_controller.add_to_group(
        gid, [ItemId.component(c2)], active_group_id=ROOT_GROUP_ID,
    )
    assert c2 in session.layout.groups[gid].component_ids
    assert c2 not in session.layout.groups[ROOT_GROUP_ID].component_ids


def test_add_to_group_empty_selection_is_noop() -> None:
    session = _session()
    c1 = _place(session, 1.0, 1.0, "R1")
    gid = session.edit_controller.create_group(
        [ItemId.component(c1)], active_group_id=ROOT_GROUP_ID, name="G1",
    )
    assert gid is not None
    before = session.layout.groups[gid].component_ids
    session.edit_controller.add_to_group(gid, [], active_group_id=ROOT_GROUP_ID)
    assert session.layout.groups[gid].component_ids == before
