# SPDX-License-Identifier: MIT
"""``Layout.preview_copy`` — the scratch fork the armed transform mutates (E5).

The armed rotate/move/flip mutates a *preview* layout so the session's stays
pristine.  These assert the fork is genuinely independent for exactly the pose
data a transform writes — placements, wire path/fragments, knots — while sharing
the read-only structure (groups).  The wire case is the subtle one: a shared
``Wire`` object would let ``transform_wire_geometry`` corrupt the live path.
"""

from __future__ import annotations

import uuid

from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import Wire, transform_wire_geometry
from protoboard.geometry import Point


def _placement(cid: uuid.UUID, x: float, y: float) -> ComponentPlacement:
    return ComponentPlacement(
        component_id=cid, position=Point(x, y), rotation=0, side="top",
    )


def test_preview_placement_write_leaves_live_untouched() -> None:
    cid = uuid.uuid4()
    live = Layout(placements={cid: _placement(cid, 1.0, 1.0)})  # type: ignore[arg-type]

    preview = live.preview_copy()
    preview.placements[cid] = _placement(cid, 5.0, 5.0)

    assert preview.placements[cid].position == Point(5.0, 5.0)
    assert live.placements[cid].position == Point(1.0, 1.0)


def test_preview_wire_transform_does_not_alias_live_geometry() -> None:
    wid = uuid.uuid4()
    wire = Wire(id=wid, path=[Point(0.0, 0.0), Point(1.0, 0.0)], side="top")
    live = Layout(wires=[wire])  # type: ignore[arg-type]

    preview = live.preview_copy()
    # Translate the preview's copy of the wire by (10, 10).
    transform_wire_geometry(
        preview.wires[wid], lambda p: Point(p.x + 10.0, p.y + 10.0)
    )

    assert preview.wires[wid].path[0] == Point(10.0, 10.0)
    # The live wire's path (and its fragments) are unchanged — no shared list.
    assert live.wires[wid].path[0] == Point(0.0, 0.0)
    assert live.wires[wid].fragments[0].path[0] == Point(0.0, 0.0)


def test_preview_shares_groups_by_reference() -> None:
    from protoboard.domain.layout.layout import ROOT_GROUP_ID

    live = Layout()
    live.ensure_root_group()
    preview = live.preview_copy()
    # Structural read-only data is shared (the world-transform basis a transform
    # reads but never rewrites mid-gesture).
    assert preview.groups is live.groups
    assert ROOT_GROUP_ID in preview.groups
