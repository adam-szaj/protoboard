# SPDX-License-Identifier: MIT
"""Post-routing wire simplification — straighten + corner-cut.

Runs after the main A* router has emitted a list of WireSegments.  The pass
shortens paths in two ways:

1. **Straighten** — merge consecutive collinear segments inside one wire.
   ``A→B→C`` with B on segment AC collapses to ``A→C``.

2. **Corner-cut** — replace a right-angle ortho-ortho pair ``A→B→C`` with a
   diagonal ``A→C`` when both legs are length 1, the diagonal's slope sign
   matches the wire's parity family, the endpoints A and C are board pads,
   and the new segment introduces no collision with any other bare wire.

Both passes preserve every endpoint at a pad position (so connectivity is
unchanged) and never extend a wire — only shorten it.
"""

from __future__ import annotations

import math
import os
import uuid

import structlog

_log = structlog.get_logger(__name__)

# Opt-in diagnostic: when SIMPLIFY_TRACE=1 every rejected (i, j) candidate
# inside ``generic_simplify_wire`` emits a structured log line with the
# specific rejection reason and the relevant geometry.  Off by default
# because dense boards generate hundreds of lines per wire.  Used to
# diagnose "uparty wire" reports where simplify is expected to shorten
# a path but produces no change.
_SIMPLIFY_TRACE = os.environ.get("SIMPLIFY_TRACE", "").lower() in ("1", "true", "yes")
from enum import StrEnum  # noqa: E402
from typing import TYPE_CHECKING, cast  # noqa: E402

from protoboard.domain.layout.wire import Wire  # noqa: E402
from protoboard.geometry import Point, Vector  # noqa: E402
from protoboard.services.routing.astar import (  # noqa: E402
    _build_bare_seg_index,
    _segs_cross,
)
from protoboard.types import Side  # noqa: E402

if TYPE_CHECKING:
    from protoboard.domain.board.pad_state import Pad


class SimplifyMode(StrEnum):
    """How aggressively to shorten a wire path.

    CHEBYSHEV:   replace any subpath with the shortest grid-aligned path
                 (diagonal where possible, ortho where forced).  Ignores the
                 bare-diagonal parity rule — use only when the user understands
                 that the produced diagonal may cross perpendicular bare
                 diagonals at cell centres.  Best visual result.

    BARE_PARITY: same as CHEBYSHEV for insulated wires; for bare wires, every
                 new diagonal segment must respect the source pad's parity
                 family (the anti-short rule that bare_diagonal routing relies
                 on).  Default — preserves the invariants used by autoroute.
    """

    CHEBYSHEV = "chebyshev"
    BARE_PARITY = "bare_parity"


# ---------------------------------------------------------------------------
# Straighten — merge collinear segments
# ---------------------------------------------------------------------------


def _collinear(a: Point, b: Point, c: Point) -> bool:
    """True if b lies on segment a→c (strict interior or endpoint match)."""
    ab = b - a
    bc = c - b
    # Cross product must be zero (collinear) and direction the same sign.
    if ab.cross(bc) != 0.0:
        return False
    return ab.dot(bc) >= 0.0


def straighten_wire(wire: Wire, board_pads: set[Point] | None = None) -> Wire:
    """Return a copy of *wire* with internal collinear vertices removed.

    For a *bare* wire, any internal point that is also a board pad must be
    kept — the bare conductor physically contacts that pad and the
    connectivity graph relies on those vertices to merge electric sets
    correctly.  An insulated wire floats above the board and may drop pad
    vertices freely.  *board_pads* is consulted only when the wire is bare.
    """
    if len(wire.path) < 3:
        return wire
    pads = board_pads if (board_pads is not None and not wire.insulated) else set()
    out: list[Point] = [wire.path[0]]
    for i in range(1, len(wire.path) - 1):
        a, b, c = out[-1], wire.path[i], wire.path[i + 1]
        if _collinear(a, b, c) and b not in pads:
            continue
        out.append(b)
    out.append(wire.path[-1])
    if len(out) == len(wire.path):
        return wire
    # Preserve wire id: this is an edit of the same logical wire, not a new
    # one.  Stable identity matters for highlight, recording and undo.
    return Wire(
        id=wire.id,
        path=out,
        insulated=wire.insulated,
        side=wire.side,
        color=wire.color,
    )


# ---------------------------------------------------------------------------
# Corner-cut — replace ortho-ortho 90° with diagonal
# ---------------------------------------------------------------------------


def _parity_of(p: Point) -> int:
    import math as _math

    return (int(_math.floor(p.x)) + int(_math.floor(p.y))) % 2


def _is_ortho(a: Point, b: Point) -> bool:
    return (a.x == b.x) != (a.y == b.y)


def _unit_step(a: Point, b: Point) -> bool:
    return abs(a.x - b.x) + abs(a.y - b.y) == 1.0


def _wire_to_bare_segs(wire: Wire) -> set[tuple[Point, Point, str]]:
    """Decompose a wire's path into (p1, p2, side) tuples."""
    segs: set[tuple[Point, Point, str]] = set()
    for a, b in zip(wire.path, wire.path[1:]):
        segs.add((a, b, wire.side))
    return segs


def corner_cut_wire(
    wire: Wire,
    other_bare_wires: list[Wire],
    board_pads: set[Point],
) -> Wire:
    """Replace ortho-ortho-90° corners in *wire* with diagonals where safe.

    Only bare wires are eligible; the new diagonal must respect the parity
    family of the corner's apex and must not cross any segment from
    *other_bare_wires* (full segment×segment intersection check).
    """
    if wire.insulated or len(wire.path) < 3:
        return wire

    # Build a mutable index of all other bare segments on this layer.
    other_segs: set[tuple[Point, Point, str]] = set()
    for ow in other_bare_wires:
        if ow.insulated or ow.id == wire.id:
            continue
        other_segs.update(_wire_to_bare_segs(ow))
    bare_idx = _build_bare_seg_index(other_segs)

    # Also accumulate segments from this same wire that aren't being replaced,
    # so we don't introduce a self-cross.
    own_segs = _wire_to_bare_segs(wire)

    changed = True
    cur_path = list(wire.path)
    while changed:
        changed = False
        i = 0
        while i + 2 < len(cur_path):
            a, b, c = cur_path[i], cur_path[i + 1], cur_path[i + 2]
            if (
                _is_ortho(a, b)
                and _is_ortho(b, c)
                and _unit_step(a, b)
                and _unit_step(b, c)
                and a.x != c.x  # not a U-turn back on the same column
                and a.y != c.y
                and a in board_pads
                and c in board_pads
            ):
                # Parity check: the diagonal's slope sign must match the
                # source-pad parity family.
                src_parity = _parity_of(wire.path[0])
                slope_sign = (c.x - a.x) * (c.y - a.y)
                allowed_sign = 1.0 if src_parity == 0 else -1.0
                if slope_sign * allowed_sign > 0.0:
                    # Collision check against all *other* bare segments.
                    safe = True
                    for _seg in (s for cells in bare_idx.values() for s in cells):
                        if _segs_cross(a, c, _seg[0], _seg[1]):
                            safe = False
                            break
                    # And against own non-replaced segments.
                    if safe:
                        for src, dst, _side in own_segs:
                            # Skip the two segments being replaced.
                            if (src, dst) in ((a, b), (b, c)):
                                continue
                            if _segs_cross(a, c, src, dst):
                                safe = False
                                break
                    if safe:
                        cur_path[i + 1 : i + 2] = []  # remove b
                        # Update own_segs to reflect the new diagonal.
                        own_segs.discard((a, b, wire.side))
                        own_segs.discard((b, c, wire.side))
                        own_segs.add((a, c, wire.side))
                        changed = True
                        continue
            i += 1

    if len(cur_path) == len(wire.path):
        return wire
    # Preserve wire id — corner-cut is a geometry-only edit of the same wire.
    return Wire(
        id=wire.id,
        path=cur_path,
        insulated=wire.insulated,
        side=cast(Side, wire.side),
        color=wire.color,
    )


# ---------------------------------------------------------------------------
# Top-level driver
# ---------------------------------------------------------------------------


def simplify_wires(
    wires: list[Wire],
    board_pads: set[Point],
) -> list[Wire]:
    """Run straighten + corner-cut over *wires*.

    *board_pads* is the set of every valid pad position on the board (used to
    verify that new diagonal endpoints still land on legal pads).  Order is
    preserved; wire IDs are regenerated only when a wire's path actually
    changes — unchanged wires are returned as-is.
    """
    # First pass: straighten each wire (preserving on-pad vertices for bare).
    straightened = [straighten_wire(w, board_pads) for w in wires]
    # Second pass: corner-cut each wire against the others.
    out: list[Wire] = []
    for i, w in enumerate(straightened):
        cur = corner_cut_wire(w, straightened, board_pads)
        # Re-straighten after corner-cut may expose new collinear runs.
        cur = straighten_wire(cur, board_pads)
        out.append(cur)
        # Reflect possible change back into the working list so later wires
        # see the cut version when checking for collisions.
        straightened[i] = cur
    return out


