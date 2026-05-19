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
import uuid
from enum import Enum
from typing import cast, Literal

from protoboard.domain.layout.wire import WireSegment
from protoboard.geometry import Point
from protoboard.services.routing.astar import (
    _build_bare_seg_index,
    _segs_cross,
)


class SimplifyMode(str, Enum):
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
    abx, aby = b.x - a.x, b.y - a.y
    bcx, bcy = c.x - b.x, c.y - b.y
    # Cross product must be zero (collinear) and direction the same sign.
    cross = abx * bcy - aby * bcx
    if cross != 0.0:
        return False
    dot = abx * bcx + aby * bcy
    return dot >= 0.0


def straighten_wire(
    wire: WireSegment, board_pads: set[Point] | None = None
) -> WireSegment:
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
    return WireSegment(
        id=uuid.uuid4(),
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


def _wire_to_bare_segs(wire: WireSegment) -> set[tuple[Point, Point, str]]:
    """Decompose a wire's path into (p1, p2, side) tuples."""
    segs: set[tuple[Point, Point, str]] = set()
    for a, b in zip(wire.path, wire.path[1:]):
        segs.add((a, b, wire.side))
    return segs


def corner_cut_wire(
    wire: WireSegment,
    other_bare_wires: list[WireSegment],
    board_pads: set[Point],
) -> WireSegment:
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
                    for bsrc, bdst in (
                        s for cells in bare_idx.values() for s in cells
                    ):
                        if _segs_cross(a, c, bsrc, bdst):
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
    return WireSegment(
        id=uuid.uuid4(),
        path=cur_path,
        insulated=wire.insulated,
        side=cast(Literal["top", "bottom"], wire.side),
        color=wire.color,
    )


# ---------------------------------------------------------------------------
# Top-level driver
# ---------------------------------------------------------------------------

def simplify_wires(
    wires: list[WireSegment],
    board_pads: set[Point],
) -> list[WireSegment]:
    """Run straighten + corner-cut over *wires*.

    *board_pads* is the set of every valid pad position on the board (used to
    verify that new diagonal endpoints still land on legal pads).  Order is
    preserved; wire IDs are regenerated only when a wire's path actually
    changes — unchanged wires are returned as-is.
    """
    # First pass: straighten each wire (preserving on-pad vertices for bare).
    straightened = [straighten_wire(w, board_pads) for w in wires]
    # Second pass: corner-cut each wire against the others.
    out: list[WireSegment] = []
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
        total += math.hypot(q.x - p.x, q.y - p.y)
    return total


def _canonical_subpath(
    a: Point, b: Point,
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
        m = Point(a.x + sx * diag, a.y + sy * diag)
        base = [a, m, b]
    if board_pads:
        from protoboard.domain.layout.wire_utils import insert_pad_nodes
        return insert_pad_nodes(base, board_pads)
    return base


def _wire_to_segs(wire: WireSegment) -> list[tuple[Point, Point]]:
    return list(zip(wire.path, wire.path[1:]))


def _point_on_segment_interior(
    p: Point, a: Point, b: Point, eps: float = _EPS,
) -> bool:
    """True if *p* lies on segment a→b strictly between the endpoints.

    Used to detect T-junctions: an endpoint/vertex of one wire path that
    falls in the middle of another wire's segment.  Such a meeting is a
    physical electrical contact — ``_segs_cross`` deliberately ignores it
    because the autoroute upstream uses a separate occupancy map to catch
    these, but simplify has no such upstream and must check here.
    """
    abx, aby = b.x - a.x, b.y - a.y
    apx, apy = p.x - a.x, p.y - a.y
    cross = abx * apy - aby * apx
    if abs(cross) > eps:
        return False  # not collinear
    # Parameterise along the longer axis for numerical stability.
    if abs(abx) >= abs(aby):
        if abs(abx) < eps:
            return False
        t = apx / abx
    else:
        t = apy / aby
    return eps < t < 1.0 - eps


def _segs_cross_or_touch(
    a1: Point, a2: Point, b1: Point, b2: Point,
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


def _candidate_safe_against_others(
    candidate_segs: list[tuple[Point, Point]],
    other_wires: list[WireSegment],
    wire_id: uuid.UUID,
    *,
    wire_side: str | None = None,
    wire_net_id: "uuid.UUID | None" = None,
    other_wire_nets: "dict[uuid.UUID, uuid.UUID | None] | None" = None,
    original_endpoints: "tuple[Point, Point] | None" = None,
) -> bool:
    """Return True iff *candidate_segs* is safe against every other wire.

    A candidate is rejected when it would create a physical short to a wire
    of a *different* net.  Three failure modes:

    * **Bare×Bare segment**: any crossing or T-junction with another bare
      wire on the same side (``_segs_cross_or_touch``).

    * **Bare×Bare shared vertex (pad)**: a candidate vertex coincides with
      a vertex of another bare wire on the same side — physically the same
      pad is now soldered to two wires of different nets.
      ``_segs_cross_or_touch`` only detects strict-interior touches; this
      check handles endpoint-on-endpoint coincidence.

    * **Bare×Insulated endpoint**: insulated wires float above the board but
      are soldered to a pad at each endpoint.  If the candidate path passes
      through such an endpoint and that endpoint sits on a *foreign* net,
      that's a short.  Mid-segment insulated geometry is still ignored —
      it doesn't touch the board.

    Insulated×anything mid-segment is fine — the insulated path is air.

    Same-net other-wires never block — extending or sharing a pad with a
    wire of our own net is intentional.

    ``wire_net_id`` may be None when the wire's net is unknown; in that case
    any contact with a netted other-wire is treated as alien (conservative).

    ``original_endpoints`` — the two endpoints of the original sub-path
    being replaced.  Vertex coincidences AT those points are allowed
    because the contact already existed before simplify; we only flag
    *new* contacts introduced by the canonical replacement.
    """
    other_nets = other_wire_nets or {}
    # Collect every vertex on the candidate path (endpoints of consecutive
    # segments).  ``candidate_segs`` is the zipped list of (path[i], path[i+1])
    # pairs, so collecting the first point of each pair plus the last point
    # of the final pair gives the full vertex sequence.
    cand_vertices: list[Point] = []
    if candidate_segs:
        cand_vertices.append(candidate_segs[0][0])
        for _src, dst in candidate_segs:
            cand_vertices.append(dst)
    # Skip the two ends of the *replaced* subpath: they were already
    # vertices of the original wire and any contact there is pre-existing.
    skip_pts: set[tuple[float, float]] = set()
    if original_endpoints is not None:
        for ep in original_endpoints:
            skip_pts.add((ep.x, ep.y))

    def _same_pt(a: Point, b: Point) -> bool:
        return abs(a.x - b.x) < _EPS and abs(a.y - b.y) < _EPS

    for ow in other_wires:
        if ow.id == wire_id:
            continue
        if wire_side is not None and ow.side != wire_side:
            continue  # different physical layer — can never short
        ow_net = other_nets.get(ow.id)
        same_net = (
            wire_net_id is not None
            and ow_net is not None
            and ow_net == wire_net_id
        )
        if same_net:
            continue
        if not ow.insulated:
            # Bare-other: any geometric contact is a short.
            for c_src, c_dst in candidate_segs:
                for o_src, o_dst in _wire_to_segs(ow):
                    if _segs_cross_or_touch(c_src, c_dst, o_src, o_dst):
                        return False
            # Shared-pad check: candidate vertex coinciding with any vertex
            # of the other bare wire = both lutowane do tej samej kratki.
            ow_vertex_set = {(p.x, p.y) for p in ow.path}
            for cv in cand_vertices:
                if (cv.x, cv.y) in skip_pts:
                    continue  # pre-existing endpoint of the replaced subpath
                if (cv.x, cv.y) in ow_vertex_set:
                    return False
        else:
            # Insulated-other: only the two endpoints sit on board pads.  The
            # candidate path passing through one of them is a short.
            if len(ow.path) < 2:
                continue
            endpoints = (ow.path[0], ow.path[-1])
            for c_src, c_dst in candidate_segs:
                for ep in endpoints:
                    if _point_on_segment_interior(ep, c_src, c_dst):
                        return False
            for cv in cand_vertices:
                if (cv.x, cv.y) in skip_pts:
                    continue
                for ep in endpoints:
                    if _same_pt(cv, ep):
                        return False
    return True


def _candidate_pins_alien(
    candidate_segs: list[tuple[Point, Point]],
    candidate_vertices: list[Point],
    pin_net_map: "dict[Point, uuid.UUID] | None",
    wire_net_id: "uuid.UUID | None",
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
            if (
                abs(v.x - pad.x) < _EPS
                and abs(v.y - pad.y) < _EPS
            ):
                return True
    return False


def _candidate_safe_against_own(
    candidate_segs: list[tuple[Point, Point]],
    wire: WireSegment,
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
    abx, aby = b.x - a.x, b.y - a.y
    apx, apy = pad.x - a.x, pad.y - a.y
    cross = abx * apy - aby * apx
    if abs(cross) > _EPS:
        return False
    # Parameterise: pad = a + t*(b - a); use longest axis for stability.
    if abs(abx) >= abs(aby):
        if abs(abx) < _EPS:
            return False
        t = apx / abx
    else:
        t = apy / aby
    return -_EPS <= t <= 1.0 + _EPS


def _new_alien_pad_contact(
    candidate_segs: list[tuple[Point, Point]],
    wire: WireSegment,
    pad_to_net: dict[Point, uuid.UUID] | None,
    wire_net_id: "uuid.UUID | None" = None,
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


def _candidate_safe_via_pad_index(
    candidate_vertices: list[Point],
    original_endpoints: "tuple[Point, Point]",
    pad_index: object,
    wire_side: str,
    wire_net_id: "uuid.UUID | None",
    wire_is_insulated: bool,
) -> bool:
    """Pad-state check for every pad the candidate path touches.

    For *bare* wires the conductor physically rests on every pad the
    path crosses, including pads between two adjacent vertices that the
    path skips over (mid-segment crossings).  For *insulated* wires
    only the vertices make physical contact with copper, so mid-segment
    crossings don't need a pad-state check.

    Asks the PadIndex whether every relevant pad can carry a wire of
    *wire_net_id* on *wire_side*.  Endpoints of the replaced sub-path
    are skipped (they were already part of the wire).  Returns False
    on the first conflict.
    """
    if pad_index is None:
        return True  # caller hasn't migrated yet — preserve legacy path
    wire_type = "insulated" if wire_is_insulated else "bare"
    ep0_x, ep0_y = original_endpoints[0].x, original_endpoints[0].y
    ep1_x, ep1_y = original_endpoints[1].x, original_endpoints[1].y

    def _is_endpoint(px: float, py: float) -> bool:
        return (
            (abs(px - ep0_x) < _EPS and abs(py - ep0_y) < _EPS)
            or (abs(px - ep1_x) < _EPS and abs(py - ep1_y) < _EPS)
        )

    # 1. Every candidate vertex must be a permissible pad for the wire.
    for v in candidate_vertices:
        if _is_endpoint(v.x, v.y):
            continue
        if not pad_index.can_route_through(  # type: ignore[attr-defined]
            v, wire_side, wire_net_id, wire_type,
        ):
            return False
    # 2. For bare wires, every pad strictly between consecutive vertices
    #    that the segment crosses (within _CROSS_EPS of the line) must
    #    also be permissible — that is where the conductor physically
    #    rests on alien copper if simplify routes through it.
    if wire_is_insulated:
        return True
    # Quantise vertices so we can skip pads that *are* vertices already.
    vertex_keys = {(round(v.x, 4), round(v.y, 4)) for v in candidate_vertices}
    for i in range(len(candidate_vertices) - 1):
        a = candidate_vertices[i]
        b = candidate_vertices[i + 1]
        try:
            pads_near = pad_index.pads_near_segment(  # type: ignore[attr-defined]
                a.x, a.y, b.x, b.y,
            )
        except AttributeError:
            return True  # caller passed a non-PadIndex object
        for pad in pads_near:
            key = (round(pad.position.x, 4), round(pad.position.y, 4))
            if key in vertex_keys:
                continue  # already checked as a vertex above
            if _is_endpoint(pad.position.x, pad.position.y):
                continue
            if not pad_index.can_route_through(  # type: ignore[attr-defined]
                pad.position, wire_side, wire_net_id, wire_type,
            ):
                return False
    return True


def generic_simplify_wire(
    wire: WireSegment,
    other_wires: list[WireSegment],
    pin_positions: set[Point] | None = None,
    pad_to_net: dict[Point, uuid.UUID] | None = None,
    *,
    mode: SimplifyMode = SimplifyMode.BARE_PARITY,
    board_pads: set[Point] | None = None,
    wire_net_id: "uuid.UUID | None" = None,
    other_wire_nets: "dict[uuid.UUID, uuid.UUID | None] | None" = None,
    pin_net_map: "dict[Point, uuid.UUID] | None" = None,
    pad_index: object = None,
) -> WireSegment:
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
    use_parity = (
        mode == SimplifyMode.BARE_PARITY and not wire.insulated
    )

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
                    continue
                # Skip no-ops (e.g. canon == path[i:j+1] for a pure two-vertex
                # subpath that already is canonical).
                if len(canon) == j - i + 1 and all(
                    abs(a.x - b.x) < _EPS and abs(a.y - b.y) < _EPS
                    for a, b in zip(canon, path[i:j + 1])
                ):
                    continue
                # Accept the canonical if either:
                #   (a) it's geometrically shorter, OR
                #   (b) length is unchanged but vertex count decreases
                #       (this covers the classic "straighten" case where the
                #       canonical is the same straight line but with fewer
                #       intermediate vertices).
                cur_len = _path_length(path[i:j + 1])
                cand_len = _path_length(canon)
                shorter = cand_len < cur_len - _EPS
                fewer_verts = (
                    abs(cand_len - cur_len) < _EPS
                    and len(canon) < (j - i + 1)
                )
                if not shorter and not fewer_verts:
                    continue

                # Preserve internal pad-pins (Steiner points the router put
                # there on purpose).
                internal = path[i + 1:j]
                internal_pins = {p for p in internal if p in pins}
                if internal_pins and not internal_pins.issubset(set(canon)):
                    continue

                candidate_segs = list(zip(canon, canon[1:]))
                if use_parity and not _parity_ok_for_bare(
                    candidate_segs, src_parity
                ):
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
                    continue
                # Geometric mid-air crossings — PadIndex doesn't know about
                # segments BETWEEN pads, so this stays.
                if not _candidate_safe_against_others(
                    candidate_segs,
                    other_wires,
                    wire.id,
                    wire_side=wire.side,
                    wire_net_id=wire_net_id,
                    other_wire_nets=other_wire_nets,
                    original_endpoints=(path[i], path[j]),
                ):
                    continue
                if not _candidate_safe_against_own(candidate_segs, wire, i, j):
                    continue
                # Legacy alien-pad-contact check — kept as a fallback when
                # caller hasn't migrated to PadIndex yet.  When PadIndex is
                # provided, the per-vertex check above is authoritative and
                # this duplicate check is harmless (always passes).
                if pad_index is None and _new_alien_pad_contact(
                    candidate_segs, wire, pad_to_net, wire_net_id
                ):
                    continue
                # Legacy alien-pin pass-through — same story.
                if pad_index is None and not wire.insulated \
                        and _candidate_pins_alien(
                    candidate_segs, list(canon), pin_net_map,
                    wire_net_id, list(wire.path),
                ):
                    continue

                # Accept: splice canon into path in place of [i..j].
                # For bare wires re-insert any board pad that lies on a segment
                # of the new canon so downstream consumers (connectivity,
                # wire-edit, exports) see every physical pad contact.
                if board_pads and not wire.insulated and len(canon) >= 2:
                    from protoboard.domain.layout.wire_utils import insert_pad_nodes
                    canon = insert_pad_nodes(canon, board_pads)
                new_path = path[:i + 1] + canon[1:-1] + path[j:]
                # Detect no-op after pad re-insertion (the enriched canon may
                # exactly reproduce the slice it would replace).  Without this
                # guard the greedy loop would never reach a fixed point.
                if new_path == path:
                    continue
                path = new_path
                changed = True
                outer_found = True
                break
            if outer_found:
                break

    if path == list(wire.path):
        return wire
    return WireSegment(
        id=uuid.uuid4(),
        path=path,
        insulated=wire.insulated,
        side=cast(Literal["top", "bottom"], wire.side),
        color=wire.color,
    )


def simplify_wires_generic(
    wires: list[WireSegment],
    pin_positions: set[Point] | None = None,
    pad_to_net: dict[Point, uuid.UUID] | None = None,
    *,
    mode: SimplifyMode = SimplifyMode.BARE_PARITY,
    board_pads: set[Point] | None = None,
    wire_to_net: "dict[uuid.UUID, uuid.UUID | None] | None" = None,
    pin_net_map: "dict[Point, uuid.UUID] | None" = None,
    pad_index: object = None,
) -> list[WireSegment]:
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
