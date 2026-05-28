# SPDX-License-Identifier: MIT
"""Wire path utilities — pad-node insertion and board pad enumeration."""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterable
from typing import TYPE_CHECKING

from protoboard.domain.board.boxes import PadAtom, StripAtom
from protoboard.geometry import Point

if TYPE_CHECKING:
    import uuid

    from protoboard.domain.board.board import Board
    from protoboard.domain.layout.layout import Layout
    from protoboard.domain.layout.wire import Fragment, Wire


def board_pads_for_side(board: Board, side: str) -> set[Point]:
    """Return all pad positions accessible on *side* ("top" or "bottom").

    Includes:
    - PadAtom pads whose layers include *side* or are plated-through.
    - Individual holes of StripAtom strips whose layers include *side* or
      are plated-through.
    """
    pts: set[Point] = set()
    for atom, center in board.atoms():
        if isinstance(atom, PadAtom):
            layers = atom.layers
            if layers.plated_through or \
               (side == "top" and layers.top) or \
               (side == "bottom" and layers.bottom):
                pts.add(center)
        elif isinstance(atom, StripAtom):
            layers = atom.layers
            if layers.plated_through or \
               (side == "top" and layers.top) or \
               (side == "bottom" and layers.bottom):
                n = int(atom.length) if isinstance(atom.length, int) else 1
                if atom.orientation == "horizontal":
                    sx = center.x - n / 2.0 + 0.5
                    for i in range(n):
                        pts.add(Point(sx + i, center.y))
                else:
                    sy = center.y - n / 2.0 + 0.5
                    for i in range(n):
                        pts.add(Point(center.x, sy + i))
    return pts


def plated_through_pads(board: Board) -> set[Point]:
    """Return positions of all plated-through pads on the board."""
    pts: set[Point] = set()
    for atom, center in board.atoms():
        if isinstance(atom, PadAtom) and atom.layers.plated_through:
            pts.add(center)
        elif isinstance(atom, StripAtom) and atom.layers.plated_through:
            n = int(atom.length) if isinstance(atom.length, int) else 1
            if atom.orientation == "horizontal":
                sx = center.x - n / 2.0 + 0.5
                for i in range(n):
                    pts.add(Point(sx + i, center.y))
            else:
                sy = center.y - n / 2.0 + 0.5
                for i in range(n):
                    pts.add(Point(center.x, sy + i))
    return pts


def insert_pad_nodes(
    path: list[Point],
    pad_positions: set[Point],
    tolerance: float = 1e-6,
) -> list[Point]:
    """Return a new path with any pad positions that lie on segments inserted.

    For each consecutive pair (p1, p2), finds all pads from *pad_positions*
    that lie strictly between p1 and p2 on the segment (collinear and within
    the half-open interval, exclusive of endpoints already in path).  Inserts
    them sorted by distance from p1.

    Never modifies the input list.  Points already present in *path* are never
    duplicated.
    """
    if len(path) < 2:
        return list(path)

    result: list[Point] = []
    for i in range(len(path) - 1):
        p1 = path[i]
        p2 = path[i + 1]
        result.append(p1)

        # Find pads strictly interior to segment p1→p2
        interior: list[tuple[float, Point]] = []
        abx = p2.x - p1.x
        aby = p2.y - p1.y
        ab_sq = abx * abx + aby * aby
        if ab_sq < tolerance * tolerance:
            # Degenerate segment (zero length) — skip
            continue
        for pad in pad_positions:
            # Already an endpoint — skip
            if (abs(pad.x - p1.x) < tolerance and abs(pad.y - p1.y) < tolerance) or \
               (abs(pad.x - p2.x) < tolerance and abs(pad.y - p2.y) < tolerance):
                continue
            apx = pad.x - p1.x
            apy = pad.y - p1.y
            # Cross product magnitude — must be ~0 for collinearity
            cross = abs(apx * aby - apy * abx)
            if cross > tolerance * math.sqrt(ab_sq):
                continue
            # Dot product — must be in (0, ab_sq) for interior
            dot = apx * abx + apy * aby
            if dot <= tolerance or dot >= ab_sq - tolerance:
                continue
            interior.append((dot, pad))

        # Insert in order of distance from p1
        interior.sort(key=lambda x: x[0])
        for _, pad in interior:
            result.append(pad)

    result.append(path[-1])
    return result