# ===========================================================================
# Generic shortcut algorithm
# ===========================================================================

_EPS = 1e-9


def _path_length(path: list[Point]) -> float:
    """Geometric length of a polyline (sum of Euclidean segment lengths)."""
    total = 0.0
    for p, q in zip(path, path[1:]):
        total += (q - p).norm()
    return total


def _count_total_corners(path: list[Point]) -> int:
    """Count every geometric direction change on *path* (any angle).

    Returns the number of vertices where the outgoing segment's direction
    differs from the incoming segment's direction.  Collinear vertices (no
    direction change) are NOT counted, so chains of pad-nodes laid along
    a straight line don't inflate the count.

    Used by the simplify diagnostic that flags any output path with more
    corners than the input — simplify is supposed to never multiply bends.
    """
    if len(path) < 3:
        return 0
    count = 0
    for k in range(1, len(path) - 1):
        a, b, c = path[k - 1], path[k], path[k + 1]
        d1 = b - a
        d2 = c - b
        l1 = d1.norm()
        l2 = d2.norm()
        if l1 < _EPS or l2 < _EPS:
            continue  # zero-length segment, skip
        # Same direction? Normalised dot ≈ 1 → collinear.
        cos = d1.dot(d2) / (l1 * l2)
        if cos > 1.0 - _EPS:
            continue
        count += 1
    return count


def _count_90_corners(path: list[Point]) -> int:
    """Count 90° corners on *path* (sharp ortho-ortho bends).

    A 90° corner is a vertex where the incoming segment is purely ortho
    (horizontal or vertical) and the outgoing segment is the other ortho
    axis — the angle between them is exactly 90°.  Ortho→diag transitions
    are 45° (mild bend) and don't count.  Collinear ortho-ortho is 0°
    (straight) and doesn't count either.

    Solder-quality heuristic: a wire with fewer 90° bends is easier to
    seat flat on the board, so simplify prefers same-length replacements
    that reduce the count.
    """
    if len(path) < 3:
        return 0
    count = 0
    for k in range(1, len(path) - 1):
        a, b, c = path[k - 1], path[k], path[k + 1]
        ab_dx, ab_dy = b.x - a.x, b.y - a.y
        bc_dx, bc_dy = c.x - b.x, c.y - b.y
        ab_is_ortho = (abs(ab_dx) < _EPS) != (abs(ab_dy) < _EPS)
        bc_is_ortho = (abs(bc_dx) < _EPS) != (abs(bc_dy) < _EPS)
        if not (ab_is_ortho and bc_is_ortho):
            continue
        # Both ortho — perpendicular if one is horizontal and the other vertical.
        ab_horiz = abs(ab_dy) < _EPS
        bc_horiz = abs(bc_dy) < _EPS
        if ab_horiz != bc_horiz:
            count += 1
    return count


def _fast_pads_on_segment(
    a: Point,
    b: Point,
    board_pads: set[Point],
) -> list[Point]:
    """Return pads from *board_pads* strictly interior to segment a→b.

    Fast path: the segment is assumed to be pure ortho (one of dx/dy is 0)
    or pure diag (|dx| == |dy|), as produced by ``_canonical_subpath``.
    Under that assumption we can ENUMERATE candidate pad positions in
    O(seg_length) rather than scanning every pad in the set.

    Falls back to the generic ``insert_pad_nodes`` slow path for skewed
    segments (shouldn't happen here, but kept for safety).
    """
    dx = b.x - a.x
    dy = b.y - a.y
    adx = abs(dx)
    ady = abs(dy)
    # Pure ortho (horizontal or vertical) — step ±1 along the long axis.
    if ady < _EPS:
        # Horizontal: enumerate (x, a.y) for integer pads in (a.x, b.x).
        sx = 1.0 if dx > 0 else -1.0
        n = int(round(adx))
        out: list[Point] = []
        for k in range(1, n):
            p = a + Vector(sx * k, 0.0)
            if p in board_pads:
                out.append(p)
        return out
    if adx < _EPS:
        sy = 1.0 if dy > 0 else -1.0
        n = int(round(ady))
        out = []
        for k in range(1, n):
            p = a + Vector(0.0, sy * k)
            if p in board_pads:
                out.append(p)
        return out
    # Pure diagonal: |dx| == |dy|.  Step (±1, ±1).
    if abs(adx - ady) < _EPS:
        sx = 1.0 if dx > 0 else -1.0
        sy = 1.0 if dy > 0 else -1.0
        n = int(round(adx))
        out = []
        for k in range(1, n):
            p = a + Vector(sx * k, sy * k)
            if p in board_pads:
                out.append(p)
        return out
    # Skewed — should not happen for canonical output.  Slow generic path.
    from protoboard.domain.layout.wire_utils import insert_pad_nodes

    enriched = insert_pad_nodes([a, b], board_pads)
    return enriched[1:-1]


def _canonical_subpath(
    a: Point,
    b: Point,
    *,
    allow_l_shape: bool = True,
    board_pads: set[Point] | None = None,
) -> list[Point] | None:
    """Shortest grid-aligned path from *a* to *b* preferring diagonals.

    Returns a list of vertices ``[a, ..., b]``.  Without ``board_pads`` the
    result has at most three vertices: pure ortho or diagonal collapses to
    two; mixed (|dx| != |dy|) becomes a diag-prefix + ortho-tail L.

    When ``board_pads`` is provided, any pad position that lies on a segment
    of the canonical path is inserted as a vertex.  Callers use this for bare
    wires so that downstream consumers see every physical pad contact.

    Returns None when ``allow_l_shape`` is False and the canonical path would
    have to bend (callers that only want "truly straight" shortcuts use this).
    """
    dx = b.x - a.x
    dy = b.y - a.y
    ax = abs(dx)
    ay = abs(dy)
    if ax < _EPS and ay < _EPS:
        return None
    # Pure ortho or pure diag → a single straight segment.
    if ax < _EPS or ay < _EPS or abs(ax - ay) < _EPS:
        base: list[Point] = [a, b]
    else:
        # L-shape: diag prefix of length min(ax, ay), ortho tail of the remainder.
        if not allow_l_shape:
            return None
        sx = 1.0 if dx > 0 else -1.0
        sy = 1.0 if dy > 0 else -1.0
        diag = min(ax, ay)
        m = a + Vector(sx * diag, sy * diag)
        base = [a, m, b]
    if board_pads:
        # Each segment of `base` is pure ortho or pure diag (guaranteed by
        # the construction above).  Use the fast enumeration helper instead
        # of the generic O(pads) scan in insert_pad_nodes.
        enriched: list[Point] = [base[0]]
        for k in range(len(base) - 1):
            enriched.extend(_fast_pads_on_segment(base[k], base[k + 1], board_pads))
            enriched.append(base[k + 1])
        return enriched
    return base


def _wire_to_segs(wire: Wire) -> list[tuple[Point, Point]]:
    return list(zip(wire.path, wire.path[1:]))


def _point_on_segment_interior(
    p: Point,
    a: Point,
    b: Point,
    eps: float = _EPS,
) -> bool:
    """True if *p* lies on segment a→b strictly between the endpoints.

    Used to detect T-junctions: an endpoint/vertex of one wire path that
    falls in the middle of another wire's segment.  Such a meeting is a
    physical electrical contact — ``_segs_cross`` deliberately ignores it
    because the autoroute upstream uses a separate occupancy map to catch
    these, but simplify has no such upstream and must check here.
    """
    ab = b - a
    ap = p - a
    if abs(ab.cross(ap)) > eps:
        return False  # not collinear
    # Collinear, so t = (ap · ab) / |ab|² is the projection parameter
    # (degenerate ab guarded by the near-zero length check).
    ab_sq = ab.dot(ab)
    if ab_sq < eps * eps:
        return False
    t = ap.dot(ab) / ab_sq
    return eps < t < 1.0 - eps


def _segs_cross_or_touch(
    a1: Point,
    a2: Point,
    b1: Point,
    b2: Point,
) -> bool:
    """Like ``_segs_cross`` but also reports T-junctions and vertex-on-edge.

    Returns True if segments a1→a2 and b1→b2 share any point other than an
    endpoint-on-endpoint coincidence.  In particular: if a vertex of one
    segment falls strictly inside the other, that's a real electrical
    contact and counts as a crossing here.
    """
    if _segs_cross(a1, a2, b1, b2):
        return True
    # T-junction or vertex-on-interior cases.
    if _point_on_segment_interior(a1, b1, b2):
        return True
    if _point_on_segment_interior(a2, b1, b2):
        return True
    if _point_on_segment_interior(b1, a1, a2):
        return True
    if _point_on_segment_interior(b2, a1, a2):
        return True
    return False


