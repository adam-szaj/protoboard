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

import structlog

_log = structlog.get_logger(__name__)

from protoboard.domain.layout.wire import Wire  # noqa: E402
from protoboard.domain.layout.wire_utils import insert_pad_nodes  # noqa: E402
from protoboard.domain.routing.simplify_mode import SimplifyMode  # noqa: E402
from protoboard.geometry import (  # noqa: E402
    Point,
    Vector,
    segments_cross_strict,
)
from protoboard.services.routing.astar import build_bare_seg_index  # noqa: E402
from protoboard.services.routing.path_safety import _EPS as _EPS  # noqa: E402
from protoboard.services.routing.path_safety import (  # noqa: E402
    _SIMPLIFY_TRACE as _SIMPLIFY_TRACE,
)
from protoboard.services.routing.path_safety import (  # noqa: E402
    _count_crossings_with_other as _count_crossings_with_other,
)
from protoboard.services.routing.path_safety import (  # noqa: E402
    _count_insulated_pad_contacts as _count_insulated_pad_contacts,
)
from protoboard.services.routing.path_safety import (  # noqa: E402
    _seg_passes_through_pad as _seg_passes_through_pad,
)
from protoboard.services.routing.path_safety import (  # noqa: E402
    _segs_cross_or_touch as _segs_cross_or_touch,
)
from protoboard.services.routing.path_safety import (  # noqa: E402
    _segs_overlap_collinear as _segs_overlap_collinear,
)
from protoboard.services.routing.path_safety import (  # noqa: E402
    _wire_to_segs as _wire_to_segs,
)
from protoboard.services.routing.path_safety import (  # noqa: E402
    candidate_no_new_pin_contact as _candidate_no_new_pin_contact,
)
from protoboard.services.routing.path_safety import (  # noqa: E402
    candidate_pins_alien as _candidate_pins_alien,
)
from protoboard.services.routing.path_safety import (  # noqa: E402
    candidate_safe_against_others as _candidate_safe_against_others,
)
from protoboard.services.routing.path_safety import (  # noqa: E402
    candidate_safe_against_own as _candidate_safe_against_own,
)
from protoboard.services.routing.path_safety import (  # noqa: E402
    candidate_safe_via_pad_index as _candidate_safe_via_pad_index,
)
from protoboard.services.routing.path_safety import (  # noqa: E402
    new_alien_pad_contact as _new_alien_pad_contact,
)
from protoboard.services.routing.path_safety import (  # noqa: E402
    parity_ok_for_bare as _parity_ok_for_bare,
)

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
    return (int(math.floor(p.x)) + int(math.floor(p.y))) % 2


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
    bare_idx = build_bare_seg_index(other_segs)

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
                        if segments_cross_strict(a, c, _seg[0], _seg[1]):
                            safe = False
                            break
                    # And against own non-replaced segments.
                    if safe:
                        for src, dst, _side in own_segs:
                            # Skip the two segments being replaced.
                            if (src, dst) in ((a, b), (b, c)):
                                continue
                            if segments_cross_strict(a, c, src, dst):
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
        side=wire.side,
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
    or pure diag (|dx| == |dy|), as produced by ``canonical_subpath``.
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
    enriched = insert_pad_nodes([a, b], board_pads)
    return enriched[1:-1]


def canonical_subpath(
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
      - No crossing with other bare wires (strict segment crossing).
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
                canon = canonical_subpath(path[i], path[j], allow_l_shape=allow_l_shape)
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
        side=wire.side,
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
