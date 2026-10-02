# SPDX-License-Identifier: MIT
"""Board pad lookup and pad-node enrichment of wire geometry."""

from __future__ import annotations

import math
from collections.abc import Collection
from typing import TYPE_CHECKING

from protoboard.domain.layout.wire import Fragment, Wire
from protoboard.geometry import Point
from protoboard.instrumentation.call_counters import counted

if TYPE_CHECKING:
    from protoboard.domain.board.board import Board


def board_pads_for_side(board: Board, side: str) -> set[Point]:
    """Return all pad positions accessible on *side* ("top" or "bottom").

    Includes:
    - PadAtom pads whose layers include *side* or are plated-through.
    - Individual holes of StripAtom strips whose layers include *side* or
      are plated-through.

    Result cached per (board, side) — board topology is immutable across
    a session so the full walk only runs once.
    """
    return board.pad_positions_for_side(side)


def point_on_pads(
    point: Point,
    pads: Collection[Point],
    tol: float = 1e-6,
) -> bool:
    """Return whether *point* matches a pad exactly or within *tol*."""
    if point in pads:
        return True
    return any(abs(p.x - point.x) < tol and abs(p.y - point.y) < tol for p in pads)


def board_pads_union(board: Board) -> set[Point]:
    """Return the union of all pad positions across both sides.

    Cached on the board — repeated calls return the **same** set
    instance so callers that build per-set spatial indices (e.g.
    :func:`insert_pad_nodes`) can identity-cache their results.  The
    set is mutable (callers must not modify it) but the contents stay
    stable for the board's lifetime.
    """
    return board.pad_positions_union()


def plated_through_pads(board: Board) -> set[Point]:
    """Return positions of all plated-through pads on the board.

    Cached on the board (see :func:`board_pads_for_side`).
    """
    return board.plated_through_positions()


_PAD_BUCKET_CACHE: dict[
    int,
    tuple[int, object | None, tuple[dict[int, list[Point]], dict[int, list[Point]]]],
] = {}  # noqa: E501


@counted("_get_pad_bucket_index")
def _get_pad_bucket_index(
    pad_positions: set[Point],
) -> tuple[dict[int, list[Point]], dict[int, list[Point]]]:
    """Return (pads_by_x, pads_by_y) buckets for *pad_positions*.

    Identity-cached on ``id(pad_positions)`` together with the current
    ``len(pad_positions)``.  The production callsite always passes a
    set returned by ``board_pads_union`` / ``board_pads_for_side`` —
    those return the **same** cached instance for the board lifetime,
    so successive calls hit a single dict lookup instead of rebuilding
    a ~5000-entry bucket twice per invocation.  Profiler put 144s of
    own time in this function before the cache; insert_pad_nodes was
    the dominant consumer through ``_enrich_bare_path``.

    Length is part of the cache key as a cheap mutation guard — if a
    test (or production code) reuses the same set object after editing
    it, the size will have changed and we rebuild.

    A weakref guard catches address recycling: when the cached
    pad_positions set is garbage-collected, ``id()`` of a freshly
    allocated set can land on the same slot.  Without the weakref,
    an unrelated board's pad set inherits the recycled id and silently
    reads stale buckets (test_connectivity flake 2026-06-09).
    """
    import weakref as _wr

    key = id(pad_positions)
    n = len(pad_positions)
    cached = _PAD_BUCKET_CACHE.get(key)
    if cached is not None and cached[0] == n:
        ref = cached[1]
        # ref is either a weakref.ref or None (set wasn't weak-referenceable).
        # Stale cache when the weakref no longer resolves to the same object.
        if ref is None:
            return cached[2]
        if isinstance(ref, _wr.ReferenceType) and ref() is pad_positions:
            return cached[2]
    try:
        wr_obj: object | None = _wr.ref(pad_positions)
    except TypeError:
        wr_obj = None
    return _build_pad_bucket_index(pad_positions, key, n, wr_obj)


@counted("_build_pad_bucket_index")
def _build_pad_bucket_index(
    pad_positions: set[Point],
    key: int,
    n: int,
    wr_obj: object | None = None,
) -> tuple[dict[int, list[Point]], dict[int, list[Point]]]:
    """The cache-miss path of :func:`_get_pad_bucket_index`.

    Split out so that ``@counted`` can record only the actual bucket
    rebuilds — the cache-hit path returns without invoking this
    function at all, giving the call counter a clean signal for fix
    #1.  Production should see ``_build_pad_bucket_index`` fire once
    per unique ``id(pad_positions)`` and never again until the
    underlying set instance is replaced.
    """
    pads_by_x: dict[int, list[Point]] = {}
    pads_by_y: dict[int, list[Point]] = {}
    for pad in pad_positions:
        pads_by_x.setdefault(round(pad.x), []).append(pad)
        pads_by_y.setdefault(round(pad.y), []).append(pad)
    _PAD_BUCKET_CACHE[key] = (n, wr_obj, (pads_by_x, pads_by_y))
    return (pads_by_x, pads_by_y)