def _segs_overlap_collinear(
    a1: Point,
    a2: Point,
    b1: Point,
    b2: Point,
    eps: float = _EPS,
) -> bool:
    """Return True if segments a1→a2 and b1→b2 are collinear and share more
    than a single point (i.e. one's interior overlaps the other).

    Used to detect bare-on-bare overlap: a candidate wire that lies *along*
    another net's wire is a long galvanic short — ``_segs_cross`` ignores
    this case because it tests strict interior intersection only.

    Endpoint-only coincidences (segments meeting at one point) return False;
    those are caught by the shared-vertex check in the caller.
    """
    # Collinearity: both b1 and b2 must lie on the infinite line through a1-a2.
    ab = a2 - a1
    if abs(ab.dx) < eps and abs(ab.dy) < eps:
        return False  # degenerate
    cross1 = ab.cross(b1 - a1)
    cross2 = ab.cross(b2 - a1)
    ab_len = ab.norm()
    if abs(cross1) > eps * ab_len or abs(cross2) > eps * ab_len:
        return False
    # Project both segments to the dominant axis to measure interval overlap.
    if abs(ab.dx) >= abs(ab.dy):
        ta1, ta2 = a1.x, a2.x
        tb1, tb2 = b1.x, b2.x
    else:
        ta1, ta2 = a1.y, a2.y
        tb1, tb2 = b1.y, b2.y
    a_lo, a_hi = (ta1, ta2) if ta1 <= ta2 else (ta2, ta1)
    b_lo, b_hi = (tb1, tb2) if tb1 <= tb2 else (tb2, tb1)
    # Intervals overlap by more than a point iff lo < min(hi) and hi > max(lo).
    overlap_lo = max(a_lo, b_lo)
    overlap_hi = min(a_hi, b_hi)
    return overlap_hi - overlap_lo > eps


def _count_crossings_with_other(
    subpath_segs: list[tuple[Point, Point]],
    subpath_vertices: list[Point],
    ow: Wire,
    *,
    skip_pts: set[tuple[float, float]],
) -> int:
    """Count physical / electrical contacts between *subpath* and a single
    other wire *ow*.  Mirrors the three failure modes of the legacy
    "any-contact-is-a-short" check, but as a *count* instead of a bool,
    so simplify can compare original-vs-candidate locally.

    *subpath_segs* — segments of the path slice being compared.
    *subpath_vertices* — vertices (endpoints of those segments, in order).
    *skip_pts* — point coords that should NOT count as contacts even if
    they coincide with vertices of *ow*; typically the two endpoints of
    the replaced subpath (those contacts are pre-existing and not "new").

    Returns the contact count.
    """

    def _same_pt(a: Point, b: Point) -> bool:
        return abs(a.x - b.x) < _EPS and abs(a.y - b.y) < _EPS

    count = 0
    # Geometric crossings are counted for EVERY pair (regardless of bare/
    # insulated) — visually they're still crossings, and the M ≤ N rule
    # ("0 nowych przecięć") covers both electrical and visual tangle.
    # Insulated×insulated cross-over is not an electrical short but it is
    # a visual crossing the user cares about for solder-quality.
    ow_segs = _wire_to_segs(ow)
    # Per-segment bbox precompute (avoid recomputing min/max in the hot loop).
    ow_seg_bbox: list[tuple[float, float, float, float]] = []
    for o_src, o_dst in ow_segs:
        ow_seg_bbox.append(
            (
                o_src.x if o_src.x < o_dst.x else o_dst.x,
                o_src.y if o_src.y < o_dst.y else o_dst.y,
                o_src.x if o_src.x > o_dst.x else o_dst.x,
                o_src.y if o_src.y > o_dst.y else o_dst.y,
            )
        )
    for c_src, c_dst in subpath_segs:
        c_lo_x = c_src.x if c_src.x < c_dst.x else c_dst.x
        c_hi_x = c_src.x if c_src.x > c_dst.x else c_dst.x
        c_lo_y = c_src.y if c_src.y < c_dst.y else c_dst.y
        c_hi_y = c_src.y if c_src.y > c_dst.y else c_dst.y
        for k, (o_src, o_dst) in enumerate(ow_segs):
            o_lo_x, o_lo_y, o_hi_x, o_hi_y = ow_seg_bbox[k]
            # Per-segment bbox reject — disjoint bboxes can never contact.
            if (
                o_hi_x < c_lo_x - _EPS
                or o_lo_x > c_hi_x + _EPS
                or o_hi_y < c_lo_y - _EPS
                or o_lo_y > c_hi_y + _EPS
            ):
                continue
            if _segs_cross_or_touch(c_src, c_dst, o_src, o_dst):
                count += 1
            # Collinear overlap: two wires sharing a stretch of geometry on
            # the same side is heavier than a point crossing (long visual
            # parallel + electrical short when bare).  +2 so M ≤ N treats
            # it strictly worse.
            elif _segs_overlap_collinear(c_src, c_dst, o_src, o_dst):
                count += 2
    # Plus shared-pad / shared-endpoint coincidences.  For bare other-wires
    # this is every vertex; for insulated only the two endpoints sit on
    # board pads.  Either way, vertex coincidence with the subpath that's
    # NOT on the replaced-subpath endpoints is one extra contact.
    if not ow.insulated:
        ow_anchor_set = {(p.x, p.y) for p in ow.path}
    else:
        if len(ow.path) < 2:
            return count
        ow_anchor_set = {
            (ow.path[0].x, ow.path[0].y),
            (ow.path[-1].x, ow.path[-1].y),
        }
    for cv in subpath_vertices:
        if (cv.x, cv.y) in skip_pts:
            continue
        if (cv.x, cv.y) in ow_anchor_set:
            count += 1
    # An insulated other-wire's endpoint can also be hit by a *segment* of
    # the subpath passing through it mid-interior — the conductor lands on
    # that pad.  Bare-other already covered above by _segs_cross_or_touch
    # (T-junction detection).
    if ow.insulated and len(ow.path) >= 2:
        endpoints = (ow.path[0], ow.path[-1])
        for c_src, c_dst in subpath_segs:
            # Fast bbox check: an endpoint outside the segment's bbox cannot
            # lie on its interior.
            c_lo_x = c_src.x if c_src.x < c_dst.x else c_dst.x
            c_hi_x = c_src.x if c_src.x > c_dst.x else c_dst.x
            c_lo_y = c_src.y if c_src.y < c_dst.y else c_dst.y
            c_hi_y = c_src.y if c_src.y > c_dst.y else c_dst.y
            for ep in endpoints:
                if (
                    ep.x < c_lo_x - _EPS
                    or ep.x > c_hi_x + _EPS
                    or ep.y < c_lo_y - _EPS
                    or ep.y > c_hi_y + _EPS
                ):
                    continue
                if _point_on_segment_interior(ep, c_src, c_dst):
                    count += 1
    return count


def _count_insulated_pad_contacts(
    subpath_segs: list[tuple[Point, Point]],
    subpath_vertices: list[Point],
    ow: Wire,
    *,
    skip_pts: set[tuple[float, float]],
) -> int:
    """Pure pad-coincidence count between *subpath* and an INSULATED other
    wire *ow*.  Counts every pad where:

      * a vertex of *subpath* sits on a vertex of *ow* (any vertex, not
        just endpoints), or
      * a vertex of *ow* lies strictly inside a *subpath* segment (the
        subpath passes over the pad ``ow`` rests on).

    Mid-segment crossings WITHOUT pad coincidence (two diagonals
    intersecting in the air between pads) are NOT counted — they are
    transparent for insulated wires that pass each other without sharing
    a pad.

    Symmetric: a pad shared with ``ow`` contributes 1 to the count
    regardless of whether the subpath touches it as a vertex or as an
    interior point, and regardless of which side of the shared vertex
    ``ow`` runs through.  This eliminates the asymmetry of
    ``_count_crossings_with_other`` where endpoint-on-endpoint counted
    as 0 but interior-touch-on-endpoint counted as 1 per touching
    foreign segment.
    """
    if len(ow.path) < 2:
        return 0
    # Every vertex of the insulated foreign wire is a pad where the wire
    # physically rests (endpoints anchored at pins, mid-vertices at
    # Knots / Steiner points).  Include all of them.
    ow_anchor_set = {(p.x, p.y) for p in ow.path}
    # Track which anchor pads we have already counted so the same pad
    # contributes at most once per subpath.
    counted: set[tuple[float, float]] = set()
    # Vertex coincidence: subpath vertex == foreign anchor.
    for cv in subpath_vertices:
        key = (cv.x, cv.y)
        if key in skip_pts:
            continue
        if key in ow_anchor_set and key not in counted:
            counted.add(key)
    # Foreign anchor strictly inside a subpath segment — the subpath
    # passes over the foreign's pad without using it as a vertex.
    for c_src, c_dst in subpath_segs:
        c_lo_x = c_src.x if c_src.x < c_dst.x else c_dst.x
        c_hi_x = c_src.x if c_src.x > c_dst.x else c_dst.x
        c_lo_y = c_src.y if c_src.y < c_dst.y else c_dst.y
        c_hi_y = c_src.y if c_src.y > c_dst.y else c_dst.y
        for ax, ay in ow_anchor_set:
            if (ax, ay) in skip_pts or (ax, ay) in counted:
                continue
            if (
                ax < c_lo_x - _EPS
                or ax > c_hi_x + _EPS
                or ay < c_lo_y - _EPS
                or ay > c_hi_y + _EPS
            ):
                continue
            if _point_on_segment_interior(Point(ax, ay), c_src, c_dst):
                counted.add((ax, ay))
    return len(counted)


