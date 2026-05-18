# SPDX-License-Identifier: MIT
"""Wire path utilities — pad-node insertion and board pad enumeration."""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import TYPE_CHECKING

from protoboard.domain.board.boxes import PadAtom, StripAtom
from protoboard.geometry import Point

if TYPE_CHECKING:
    import uuid

    from protoboard.domain.board.board import Board
    from protoboard.domain.layout.wire import WireSegment


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


def wire_has_skewed_segment(wire: WireSegment, tol: float = 1e-6) -> bool:
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
    wires: Iterable[WireSegment],
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
    "wire_has_skewed_segment",
    "find_skewed_wires",
]