def rebuild_fragments_from_path(
    wire: "Wire", new_path: list[Point],
) -> "list[Fragment]":
    """Split ``new_path`` along the existing fragment boundaries of ``wire``.

    Path-editing tools (simplify, segment-edit, drag-commit) reshape a
    wire's path without crossing any of its vias.  Every via vertex of
    ``wire`` survives in ``new_path``; this walks ``new_path`` once,
    splitting at each original boundary to recover the per-fragment side
    assignment.  Single-fragment wires collapse trivially.

    If a boundary cannot be located (shouldn't happen for legal edits),
    we fall back to a single-fragment wire on the primary side.
    """
    from protoboard.domain.layout.wire import Fragment as _Fragment
    if len(wire.fragments) <= 1:
        return [_Fragment(path=list(new_path), side=wire.side)]
    boundary_positions: list[Point] = [
        frag.path[-1] for frag in wire.fragments[:-1]
    ]
    out: list[_Fragment] = []
    cursor = 0
    for boundary, frag in zip(boundary_positions, wire.fragments):
        idx = None
        for i in range(cursor, len(new_path)):
            if new_path[i] == boundary:
                idx = i
                break
        if idx is None:
            return [_Fragment(path=list(new_path), side=wire.side)]
        out.append(_Fragment(path=list(new_path[cursor : idx + 1]), side=frag.side))
        cursor = idx
    out.append(_Fragment(path=list(new_path[cursor:]), side=wire.fragments[-1].side))
    return out


def coalesce_same_side_fragments(
    fragments: list["Fragment"] | tuple["Fragment", ...],
) -> "list[Fragment]":
    """Fuse consecutive fragments that share ``side`` into a single fragment.

    Pure function — does not mutate the input.  Returns a new list whose
    consecutive entries always differ in ``side`` (so any remaining
    boundary marks a real via through a plated-through pad).  Single
    fragments and empty input pass through unchanged.

    Boundary vertex policy: when two fragments fuse, the boundary point
    (shared between ``prev.path[-1]`` and ``nxt.path[0]``) appears once
    in the fused path.

    This helper is intentionally pure and **never invoked automatically**
    by domain constructors or commit paths — callers (an explicit
    "merge fragments" action, a step inside unweave / simplify) decide
    when fusion is appropriate for their context.  ``insulated`` is a
    per-wire attribute, so it is *not* consulted here; this only fuses
    on ``side``.
    """
    from protoboard.domain.layout.wire import Fragment as _Fragment
    if not fragments:
        return []
    out: list[_Fragment] = [
        _Fragment(path=list(fragments[0].path), side=fragments[0].side)
    ]
    for nxt in fragments[1:]:
        prev = out[-1]
        if prev.side == nxt.side:
            # Share the boundary vertex — drop the duplicate.
            prev.path.extend(nxt.path[1:])
        else:
            out.append(_Fragment(path=list(nxt.path), side=nxt.side))
    return out


def merge_colocated_joints(layout: "Layout") -> int:
    """Fuse pairs of ``JointKnot``s sharing a position into one knot.

    Two wires laid through the same off-pin pad at different times may
    each carry their own ``JointKnot`` at that pad — the back-refs end
    up split (each joint has degree=1) and ``try_collapse_joint``
    therefore can't see the chain.  This pass coalesces such colocated
    joints: for every pad position with ≥2 joints, pick the
    lexicographically smallest id as the winner, retarget every wire
    that referenced a loser knot to the winner instead, then delete the
    loser knots.  Returns the number of loser knots that were dropped.
    Pure with respect to wire identity (paths/fragments untouched).
    """
    from collections import defaultdict
    from protoboard.domain.layout.knot import JointKnot
    from protoboard.domain.layout.wire import KnotRef as _KnotRef
    by_pos: dict[tuple[float, float], list[uuid.UUID]] = defaultdict(list)
    for knot in layout.knots.values():
        if isinstance(knot, JointKnot):
            key = (round(knot.position.x, 6), round(knot.position.y, 6))
            by_pos[key].append(knot.id)
    dropped = 0
    for _key, ids in by_pos.items():
        if len(ids) < 2:
            continue
        ids_sorted = sorted(ids, key=str)
        winner = ids_sorted[0]
        losers = ids_sorted[1:]
        winner_knot = layout.knots[winner]
        # Collect wire ids referenced by losers so we can retarget them
        # to the winner and accumulate their back-refs there.
        loser_wire_ids: set[uuid.UUID] = set()
        for loser_id in losers:
            loser_knot = layout.knots.get(loser_id)
            if not isinstance(loser_knot, JointKnot):
                continue
            loser_wire_ids |= loser_knot.wire_ids
        # Retarget wires.
        for wid in loser_wire_ids:
            w = layout.wires.get(wid)
            if w is None:
                continue
            if w.a is not None and w.a.id in losers:
                w.a = _KnotRef(id=winner)
            if w.b is not None and w.b.id in losers:
                w.b = _KnotRef(id=winner)
        # Move wire_ids onto the winner.
        layout.knots[winner] = JointKnot(
            id=winner,
            position=winner_knot.position,
            wire_ids=winner_knot.wire_ids | loser_wire_ids,
        )
        # Drop losers.
        for loser_id in losers:
            layout.knots.pop(loser_id, None)
            dropped += 1
    return dropped