def _candidate_safe_against_others(
    candidate_segs: list[tuple[Point, Point]],
    other_wires: list[Wire],
    wire_id: uuid.UUID,
    *,
    wire_side: str | None = None,
    wire_net_id: uuid.UUID | None = None,
    other_wire_nets: dict[uuid.UUID, uuid.UUID | None] | None = None,
    original_endpoints: tuple[Point, Point] | None = None,
    original_subpath_segs: list[tuple[Point, Point]] | None = None,
) -> bool:
    """Return True iff *candidate_segs* does not introduce *new* crossings
    with any other wire compared to the original subpath being replaced.

    User rule "0 nowych przecięć": for every other wire ``W2`` of a foreign
    net, the count of physical contacts between the *candidate* and W2 must
    be ≤ the count between the *original subpath* and W2.  Crossings may
    shift geometrically but not multiply.

    Three contact modes are counted (mirrored on the original and the
    candidate side for a fair comparison):

    * **Bare×Bare segment**: crossing / T-junction with a bare wire on
      the same side.
    * **Bare×Bare shared vertex**: vertex coincidence (shared pad).
    * **Bare×Insulated endpoint**: a candidate segment passing through
      or terminating at an insulated wire's pad-endpoint.

    Insulated×anything mid-segment is air — never counted.

    Same-net other-wires never block — extending or sharing a pad with a
    wire of our own net is intentional, and those contacts are excluded
    from the count entirely.

    ``original_subpath_segs`` — segments of the subpath being replaced.
    When omitted the function reverts to the strict "no-contact" rule
    (count > 0 → reject), preserving the legacy contract for callers
    that don't pass the original subpath.

    ``wire_net_id`` may be None when the wire's net is unknown; in that
    case any contact with a netted other-wire is treated as alien
    (conservative).
    """
    other_nets = other_wire_nets or {}
    # Collect every vertex on the candidate path.
    cand_vertices: list[Point] = []
    if candidate_segs:
        cand_vertices.append(candidate_segs[0][0])
        for _src, dst in candidate_segs:
            cand_vertices.append(dst)
    # Endpoints of the replaced subpath — contacts AT these points are
    # pre-existing and skipped on both sides of the comparison.
    skip_pts: set[tuple[float, float]] = set()
    if original_endpoints is not None:
        for ep in original_endpoints:
            skip_pts.add((ep.x, ep.y))

    # Vertices of the original subpath (for the baseline count).  When the
    # caller doesn't supply original_subpath_segs we fall back to the
    # strict "no-contact" semantics — baseline_count = 0.
    if original_subpath_segs is not None:
        orig_segs = original_subpath_segs
        orig_vertices: list[Point] = []
        if orig_segs:
            orig_vertices.append(orig_segs[0][0])
            for _s, d in orig_segs:
                orig_vertices.append(d)
    else:
        orig_segs = []
        orig_vertices = []

    # Bounding box covering BOTH the candidate and the original subpath.
    # Other wires whose bbox doesn't overlap this union cannot contribute
    # any contact — saves ~99% of pair checks on realistic boards.
    union_lo_x = math.inf
    union_lo_y = math.inf
    union_hi_x = -math.inf
    union_hi_y = -math.inf
    for pts in (cand_vertices, orig_vertices):
        for p in pts:
            if p.x < union_lo_x:
                union_lo_x = p.x
            if p.x > union_hi_x:
                union_hi_x = p.x
            if p.y < union_lo_y:
                union_lo_y = p.y
            if p.y > union_hi_y:
                union_hi_y = p.y

    # Two-tier acceptance:
    #
    # * BARE foreign wires use the legacy per-wire M ≤ N rule.  A bare
    #   conductor on the same side carrying a different net is an
    #   electrical short the moment our wire touches it — strict
    #   accounting per individual wire is mandatory (HARD rule).
    #
    # * INSULATED foreign wires use a SUM-BASED rule across all foreign
    #   insulated wires.  Two insulated wires crossing in 2D is not an
    #   electrical short (one floats over the other), so simplify may
    #   shift a crossing from one foreign insulated wire to another as
    #   long as the *total* count of insulated foreign-wire contacts
    #   does not grow.  Lets simplify move our path through a denser
    #   bundle when an equivalent contact already existed elsewhere.
    #
    # Cross-side: electrically harmless even for bare-bare (different
    # layers).  Keep the legacy per-wire overlap check.
    baseline_ins_total = 0
    candidate_ins_total = 0
    for ow in other_wires:
        if ow.id == wire_id:
            continue
        ow_net = other_nets.get(ow.id)
        same_net = (
            wire_net_id is not None and ow_net is not None and ow_net == wire_net_id
        )
        if same_net:
            continue
        cross_side = wire_side is not None and ow.side != wire_side
        if cross_side:
            # Cross-side collinear overlap (two wires sharing a track on
            # different sides — visible as a stacked run).  Strict per-
            # wire M ≤ N.
            cand_overlap = 0
            for c_src, c_dst in candidate_segs:
                for o_src, o_dst in zip(ow.path, ow.path[1:]):
                    if _segs_overlap_collinear(c_src, c_dst, o_src, o_dst):
                        cand_overlap += 1
            orig_overlap = 0
            for o_src_inner, o_dst_inner in orig_segs:
                for o_src, o_dst in zip(ow.path, ow.path[1:]):
                    if _segs_overlap_collinear(
                        o_src_inner,
                        o_dst_inner,
                        o_src,
                        o_dst,
                    ):
                        orig_overlap += 1
            if cand_overlap > orig_overlap:
                if _SIMPLIFY_TRACE:
                    _log.info(
                        "simplify.crosses_other.cross_side_overlap_grew",
                        wire_id=str(wire_id),
                        ow_id=str(ow.id),
                        ow_side=ow.side,
                        baseline=orig_overlap,
                        candidate=cand_overlap,
                    )
                return False
            # Cross-side shared-pad check.  Even on different copper
            # layers two wires that touch the same pad sit on the same
            # plated-through hole / pad-hole metallisation — physically
            # one electrical node.  Bare cross-side shared pad shorts
            # the two nets; insulated cross-side shared pad obstructs
            # solder access and is treated like same-side pad coincidence.
            # Bare on the same pad as ANY other-wire is a hard-reject
            # (M ≤ N per wire); insulated×insulated goes into the
            # sum-based pool so total cross-side contacts may shift but
            # not grow.
            if not ow.insulated:
                baseline = (
                    _count_crossings_with_other(
                        orig_segs,
                        orig_vertices,
                        ow,
                        skip_pts=skip_pts,
                    )
                    if original_subpath_segs is not None
                    else 0
                )
                candidate = _count_crossings_with_other(
                    candidate_segs,
                    cand_vertices,
                    ow,
                    skip_pts=skip_pts,
                )
                if candidate > baseline:
                    if _SIMPLIFY_TRACE:
                        _log.info(
                            "simplify.crosses_other.cross_side_bare_grew",
                            wire_id=str(wire_id),
                            ow_id=str(ow.id),
                            ow_side=ow.side,
                            baseline=baseline,
                            candidate=candidate,
                        )
                    return False
                continue
            baseline_ins = (
                _count_insulated_pad_contacts(
                    orig_segs,
                    orig_vertices,
                    ow,
                    skip_pts=skip_pts,
                )
                if original_subpath_segs is not None
                else 0
            )
            candidate_ins = _count_insulated_pad_contacts(
                candidate_segs,
                cand_vertices,
                ow,
                skip_pts=skip_pts,
            )
            if _SIMPLIFY_TRACE and (baseline_ins > 0 or candidate_ins > 0):
                _log.info(
                    "simplify.crosses_other.cross_side_insulated_contact",
                    wire_id=str(wire_id),
                    ow_id=str(ow.id),
                    ow_side=ow.side,
                    baseline=baseline_ins,
                    candidate=candidate_ins,
                )
            baseline_ins_total += baseline_ins
            candidate_ins_total += candidate_ins
            continue
        # Fast bbox-vs-bbox reject.
        ow_lo_x = ow_lo_y = math.inf
        ow_hi_x = ow_hi_y = -math.inf
        for p in ow.path:
            if p.x < ow_lo_x:
                ow_lo_x = p.x
            if p.x > ow_hi_x:
                ow_hi_x = p.x
            if p.y < ow_lo_y:
                ow_lo_y = p.y
            if p.y > ow_hi_y:
                ow_hi_y = p.y
        if (
            ow_hi_x < union_lo_x - _EPS
            or ow_lo_x > union_hi_x + _EPS
            or ow_hi_y < union_lo_y - _EPS
            or ow_lo_y > union_hi_y + _EPS
        ):
            continue
        # Bare foreign wires: strict per-wire (HARD rule, electrical short).
        if not ow.insulated:
            baseline = (
                _count_crossings_with_other(
                    orig_segs,
                    orig_vertices,
                    ow,
                    skip_pts=skip_pts,
                )
                if original_subpath_segs is not None
                else 0
            )
            candidate = _count_crossings_with_other(
                candidate_segs,
                cand_vertices,
                ow,
                skip_pts=skip_pts,
            )
            if candidate > baseline:
                if _SIMPLIFY_TRACE:
                    _log.info(
                        "simplify.crosses_other.bare_grew",
                        wire_id=str(wire_id),
                        ow_id=str(ow.id),
                        ow_net=str(ow_net) if ow_net is not None else None,
                        ow_side=ow.side,
                        baseline=baseline,
                        candidate=candidate,
                        ow_path=[(p.x, p.y) for p in ow.path],
                    )
                return False
            continue
        # Insulated foreign wires: count pad-coincidence symmetrically.
        # HARD rule for insulated×insulated in simplify: only alien
        # pin/joint/via gate (handled elsewhere — `_candidate_no_new_pin_contact`).
        # Visual diagonal crossings between two insulated wires that do
        # NOT share a pad are transparent (air gap, no physical contact).
        baseline_ins = (
            _count_insulated_pad_contacts(
                orig_segs,
                orig_vertices,
                ow,
                skip_pts=skip_pts,
            )
            if original_subpath_segs is not None
            else 0
        )
        candidate_ins = _count_insulated_pad_contacts(
            candidate_segs,
            cand_vertices,
            ow,
            skip_pts=skip_pts,
        )
        if _SIMPLIFY_TRACE and (baseline_ins > 0 or candidate_ins > 0):
            _log.info(
                "simplify.crosses_other.insulated_contact",
                wire_id=str(wire_id),
                ow_id=str(ow.id),
                ow_net=str(ow_net) if ow_net is not None else None,
                ow_side=ow.side,
                baseline=baseline_ins,
                candidate=candidate_ins,
                ow_path=[(p.x, p.y) for p in ow.path],
            )
        baseline_ins_total += baseline_ins
        candidate_ins_total += candidate_ins
    # Sum-based gate for insulated foreign wires.  Total contacts may
    # shift between wires but must not grow overall.
    if candidate_ins_total > baseline_ins_total:
        if _SIMPLIFY_TRACE:
            _log.info(
                "simplify.crosses_other.insulated_sum_grew",
                wire_id=str(wire_id),
                baseline_total=baseline_ins_total,
                candidate_total=candidate_ins_total,
            )
        return False
    return True


