# SPDX-License-Identifier: MIT
"""Handle type-dispatch — typed getters + the ``.call()`` visitor.

``get_group`` / ``get_component`` / ``get_wire`` return a narrow handle (and
raise if the id is the wrong kind), so a caller that knows the type gets it
without a union. ``.call(component_call=…, group_call=…, wire_call=…)`` is the
per-type visitor for the generic case: the handle invokes the one callback for
its own kind (with a correctly-narrowed argument), so callers never branch on
type themselves.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, PadLayers, SizePolicy
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.layout import ROOT_GROUP_ID
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import Wire
from protoboard.geometry import Point
from protoboard.services.editing import HeadlessSession
from protoboard.services.editing.item_handle import (
    ComponentHandle,
    GroupHandle,
    ItemId,
    WireHandle,
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


def _wire(session: HeadlessSession) -> uuid.UUID:
    w = Wire(id=uuid.uuid4(), path=[Point(0.0, 0.0), Point(2.0, 0.0)], side="top")
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


# -- typed getters -----------------------------------------------------------


def test_get_group_returns_group_handle() -> None:
    session = _session()
    gid = _make_group(session, [_place(session, 0.5, 0.5, "R1")])
    handle = session.edit_controller.get_group(gid)
    assert isinstance(handle, GroupHandle)
    assert handle.name == "G1"


def test_get_component_returns_component_handle() -> None:
    session = _session()
    cid = _place(session, 0.5, 0.5, "R1")
    handle = session.edit_controller.get_component(cid)
    assert isinstance(handle, ComponentHandle)


def test_get_wire_returns_wire_handle() -> None:
    session = _session()
    wid = _wire(session)
    handle = session.edit_controller.get_wire(wid)
    assert isinstance(handle, WireHandle)


def test_get_group_raises_for_non_group_id() -> None:
    session = _session()
    cid = _place(session, 0.5, 0.5, "R1")  # a component id, not a group
    with pytest.raises(Exception):
        session.edit_controller.get_group(cid)


# -- .call() visitor ---------------------------------------------------------


def test_call_invokes_matching_callback() -> None:
    session = _session()
    cid = _place(session, 0.5, 0.5, "R1")
    gid = _make_group(session, [_place(session, 1.5, 0.5, "R2")])
    wid = _wire(session)

    assert session.edit_controller.get_item(ItemId.component(cid)).call(
        component_call=lambda c: ("comp", c.refdes),
        group_call=lambda g: ("group", g.name),
        wire_call=lambda w: ("wire", w.side),
    ) == ("comp", "R1")

    assert session.edit_controller.get_item(ItemId.group(gid)).call(
        component_call=lambda c: ("comp", c.refdes),
        group_call=lambda g: ("group", g.name),
        wire_call=lambda w: ("wire", w.side),
    ) == ("group", "G1")

    assert session.edit_controller.get_item(ItemId.wire(wid)).call(
        component_call=lambda c: ("comp", c.refdes),
        group_call=lambda g: ("group", g.name),
        wire_call=lambda w: ("wire", w.side),
    ) == ("wire", "top")


def test_call_missing_callback_for_hit_type_is_noop() -> None:
    """A None callback for the handle's own kind is a no-op returning None."""
    session = _session()
    gid = _make_group(session, [_place(session, 0.5, 0.5, "R1")])
    result = session.edit_controller.get_item(ItemId.group(gid)).call(
        component_call=lambda c: "comp",  # group_call omitted → no-op
    )
    assert result is None


def test_call_with_no_callbacks_raises() -> None:
    """At least one callback must be supplied."""
    session = _session()
    cid = _place(session, 0.5, 0.5, "R1")
    with pytest.raises(Exception):
        session.edit_controller.get_item(ItemId.component(cid)).call()
