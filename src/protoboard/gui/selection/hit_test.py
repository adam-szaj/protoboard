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
    WireVertexSelectable,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from protoboard.domain.layout.group import ComponentGroup
    from protoboard.domain.layout.placement import ComponentPlacement
    from protoboard.domain.layout.wire import Wire
    from protoboard.geometry import Point, Rect
    from protoboard.gui.selection.selectable import Selectable
    from protoboard.types import Side

# Thresholds — identical to the legacy SelectTool constants.
HIT_RADIUS = 0.5  # pin reach (Euclidean, raster units)
# A click counts as landing *on a pin-pad* (→ that pin's net context) only when
# this close to the pin; between PAD_HIT_RADIUS and HIT_RADIUS the component is
# still selected, but as a body click (no net context).  Mirrors
# ``Theme.pad_radius`` (kept a constant so the hit-test stays Theme-free).
PAD_HIT_RADIUS = 0.35
BBOX_MARGIN = 0.45  # component bbox padding
WIRE_HIT_DIST = 0.35  # perpendicular distance to a wire segment
NODE_HIT_RADIUS = 0.32  # wire-vertex reach (Euclidean, raster units)
_CLUSTER_EPS = 1e-3  # two vertices within this are the same shared knot


@dataclass(frozen=True, slots=True)
class WireEditCycle:
    """Round-robin state for repeated clicks at a shared knot (Qt-free).

    ``key`` is ``(round(x, 4), round(y, 4), start_net_id)`` — the cluster
    identity; ``last_wire_id`` is the wire returned on the previous click at
    that cluster.  Both ``None`` means "fresh / no active cluster".  Replaces
    the legacy ``_wire_edit_cycle_key`` / ``_wire_edit_cycle_last_wire_id``
    pair; the tool stores one and passes it back on the next pick so the cluster
    cycle survives only while the user keeps clicking the same knot.
    """

    key: tuple[float, float, uuid.UUID | None] | None = None
    last_wire_id: uuid.UUID | None = None


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

    @property
    def wires(self) -> Iterable[Wire]: ...


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
            pin_hit = self._hit_component_pin(x, y)
            if pin_hit is not None:
                cid, pin_number = pin_hit
                pl = self._ctx.layout.placements.get(cid)
                if pl is not None:
                    return ComponentSelectable(
                        component_id=cid, placement=pl, pin_number=pin_number,
                    )

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
            bbox_cid = self._hit_component_bbox(x, y)
            if bbox_cid is not None:
                pl = self._ctx.layout.placements.get(bbox_cid)
                if pl is not None:
                    # bbox fallback = body click → no pin context.
                    return ComponentSelectable(
                        component_id=bbox_cid, placement=pl, pin_number=None,
                    )

        return None

    def hit_wire_vertex(
        self,
        x: float,
        y: float,
        *,
        start_wire_id: uuid.UUID | None,
        wire_net_map: dict[uuid.UUID, uuid.UUID],
        cycle: WireEditCycle,
    ) -> tuple[WireVertexSelectable | None, WireEditCycle]:
        """Nearest wire vertex within NODE_HIT_RADIUS, net-restricted, cluster-cycling.

        Verbatim port of the legacy ``_hit_test_wire_node_any`` (minus the
        ``_ensure_wire_ready_for_edit`` pad-vertex enrichment, deferred): the
        search is restricted to the net of ``start_wire_id`` (looked up in
        ``wire_net_map``); when the start wire has no resolvable net the search
        is restricted to the start wire alone (so hitting an unrelated wire mid-
        edit is impossible).  At a shared knot the currently-selected start wire
        wins ties, and repeated clicks at the same cluster rotate through the
        sibling wires; changing cluster (or a miss) resets the cycle.

        Returns the picked vertex (or ``None``) and the updated
        :class:`WireEditCycle` the caller stores for the next click.
        """
        wires = self._ctx.layout.wires

        start_net_id: uuid.UUID | None = None
        if start_wire_id is not None:
            start_net_id = wire_net_map.get(start_wire_id)

        # When the start wire carries no resolvable net, limit to it alone.
        restrict_to_start_wire = (
            start_net_id is None and start_wire_id is not None
        )

        # Collect every candidate (wire, idx, dist) within the radius.
        candidates: list[tuple[Wire, int, float]] = []
        for wire in wires:
            if start_net_id is not None and wire_net_map.get(wire.id) != start_net_id:
                continue
            if restrict_to_start_wire and wire.id != start_wire_id:
                continue
            for i, pt in enumerate(wire.path):
                d = math.hypot(x - pt.x, y - pt.y)
                if d < NODE_HIT_RADIUS:
                    candidates.append((wire, i, d))

        if not candidates:
            # No hit — drop cycle state so a future hit starts fresh.
            return None, WireEditCycle()

        # Nearest cluster: the closest hit defines a snap-point; any other hit
        # within _CLUSTER_EPS of it is the same shared knot (any wire).
        candidates.sort(key=lambda c: c[2])
        anchor_wire, anchor_idx, _ = candidates[0]
        ax = anchor_wire.path[anchor_idx].x
        ay = anchor_wire.path[anchor_idx].y

        cluster = [
            (w, i) for (w, i, _d) in candidates
            if abs(w.path[i].x - ax) < _CLUSTER_EPS
            and abs(w.path[i].y - ay) < _CLUSTER_EPS
        ]

        # De-dup per-wire (a self-touch may visit a point twice — first index).
        seen_wires: set[uuid.UUID] = set()
        cluster_unique: list[tuple[Wire, int]] = []
        for w, i in cluster:
            if w.id in seen_wires:
                continue
            seen_wires.add(w.id)
            cluster_unique.append((w, i))

        cluster_key = (round(ax, 4), round(ay, 4), start_net_id)
        same_cluster = cluster_key == cycle.key
        last_wire_id = cycle.last_wire_id if same_cluster else None

        # Single candidate — no rotation.
        if len(cluster_unique) == 1:
            w, i = cluster_unique[0]
            return (
                WireVertexSelectable(wire_id=w.id, index=i),
                WireEditCycle(key=cluster_key, last_wire_id=w.id),
            )

        # Rotation order: selected wire first, then the rest in layout order.
        ordered: list[tuple[Wire, int]] = []
        rest: list[tuple[Wire, int]] = []
        for w, i in cluster_unique:
            if start_wire_id is not None and w.id == start_wire_id:
                ordered.append((w, i))
            else:
                rest.append((w, i))
        ordered.extend(rest)

        # First click at this cluster → head; repeat click → advance.
        if same_cluster and last_wire_id is not None:
            ids = [w.id for w, _ in ordered]
            try:
                pick_pos = (ids.index(last_wire_id) + 1) % len(ordered)
            except ValueError:
                pick_pos = 0
        else:
            pick_pos = 0

        w, i = ordered[pick_pos]
        return (
            WireVertexSelectable(wire_id=w.id, index=i),
            WireEditCycle(key=cluster_key, last_wire_id=w.id),
        )

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

    def _hit_component_pin(
        self, px: float, py: float
    ) -> tuple[uuid.UUID, int | None] | None:
        """Nearest component within HIT_RADIUS; camera-side preferred.

        Returns ``(comp_id, pin_number)`` where ``pin_number`` is the nearest
        pin **only when the click landed within ``PAD_HIT_RADIUS`` of it** (→ a
        genuine pin-pad click, carrying that pin's net context); otherwise
        ``None`` (the component is still selected, but as a body click with no
        net context).  Returns ``None`` outright when nothing is within reach.
        """
        front_best: tuple[uuid.UUID, int] | None = None
        front_d = math.inf
        back_best: tuple[uuid.UUID, int] | None = None
        back_d = math.inf
        for cid in self._candidates(self._ctx.comp_spatial_index(), px, py):
            pl = self._ctx.layout.placements.get(cid)
            if pl is None:
                continue
            for pin, pos in self._ctx.iter_pin_positions(cid):
                d = math.hypot(px - pos.x, py - pos.y)
                if pl.side == self._camera:
                    if d < front_d:
                        front_d, front_best = d, (cid, pin)
                elif d < back_d:
                    back_d, back_best = d, (cid, pin)
        for best, dist in ((front_best, front_d), (back_best, back_d)):
            if best is not None and dist <= HIT_RADIUS:
                cid, pin = best
                # Pin context only when truly on the pad; otherwise body click.
                return cid, (pin if dist <= PAD_HIT_RADIUS else None)
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