def _candidate_pins_alien(
    candidate_segs: list[tuple[Point, Point]],
    candidate_vertices: list[Point],
    pin_net_map: dict[Point, uuid.UUID] | None,
    wire_net_id: uuid.UUID | None,
    original_path: list[Point],
) -> bool:
    """True if the candidate path passes through (or terminates at) a pin
    whose net differs from *wire_net_id*.

    *pin_net_map* — pad position → net id for every component pin assigned
    to a net.  Pins not in the map carry no net yet (floating) and are not
    considered alien.
    """
    if not pin_net_map:
        return False
    own_pts = {(round(p.x, 6), round(p.y, 6)) for p in original_path}
    for pad, net_id in pin_net_map.items():
        if wire_net_id is not None and net_id == wire_net_id:
            continue  # same net — OK
        key = (round(pad.x, 6), round(pad.y, 6))
        if key in own_pts:
            continue  # pin already on original path — unchanged contact
        # Pass-through check on every candidate segment.
        for c_src, c_dst in candidate_segs:
            if _seg_passes_through_pad(c_src, c_dst, pad):
                return True
        # Vertex coincidence at a candidate interior vertex.
        for v in candidate_vertices[1:-1]:
            if abs(v.x - pad.x) < _EPS and abs(v.y - pad.y) < _EPS:
                return True
    return False


def _candidate_safe_against_own(
    candidate_segs: list[tuple[Point, Point]],
    wire: Wire,
    i: int,
    j: int,
) -> bool:
    """No crossing between the candidate and the wire's *unchanged* segments
    (those before index i and from index j onwards)."""
    own = list(zip(wire.path, wire.path[1:]))
    # The segments being replaced are own[i:j]; everything else stays.
    keep = own[:i] + own[j:]
    for c_src, c_dst in candidate_segs:
        for o_src, o_dst in keep:
            if _segs_cross(c_src, c_dst, o_src, o_dst):
                return False
    return True


def _seg_passes_through_pad(a: Point, b: Point, pad: Point) -> bool:
    """True if *pad* lies strictly inside or at the endpoint of segment a→b."""
    # Cross product zero → collinear; then check param t in [0, 1].
    ab = b - a
    ap = pad - a
    if abs(ab.cross(ap)) > _EPS:
        return False
    # Collinear, so t = (ap · ab) / |ab|² is the projection parameter
    # (degenerate ab guarded by the near-zero length check).
    ab_sq = ab.dot(ab)
    if ab_sq < _EPS * _EPS:
        return False
    t = ap.dot(ab) / ab_sq
    return -_EPS <= t <= 1.0 + _EPS


def _new_alien_pad_contact(
    candidate_segs: list[tuple[Point, Point]],
    wire: Wire,
    pad_to_net: dict[Point, uuid.UUID] | None,
    wire_net_id: uuid.UUID | None = None,
) -> bool:
    """Return True if the candidate creates an unintended bare contact with
    a pad belonging to *another* electric net than the wire's own.

    Insulated wires never trigger this — they float over pads.

    A pad is "alien" when its net id differs from *wire_net_id*.  When the
    wire's own net is unknown (``wire_net_id is None``) every netted pad
    not on the original path is treated as alien — conservative fallback
    that may over-block but never under-blocks.
    """
    if wire.insulated or pad_to_net is None:
        return False
    own_path_pts = set(wire.path)
    for c_src, c_dst in candidate_segs:
        for pad, pad_net in pad_to_net.items():
            if pad in own_path_pts:
                continue  # already touched by the original wire
            if wire_net_id is not None and pad_net == wire_net_id:
                continue  # same net — extending the wire's own contact
            if not _seg_passes_through_pad(c_src, c_dst, pad):
                continue
            return True
    return False


def _parity_ok_for_bare(
    candidate_segs: list[tuple[Point, Point]], src_parity: int
) -> bool:
    """In BARE_PARITY mode each diagonal segment must respect the parity
    family of the source pad (anti-short rule used by bare_diagonal routing).
    Pure-ortho segments are always OK.
    """
    allowed_sign = 1.0 if src_parity == 0 else -1.0
    for a, b in candidate_segs:
        dx, dy = b.x - a.x, b.y - a.y
        if abs(dx) < _EPS or abs(dy) < _EPS:
            continue  # ortho
        slope_sign = dx * dy
        if slope_sign * allowed_sign <= 0.0:
            return False
    return True