def merge_endpoint_wires(layout: "Layout", board: "Board") -> int:
    """Fuse every pair of wires sharing a JointKnot with matching ``insulated``.

    Two passes:
    * Pass 1 — ``merge_colocated_joints``: coalesce JointKnots that
      occupy the same pad position so a shared hub exists wherever wires
      physically touch.
    * Pass 2 — iterate ``layout.knots`` and call
      ``Layout.try_collapse_joint`` on every ``JointKnot`` whose
      ``wire_ids`` has length 2 and whose two incident wires share
      ``insulated``.  Repeat until no more collapses succeed (a chain
      of three+ wires needs N-1 passes).

    Returns the total number of wires that disappeared because they
    fused into a neighbour.  Pure: only wire / knot identity changes —
    ``wire.path`` / ``wire.fragments`` only change to reflect the union
    of the source paths (multi-fragment Wire is produced for cross-side
    via collapses).
    """
    from protoboard.domain.layout.knot import JointKnot
    merge_colocated_joints(layout)
    pt_pads = frozenset(plated_through_pads(board))
    total_fused = 0
    progress = True
    while progress:
        progress = False
        # Snapshot ids — try_collapse_joint mutates the dict.
        for knot_id in list(layout.knots.keys()):
            knot = layout.knots.get(knot_id)
            if not isinstance(knot, JointKnot):
                continue
            if len(knot.wire_ids) != 2:
                continue
            w_ids = tuple(knot.wire_ids)
            w1 = layout.wires.get(w_ids[0])
            w2 = layout.wires.get(w_ids[1])
            if w1 is None or w2 is None:
                continue
            if w1.insulated != w2.insulated:
                continue
            merged = layout.try_collapse_joint(
                knot_id, plated_through_pads=pt_pads,
            )
            if merged is not None:
                total_fused += 1
                progress = True
    return total_fused


def wire_has_skewed_segment(wire: Wire, tol: float = 1e-6) -> bool:
    """Return True if any segment of *wire* is neither orthogonal nor diagonal.

    A segment from (x1,y1) to (x2,y2) is *orthogonal* iff dx==0 or dy==0, and
    *diagonal* iff |dx|==|dy|.  Anything else is "skewed" — a slope our
    routing/cost model cannot represent.  Used after drag to detect wires that
    became invalid because their endpoints were translated unevenly with the
    moved component.
    """
    pts = wire.path
    for i in range(len(pts) - 1):
        dx = pts[i + 1].x - pts[i].x
        dy = pts[i + 1].y - pts[i].y
        if abs(dx) < tol and abs(dy) < tol:
            continue  # zero-length segment — handled elsewhere
        if abs(dx) < tol or abs(dy) < tol:
            continue  # orthogonal
        if abs(abs(dx) - abs(dy)) < tol:
            continue  # diagonal
        return True
    return False


def find_skewed_wires(
    wires: Iterable[Wire],
    moved_wire_ids: set[uuid.UUID] | frozenset[uuid.UUID] | None = None,
    tol: float = 1e-6,
) -> list[uuid.UUID]:
    """Return IDs of wires with at least one skewed segment.

    If *moved_wire_ids* is given, only those wires are inspected — typical use
    after a component drag, where only wires that moved with the component need
    re-validation.
    """
    out: list[uuid.UUID] = []
    for w in wires:
        if moved_wire_ids is not None and w.id not in moved_wire_ids:
            continue
        if wire_has_skewed_segment(w, tol=tol):
            out.append(w.id)
    return out


__all__ = [
    "board_pads_for_side",
    "plated_through_pads",
    "insert_pad_nodes",
    "coalesce_same_side_fragments",
    "merge_colocated_joints",
    "merge_endpoint_wires",
    "wire_has_skewed_segment",
    "find_skewed_wires",
]
