# SPDX-License-Identifier: MIT
"""``HitTester`` — resolve a board point to a :class:`Selectable`.

Pure geometry: given a scene point and a narrow context of the board's
placements / wires / groups (which ``EditorSession`` satisfies, and a test fake
can stub), return the entity under the cursor as a :class:`Selectable`, or
``None``.  Lifted verbatim from the old SelectTool's ``_hit_test`` family so the
new tool reproduces the exact thresholds and priority, but headless-testable.

Priority order (highest first), matching the legacy ``_build_hit_from_press``:

1. **component pin** — nearest pin within :data:`HIT_RADIUS`
2. **wire** — nearest segment within :data:`WIRE_HIT_DIST`
3. **group** — smallest enclosing child-group outline bbox
4. **component bbox** — first component bbox containing the point

Each layer is gated by a :class:`SelectionFilters` flag so the caller can scope
what is hittable (components / wires / groups).
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from protoboard.gui.selection.adapters import (
    ComponentSelectable,
    GroupSelectable,
    WireSelectable,
)

if TYPE_CHECKING:
    from protoboard.domain.layout.group import ComponentGroup
    from protoboard.domain.layout.placement import ComponentPlacement
    from protoboard.domain.layout.wire import Wire
    from protoboard.geometry import Point, Rect
    from protoboard.gui.selection.selectable import Selectable
    from protoboard.types import Side

# Thresholds — identical to the legacy SelectTool constants.
HIT_RADIUS = 0.5  # pin reach (Euclidean, raster units)
BBOX_MARGIN = 0.45  # component bbox padding
WIRE_HIT_DIST = 0.35  # perpendicular distance to a wire segment


@dataclass(frozen=True, slots=True)
class SelectionFilters:
    """Which kinds the hit-test may return (status-bar selection filter)."""

    components: bool = True
    wires: bool = True
    groups: bool = True


class HitContext(Protocol):
    """The board geometry HitTester needs — a subset of ``EditorSession``.

    Kept narrow so the tester is pure and a unit test can supply a minimal
    fake instead of a full session.
    """

    @property
    def layout(self) -> LayoutLike: ...

    def comp_spatial_index(self) -> dict[tuple[int, int], list[uuid.UUID]]: ...

    def wire_spatial_index(self) -> dict[tuple[int, int], list[uuid.UUID]]: ...

    def wire_by_id(self, wire_id: uuid.UUID) -> Wire | None: ...

    def iter_pin_positions(self, comp_id: uuid.UUID) -> list[tuple[int, Point]]: ...

    def component_bbox(self, comp_id: uuid.UUID) -> Rect | None: ...


class LayoutLike(Protocol):
    """The ``layout`` attributes HitTester reads."""

    @property
    def placements(self) -> dict[uuid.UUID, ComponentPlacement]: ...

    @property
    def groups(self) -> dict[uuid.UUID, ComponentGroup]: ...


def _dist_point_to_segment(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> float:
    """Shortest distance from (px,py) to segment (ax,ay)-(bx,by)."""
    dx, dy = bx - ax, by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq < 1e-12:
        return math.sqrt((px - ax) ** 2 + (py - ay) ** 2)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len_sq))
    cx, cy = ax + t * dx, ay + t * dy
    return math.sqrt((px - cx) ** 2 + (py - cy) ** 2)


def _min_dist_to_wire(px: float, py: float, wire: Wire) -> float:
    """Minimum distance from (px,py) to any segment of *wire*'s path."""
    pts = wire.path
    best = math.inf
    for i in range(len(pts) - 1):
        d = _dist_point_to_segment(
            px, py, pts[i].x, pts[i].y, pts[i + 1].x, pts[i + 1].y
        )
        if d < best:
            best = d
    return best