def _candidate_no_new_pin_contact(
    candidate_vertices: list[Point],
    original_path: list[Point],
    pad_index: object,
    wire_side: str,
    wire_net_id: uuid.UUID | None,
    wire_is_insulated: bool,
) -> bool:
    """Return True if the candidate path introduces no new HARD contact
    AND does not increase the total count of soft pad-level crossings
    against foreign wires.

    Two classes of pad contact are evaluated separately:

    Hard contacts (absolute reject if ANY new one appears on the candidate):
      * Alien ``PinItem`` (pin of another component, foreign or floating net).
      * Hard-blocking ``ComponentBody`` (``body_blocks_routing=True``).
      * ``SHORT_CIRCUIT`` net on this side.
      * For bare wires: any foreign-net electrical net on this side
        (``can_route_through`` rejects).

    Soft contacts (counted; reject only when candidate total > baseline total):
      * Alien ``InsulatedWireItem`` / ``InsulatedWireCrossing`` on the same
        side, regardless of which foreign wire.  These represent another
        net's insulated wire sharing this pad — physically two wires sit
        on the same copper, electrically harmless (both insulated), but
        a stack-up the user prefers to minimise.  Simplify may *shift*
        these from one pad to another as long as the total count does
        not grow.

    Mid-segment crossings without pad vertex coincidence are out of scope
    here — those are handled by ``_candidate_safe_against_others`` via the
    M ≤ N rule on geometric crossings.

    Pads strictly on the original path are pre-existing contacts and excluded
    from both counts (hard) and the candidate-side tally (soft).
    """
    if pad_index is None:
        return True
    try:
        from protoboard.domain.board.pad_state import (
            ComponentBody,
            InsulatedWireCrossing,
            InsulatedWireItem,
            PinItem,
        )
    except ImportError:
        return True
    orig_keys = {(round(p.x, 6), round(p.y, 6)) for p in original_path}
    wire_type = "insulated" if wire_is_insulated else "bare"

    def _is_hard_blocker(pad: Pad) -> bool:
        """Hard-block contacts that the candidate must NOT introduce, even
        if it just shifts them from an existing position.  Reject reasons:
        SHORT_CIRCUIT, alien same-side bare conductor, hard-blocking body,
        or alien PinItem (any side)."""
        # ``can_route_through`` already encodes SHORT, bare-foreign-net
        # short, and hard-blocking ComponentBody.  Reusing it keeps the
        # decision authoritative.  But we need to be careful: it ALSO
        # rejects insulated-on-insulated same-side conflicts which we
        # want to count softly, not absolutely.  So we strip those from
        # the verdict by re-checking the pad ourselves.
        if not pad_index.can_route_through(  # type: ignore[attr-defined]
            pad.position,
            wire_side,
            wire_net_id,
            wire_type,
        ):
            # Was the rejection caused by alien insulated wire only?  If
            # yes, it's a SOFT contact (counted elsewhere).  Otherwise
            # something else hard-blocks — SHORT, body, or alien bare.
            blocked_by = pad.physically_blocked_on(wire_side)
            only_insulated_alien = True
            for it in blocked_by:
                if isinstance(it, (InsulatedWireItem, InsulatedWireCrossing)):
                    if wire_net_id is not None and it.net == wire_net_id:
                        continue  # same net — irrelevant
                    continue  # alien insulated — soft
                if isinstance(it, ComponentBody):
                    if it.blocks_routing:
                        only_insulated_alien = False
                        break
                    continue  # non-blocking body — irrelevant
                # Anything else that physically blocks (bare wires, etc.)
                # is hard.
                only_insulated_alien = False
                break
            if not only_insulated_alien:
                if _SIMPLIFY_TRACE:
                    items_dump = []
                    for it in pad.iter_items(side=None):
                        items_dump.append(
                            {
                                "type": type(it).__name__,
                                "id": str(getattr(it, "id", None)),
                                "net": str(getattr(it, "net", None)),
                            }
                        )
                    _log.info(
                        "simplify.pad_blocks.hard",
                        pad_pos=(pad.position.x, pad.position.y),
                        wire_side=wire_side,
                        wire_net_id=(
                            str(wire_net_id) if wire_net_id is not None else None
                        ),
                        wire_type=wire_type,
                        items_on_pad=items_dump,
                    )
                return True
        # Alien PinItem on either side — always hard.
        for it in pad.iter_items(side=None):
            if not isinstance(it, PinItem):
                continue
            if wire_net_id is not None and it.net == wire_net_id:
                continue
            if _SIMPLIFY_TRACE:
                _log.info(
                    "simplify.pad_blocks.alien_pin",
                    pad_pos=(pad.position.x, pad.position.y),
                    wire_side=wire_side,
                    wire_net_id=(str(wire_net_id) if wire_net_id is not None else None),
                    pin_id=str(getattr(it, "id", None)),
                    pin_number=getattr(it, "pin_number", None),
                    pin_net=str(getattr(it, "net", None)),
                )
            return True
        return False

    def _has_soft_insulated_contact(pad: Pad) -> bool:
        """Pad hosts an alien InsulatedWireItem/Crossing on the wire's side.
        Such contacts are counted (shift-able) rather than absolutely
        rejected — two insulated wires sharing a pad is mechanical but
        electrically harmless."""
        for it in pad.physically_blocked_on(wire_side):
            if not isinstance(it, (InsulatedWireItem, InsulatedWireCrossing)):
                continue
            if wire_net_id is not None and it.net == wire_net_id:
                continue  # same net — not a foreign contact
            return True
        return False

    def _iter_pads_along(path: list[Point]) -> list[Pad]:
        """Every pad touched by *path* (vertices + mid-segment pads).
        De-duplicates by pad position so each pad counts at most once."""
        seen: set[tuple[float, float]] = set()
        out: list[Pad] = []
        # Vertex pads.
        for v in path:
            key = (round(v.x, 6), round(v.y, 6))
            if key in seen:
                continue
            seen.add(key)
            pad = pad_index.pad_at(v)  # type: ignore[attr-defined]
            if pad is not None:
                out.append(pad)
        # Mid-segment pads.
        for i in range(len(path) - 1):
            a = path[i]
            b = path[i + 1]
            try:
                pads_near = pad_index.pads_near_segment(  # type: ignore[attr-defined]
                    a.x,
                    a.y,
                    b.x,
                    b.y,
                )
            except AttributeError:
                return out  # caller passed a non-PadIndex object
            for pad in pads_near:
                key = (round(pad.position.x, 6), round(pad.position.y, 6))
                if key in seen:
                    continue
                seen.add(key)
                out.append(pad)
        return out

    # Hard-block check first: any NEW hard contact on the candidate aborts.
    for pad in _iter_pads_along(candidate_vertices):
        key = (round(pad.position.x, 6), round(pad.position.y, 6))
        if key in orig_keys:
            continue  # pre-existing — not "new"
        if _is_hard_blocker(pad):
            return False
    # Soft-contact count: candidate total ≤ baseline total.  Foreign
    # insulated wires sharing a pad with our wire are tolerated as long
    # as the simplification does not multiply them.
    baseline_soft = sum(
        1 for pad in _iter_pads_along(original_path) if _has_soft_insulated_contact(pad)
    )
    candidate_soft = sum(
        1
        for pad in _iter_pads_along(candidate_vertices)
        if _has_soft_insulated_contact(pad)
    )
    if candidate_soft > baseline_soft:
        if _SIMPLIFY_TRACE:
            _log.info(
                "simplify.pad_blocks.soft_grew",
                wire_side=wire_side,
                wire_net_id=(str(wire_net_id) if wire_net_id is not None else None),
                baseline_soft=baseline_soft,
                candidate_soft=candidate_soft,
            )
        return False
    return True


def _candidate_safe_via_pad_index(
    candidate_vertices: list[Point],
    original_endpoints: tuple[Point, Point],
    pad_index: object,
    wire_side: str,
    wire_net_id: uuid.UUID | None,
    wire_is_insulated: bool,
) -> bool:
    """Pad-state check for every pad the candidate path touches.

    For *bare* wires the conductor physically rests on every pad the
    path crosses, including pads between two adjacent vertices that the
    path skips over (mid-segment crossings).  For *insulated* wires
    only the vertices make physical contact with copper, so mid-segment
    crossings don't need a pad-state check.

    For BARE wires this is an absolute hard-block gate: any pad that
    ``can_route_through`` rejects (SHORT, alien-net bare, body,
    plated-through cross-layer short) is non-negotiable — bare wires
    must never extend electrical shorts.

    For INSULATED wires the gate splits into hard vs soft blockers:
    SHORT_CIRCUIT and hard-blocking ComponentBody hard-reject, while
    alien same-side insulated wires are SOFT (tolerated, counted by
    ``_candidate_no_new_pin_contact`` in sum-based fashion).
    """
    if pad_index is None:
        return True  # caller hasn't migrated yet — preserve legacy path
    wire_type = "insulated" if wire_is_insulated else "bare"
    ep0_x, ep0_y = original_endpoints[0].x, original_endpoints[0].y
    ep1_x, ep1_y = original_endpoints[1].x, original_endpoints[1].y

    def _is_endpoint(px: float, py: float) -> bool:
        return (abs(px - ep0_x) < _EPS and abs(py - ep0_y) < _EPS) or (
            abs(px - ep1_x) < _EPS and abs(py - ep1_y) < _EPS
        )

    if wire_is_insulated:
        try:
            from protoboard.domain.board.pad_state import (
                ComponentBody,
                InsulatedWireCrossing,
                InsulatedWireItem,
            )
        except ImportError:
            return True

        def _pad_is_hard_block_insulated(pad: Pad) -> bool:
            """For insulated wires only: hard-reject SHORT, body, alien
            bare.  Alien insulated same-side is soft."""
            if pad_index.can_route_through(  # type: ignore[attr-defined]
                pad.position,
                wire_side,
                wire_net_id,
                wire_type,
            ):
                return False
            blocked_by = pad.physically_blocked_on(wire_side)
            for it in blocked_by:
                if isinstance(it, (InsulatedWireItem, InsulatedWireCrossing)):
                    if wire_net_id is not None and it.net == wire_net_id:
                        continue
                    continue  # alien insulated — soft
                if isinstance(it, ComponentBody):
                    if it.blocks_routing:
                        return True
                    continue  # non-blocking body — irrelevant
                return True  # bare conductor or anything else hard
            return False

        for v in candidate_vertices:
            if _is_endpoint(v.x, v.y):
                continue
            pad = pad_index.pad_at(v)  # type: ignore[attr-defined]
            if pad is None:
                continue
            if _pad_is_hard_block_insulated(pad):
                return False
        return True  # insulated: no mid-segment pad check needed

    # BARE wire — strict legacy semantics.  Any can_route_through reject
    # is a hard physical short.  No softening allowed; the tangled fixture
    # tests would regress if we did.
    for v in candidate_vertices:
        if _is_endpoint(v.x, v.y):
            continue
        if not pad_index.can_route_through(  # type: ignore[attr-defined]
            v,
            wire_side,
            wire_net_id,
            wire_type,
        ):
            return False
    vertex_keys = {(round(v.x, 4), round(v.y, 4)) for v in candidate_vertices}
    for i in range(len(candidate_vertices) - 1):
        a = candidate_vertices[i]
        b = candidate_vertices[i + 1]
        try:
            pads_near = pad_index.pads_near_segment(  # type: ignore[attr-defined]
                a.x,
                a.y,
                b.x,
                b.y,
            )
        except AttributeError:
            return True
        for pad in pads_near:
            key = (round(pad.position.x, 4), round(pad.position.y, 4))
            if key in vertex_keys:
                continue
            if _is_endpoint(pad.position.x, pad.position.y):
                continue
            if not pad_index.can_route_through(  # type: ignore[attr-defined]
                pad.position,
                wire_side,
                wire_net_id,
                wire_type,
            ):
                return False
    return True


