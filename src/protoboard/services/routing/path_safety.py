# SPDX-License-Identifier: MIT
"""Physical and electrical contact checks for candidate wire paths."""

from __future__ import annotations

import math
import os
import uuid
from typing import TYPE_CHECKING

import structlog

from protoboard.domain.layout.wire import Wire
from protoboard.geometry import (
    Point,
    point_on_segment_interior,
    rounded_point,
    segments_cross_strict,
)

if TYPE_CHECKING:
    from protoboard.domain.board.pad_state import Pad

# Keep the existing logger namespace for simplify diagnostics.
_log = structlog.get_logger("protoboard.services.routing.postprocess")
# Opt-in diagnostic: when SIMPLIFY_TRACE=1 every rejected (i, j) candidate
# inside ``generic_simplify_wire`` emits a structured log line with the
# specific rejection reason and the relevant geometry.  Off by default
# because dense boards generate hundreds of lines per wire.  Used to
# diagnose "uparty wire" reports where simplify is expected to shorten
# a path but produces no change.
_SIMPLIFY_TRACE = os.environ.get("SIMPLIFY_TRACE", "").lower() in ("1", "true", "yes")
_EPS = 1e-9


def _wire_to_segs(wire: Wire) -> list[tuple[Point, Point]]:
    return list(zip(wire.path, wire.path[1:]))


def _segs_cross_or_touch(
    a1: Point,
    a2: Point,
    b1: Point,
    b2: Point,
) -> bool:
    """Like strict crossing but also reports T-junctions and vertex-on-edge.

    Returns True if segments a1→a2 and b1→b2 share any point other than an
    endpoint-on-endpoint coincidence.  In particular: if a vertex of one
    segment falls strictly inside the other, that's a real electrical
    contact and counts as a crossing here.
    """
    if segments_cross_strict(a1, a2, b1, b2):
        return True
    # T-junction or vertex-on-interior cases.
    if point_on_segment_interior(a1, b1, b2, eps=_EPS):
        return True
    if point_on_segment_interior(a2, b1, b2, eps=_EPS):
        return True
    if point_on_segment_interior(b1, a1, a2, eps=_EPS):
        return True
    if point_on_segment_interior(b2, a1, a2, eps=_EPS):
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
    another net's wire is a long galvanic short — strict crossing ignores
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
                if point_on_segment_interior(ep, c_src, c_dst, eps=_EPS):
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
            if point_on_segment_interior(Point(ax, ay), c_src, c_dst, eps=_EPS):
                counted.add((ax, ay))
    return len(counted)


def candidate_safe_against_others(
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
        # pin/joint/via gate (handled elsewhere — `candidate_no_new_pin_contact`).
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


def candidate_pins_alien(
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
    own_pts = {rounded_point(p) for p in original_path}
    for pad, net_id in pin_net_map.items():
        if wire_net_id is not None and net_id == wire_net_id:
            continue  # same net — OK
        key = rounded_point(pad)
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


def candidate_safe_against_own(
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
            if segments_cross_strict(c_src, c_dst, o_src, o_dst):
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


def new_alien_pad_contact(
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


def parity_ok_for_bare(
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


def candidate_no_new_pin_contact(
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
    here — those are handled by ``candidate_safe_against_others`` via the
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
    orig_keys = {rounded_point(p) for p in original_path}
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
            key = rounded_point(v)
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
                key = rounded_point(pad.position)
                if key in seen:
                    continue
                seen.add(key)
                out.append(pad)
        return out

    # Hard-block check first: any NEW hard contact on the candidate aborts.
    for pad in _iter_pads_along(candidate_vertices):
        key = rounded_point(pad.position)
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


def candidate_safe_via_pad_index(
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
    ``candidate_no_new_pin_contact`` in sum-based fashion).
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
