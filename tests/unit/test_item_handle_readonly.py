# SPDX-License-Identifier: MIT
"""ReadOnlyComponentHandle — a component addressed from outside its group.

A component sealed inside a group (viewed from outside its active context) is
selectable but read-only: ``get_item`` hands back a handle that exposes the
component's properties (value / part-number / pin-names / refdes) but has NO
mutating op — rotate / flip / delete are absent by type, not a silent no-op.
Entering the group (active_group == the group) makes the same component
editable, and ``get_item`` returns the full ``ComponentHandle``.
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
from protoboard.services.editing.item_handle import (
    ComponentHandle,
    ItemId,
    ReadOnlyComponentHandle,
)
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


def _place(
    session: HeadlessSession, x: float, y: float, refdes: str, value: str = "10k"
) -> uuid.UUID:
    defn = session.components["R"]
    inst = ComponentInstance.new(
        definition_ref="R", refdes=refdes, value=value,
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


# -- which handle type get_item returns --------------------------------------


def test_free_component_is_editable() -> None:
    session = _session()
    cid = _place(session, 0.5, 0.5, "R1")
    handle = session.edit_controller.get_item(ItemId.component(cid))
    assert isinstance(handle, ComponentHandle)
    assert hasattr(handle, "rotate")


def test_sealed_component_from_outside_is_read_only() -> None:
    session = _session()
    cid = _place(session, 0.5, 0.5, "R1")
    _make_group(session, [cid])

    handle = session.edit_controller.get_item(
        ItemId.component(cid), active_group_id=ROOT_GROUP_ID
    )
    assert isinstance(handle, ReadOnlyComponentHandle)
    assert not isinstance(handle, ComponentHandle)
    assert not hasattr(handle, "rotate")
    assert not hasattr(handle, "flip")
    assert not hasattr(handle, "delete")


def test_sealed_component_editable_after_entering_group() -> None:
    session = _session()
    cid = _place(session, 0.5, 0.5, "R1")
    gid = _make_group(session, [cid])

    handle = session.edit_controller.get_item(
        ItemId.component(cid), active_group_id=gid
    )
    assert isinstance(handle, ComponentHandle)
    assert hasattr(handle, "rotate")


# -- read-only handle exposes properties -------------------------------------


def test_read_only_handle_exposes_properties() -> None:
    session = _session()
    cid = _place(session, 0.5, 0.5, "R7", value="4k7")
    _make_group(session, [cid])

    handle = session.edit_controller.get_item(
        ItemId.component(cid), active_group_id=ROOT_GROUP_ID
    )
    assert handle.value == "4k7"
    assert handle.refdes == "R7"
    assert handle.part_number == "R"
    assert isinstance(handle.pin_names, dict)


def test_editable_handle_exposes_same_reads() -> None:
    """The editable handle is a superset — it exposes the read-only reads too."""
    session = _session()
    cid = _place(session, 0.5, 0.5, "R1", value="1k")
    handle = session.edit_controller.get_item(ItemId.component(cid))
    assert isinstance(handle, ComponentHandle)
    assert handle.value == "1k"
    assert handle.refdes == "R1"
    assert handle.part_number == "R"
    assert isinstance(handle.pin_names, dict)


# -- group / wire handle read properties -------------------------------------


def test_group_handle_exposes_name_and_members() -> None:
    session = _session()
    c1 = _place(session, 0.5, 0.5, "R1")
    c2 = _place(session, 1.5, 0.5, "R2")
    gid = _make_group(session, [c1, c2])

    handle = session.edit_controller.get_item(ItemId.group(gid))
    assert handle.name == "G1"
    assert handle.component_ids == frozenset({c1, c2})


def test_wire_handle_exposes_side_and_insulated() -> None:
    from protoboard.domain.layout.wire import Wire

    session = _session()
    w = Wire(
        id=uuid.uuid4(), path=[Point(0.0, 0.0), Point(2.0, 0.0)],
        insulated=True, side="bottom",
    )
    session.layout.wires.append(w)
    session.invalidate_graph()

    handle = session.edit_controller.get_item(ItemId.wire(w.id))
    assert handle.side == "bottom"
    assert handle.insulated is True


# -- HandleKind predicates (is_* / supports_*) -------------------------------
#
# The predicate matrix get_items() callers filter on, one row per handle kind:
#                      component group wire  readonly mutable | rot flip del
#   ComponentHandle        T      F    F       F        T     |  T   T   T
#   ReadOnlyComponent      T      F    F       T        F     |  F   F   F
#   GroupHandle            F      T    F       F        T     |  T   T   T
#   WireHandle             F      F    T       F        T     |  F   F   T


def _wire(session: HeadlessSession) -> uuid.UUID:
    from protoboard.domain.layout.wire import Wire

    w = Wire(
        id=uuid.uuid4(), path=[Point(0.0, 0.0), Point(2.0, 0.0)],
        insulated=False, side="top",
    )
    session.layout.wires.append(w)
    session.invalidate_graph()
    return w.id


def test_mutable_component_handle_predicates() -> None:
    session = _session()
    cid = _place(session, 0.5, 0.5, "R1")
    h = session.edit_controller.get_item(ItemId.component(cid))
    assert (h.is_component(), h.is_group(), h.is_wire()) == (True, False, False)
    assert (h.is_readonly(), h.is_mutable()) == (False, True)
    assert (h.supports_rotate(), h.supports_flip(), h.supports_delete()) == (
        True, True, True,
    )


def test_readonly_component_handle_predicates() -> None:
    session = _session()
    cid = _place(session, 0.5, 0.5, "R1")
    _make_group(session, [cid])
    h = session.edit_controller.get_item(
        ItemId.component(cid), active_group_id=ROOT_GROUP_ID
    )
    assert h.is_component() and h.is_readonly() and not h.is_mutable()
    assert not (h.supports_rotate() or h.supports_flip() or h.supports_delete())


def test_group_handle_predicates() -> None:
    session = _session()
    cid = _place(session, 0.5, 0.5, "R1")
    gid = _make_group(session, [cid])
    h = session.edit_controller.get_item(ItemId.group(gid))
    assert (h.is_component(), h.is_group(), h.is_wire()) == (False, True, False)
    assert (h.is_readonly(), h.is_mutable()) == (False, True)
    assert (h.supports_rotate(), h.supports_flip(), h.supports_delete()) == (
        True, True, True,
    )


def test_wire_handle_predicates() -> None:
    session = _session()
    wid = _wire(session)
    h = session.edit_controller.get_item(ItemId.wire(wid))
    assert (h.is_component(), h.is_group(), h.is_wire()) == (False, False, True)
    assert (h.is_readonly(), h.is_mutable()) == (False, True)
    assert (h.supports_rotate(), h.supports_flip(), h.supports_delete()) == (
        False, False, True,
    )


def test_get_items_mixed_selection_filters_by_predicate() -> None:
    """The intended caller pattern: filter a mixed get_items() list by predicate."""
    session = _session()
    c_free = _place(session, 0.5, 0.5, "R1")
    c_sealed = _place(session, 2.5, 2.5, "R2")
    gid = _make_group(session, [c_sealed])
    wid = _wire(session)

    handles = session.edit_controller.get_items(
        [ItemId.component(c_free), ItemId.group(gid), ItemId.wire(wid)],
        active_group_id=ROOT_GROUP_ID,
    )
    assert len(handles) == 3
    assert [h for h in handles if h.supports_rotate()]  # free comp + group
    assert len([h for h in handles if h.is_wire()]) == 1
    assert all(h.is_mutable() for h in handles)  # none sealed in this set
