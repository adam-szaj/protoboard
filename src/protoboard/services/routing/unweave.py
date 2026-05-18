# SPDX-License-Identifier: MIT
"""Topological un-weaving of mutually-crossing wires.

When two wires cross each other twice between fixed endpoints (and the
topological permutation of those endpoints is even), the two crossings can
be removed by *swapping* the path fragments that sit between them.  The
geometry is unchanged — every vertex on the original paths remains a vertex
on some wire — only the way the segments are glued together changes.

Example::

    Before:                            After:
    W1: A ────╲                        W1: A ────╲
              ╲                                  ╲────────── B
               ╲ X                       ←        X
                ╲                        →         ╲
                 ╲                                  ╲
    W2: C ────────╲                    W2: C ─────────╲──── D
                   ╲ Y                                 Y
                    ╲                                   ╲
                     B                                   ╲ (was W1 tail)
    (2 crossings)                       (0 crossings)

The algorithm:
1. For every pair of input wires, find all strict-interior crossings.
2. Pair up neighbouring crossings along W1.
3. For each pair (X, Y) swap the fragment of W1 between X and Y with the
   corresponding fragment of W2.  Both crossings disappear.
4. Restart after each successful swap (other pairs' crossings may have
   shifted).

MVP — Phase 1: only wires with **identical** ``side`` and ``insulated``
attributes are eligible.  Mixed pairs are skipped (would require splitting
each wire into multiple ``WireSegment``s at the swap points, which is
deferred to Phase 2).
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal, cast

from protoboard.domain.layout.wire import WireSegment
from protoboard.geometry import Point
from protoboard.services.routing.astar import _segs_cross
from protoboard.services.routing.postprocess import _canonical_subpath

if TYPE_CHECKING:
    from protoboard.domain.connectivity.graph import ConnectivityGraph
    from protoboard.domain.schematic.schematic import Schematic

_EPS = 1e-7


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Crossing:
    """One strict-interior crossing between two wires."""

    point: Point          # geometric intersection point
    wire1_seg_idx: int    # index of segment in wire1 containing the crossing
    wire2_seg_idx: int    # index of segment in wire2 containing the crossing
    wire1_param: float    # 0..1 position of the crossing along wire1's segment
    wire2_param: float    # 0..1 position of the crossing along wire2's segment


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _line_intersection(
    a1: Point, a2: Point, b1: Point, b2: Point
) -> tuple[Point, float, float] | None:
    """Intersection of segments a1→a2 and b1→b2.

    Returns ``(point, t_a, t_b)`` where ``t_a`` ∈ [0, 1] is the parameter
    along a1→a2 and ``t_b`` ∈ [0, 1] along b1→b2.  Returns ``None`` for
    parallel or non-overlapping segments.  Callers should run
    :func:`_segs_cross` first; this helper assumes a real interior crossing.
    """
    dax, day = a2.x - a1.x, a2.y - a1.y
    dbx, dby = b2.x - b1.x, b2.y - b1.y
    denom = dax * dby - day * dbx
    if abs(denom) < _EPS:
        return None
    rx, ry = b1.x - a1.x, b1.y - a1.y
    t_a = (rx * dby - ry * dbx) / denom
    t_b = (rx * day - ry * dax) / denom
    px = a1.x + t_a * dax
    py = a1.y + t_a * day
    return Point(px, py), t_a, t_b


def _pt_equal(a: Point, b: Point) -> bool:
    return abs(a.x - b.x) < _EPS and abs(a.y - b.y) < _EPS


def _dist(a: Point, b: Point) -> float:
    return math.hypot(b.x - a.x, b.y - a.y)


# ---------------------------------------------------------------------------
# Crossing detection
# ---------------------------------------------------------------------------


def find_crossings(w1: WireSegment, w2: WireSegment) -> list[Crossing]:
    """Every strict-interior crossing between 2D projections of w1 and w2.

    Wires that live on different board sides do not physically short, but
    their 2D projections may still "weave" through one another — visually
    spaghetti, sub-optimal for soldering, and a candidate for unweaving.
    Side is treated as an **output** of unweave, not a hard constraint on
    input: after the topological swap, wires may end up sharing a single
    side when the resulting geometry allows it (no same-side bare conflict).
    """
    out: list[Crossing] = []
    for i in range(len(w1.path) - 1):
        a1, a2 = w1.path[i], w1.path[i + 1]
        for j in range(len(w2.path) - 1):
            b1, b2 = w2.path[j], w2.path[j + 1]
            if not _segs_cross(a1, a2, b1, b2):
                continue
            xx = _line_intersection(a1, a2, b1, b2)
            if xx is None:
                continue
            pt, t_a, t_b = xx
            out.append(
                Crossing(
                    point=pt,
                    wire1_seg_idx=i,
                    wire2_seg_idx=j,
                    wire1_param=t_a,
                    wire2_param=t_b,
                )
            )
    return out


# ---------------------------------------------------------------------------
# Path manipulation
# ---------------------------------------------------------------------------


PadSafetyFn = "Callable[[Point, str, str, bool], bool]"
# Signature: (pad, current_side, new_side, is_insulated) -> safe?
# Pad is "safe" for a side change when:
#   - pad is empty (no pin, no foreign wire), OR
#   - pad sits on a pin / wire that already belongs to the same electric net
#     as the wire being modified.
# Caller-supplied because the function needs schematic + ConnectivityGraph
# context that the pure geometry module shouldn't import.


def _swap_fragment_between(
    w1: WireSegment, w2: WireSegment, x: Crossing, y: Crossing,
    *, pad_safe: "Callable[[Point, str, str, bool], bool] | None" = None,
) -> tuple[list[WireSegment], list[WireSegment]] | None:
    """Re-glue W1/W2 by cutting the two segments that cross at X and Y.

    Returns each new logical wire as a **list of WireSegments**.  When the
    swapped middle fragment shares (side, insulated) with the outer fragments
    the result is a single WireSegment (legacy behaviour).  When they differ
    the result is three WireSegments anchored at the split pads — head/outer
    keeps the original (side, insulated), middle inherits from the other
    wire, tail/outer matches head.

    Splitting is permitted only at pads where ``pad_safe`` returns True (the
    pad is empty or already in the same electric net).  When ``pad_safe`` is
    None or rejects either split point, the function falls back to the
    legacy single-WireSegment glue, which forces the middle onto the outer's
    side — typically the same case the caller already had.
    """
    # Ensure crossing X comes before Y along W1.
    if (x.wire1_seg_idx, x.wire1_param) > (y.wire1_seg_idx, y.wire1_param):
        x, y = y, x

    # --- Carve W1 into [head, mid, tail] anchored on existing pads ---
    w1_path = list(w1.path)
    head1 = w1_path[: x.wire1_seg_idx + 1]              # ends at pad before X
    mid1 = w1_path[x.wire1_seg_idx + 1: y.wire1_seg_idx + 1]
    tail1 = w1_path[y.wire1_seg_idx + 1:]               # starts at pad after Y

    # --- Carve W2 the same way; figure out W2's traversal direction ---
    w2_x_before_y = (
        (x.wire2_seg_idx, x.wire2_param) < (y.wire2_seg_idx, y.wire2_param)
    )
    w2_path = list(w2.path)
    if w2_x_before_y:
        head2 = w2_path[: x.wire2_seg_idx + 1]
        mid2 = w2_path[x.wire2_seg_idx + 1: y.wire2_seg_idx + 1]
        tail2 = w2_path[y.wire2_seg_idx + 1:]
    else:
        head2 = w2_path[: y.wire2_seg_idx + 1]
        mid2 = list(reversed(
            w2_path[y.wire2_seg_idx + 1: x.wire2_seg_idx + 1]
        ))
        tail2 = w2_path[x.wire2_seg_idx + 1:]

    if not mid1 or not mid2:
        return None  # degenerate cut

    # Compute the canonical link vertices once.  Each link is the intermediate
    # canonical_subpath vertex (or empty when pads share a row/column).
    link_h1_m2 = _link(head1[-1], mid2[0])
    link_m2_t1 = _link(mid2[-1], tail1[0])

    if w2_x_before_y:
        mid1_for_w2 = mid1
        link_h2_m1 = _link(head2[-1], mid1_for_w2[0])
        link_m1_t2 = _link(mid1_for_w2[-1], tail2[0])
    else:
        mid1_for_w2 = list(reversed(mid1))
        link_h2_m1 = _link(head2[-1], mid1_for_w2[0])
        link_m1_t2 = _link(mid1_for_w2[-1], tail2[0])

    # Decide whether each new wire can be a single segment or must split.
    same_attrs = (w1.side == w2.side) and (w1.insulated == w2.insulated)

    new_w1_segments = _build_swapped_wire(
        outer_wire=w1,
        middle_wire=w2,
        head=head1,
        link_to_mid=link_h1_m2,
        mid=mid2,
        link_to_tail=link_m2_t1,
        tail=tail1,
        same_attrs=same_attrs,
        pad_safe=pad_safe,
    )
    if new_w1_segments is None:
        return None
    new_w2_segments = _build_swapped_wire(
        outer_wire=w2,
        middle_wire=w1,
        head=head2,
        link_to_mid=link_h2_m1,
        mid=mid1_for_w2,
        link_to_tail=link_m1_t2,
        tail=tail2,
        same_attrs=same_attrs,
        pad_safe=pad_safe,
    )
    if new_w2_segments is None:
        return None

    return new_w1_segments, new_w2_segments


def _build_swapped_wire(
    *,
    outer_wire: WireSegment,
    middle_wire: WireSegment,
    head: list[Point],
    link_to_mid: list[Point],
    mid: list[Point],
    link_to_tail: list[Point],
    tail: list[Point],
    same_attrs: bool,
    pad_safe: "Callable[[Point, str, str, bool], bool] | None",
) -> list[WireSegment] | None:
    """Assemble the new wire after a swap.

    Single WireSegment when (side, insulated) match between outer and middle,
    or when ``pad_safe`` is unavailable / rejects splitting (legacy fallback).
    Otherwise three WireSegments — outer-head, middle, outer-tail — anchored
    at the split pads ``head[-1]`` and ``tail[0]``.
    """
    if same_attrs or pad_safe is None:
        # Single segment — middle inherits outer's attributes.
        full_path = head + link_to_mid + mid + link_to_tail + tail
        return [replace(outer_wire, id=uuid.uuid4(), path=full_path)]

    split_a = head[-1]                                       # pad before X
    split_b = tail[0]                                        # pad after Y
    ok_a = pad_safe(split_a, outer_wire.side, middle_wire.side, outer_wire.insulated or middle_wire.insulated)
    ok_b = pad_safe(split_b, outer_wire.side, middle_wire.side, outer_wire.insulated or middle_wire.insulated)
    if not (ok_a and ok_b):
        # Either split pad is unsafe — fall back to single-segment glue.
        full_path = head + link_to_mid + mid + link_to_tail + tail
        return [replace(outer_wire, id=uuid.uuid4(), path=full_path)]

    # Three-segment build.
    head_path = head + link_to_mid + [mid[0]]
    mid_path = [mid[0]] + mid[1:] if len(mid) > 1 else [mid[0]]
    if mid_path[-1] != mid[-1]:
        mid_path = mid
    # Make sure middle goes from mid[0] to mid[-1] inclusive (it already does).
    tail_path = [mid[-1]] + link_to_tail + tail

    head_seg = replace(outer_wire, id=uuid.uuid4(), path=head_path)
    mid_seg = WireSegment(
        id=uuid.uuid4(),
        path=mid_path,
        insulated=middle_wire.insulated,
        side=middle_wire.side,
        color=outer_wire.color,
    )
    tail_seg = replace(outer_wire, id=uuid.uuid4(), path=tail_path)
    return [head_seg, mid_seg, tail_seg]


def _link(a: Point, b: Point) -> list[Point]:
    """Canonical (Chebyshev) link between two pads, excluding the endpoints.

    Returns the *intermediate* vertices of the canonical subpath from ``a`` to
    ``b`` — the endpoints themselves are already in the surrounding head /
    middle / tail slices and would duplicate if included.  For pure ortho or
    diagonal links the result is an empty list; for L-shaped links it
    contains the corner vertex.
    """
    if _pt_equal(a, b):
        return []
    canon = _canonical_subpath(a, b, allow_l_shape=True)
    if canon is None or len(canon) <= 2:
        return []
    # canon is [a, mid, b]; drop the endpoints.
    return canon[1:-1]


def _pick_swap_pair(crossings: list[Crossing]) -> tuple[int, int] | None:
    """Pair the two consecutive crossings along W1 that sit closest together
    (by geometric distance).  Returns indices ``(i, j)`` into *crossings*.

    "Consecutive along W1" means after sorting crossings by their ordering
    on W1 (segment index + param-t), adjacent positions in that sorted list.
    Returns ``None`` when fewer than two crossings remain.
    """
    if len(crossings) < 2:
        return None
    # Sort indices by W1 position (seg_idx, then param-t inside the seg).
    order = sorted(
        range(len(crossings)),
        key=lambda k: (crossings[k].wire1_seg_idx, crossings[k].wire1_param),
    )
    best: tuple[int, int] | None = None
    best_dist = math.inf
    for k in range(len(order) - 1):
        a, b = order[k], order[k + 1]
        d = _dist(crossings[a].point, crossings[b].point)
        if d < best_dist:
            best_dist = d
            best = (a, b) if a < b else (b, a)
    return best


# ---------------------------------------------------------------------------
# Top-level driver
# ---------------------------------------------------------------------------


def unweave_wires(
    wires: list[WireSegment],
    *,
    schematic: "Schematic | None" = None,
    graph: "ConnectivityGraph | None" = None,
) -> list[WireSegment]:
    """Reduce mutual crossings within *wires* by topological fragment swaps.

    Greedy: scan every wire pair, pick the closest pair of crossings, swap
    fragments between them, and restart.  Continues until no eligible swap
    exists.  Endpoints of each *logical wire* (chain of WireSegments linked
    at shared pads) are always preserved.

    When ``schematic`` and ``graph`` are provided, the algorithm may split a
    wire into several WireSegments at the swap pads — letting two wires on
    different ``side`` or different ``insulated`` converge by changing layer
    mid-path through a galvanic pad connection.  Without them the legacy
    single-segment glue is used (wire keeps its original outer attributes).

    Returns the list of WireSegments after all swaps and a final re-merge of
    consecutive segments that share (side, insulated).  The output may
    contain more or fewer WireSegments than the input.
    """
    # Group WireSegments into "logical wires" — chains where consecutive
    # segments share an endpoint pad.  This makes the driver work correctly
    # whether the caller passes pristine wires or output from a previous
    # unweave pass (split into multiple WireSegments).
    logical: list[list[WireSegment]] = _group_by_endpoints(wires)

    pad_safe: "Callable[[Point, str, str, bool], bool] | None" = None
    short_check: "Callable[[list[list[WireSegment]]], bool] | None" = None
    if schematic is not None and graph is not None:
        pad_safe = _make_pad_safety(wires, schematic, graph)
        # Capture the IDs of the original wires up-front; the short-checker
        # subtracts these (and only these) from the baseline so post-swap
        # WireSegments don't duplicate.  Newly-created WireSegments from
        # earlier swap iterations are also excluded — see _attempt_pair_swap.
        initial_target_ids = {w.id for w in wires}
        # Liberal checker: net invariants (a) + (b) only.  Bare-bare same-side
        # crossings (invariant c) are NOT enforced here — unweave is a pure
        # topological untangling pass that may leave physical shorts, which
        # are expected to be caught by DRC and resolved later (manually or
        # by a future side-unification pass).
        short_check = _make_short_checker(
            schematic,
            graph.board, graph.layout, graph.components, graph.instances,
            target_ids=initial_target_ids,
        )

    # Hard limit to guard against pathological inputs.
    max_iter = sum(len(w.path) for w in wires) * 4 + 16
    for _ in range(max_iter):
        progressed = False
        for i in range(len(logical)):
            for j in range(i + 1, len(logical)):
                # Pairwise crossings between any segment of logical[i] and
                # any segment of logical[j].  The current MVP swap operates
                # on single WireSegment endpoints; pick the most-tangled
                # pair of segments to drive a swap.
                swap_done = _attempt_pair_swap(
                    logical, i, j, pad_safe=pad_safe,
                    short_check=short_check,
                )
                if swap_done:
                    progressed = True
                    break
            if progressed:
                break
        if not progressed:
            break

    # Side unification is a separate concern handled by a future pass.
    # Unweave only performs topological fragment swaps; consecutive segments
    # with matching attributes are merged below, but minority-side segments
    # are left alone.

    # Re-merge consecutive segments with identical (side, insulated).
    merged: list[WireSegment] = []
    for segs in logical:
        merged.extend(_remerge(segs))
    return merged


def _rebalance_sides(
    logical: list[list[WireSegment]],
    *,
    short_check: "Callable[[list[list[WireSegment]]], bool]",
) -> None:
    """Try to consolidate each logical wire onto a single side.

    For every logical wire whose segments use more than one side, attempt
    to flip each minority segment to the majority side.  Each flip is
    validated by ``short_check`` (full ConnectivityGraph rebuild) — flips
    that would break any net are reverted.

    Greedy: keep flipping while progress is made.
    """
    changed = True
    while changed:
        changed = False
        for i, chain in enumerate(logical):
            if len(chain) < 2:
                continue
            sides = [w.side for w in chain]
            if len(set(sides)) < 2:
                continue  # already unified
            # Majority side wins.  Tie-break: the side covering more vertices.
            vertex_counts: dict[str, int] = {}
            for w in chain:
                vertex_counts[w.side] = vertex_counts.get(w.side, 0) + len(w.path)
            target_side = max(vertex_counts, key=lambda s: vertex_counts[s])
            for k, seg in enumerate(chain):
                if seg.side == target_side:
                    continue
                # Propose flipping just this segment.
                from typing import cast as _cast
                from typing import Literal as _Literal
                new_seg = replace(
                    seg, id=uuid.uuid4(),
                    side=_cast(_Literal["top", "bottom"], target_side),
                )
                proposed = [list(c) for c in logical]
                proposed[i][k] = new_seg
                if short_check(proposed):
                    logical[i][k] = new_seg
                    changed = True
                    break
            if changed:
                break


@dataclass(frozen=True)
class _LogicalPoint:
    """A vertex on a logical wire's concatenated path.

    ``side`` and ``insulated`` describe the segment **leaving** this vertex.
    For the terminal vertex these fields are read from the previous segment.
    """

    point: Point
    side: str
    insulated: bool


def _to_logical_path(segments: list[WireSegment]) -> list[_LogicalPoint]:
    """Walk an ordered list of WireSegments and produce a single concatenated
    vertex sequence.  Consecutive segments must share an endpoint pad.
    """
    if not segments:
        return []
    result: list[_LogicalPoint] = []
    for seg in segments:
        pts = seg.path
        if not result:
            for k, p in enumerate(pts):
                result.append(_LogicalPoint(p, seg.side, seg.insulated))
        else:
            # Skip the first vertex if it matches the previous segment's last
            # (they share a junction pad).
            start = 1 if _pt_equal(result[-1].point, pts[0]) else 0
            for p in pts[start:]:
                result.append(_LogicalPoint(p, seg.side, seg.insulated))
    return result


def _from_logical_path(lpath: list[_LogicalPoint]) -> list[WireSegment]:
    """Inverse of ``_to_logical_path`` — emits a fresh WireSegment whenever
    the (side, insulated) attributes change between consecutive vertices.
    """
    if len(lpath) < 2:
        return []
    segs: list[WireSegment] = []
    cur_path: list[Point] = [lpath[0].point]
    cur_side = lpath[0].side
    cur_ins = lpath[0].insulated
    for k in range(1, len(lpath)):
        if lpath[k].side != cur_side or lpath[k].insulated != cur_ins:
            # Close current segment at the split pad (last vertex), then
            # open a new one whose first vertex is the same pad.
            cur_path.append(lpath[k].point)
            segs.append(WireSegment(
                id=uuid.uuid4(),
                path=cur_path,
                insulated=cur_ins,
                side=cast(Literal["top", "bottom"], cur_side),
            ))
            cur_path = [lpath[k].point]
            cur_side = lpath[k].side
            cur_ins = lpath[k].insulated
        else:
            cur_path.append(lpath[k].point)
    if len(cur_path) >= 2:
        segs.append(WireSegment(
            id=uuid.uuid4(),
            path=cur_path,
            insulated=cur_ins,
            side=cast(Literal["top", "bottom"], cur_side),
        ))
    return segs


def _logical_path_to_points(lpath: list[_LogicalPoint]) -> list[Point]:
    return [lp.point for lp in lpath]


def _find_crossings_logical(
    lpath1: list[_LogicalPoint], lpath2: list[_LogicalPoint],
) -> list[Crossing]:
    """Crossings between two concatenated logical paths.

    Returns Crossings with seg_idx interpreted as the index *within the
    concatenated path*, so callers must translate back to the per-WireSegment
    granularity if they need it.
    """
    p1 = _logical_path_to_points(lpath1)
    p2 = _logical_path_to_points(lpath2)
    out: list[Crossing] = []
    for i in range(len(p1) - 1):
        a1, a2 = p1[i], p1[i + 1]
        for j in range(len(p2) - 1):
            b1, b2 = p2[j], p2[j + 1]
            if not _segs_cross(a1, a2, b1, b2):
                continue
            xx = _line_intersection(a1, a2, b1, b2)
            if xx is None:
                continue
            pt, t_a, t_b = xx
            out.append(Crossing(
                point=pt, wire1_seg_idx=i, wire2_seg_idx=j,
                wire1_param=t_a, wire2_param=t_b,
            ))
    return out


def _swap_logical_fragment(
    lpath1: list[_LogicalPoint], lpath2: list[_LogicalPoint],
    x: Crossing, y: Crossing,
    *, pad_safe: "Callable[[Point, str, str, bool], bool] | None",
) -> tuple[list[_LogicalPoint], list[_LogicalPoint]] | None:
    """Swap fragments between two logical paths, like
    ``_swap_fragment_between`` but on the concatenated representation.

    Each output logical path preserves the **outer** (side, insulated)
    attributes from its source, and the spliced middle keeps the
    attributes from the OTHER source.  When the middle's attributes differ
    from the outer's, the function validates ``pad_safe`` at the two split
    pads (head[-1] and tail[0]).  When pad_safe rejects, falls back to
    forcing the middle onto the outer's attributes (legacy glue).
    """
    if (x.wire1_seg_idx, x.wire1_param) > (y.wire1_seg_idx, y.wire1_param):
        x, y = y, x

    p1 = _logical_path_to_points(lpath1)
    p2 = _logical_path_to_points(lpath2)

    head1_pts = p1[: x.wire1_seg_idx + 1]
    mid1_pts = p1[x.wire1_seg_idx + 1: y.wire1_seg_idx + 1]
    tail1_pts = p1[y.wire1_seg_idx + 1:]

    w2_x_before_y = (
        (x.wire2_seg_idx, x.wire2_param) < (y.wire2_seg_idx, y.wire2_param)
    )
    if w2_x_before_y:
        head2_pts = p2[: x.wire2_seg_idx + 1]
        mid2_pts = p2[x.wire2_seg_idx + 1: y.wire2_seg_idx + 1]
        tail2_pts = p2[y.wire2_seg_idx + 1:]
        mid1_for_w2 = mid1_pts
    else:
        head2_pts = p2[: y.wire2_seg_idx + 1]
        mid2_pts = list(reversed(p2[y.wire2_seg_idx + 1: x.wire2_seg_idx + 1]))
        tail2_pts = p2[x.wire2_seg_idx + 1:]
        mid1_for_w2 = list(reversed(mid1_pts))

    if not mid1_pts or not mid2_pts:
        return None

    link_h1_m2 = _link(head1_pts[-1], mid2_pts[0])
    link_m2_t1 = _link(mid2_pts[-1], tail1_pts[0])
    link_h2_m1 = _link(head2_pts[-1], mid1_for_w2[0])
    link_m1_t2 = _link(mid1_for_w2[-1], tail2_pts[0])

    # Helper: bind the outer/middle attribute decision to lpath data.
    def _attr_for_index(lp: list[_LogicalPoint], idx: int) -> tuple[str, bool]:
        """The (side, insulated) used for segments leaving lp[idx]."""
        return (lp[idx].side, lp[idx].insulated)

    # Outer attrs (head/tail) for wire1: read from lpath1's head[-1] and tail[0].
    h1_attrs = _attr_for_index(lpath1, x.wire1_seg_idx)            # outgoing from head[-1]
    t1_attrs = _attr_for_index(lpath1, y.wire1_seg_idx + 1)        # incoming to tail[0]
    h2_attrs = (
        _attr_for_index(lpath2, x.wire2_seg_idx) if w2_x_before_y
        else _attr_for_index(lpath2, y.wire2_seg_idx)
    )
    t2_attrs = (
        _attr_for_index(lpath2, y.wire2_seg_idx + 1) if w2_x_before_y
        else _attr_for_index(lpath2, x.wire2_seg_idx + 1)
    )

    # Middle attrs (from the other wire's body).  Use the most common
    # (side, insulated) along the middle slice; for simple two-pin nets this
    # is the side at the slice start.
    if w2_x_before_y:
        m2_attrs = _attr_for_index(lpath2, x.wire2_seg_idx + 1)
    else:
        m2_attrs = _attr_for_index(lpath2, y.wire2_seg_idx + 1)
    m1_attrs = _attr_for_index(lpath1, x.wire1_seg_idx + 1)

    def _build_lpath(
        head_pts: list[Point], link_to_mid: list[Point],
        mid_pts: list[Point], link_to_tail: list[Point],
        tail_pts: list[Point],
        outer_attrs: tuple[str, bool], mid_attrs: tuple[str, bool],
        outer_attrs_tail: tuple[str, bool],
    ) -> list[_LogicalPoint] | None:
        outer_side, outer_ins = outer_attrs
        mid_side, mid_ins = mid_attrs
        same = outer_attrs == mid_attrs == outer_attrs_tail
        if same or pad_safe is None:
            # Single attribute set — middle inherits outer.
            full = head_pts + link_to_mid + mid_pts + link_to_tail + tail_pts
            return [_LogicalPoint(p, outer_side, outer_ins) for p in full]
        # Need split — check pad safety at the two split pads.
        split_a = head_pts[-1]
        split_b = tail_pts[0]
        # We only need to validate when crossing into a new (side, insulated).
        ok_a = (
            mid_attrs == outer_attrs
            or pad_safe(split_a, outer_side, mid_side, outer_ins or mid_ins)
        )
        ok_b = (
            mid_attrs == outer_attrs_tail
            or pad_safe(
                split_b, outer_attrs_tail[0], mid_side,
                outer_attrs_tail[1] or mid_ins,
            )
        )
        if not (ok_a and ok_b):
            # Fallback — force middle onto outer's attrs.
            full = head_pts + link_to_mid + mid_pts + link_to_tail + tail_pts
            return [_LogicalPoint(p, outer_side, outer_ins) for p in full]
        # Three-attribute build.
        result: list[_LogicalPoint] = []
        for p in head_pts:
            result.append(_LogicalPoint(p, outer_side, outer_ins))
        for p in link_to_mid:
            # Link sits between head's last pad and mid's first pad.  Use
            # outer attrs up to and including the split pad; mid attrs
            # after.  Since the link is intermediate vertices (canonical
            # subpath excluding endpoints), assign them to outer side to be
            # conservative — they live "between" the two segments.
            result.append(_LogicalPoint(p, outer_side, outer_ins))
        # Convert the existing tail of `result` (the split pad) to mid_attrs.
        # Pad already in `result`; the *next* link segment switches to mid.
        # We model the change by re-tagging the next vertex onward.
        for k, p in enumerate(mid_pts):
            result.append(_LogicalPoint(p, mid_side, mid_ins))
        for p in link_to_tail:
            result.append(_LogicalPoint(p, mid_side, mid_ins))
        ot_side, ot_ins = outer_attrs_tail
        for p in tail_pts:
            result.append(_LogicalPoint(p, ot_side, ot_ins))
        return result

    new_lp1 = _build_lpath(
        head1_pts, link_h1_m2, mid2_pts, link_m2_t1, tail1_pts,
        outer_attrs=h1_attrs, mid_attrs=m2_attrs,
        outer_attrs_tail=t1_attrs,
    )
    new_lp2 = _build_lpath(
        head2_pts, link_h2_m1, mid1_for_w2, link_m1_t2, tail2_pts,
        outer_attrs=h2_attrs, mid_attrs=m1_attrs,
        outer_attrs_tail=t2_attrs,
    )
    if new_lp1 is None or new_lp2 is None:
        return None
    return new_lp1, new_lp2


def _attempt_pair_swap(
    logical: list[list[WireSegment]], i: int, j: int,
    *, pad_safe: "Callable[[Point, str, str, bool], bool] | None",
    short_check: "Callable[[list[list[WireSegment]]], bool] | None" = None,
) -> bool:
    """Try one swap between logical wires *i* and *j*, operating on
    concatenated logical paths.  Returns True on success.

    ``short_check`` (optional) is invoked on a proposed `logical` after the
    swap; if it returns False, the swap is rejected (rolled back).
    """
    lp_i = _to_logical_path(logical[i])
    lp_j = _to_logical_path(logical[j])
    cs = _find_crossings_logical(lp_i, lp_j)
    pair = _pick_swap_pair(cs)
    if pair is None:
        return False
    a, b = cs[pair[0]], cs[pair[1]]
    result = _swap_logical_fragment(lp_i, lp_j, a, b, pad_safe=pad_safe)
    if result is None:
        return False
    new_lp_i, new_lp_j = result
    # Endpoints preserved?
    if (
        not _pt_equal(new_lp_i[0].point, lp_i[0].point)
        or not _pt_equal(new_lp_i[-1].point, lp_i[-1].point)
        or not _pt_equal(new_lp_j[0].point, lp_j[0].point)
        or not _pt_equal(new_lp_j[-1].point, lp_j[-1].point)
    ):
        return False
    # Convert each logical path back into a chain of WireSegments.
    new_segs_i = _from_logical_path(new_lp_i)
    new_segs_j = _from_logical_path(new_lp_j)
    if short_check is not None:
        # Propose the swap; pass the entire current `logical` state with
        # i/j swapped so the checker sees the FULL post-swap layout from
        # the unweave scope (other logical wires already swapped in earlier
        # iterations stay in place).
        proposed = list(logical)
        proposed[i] = new_segs_i
        proposed[j] = new_segs_j
        if not short_check(proposed):
            return False
    logical[i] = new_segs_i
    logical[j] = new_segs_j
    return True


def _group_by_endpoints(wires: list[WireSegment]) -> list[list[WireSegment]]:
    """Chain WireSegments into ordered logical-wire lists.

    Two segments belong to the same logical wire when one's terminal pad
    equals the other's starting pad.  Each returned chain is ordered so that
    ``chain[k].path[-1] == chain[k+1].path[0]``.  Standalone WireSegments
    become 1-element chains.
    """
    from collections import defaultdict
    # Bucket segments by their endpoints so we can quickly find neighbours.
    by_start: dict[tuple[float, float], list[int]] = defaultdict(list)
    by_end: dict[tuple[float, float], list[int]] = defaultdict(list)
    for i, w in enumerate(wires):
        if len(w.path) < 2:
            continue
        s = (w.path[0].x, w.path[0].y)
        e = (w.path[-1].x, w.path[-1].y)
        by_start[s].append(i)
        by_end[e].append(i)

    visited: set[int] = set()
    chains: list[list[WireSegment]] = []
    for i in range(len(wires)):
        if i in visited or len(wires[i].path) < 2:
            if i not in visited and len(wires[i].path) >= 2:
                visited.add(i)
            continue
        # Walk forward from i.
        forward: list[int] = [i]
        visited.add(i)
        while True:
            tail = (wires[forward[-1]].path[-1].x, wires[forward[-1]].path[-1].y)
            candidates = [k for k in by_start.get(tail, []) if k not in visited]
            if not candidates:
                break
            nxt = candidates[0]
            forward.append(nxt)
            visited.add(nxt)
        # Walk backward from i.
        backward: list[int] = []
        while True:
            head = wires[backward[-1] if backward else forward[0]]
            head_pt = (head.path[0].x, head.path[0].y)
            candidates = [k for k in by_end.get(head_pt, []) if k not in visited]
            if not candidates:
                break
            prv = candidates[0]
            backward.append(prv)
            visited.add(prv)
        chain = list(reversed(backward)) + forward
        chains.append([wires[k] for k in chain])
    # Append any remaining (untouched) wires that had fewer than 2 path points.
    for i, w in enumerate(wires):
        if i not in visited:
            chains.append([w])
    return chains


def _remerge(segments: list[WireSegment]) -> list[WireSegment]:
    """Fuse consecutive WireSegments that share (side, insulated) and an
    endpoint pad.  Preserves order and never crosses an attribute boundary.
    """
    if not segments:
        return []
    out: list[WireSegment] = [segments[0]]
    for nxt in segments[1:]:
        prev = out[-1]
        if (
            prev.side == nxt.side
            and prev.insulated == nxt.insulated
            and _pt_equal(prev.path[-1], nxt.path[0])
        ):
            fused = replace(
                prev,
                id=uuid.uuid4(),
                path=list(prev.path) + list(nxt.path[1:]),
            )
            out[-1] = fused
        else:
            out.append(nxt)
    return out


def _make_pad_safety(
    all_wires: list[WireSegment],
    schematic: "Schematic",
    graph: "ConnectivityGraph",
) -> "Callable[[Point, str, str, bool], bool]":
    """Build the pad-safety predicate used by ``_swap_fragment_between``.

    A pad is "safe" to attach to (for a side change) when joining there
    does not create a galvanic contact with a foreign electric net.
    Concretely:
      - the pad on the *new* side belongs to the same electric set as the
        pad on the *current* side (plated-through or already in this net), OR
      - the pad on the new side is not part of any other net (empty pad).
    """
    # Collect pad positions that belong to any net (via pins).
    pad_to_net_id: dict[tuple[float, float], object] = {}
    for net in schematic.nets.values():
        for ref in net.pins:
            # We cannot resolve pin positions here without instances —
            # the caller already encoded that via the graph.  Fall back to
            # the graph for membership testing.
            _ = ref
            _ = net
    # The graph encodes pad-to-electric-set membership directly.  We use it
    # to compare the canonical keys on both sides of the candidate pad.
    def _safe(pad: Point, current_side: str, new_side: str, _insulated: bool) -> bool:
        if current_side == new_side:
            return True  # no side change needed
        node_cur = graph.node_for_pad(pad, current_side)
        node_new = graph.node_for_pad(pad, new_side)
        if node_new is None:
            # Pad doesn't exist on the new side (e.g. single-side pad) →
            # cannot land there.
            return False
        if node_cur is None:
            return False
        # Same electric set on both sides (plated-through or already in
        # this wire's net) → safe.
        return node_cur == node_new
    return _safe


def _count_bare_same_side_shorts(
    wires: list[WireSegment],
    graph: "ConnectivityGraph",
) -> int:
    """Count strict-interior crossings between bare wires that share a side
    but belong to different electric sets — i.e. physical off-pad shorts.

    Same electric set (e.g. two segments of the same logical wire that
    happen to cross — possible after a swap) does not count.
    """
    from collections import defaultdict
    bare = [w for w in wires if not w.insulated]
    by_side: dict = defaultdict(list)
    for w in bare:
        by_side[w.side].append(w)
    count = 0
    for side, ws in by_side.items():
        for i in range(len(ws)):
            wi = ws[i]
            k_i = (
                graph.node_for_pad(wi.path[0], side)
                or graph.node_for_pad(wi.path[-1], side)
            )
            for j in range(i + 1, len(ws)):
                wj = ws[j]
                k_j = (
                    graph.node_for_pad(wj.path[0], side)
                    or graph.node_for_pad(wj.path[-1], side)
                )
                if k_i is not None and k_j is not None and k_i == k_j:
                    continue
                for a in range(len(wi.path) - 1):
                    for b in range(len(wj.path) - 1):
                        if _segs_cross(
                            wi.path[a], wi.path[a + 1],
                            wj.path[b], wj.path[b + 1],
                        ):
                            count += 1
    return count


def _make_short_checker(
    schematic: "Schematic",
    base_board: object,
    base_layout: object,
    base_components: object,
    base_instances: object,
    *,
    target_ids: "set[uuid.UUID] | None" = None,
    check_bare_same_side: bool = False,
) -> "Callable[[list[list[WireSegment]]], bool]":
    """Build a checker that rejects a proposed unweave step if it merges
    or fragments any schematic net.

    Each call rebuilds a ConnectivityGraph from the proposed wire layout and
    verifies that every original net's pin set still lands in exactly one
    electric set, and that no electric set hosts pins from two different
    nets.  Catches both net-fragmentation and inadvertent shorts (e.g. a
    bare wire's middle pad touching a foreign pin on the same side).

    *target_ids* identifies which wires from the baseline layout are being
    replaced by *proposed*.  All other baseline wires are kept untouched so
    nets they participate in stay connected.  When *target_ids* is None,
    the proposed list is taken as the entire wire set (legacy semantics).

    *check_bare_same_side* enables invariant (c): two bare wires on the
    same side belonging to different electric sets must not cross at an
    off-pad interior point (a physical short the ConnectivityGraph cannot
    detect).  Off by default — the main swap path treats side changes as
    cheap and pad_safe already guards split points.  `_rebalance_sides`
    enables it because flipping a segment to the majority side can drag
    it across foreign bare wires.
    """
    # Pin → net mapping snapshot — source of truth for invariants.
    pin_to_net: dict[tuple[uuid.UUID, int], uuid.UUID] = {}
    for net in schematic.nets.values():
        for ref in net.pins:
            pin_to_net[(ref.component_id, ref.pin_number)] = net.id

    # Nothing to enforce if the schematic has no nets — skip the expensive
    # ConnectivityGraph rebuild entirely.  Useful for fixture-free unit tests.
    if not pin_to_net:
        def _trivially_ok(_proposed: list[list[WireSegment]]) -> bool:
            return True
        return _trivially_ok

    # Baseline count of bare-bare same-side crossings in the original layout.
    # Invariant (c) is enforced *monotonically*: a proposed swap may leave
    # the count unchanged (or reduce it) but must not *introduce* new shorts.
    # Without this baseline, the checker also rejects swaps that don't touch
    # an existing same-side bare crossing — every recompute would re-find it
    # and short-circuit the check.
    baseline_same_side_shorts = 0
    if check_bare_same_side:
        from protoboard.domain.connectivity.graph import ConnectivityGraph
        try:
            _baseline_graph = ConnectivityGraph(
                board=base_board,  # type: ignore[arg-type]
                layout=base_layout,  # type: ignore[arg-type]
                components=base_components,  # type: ignore[arg-type]
                instances=base_instances,  # type: ignore[arg-type]
            )
            baseline_same_side_shorts = _count_bare_same_side_shorts(
                base_layout.wires, _baseline_graph,  # type: ignore[attr-defined]
            )
        except Exception:
            baseline_same_side_shorts = 0

    def _check(proposed: list[list[WireSegment]]) -> bool:
        from protoboard.domain.connectivity.graph import ConnectivityGraph
        from protoboard.domain.layout.layout import Layout
        from protoboard.domain.schematic.net import PinRef
        from collections import defaultdict

        new_layout = Layout()
        new_layout.placements = base_layout.placements  # type: ignore[attr-defined]
        new_layout.groups = base_layout.groups  # type: ignore[attr-defined]
        # Baseline = every wire from the original layout that is NOT being
        # replaced by `proposed`.  This keeps unrelated nets connected so
        # the checker doesn't fail on wires we never touched.
        new_wires: list[WireSegment] = []
        if target_ids is not None:
            for w in base_layout.wires:  # type: ignore[attr-defined]
                if w.id not in target_ids:
                    new_wires.append(w)
        for chain in proposed:
            new_wires.extend(chain)
        new_layout.wires = new_wires
        try:
            new_graph = ConnectivityGraph(
                board=base_board,  # type: ignore[arg-type]
                layout=new_layout,
                components=base_components,  # type: ignore[arg-type]
                instances=base_instances,  # type: ignore[arg-type]
            )
        except Exception:
            return False

        for net in schematic.nets.values():
            keys = {new_graph.node_for_pin(ref) for ref in net.pins}
            keys.discard(None)
            if len(keys) > 1:
                return False  # fragmented

        key_to_nets: dict = defaultdict(set)
        for (cid, pn), nid in pin_to_net.items():
            k = new_graph.node_for_pin(PinRef(cid, pn))
            if k is not None:
                key_to_nets[k].add(nid)
        for nids in key_to_nets.values():
            if len(nids) > 1:
                return False  # short

        # Invariant (c): bare wires on the same side that belong to
        # different electric sets must not cross at an off-pad interior
        # point.  Enforced monotonically — the proposed layout may not
        # exceed the baseline count.  This lets unweave reduce crossings
        # without introducing *new* shorts, even when the baseline already
        # has some unavoidable ones.
        if check_bare_same_side:
            new_count = _count_bare_same_side_shorts(
                new_layout.wires, new_graph,
            )
            if new_count > baseline_same_side_shorts:
                return False

        return True

    return _check


def count_mutual_crossings(wires: list[WireSegment]) -> int:
    """Total crossings between every pair of wires in *wires*."""
    total = 0
    for i in range(len(wires)):
        for j in range(i + 1, len(wires)):
            total += len(find_crossings(wires[i], wires[j]))
    return total
