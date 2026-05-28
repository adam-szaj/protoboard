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
    from protoboard.domain.layout.knot import JointKnot
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


def find_wire_with_interior_vertex_at(
    layout: "Layout", point: Point, *, tol: float = 1e-6,
) -> "tuple[Wire, int] | None":
    """Return ``(wire, vertex_idx)`` if any wire passes ``point`` at an
    interior (non-endpoint) vertex; otherwise ``None``.

    Used by ``AddWireCommand`` and load/save migration to detect the
    "new wire ends on existing wire mid-path" case and trigger a
    split.  Interior = vertex index in ``[1, len(path)-2]`` (excludes
    both endpoints which are handled by ``find_joint_at`` / pin
    lookup instead).
    """
    for w in layout.wires:
        for i in range(1, len(w.path) - 1):
            v = w.path[i]
            if abs(v.x - point.x) < tol and abs(v.y - point.y) < tol:
                return w, i
    return None


def split_wire_at_vertex(
    layout: "Layout",
    wire: "Wire",
    vertex_idx: int,
) -> "tuple[Wire, Wire, JointKnot]":
    """Split ``wire`` into two wires at ``wire.path[vertex_idx]``.

    The vertex becomes a shared ``JointKnot`` (created here); both
    halves keep the original ``insulated`` and per-fragment ``side``
    decomposition (so a multi-fragment wire crossing a via on either
    side of the split still has its boundaries preserved).  The first
    half keeps the original wire id; the second half gets a fresh id.

    Returns ``(wire_a, wire_b, shared_joint)`` — both wires already
    inserted into ``layout.wires``, the joint into ``layout.knots``.
    The original ``wire`` is removed from layout.

    Caller is responsible for any net assignment / DRC follow-up — this
    function only reshapes geometry + knot back-refs.
    """
    from protoboard.domain.layout.knot import JointKnot
    from protoboard.domain.layout.wire import Fragment, KnotRef, Wire
    if vertex_idx <= 0 or vertex_idx >= len(wire.path) - 1:
        raise ValueError(
            f"split_wire_at_vertex: vertex_idx={vertex_idx} not interior "
            f"(path len={len(wire.path)})"
        )
    if wire.a is None or wire.b is None:
        raise ValueError("split_wire_at_vertex: wire has no endpoint knots")

    split_point = wire.path[vertex_idx]

    # Split each fragment list along ``vertex_idx`` measured in path
    # coordinates.  Walk fragments accumulating their lengths until we
    # bracket the split.
    fragments_a: list[Fragment] = []
    fragments_b: list[Fragment] = []
    cursor = 0  # current global index of fragments[k].path[0]
    split_done = False
    for frag in wire.fragments:
        frag_len = len(frag.path)
        # Range of global indices this fragment covers: [cursor, cursor + frag_len - 1]
        frag_end_idx = cursor + frag_len - 1
        if split_done:
            fragments_b.append(Fragment(path=list(frag.path), side=frag.side))
        elif cursor <= vertex_idx <= frag_end_idx:
            local_idx = vertex_idx - cursor
            # Slice: [0..local_idx] → A, [local_idx..end] → B (share boundary vertex).
            if local_idx > 0:
                fragments_a.append(Fragment(
                    path=list(frag.path[: local_idx + 1]), side=frag.side,
                ))
            if local_idx < frag_len - 1:
                fragments_b.append(Fragment(
                    path=list(frag.path[local_idx:]), side=frag.side,
                ))
            split_done = True
        else:
            fragments_a.append(Fragment(path=list(frag.path), side=frag.side))
        # Advance cursor; next fragment shares its first vertex with this
        # fragment's last, so subtract 1.
        cursor += frag_len - 1
    if not split_done:
        raise ValueError(
            f"split_wire_at_vertex: vertex_idx={vertex_idx} not located in "
            f"wire fragments"
        )

    # Sanity — both halves must have at least one fragment with ≥2 points.
    if not fragments_a or len(fragments_a[-1].path) < 2:
        raise ValueError("split_wire_at_vertex: A half collapsed; refusing")
    if not fragments_b or len(fragments_b[0].path) < 2:
        raise ValueError("split_wire_at_vertex: B half collapsed; refusing")

    # Remove the original wire (this prunes orphan outer joints — we'll
    # recreate the references when adding the new wires).
    a_knot_id = wire.a.id
    b_knot_id = wire.b.id
    a_knot = layout.knots.get(a_knot_id)
    b_knot = layout.knots.get(b_knot_id)
    layout.remove_wire(wire.id)
    # ``remove_wire`` only prunes JointKnots that became orphan; PinKnots
    # survive regardless.  Revive any pruned outer joint at its original
    # position so we can re-attach the half.
    if a_knot_id not in layout.knots and isinstance(a_knot, JointKnot):
        layout.knots[a_knot_id] = JointKnot(
            id=a_knot_id, position=a_knot.position,
        )
    if b_knot_id not in layout.knots and isinstance(b_knot, JointKnot):
        layout.knots[b_knot_id] = JointKnot(
            id=b_knot_id, position=b_knot.position,
        )

    # Determine the hub at the split point.  Prefer an existing PinKnot
    # (a pin always wins identity over a joint at the same pad) or an
    # existing JointKnot at the position; only create a fresh JointKnot
    # when nothing's there.  Returning the hub for the public API.
    from protoboard.domain.layout.knot import PinKnot
    shared_hub: "PinKnot | JointKnot | None" = None
    for k in layout.knots.values():
        if isinstance(k, PinKnot):
            # PinKnot's world position requires the pin resolver; we
            # detect "PinKnot at split_point" by checking pin_knots
            # back-map below instead.
            continue
        if isinstance(k, JointKnot):
            if (
                abs(k.position.x - split_point.x) < 1e-6
                and abs(k.position.y - split_point.y) < 1e-6
            ):
                shared_hub = k
                break
    # PinKnot lookup: scan pin_knots → resolver via layout._pin_resolver
    # if available.  Falls back to "no pin found" when resolver isn't
    # set (tests / migration path without a library).
    if shared_hub is None and getattr(layout, "_pin_resolver", None) is not None:
        for pin_ref, knot_id in layout.pin_knots.items():
            try:
                pin_pos = layout._pin_resolver(pin_ref)  # type: ignore[attr-defined]
            except Exception:
                continue
            if (
                abs(pin_pos.x - split_point.x) < 1e-6
                and abs(pin_pos.y - split_point.y) < 1e-6
            ):
                pk = layout.knots.get(knot_id)
                if isinstance(pk, PinKnot):
                    shared_hub = pk
                    break
    if shared_hub is None:
        shared_hub = layout.create_joint_knot(split_point)

    # Build A (original id) and B (fresh id).
    flat_a = _flatten_fragments(fragments_a)
    flat_b = _flatten_fragments(fragments_b)
    wire_a = Wire(
        id=wire.id,
        path=flat_a,
        insulated=wire.insulated,
        side=fragments_a[0].side,
        color=wire.color,
        fragments=fragments_a,
        a=KnotRef(id=a_knot_id),
        b=KnotRef(id=shared_hub.id),
    )
    wire_b = Wire(
        id=uuid.uuid4(),
        path=flat_b,
        insulated=wire.insulated,
        side=fragments_b[0].side,
        color=wire.color,
        fragments=fragments_b,
        a=KnotRef(id=shared_hub.id),
        b=KnotRef(id=b_knot_id),
    )
    layout.add_wire(wire_a)
    layout.add_wire(wire_b)
    return wire_a, wire_b, shared_hub  # type: ignore[return-value]


