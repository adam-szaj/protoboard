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

import uuid
from typing import cast, Literal

from protoboard.domain.layout.wire import WireSegment
from protoboard.geometry import Point
from protoboard.services.routing.astar import (
    _build_bare_seg_index,
    _segs_cross,
)


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