def clear_pad_bucket_cache() -> None:
    """Drop the identity cache used by :func:`_get_pad_bucket_index`.

    Production code never needs to call this (pad set instances live
    as long as the board), but tests that rotate through several
    short-lived sets reusing similar ids can hit a stale entry.
    """
    _PAD_BUCKET_CACHE.clear()


@counted("insert_pad_nodes")
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

    Fast path for ortho (axis-aligned) segments: when a segment has dx=0
    or dy=0, only pads sharing the same fixed coordinate can lie on it.
    We pre-bucket pads by (rounded-x → list[pad]) and (rounded-y →
    list[pad]) so each ortho segment scans O(pads-on-line) instead of
    O(total-pads).  Diagonal segments (rare in practice — only A* with
    bare_diagonal mode) fall back to the full O(pads) collinearity
    scan.
    """
    if len(path) < 2:
        return list(path)

    pads_by_x, pads_by_y = _get_pad_bucket_index(pad_positions)

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

        # Pick candidate set: ortho segments check only pads on the
        # fixed-coordinate line; diagonal/full-scan fallback otherwise.
        is_horizontal = abs(aby) < tolerance
        is_vertical = abs(abx) < tolerance
        if is_horizontal:
            candidates = pads_by_y.get(round(p1.y), [])
        elif is_vertical:
            candidates = pads_by_x.get(round(p1.x), [])
        else:
            candidates = list(pad_positions)

        for pad in candidates:
            # Already an endpoint — skip
            if (abs(pad.x - p1.x) < tolerance and abs(pad.y - p1.y) < tolerance) or (
                abs(pad.x - p2.x) < tolerance and abs(pad.y - p2.y) < tolerance
            ):
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


def enrich_insulated_wire_with_pad_nodes(wire: Wire, board: Board) -> Wire:
    """Insert board-pad positions into the path of an *insulated* wire.

    An insulated wire that crosses a pad geometrically between two of its
    recorded vertices has no "handle" at that pad — simplify, unweave and
    interactive edits cannot manipulate the path there.  Inserting an
    explicit pad-vertex at each crossed pad gives downstream tools the
    nodes they need.  Idempotent — pads already on the path are not
    duplicated.

    Bare wires are returned unchanged — they're produced with pad-vertices
    by their callers (RouteTool's ``insert_pad_nodes`` enrichment, A*
    step-by-step paths) and the bare-diagonal parity model relies on the
    caller-controlled vertex set.

    A new ``Wire`` is returned only when the path actually grows;
    otherwise the input is returned as-is.  The wire's id is preserved
    so callers comparing by identity stay consistent.
    """
    if not wire.insulated:
        return wire
    # Insulated wires sit on one side; their endpoints solder to that side's
    # pads.  Pads from BOTH sides are relevant for vertex insertion because
    # the wire's geometric path may pass over a pad that physically exists
    # on either side (plated-through pads in particular).
    pads = board_pads_union(board)
    new_path = insert_pad_nodes(list(wire.path), pads)
    if len(new_path) == len(wire.path):
        return wire
    return Wire(
        id=wire.id,
        path=new_path,
        insulated=wire.insulated,
        side=wire.side,
        color=wire.color,
        a=wire.a,
        b=wire.b,
    )


def enrich_insulated_fragments_with_pad_nodes(
    fragments: tuple[Fragment, ...],
    is_insulated: bool,
    board: Board,
) -> tuple[Fragment, ...]:
    """Pad-node enrichment for fragment tuples (ReplaceWireFragmentsCommand).

    Mirrors :func:`enrich_insulated_wire_with_pad_nodes` but operates on the
    ``fragments`` tuple consumed by :class:`ReplaceWireFragmentsCommand`.
    Without this, hand-edited insulated wires (committed via [E] → Enter
    or merge-wires) reach the layout with raw user-clicked vertices only,
    so ``PadIndex._build_wires`` never records ``InsulatedWireCrossing``
    items for intermediate pad crossings.  Simplify then reads zero pad
    contacts on the baseline and is free to swap an off-pad diagonal for
    an on-pad ortho L-bend that adds new crossings.  Bug surfaced as
    "simplify increased on-pad-crossings after manual wire edit" and only
    disappeared after save/load, because ``load_project`` runs the same
    enrichment.

    Bare wires are returned unchanged — they get their pad-vertices from
    their callers (autoroute / RouteTool / pad-bucketing in PadIndex's
    mid-segment crossing detection).
    """
    if not is_insulated or not fragments:
        return fragments
    pads = board_pads_union(board)
    changed = False
    new_fragments: list[Fragment] = []
    for frag in fragments:
        new_path = insert_pad_nodes(list(frag.path), pads)
        if len(new_path) == len(frag.path):
            new_fragments.append(frag)
            continue
        changed = True
        new_fragments.append(Fragment(path=new_path, side=frag.side))
    return tuple(new_fragments) if changed else fragments