def _flatten_fragments(fragments: "list[Fragment]") -> list[Point]:
    """Concatenate fragments into a single path; duplicate via vertex dropped."""
    if not fragments:
        return []
    out: list[Point] = list(fragments[0].path)
    for frag in fragments[1:]:
        out.extend(frag.path[1:])
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


def make_t_junction(
    layout: "Layout",
    w_through: "Wire",
    vertex_idx: int,
    w_incoming: "Wire",
    incoming_endpoint: "str",  # "a" or "b" — which end of w_incoming hits the vertex
) -> "tuple[Wire, Wire, Wire, JointKnot | object]":
    """Atomically build a T-junction: split ``w_through`` at
    ``vertex_idx`` and re-anchor ``w_incoming``'s matching endpoint to
    the same hub in one step.

    Atomicity matters: a two-step "split, then merge" pipeline passes
    through an intermediate state where the new shared hub has
    ``degree=2`` and ``merge_endpoint_wires`` happily collapses it
    back, producing a livelock.  ``make_t_junction`` skips that
    intermediate state — when control returns, the hub already has
    ``wire_ids = {w_a.id, w_b.id, w_incoming.id}`` (degree-3) and
    nothing downstream sees a fusable degree-2 joint.

    The hub is an existing ``PinKnot`` when ``w_through.path[vertex_idx]``
    sits on a pin; otherwise a freshly created (or reused) ``JointKnot``
    at the split position.  ``w_incoming``'s old endpoint knot is
    dropped if it becomes orphan, kept otherwise.

    Returns ``(w_a, w_b, w_incoming_updated, hub)``.  ``w_a`` keeps the
    original ``w_through.id``; ``w_b`` gets a fresh id (matching
    ``split_wire_at_vertex``).
    """
    from protoboard.domain.layout.knot import JointKnot
    from protoboard.domain.layout.wire import KnotRef
    if incoming_endpoint not in ("a", "b"):
        raise ValueError(f"incoming_endpoint must be 'a' or 'b'; got {incoming_endpoint!r}")
    # Remember w_incoming's old endpoint knot id so we can prune it if
    # it became orphan (degree=0 JointKnot) after the swap.
    old_ref = w_incoming.a if incoming_endpoint == "a" else w_incoming.b
    if old_ref is None:
        raise ValueError("w_incoming has no endpoint to re-anchor")
    old_endpoint_id = old_ref.id

    # Step 1 — split w_through.  This produces the hub already at the
    # right position (PinKnot reuse / fresh JointKnot).
    w_a, w_b, hub = split_wire_at_vertex(layout, w_through, vertex_idx)
    hub_id = hub.id  # type: ignore[union-attr]

    # Step 2 — re-anchor w_incoming's endpoint to the hub.  Decrement
    # the old knot's wire_ids in place; add w_incoming to the hub.
    if incoming_endpoint == "a":
        w_incoming.a = KnotRef(id=hub_id)
    else:
        w_incoming.b = KnotRef(id=hub_id)
    # Update hub back-refs.
    current_hub = layout.knots.get(hub_id)
    if current_hub is not None:
        new_wire_ids = current_hub.wire_ids | {w_incoming.id}
        # Rebuild knot with updated wire_ids (knots are mutable but
        # wire_ids is a frozenset — replace the whole knot).
        if isinstance(current_hub, JointKnot):
            layout.knots[hub_id] = JointKnot(
                id=current_hub.id,
                position=current_hub.position,
                wire_ids=new_wire_ids,
            )
        else:
            # PinKnot
            from protoboard.domain.layout.knot import PinKnot
            layout.knots[hub_id] = PinKnot(
                id=current_hub.id,
                pin=current_hub.pin,  # type: ignore[attr-defined]
                wire_ids=new_wire_ids,
            )
    # Update old knot: remove w_incoming from its back-refs.  Skip
    # when the old knot IS the hub — that's the case where W1 already
    # referenced the same knot the split happened to reuse, so the
    # "add to hub / remove from old" cancel out and would leave the
    # hub at degree-2 (the bug we're explicitly trying to avoid).
    if old_endpoint_id == hub_id:
        return w_a, w_b, w_incoming, hub
    old_knot = layout.knots.get(old_endpoint_id)
    if old_knot is not None:
        updated_old_ids = old_knot.wire_ids - {w_incoming.id}
        if isinstance(old_knot, JointKnot):
            if updated_old_ids:
                layout.knots[old_endpoint_id] = JointKnot(
                    id=old_knot.id,
                    position=old_knot.position,
                    wire_ids=updated_old_ids,
                )
            else:
                # Orphan joint — drop.
                del layout.knots[old_endpoint_id]
        else:
            # PinKnot — never drop, just update wire_ids.
            from protoboard.domain.layout.knot import PinKnot
            layout.knots[old_endpoint_id] = PinKnot(
                id=old_knot.id,
                pin=old_knot.pin,  # type: ignore[attr-defined]
                wire_ids=updated_old_ids,
            )
    return w_a, w_b, w_incoming, hub


