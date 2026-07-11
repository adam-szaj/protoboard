# SPDX-License-Identifier: MIT
"""EditController.delete — group-lock parity, driven headless (no Qt).

Mirrors the SelectTool group-lock delete parity (test_select_tool_parity ::
TestGroupedComponentProtection) at the controller seam: the same veto /
filter / unlock-after-entering behaviour, exercised directly on a
:class:`HeadlessSession` with no display server. The tool tests keep asserting
the same behaviour through the tool, which now delegates here.
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


def _perfboard(rows: int = 5, cols: int = 5) -> Board:
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
    """Add an "R" instance + placement at (x, y); returns its component id."""
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
    """Wrap *member_ids* in a new non-root group, child of ROOT."""
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


def test_delete_loose_component() -> None:
    session = _session()
    cid = _place(session, 0.5, 0.5, "R1")
    ran = session.edit_controller.delete(
        [ItemId.component(cid)], active_group_id=ROOT_GROUP_ID,
    )
    assert ran is True
    assert cid not in session.layout.placements


def test_delete_vetoed_for_grouped_from_outside() -> None:
    session = _session()
    cid = _place(session, 0.5, 0.5, "R1")
    _make_group(session, [cid])
    ran = session.edit_controller.delete(
        [ItemId.component(cid)], active_group_id=ROOT_GROUP_ID,
    )
    assert ran is False  # nothing survived the group-lock filter
    assert cid in session.layout.placements  # mutation blocked


def test_multi_delete_filters_grouped() -> None:
    session = _session()
    free = _place(session, 0.5, 0.5, "R1")
    grouped = _place(session, 1.5, 0.5, "R2")
    _make_group(session, [grouped])
    ran = session.edit_controller.delete(
        [ItemId.component(free), ItemId.component(grouped)],
        active_group_id=ROOT_GROUP_ID,
    )
    assert ran is True
    assert free not in session.layout.placements   # free deleted
    assert grouped in session.layout.placements    # grouped survives


def test_delete_works_after_entering_group() -> None:
    session = _session()
    cid = _place(session, 0.5, 0.5, "R1")
    gid = _make_group(session, [cid])
    # Active context is the group itself → its direct members are mutable.
    ran = session.edit_controller.delete(
        [ItemId.component(cid)], active_group_id=gid,
    )
    assert ran is True
    assert cid not in session.layout.placements


def test_stale_ids_are_dropped() -> None:
    """Ids no longer in the layout are filtered, not crashed on."""
    session = _session()
    ran = session.edit_controller.delete(
        [ItemId.component(uuid.uuid4()), ItemId.wire(uuid.uuid4())],
        active_group_id=ROOT_GROUP_ID,
    )
    assert ran is False


def test_force_does_not_override_component_group_lock() -> None:
    """force (Shift+Delete) unlocks *wires* only — a grouped component still
    survives, matching the legacy tool contract (component filter has no force
    branch)."""
    session = _session()
    cid = _place(session, 0.5, 0.5, "R1")
    _make_group(session, [cid])
    ran = session.edit_controller.delete(
        [ItemId.component(cid)], active_group_id=ROOT_GROUP_ID, force=True,
    )
    assert ran is False
    assert cid in session.layout.placements
