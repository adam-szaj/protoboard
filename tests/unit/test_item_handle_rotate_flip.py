# SPDX-License-Identifier: MIT
"""ItemId + handle model — ``get_item`` / ``get_items`` rotate / flip.

The unified addressing seam (``controller.get_item(ItemId)`` /
``get_items([ItemId, ...])``) that both the GUI tools and the CLI drive. A
handle offers only the operations its target supports (a ``WireHandle`` has no
``rotate``); a selection handle folds a heterogeneous set (groups + loose
components) into one atomic macro — which is what fixes the "can't rotate
several groups at once" bug the single ``group_id`` verb signature carried.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, PadLayers, SizePolicy
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.layout import ROOT_GROUP_ID
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.geometry import Point
from protoboard.services.editing import HeadlessSession
from protoboard.services.editing.item_handle import ItemId
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
    session.invalidate_graph()
    return inst.id


def _make_group(session: HeadlessSession, member_ids: list[uuid.UUID]) -> uuid.UUID:
    grp_id = uuid.uuid4()
    session.layout.groups[grp_id] = ComponentGroup(
        id=grp_id, name="G",
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


def test_rotate_two_groups_at_once() -> None:
    """Rotating a selection of two groups rotates BOTH (the multi-group bug).

    The legacy ``rotate(comp_ids, *, group_id=...)`` verb took a single
    ``group_id``, so a two-group selection only ever rotated the first. The
    ``get_items`` handle folds per-group ``RotateGroupCommand``s into one macro,
    so both turn — and undo reverts both in one step.
    """
    session = _session()
    c1 = _place(session, 2.0, 2.0, "R1")
    c2 = _place(session, 5.0, 5.0, "R2")
    g1 = _make_group(session, [c1])
    g2 = _make_group(session, [c2])

    session.edit_controller.get_items(
        [ItemId.group(g1), ItemId.group(g2)]
    ).rotate(90)

    assert session.layout.placements[c1].rotation == 90
    assert session.layout.placements[c2].rotation == 90  # legacy bug: stayed 0


def test_rotate_two_groups_is_one_undo_step() -> None:
    """The two-group rotate is atomic — a single undo reverts both groups."""
    session = _session()
    c1 = _place(session, 2.0, 2.0, "R1")
    c2 = _place(session, 5.0, 5.0, "R2")
    g1 = _make_group(session, [c1])
    g2 = _make_group(session, [c2])

    session.edit_controller.get_items(
        [ItemId.group(g1), ItemId.group(g2)]
    ).rotate(90)
    session.stack.undo(session)

    assert session.layout.placements[c1].rotation == 0
    assert session.layout.placements[c2].rotation == 0


def test_redo_rotate_reapplies_original_target_not_current_selection() -> None:
    """redo re-rotates the ORIGINAL target, independent of any later selection.

    Guards the "rotate operates on selection, selection is not part of undo"
    design: because a rotate command freezes its target ids at construction, the
    ``rotate([c1]); undo; (address c2); redo`` sequence re-rotates c1 — never c2.
    Previously untested (no rotate/flip command was ever redone in the suite).
    """
    session = _session()
    c1 = _place(session, 2.0, 2.0, "R1")
    c2 = _place(session, 5.0, 5.0, "R2")

    session.edit_controller.get_item(ItemId.component(c1)).rotate(90)
    assert session.layout.placements[c1].rotation == 90

    session.stack.undo(session)
    assert session.layout.placements[c1].rotation == 0

    # "Change selection" to c2 (address a different item) before redo — the
    # command's frozen target must win, so c1 turns and c2 stays put.
    session.stack.redo(session)
    assert session.layout.placements[c1].rotation == 90
    assert session.layout.placements[c2].rotation == 0


def test_rotate_execute_undo_redo_round_trip() -> None:
    """A plain execute → undo → redo round-trip on a loose rotate (was untested)."""
    session = _session()
    cid = _place(session, 2.5, 2.5, "R1")

    session.edit_controller.get_item(ItemId.component(cid)).rotate(90)
    session.stack.undo(session)
    assert session.layout.placements[cid].rotation == 0
    session.stack.redo(session)
    assert session.layout.placements[cid].rotation == 90


def test_flip_group_execute_undo_redo_round_trip() -> None:
    """execute → undo → redo on a group flip restores then re-applies (was untested)."""
    session = _session()
    cid = _place(session, 2.5, 2.5, "R1")
    gid = _make_group(session, [cid])
    before = session.layout.placements[cid].side

    session.edit_controller.get_item(ItemId.group(gid)).flip()
    flipped_side = session.layout.placements[cid].side
    assert flipped_side != before

    session.stack.undo(session)
    assert session.layout.placements[cid].side == before
    session.stack.redo(session)
    assert session.layout.placements[cid].side == flipped_side


def test_wire_handle_has_no_rotate() -> None:
    """A wire target's handle offers no rotate / flip — the op is undefined for it."""
    session = _session()
    handle = session.edit_controller.get_item(ItemId.wire(uuid.uuid4()))
    assert not hasattr(handle, "rotate")
    assert not hasattr(handle, "flip")


def test_itemid_str_roundtrip() -> None:
    """ItemId's ``<tag>:<uuid>`` string form parses back to an equal ItemId."""
    iid = ItemId.group(uuid.uuid4())
    assert str(iid) == f"group:{iid.uuid}"
    assert ItemId.parse(str(iid)) == iid