def generic_simplify_wire(
    wire: Wire,
    other_wires: list[Wire],
    pin_positions: set[Point] | None = None,
    pad_to_net: dict[Point, uuid.UUID] | None = None,
    *,
    mode: SimplifyMode = SimplifyMode.BARE_PARITY,
    board_pads: set[Point] | None = None,
    wire_net_id: uuid.UUID | None = None,
    other_wire_nets: dict[uuid.UUID, uuid.UUID | None] | None = None,
    pin_net_map: dict[Point, uuid.UUID] | None = None,
    pad_index: object = None,
) -> Wire:
    """Shorten *wire* by replacing any subpath ``path[i..j]`` with the
    canonical (grid-aligned shortest) path between its endpoints, when safe.

    Generalises ``straighten_wire`` (collapses kolinearne wierzchołki) and
    ``corner_cut_wire`` (zamienia ortho-L na diagonal) into a single pass:
    for every pair ``(i, j)`` the canonical replacement is tested.  Greedy
    largest-span-first iteration with restart on success.

    Safety constraints:
      - Total length must strictly decrease.
      - No crossing with other bare wires (`_segs_cross`).
      - No crossing with this wire's segments outside ``[i..j]``.
      - Internal pad-pins on the original subpath must remain on the new path
        (otherwise we would silently disconnect a multi-pin net).
      - For bare wires in BARE_PARITY mode, every new diagonal segment must
        match the source pad's parity family.
      - For bare wires, the new path must not introduce a contact with a pad
        belonging to another net (``pad_to_net`` lookup).

    *pin_positions* — every component pin position on the layout.
    *pad_to_net*    — pad → electric-net id for every pad on the same side as
                      the wire (used to detect alien-net contacts).
    """
    pins = pin_positions or set()
    # Foreign pads are pads that don't belong to this wire's net set.  Caller
    # passes pad_to_net pre-filtered if it wants strict alien-only checks; we
    # operate on whatever it gave us.
    src_parity = _parity_of(wire.path[0]) if wire.path else 0
    allow_l_shape = True
    use_parity = mode == SimplifyMode.BARE_PARITY and not wire.insulated

    # Pre-filter other_wires by full-path bbox: any other wire whose bbox
    # cannot overlap our wire's full bbox can never contact any subpath of
    # our wire, so we can drop it from consideration once.  Crucial for
    # boards with hundreds of wires — without it every candidate pair re-
    # iterates the entire list.
    #
    # Cross-side wires are KEPT in the relevant list: user rule "no
    # overlay along another net's geometry, even on the opposite side"
    # means we still check collinear overlap against them.
    # ``_candidate_safe_against_others`` handles the side semantics.
    if other_wires and wire.path:
        wire_lo_x = min(p.x for p in wire.path) - 1.0
        wire_hi_x = max(p.x for p in wire.path) + 1.0
        wire_lo_y = min(p.y for p in wire.path) - 1.0
        wire_hi_y = max(p.y for p in wire.path) + 1.0
        relevant_others: list[Wire] = []
        for ow in other_wires:
            if ow.id == wire.id:
                continue
            ow_lo_x = min(p.x for p in ow.path)
            ow_hi_x = max(p.x for p in ow.path)
            ow_lo_y = min(p.y for p in ow.path)
            ow_hi_y = max(p.y for p in ow.path)
            if (
                ow_hi_x < wire_lo_x
                or ow_lo_x > wire_hi_x
                or ow_hi_y < wire_lo_y
                or ow_lo_y > wire_hi_y
            ):
                continue
            relevant_others.append(ow)
        other_wires = relevant_others

    # Optional diagnostic: log every rejected candidate with the reason and
    # enough context to reproduce.  Gated on SIMPLIFY_TRACE env var to keep
    # production silent on dense boards.  Helper captures wire identity once
    # so per-call sites stay compact.
    if _SIMPLIFY_TRACE:

        def _trace_reject(reason: str, i: int, j: int, /, **extra: object) -> None:
            _log.info(
                "simplify.reject",
                wire_id=str(wire.id),
                insulated=wire.insulated,
                side=wire.side,
                wire_net_id=str(wire_net_id) if wire_net_id is not None else None,
                i=i,
                j=j,
                p_i=(path[i].x, path[i].y) if i < len(path) else None,
                p_j=(path[j].x, path[j].y) if j < len(path) else None,
                reason=reason,
                **extra,
            )

        _log.info(
            "simplify.wire_begin",
            wire_id=str(wire.id),
            insulated=wire.insulated,
            side=wire.side,
            wire_net_id=str(wire_net_id) if wire_net_id is not None else None,
            n_vertices=len(wire.path),
            path=[(p.x, p.y) for p in wire.path],
            has_pad_index=pad_index is not None,
            n_other_wires=len(other_wires),
            n_pins=len(pins),
        )
    else:

        def _trace_reject(
            reason: str, i: int, j: int, /, **extra: object
        ) -> None:  # noqa: ARG001
            return None

    path = list(wire.path)
    changed = True
    while changed and len(path) >= 3:
        changed = False
        n = len(path)
        # Try pairs by descending span — bigger savings first.
        outer_found = False
        for span in range(n - 1, 1, -1):
            for i in range(0, n - span):
                j = i + span
                canon = _canonical_subpath(
                    path[i], path[j], allow_l_shape=allow_l_shape
                )
                if canon is None or len(canon) < 2:
                    _trace_reject("canonical_subpath_none", i, j)
                    continue
                # Skip no-ops (e.g. canon == path[i:j+1] for a pure two-vertex
                # subpath that already is canonical).
                if len(canon) == j - i + 1 and all(
                    abs(a.x - b.x) < _EPS and abs(a.y - b.y) < _EPS
                    for a, b in zip(canon, path[i : j + 1])
                ):
                    _trace_reject(
                        "canonical_is_noop", i, j, canon=[(p.x, p.y) for p in canon]
                    )
                    continue
                # Accept the canonical if either:
                #   (a) it's geometrically shorter, OR
                #   (b) length is unchanged but vertex count decreases
                #       (this covers the classic "straighten" case where the
                #       canonical is the same straight line but with fewer
                #       intermediate vertices), OR
                #   (c) length and vertex count are unchanged but the
                #       canonical has fewer 90° corners — soldering preference:
                #       2×45° rounds better than 1×90°, so a same-cost
                #       replacement that smooths sharp bends is worth taking.
                cur_len = _path_length(path[i : j + 1])
                cand_len = _path_length(canon)
                shorter = cand_len < cur_len - _EPS
                fewer_verts = abs(cand_len - cur_len) < _EPS and len(canon) < (
                    j - i + 1
                )
                fewer_90s = (
                    abs(cand_len - cur_len) < _EPS
                    and len(canon) <= (j - i + 1)
                    and _count_90_corners(canon) < _count_90_corners(path[i : j + 1])
                )
                if not shorter and not fewer_verts and not fewer_90s:
                    _trace_reject(
                        "not_shorter_not_fewer",
                        i,
                        j,
                        cur_len=cur_len,
                        cand_len=cand_len,
                        cur_verts=j - i + 1,
                        cand_verts=len(canon),
                        cur_90s=_count_90_corners(path[i : j + 1]),
                        cand_90s=_count_90_corners(canon),
                    )
                    continue

                # Preserve internal pad-pins (Steiner points the router put
                # there on purpose).
                internal = path[i + 1 : j]
                internal_pins = {p for p in internal if p in pins}
                if internal_pins and not internal_pins.issubset(set(canon)):
                    _trace_reject(
                        "would_drop_internal_pin",
                        i,
                        j,
                        dropped=[(p.x, p.y) for p in internal_pins - set(canon)],
                    )
                    continue

                candidate_segs = list(zip(canon, canon[1:]))
                if use_parity and not _parity_ok_for_bare(candidate_segs, src_parity):
                    _trace_reject(
                        "parity_violation",
                        i,
                        j,
                        src_parity=src_parity,
                        canon=[(p.x, p.y) for p in canon],
                    )
                    continue
                # Pad-state authoritative check (preferred path when caller
                # supplied a PadIndex).  Catches: shared-pad shorts, alien
                # pins, plated-through cross-layer shorts, insulated endpoint
                # contacts — all the cases that legacy heuristics missed.
                if pad_index is not None and not _candidate_safe_via_pad_index(
                    list(canon),
                    (path[i], path[j]),
                    pad_index,
                    wire.side,
                    wire_net_id,
                    wire.insulated,
                ):
                    _trace_reject(
                        "pad_index_blocks_via_can_route_through",
                        i,
                        j,
                        canon=[(p.x, p.y) for p in canon],
                    )
                    continue
                # Stricter pad rule: simplify must not introduce a NEW pin
                # contact (alien-net pins, including floating pins of other
                # components).  Pads already on the original path are
                # pre-existing contacts and stay legal.
                if pad_index is not None and not _candidate_no_new_pin_contact(
                    list(canon),
                    list(wire.path),
                    pad_index,
                    wire.side,
                    wire_net_id,
                    wire.insulated,
                ):
                    _trace_reject(
                        "new_pin_contact_or_alien_pad",
                        i,
                        j,
                        canon=[(p.x, p.y) for p in canon],
                    )
                    continue
                # Geometric mid-air crossings — PadIndex doesn't know about
                # segments BETWEEN pads, so this stays.  Rule: M ≤ N — the
                # candidate may keep (or shift) existing crossings of the
                # replaced subpath but must not introduce new ones.  Pass
                # the original subpath segs so the count is per-W2 local.
                original_subpath_segs = list(zip(path[i : j + 1], path[i + 1 : j + 1]))
                if not _candidate_safe_against_others(
                    candidate_segs,
                    other_wires,
                    wire.id,
                    wire_side=wire.side,
                    wire_net_id=wire_net_id,
                    other_wire_nets=other_wire_nets,
                    original_endpoints=(path[i], path[j]),
                    original_subpath_segs=original_subpath_segs,
                ):
                    _trace_reject(
                        "crosses_other_wire_or_grows_crossings",
                        i,
                        j,
                        canon=[(p.x, p.y) for p in canon],
                        n_other_wires=len(other_wires),
                    )
                    continue
                if not _candidate_safe_against_own(candidate_segs, wire, i, j):
                    _trace_reject(
                        "crosses_own_wire_unchanged_segments",
                        i,
                        j,
                        canon=[(p.x, p.y) for p in canon],
                    )
                    continue
                # Legacy alien-pad-contact check — kept as a fallback when
                # caller hasn't migrated to PadIndex yet.  When PadIndex is
                # provided, the per-vertex check above is authoritative and
                # this duplicate check is harmless (always passes).
                if pad_index is None and _new_alien_pad_contact(
                    candidate_segs, wire, pad_to_net, wire_net_id
                ):
                    _trace_reject(
                        "legacy_new_alien_pad_contact",
                        i,
                        j,
                        canon=[(p.x, p.y) for p in canon],
                    )
                    continue
                # Legacy alien-pin pass-through — same story.
                if (
                    pad_index is None
                    and not wire.insulated
                    and _candidate_pins_alien(
                        candidate_segs,
                        list(canon),
                        pin_net_map,
                        wire_net_id,
                        list(wire.path),
                    )
                ):
                    _trace_reject(
                        "legacy_alien_pin_passthrough",
                        i,
                        j,
                        canon=[(p.x, p.y) for p in canon],
                    )
                    continue

                # Accept: splice canon into path in place of [i..j].
                # Re-insert every board pad that lies on a segment of the new
                # canon — bare wires physically contact those pads, insulated
                # wires need them as edit/simplify/unweave "handles".  Same
                # invariant the AddWireCommand chokepoint and load_project()
                # migration enforce at wire-creation time.  Use the fast
                # enumeration helper (segments here are pure ortho or diag).
                if board_pads and len(canon) >= 2:
                    enriched: list[Point] = [canon[0]]
                    for k in range(len(canon) - 1):
                        enriched.extend(
                            _fast_pads_on_segment(
                                canon[k],
                                canon[k + 1],
                                board_pads,
                            )
                        )
                        enriched.append(canon[k + 1])
                    canon = enriched
                new_path = path[: i + 1] + canon[1:-1] + path[j:]
                # Detect no-op after pad re-insertion (the enriched canon may
                # exactly reproduce the slice it would replace).  Without this
                # guard the greedy loop would never reach a fixed point.
                if new_path == path:
                    _trace_reject(
                        "new_path_equals_old_after_enrichment",
                        i,
                        j,
                        canon=[(p.x, p.y) for p in canon],
                    )
                    continue
                # User rule "nie tworzyć nowych zagięć": never accept a
                # replacement that increases the total bend count of the
                # full wire path.  A canonical L between two endpoints can
                # have ≤1 corner *locally*, but chained against the rest of
                # the path that wasn't touched the composed result can end
                # up zigzagging more than the original.  Cheap check on the
                # full proposed path.
                in_total = _count_total_corners(path)
                out_total = _count_total_corners(new_path)
                if out_total > in_total:
                    _trace_reject(
                        "would_grow_total_corners",
                        i,
                        j,
                        in_total_corners=in_total,
                        out_total_corners=out_total,
                        canon=[(p.x, p.y) for p in canon],
                        new_path=[(p.x, p.y) for p in new_path],
                    )
                    continue
                if _SIMPLIFY_TRACE:
                    _log.info(
                        "simplify.accept",
                        wire_id=str(wire.id),
                        i=i,
                        j=j,
                        cur_len=cur_len,
                        cand_len=cand_len,
                        in_corners=in_total,
                        out_corners=out_total,
                        canon=[(p.x, p.y) for p in canon],
                    )
                path = new_path
                changed = True
                outer_found = True
                break
            if outer_found:
                break

    if _SIMPLIFY_TRACE:
        _log.info(
            "simplify.wire_done",
            wire_id=str(wire.id),
            unchanged=(path == list(wire.path)),
            n_in_vertices=len(wire.path),
            n_out_vertices=len(path),
            out_path=[(p.x, p.y) for p in path],
        )

    if path == list(wire.path):
        return wire
    # Diagnostic: simplify is supposed to never multiply bends.  If the
    # output has more corners than the input, log a structured warning so
    # the bug can be reproduced.  Keep both paths verbatim in the log so a
    # user report can be replayed against the canonical algorithm.
    in_corners = _count_total_corners(list(wire.path))
    out_corners = _count_total_corners(path)
    if out_corners > in_corners:
        _log.warning(
            "simplify.corner_growth",
            wire_id=str(wire.id),
            insulated=wire.insulated,
            side=wire.side,
            in_corners=in_corners,
            out_corners=out_corners,
            in_path=[(p.x, p.y) for p in wire.path],
            out_path=[(p.x, p.y) for p in path],
            mode=mode.value if hasattr(mode, "value") else str(mode),
        )
    # Preserve wire id — simplify is a path edit of the same logical wire.
    return Wire(
        id=wire.id,
        path=path,
        insulated=wire.insulated,
        side=cast(Side, wire.side),
        color=wire.color,
    )