def split_wires_at_endpoint_collisions(layout: "Layout") -> int:
    """Find every wire endpoint that sits on another wire's interior
    vertex and atomically build a T-junction there.

    A T-junction normally lives as: ``W1`` ending at ``W2.path[k]`` for
    some interior ``k``.  Without a shared hub, ``W2`` has no JointKnot
    at the meeting pad and the user sees a "three-way connection" that
    the underlying model can't acknowledge.

    Each fix uses ``make_t_junction`` which splits W2 AND retargets W1's
    endpoint to the shared hub in one step — the hub never passes
    through a degree-2 intermediate state, so ``merge_endpoint_wires``
    can't undo the split (which is what produced an infinite split↔merge
    livelock with the two-step approach).

    Returns the number of T-junctions created.  Idempotent — running
    again on a clean layout returns 0.
    """
    fixes = 0
    progress = True
    while progress:
        progress = False
        candidates = list(layout.wires)
        for w1 in candidates:
            if layout.wires.get(w1.id) is None:
                continue
            for endpoint_name, ep in (("a", w1.path[0]), ("b", w1.path[-1])):
                hit = find_wire_with_interior_vertex_at(layout, ep)
                if hit is None:
                    continue
                w2, vertex_idx = hit
                if w2.id == w1.id:
                    continue
                try:
                    make_t_junction(layout, w2, vertex_idx, w1, endpoint_name)
                except ValueError:
                    continue
                fixes += 1
                progress = True
                break
            if progress:
                break
    return fixes


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
    "find_wire_with_interior_vertex_at",
    "split_wire_at_vertex",
    "merge_colocated_joints",
    "merge_endpoint_wires",
    "split_wires_at_endpoint_collisions",
    "wire_has_skewed_segment",
    "find_skewed_wires",
]