class HitTester:
    """Resolve a scene point to the selectable entity under it."""

    def __init__(self, ctx: HitContext, *, camera_side: Side = "top") -> None:
        self._ctx = ctx
        self._camera = camera_side

    def hit(
        self, x: float, y: float, filters: SelectionFilters | None = None
    ) -> Selectable | None:
        """Return the Selectable at world point (x, y), or None.

        Honours the priority order pin > wire > group > bbox, each gated by
        *filters* (defaults to all kinds hittable).
        """
        f = filters if filters is not None else SelectionFilters()

        if f.components:
            cid = self._hit_component_pin(x, y)
            if cid is not None:
                pl = self._ctx.layout.placements.get(cid)
                if pl is not None:
                    return ComponentSelectable(component_id=cid, placement=pl)

        if f.wires:
            wid = self._hit_wire(x, y)
            if wid is not None:
                wire = self._ctx.wire_by_id(wid)
                if wire is not None:
                    return WireSelectable(wire=wire)

        if f.groups:
            gid = self._hit_group(x, y)
            if gid is not None:
                grp = self._ctx.layout.groups.get(gid)
                if grp is not None:
                    return GroupSelectable(group=grp)

        if f.components:
            cid = self._hit_component_bbox(x, y)
            if cid is not None:
                pl = self._ctx.layout.placements.get(cid)
                if pl is not None:
                    return ComponentSelectable(component_id=cid, placement=pl)

        return None

    # -- per-kind resolvers (faithful to the legacy algorithms) ------------

    def _candidates(
        self, index: dict[tuple[int, int], list[uuid.UUID]], x: float, y: float
    ) -> set[uuid.UUID]:
        """Collect ids from the 3x3 grid cells around (x, y)."""
        cell_x, cell_y = round(x), round(y)
        out: set[uuid.UUID] = set()
        for dcx in (-1, 0, 1):
            for dcy in (-1, 0, 1):
                out.update(index.get((cell_x + dcx, cell_y + dcy), []))
        return out

    def _hit_component_pin(self, px: float, py: float) -> uuid.UUID | None:
        """Nearest component within HIT_RADIUS; camera-side preferred."""
        front_best: uuid.UUID | None = None
        front_d = math.inf
        back_best: uuid.UUID | None = None
        back_d = math.inf
        for cid in self._candidates(self._ctx.comp_spatial_index(), px, py):
            pl = self._ctx.layout.placements.get(cid)
            if pl is None:
                continue
            for _pin, pos in self._ctx.iter_pin_positions(cid):
                d = math.hypot(px - pos.x, py - pos.y)
                if pl.side == self._camera:
                    if d < front_d:
                        front_d, front_best = d, cid
                elif d < back_d:
                    back_d, back_best = d, cid
        if front_best is not None and front_d <= HIT_RADIUS:
            return front_best
        if back_best is not None and back_d <= HIT_RADIUS:
            return back_best
        return None

    def _hit_component_bbox(self, px: float, py: float) -> uuid.UUID | None:
        """First component whose padded pin-bbox contains (px, py)."""
        candidates = self._candidates(self._ctx.comp_spatial_index(), px, py)

        def contains(cid: uuid.UUID) -> bool:
            positions = [pos for _, pos in self._ctx.iter_pin_positions(cid)]
            if not positions:
                return False
            xs = [p.x for p in positions]
            ys = [p.y for p in positions]
            m = BBOX_MARGIN
            return (
                min(xs) - m <= px <= max(xs) + m
                and min(ys) - m <= py <= max(ys) + m
            )

        # Camera side first, then the other side.
        for want_camera in (True, False):
            for cid in candidates:
                pl = self._ctx.layout.placements.get(cid)
                if pl is None:
                    continue
                if (pl.side == self._camera) != want_camera:
                    continue
                if contains(cid):
                    return cid
        return None

    def _hit_wire(self, px: float, py: float) -> uuid.UUID | None:
        """Wire with the nearest segment within WIRE_HIT_DIST."""
        best_id: uuid.UUID | None = None
        best_d = WIRE_HIT_DIST
        for wid in self._candidates(self._ctx.wire_spatial_index(), px, py):
            wire = self._ctx.wire_by_id(wid)
            if wire is None:
                continue
            d = _min_dist_to_wire(px, py, wire)
            if d < best_d:
                best_d, best_id = d, wid
        return best_id

    def _hit_group(self, px: float, py: float) -> uuid.UUID | None:
        """Smallest group whose member-bbox union contains (px, py).

        The legacy tester read group-outline scene items; here we recompute
        the enclosing bbox from member component bboxes (same geometry, no Qt
        scene dependency) and pick the smallest enclosing group.
        """
        best_gid: uuid.UUID | None = None
        best_area = math.inf
        for gid, grp in self._ctx.layout.groups.items():
            member_ids = grp.component_ids
            if not member_ids:
                continue
            x0 = y0 = math.inf
            x1 = y1 = -math.inf
            for cid in member_ids:
                bbox = self._ctx.component_bbox(cid)
                if bbox is None:
                    continue
                x0 = min(x0, bbox.x)
                y0 = min(y0, bbox.y)
                x1 = max(x1, bbox.x + bbox.width)
                y1 = max(y1, bbox.y + bbox.height)
            if x0 > x1 or y0 > y1:
                continue  # no resolvable members
            if x0 <= px <= x1 and y0 <= py <= y1:
                area = (x1 - x0) * (y1 - y0)
                if area < best_area:
                    best_area, best_gid = area, gid
        return best_gid
