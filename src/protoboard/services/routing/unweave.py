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
each wire into multiple ``Wire``s at the swap points, which is
deferred to Phase 2).
"""

from __future__ import annotations

import math
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal, cast

import structlog

from protoboard.domain.components.resolution import iter_placed_components
from protoboard.domain.layout.placement import iter_footprint_pin_positions
from protoboard.domain.layout.wire import Wire, WireCollection
from protoboard.geometry import Point, Vector, segments_cross_strict
from protoboard.services.routing.postprocess import _canonical_subpath
from protoboard.types import NodeKey, Side

_log = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from protoboard.domain.board.board import Board
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.connectivity.graph import ConnectivityGraph
    from protoboard.domain.layout.layout import Layout
    from protoboard.domain.schematic.schematic import Schematic

_EPS = 1e-7

_PointFingerprint = tuple[float, float]
_WireFingerprint = tuple[_PointFingerprint, ...]
_ChainFingerprint = tuple[_WireFingerprint, ...]
_StateFingerprint = tuple[_ChainFingerprint, ...]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Crossing:
    """One strict-interior crossing between two wires."""

    point: Point  # geometric intersection point
    wire1_seg_idx: int  # index of segment in wire1 containing the crossing
    wire2_seg_idx: int  # index of segment in wire2 containing the crossing
    wire1_param: float  # 0..1 position of the crossing along wire1's segment
    wire2_param: float  # 0..1 position of the crossing along wire2's segment


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
    strict segment crossing first; this helper assumes a real interior crossing.
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
    return (b - a).norm()


# ---------------------------------------------------------------------------
# Crossing detection
# ---------------------------------------------------------------------------


def find_crossings(w1: Wire, w2: Wire) -> list[Crossing]:
    """Every crossing between 2D projections of w1 and w2.

    Returns both:
    * **Strict-interior crossings** — two segments physically intersect in
      their interiors (the strict segment-crossing case).
    * **Shared-pad crossings** — both wires have an *interior* vertex at
      the same pad position AND the four direction vectors around that pad
      are topologically interleaved (W1's in/out directions sit between
      W2's, in the angular sort).  Visually the two wires weave through
      one another at the pad.

    Endpoint-shared pads (one or both wires terminate at the shared pad)
    are NOT crossings — they're legitimate same-pin contacts.

    Wires that live on different board sides do not physically short, but
    their 2D projections may still "weave" through one another — visually
    spaghetti, sub-optimal for soldering, and a candidate for unweaving.
    """
    out: list[Crossing] = []
    # Pass 1 — strict-interior segment crossings (legacy).
    for i in range(len(w1.path) - 1):
        a1, a2 = w1.path[i], w1.path[i + 1]
        for j in range(len(w2.path) - 1):
            b1, b2 = w2.path[j], w2.path[j + 1]
            if not segments_cross_strict(a1, a2, b1, b2):
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
    # Pass 2 — shared-pad crossings.  Both wires have an interior vertex at
    # the same pad position, with topologically interleaved directions.
    out.extend(_find_shared_pad_crossings(w1, w2))
    return out


def _find_shared_pad_crossings(
    w1: Wire,
    w2: Wire,
) -> list[Crossing]:
    """Crossings where both wires have an interior vertex at the same pad
    AND their directions around that pad interleave (true topological
    weave, not just a touch-and-leave-the-same-way meeting).

    Yields ``Crossing`` records where ``wireN_seg_idx`` is the index of the
    *incoming* segment (the one ending at the shared vertex) and
    ``wireN_param = 1.0`` to mark "shared at end of segment".  The
    downstream swap code (``_swap_logical_fragment``) treats those as the
    boundary between head and mid/tail slices.
    """
    out: list[Crossing] = []
    p1 = w1.path
    p2 = w2.path
    # Index W2's interior vertices by position for O(1) lookup.
    interior_w2: dict[tuple[float, float], int] = {}
    for j in range(1, len(p2) - 1):
        key = (round(p2[j].x, 6), round(p2[j].y, 6))
        interior_w2.setdefault(key, j)
    for i in range(1, len(p1) - 1):
        v = p1[i]
        key = (round(v.x, 6), round(v.y, 6))
        matched_j = interior_w2.get(key)
        if matched_j is None:
            continue
        # Four direction vectors leaving the shared vertex.
        d1_prev = (p1[i - 1].x - v.x, p1[i - 1].y - v.y)
        d1_next = (p1[i + 1].x - v.x, p1[i + 1].y - v.y)
        d2_prev = (p2[matched_j - 1].x - v.x, p2[matched_j - 1].y - v.y)
        d2_next = (p2[matched_j + 1].x - v.x, p2[matched_j + 1].y - v.y)
        if not _directions_interleave(d1_prev, d1_next, d2_prev, d2_next):
            continue
        out.append(
            Crossing(
                point=v,
                wire1_seg_idx=i - 1,
                wire2_seg_idx=matched_j - 1,
                wire1_param=1.0,
                wire2_param=1.0,
            )
        )
    return out


def _directions_interleave(
    d1a: tuple[float, float],
    d1b: tuple[float, float],
    d2a: tuple[float, float],
    d2b: tuple[float, float],
) -> bool:
    """True if the four direction vectors at a shared pad interleave.

    Sort the four directions by angle.  If the angular sort is
    ``[W1, W2, W1, W2]`` (or rotated), the wires weave through one
    another.  Otherwise (``[W1, W1, W2, W2]`` etc.) they just touch and
    return.
    """
    import math as _math

    items = [
        ("1", _math.atan2(d1a[1], d1a[0])),
        ("1", _math.atan2(d1b[1], d1b[0])),
        ("2", _math.atan2(d2a[1], d2a[0])),
        ("2", _math.atan2(d2b[1], d2b[0])),
    ]
    items.sort(key=lambda t: t[1])
    labels = [t[0] for t in items]
    # Interleaved ↔ adjacent labels differ across the full cycle.
    return labels in (["1", "2", "1", "2"], ["2", "1", "2", "1"])


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
    w1: Wire,
    w2: Wire,
    x: Crossing,
    y: Crossing,
    *,
    pad_safe: Callable[[Point, str, str, bool], bool] | None = None,
) -> tuple[list[Wire], list[Wire]] | None:
    """Re-glue W1/W2 by cutting the two segments that cross at X and Y.

    Returns each new logical wire as a **list of WireSegments**.  When the
    swapped middle fragment shares (side, insulated) with the outer fragments
    the result is a single Wire (legacy behaviour).  When they differ
    the result is three WireSegments anchored at the split pads — head/outer
    keeps the original (side, insulated), middle inherits from the other
    wire, tail/outer matches head.

    Splitting is permitted only at pads where ``pad_safe`` returns True (the
    pad is empty or already in the same electric net).  When ``pad_safe`` is
    None or rejects either split point, the function falls back to the
    legacy single-Wire glue, which forces the middle onto the outer's
    side — typically the same case the caller already had.
    """
    # Ensure crossing X comes before Y along W1.
    if (x.wire1_seg_idx, x.wire1_param) > (y.wire1_seg_idx, y.wire1_param):
        x, y = y, x

    # --- Carve W1 into [head, mid, tail] anchored on existing pads ---
    w1_path = list(w1.path)
    head1 = w1_path[: x.wire1_seg_idx + 1]  # ends at pad before X
    mid1 = w1_path[x.wire1_seg_idx + 1 : y.wire1_seg_idx + 1]
    tail1 = w1_path[y.wire1_seg_idx + 1 :]  # starts at pad after Y

    # --- Carve W2 the same way; figure out W2's traversal direction ---
    w2_x_before_y = (x.wire2_seg_idx, x.wire2_param) < (y.wire2_seg_idx, y.wire2_param)
    w2_path = list(w2.path)
    if w2_x_before_y:
        head2 = w2_path[: x.wire2_seg_idx + 1]
        mid2 = w2_path[x.wire2_seg_idx + 1 : y.wire2_seg_idx + 1]
        tail2 = w2_path[y.wire2_seg_idx + 1 :]
    else:
        head2 = w2_path[: y.wire2_seg_idx + 1]
        mid2 = list(reversed(w2_path[y.wire2_seg_idx + 1 : x.wire2_seg_idx + 1]))
        tail2 = w2_path[x.wire2_seg_idx + 1 :]

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
    outer_wire: Wire,
    middle_wire: Wire,
    head: list[Point],
    link_to_mid: list[Point],
    mid: list[Point],
    link_to_tail: list[Point],
    tail: list[Point],
    same_attrs: bool,
    pad_safe: Callable[[Point, str, str, bool], bool] | None,
) -> list[Wire] | None:
    """Assemble the new wire after a swap.

    Single Wire when (side, insulated) match between outer and middle,
    or when ``pad_safe`` is unavailable / rejects splitting (legacy fallback).
    Otherwise three WireSegments — outer-head, middle, outer-tail — anchored
    at the split pads ``head[-1]`` and ``tail[0]``.
    """
    if same_attrs or pad_safe is None:
        # Single segment — middle inherits outer's attributes.  Preserve
        # outer_wire's id (this is an extension, not a creation).
        full_path = head + link_to_mid + mid + link_to_tail + tail
        return [replace(outer_wire, path=full_path)]

    split_a = head[-1]  # pad before X
    split_b = tail[0]  # pad after Y
    ok_a = pad_safe(
        split_a,
        outer_wire.side,
        middle_wire.side,
        outer_wire.insulated or middle_wire.insulated,
    )
    ok_b = pad_safe(
        split_b,
        outer_wire.side,
        middle_wire.side,
        outer_wire.insulated or middle_wire.insulated,
    )
    if not (ok_a and ok_b):
        # Either split pad is unsafe — fall back to single-segment glue.
        full_path = head + link_to_mid + mid + link_to_tail + tail
        return [replace(outer_wire, path=full_path)]

    # Three-segment build.  Head keeps outer_wire.id (continuation of the
    # outer segment), middle keeps middle_wire.id (continuation of the
    # absorbed middle), tail is a freshly-split portion of outer with a
    # new id (it is genuinely a new logical sub-wire of the swap).
    head_path = head + link_to_mid + [mid[0]]
    mid_path = [mid[0]] + mid[1:] if len(mid) > 1 else [mid[0]]
    if mid_path[-1] != mid[-1]:
        mid_path = mid
    tail_path = [mid[-1]] + link_to_tail + tail

    head_seg = replace(outer_wire, path=head_path)
    mid_seg = Wire(
        id=middle_wire.id,
        path=mid_path,
        insulated=middle_wire.insulated,
        side=middle_wire.side,
        color=outer_wire.color,
    )
    tail_seg = replace(outer_wire, id=uuid.uuid4(), path=tail_path)
    return [head_seg, mid_seg, tail_seg]


def _link(
    a: Point,
    b: Point,
    *,
    board_pads: set[Point] | None = None,
    insulated: bool = False,
) -> list[Point]:
    """Canonical (Chebyshev) link between two pads, excluding the endpoints.

    Returns the *intermediate* vertices of the canonical subpath from ``a`` to
    ``b`` — the endpoints themselves are already in the surrounding head /
    middle / tail slices and would duplicate if included.  For pure ortho or
    diagonal links the result is an empty list; for L-shaped links it
    contains the corner vertex.

    When the link belongs to a bare wire (``insulated=False``) and
    ``board_pads`` is supplied, any pad position that lies on a segment of
    the canonical link is inserted as an intermediate vertex, so connectivity
    and wire-edit see every physical pad contact.
    """
    if _pt_equal(a, b):
        return []
    pads_arg = board_pads if (board_pads and not insulated) else None
    canon = _canonical_subpath(a, b, allow_l_shape=True, board_pads=pads_arg)
    if canon is None or len(canon) <= 2:
        return []
    # canon is [a, ..., b]; drop the endpoints.
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
    wires: list[Wire],
    *,
    schematic: Schematic | None = None,
    graph: ConnectivityGraph | None = None,
    board_pads: set[Point] | None = None,
    allow_layer_change: bool = True,
    allow_side_flip: bool = False,
) -> list[Wire]:
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

    ``allow_layer_change`` (default True) gates the side-changing branch:
    when False, every candidate swap that would introduce a fragment on a
    different board side is rejected via the pad-safety predicate and the
    algorithm falls back to single-side glue.  Wires never grow new side
    transitions and never spawn mid-path vias.  Routing-settings callers
    pass ``rs.allow_layer_change`` here.

    Returns the list of WireSegments after all swaps and a final re-merge of
    consecutive segments that share (side, insulated).  The output may
    contain more or fewer WireSegments than the input.
    """
    # Group WireSegments into "logical wires" — chains where consecutive
    # segments share an endpoint pad.  This makes the driver work correctly
    # whether the caller passes pristine wires or output from a previous
    # unweave pass (split into multiple WireSegments).
    logical: list[list[Wire]] = _group_by_endpoints(wires)

    # Default board pads from graph.board so callers passing only graph still
    # benefit from pad-preservation on bare links.
    if board_pads is None and graph is not None and graph.board is not None:
        from protoboard.domain.layout.wire_utils import board_pads_for_side

        board_pads = board_pads_for_side(graph.board, "top") | board_pads_for_side(
            graph.board, "bottom"
        )

    pad_safe: Callable[[Point, str, str, bool], bool] | None = None
    short_check: Callable[[list[list[Wire]]], bool] | None = None
    if schematic is not None and graph is not None:
        pad_safe = _make_pad_safety(
            wires,
            schematic,
            graph,
            allow_layer_change=allow_layer_change,
        )
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
            graph.board,
            graph.layout,
            graph.components,
            graph.instances,
            target_ids=initial_target_ids,
        )

    # Hard limit to guard against pathological inputs.
    max_iter = sum(len(w.path) for w in wires) * 4 + 16
    _log.info(
        "unweave.algo_start",
        n_wires=len(wires),
        n_logical=len(logical),
        max_iter=max_iter,
        has_pad_safe=pad_safe is not None,
        has_short_check=short_check is not None,
        has_board_pads=board_pads is not None,
        n_board_pads=len(board_pads) if board_pads is not None else 0,
    )

    # Forbidden pads = pin positions of all components.  Used by both the
    # pre-swap overlap-separation pass and the post-swap shared-pad cleanup
    # pass so that reroute candidates stay on free pads (no alien pins).
    forbidden_pads: set[Point] | None = None
    if graph is not None:
        from protoboard.domain.layout.pad_conflict import footprint_of

        forbidden_pads = set()
        for component in iter_placed_components(
            graph.layout.placements,
            graph.instances,
            graph.components,
        ):
            comp_fp = footprint_of(component.definition, component.instance)
            forbidden_pads.update(
                position
                for _pin_number, position in iter_footprint_pin_positions(
                    comp_fp, component.placement
                )
            )

    # Foreign-wire vertices = every (pad, side) used by a wire on the board
    # that is NOT one of our unweave targets.  Used by Phase 0 to reject
    # shift candidates that would land on (or cross through) another wire's
    # pad — important when the user unweaves a subset of wires but other
    # wires physically occupy the free corridor we'd otherwise use.
    #
    # Side-aware: a top-side foreign wire does NOT block a shift of a
    # bottom-side wire (they live on different layers).  Without the side
    # filter unweave of two cross-side targets was incorrectly blocked
    # whenever any unrelated wire happened to share a pad position on the
    # other layer — reproducing reference_big_project case 2026-06-09.
    foreign_wire_pads: set[tuple[Point, str]] | None = None
    if graph is not None and graph.layout is not None:
        target_ids = {w.id for w in wires}
        foreign_wire_pads = set()
        for wire in graph.layout.wires:
            if wire.id in target_ids:
                continue
            for p in wire.path:
                foreign_wire_pads.add((p, wire.side))

    # Phase 0 — separate overlap runs.  When two logical wires share a
    # contiguous run of K≥2 interior pads (often produced by greedy A*
    # routing both nets along the same corridor), try to shift one wire off
    # the overlap onto a parallel free row/column.  Runs BEFORE the main
    # swap loop because: (a) eliminating overlap runs often turns
    # complex multi-shared-pad tangles into single-point crossings the
    # canonical swap can trivially resolve; (b) the swap operates on
    # crossings, but parallel overlaps produce ZERO crossings (parallel
    # directions don't interleave) so the swap loop alone never sees them.
    _separate_overlap_runs_pass(
        logical,
        pad_safe=pad_safe,
        short_check=short_check,
        board_pads=board_pads,
        forbidden_pads=forbidden_pads,
        foreign_wire_pads=foreign_wire_pads,
    )

    def _state_fingerprint(state: list[list[Wire]]) -> _StateFingerprint:
        """Hashable snapshot of every wire path in *state*.

        Used by the livelock guard below.  Two states with identical
        wire paths produce the same fingerprint regardless of which
        pair drove the most recent swap, so the global view catches
        cycles even when no single pair shows a self-recurring loop.
        """
        return tuple(
            tuple(
                tuple((round(p.x, 6), round(p.y, 6)) for p in seg.path) for seg in chain
            )
            for chain in state
        )

    # Livelock guard: real-world swap chains have produced sequences
    # where pair (i, j) gets repeatedly flipped between two equivalent
    # geometries because each side of the swap satisfies the local
    # invariants but neither reduces the global crossing count
    # monotonically (see reference_big_project user report 2026-05-30
    # — 178 identical-fingerprint swaps for pair (0, 1)).  Without a
    # cycle detector the main thread spins until ``max_iter`` runs out
    # or the user kills the process.  Track every post-swap state
    # fingerprint; the second time we see one, we are in a cycle.
    seen_fingerprints: set[_StateFingerprint] = set()
    seen_fingerprints.add(_state_fingerprint(logical))
    swap_count = 0
    livelock = False
    for it_idx in range(max_iter):
        progressed = False
        for i in range(len(logical)):
            for j in range(i + 1, len(logical)):
                # Pairwise crossings between any segment of logical[i] and
                # any segment of logical[j].  The current MVP swap operates
                # on single Wire endpoints; pick the most-tangled
                # pair of segments to drive a swap.
                swap_done = _attempt_pair_swap(
                    logical,
                    i,
                    j,
                    pad_safe=pad_safe,
                    short_check=short_check,
                    board_pads=board_pads,
                )
                if swap_done:
                    swap_count += 1
                    fp = _state_fingerprint(logical)
                    if fp in seen_fingerprints:
                        _log.warning(
                            "unweave.livelock_detected",
                            iter=it_idx,
                            pair=(i, j),
                            total_swaps=swap_count,
                            n_seen_states=len(seen_fingerprints),
                        )
                        # Abort the whole pass — keep the swaps we
                        # already committed (they may have reduced some
                        # crossings before the cycle started) but stop
                        # the runaway loop.
                        livelock = True
                        break
                    seen_fingerprints.add(fp)
                    _log.info(
                        "unweave.swap_done",
                        iter=it_idx,
                        pair=(i, j),
                        total_swaps=swap_count,
                    )
                    progressed = True
                    break
            if progressed or livelock:
                break
        if livelock or not progressed:
            _log.info(
                "unweave.swap_phase_done",
                iterations=it_idx,
                total_swaps=swap_count,
                reason="livelock" if livelock else "no_progress",
            )
            break

    # Side unification is a separate concern handled by a future pass.
    # Unweave only performs topological fragment swaps; consecutive segments
    # with matching attributes are merged below, but minority-side segments
    # are left alone.

    # Cleanup pass: trivial 1-step reroute for shared-pad crossings the swap
    # could not eliminate (typically because both swap pads are shared and
    # the swap leaves them shared on both wires).  No-op when the pair has
    # nowhere to move S to.  ``forbidden_pads`` already computed above.
    _cleanup_shared_pads_pass(
        logical,
        pad_safe=pad_safe,
        short_check=short_check,
        board_pads=board_pads,
        forbidden_pads=forbidden_pads,
    )

    # Visual-unweave: opt-in side-flip pass.  Tackles cross-side
    # tangling that the per-side topological machinery cannot reduce —
    # see _side_flip_pass for the acceptance gate.  Off by default; user
    # turns it on via RoutingSettings.visual_unweave_side_flip.
    if allow_side_flip:
        _side_flip_pass(
            logical,
            pad_safe=pad_safe,
            short_check=short_check,
        )

    # Re-merge consecutive segments with identical (side, insulated).
    merged: list[Wire] = []
    for segs in logical:
        merged.extend(_remerge(segs))
    return merged