def simplify_wires_generic(
    wires: list[Wire],
    pin_positions: set[Point] | None = None,
    pad_to_net: dict[Point, uuid.UUID] | None = None,
    *,
    mode: SimplifyMode = SimplifyMode.BARE_PARITY,
    board_pads: set[Point] | None = None,
    wire_to_net: dict[uuid.UUID, uuid.UUID | None] | None = None,
    pin_net_map: dict[Point, uuid.UUID] | None = None,
    pad_index: object = None,
) -> list[Wire]:
    """Run generic_simplify_wire over *wires*, propagating each rewrite back
    into the working list so later wires see the shortened versions when
    checking collisions.  Mirrors the multi-pass behaviour of
    ``simplify_wires`` but with a single unified algorithm.

    *board_pads* (optional) — every pad position on the board.  When supplied,
    bare wires keep every pad-vertex on their physical path even after
    simplification, so connectivity / wire-edit / exports see complete contact.

    *wire_to_net* (optional) — wire id → net id mapping; enables same-net
    detection so a candidate doesn't get blocked by another wire on the same
    electrical net.  *pin_net_map* — pad position → net id for every pinned
    pad, used to reject shortcuts that would run through an alien pin.
    """
    cur = list(wires)
    # If no explicit wire→net mapping was supplied but we do have a pad-to-net
    # map, derive a best-effort one: a wire's net is the net of the first
    # path point that hits a known pad.
    net_map: dict[uuid.UUID, uuid.UUID | None]
    if wire_to_net is not None:
        net_map = dict(wire_to_net)
    elif pad_to_net:
        net_map = {}
        for w in cur:
            wnet: uuid.UUID | None = None
            for pt in w.path:
                if pt in pad_to_net:
                    wnet = pad_to_net[pt]
                    break
            net_map[w.id] = wnet
    else:
        net_map = {}
    for idx in range(len(cur)):
        w = cur[idx]
        new = generic_simplify_wire(
            w,
            other_wires=cur,
            pin_positions=pin_positions,
            pad_to_net=pad_to_net,
            mode=mode,
            board_pads=board_pads,
            wire_net_id=net_map.get(w.id),
            other_wire_nets=net_map if net_map else None,
            pin_net_map=pin_net_map or pad_to_net,
            pad_index=pad_index,
        )
        cur[idx] = new
    return cur
