# SPDX-License-Identifier: MIT
"""ItemId handle model — delete (incl. group=disband) + create/add-to-group.

delete over the ItemId seam: a component / wire handle deletes its target, a
group handle *disbands* it (dissolve, members stay placed — the domain's only
"remove a group" op), and a mixed selection folds RemoveComponent / RemoveWire /
Disband into one atomic macro. create_group / add_to_group take Iterable[ItemId]
(component tags) — a unified boundary type, the new-vs-existing dialog stays in
the tool.
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
from protoboard.domain.layout.wire import Wire
from protoboard.geometry import Point
from protoboard.controller.editing import HeadlessSession
from protoboard.controller.editing.item_handle import ComponentHandle, ItemId
from protoboard.services.library.loader import load_library

_LIBRARY_DIR = Path(__file__).parent.parent / "fixtures" / "library"


def _perfboard(rows: int = 6, cols: int = 6) -> Board:
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
    return HeadlessSession(board=_perfboard(), library=load_library([_LIBRARY_DIR]))


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


def _wire(session: HeadlessSession, a: Point, b: Point) -> uuid.UUID:
    w = Wire(id=uuid.uuid4(), path=[a, b], insulated=False, side="top")
    session.layout.wires.append(w)
    session.invalidate_graph()
    return w.id


def _make_group(session: HeadlessSession, member_ids: list[uuid.UUID]) -> uuid.UUID:
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


# -- delete via handle -------------------------------------------------------


def test_component_handle_delete() -> None:
    session = _session()
    cid = _place(session, 0.5, 0.5, "R1")
    handle = session.edit_controller.get_component(cid)
    assert isinstance(handle, ComponentHandle)  # free → editable
    handle.delete()
    assert cid not in session.layout.placements


def test_wire_handle_delete() -> None:
    session = _session()
    wid = _wire(session, Point(0.0, 0.0), Point(2.0, 0.0))
    session.edit_controller.get_wire(wid).delete()
    assert wid not in {w.id for w in session.layout.wires}


def test_group_handle_delete_disbands_keeping_members() -> None:
    """Deleting a group disbands it — the group is gone, members stay placed."""
    session = _session()
    cid = _place(session, 0.5, 0.5, "R1")
    gid = _make_group(session, [cid])

    session.edit_controller.get_group(gid).delete()

    assert gid not in session.layout.groups          # group dissolved
    assert cid in session.layout.placements          # member kept


def test_selection_delete_mixed_is_one_undo_step() -> None:
    """A mixed component + wire + group delete folds into one atomic macro."""
    session = _session()
    c_free = _place(session, 0.5, 0.5, "R1")
    c_grouped = _place(session, 2.5, 2.5, "R2")
    gid = _make_group(session, [c_grouped])
    wid = _wire(session, Point(0.0, 0.0), Point(2.0, 0.0))

    session.edit_controller.delete(
        [ItemId.component(c_free), ItemId.wire(wid), ItemId.group(gid)],
        active_group_id=ROOT_GROUP_ID,
    )

    assert c_free not in session.layout.placements          # free comp deleted
    assert wid not in {w.id for w in session.layout.wires}  # wire deleted
    assert gid not in session.layout.groups                 # group disbanded
    assert c_grouped in session.layout.placements           # its member kept

    session.stack.undo(session)  # one step restores everything
    assert c_free in session.layout.placements
    assert wid in {w.id for w in session.layout.wires}
    assert gid in session.layout.groups


def test_delete_no_args_operates_on_selection() -> None:
    session = _session()
    cid = _place(session, 0.5, 0.5, "R1")
    session.select([ItemId.component(cid)])
    session.edit_controller.delete(active_group_id=ROOT_GROUP_ID)
    assert cid not in session.layout.placements


def test_wire_handle_has_no_rotate_but_has_delete() -> None:
    session = _session()
    handle = session.edit_controller.get_item(ItemId.wire(uuid.uuid4()))
    assert hasattr(handle, "delete")
    assert not hasattr(handle, "rotate")


# -- per-kind delete policy: the guard differs by tag ------------------------


def test_component_locked_in_group_is_not_deleted_from_outside() -> None:
    """The component policy's lock guard: a sealed component survives delete."""
    session = _session()
    cid = _place(session, 0.5, 0.5, "R1")
    _make_group(session, [cid])

    # Address it from ROOT (outside the group) — the lock guard drops it.
    ran = session.edit_controller.delete(
        [ItemId.component(cid)], active_group_id=ROOT_GROUP_ID
    )
    assert ran is False
    assert cid in session.layout.placements


def test_force_does_not_lift_the_component_lock() -> None:
    """*force* lifts the WIRE lock only — a locked component still survives it."""
    session = _session()
    cid = _place(session, 0.5, 0.5, "R1")
    _make_group(session, [cid])

    ran = session.edit_controller.delete(
        [ItemId.component(cid)], active_group_id=ROOT_GROUP_ID, force=True
    )
    assert ran is False
    assert cid in session.layout.placements


# -- create_group / add_to_group on ItemId -----------------------------------


def test_create_group_takes_item_ids() -> None:
    session = _session()
    c1 = _place(session, 0.5, 0.5, "R1")
    c2 = _place(session, 1.5, 0.5, "R2")

    gid = session.edit_controller.create_group(
        [ItemId.component(c1), ItemId.component(c2)],
        active_group_id=ROOT_GROUP_ID, name="Amp",
    )
    assert gid is not None
    grp = session.layout.groups[gid]
    assert grp.component_ids == frozenset({c1, c2})
    assert gid in session.layout.groups[ROOT_GROUP_ID].child_group_ids


def test_add_to_group_takes_item_ids() -> None:
    session = _session()
    c1 = _place(session, 0.5, 0.5, "R1")
    c2 = _place(session, 1.5, 0.5, "R2")
    gid = session.edit_controller.create_group(
        [ItemId.component(c1)], active_group_id=ROOT_GROUP_ID, name="Amp",
    )
    assert gid is not None

    session.edit_controller.add_to_group(
        gid, [ItemId.component(c2)], active_group_id=ROOT_GROUP_ID,
    )
    assert c2 in session.layout.groups[gid].component_ids