def _rebalance_sides(
    logical: list[list[Wire]],
    *,
    short_check: Callable[[list[list[Wire]]], bool],
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
                # Propose flipping just this segment.  Preserve seg.id —
                # this is an attribute edit, not a creation.
                from typing import cast as _cast

                new_seg = replace(
                    seg,
                    side=_cast(Side, target_side),
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


def _to_logical_path(segments: list[Wire]) -> list[_LogicalPoint]:
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


def _from_logical_path(
    lpath: list[_LogicalPoint],
    seed_ids: list[uuid.UUID] | None = None,
) -> list[Wire]:
    """Inverse of ``_to_logical_path`` — emits a fresh Wire whenever
    the (side, insulated) attributes change between consecutive vertices.

    ``seed_ids`` preserves wire identity across the unweave swap: the first
    N output segments reuse those ids, where N = min(emitted, len(seed_ids)).
    Without this hint each unweave call would mint fresh UUIDs, breaking
    highlight / recording / undo for the unchanged segments (the user-
    reported "3 wires → 7 wires" symptom).
    """
    if len(lpath) < 2:
        return []
    segs: list[Wire] = []
    cur_path: list[Point] = [lpath[0].point]
    cur_side = lpath[0].side
    cur_ins = lpath[0].insulated

    def _next_id() -> uuid.UUID:
        if seed_ids and len(segs) < len(seed_ids):
            return seed_ids[len(segs)]
        return uuid.uuid4()

    for k in range(1, len(lpath)):
        if lpath[k].side != cur_side or lpath[k].insulated != cur_ins:
            # Close current segment at the split pad (last vertex), then
            # open a new one whose first vertex is the same pad.
            cur_path.append(lpath[k].point)
            segs.append(
                Wire(
                    id=_next_id(),
                    path=cur_path,
                    insulated=cur_ins,
                    side=cast(Side, cur_side),
                )
            )
            cur_path = [lpath[k].point]
            cur_side = lpath[k].side
            cur_ins = lpath[k].insulated
        else:
            cur_path.append(lpath[k].point)
    if len(cur_path) >= 2:
        segs.append(
            Wire(
                id=_next_id(),
                path=cur_path,
                insulated=cur_ins,
                side=cast(Side, cur_side),
            )
        )
    return segs


def _logical_path_to_points(lpath: list[_LogicalPoint]) -> list[Point]:
    return [lp.point for lp in lpath]


def _find_crossings_logical(
    lpath1: list[_LogicalPoint],
    lpath2: list[_LogicalPoint],
) -> list[Crossing]:
    """Crossings between two concatenated logical paths.

    Returns Crossings with seg_idx interpreted as the index *within the
    concatenated path*, so callers must translate back to the per-Wire
    granularity if they need it.  Includes both strict-interior segment
    crossings and shared-pad crossings (interleaved directions at a shared
    interior vertex).
    """
    p1 = _logical_path_to_points(lpath1)
    p2 = _logical_path_to_points(lpath2)
    out: list[Crossing] = []
    # Pass 1 — strict-interior segment crossings.
    for i in range(len(p1) - 1):
        a1, a2 = p1[i], p1[i + 1]
        for j in range(len(p2) - 1):
            b1, b2 = p2[j], p2[j + 1]
            if not segments_cross_strict(a1, a2, b1, b2):
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
    # Pass 2 — shared-pad crossings (interior vertex shared with topologically
    # interleaved directions).  Build a synthetic Wire so we can reuse
    # the existing shared-pad detector.
    fake_w1 = Wire(
        id=uuid.uuid4(),
        path=p1,
        insulated=False,
        side="top",  # side/insulated irrelevant for geometry
    )
    fake_w2 = Wire(
        id=uuid.uuid4(),
        path=p2,
        insulated=False,
        side="top",
    )
    out.extend(_find_shared_pad_crossings(fake_w1, fake_w2))
    return out


def _swap_logical_fragment(
    lpath1: list[_LogicalPoint],
    lpath2: list[_LogicalPoint],
    x: Crossing,
    y: Crossing,
    *,
    pad_safe: Callable[[Point, str, str, bool], bool] | None,
    board_pads: set[Point] | None = None,
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

    # Shared-pad crossings (param == 1.0) put the crossing AT a path vertex
    # (the boundary between seg_idx and seg_idx+1).  Treat such a crossing
    # as splitting the path INTO that vertex — head includes it, mid begins
    # WITH it (small duplication is fine, link logic returns empty for
    # zero-distance bridges).
    def _slice_boundaries(
        seg_idx_x: int,
        param_x: float,
        seg_idx_y: int,
        param_y: float,
    ) -> tuple[int, int, int, int]:
        """Return (head_end, mid_start, mid_end, tail_start) indices.

        ``head_end`` is *exclusive*: head = path[: head_end].
        ``mid_start`` is *inclusive*: mid = path[mid_start: mid_end].
        ``tail_start`` is *inclusive*: tail = path[tail_start:].
        """
        # X boundary.
        if abs(param_x - 1.0) < 1e-9:
            head_end = seg_idx_x + 2  # include the shared vertex
            mid_start = seg_idx_x + 1  # shared vertex is also mid's first
        else:
            head_end = seg_idx_x + 1
            mid_start = seg_idx_x + 1
        # Y boundary.
        if abs(param_y - 1.0) < 1e-9:
            mid_end = seg_idx_y + 2  # include the shared vertex
            tail_start = seg_idx_y + 1  # shared vertex is also tail's first
        else:
            mid_end = seg_idx_y + 1
            tail_start = seg_idx_y + 1
        return head_end, mid_start, mid_end, tail_start

    h1_end, m1_start, m1_end, t1_start = _slice_boundaries(
        x.wire1_seg_idx,
        x.wire1_param,
        y.wire1_seg_idx,
        y.wire1_param,
    )
    head1_pts = p1[:h1_end]
    mid1_pts = p1[m1_start:m1_end]
    tail1_pts = p1[t1_start:]

    w2_x_before_y = (x.wire2_seg_idx, x.wire2_param) < (y.wire2_seg_idx, y.wire2_param)
    if w2_x_before_y:
        h2_end, m2_start, m2_end, t2_start = _slice_boundaries(
            x.wire2_seg_idx,
            x.wire2_param,
            y.wire2_seg_idx,
            y.wire2_param,
        )
        head2_pts = p2[:h2_end]
        mid2_pts = p2[m2_start:m2_end]
        tail2_pts = p2[t2_start:]
        mid1_for_w2 = mid1_pts
    else:
        h2_end, m2_start, m2_end, t2_start = _slice_boundaries(
            y.wire2_seg_idx,
            y.wire2_param,
            x.wire2_seg_idx,
            x.wire2_param,
        )
        head2_pts = p2[:h2_end]
        mid2_pts = list(reversed(p2[m2_start:m2_end]))
        tail2_pts = p2[t2_start:]
        mid1_for_w2 = list(reversed(mid1_pts))

    if not mid1_pts or not mid2_pts:
        return None

    # Helper: bind the outer/middle attribute decision to lpath data.
    def _attr_for_index(lp: list[_LogicalPoint], idx: int) -> tuple[str, bool]:
        """The (side, insulated) used for segments leaving lp[idx]."""
        return (lp[idx].side, lp[idx].insulated)

    # Outer attrs (head/tail) for wire1: read from lpath1's head[-1] and tail[0].
    h1_attrs = _attr_for_index(lpath1, x.wire1_seg_idx)  # outgoing from head[-1]
    t1_attrs = _attr_for_index(lpath1, y.wire1_seg_idx + 1)  # incoming to tail[0]
    h2_attrs = (
        _attr_for_index(lpath2, x.wire2_seg_idx)
        if w2_x_before_y
        else _attr_for_index(lpath2, y.wire2_seg_idx)
    )
    t2_attrs = (
        _attr_for_index(lpath2, y.wire2_seg_idx + 1)
        if w2_x_before_y
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

    # A link physically rests on the board whenever either side it bridges is
    # bare — propagate that to _link so board-pad vertices are preserved.
    def _link_insulated(a: tuple[str, bool], b: tuple[str, bool]) -> bool:
        return a[1] and b[1]

    link_h1_m2 = _link(
        head1_pts[-1],
        mid2_pts[0],
        board_pads=board_pads,
        insulated=_link_insulated(h1_attrs, m2_attrs),
    )
    link_m2_t1 = _link(
        mid2_pts[-1],
        tail1_pts[0],
        board_pads=board_pads,
        insulated=_link_insulated(m2_attrs, t1_attrs),
    )
    link_h2_m1 = _link(
        head2_pts[-1],
        mid1_for_w2[0],
        board_pads=board_pads,
        insulated=_link_insulated(h2_attrs, m1_attrs),
    )
    link_m1_t2 = _link(
        mid1_for_w2[-1],
        tail2_pts[0],
        board_pads=board_pads,
        insulated=_link_insulated(m1_attrs, t2_attrs),
    )

    def _dedupe_consecutive(pts: list[Point]) -> list[Point]:
        """Drop consecutive duplicate vertices — shared-pad slicing duplicates
        the boundary vertex (head ends, mid begins, both contain X)."""
        if not pts:
            return pts
        out: list[Point] = [pts[0]]
        for p in pts[1:]:
            if _pt_equal(p, out[-1]):
                continue
            out.append(p)
        return out

    def _build_lpath(
        head_pts: list[Point],
        link_to_mid: list[Point],
        mid_pts: list[Point],
        link_to_tail: list[Point],
        tail_pts: list[Point],
        outer_attrs: tuple[str, bool],
        mid_attrs: tuple[str, bool],
        outer_attrs_tail: tuple[str, bool],
    ) -> list[_LogicalPoint] | None:
        outer_side, outer_ins = outer_attrs
        mid_side, mid_ins = mid_attrs
        same = outer_attrs == mid_attrs == outer_attrs_tail
        if same or pad_safe is None:
            # Single attribute set — middle inherits outer.
            full = head_pts + link_to_mid + mid_pts + link_to_tail + tail_pts
            full = _dedupe_consecutive(full)
            return [_LogicalPoint(p, outer_side, outer_ins) for p in full]
        # Need split — check pad safety at the two split pads.
        split_a = head_pts[-1]
        split_b = tail_pts[0]
        # We only need to validate when crossing into a new (side, insulated).
        ok_a = mid_attrs == outer_attrs or pad_safe(
            split_a, outer_side, mid_side, outer_ins or mid_ins
        )
        ok_b = mid_attrs == outer_attrs_tail or pad_safe(
            split_b,
            outer_attrs_tail[0],
            mid_side,
            outer_attrs_tail[1] or mid_ins,
        )
        if not (ok_a and ok_b):
            # Fallback — force middle onto outer's attrs.
            full = head_pts + link_to_mid + mid_pts + link_to_tail + tail_pts
            full = _dedupe_consecutive(full)
            return [_LogicalPoint(p, outer_side, outer_ins) for p in full]
        # Three-attribute build.
        full = head_pts + link_to_mid + mid_pts + link_to_tail + tail_pts
        # Dedupe before tagging so attr-per-position alignment isn't shifted.
        dedup = _dedupe_consecutive(full)
        # Re-tag: head + link_to_mid → outer_attrs; mid + link_to_tail → mid_attrs;
        # tail → outer_attrs_tail.  Compare positions to source slices.
        head_set = {(p.x, p.y) for p in head_pts + link_to_mid}
        tail_set = {(p.x, p.y) for p in tail_pts}
        ot_side, ot_ins = outer_attrs_tail
        result: list[_LogicalPoint] = []
        for p in dedup:
            key = (p.x, p.y)
            if key in tail_set and key not in head_set:
                result.append(_LogicalPoint(p, ot_side, ot_ins))
            elif key in head_set:
                result.append(_LogicalPoint(p, outer_side, outer_ins))
            else:
                # mid or link_to_tail
                result.append(_LogicalPoint(p, mid_side, mid_ins))
        return result

    new_lp1 = _build_lpath(
        head1_pts,
        link_h1_m2,
        mid2_pts,
        link_m2_t1,
        tail1_pts,
        outer_attrs=h1_attrs,
        mid_attrs=m2_attrs,
        outer_attrs_tail=t1_attrs,
    )
    new_lp2 = _build_lpath(
        head2_pts,
        link_h2_m1,
        mid1_for_w2,
        link_m1_t2,
        tail2_pts,
        outer_attrs=h2_attrs,
        mid_attrs=m1_attrs,
        outer_attrs_tail=t2_attrs,
    )
    if new_lp1 is None or new_lp2 is None:
        return None
    return new_lp1, new_lp2


# ---------------------------------------------------------------------------
# Trivial 1-step reroute for shared-pad crossings
# ---------------------------------------------------------------------------
#
# When two logical wires share an interior vertex S that survives the main
# swap pass (e.g. the pair has two shared-pad crossings; swap moves fragments
# but leaves both shared vertices intact), try the simplest possible reroute:
# replace S on one of the wires with a board pad adjacent to S (1-step ortho
# or diagonal).  No A*, no multi-step detours — if the immediate neighbourhood
# has nothing usable, give up and leave the shared vertex (the swap already
# rearranged the wires around it, which is no worse than the pre-unweave
# state).


_NEIGHBOUR_OFFSETS: tuple[tuple[int, int], ...] = (
    (1, 0),
    (-1, 0),
    (0, 1),
    (0, -1),
    (1, 1),
    (1, -1),
    (-1, 1),
    (-1, -1),
)


def _is_unit_step(a: Point, b: Point) -> bool:
    """True iff ``b`` is one ortho or diagonal step away from ``a``.

    Uses Chebyshev distance == 1; matches the routing grid's 8-connectivity.
    """
    dx = abs(a.x - b.x)
    dy = abs(a.y - b.y)
    return max(dx, dy) - 1.0 < _EPS and (dx + dy) > _EPS


def _try_1step_reroute_at(
    lp_self: list[_LogicalPoint],
    lp_other: list[_LogicalPoint],
    idx: int,
    *,
    board_pads: set[Point] | None,
    forbidden_pads: set[Point] | None,
    pad_safe: Callable[[Point, str, str, bool], bool] | None,
) -> list[_LogicalPoint] | None:
    """Replace ``lp_self[idx].point`` with a one-step-adjacent board pad that
    leaves ``prev → S' → next`` geometrically valid and frees the original pad
    from this wire.  Returns a new logical path on success, ``None`` if no
    suitable neighbour exists.

    Constraints:
      * ``S'`` lies on ``board_pads`` (when given).
      * ``S'`` is not in ``forbidden_pads`` (typically: pin positions of
        components other than the wire's own endpoints — we don't want
        reroute to land on an alien pin even though insulated wires would
        physically tolerate it; staying on free pads keeps later edits
        and DRC simpler).
      * ``S'`` is not used by ``lp_other`` (we don't want to introduce a new
        shared vertex while removing the old one).
      * ``S'`` is one Chebyshev step from S **and** from both neighbours, so
        the resulting segments stay legal routing steps.
      * ``pad_safe`` (when given) accepts S' against the segment's side /
        insulated state.  We use the attributes of the segment ENTERING S
        (``lp_self[idx - 1].side`` / ``insulated``) for both new sub-segments
        — reroute is purely local and never crosses a layer.
    """
    if idx <= 0 or idx >= len(lp_self) - 1:
        return None
    prev_pt = lp_self[idx - 1].point
    s = lp_self[idx].point
    next_pt = lp_self[idx + 1].point

    side_in = lp_self[idx - 1].side
    ins_in = lp_self[idx - 1].insulated
    # The vertex's own side/insulated describes the LEAVING segment; for a
    # 1-step reroute we keep both segments on the same layer the wire was
    # already using around S.
    side_out = lp_self[idx].side
    ins_out = lp_self[idx].insulated
    if side_in != side_out or ins_in != ins_out:
        # Layer change happens exactly at S — reroute is out of scope (a
        # neighbour pad would shift the via, not just relocate a vertex).
        return None

    other_pts: set[tuple[float, float]] = {
        (round(lp.point.x, 6), round(lp.point.y, 6)) for lp in lp_other
    }

    for dx, dy in _NEIGHBOUR_OFFSETS:
        cand = s + Vector(dx, dy)
        if board_pads is not None and cand not in board_pads:
            continue
        if forbidden_pads is not None and cand in forbidden_pads:
            # Alien pin (or any other reserved pad).  Stay on free pads only.
            continue
        if (round(cand.x, 6), round(cand.y, 6)) in other_pts:
            # Don't trade one shared vertex for another with the same wire.
            continue
        if _pt_equal(cand, prev_pt) or _pt_equal(cand, next_pt):
            # Would collapse prev–S'–next to a degenerate path.
            continue
        if not _is_unit_step(prev_pt, cand) or not _is_unit_step(cand, next_pt):
            continue
        if pad_safe is not None and not pad_safe(cand, side_in, side_in, ins_in):
            continue
        # Build the rerouted logical path: same prefix/suffix, S replaced.
        new_lp = list(lp_self)
        new_lp[idx] = _LogicalPoint(cand, side_out, ins_out)
        return new_lp
    return None


def _count_pair_crossings(
    lp1: list[_LogicalPoint],
    lp2: list[_LogicalPoint],
) -> int:
    """Count *all* crossings (interior segment + shared-pad) between two
    logical paths.  Used as the acceptance guard for 1-step reroute — the
    move is only kept when it does not increase the total."""
    return len(_find_crossings_logical(lp1, lp2))


def _count_pair_shared_vertices(
    lp1: list[_LogicalPoint],
    lp2: list[_LogicalPoint],
) -> int:
    """Count interior vertices that appear on both logical paths."""
    return len(_find_shared_pad_crossings_logical(lp1, lp2))


def _count_pair_2d_crossings(w1: Wire, w2: Wire) -> int:
    """Count all 2D crossings between two wires (geometric + interleaved
    shared-pad), IGNORING side.  Used by the visual-unweave side-flip pass
    where the goal is to reduce visual tangling regardless of layer."""
    return len(find_crossings(w1, w2))


def _count_pair_2d_shared_interior(w1: Wire, w2: Wire) -> int:
    """Count interior pads shared between two wires in the 2D projection,
    IGNORING side and ignoring direction interleave.  Counts every pad
    that both wires visit as a non-endpoint vertex — the geometric measure
    of "wires lie on top of each other" visible to the user looking at
    the board from above."""
    interior2: set[tuple[float, float]] = set()
    for j in range(1, len(w2.path) - 1):
        interior2.add((round(w2.path[j].x, 6), round(w2.path[j].y, 6)))
    count = 0
    for i in range(1, len(w1.path) - 1):
        key = (round(w1.path[i].x, 6), round(w1.path[i].y, 6))
        if key in interior2:
            count += 1
    return count


def _cleanup_shared_pads_pass(
    logical: list[list[Wire]],
    *,
    pad_safe: Callable[[Point, str, str, bool], bool] | None,
    short_check: Callable[[list[list[Wire]]], bool] | None,
    board_pads: set[Point] | None,
    forbidden_pads: set[Point] | None,
) -> None:
    """One pass of trivial 1-step reroute over residual shared-pad crossings.

    For every pair (i, j) of logical wires and every shared-pad crossing
    between them, try to relocate the shared vertex on lp_i; if that fails,
    try lp_j.  On success the move is committed to ``logical`` in-place.

    Acceptance gates:
      * total i↔j crossings must not increase
      * ``short_check`` (when given) must accept the resulting layout
    Endpoints are preserved by construction (idx ∈ [1, len-2]).
    """
    n = len(logical)
    n_pairs_with_shared = 0
    n_reroutes_committed = 0
    n_reroutes_rejected = 0
    for i in range(n):
        for j in range(i + 1, n):
            # Re-derive lpaths each pair because earlier pairs may have
            # rewritten one of these wires.
            lp_i = _to_logical_path(logical[i])
            lp_j = _to_logical_path(logical[j])
            shared = _find_shared_pad_crossings_logical(lp_i, lp_j)
            if shared:
                n_pairs_with_shared += 1
                _log.info(
                    "unweave.cleanup.pair_has_shared",
                    pair=(i, j),
                    n_shared=len(shared),
                    shared_pts=[
                        (lp_i[si].point.x, lp_i[si].point.y) for si, _sj in shared
                    ],
                )
            if not shared:
                continue
            for s_idx_i, s_idx_j in shared:
                shared_before = _count_pair_shared_vertices(lp_i, lp_j)
                crossings_before = _count_pair_crossings(lp_i, lp_j)

                def _accepts(
                    cand_i: list[_LogicalPoint],
                    cand_j: list[_LogicalPoint],
                ) -> bool:
                    # Reroute must strictly reduce shared interior pads and
                    # never grow the total crossing count.
                    return (
                        _count_pair_shared_vertices(cand_i, cand_j) < shared_before
                        and _count_pair_crossings(cand_i, cand_j) <= crossings_before
                    )

                # Try wire i first.
                new_lp_i = _try_1step_reroute_at(
                    lp_i,
                    lp_j,
                    s_idx_i,
                    board_pads=board_pads,
                    forbidden_pads=forbidden_pads,
                    pad_safe=pad_safe,
                )
                cand_lp_i, cand_lp_j = lp_i, lp_j
                chose: Literal["i", "j", None] = None
                if new_lp_i is not None and _accepts(new_lp_i, lp_j):
                    cand_lp_i = new_lp_i
                    chose = "i"
                else:
                    new_lp_j = _try_1step_reroute_at(
                        lp_j,
                        lp_i,
                        s_idx_j,
                        board_pads=board_pads,
                        forbidden_pads=forbidden_pads,
                        pad_safe=pad_safe,
                    )
                    if new_lp_j is not None and _accepts(lp_i, new_lp_j):
                        cand_lp_j = new_lp_j
                        chose = "j"
                if chose is None:
                    n_reroutes_rejected += 1
                    _log.info(
                        "unweave.cleanup.reroute_rejected",
                        pair=(i, j),
                        shared_pt=(lp_i[s_idx_i].point.x, lp_i[s_idx_i].point.y),
                        reason="no_free_neighbour_or_grows_crossings",
                    )
                    continue
                new_segs_i = _from_logical_path(
                    cand_lp_i,
                    seed_ids=[s.id for s in logical[i]],
                )
                new_segs_j = _from_logical_path(
                    cand_lp_j,
                    seed_ids=[s.id for s in logical[j]],
                )
                if short_check is not None:
                    proposed = list(logical)
                    proposed[i] = new_segs_i
                    proposed[j] = new_segs_j
                    if not short_check(proposed):
                        n_reroutes_rejected += 1
                        _log.info(
                            "unweave.cleanup.reroute_rejected",
                            pair=(i, j),
                            shared_pt=(lp_i[s_idx_i].point.x, lp_i[s_idx_i].point.y),
                            reason="short_check",
                        )
                        continue
                logical[i] = new_segs_i
                logical[j] = new_segs_j
                n_reroutes_committed += 1
                _log.info(
                    "unweave.cleanup.reroute_done",
                    pair=(i, j),
                    chose=chose,
                    shared_pt=(lp_i[s_idx_i].point.x, lp_i[s_idx_i].point.y),
                    new_pt=(
                        (cand_lp_i[s_idx_i].point.x, cand_lp_i[s_idx_i].point.y)
                        if chose == "i"
                        else (cand_lp_j[s_idx_j].point.x, cand_lp_j[s_idx_j].point.y)
                    ),
                )
                # Refresh both lpaths for the next shared vertex in this pair.
                lp_i = _to_logical_path(logical[i])
                lp_j = _to_logical_path(logical[j])
    _log.info(
        "unweave.cleanup.summary",
        n_pairs_with_shared=n_pairs_with_shared,
        n_reroutes_committed=n_reroutes_committed,
        n_reroutes_rejected=n_reroutes_rejected,
    )


# ---------------------------------------------------------------------------
# Visual-unweave: side-flip pass (opt-in via RoutingSettings.visual_unweave_side_flip)
# ---------------------------------------------------------------------------
#
# When the topological swap and overlap-separation passes have done what they
# can, residual visual tangling may remain — typically two wires whose 2D
# projections weave through one another even though they sit on different
# board sides.  The swap/overlap machinery operates per-side (after the
# 2026-06-09 cross-side fix), so it has nothing to do for these pairs.
#
# This pass tries a structural move: flip the entire ``side`` attribute of
# one wire to its opposite, validate via pad_safe + short_check, accept if
# the 2D measure improves.  No path mutation — only the layer attribute
# changes.  Endpoints stay where they are (their plated-through-ness
# determines whether the flip is electrically legal — pad_safe catches that).


def _side_flip_pass(
    logical: list[list[Wire]],
    *,
    pad_safe: Callable[[Point, str, str, bool], bool] | None,
    short_check: Callable[[list[list[Wire]]], bool] | None,
) -> None:
    """Greedy per-pair side flip.

    For every pair (i, j) of logical wires sharing 2D tangling (>0
    crossings or >0 shared interior pads in the 2D projection), try
    flipping one wire's side to the opposite layer.  Accept when the
    combined gate passes:

      * 2D shared interior pads do not grow AND
      * 2D crossings do not grow AND
      * at least one of the two strictly decreases AND
      * ``short_check`` (when given) accepts the resulting layout.

    Tries flipping wire i first, then wire j if (i) didn't qualify.
    Restart the outer loop after every successful flip so cascading
    benefits are captured (one flip may free another flip).  Bounded by
    ``2 * n * n`` flips total (defensive).
    """
    n = len(logical)
    if n < 2:
        return

    def _flip_chain(chain: list[Wire]) -> list[Wire]:
        """Return a copy of chain with every wire's side flipped."""
        flipped: list[Wire] = []
        for w in chain:
            new_side: Side = "bottom" if w.side == "top" else "top"
            flipped.append(replace(w, side=new_side))
        return flipped

    def _validate_pad_safe(flipped_chain: list[Wire]) -> bool:
        """Per-vertex pad_safe check for every wire in the flipped chain.
        We call pad_safe(pad, current_side=new_side, new_side=new_side,
        is_insulated=wire.insulated) — the caller's predicate decides
        whether the pad accepts a wire of (side, insulated) here.
        Returns False on the first rejection."""
        if pad_safe is None:
            return True
        for w in flipped_chain:
            for p in w.path:
                if not pad_safe(p, w.side, w.side, w.insulated):
                    return False
        return True

    def _measure_pair(chain_i: list[Wire], chain_j: list[Wire]) -> tuple[int, int]:
        """Return (total_2d_crossings, total_2d_shared) between two chains
        of WireSegments, summed over all (segment_i, segment_j) pairs."""
        total_x = 0
        total_s = 0
        for wi in chain_i:
            for wj in chain_j:
                total_x += _count_pair_2d_crossings(wi, wj)
                total_s += _count_pair_2d_shared_interior(wi, wj)
        return total_x, total_s

    n_flips_committed = 0
    n_flips_rejected = 0
    max_iter = 2 * n * n + 4
    for _it in range(max_iter):
        progressed = False
        for i in range(n):
            for j in range(i + 1, n):
                cross_before, shared_before = _measure_pair(logical[i], logical[j])
                if cross_before == 0 and shared_before == 0:
                    continue  # nothing to fix for this pair

                def _accepts(cross_after: int, shared_after: int) -> bool:
                    if cross_after > cross_before or shared_after > shared_before:
                        return False
                    return cross_after < cross_before or shared_after < shared_before

                committed: Literal["i", "j", None] = None
                # Try flipping wire i first.
                cand_i = _flip_chain(logical[i])
                if _validate_pad_safe(cand_i):
                    cx, cs = _measure_pair(cand_i, logical[j])
                    if _accepts(cx, cs):
                        proposed = list(logical)
                        proposed[i] = cand_i
                        if short_check is None or short_check(proposed):
                            logical[i] = cand_i
                            committed = "i"

                if committed is None:
                    cand_j = _flip_chain(logical[j])
                    if _validate_pad_safe(cand_j):
                        cx, cs = _measure_pair(logical[i], cand_j)
                        if _accepts(cx, cs):
                            proposed = list(logical)
                            proposed[j] = cand_j
                            if short_check is None or short_check(proposed):
                                logical[j] = cand_j
                                committed = "j"

                if committed is None:
                    n_flips_rejected += 1
                    _log.info(
                        "unweave.side_flip.rejected",
                        pair=(i, j),
                        cross_before=cross_before,
                        shared_before=shared_before,
                        reason="no_improvement_or_short_check_failed",
                    )
                else:
                    n_flips_committed += 1
                    _log.info(
                        "unweave.side_flip.committed",
                        pair=(i, j),
                        chose=committed,
                        cross_before=cross_before,
                        shared_before=shared_before,
                    )
                    progressed = True
                    break
            if progressed:
                break
        if not progressed:
            break
    _log.info(
        "unweave.side_flip.summary",
        n_flips_committed=n_flips_committed,
        n_flips_rejected=n_flips_rejected,
    )


def _find_shared_pad_crossings_logical(
    lp1: list[_LogicalPoint],
    lp2: list[_LogicalPoint],
) -> list[tuple[int, int]]:
    """Return ``(idx_in_lp1, idx_in_lp2)`` pairs for each shared interior
    vertex between the two logical paths.

    Unlike ``_find_shared_pad_crossings`` this does NOT require interleaved
    directions — the 1-step reroute wants to free *any* shared interior pad,
    even when the two wires meet tangentially (after a swap has already
    untangled the topology around them).  Removing a tangent shared vertex
    leaves more freedom for downstream tools (manual edits, simplify,
    autoroute optimize) even if it doesn't reduce the topological crossing
    count by itself.
    """
    out: list[tuple[int, int]] = []
    p1 = [lp.point for lp in lp1]
    p2 = [lp.point for lp in lp2]
    interior_p2: dict[tuple[float, float], int] = {}
    for j in range(1, len(p2) - 1):
        key = (round(p2[j].x, 6), round(p2[j].y, 6))
        interior_p2.setdefault(key, j)
    for i in range(1, len(p1) - 1):
        v = p1[i]
        key = (round(v.x, 6), round(v.y, 6))
        matched_j = interior_p2.get(key)
        if matched_j is None:
            continue
        out.append((i, matched_j))
    return out


# ---------------------------------------------------------------------------
# Overlap-run separation pass (runs BEFORE the main swap loop)
# ---------------------------------------------------------------------------
#
# When two logical wires share a contiguous run of K≥2 interior pads — the
# common output of greedy A* sending two nets along the same corridor — the
# main swap loop misses them entirely: parallel directions at a shared pad
# don't interleave, so `_find_shared_pad_crossings` returns nothing.  The
# post-swap 1-step cleanup also fails because reroute candidates for the
# first shared pad collide with the *next* shared pad (still on the wire),
# so the wire stays glued to the overlap.
#
# This pass detects every overlap run and tries to shift one wire's interior
# slice off the overlap onto a parallel free corridor (row or column 1 step
# away).  Acceptance is conservative: shared count must strictly decrease
# AND total crossings must not grow AND short_check must still pass.


def _find_overlap_runs(
    lp1: list[_LogicalPoint],
    lp2: list[_LogicalPoint],
) -> list[tuple[int, int, int, int, int]]:
    """Maximal contiguous runs of shared interior pads between two logical
    paths, restricted to the **same side**.

    Returns a list of ``(i_start, i_end, j_start, j_end, direction)`` where:
      * ``lp1[i_start..i_end]`` equals ``lp2[j_start..j_end]`` pointwise
        when ``direction == +1`` (same traversal direction), or equals it
        reversed when ``direction == -1``.
      * All indices are STRICT interior: ``1 <= i_start <= i_end <= len-2``
        and likewise for j.
      * Minimum run length 2 (single shared pads are handled by the
        post-swap ``_cleanup_shared_pads_pass``).
      * Both vertices on a matched pair must agree on ``side`` — cross-side
        "overlap" is purely visual (the two wires live on different layers
        and do not physically share copper), so the shift pass has nothing
        to fix.  Filtering here avoids a futile shift attempt that always
        rejects later anyway.

    Runs are returned sorted by ``i_start`` and are non-overlapping (a
    vertex is part of at most one run).
    """
    if len(lp1) < 4 or len(lp2) < 4:
        return []

    def _key(p: Point) -> tuple[float, float]:
        return (round(p.x, 6), round(p.y, 6))

    # Map every interior pad on lp2 to its index(es).  A vertex appearing
    # multiple times yields multiple candidates — try each.
    interior_p2: dict[tuple[float, float], list[int]] = {}
    for j in range(1, len(lp2) - 1):
        interior_p2.setdefault(_key(lp2[j].point), []).append(j)

    runs: list[tuple[int, int, int, int, int]] = []
    i = 1
    last_i_end = 0  # ensure runs don't overlap on lp1
    while i < len(lp1) - 1:
        if i <= last_i_end:
            i += 1
            continue
        candidates = interior_p2.get(_key(lp1[i].point), [])
        # Side filter: only pads where both wires are on the same side.
        candidates = [j for j in candidates if lp2[j].side == lp1[i].side]
        best: tuple[int, int, int, int, int] | None = None
        best_len = 0
        for j0 in candidates:
            for direction in (+1, -1):
                # Extend forward on lp1 as long as the matching lp2 index
                # stays interior, the pads coincide, AND both wires remain
                # on the same side at the matched pair.
                length = 1
                while True:
                    ni = i + length
                    nj = j0 + direction * length
                    if ni >= len(lp1) - 1 or nj < 1 or nj > len(lp2) - 2:
                        break
                    if _key(lp1[ni].point) != _key(lp2[nj].point):
                        break
                    if lp1[ni].side != lp2[nj].side:
                        break
                    length += 1
                if length >= 2 and length > best_len:
                    j_start = j0 if direction == +1 else j0 - (length - 1)
                    j_end = j0 + (length - 1) if direction == +1 else j0
                    best = (i, i + length - 1, j_start, j_end, direction)
                    best_len = length
        if best is not None:
            runs.append(best)
            last_i_end = best[1]
            i = best[1] + 1
        else:
            i += 1
    return runs


def _try_shift_overlap_run(
    lp_self: list[_LogicalPoint],
    lp_other: list[_LogicalPoint],
    i_start: int,
    i_end: int,
    *,
    board_pads: set[Point] | None,
    forbidden_pads: set[Point] | None,
    foreign_wire_pads: set[tuple[Point, str]] | None,
    pad_safe: Callable[[Point, str, str, bool], bool] | None,
) -> list[_LogicalPoint] | None:
    """Try to replace ``lp_self[i_start..i_end]`` with a parallel-shifted
    sequence of pads that frees the overlap.

    Strategy: for every shift offset ``(dx, dy)`` in ``_NEIGHBOUR_OFFSETS``
    build the candidate sequence ``[shift(p) for p in lp_self[i_start..i_end]]``
    and accept the first one that passes every constraint:

      * ``prev → cand[0]`` is a unit Chebyshev step.
      * Every consecutive ``(cand[k-1], cand[k])`` is a unit step.
      * ``cand[-1] → next`` is a unit step.
      * Every cand pad lies on ``board_pads`` (when given).
      * No cand pad sits on ``forbidden_pads`` (alien pin positions).
      * No cand pad coincides with any vertex of ``lp_other``.
      * The (side, insulated) of every shifted vertex matches the segment
        the wire was running on through the overlap — we never change
        layer.  Layer changes inside the run abort the shift.
      * ``pad_safe`` (when given) accepts each cand pad.

    Returns the new logical path with shifted slice; ``None`` if no offset
    qualifies.
    """
    if i_start <= 0 or i_end >= len(lp_self) - 1 or i_end < i_start:
        return None

    # Layer must be uniform across the run (same side+insulated on every
    # segment leaving the run's interior).  A layer change inside the run
    # would shift the via too, which is out of scope for this local pass.
    side = lp_self[i_start - 1].side
    ins = lp_self[i_start - 1].insulated
    for k in range(i_start - 1, i_end + 1):
        if lp_self[k].side != side or lp_self[k].insulated != ins:
            return None

    prev_pt = lp_self[i_start - 1].point
    next_pt = lp_self[i_end + 1].point
    orig_pts = [lp_self[k].point for k in range(i_start, i_end + 1)]

    other_pts: set[tuple[float, float]] = {
        (round(lp.point.x, 6), round(lp.point.y, 6)) for lp in lp_other
    }

    for dx, dy in _NEIGHBOUR_OFFSETS:
        cand_pts = [p + Vector(dx, dy) for p in orig_pts]

        # Geometric validity of the chained steps.
        if not _is_unit_step(prev_pt, cand_pts[0]):
            continue
        if not _is_unit_step(cand_pts[-1], next_pt):
            continue
        chain_ok = True
        for k in range(1, len(cand_pts)):
            if not _is_unit_step(cand_pts[k - 1], cand_pts[k]):
                chain_ok = False
                break
        if not chain_ok:
            continue

        # Pad-set membership and reservations.
        pad_ok = True
        for cand in cand_pts:
            if board_pads is not None and cand not in board_pads:
                pad_ok = False
                break
            if forbidden_pads is not None and cand in forbidden_pads:
                pad_ok = False
                break
            if foreign_wire_pads is not None and (cand, side) in foreign_wire_pads:
                # Another wire on this side (not part of this unweave
                # operation) already sits on this pad — shift would
                # create a collision invisible to the lp_other check.
                # Cross-side foreign wires do NOT block (different layer).
                pad_ok = False
                break
            if (round(cand.x, 6), round(cand.y, 6)) in other_pts:
                pad_ok = False
                break
            if _pt_equal(cand, prev_pt) or _pt_equal(cand, next_pt):
                pad_ok = False
                break
            if pad_safe is not None and not pad_safe(cand, side, side, ins):
                pad_ok = False
                break
        if not pad_ok:
            continue

        # Build the rerouted logical path.
        new_lp = list(lp_self)
        for k, cand in enumerate(cand_pts):
            new_lp[i_start + k] = _LogicalPoint(cand, side, ins)
        return new_lp
    return None


def _separate_overlap_runs_pass(
    logical: list[list[Wire]],
    *,
    pad_safe: Callable[[Point, str, str, bool], bool] | None,
    short_check: Callable[[list[list[Wire]]], bool] | None,
    board_pads: set[Point] | None,
    forbidden_pads: set[Point] | None,
    foreign_wire_pads: set[tuple[Point, str]] | None = None,
) -> None:
    """First-phase pass: detect and separate overlap runs between every
    logical-wire pair.

    For each pair ``(i, j)`` find every overlap run and try to shift it on
    ``lp_i``; if that fails, try on ``lp_j``.  Accept when:

      * shared vertex count strictly decreases AND
      * total crossing count does not grow AND
      * ``short_check`` (when given) accepts the proposed full layout.

    Successful shifts are committed in-place to ``logical``.  Re-derives
    lpaths after every commit because the next run on the same pair may
    have shifted.  No livelock guard needed — every commit strictly
    reduces shared vertex count, so the total work is bounded by the
    initial sum of overlap-run lengths.
    """
    n = len(logical)
    n_pairs_with_overlap = 0
    n_shifts_committed = 0
    n_shifts_rejected = 0
    for i in range(n):
        for j in range(i + 1, n):
            progressed_pair = True
            while progressed_pair:
                progressed_pair = False
                lp_i = _to_logical_path(logical[i])
                lp_j = _to_logical_path(logical[j])
                runs = _find_overlap_runs(lp_i, lp_j)
                if not runs:
                    break
                n_pairs_with_overlap += 1
                # Pick the longest run first — biggest crossing-count win
                # if any.
                runs.sort(key=lambda r: -(r[1] - r[0] + 1))
                for i_start, i_end, j_start, j_end, _direction in runs:
                    shared_before = _count_pair_shared_vertices(lp_i, lp_j)
                    crossings_before = _count_pair_crossings(lp_i, lp_j)

                    def _accepts(
                        cand_i: list[_LogicalPoint],
                        cand_j: list[_LogicalPoint],
                    ) -> bool:
                        return (
                            _count_pair_shared_vertices(cand_i, cand_j) < shared_before
                            and _count_pair_crossings(cand_i, cand_j)
                            <= crossings_before
                        )

                    # Try shifting wire i's slice first.
                    new_lp_i = _try_shift_overlap_run(
                        lp_i,
                        lp_j,
                        i_start,
                        i_end,
                        board_pads=board_pads,
                        forbidden_pads=forbidden_pads,
                        foreign_wire_pads=foreign_wire_pads,
                        pad_safe=pad_safe,
                    )
                    cand_lp_i, cand_lp_j = lp_i, lp_j
                    chose: Literal["i", "j", None] = None
                    if new_lp_i is not None and _accepts(new_lp_i, lp_j):
                        cand_lp_i = new_lp_i
                        chose = "i"
                    else:
                        new_lp_j = _try_shift_overlap_run(
                            lp_j,
                            lp_i,
                            j_start,
                            j_end,
                            board_pads=board_pads,
                            forbidden_pads=forbidden_pads,
                            foreign_wire_pads=foreign_wire_pads,
                            pad_safe=pad_safe,
                        )
                        if new_lp_j is not None and _accepts(lp_i, new_lp_j):
                            cand_lp_j = new_lp_j
                            chose = "j"
                    if chose is None:
                        n_shifts_rejected += 1
                        _log.info(
                            "unweave.overlap.shift_rejected",
                            pair=(i, j),
                            run_len=i_end - i_start + 1,
                            reason="no_free_corridor_or_acceptance_failed",
                        )
                        continue

                    new_segs_i = _from_logical_path(
                        cand_lp_i,
                        seed_ids=[s.id for s in logical[i]],
                    )
                    new_segs_j = _from_logical_path(
                        cand_lp_j,
                        seed_ids=[s.id for s in logical[j]],
                    )
                    if short_check is not None:
                        proposed = list(logical)
                        proposed[i] = new_segs_i
                        proposed[j] = new_segs_j
                        if not short_check(proposed):
                            n_shifts_rejected += 1
                            _log.info(
                                "unweave.overlap.shift_rejected",
                                pair=(i, j),
                                run_len=i_end - i_start + 1,
                                reason="short_check",
                            )
                            continue
                    logical[i] = new_segs_i
                    logical[j] = new_segs_j
                    n_shifts_committed += 1
                    _log.info(
                        "unweave.overlap.shift_done",
                        pair=(i, j),
                        chose=chose,
                        run_len=i_end - i_start + 1,
                    )
                    progressed_pair = True
                    break  # re-derive lpaths and look for next overlap
    _log.info(
        "unweave.overlap.summary",
        n_pairs_with_overlap=n_pairs_with_overlap,
        n_shifts_committed=n_shifts_committed,
        n_shifts_rejected=n_shifts_rejected,
    )


def _attempt_pair_swap(
    logical: list[list[Wire]],
    i: int,
    j: int,
    *,
    pad_safe: Callable[[Point, str, str, bool], bool] | None,
    short_check: Callable[[list[list[Wire]]], bool] | None = None,
    board_pads: set[Point] | None = None,
) -> bool:
    """Try one swap between logical wires *i* and *j*, operating on
    concatenated logical paths.  Returns True on success.

    ``short_check`` (optional) is invoked on a proposed `logical` after the
    swap; if it returns False, the swap is rejected (rolled back).
    """
    lp_i = _to_logical_path(logical[i])
    lp_j = _to_logical_path(logical[j])
    cs = _find_crossings_logical(lp_i, lp_j)
    if len(cs) > 0:
        _log.info(
            "unweave.pair_crossings",
            pair=(i, j),
            n_crossings=len(cs),
            points=[(c.point.x, c.point.y) for c in cs],
        )
    pair = _pick_swap_pair(cs)
    if pair is None:
        if len(cs) == 1:
            _log.info(
                "unweave.pair_skip",
                pair=(i, j),
                reason="single_crossing_unresolvable",
                point=(cs[0].point.x, cs[0].point.y),
            )
        return False
    a, b = cs[pair[0]], cs[pair[1]]
    result = _swap_logical_fragment(
        lp_i,
        lp_j,
        a,
        b,
        pad_safe=pad_safe,
        board_pads=board_pads,
    )
    if result is None:
        _log.info(
            "unweave.pair_skip",
            pair=(i, j),
            reason="swap_fragment_returned_none",
            a=(a.point.x, a.point.y),
            b=(b.point.x, b.point.y),
        )
        return False
    new_lp_i, new_lp_j = result
    # Endpoints preserved?
    if (
        not _pt_equal(new_lp_i[0].point, lp_i[0].point)
        or not _pt_equal(new_lp_i[-1].point, lp_i[-1].point)
        or not _pt_equal(new_lp_j[0].point, lp_j[0].point)
        or not _pt_equal(new_lp_j[-1].point, lp_j[-1].point)
    ):
        _log.info("unweave.pair_skip", pair=(i, j), reason="endpoints_lost")
        return False
    # Convert each logical path back into a chain of WireSegments.  Reuse
    # the input segment ids so wire identity survives the swap (a swap is
    # a redistribution of geometry between two wires, not a creation of
    # new ones — preserves highlight, recording and undo).
    new_segs_i = _from_logical_path(new_lp_i, seed_ids=[s.id for s in logical[i]])
    new_segs_j = _from_logical_path(new_lp_j, seed_ids=[s.id for s in logical[j]])
    if short_check is not None:
        # Propose the swap; pass the entire current `logical` state with
        # i/j swapped so the checker sees the FULL post-swap layout from
        # the unweave scope (other logical wires already swapped in earlier
        # iterations stay in place).
        proposed = list(logical)
        proposed[i] = new_segs_i
        proposed[j] = new_segs_j
        if not short_check(proposed):
            _log.info("unweave.pair_skip", pair=(i, j), reason="short_check_rejected")
            return False
    logical[i] = new_segs_i
    logical[j] = new_segs_j
    return True


def _group_by_endpoints(wires: list[Wire]) -> list[list[Wire]]:
    """Chain WireSegments into ordered logical-wire lists.

    Two segments belong to the same logical wire when one's terminal pad
    equals the other's starting pad.  Each returned chain is ordered so that
    ``chain[k].path[-1] == chain[k+1].path[0]``.  Standalone WireSegments
    become 1-element chains.
    """
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
    chains: list[list[Wire]] = []
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


def _remerge(segments: list[Wire]) -> list[Wire]:
    """Fuse consecutive WireSegments that share (side, insulated) and an
    endpoint pad.  Preserves order and never crosses an attribute boundary.

    Identity policy: the fused segment keeps the id of the *first* segment
    in the run — the second is absorbed.  This preserves wire identity for
    the dominant wire when a no-op split-and-merge cycle would otherwise
    have churned UUIDs (segment-proliferation bug, d33ea56).
    """
    if not segments:
        return []
    out: list[Wire] = [segments[0]]
    for nxt in segments[1:]:
        prev = out[-1]
        if (
            prev.side == nxt.side
            and prev.insulated == nxt.insulated
            and _pt_equal(prev.path[-1], nxt.path[0])
        ):
            fused = replace(
                prev,
                path=list(prev.path) + list(nxt.path[1:]),
            )
            out[-1] = fused
        else:
            out.append(nxt)
    return out


def _make_pad_safety(
    all_wires: list[Wire],
    schematic: Schematic,
    graph: ConnectivityGraph,
    *,
    allow_layer_change: bool = True,
) -> Callable[[Point, str, str, bool], bool]:
    """Build the pad-safety predicate used by ``_swap_fragment_between``.

    A pad is "safe" to attach to (for a side change) when joining there
    does not create a galvanic contact with a foreign electric net.
    Concretely:
      - the pad on the *new* side belongs to the same electric set as the
        pad on the *current* side (plated-through or already in this net), OR
      - the pad on the new side is not part of any other net (empty pad).

    When ``allow_layer_change`` is False every side-changing query returns
    False — unweave then falls back to single-side glue and never produces
    multi-side wires.  Same-side queries are still answered truthfully so
    insulated/bare changes inside one layer remain available.
    """
    # Collect pad positions that belong to any net (via pins).
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
        if not allow_layer_change:
            return False
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
    wires: Iterable[Wire],
    graph: ConnectivityGraph,
) -> int:
    """Count strict-interior crossings between bare wires that share a side
    but belong to different electric sets — i.e. physical off-pad shorts.

    Same electric set (e.g. two segments of the same logical wire that
    happen to cross — possible after a swap) does not count.
    """
    bare = [w for w in wires if not w.insulated]
    by_side: dict[str, list[Wire]] = defaultdict(list)
    for w in bare:
        by_side[w.side].append(w)
    count = 0
    for side, ws in by_side.items():
        for i in range(len(ws)):
            wi = ws[i]
            k_i = graph.node_for_pad(wi.path[0], side) or graph.node_for_pad(
                wi.path[-1], side
            )
            for j in range(i + 1, len(ws)):
                wj = ws[j]
                k_j = graph.node_for_pad(wj.path[0], side) or graph.node_for_pad(
                    wj.path[-1], side
                )
                if k_i is not None and k_j is not None and k_i == k_j:
                    continue
                for a in range(len(wi.path) - 1):
                    for b in range(len(wj.path) - 1):
                        if segments_cross_strict(
                            wi.path[a],
                            wi.path[a + 1],
                            wj.path[b],
                            wj.path[b + 1],
                        ):
                            count += 1
    return count


def _make_short_checker(
    schematic: Schematic,
    base_board: Board,
    base_layout: Layout,
    base_components: dict[str, ComponentDefinition],
    base_instances: dict[uuid.UUID, ComponentInstance],
    *,
    target_ids: set[uuid.UUID] | None = None,
    check_bare_same_side: bool = False,
) -> Callable[[list[list[Wire]]], bool]:
    """Build a *monotonic* checker that rejects a proposed unweave step
    when it would *worsen* the project's electrical invariants relative to
    the baseline layout.

    Three invariants are tracked monotonically:

    (a) **Net fragmentation** — a net whose pins are already split across
        multiple electric sets in the baseline (typically because routing
        is incomplete) may stay fragmented after the swap, but the swap
        must not *increase* the number of disjoint sets the net spans nor
        *drop* a pin that previously had connectivity.
    (b) **Cross-net shorts** — an electric set that already hosts pins
        from multiple nets in the baseline (an existing short the user
        will eventually resolve) is tolerated, but the swap must not
        introduce a new such set.  Counted as (set_id, {net_ids}) pairs
        and required to be a subset of the baseline pair set.
    (c) **Bare same-side off-pad crossings** — already monotonic, kept.

    Rationale: unweave is also a *cleanup* pass that makes room for nets
    that have not been fully routed yet.  Treating any baseline imperfection
    as a hard reject would make unweave useless mid-routing.  The
    monotonic semantics let swaps reduce crossings even when GND etc. is
    still being wired up; only swaps that *break* something get blocked.

    *target_ids* identifies which wires from the baseline layout are being
    replaced by *proposed*.  All other baseline wires are kept untouched so
    nets they participate in stay connected.
    """
    from protoboard.domain.connectivity.graph import ConnectivityGraph
    from protoboard.domain.layout.layout import Layout
    from protoboard.domain.schematic.net import PinRef

    # Pin → net mapping snapshot — source of truth for invariants.
    pin_to_net: dict[tuple[uuid.UUID, int], uuid.UUID] = {}
    for net in schematic.nets.values():
        for ref in net.pins:
            pin_to_net[(ref.component_id, ref.pin_number)] = net.id

    # Nothing to enforce if the schematic has no nets — skip the expensive
    # ConnectivityGraph rebuild entirely.  Useful for fixture-free unit tests.
    if not pin_to_net:

        def _trivially_ok(_proposed: list[list[Wire]]) -> bool:
            return True

        return _trivially_ok

    # ── Compute baseline invariants once.  All comparisons are against
    # these numbers; the checker is *monotonic* with respect to them. ──

    baseline_frag: dict[uuid.UUID, int] = (
        {}
    )  # net_id → number of disjoint electric sets
    baseline_connected_pins: dict[uuid.UUID, set[tuple[uuid.UUID, int]]] = (
        {}
    )  # net_id → pins with key != None
    baseline_short_pairs: set[tuple[NodeKey, frozenset[uuid.UUID]]] = (
        set()
    )  # (set_key, frozenset(net_ids))
    baseline_same_side_shorts = 0

    try:
        _baseline_graph = ConnectivityGraph(
            board=base_board,
            layout=base_layout,
            components=base_components,
            instances=base_instances,
        )
    except Exception:
        # If even the baseline graph can't be built, fall back to the legacy
        # absolute checker — nothing useful can be said monotonically.
        _baseline_graph = None

    if _baseline_graph is not None:
        for net in schematic.nets.values():
            keys: set[NodeKey] = set()
            connected: set[tuple[uuid.UUID, int]] = set()
            for ref in net.pins:
                k = _baseline_graph.node_for_pin(ref)
                if k is not None:
                    keys.add(k)
                    connected.add((ref.component_id, ref.pin_number))
            baseline_frag[net.id] = len(keys)
            baseline_connected_pins[net.id] = connected

        key_to_nets_base: dict[NodeKey, set[uuid.UUID]] = defaultdict(set)
        for (cid, pn), nid in pin_to_net.items():
            k = _baseline_graph.node_for_pin(PinRef(cid, pn))
            if k is not None:
                key_to_nets_base[k].add(nid)
        for k, nids in key_to_nets_base.items():
            if len(nids) > 1:
                baseline_short_pairs.add((k, frozenset(nids)))

        if check_bare_same_side:
            baseline_same_side_shorts = _count_bare_same_side_shorts(
                base_layout.wires,
                _baseline_graph,
            )

    def _check(proposed: list[list[Wire]]) -> bool:
        new_layout = Layout()
        new_layout.placements = base_layout.placements
        new_layout.groups = base_layout.groups
        # Baseline = every wire from the original layout that is NOT being
        # replaced by `proposed`.  This keeps unrelated nets connected so
        # the checker doesn't fail on wires we never touched.
        new_wires: list[Wire] = []
        if target_ids is not None:
            for w in base_layout.wires:
                if w.id not in target_ids:
                    new_wires.append(w)
        for chain in proposed:
            new_wires.extend(chain)
        new_layout.wires = WireCollection(new_wires)
        try:
            new_graph = ConnectivityGraph(
                board=base_board,
                layout=new_layout,
                components=base_components,
                instances=base_instances,
            )
        except Exception:
            return False

        # Invariant (a): per-net fragmentation count must not grow, and
        # no previously-connected pin may become disconnected.
        for net in schematic.nets.values():
            keys: set[NodeKey] = set()
            connected: set[tuple[uuid.UUID, int]] = set()
            for ref in net.pins:
                k = new_graph.node_for_pin(ref)
                if k is not None:
                    keys.add(k)
                    connected.add((ref.component_id, ref.pin_number))
            base_n = baseline_frag.get(net.id, 0)
            if len(keys) > max(base_n, 1):
                # Worse than baseline.  Note: ``max(base_n, 1)`` — a net
                # that was fully connected (1 key) stays at 1; a net that
                # was 2-way fragmented may stay 2-way but not grow to 3.
                return False
            base_connected = baseline_connected_pins.get(net.id, set())
            if not base_connected.issubset(connected):
                # Lost a pin that previously had connectivity.
                return False

        # Invariant (b): cross-net shorts may stay, but no new ones may
        # appear.  Compare as (set_key, frozenset(net_ids)) pairs.
        key_to_nets: dict[NodeKey, set[uuid.UUID]] = defaultdict(set)
        for (cid, pn), nid in pin_to_net.items():
            k = new_graph.node_for_pin(PinRef(cid, pn))
            if k is not None:
                key_to_nets[k].add(nid)
        new_short_pairs: set[tuple[NodeKey, frozenset[uuid.UUID]]] = set()
        for k, nids in key_to_nets.items():
            if len(nids) > 1:
                new_short_pairs.add((k, frozenset(nids)))
        if not new_short_pairs.issubset(baseline_short_pairs):
            return False

        # Invariant (c): bare wires on the same side belonging to different
        # electric sets must not cross at an off-pad interior point.
        # Monotonic — proposed may not exceed the baseline count.
        if check_bare_same_side:
            new_count = _count_bare_same_side_shorts(
                new_wires,
                new_graph,
            )
            if new_count > baseline_same_side_shorts:
                return False

        return True

    return _check


def count_mutual_crossings(wires: list[Wire]) -> int:
    """Total crossings between every pair of wires in *wires*."""
    total = 0
    for i in range(len(wires)):
        for j in range(i + 1, len(wires)):
            total += len(find_crossings(wires[i], wires[j]))
    return total
