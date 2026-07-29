# SPDX-License-Identifier: MIT
"""Tests for HitTester — resolving a board point to a Selectable (headless).

A fake HitContext supplies controllable placements / pins / wires / groups, so
the priority order (pin > wire > group > bbox), thresholds and camera-side
preference are exercised without a session or QApplication.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import Wire
from protoboard.geometry import Point, Rect
from protoboard.controller.selection import HitTester, SelectionFilters
from protoboard.controller.selection.adapters import (
    ComponentSelectable,
    GroupSelectable,
    WireSelectable,
)

if TYPE_CHECKING:
    from protoboard.controller.selection.hit_test import HitContext
    from protoboard.types import Side


def _tester(ctx: _FakeCtx, *, camera_side: str = "top") -> HitTester:
    """Build a HitTester over the minimal fake, satisfying the protocol type.

    ``_FakeCtx`` structurally provides every method HitTester reads, but it is
    deliberately narrower than the full :class:`HitContext` protocol (e.g. its
    ``layout`` omits ``wires``), so cast it at the boundary the tester accepts.
    """
    return HitTester(cast("HitContext", ctx), camera_side=cast("Side", camera_side))


@dataclass
class _FakeLayout:
    placements: dict[uuid.UUID, ComponentPlacement] = field(default_factory=dict)
    groups: dict[uuid.UUID, ComponentGroup] = field(default_factory=dict)


@dataclass
class _FakeCtx:
    """Minimal HitContext — geometry the tester reads, all controllable."""

    layout: _FakeLayout = field(default_factory=_FakeLayout)
    _pins: dict[uuid.UUID, list[tuple[int, Point]]] = field(default_factory=dict)
    _wires: dict[uuid.UUID, Wire] = field(default_factory=dict)
    _bboxes: dict[uuid.UUID, Rect] = field(default_factory=dict)

    def comp_spatial_index(self) -> dict[tuple[int, int], list[uuid.UUID]]:
        # Bucket every component into the cell of its first pin.
        idx: dict[tuple[int, int], list[uuid.UUID]] = {}
        for cid, pins in self._pins.items():
            if not pins:
                continue
            p = pins[0][1]
            idx.setdefault((round(p.x), round(p.y)), []).append(cid)
        return idx

    def wire_spatial_index(self) -> dict[tuple[int, int], list[uuid.UUID]]:
        # Bucket every cell each segment passes through (coarse rasterise),
        # matching the real index — vertices alone would miss mid-segment.
        idx: dict[tuple[int, int], list[uuid.UUID]] = {}
        for wid, w in self._wires.items():
            for i in range(len(w.path) - 1):
                a, b = w.path[i], w.path[i + 1]
                steps = int(max(abs(b.x - a.x), abs(b.y - a.y))) + 1
                for s in range(steps + 1):
                    t = s / steps
                    cx = round(a.x + (b.x - a.x) * t)
                    cy = round(a.y + (b.y - a.y) * t)
                    bucket = idx.setdefault((cx, cy), [])
                    if wid not in bucket:
                        bucket.append(wid)
        return idx

    def wire_by_id(self, wire_id: uuid.UUID) -> Wire | None:
        return self._wires.get(wire_id)

    def iter_pin_positions(self, comp_id: uuid.UUID) -> list[tuple[int, Point]]:
        return self._pins.get(comp_id, [])

    def component_bbox(self, comp_id: uuid.UUID) -> Rect | None:
        return self._bboxes.get(comp_id)


def _placement(cid: uuid.UUID, side: str = "top") -> ComponentPlacement:
    return ComponentPlacement(
        component_id=cid, position=Point(0.0, 0.0), rotation=0, side=side  # type: ignore[arg-type]
    )


def _add_component(
    ctx: _FakeCtx, *, pin_xy: tuple[float, float], side: str = "top"
) -> uuid.UUID:
    cid = uuid.uuid4()
    ctx.layout.placements[cid] = _placement(cid, side)
    ctx._pins[cid] = [(1, Point(*pin_xy))]
    return cid


def _add_wire(ctx: _FakeCtx, path: list[tuple[float, float]]) -> uuid.UUID:
    wid = uuid.uuid4()
    ctx._wires[wid] = Wire(id=wid, path=[Point(x, y) for x, y in path])
    return wid


class TestHitComponent:
    def test_pin_within_radius_hits(self) -> None:
        ctx = _FakeCtx()
        cid = _add_component(ctx, pin_xy=(5.0, 5.0))
        sel = _tester(ctx).hit(5.2, 5.1)  # within 0.5
        assert isinstance(sel, ComponentSelectable)
        assert sel.component_id == cid

    def test_pin_outside_radius_misses(self) -> None:
        ctx = _FakeCtx()
        _add_component(ctx, pin_xy=(5.0, 5.0))
        # 0.6 away in x → beyond HIT_RADIUS 0.5, and no bbox set
        assert _tester(ctx).hit(5.6, 5.0) is None

    def test_camera_side_preferred_over_back(self) -> None:
        ctx = _FakeCtx()
        back = _add_component(ctx, pin_xy=(5.0, 5.0), side="bottom")
        top = _add_component(ctx, pin_xy=(5.05, 5.0), side="top")
        # Top is camera side; even though both within radius, top wins.
        sel = _tester(ctx, camera_side="top").hit(5.0, 5.0)
        assert isinstance(sel, ComponentSelectable)
        assert sel.component_id == top
        assert sel.component_id != back


class TestHitWire:
    def test_wire_segment_within_dist_hits(self) -> None:
        ctx = _FakeCtx()
        wid = _add_wire(ctx, [(0.0, 0.0), (10.0, 0.0)])
        sel = _tester(ctx).hit(5.0, 0.2)  # 0.2 < WIRE_HIT_DIST 0.35
        assert type(sel) == str
        assert isinstance(sel, WireSelectable)
        assert sel.wire.id == wid

    def test_wire_too_far_misses(self) -> None:
        ctx = _FakeCtx()
        _add_wire(ctx, [(0.0, 0.0), (10.0, 0.0)])
        assert _tester(ctx).hit(5.0, 0.5) is None  # 0.5 > 0.35


class TestHitGroup:
    def test_group_bbox_contains_point(self) -> None:
        ctx = _FakeCtx()
        cid = uuid.uuid4()
        gid = uuid.uuid4()
        ctx.layout.groups[gid] = ComponentGroup(
            id=gid, name="G", component_ids=frozenset({cid})
        )
        ctx._bboxes[cid] = Rect.of(x=0.0, y=0.0, lx=4.0, ly=4.0)
        # No pins/wires near (2,2) → group is the hit.
        sel = _tester(ctx).hit(2.0, 2.0)
        assert isinstance(sel, GroupSelectable)
        assert sel.group.id == gid

    def test_smallest_enclosing_group_wins(self) -> None:
        ctx = _FakeCtx()
        c_big, c_small = uuid.uuid4(), uuid.uuid4()
        g_big, g_small = uuid.uuid4(), uuid.uuid4()
        ctx.layout.groups[g_big] = ComponentGroup(
            id=g_big, name="big", component_ids=frozenset({c_big})
        )
        ctx.layout.groups[g_small] = ComponentGroup(
            id=g_small, name="small", component_ids=frozenset({c_small})
        )
        ctx._bboxes[c_big] = Rect.of(x=0.0, y=0.0, lx=10.0, ly=10.0)
        ctx._bboxes[c_small] = Rect.of(x=1.0, y=1.0, lx=3.0, ly=3.0)
        sel = _tester(ctx).hit(2.0, 2.0)  # inside both, small is tighter
        assert isinstance(sel, GroupSelectable)
        assert sel.group.id == g_small


class TestPriorityAndFilters:
    def test_pin_beats_wire(self) -> None:
        ctx = _FakeCtx()
        cid = _add_component(ctx, pin_xy=(5.0, 5.0))
        _add_wire(ctx, [(4.0, 5.0), (6.0, 5.0)])  # passes through the pin
        sel = _tester(ctx).hit(5.0, 5.0)
        assert isinstance(sel, ComponentSelectable)
        assert sel.component_id == cid

    def test_component_filter_off_falls_through_to_wire(self) -> None:
        ctx = _FakeCtx()
        _add_component(ctx, pin_xy=(5.0, 5.0))
        wid = _add_wire(ctx, [(4.0, 5.0), (6.0, 5.0)])
        sel = _tester(ctx).hit(
            5.0, 5.0, SelectionFilters(components=False, wires=True, groups=True)
        )
        assert isinstance(sel, WireSelectable)
        assert sel.wire.id == wid

    def test_all_filters_off_returns_none(self) -> None:
        ctx = _FakeCtx()
        _add_component(ctx, pin_xy=(5.0, 5.0))
        sel = _tester(ctx).hit(
            5.0, 5.0, SelectionFilters(components=False, wires=False, groups=False)
        )
        assert sel is None

    def test_empty_board_returns_none(self) -> None:
        assert _tester(_FakeCtx()).hit(0.0, 0.0) is None
