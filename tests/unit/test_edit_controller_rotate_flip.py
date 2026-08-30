# SPDX-License-Identifier: MIT
"""EditController.rotate / flip — dispatch + group-lock parity, headless (no Qt).

Mirrors the SelectTool multi rotate/flip parity (the ``_rotate_multi`` /
``_flip_multi`` dispatch: pivot -> group -> loose, group-lock filtered) at the
controller seam, exercised directly on a :class:`HeadlessSession`. The tool
tests keep asserting the same behaviour through the tool, which now delegates
here.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from protoboard.controller.editing import HeadlessSession
from protoboard.controller.editing.item_handle import ItemId
from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, PadLayers, SizePolicy
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.layout import ROOT_GROUP_ID
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.geometry import Point
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


def _make_group(session: HeadlessSession, member_ids: list[uuid.UUID]) -> uuid.UUID:
    grp_id = uuid.uuid4()
    session.layout.groups[grp_id] = ComponentGroup(
        id=grp_id,
        name="G1",
        component_ids=frozenset(member_ids),
        child_group_ids=frozenset(),
        exported_pins=frozenset(),
    )
    root = session.layout.groups[ROOT_GROUP_ID]
    session.layout.groups[ROOT_GROUP_ID] = ComponentGroup(
        id=ROOT_GROUP_ID,
        name=root.name,
        component_ids=root.component_ids - frozenset(member_ids),
        child_group_ids=root.child_group_ids | {grp_id},
        exported_pins=root.exported_pins,
    )
    session.invalidate_graph()
    return grp_id


def test_rotate_loose_component_in_place() -> None:
    session = _session()
    cid = _place(session, 2.5, 2.5, "R1")
    session.edit_controller.rotate(
        [ItemId.component(cid)], delta_deg=90, active_group_id=ROOT_GROUP_ID
    )
    assert session.layout.placements[cid].rotation == 90


def test_rotate_vetoed_for_grouped_from_outside() -> None:
    session = _session()
    cid = _place(session, 2.5, 2.5, "R1")
    _make_group(session, [cid])
    rot0 = session.layout.placements[cid].rotation
    session.edit_controller.rotate(
        [ItemId.component(cid)], delta_deg=90, active_group_id=ROOT_GROUP_ID
    )
    assert session.layout.placements[cid].rotation == rot0  # blocked


def test_rotate_works_after_entering_group() -> None:
    session = _session()
    cid = _place(session, 2.5, 2.5, "R1")
    gid = _make_group(session, [cid])
    # Rotating the group as a rigid body (group tag → rigid, no lock filter).
    session.edit_controller.rotate(
        [ItemId.group(gid)],
        delta_deg=90,
        active_group_id=ROOT_GROUP_ID,
    )
    assert session.layout.placements[cid].rotation == 90


def test_rotate_around_pivot() -> None:
    session = _session()
    cid = _place(session, 2.5, 2.5, "R1")
    session.edit_controller.rotate(
        [ItemId.component(cid)],
        delta_deg=90,
        pivot=Point(0.5, 0.5),
        active_group_id=ROOT_GROUP_ID,
    )
    # A pivot rotation moves the component off its original cell.
    assert session.layout.placements[cid].position != Point(2.5, 2.5)
    assert session.layout.placements[cid].rotation == 90


def test_rotate_empty_selection_is_noop() -> None:
    session = _session()
    removed = session.edit_controller.rotate(
        [],
        delta_deg=90,
        active_group_id=ROOT_GROUP_ID,
    )
    assert removed == 0


def test_flip_loose_two_pin_component() -> None:
    session = _session()
    cid = _place(session, 2.5, 2.5, "R1")
    before = session.layout.placements[cid]
    session.edit_controller.flip([ItemId.component(cid)], active_group_id=ROOT_GROUP_ID)
    after = session.layout.placements[cid]
    # A 2-pin flip mirrors the placement about the pin span → something changes.
    assert (after.position, after.rotation, after.vertical_flipped) != (
        before.position,
        before.rotation,
        before.vertical_flipped,
    )


def test_flip_vetoed_for_grouped_from_outside() -> None:
    session = _session()
    cid = _place(session, 2.5, 2.5, "R1")
    _make_group(session, [cid])
    before = session.layout.placements[cid]
    session.edit_controller.flip([ItemId.component(cid)], active_group_id=ROOT_GROUP_ID)
    assert session.layout.placements[cid] == before  # blocked
