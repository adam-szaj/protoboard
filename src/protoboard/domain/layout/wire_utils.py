# SPDX-License-Identifier: MIT
"""Wire path utilities — pad-node insertion and board pad enumeration."""

from __future__ import annotations

import math
import uuid
from collections import defaultdict
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

import structlog

from protoboard.domain.components.package.component_package import resolved_footprint
from protoboard.domain.components.package.pin_mapping import (
    meta_pin_for_package_pin,
)
from protoboard.domain.components.resolution import (
    iter_placed_components,
    resolve_instance_definition,
)
from protoboard.domain.layout.knot import JointKnot, PinKnot, WirePinKnot
from protoboard.domain.layout.pad_strategy import PadStrategy, Verdict
from protoboard.domain.layout.placement import (
    iter_footprint_pin_positions,
    pin_world_position,
)
from protoboard.domain.layout.wire import Fragment, KnotRef, Wire
from protoboard.domain.layout.wire import flatten_fragments as _flatten_fragments
from protoboard.domain.schematic.net import PinRef
from protoboard.geometry import Point, rounded_point
from protoboard.instrumentation.call_counters import counted
from protoboard.instrumentation.logging import is_debug_enabled

_log = structlog.get_logger(__name__)

if TYPE_CHECKING:
    import uuid

    from protoboard.domain.board.board import Board
    from protoboard.domain.board.pad_state import PadIndex
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.layout.layout import Layout
    from protoboard.domain.layout.placement import ComponentPlacement
    from protoboard.domain.layout.wire import WireCollection
    from protoboard.domain.schematic.net import PinRef
    from protoboard.domain.schematic.schematic import Schematic


class TJunctionVerdict(Enum):
    """Outcome of evaluating whether two wires may form a T-junction.

    A T-junction electrically merges *w_through* and *w_incoming* at the
    meeting vertex (the intermediate point of *w_through* becomes a
    shared :class:`JointKnot`).  Producing one between wires that should
    stay electrically separate is exactly the bug observed in
    *reference_big_project* — a chain of insulated crossings collapsed
    into one giant electric cluster spanning 18 nets.

    * ``SAFE`` — both endpoints resolve to compatible nets (matching, or
      one side genuinely net-less so the join doesn't bridge anything).
      The caller may invoke :func:`make_t_junction`.
    * ``ASSIGN_NET_TO_INCOMING_PIN`` — *w_incoming*'s endpoint is a pin
      with no net assignment; the caller should assign it the
      *w_through* net **before** materialising the T-junction.  Returned
      with the ``orphan_pin`` field populated.
    * ``CONFLICT_REJECT`` — both wires have explicit nets and they
      differ.  Caller must leave the geometry as a *crossing* (no
      shared hub).
    * ``INCOMPATIBLE_GEOMETRY`` — sides differ, or other physical reason
      a single hub is meaningless (a top-side wire and a bottom-side
      wire can't share a single solder joint without a via).
    """

    SAFE = "safe"
    ASSIGN_NET_TO_INCOMING_PIN = "assign_net_to_incoming_pin"
    CONFLICT_REJECT = "conflict_reject"
    INCOMPATIBLE_GEOMETRY = "incompatible_geometry"


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


def rebuild_fragments_from_path(
    wire: Wire,
    new_path: list[Point],
) -> list[Fragment]:
    """Split ``new_path`` along the existing fragment boundaries of ``wire``.

    Path-editing tools (simplify, segment-edit, drag-commit) reshape a
    wire's path without crossing any of its vias.  Every via vertex of
    ``wire`` survives in ``new_path``; this walks ``new_path`` once,
    splitting at each original boundary to recover the per-fragment side
    assignment.  Single-fragment wires collapse trivially.

    If a boundary cannot be located (shouldn't happen for legal edits),
    we fall back to a single-fragment wire on the primary side.
    """
    if len(wire.fragments) <= 1:
        return [Fragment(path=list(new_path), side=wire.side)]
    boundary_positions: list[Point] = [frag.path[-1] for frag in wire.fragments[:-1]]
    out: list[Fragment] = []
    cursor = 0
    for boundary, frag in zip(boundary_positions, wire.fragments):
        idx = None
        for i in range(cursor, len(new_path)):
            if new_path[i] == boundary:
                idx = i
                break
        if idx is None:
            return [Fragment(path=list(new_path), side=wire.side)]
        out.append(Fragment(path=list(new_path[cursor : idx + 1]), side=frag.side))
        cursor = idx
    out.append(Fragment(path=list(new_path[cursor:]), side=wire.fragments[-1].side))
    return out


def coalesce_same_side_fragments(
    fragments: list[Fragment] | tuple[Fragment, ...],
) -> list[Fragment]:
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
    if not fragments:
        return []
    out: list[Fragment] = [
        Fragment(path=list(fragments[0].path), side=fragments[0].side)
    ]
    for nxt in fragments[1:]:
        prev = out[-1]
        if prev.side == nxt.side:
            # Share the boundary vertex — drop the duplicate.
            prev.path.extend(nxt.path[1:])
        else:
            out.append(Fragment(path=list(nxt.path), side=nxt.side))
    return out


def find_wire_with_interior_vertex_at(
    layout: Layout,
    point: Point,
    *,
    tol: float = 1e-6,
) -> tuple[Wire, int] | None:
    """Return ``(wire, vertex_idx)`` if any wire passes ``point`` at an
    interior (non-endpoint) vertex; otherwise ``None``.

    Used by ``AddWireCommand`` and load/save migration to detect the
    "new wire ends on existing wire mid-path" case and trigger a
    split.  Interior = vertex index in ``[1, len(path)-2]`` (excludes
    both endpoints which are handled by ``find_joint_at`` / pin
    lookup instead).

    Fast path: uses ``Layout.interior_vertex_index()`` — a lazy bucket
    map from rounded ``(x, y)`` to candidate ``(wire_id, vertex_idx)``
    pairs.  Reduces the cost from O(W × path_len) per call to O(1)
    bucket lookup + a handful of float compares.  Falls back to a
    linear scan only when the index lookup misses (degenerate point or
    a wire mutated outside the invalidation path — should not happen
    in production code).
    """
    idx = layout.interior_vertex_index()
    key = (round(point.x), round(point.y))
    candidates = idx.get(key)
    if candidates:
        for wire_id, i in candidates:
            wire = layout.wires.get(wire_id)
            if wire is None:
                continue
            if i >= len(wire.path) - 1:
                continue
            v = wire.path[i]
            if abs(v.x - point.x) < tol and abs(v.y - point.y) < tol:
                return wire, i
    return None


def split_wire_at_vertex(
    layout: Layout,
    wire: Wire,
    vertex_idx: int,
) -> tuple[Wire, Wire, JointKnot]:
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
                fragments_a.append(
                    Fragment(
                        path=list(frag.path[: local_idx + 1]),
                        side=frag.side,
                    )
                )
            if local_idx < frag_len - 1:
                fragments_b.append(
                    Fragment(
                        path=list(frag.path[local_idx:]),
                        side=frag.side,
                    )
                )
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
            id=a_knot_id,
            position=a_knot.position,
        )
    if b_knot_id not in layout.knots and isinstance(b_knot, JointKnot):
        layout.knots[b_knot_id] = JointKnot(
            id=b_knot_id,
            position=b_knot.position,
        )

    # Determine the hub at the split point.  Prefer an existing PinKnot
    # (a pin always wins identity over a joint at the same pad) or an
    # existing JointKnot at the position; only create a fresh JointKnot
    # when nothing's there.  Returning the hub for the public API.
    shared_hub: PinKnot | JointKnot | None = None
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
    pin_resolver = layout._pin_resolver
    if shared_hub is None and pin_resolver is not None:
        for pin_ref, knot_id in layout.pin_knots.items():
            try:
                pin_pos = pin_resolver(pin_ref)
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
    # Tracing for the wire-identity / orphan-selection investigation: when
    # a wire is split, the second half gets a fresh UUID and any external
    # holder of the original id (SelectTool's _selected_wire_id, net
    # caches, recordings) now points only at the first half.  Without
    # these logs the bug shows up as "I edited a wire and a new wire
    # appeared while my old one vanished from selection" with no trail.
    _log.info(
        "wire.split_at_vertex",
        old_id=str(wire.id)[:8],
        new_a_id=str(wire_a.id)[:8],
        new_b_id=str(wire_b.id)[:8],
        hub_id=str(shared_hub.id)[:8],
        hub_kind=type(shared_hub).__name__,
        vertex_idx=vertex_idx,
        split_point=(round(split_point.x, 3), round(split_point.y, 3)),
        a_len=len(flat_a),
        b_len=len(flat_b),
    )
    return wire_a, wire_b, shared_hub  # type: ignore[return-value]


def merge_colocated_joints(
    layout: Layout,
    *,
    schematic: Schematic | None = None,
    instances: dict[uuid.UUID, ComponentInstance] | None = None,
    components: dict[str, ComponentDefinition] | None = None,
    board: Board | None = None,
) -> int:
    """Fuse ``JointKnot``s sharing a position into one knot **per bucket**.

    A bucket is defined by ``(position, side, net)`` of the wires hanging
    off each joint.  Two joints in the same pad position whose wires
    belong to different schematic nets, or to different sides, are
    physically distinct solder joints and must stay separate — fusing
    them was the *reference_big_project* bug where 4 wires of mixed
    sides and mixed nets ended up sharing one degree-4 knot, scaling
    into a 22-net electric cluster.

    Each joint in this pass has the invariant that all its incident
    wires share one ``(side, net)`` — that's what
    :func:`split_wires_at_endpoint_collisions` and
    :func:`make_t_junction` produce.  We classify per joint and group
    by ``(position, side, net)``; only within a bucket do we coalesce
    by picking the lexicographically smallest joint id as winner and
    retargeting wire back-refs.  Returns the number of loser knots
    dropped.  Behaves like the pre-fix code when ``schematic`` is not
    supplied (legacy tests).
    """

    # Classify each joint by the (side, net) of its incident wires.
    # When a joint's wires don't agree on side or net, the joint is
    # already in an inconsistent state — leave it alone (don't make it
    # worse by fusing with neighbours).
    def _joint_bucket(
        knot: JointKnot,
    ) -> tuple[str, uuid.UUID | None] | None:
        sides: set[str] = set()
        nets: set[uuid.UUID | None] = set()
        for wid in knot.wire_ids:
            w = layout.wires.get(wid)
            if w is None:
                continue
            sides.add(w.side)
            if (
                schematic is not None
                and instances is not None
                and components is not None
            ):
                nets.add(
                    wire_net_id(
                        w, schematic, layout, instances, components, board=board
                    )
                )
        if len(sides) > 1:
            return None
        if not sides:
            return None
        if schematic is None:
            return (next(iter(sides)), None)
        # Allow joints whose wires are partially-net-resolved: a net set
        # of {None, X} represents "some wires are net-less" — that's
        # still compatible with bucket X (net-less wires inherit X).
        explicit = {n for n in nets if n is not None}
        if len(explicit) > 1:
            return None  # inconsistent — leave alone
        return (next(iter(sides)), next(iter(explicit), None))

    by_bucket: dict[tuple[float, float, str, uuid.UUID | None], list[uuid.UUID]] = (
        defaultdict(list)
    )
    for knot in layout.knots.values():
        if not isinstance(knot, JointKnot):
            continue
        bucket = _joint_bucket(knot)
        if bucket is None:
            continue
        side, net_id = bucket
        key = (*rounded_point(knot.position), side, net_id)
        by_bucket[key].append(knot.id)

    dropped = 0
    for _key, ids in by_bucket.items():
        if len(ids) < 2:
            continue
        ids_sorted = sorted(ids, key=str)
        winner = ids_sorted[0]
        losers = ids_sorted[1:]
        winner_knot = layout.knots[winner]
        loser_wire_ids: set[uuid.UUID] = set()
        for loser_id in losers:
            loser_knot = layout.knots.get(loser_id)
            if not isinstance(loser_knot, JointKnot):
                continue
            loser_wire_ids |= loser_knot.wire_ids
        for wid in loser_wire_ids:
            w = layout.wires.get(wid)
            if w is None:
                continue
            if w.a is not None and w.a.id in losers:
                w.a = KnotRef(id=winner)
            if w.b is not None and w.b.id in losers:
                w.b = KnotRef(id=winner)
        layout.knots[winner] = winner_knot.with_wire_ids(
            winner_knot.wire_ids | loser_wire_ids
        )
        for loser_id in losers:
            layout.knots.pop(loser_id, None)
            dropped += 1
    return dropped


def make_t_junction(
    layout: Layout,
    w_through: Wire,
    vertex_idx: int,
    w_incoming: Wire,
    incoming_endpoint: str,  # "a" or "b" — which end of w_incoming hits the vertex
) -> tuple[Wire, Wire, Wire, JointKnot | object]:
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
    if incoming_endpoint not in ("a", "b"):
        raise ValueError(
            f"incoming_endpoint must be 'a' or 'b'; got {incoming_endpoint!r}"
        )
    # Remember w_incoming's old endpoint knot id so we can prune it if
    # it became orphan (degree=0 JointKnot) after the swap.
    old_ref = w_incoming.a if incoming_endpoint == "a" else w_incoming.b
    if old_ref is None:
        raise ValueError("w_incoming has no endpoint to re-anchor")
    old_endpoint_id = old_ref.id

    # Trace entry so a future investigator can correlate split logs with
    # the T-junction that triggered them.  The incoming wire's id is the
    # key piece of context — the user was holding it selected when the
    # T-junction collapsed the geometry.
    _log.info(
        "wire.t_junction.begin",
        w_through_id=str(w_through.id)[:8],
        w_incoming_id=str(w_incoming.id)[:8],
        incoming_endpoint=incoming_endpoint,
        vertex_idx=vertex_idx,
        split_point=(
            round(w_through.path[vertex_idx].x, 3),
            round(w_through.path[vertex_idx].y, 3),
        ),
        old_endpoint_knot_id=str(old_endpoint_id)[:8],
    )
    # Step 1 — split w_through.  This produces the hub already at the
    # right position (PinKnot reuse / fresh JointKnot).
    w_a, w_b, hub = split_wire_at_vertex(layout, w_through, vertex_idx)
    hub_id = hub.id

    # Step 2 — re-anchor w_incoming's endpoint to the hub.  Decrement
    # the old knot's wire_ids in place; add w_incoming to the hub.
    if incoming_endpoint == "a":
        w_incoming.a = KnotRef(id=hub_id)
    else:
        w_incoming.b = KnotRef(id=hub_id)
    # Update hub back-refs.
    current_hub = layout.knots.get(hub_id)
    if current_hub is not None:
        # Rebuild the hub with the incoming wire added to its back-refs;
        # each knot kind preserves its own identity fields.
        layout.knots[hub_id] = current_hub.with_wire_ids(
            current_hub.wire_ids | {w_incoming.id}
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
        if isinstance(old_knot, JointKnot) and not updated_old_ids:
            # Orphan joint — drop. (Lifetime rule: a JointKnot with no
            # incident wires is removed; PinKnots survive regardless.)
            del layout.knots[old_endpoint_id]
        else:
            layout.knots[old_endpoint_id] = old_knot.with_wire_ids(updated_old_ids)
    _log.info(
        "wire.t_junction.done",
        w_through_id=str(w_through.id)[:8],
        w_a_id=str(w_a.id)[:8],
        w_b_id=str(w_b.id)[:8],
        w_incoming_id=str(w_incoming.id)[:8],
        hub_id=str(hub_id)[:8],
        hub_degree=len(layout.knots[hub_id].wire_ids) if hub_id in layout.knots else 0,
    )
    return w_a, w_b, w_incoming, hub


def _pin_at(
    position: Point,
    side: str,
    layout: Layout,
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    *,
    tol: float = 1e-6,
    plated_pads: frozenset[Point] | None = None,
) -> PinRef | None:
    """Return the PinRef of a component pin landing on *position*.

    Side semantics:
      * When ``plated_pads`` is provided and ``position`` is a
        plated-through pad, accept pins from either side — the via
        bridges them electrically into one node.
      * Otherwise restrict to pins whose placement side equals *side*.

    Used by :func:`wire_net_id` so wires routed on the bottom layer
    that terminate at a top-side component pin (the autoroute common
    case for through-hole parts) resolve to the right schematic net.
    """
    cross_side_ok = plated_pads is not None and any(
        abs(p.x - position.x) < tol and abs(p.y - position.y) < tol for p in plated_pads
    )

    for component in iter_placed_components(layout.placements, instances, components):
        comp_id = component.placement.component_id
        if not cross_side_ok and component.placement.side != side:
            continue
        fp = resolved_footprint(
            component.definition,
            component.instance,
            component.instance.families,
        )
        for pin_num, wp in iter_footprint_pin_positions(fp, component.placement):
            if abs(wp.x - position.x) < tol and abs(wp.y - position.y) < tol:
                meta_pin_index = meta_pin_for_package_pin(
                    component.definition,
                    component.instance,
                    pin_num,
                )
                if meta_pin_index is None:
                    continue
                return PinRef(component_id=comp_id, pin_number=meta_pin_index)
    return None


def wire_net_id(
    wire: Wire,
    schematic: Schematic,
    layout: Layout,
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    *,
    board: Board | None = None,
    pad_index: PadIndex | None = None,
) -> uuid.UUID | None:
    """Resolve *wire* to a schematic net id.

    Strategy: scan every vertex of the path looking for a pin sitting on
    that point.  Same-side pins always match; cross-side pins also
    match when the pad is plated-through (a via bridges top↔bottom
    electrically into one node) — that case dominates real-world
    autoroute output, where a wire ends on the bottom layer but the
    component pin lives on the top layer of the same hole.  Without
    the plated-through allowance, ``wire_net_id`` returned None for
    every through-hole termination and the cross-net joint gate
    silently no-op'd.

    When ``pad_index`` is provided, the wire's net falls out directly
    from :meth:`PadIndex.electrical_net_at` — no per-vertex pin scan,
    no plated_through_pads rebuild.  Hot-path callers (autorouter,
    DRC) get O(1) lookup per vertex; legacy callers (load, persistence)
    fall back to the pin-scan implementation.

    Returns ``None`` when the wire touches no pin at all — those are
    wires hanging on knot-to-knot junctions, electrically "neutral"
    until a downstream join determines the net.
    """
    if pad_index is not None:
        # Hot path — query the index directly. SHORT_CIRCUIT pads are
        # ambiguous so we fall through to the per-vertex pin scan
        # below; in practice short pads are rare and DRC has already
        # flagged them.
        for vertex in wire.path:
            net = pad_index.electrical_net_at(vertex, wire.side)
            if isinstance(net, uuid.UUID):
                return net
        return None
    plated: frozenset[Point] | None = (
        frozenset(plated_through_pads(board)) if board is not None else None
    )
    for vertex in wire.path:
        pin = _pin_at(
            vertex,
            wire.side,
            layout,
            instances,
            components,
            plated_pads=plated,
        )
        if pin is None:
            continue
        net = schematic.net_for_pin(pin)
        if net is not None:
            return net.id
    return None


# Kept as a local compatibility alias for older tests and integrations; new
# callers should use the public name above.
_wire_net_id = wire_net_id


@dataclass(frozen=True)
class WireNetResolver:
    """Cohesive view for "what net does this wire belong to?" over one context.

    Collapses the 5-required-plus-2-keyword parameter sprawl of
    :func:`wire_net_id` — ``schematic / layout / instances / components``
    plus optional ``board`` / ``pad_index`` — into fields gathered once. The
    callers that resolve two wires in a row against the same context
    (``_classify_t_junction``, ``merge_endpoint_wires``,
    ``merge_colocated_joints``) become a single ``resolver.net_id(w)`` per
    wire instead of re-threading six arguments each time.

    :meth:`net_id` delegates verbatim to the free :func:`wire_net_id`, which
    stays the canonical implementation and documented pure helper. Frozen
    because no per-batch cache is kept — each ``wire_net_id`` call builds and discards
    its own plated-pad set (or skips it entirely on the ``pad_index`` fast
    path). Build via :meth:`from_state` or directly with the context.

    Mirrors :class:`~protoboard.domain.layout.group_ownership.OwnershipResolver`
    and :class:`~protoboard.domain.layout.pad_conflict.ConflictScanner`.
    """

    schematic: Schematic
    layout: Layout
    instances: dict[uuid.UUID, ComponentInstance]
    components: dict[str, ComponentDefinition]
    board: Board | None = None
    pad_index: PadIndex | None = None

    @classmethod
    def from_state(
        cls,
        state: object,
        schematic: Schematic,
        *,
        pad_index: PadIndex | None = None,
    ) -> WireNetResolver:
        """Build a resolver from a ``ComponentState`` / ``EditorSession``-like object.

        Reads ``layout``, ``instances``, ``components``, ``board``. ``schematic``
        is passed explicitly because :class:`ComponentState` does not guarantee
        it (only :class:`SchematicState` does) and callers already gate on
        ``schematic is not None`` before resolving. Duck-typed (``object``) so
        the domain module stays Qt-free.
        """
        return cls(
            schematic=schematic,
            layout=state.layout,  # type: ignore[attr-defined]
            instances=state.instances,  # type: ignore[attr-defined]
            components=state.components,  # type: ignore[attr-defined]
            board=getattr(state, "board", None),
            pad_index=pad_index,
        )

    def net_id(self, wire: Wire) -> uuid.UUID | None:
        """Resolve *wire* to a schematic net id (see :func:`wire_net_id`)."""
        return wire_net_id(
            wire,
            self.schematic,
            self.layout,
            self.instances,
            self.components,
            board=self.board,
            pad_index=self.pad_index,
        )


def can_form_t_junction(
    w_through: Wire,
    w_incoming: Wire,
    incoming_endpoint: str,
    *,
    schematic: Schematic | None = None,
    layout: Layout | None = None,
    instances: dict[uuid.UUID, ComponentInstance] | None = None,
    components: dict[str, ComponentDefinition] | None = None,
    board: Board | None = None,
    pad_index: PadIndex | None = None,
    strategy: PadStrategy | None = None,
) -> tuple[TJunctionVerdict, PinRef | None, uuid.UUID | None]:
    """Decide whether *w_incoming*'s ``incoming_endpoint`` may bond to
    *w_through*'s interior to form a T-junction.

    Returns ``(verdict, orphan_pin, net_id_to_assign)``.  ``orphan_pin``
    and ``net_id_to_assign`` are populated only when the verdict is
    ``ASSIGN_NET_TO_INCOMING_PIN`` — the caller assigns that pin to that
    net before invoking :func:`make_t_junction`.

    Rules (per user spec 2026-05-30):

    * Sides must match.  A wire on ``top`` and a wire on ``bottom``
      cannot share one solder joint.  ``INCOMPATIBLE_GEOMETRY``.
    * When schematic context is missing (legacy callers), behave as the
      pre-fix code did: return ``SAFE``.  Pure-geometry callsites
      (in-memory tests) still work without schematic plumbing.
    * Resolve each wire's net via the pin lookup on its path.
    * If either net is ``None`` (no pin on path) the join doesn't bridge
      any *declared* net → ``SAFE``.
    * If both nets are equal → ``SAFE``.
    * If both nets are set and differ → ``CONFLICT_REJECT``.
    * If *w_incoming*'s endpoint is a pin whose schematic net is unset,
      and *w_through* has a known net → ``ASSIGN_NET_TO_INCOMING_PIN``
      (the caller must execute the assignment, then the T-junction).

    When ``strategy`` and ``pad_index`` are provided the strategy is
    consulted *as a veto*: a SAFE verdict can be downgraded to
    CONFLICT_REJECT if the strategy returns REJECT (e.g. simplify
    strict mode forbidding a crossing the DRC rules tolerate).
    ASSIGN_NET cases are not gated by the strategy — they encode the
    net-inheritance flow the calling AddWireCommand still has to
    enact.
    """
    if w_through.side != w_incoming.side:
        return TJunctionVerdict.INCOMPATIBLE_GEOMETRY, None, None
    if schematic is None or layout is None or instances is None or components is None:
        return TJunctionVerdict.SAFE, None, None

    net_through = wire_net_id(
        w_through,
        schematic,
        layout,
        instances,
        components,
        board=board,
        pad_index=pad_index,
    )
    net_incoming = wire_net_id(
        w_incoming,
        schematic,
        layout,
        instances,
        components,
        board=board,
        pad_index=pad_index,
    )

    verdict, orphan, net_to_assign = _classify_t_junction(
        w_through=w_through,
        w_incoming=w_incoming,
        incoming_endpoint=incoming_endpoint,
        net_through=net_through,
        net_incoming=net_incoming,
        schematic=schematic,
        layout=layout,
        instances=instances,
        components=components,
        board=board,
    )

    if (
        strategy is not None
        and pad_index is not None
        and verdict is TJunctionVerdict.SAFE
    ):
        # Veto path — let the strategy reject a soft-rule case (e.g.
        # SimplifyStrictStrategy refuses bare-on-bare even when nets
        # match). Strategy's ASSIGN_NET is ignored here because the
        # net-inheritance path already handles it via the verdict
        # returned by `_classify_t_junction`.
        ep_point = (
            w_incoming.path[0] if incoming_endpoint == "a" else w_incoming.path[-1]
        )
        pad = pad_index.pad_at(ep_point)
        if pad is not None:
            cross_layer = w_through.side != w_incoming.side  # always False here
            d = strategy.can_form_t_junction(
                pad,
                w_through.side,
                net_through,
                net_incoming,
                cross_layer,
            )
            if d.verdict is Verdict.REJECT:
                return TJunctionVerdict.CONFLICT_REJECT, None, None

    return verdict, orphan, net_to_assign


def _classify_t_junction(
    *,
    w_through: Wire,
    w_incoming: Wire,
    incoming_endpoint: str,
    net_through: uuid.UUID | None,
    net_incoming: uuid.UUID | None,
    schematic: Schematic,
    layout: Layout,
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    board: Board | None,
) -> tuple[TJunctionVerdict, PinRef | None, uuid.UUID | None]:
    """Apply the net-equality + orphan-pin verdict logic.

    Split out of :func:`can_form_t_junction` so the strategy veto
    branch and the pure verdict logic stay independently readable.
    """
    if net_through is None and net_incoming is None:
        return TJunctionVerdict.SAFE, None, None
    if net_through is None or net_incoming is None:
        if net_through is not None and net_incoming is None:
            ep_point = (
                w_incoming.path[0] if incoming_endpoint == "a" else w_incoming.path[-1]
            )
            _orphan_plated = (
                frozenset(plated_through_pads(board)) if board is not None else None
            )
            orphan = _pin_at(
                ep_point,
                w_incoming.side,
                layout,
                instances,
                components,
                plated_pads=_orphan_plated,
            )
            if orphan is not None and schematic.net_for_pin(orphan) is None:
                return (
                    TJunctionVerdict.ASSIGN_NET_TO_INCOMING_PIN,
                    orphan,
                    net_through,
                )
        return TJunctionVerdict.CONFLICT_REJECT, None, None
    if net_through == net_incoming:
        return TJunctionVerdict.SAFE, None, None
    return TJunctionVerdict.CONFLICT_REJECT, None, None


def split_wires_at_endpoint_collisions(
    layout: Layout,
    *,
    schematic: Schematic | None = None,
    instances: dict[uuid.UUID, ComponentInstance] | None = None,
    components: dict[str, ComponentDefinition] | None = None,
    board: Board | None = None,
) -> int:
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
                # Gate the T-junction by net compatibility.  Persistence
                # passes (load/save) call this without an authoritative
                # net-rewrite path, so we only treat SAFE as a green
                # light here; ASSIGN_NET cases require a caller that can
                # mutate the schematic (e.g. AddWireCommand).  Without
                # the gate, an insulated wire crossing another insulated
                # wire of a different net would get spliced into a
                # shared hub on load — that's the bug that produced the
                # 22-net cluster in reference_big_project.
                verdict, _orphan, _net = can_form_t_junction(
                    w2,
                    w1,
                    endpoint_name,
                    schematic=schematic,
                    layout=layout,
                    instances=instances,
                    components=components,
                    board=board,
                )
                if verdict is not TJunctionVerdict.SAFE:
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


def merge_endpoint_wires(
    layout: Layout,
    board: Board,
    *,
    schematic: Schematic | None = None,
    instances: dict[uuid.UUID, ComponentInstance] | None = None,
    components: dict[str, ComponentDefinition] | None = None,
) -> int:
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

    When ``schematic`` is provided, both passes refuse to fuse wires
    that resolve to different schematic nets — a JointKnot represents a
    physical solder joint shared by wires of one net (see the docstring
    on :class:`JointKnot`), so collapsing wires of two different nets
    creates a phantom short.  Legacy callers that omit ``schematic``
    fall back to the geometry-only behaviour.

    Returns the total number of wires that disappeared because they
    fused into a neighbour.  Pure: only wire / knot identity changes —
    ``wire.path`` / ``wire.fragments`` only change to reflect the union
    of the source paths (multi-fragment Wire is produced for cross-side
    via collapses).
    """
    merge_colocated_joints(
        layout,
        schematic=schematic,
        instances=instances,
        components=components,
        board=board,
    )
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
            # Net gate: collapsing two wires fuses their endpoints; if
            # the wires belong to different declared nets the fused
            # result would short the nets together.  Skip in that case
            # and leave both wires as a 2-wire knot — physically OK as
            # the joint is at the shared pad, semantically a crossing.
            if (
                schematic is not None
                and instances is not None
                and components is not None
            ):
                n1 = wire_net_id(
                    w1, schematic, layout, instances, components, board=board
                )
                n2 = wire_net_id(
                    w2, schematic, layout, instances, components, board=board
                )
                if n1 is not None and n2 is not None and n1 != n2:
                    continue
            merged = layout.try_collapse_joint(
                knot_id,
                plated_through_pads=pt_pads,
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
    "wire_net_id",
    "insert_pad_nodes",
    "coalesce_same_side_fragments",
    "find_wire_with_interior_vertex_at",
    "split_wire_at_vertex",
    "merge_colocated_joints",
    "merge_endpoint_wires",
    "split_wires_at_endpoint_collisions",
    "wire_has_skewed_segment",
    "find_skewed_wires",
    "remove_wire_capturing_orphans",
    "restore_wire_with_orphans",
    "snapshot_wire_geometry",
    "restore_wire_geometry",
]


def remove_wire_capturing_orphans(
    layout: Layout,
    wire_id: uuid.UUID,
) -> tuple[Wire | None, list[JointKnot | WirePinKnot]]:
    """Remove *wire_id* from *layout* while snapshotting orphan joints.

    Returns ``(wire, orphan_joints)`` where:

    * ``wire`` is the removed Wire (or None if it was already gone —
      can legally happen mid-macro when a sibling command preempts the
      removal),
    * ``orphan_joints`` are free-position endpoints (``JointKnot`` or
      ``WirePinKnot``) whose only registered wire was *wire_id*,
      captured BEFORE the actual removal so callers can re-insert them
      on revert.  Wire-pins are tied to their owning wire (lifetime
      invariant), so they must be revived together with the wire on
      undo — same idiom as orphan joints.

    Single source of truth for ``RemoveWireCommand``,
    ``DropWiresCommand``, ``MoveDragSetCommand`` (and any future
    command that removes a wire while preserving undo-state).
    Previously each inlined the same orphan-capture + ``remove_wire``
    sequence with subtle drift between copies — the bug-pattern that
    has hit this codebase multiple times.

    Does NOT call ``layout.invalidate_interior_vertex_index()`` —
    callers know whether they need to invalidate.
    """
    wire = layout.wires.get(wire_id)
    if wire is None:
        return None, []
    orphans: list[JointKnot | WirePinKnot] = []
    if wire.a is not None and wire.b is not None:
        for ref in (wire.a, wire.b):
            k = layout.knots.get(ref.id)
            if isinstance(k, (JointKnot, WirePinKnot)) and k.wire_ids == frozenset(
                {wire.id}
            ):
                orphans.append(k)
        layout.remove_wire(wire_id)
    else:
        layout.wires.remove(wire)
    return wire, orphans


def restore_wire_with_orphans(
    layout: Layout,
    wire: Wire,
    orphans: list[JointKnot | WirePinKnot],
) -> None:
    """Re-insert *wire* and any of *orphans* it references back into *layout*.

    Mirror of :func:`remove_wire_capturing_orphans` — call from revert
    paths.  Idempotent against already-restored joints (uses
    ``setdefault``).
    """
    if wire.a is not None and wire.b is not None:
        for jk in orphans:
            if jk.id in {wire.a.id, wire.b.id}:
                layout.knots.setdefault(jk.id, jk)
        layout.add_wire(wire)
    else:
        layout.wires.append(wire)


@dataclass
class WireSnapshot:
    """Frozen copy of a Wire's fields, sufficient to recreate it on revert."""

    wire_id: uuid.UUID
    path: list[Point]
    insulated: bool
    side: str
    color: str | None
    a_id: uuid.UUID
    b_id: uuid.UUID
    # Per-fragment side breakdown — preserves multi-fragment wires
    # (via boundaries).
    fragment_sides: list[str]
    fragment_paths: list[list[Point]]
    # Snapshots of the endpoint knots themselves so revert can revive any
    # that ``remove_wire`` later pruned as orphans (outer joints of the
    # source wires that had no other incident wire than this one).  Keyed
    # by knot id; ``None`` for ``PinKnot`` entries — pins live in
    # ``layout.pin_knots`` and survive ``remove_wire`` independently.
    a_knot: JointKnot | None
    b_knot: JointKnot | None


def snapshot_wire(w: Wire, layout: Layout) -> WireSnapshot:
    """Capture *w*'s fields (and any JointKnot endpoints) for later revert.

    Companion to :func:`remove_wire_capturing_orphans` /
    :func:`restore_wire_with_orphans`: where those capture orphan knots
    around an actual removal, this records the full wire so a command can
    rebuild it from scratch.  ``PinKnot`` endpoints are stored as ``None``
    — pins live in ``layout.pin_knots`` and survive ``remove_wire``.
    """
    assert w.a is not None and w.b is not None
    a_knot = layout.knots.get(w.a.id)
    b_knot = layout.knots.get(w.b.id)
    return WireSnapshot(
        wire_id=w.id,
        path=list(w.path),
        insulated=w.insulated,
        side=w.side,
        color=w.color,
        a_id=w.a.id,
        b_id=w.b.id,
        fragment_sides=[f.side for f in w.fragments],
        fragment_paths=[list(f.path) for f in w.fragments],
        a_knot=a_knot if isinstance(a_knot, JointKnot) else None,
        b_knot=b_knot if isinstance(b_knot, JointKnot) else None,
    )


@dataclass
class SplitRecord:
    """Snapshot of one ``make_t_junction`` for revert."""

    original: WireSnapshot
    new_a_id: uuid.UUID
    new_b_id: uuid.UUID
    hub_id: uuid.UUID
    incoming_endpoint: str  # "a" or "b"


@dataclass
class CollapseRecord:
    """Snapshot of one ``try_collapse_joint`` operation for revert."""

    joint_id: uuid.UUID
    joint_pos: Point
    w1_snapshot: WireSnapshot
    w2_snapshot: WireSnapshot
    merged_wire_id: uuid.UUID | None = None


def restore_split(
    layout: Layout,
    wire: Wire,
    rec: SplitRecord,
) -> None:
    """Inverse of ``make_t_junction`` — replace the two split halves
    with the original wire and detach ``wire``'s endpoint from the
    shared hub.
    """
    # Detach wire's endpoint from the hub: revert its knot ref to the
    # original endpoint knot id, which was preserved on the wire
    # itself before make_t_junction ran (we stored it in ``rec`` only
    # implicitly — derive it from the wire's pre-split endpoint).
    # Concretely: the wire's matching endpoint knot was the same as
    # what AddWireCommand resolved earlier; ``make_t_junction``
    # swapped it to the hub.  AddWireCommand.revert removes the wire
    # entirely, so we don't need to revert the knot ref here —
    # restore the wires the split consumed.
    for new_id in (rec.new_a_id, rec.new_b_id):
        if layout.wires.get(new_id) is not None:
            layout.remove_wire(new_id)
    # Drop the hub if no other wires reference it (it was created by
    # make_t_junction and is now orphan after removing the two halves).
    hub = layout.knots.get(rec.hub_id)
    if hub is not None:
        if isinstance(hub, JointKnot):
            if not (hub.wire_ids - {rec.new_a_id, rec.new_b_id, wire.id}):
                layout.knots.pop(rec.hub_id, None)
    # Revive the original wire from snapshot.
    snap = rec.original
    fragments = [
        Fragment(path=list(p), side=s)  # type: ignore[arg-type]
        for p, s in zip(snap.fragment_paths, snap.fragment_sides)
    ]
    revived = Wire(
        id=snap.wire_id,
        path=list(snap.path),
        insulated=snap.insulated,
        side=snap.side,  # type: ignore[arg-type]
        color=snap.color,
        fragments=fragments,
        a=KnotRef(id=snap.a_id),
        b=KnotRef(id=snap.b_id),
    )
    # Outer knots of the original may have been pruned by remove_wire
    # earlier (orphan after split); revive them from snapshot like
    # restore_collapse does.
    for knot_id, snap_knot in (
        (snap.a_id, snap.a_knot),
        (snap.b_id, snap.b_knot),
    ):
        if knot_id in layout.knots:
            continue
        if snap_knot is None:
            continue
        layout.knots[knot_id] = JointKnot(
            id=knot_id,
            position=snap_knot.position,
        )
    layout.add_wire(revived)


def restore_collapse(layout: Layout, rec: CollapseRecord) -> None:
    """Inverse of ``try_collapse_joint`` — split the merged wire back into two.

    Removes the merged wire (which prunes any outer JointKnots that have
    no other incident wire), revives the central joint that was consumed
    by the collapse, revives any outer joints that ``remove_wire`` pruned
    as orphans, and re-inserts the two source wires.  PinKnots survive
    ``remove_wire`` independently — no revival needed for those.
    """
    if rec.merged_wire_id is not None:
        merged = layout.wires.get(rec.merged_wire_id)
        if merged is not None:
            layout.remove_wire(merged.id)
    # Re-create the central joint consumed by the merge.
    if rec.joint_id not in layout.knots:
        revived_joint = JointKnot(id=rec.joint_id, position=rec.joint_pos)
        layout.knots[rec.joint_id] = revived_joint
    # Re-create any outer JointKnots that ``remove_wire`` pruned as
    # orphans.  ``add_wire`` below will repopulate their ``wire_ids``.
    for snap in (rec.w1_snapshot, rec.w2_snapshot):
        for knot_id, snap_knot in (
            (snap.a_id, snap.a_knot),
            (snap.b_id, snap.b_knot),
        ):
            if knot_id in layout.knots:
                continue
            if snap_knot is None:
                continue
            # Revive with empty wire_ids; add_wire repopulates.
            layout.knots[knot_id] = JointKnot(
                id=knot_id,
                position=snap_knot.position,
            )
    # Re-insert both source wires.
    for snap in (rec.w1_snapshot, rec.w2_snapshot):
        fragments = [
            Fragment(path=list(p), side=s)  # type: ignore[arg-type]
            for p, s in zip(snap.fragment_paths, snap.fragment_sides)
        ]
        revived = Wire(
            id=snap.wire_id,
            path=list(snap.path),
            insulated=snap.insulated,
            side=snap.side,  # type: ignore[arg-type]
            color=snap.color,
            fragments=fragments,
            a=KnotRef(id=snap.a_id),
            b=KnotRef(id=snap.b_id),
        )
        layout.add_wire(revived)


def split_existing_wires_at_endpoints(
    wire: Wire,
    *,
    layout: Layout,
    board: Board,
    schematic: Schematic | None,
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
) -> list[SplitRecord]:
    """For each endpoint of ``wire`` that lands on the interior vertex
    of an existing wire, atomically build a T-junction (split that
    wire and re-anchor ``wire``'s endpoint to the shared hub).

    Returns one :class:`SplitRecord` per split so revert can stitch the
    split halves back into a single wire (see :func:`restore_split`).
    Pin endpoints of ``wire`` still take precedence over interior hits —
    pins are physical hubs and never demote an existing wire to
    T-junction status.

    ``schematic`` may be ``None`` (bare-bones command callers without a
    netlist); the net-conflict gate degrades to permissive when so.  The
    AddWireCommand wrapper resolves it defensively off the host state.
    """
    records: list[SplitRecord] = []
    if wire.a is None or wire.b is None:
        return records
    # Snapshot original wire endpoint positions — these don't move,
    # but the wire's own ``a``/``b`` knot refs may change as the
    # splits retarget them to the shared hub.
    for endpoint_name, point in (("a", wire.path[0]), ("b", wire.path[-1])):
        hit = find_wire_with_interior_vertex_at(layout, point)
        if hit is None:
            continue
        w_through, vertex_idx = hit
        if w_through.id == wire.id:
            continue
        # Net-conflict gate.  When a fresh wire endpoint sits inside an
        # existing wire of a different net, we leave the geometry as a
        # crossing — collapsing it into a shared hub would short the two
        # nets.  When the endpoint is a pin with no net yet, the verdict
        # tells us which net to assign before splicing.
        verdict, orphan_pin, net_to_assign = can_form_t_junction(
            w_through,
            wire,
            endpoint_name,
            schematic=schematic,
            layout=layout,
            instances=instances,
            components=components,
            board=board,
        )
        if verdict is TJunctionVerdict.CONFLICT_REJECT:
            continue
        if verdict is TJunctionVerdict.INCOMPATIBLE_GEOMETRY:
            continue
        if verdict is TJunctionVerdict.ASSIGN_NET_TO_INCOMING_PIN:
            if schematic is None or orphan_pin is None or net_to_assign is None:
                continue
            net = schematic.nets.get(net_to_assign)
            if net is None:
                continue
            schematic.add_pin_to_net(orphan_pin, net_to_assign)
        # Snapshot the pre-split state of the wire we're about to cut,
        # so revert can rebuild it from one piece.
        snapshot = snapshot_wire(w_through, layout)
        try:
            w_a, w_b, _, hub = make_t_junction(
                layout,
                w_through,
                vertex_idx,
                wire,
                endpoint_name,
            )
        except ValueError:
            # Revert the speculative pin assignment if the split itself
            # failed — we don't want a dangling pin-to-net link without
            # the corresponding geometry.
            if verdict is TJunctionVerdict.ASSIGN_NET_TO_INCOMING_PIN:
                if (
                    schematic is not None
                    and orphan_pin is not None
                    and net_to_assign is not None
                ):  # noqa: E501
                    schematic.remove_pin_from_net(orphan_pin, net_to_assign)
            continue
        records.append(
            SplitRecord(
                original=snapshot,
                new_a_id=w_a.id,
                new_b_id=w_b.id,
                hub_id=hub.id,  # type: ignore[union-attr]
                incoming_endpoint=endpoint_name,
            )
        )
        # Link the AddWireCommand context to the split: external holders
        # (SelectTool, recordings, net cache) tracking ``w_through.id``
        # now follow only ``w_a`` — the ``w_b`` half is a brand-new wire
        # nobody references yet.  If a wire-edit selection survives a
        # T-junction split as orphan, this is the log to look for.
        _log.info(
            "wire.add.t_junction_split",
            incoming_wire_id=str(wire.id)[:8],
            split_wire_id=str(snapshot.wire_id)[:8],
            split_w_a_id=str(w_a.id)[:8],
            split_w_b_id=str(w_b.id)[:8],
            hub_id=str(hub.id)[:8],  # type: ignore[union-attr]
            split_point=(round(point.x, 3), round(point.y, 3)),
        )
    return records


def collapse_endpoint_joints(
    wire: Wire,
    *,
    layout: Layout,
    board: Board,
) -> list[CollapseRecord]:
    """Run ``Layout.try_collapse_joint`` on both endpoints of ``wire``.

    Walks ``wire.a`` and ``wire.b`` (after they have been resolved and the
    wire inserted).  When an endpoint joint has exactly two incident wires
    with matching ``insulated``, fuse them into a single Wire — the joint
    disappears, the two source wires are replaced by one ``merged`` wire
    keeping the smaller id (deterministic).

    Returns a list of :class:`CollapseRecord` describing each collapse so
    the caller can roll the layout back on revert (see
    :func:`restore_collapse`).  The list may contain 0, 1, or 2 entries
    (one per endpoint that collapsed).  Pin endpoints are never
    collapsed — pins are physical hubs and remain solder targets.
    """
    records: list[CollapseRecord] = []
    # Materialise plated-through set once; cheap on small boards, used by
    # the cross-side collapse branch.  ``try_collapse_joint`` wants
    # ``frozenset`` so freeze here once.
    pt_pads = frozenset(plated_through_pads(board))
    # Track endpoint joint ids to visit.  We snapshot the current wire's
    # endpoints because the collapse can replace ``wire`` mid-loop (the
    # merged Wire inherits the lower of the two ids).
    if wire.a is None or wire.b is None:
        return records
    endpoint_ids = (wire.a.id, wire.b.id)
    for ep_id in endpoint_ids:
        # Look up joint freshly each iteration — the previous collapse may
        # have shifted the current wire's identity (new ``wire.a``/``b``).
        knot = layout.knots.get(ep_id)
        if not isinstance(knot, JointKnot):
            continue
        if len(knot.wire_ids) != 2:
            continue
        w_ids = tuple(knot.wire_ids)
        # Only collapse when the *other* wire has matching ``insulated`` —
        # ``try_collapse_joint`` already checks this, but we snapshot the
        # two source wires for revert before invoking it.
        w1 = layout.wires.get(w_ids[0])
        w2 = layout.wires.get(w_ids[1])
        if w1 is None or w2 is None:
            continue
        if w1.insulated != w2.insulated:
            continue
        snapshot = CollapseRecord(
            joint_id=ep_id,
            joint_pos=knot.position,
            w1_snapshot=snapshot_wire(w1, layout),
            w2_snapshot=snapshot_wire(w2, layout),
        )
        merged = layout.try_collapse_joint(
            ep_id,
            plated_through_pads=pt_pads,
        )
        if merged is None:
            continue
        snapshot.merged_wire_id = merged.id
        records.append(snapshot)
        # Re-pin the wire so the second-endpoint pass sees the merged wire.
        wire = merged
    return records


_EPS_KNOT = 1e-6


def find_pin_at(
    point: Point,
    *,
    layout: Layout,
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    our_net: uuid.UUID | None = None,
    strategy: PadStrategy | None = None,
    pad_index: PadIndex | None = None,
    tol: float = _EPS_KNOT,
) -> PinRef | None:
    """Return the PinRef of the pin sitting at ``point``, or None.

    Scans current placements.  Exact match first, then a tolerant
    fallback (``tol``) to absorb the rounding present in legacy paths.

    When ``strategy`` and ``pad_index`` are both supplied, the returned
    pin is gated by :meth:`PadStrategy.can_place_pin` (rule 8 — a pin
    cannot accept an incoming wire of a foreign net even though the
    geometry matches).  Without them the gate is skipped and the
    historical behaviour applies — important for the many in-memory
    tests that drive commands without a full session.  The command-layer
    caller resolves ``strategy`` / ``pad_index`` off the host session.
    """
    for component in iter_placed_components(layout.placements, instances, components):
        comp_id = component.placement.component_id
        fp = resolved_footprint(
            component.definition,
            component.instance,
            component.instance.families,
        )
        for pin_num, pos in iter_footprint_pin_positions(fp, component.placement):
            if not (abs(pos.x - point.x) < tol and abs(pos.y - point.y) < tol):
                continue
            meta_pin_index = meta_pin_for_package_pin(
                component.definition,
                component.instance,
                pin_num,
            )
            if meta_pin_index is None:
                continue
            pin_ref = PinRef(component_id=comp_id, pin_number=meta_pin_index)
            # If a PinKnot for this pin already exists, the pin is already
            # accepting wire connections — adding one more is a legal
            # multi-degree pin junction.  Strategy.can_place_pin's rule 6
            # check (reject when pad has any wire item) is intended for
            # *new pin placement*, not for additional wires soldering to
            # an existing pin; gate it on the absence of a PinKnot.
            if pin_ref in layout.pin_knots:
                return pin_ref
            if strategy is not None and pad_index is not None:
                pad = pad_index.pad_at(pos)
                if pad is not None:
                    d = strategy.can_place_pin(pad, our_net)
                    if d.verdict is Verdict.REJECT:
                        # Rule 8: foreign-net wire cannot solder to
                        # this pin. Skip — caller treats it as "no pin
                        # here" and falls through to JointKnot creation
                        # (which will run its own net gate via
                        # ``find_joint_at``).
                        continue
            return pin_ref
    return None


def _wire_net_for_joint(
    wire: Wire,
    *,
    schematic: Schematic | None,
    layout: Layout,
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    board: Board,
) -> uuid.UUID | None:
    """Resolve *wire*'s schematic net id for joint-compatibility checks."""
    if schematic is None:
        return None
    return wire_net_id(
        wire,
        schematic,
        layout,
        instances,
        components,
        board=board,
    )


def joint_accepts_wire(
    knot: JointKnot,
    *,
    side: str | None,
    net_id: uuid.UUID | None,
    schematic: Schematic | None,
    layout: Layout,
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    board: Board,
    strategy: PadStrategy | None,
    pad_index: PadIndex | None,
    plated_pads_cache: frozenset[Point] | None,
) -> bool:
    """Return True if *knot* is compatible with the incoming wire.

    Pulled out of :func:`find_joint_at` so the strategy-driven and
    legacy-inline branches share the per-incident-wire loop.
    """
    pad = pad_index.pad_at(knot.position) if pad_index is not None else None
    for wid in knot.wire_ids:
        w = layout.wires.get(wid)
        if w is None:
            continue
        cross_layer = side is not None and w.side != side
        # Strategy path — single source of truth when available.
        if strategy is not None and pad is not None:
            w_net = _wire_net_for_joint(
                w,
                schematic=schematic,
                layout=layout,
                instances=instances,
                components=components,
                board=board,
            )
            d = strategy.can_form_t_junction(
                pad,
                w.side,
                w_net,
                net_id,
                cross_layer,
            )
            if d.verdict is Verdict.REJECT:
                return False
            # ALLOW / ASSIGN_NET both keep the joint as a candidate;
            # ASSIGN_NET's actual net mutation is handled later by
            # ``split_existing_wires_at_endpoints`` when the wire is
            # spliced into the existing one.
            continue
        # Legacy inline path — preserved for tests that drive commands
        # without a session-backed strategy.
        if side is not None and w.side != side:
            if plated_pads_cache is None:
                plated_pads_cache = frozenset(plated_through_pads(board))
            if knot.position not in plated_pads_cache:
                return False
        if schematic is not None:
            w_net = _wire_net_for_joint(
                w,
                schematic=schematic,
                layout=layout,
                instances=instances,
                components=components,
                board=board,
            )
            if (w_net is not None and net_id is not None and w_net != net_id) or (
                w_net is not None and net_id is None
            ):
                return False
    return True


def find_joint_at(
    point: Point,
    *,
    layout: Layout,
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    board: Board,
    side: str | None = None,
    net_id: uuid.UUID | None = None,
    schematic: Schematic | None = None,
    strategy: PadStrategy | None = None,
    pad_index: PadIndex | None = None,
) -> JointKnot | None:
    """Return an existing JointKnot at ``point`` *compatible* with the
    incoming wire's ``side`` and ``net_id``.

    Pre-fix this returned the first JointKnot at the position regardless
    of who else was hanging off it.  That let an autoroute pass build a
    single degree-N JointKnot where wires of different schematic nets
    converged on the same off-pad point — the *reference_big_project*
    bug.

    Compatibility (delegated to :class:`PadStrategy` when available,
    otherwise enforced inline for legacy callers):
      * If the joint already carries any wire on a different ``side``
        **and the position is not a plated-through pad** → skip; top
        and bottom can only share one solder joint via a via.
      * If any incident wire resolves to a net different from ``net_id``
        (both sides explicit, not None) → skip; merging would short two
        nets.  Net-less wires already on the joint are tolerated only
        when the incoming wire is also net-less.

    ``side`` / ``net_id`` of ``None`` mean "caller does not know" (legacy
    call sites pre-fix) — behave like before and return the first hit.
    """
    plated_pads: frozenset[Point] | None = None
    for knot in layout.knots.values():
        if not isinstance(knot, JointKnot):
            continue
        if not (
            abs(knot.position.x - point.x) < _EPS_KNOT
            and abs(knot.position.y - point.y) < _EPS_KNOT
        ):
            continue
        if side is None and net_id is None:
            return knot
        if not joint_accepts_wire(
            knot,
            side=side,
            net_id=net_id,
            schematic=schematic,
            layout=layout,
            instances=instances,
            components=components,
            board=board,
            strategy=strategy,
            pad_index=pad_index,
            plated_pads_cache=plated_pads,
        ):
            continue
        return knot
    return None


def resolve_endpoint_knot(
    point: Point,
    *,
    layout: Layout,
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    board: Board,
    schematic: Schematic | None = None,
    side: str | None = None,
    net_id: uuid.UUID | None = None,
    strategy: PadStrategy | None = None,
    pad_index: PadIndex | None = None,
) -> tuple[KnotRef, JointKnot | None]:
    """Resolve an endpoint position to (KnotRef, optional created JointKnot).

    Pin hit → reuse/create the singleton PinKnot for that PinRef.
    Existing JointKnot at this position **and compatible with the
    incoming wire's ``side`` / ``net_id``** → reuse it (so two wires of
    the same net meeting at the same off-pin pad share one hub).
    Otherwise → create a fresh JointKnot at ``point``.  The created knot
    (if any) is returned so AddWireCommand.revert can drop it.

    ``strategy`` / ``pad_index`` gate pin and joint reuse via the
    strategy contract — rules 7 + 8 of pad/pin/wire/junction semantics.
    The command-layer caller resolves them off the host session; legacy
    callers that pass neither get the same inline behaviour as before
    this refactor.
    """
    pin = find_pin_at(
        point,
        layout=layout,
        instances=instances,
        components=components,
        our_net=net_id,
        strategy=strategy,
        pad_index=pad_index,
    )
    if pin is not None:
        pk = layout.get_or_create_pin_knot(pin)
        return KnotRef(id=pk.id), None
    existing = find_joint_at(
        point,
        layout=layout,
        instances=instances,
        components=components,
        board=board,
        side=side,
        net_id=net_id,
        schematic=schematic,
        strategy=strategy,
        pad_index=pad_index,
    )
    if existing is not None:
        return KnotRef(id=existing.id), None
    # Joint at this position (off-pad joints are flagged by DRC, not here).
    jk = layout.create_joint_knot(point)
    return KnotRef(id=jk.id), jk


def _pts_equal(a: Point, b: Point) -> bool:
    return abs(a.x - b.x) < _EPS_KNOT and abs(a.y - b.y) < _EPS_KNOT


def update_wires_for_move(
    wire_list: WireCollection,
    *,
    layout: Layout,
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    comp_id: uuid.UUID,
    old_pl: ComponentPlacement,
    new_pl: ComponentPlacement,
    exclude_ids: frozenset[uuid.UUID] = frozenset(),
) -> None:
    """Adjust wire endpoints that coincide with pins of the moved component.

    For each pin of the component, if a wire endpoint was at the old pin
    position, move it to the new pin position.  If the resulting path
    collapses (start == end) or has fewer than 2 distinct points, the
    wire is removed.  Mutates ``wire_list`` in place (the caller passes
    ``state.layout.wires``).

    exclude_ids: wire IDs already translated by live drag — skipped here to
    avoid double-translation when old_pin_pos of this component coincides with
    the (already-shifted) position of another component's pin.
    """
    resolved = resolve_instance_definition(comp_id, instances, components)
    if resolved is None:
        return
    inst, defn = resolved

    fp = resolved_footprint(defn, inst, inst.families)
    n_pins = fp.pin_count()
    # Build map: old_pin_pos → new_pin_pos
    pin_map: dict[tuple[float, float], Point] = {}
    for pin_num in range(1, n_pins + 1):
        old_pos = pin_world_position(fp, old_pl, pin_num)
        new_pos = pin_world_position(fp, new_pl, pin_num)
        if not _pts_equal(old_pos, new_pos):
            pin_map[rounded_point(old_pos)] = new_pos

    if not pin_map:
        return

    if is_debug_enabled(__name__):
        _log.debug(
            "drag.update_wires_for_move.start",
            comp_id=str(comp_id),
            refdes=getattr(inst, "refdes", "?"),
            exclude=[str(wid) for wid in exclude_ids],
            pin_map=[(k, (v.x, v.y)) for k, v in pin_map.items()],
            wires_in=[str(w.id) for w in wire_list],
        )

    def _wire_anchored_at_comp_pin(
        w: Wire,
        pin_num: int,
        old_pin_pos: Point,
    ) -> bool:
        """True if *w* is logically anchored to ``(comp_id, pin_num)``.

        Anchor evidence comes in two flavours:

        1. **Strong** — either endpoint knot ref is a PinKnot whose
           ``pin`` points at ``(comp_id, pin_num)``. This is the
           canonical post-2026-05-27 wire model and the only
           dependable signal.
        2. **Weak fallback** — the wire carries *no* knot refs at all
           (legacy persistence, hand-constructed test wires) AND one
           of its path endpoints sits on ``old_pin_pos``. We can't
           prove ownership, but treating the endpoint as anchored is
           the historic behaviour ``update_wires_for_move`` had
           before the fix — needed by tests like
           ``test_skewed_wire_dropped_on_drag_commit``.

        Without this gate, ``update_wires_for_move`` would drag every
        wire whose path happens to pass through the *old* pin position
        — a foreign wire ending or crossing at (x, y) before the move
        would silently snap to the new pin position, sticking itself
        to the moved component (user-observed bug 2026-05-31).
        """
        # Strong: explicit PinKnot ref.
        meta_pin_index = meta_pin_for_package_pin(defn, inst, pin_num)
        has_any_knot_ref = False
        for ref in (w.a, w.b):
            if ref is None:
                continue
            has_any_knot_ref = True
            knot = layout.knots.get(ref.id)
            if isinstance(knot, PinKnot):
                if (
                    knot.pin.component_id == comp_id
                    and knot.pin.meta_pin_index == meta_pin_index
                ):
                    return True
        if has_any_knot_ref:
            return False
        # Weak fallback: orphan wire with no knot refs at all. Match
        # endpoints (not interior vertices) against old pin position.
        if not w.path:
            return False
        for endpoint in (w.path[0], w.path[-1]):
            if _pts_equal(endpoint, old_pin_pos):
                return True
        return False

    new_wires: list[Wire] = []
    for wire in wire_list:
        if wire.id in exclude_ids:
            # Even excluded (already live-translated) wires may have ended up
            # with a skewed segment if the component's pin offsets stretched
            # the path unevenly. Drop those.
            if wire_has_skewed_segment(wire):
                if is_debug_enabled(__name__):
                    _log.debug(
                        "drag.update_wires_for_move.drop_excluded_skewed",
                        wire_id=str(wire.id),
                        path=[(p.x, p.y) for p in wire.path],
                    )
                continue
            new_wires.append(wire)
            continue
        # Build a per-wire pin_map containing only entries for pins the
        # wire is *actually* anchored to (via PinKnot endpoint refs).
        # Wires that merely *cross* an old pin position without being
        # soldered to it are left untouched — translating them would
        # drag a foreign wire onto the new pin location.
        wire_pin_map: dict[tuple[float, float], Point] = {}
        for pin_num in range(1, n_pins + 1):
            old_pos = pin_world_position(fp, old_pl, pin_num)
            new_pos = pin_world_position(fp, new_pl, pin_num)
            if _pts_equal(old_pos, new_pos):
                continue
            if _wire_anchored_at_comp_pin(wire, pin_num, old_pos):
                wire_pin_map[rounded_point(old_pos)] = new_pos
        new_path: list[Point] = []
        pin_map_hits: list[tuple[tuple[float, float], tuple[float, float]]] = []
        for pt in wire.path:
            key = rounded_point(pt)
            mapped = wire_pin_map.get(key)
            if mapped is not None:
                pin_map_hits.append((key, (mapped.x, mapped.y)))
                new_path.append(mapped)
            else:
                new_path.append(pt)
        # Drop wire if it collapsed (all points identical or < 2 distinct).
        distinct = {rounded_point(p) for p in new_path}
        if len(distinct) < 2:
            if is_debug_enabled(__name__):
                _log.debug(
                    "drag.update_wires_for_move.drop_collapsed",
                    wire_id=str(wire.id),
                    old_path=[(p.x, p.y) for p in wire.path],
                    new_path=[(p.x, p.y) for p in new_path],
                    pin_map_hits=pin_map_hits,
                )
            continue
        if pin_map_hits and is_debug_enabled(__name__):
            _log.debug(
                "drag.update_wires_for_move.translated",
                wire_id=str(wire.id),
                old_path=[(p.x, p.y) for p in wire.path],
                new_path=[(p.x, p.y) for p in new_path],
                pin_map_hits=pin_map_hits,
            )
        if any(not _pts_equal(new_path[i], wire.path[i]) for i in range(len(new_path))):
            wire.path = new_path
            # Keep fragments in sync — same boundary topology, translated
            # vertices via the per-wire pin_map.  Single-fragment wires
            # stay single.
            if wire.fragments:
                new_fragments_local = []
                for frag in wire.fragments:
                    translated = [
                        wire_pin_map.get(
                            rounded_point(p),
                            p,
                        )
                        for p in frag.path
                    ]
                    new_fragments_local.append(
                        type(frag)(path=translated, side=frag.side),
                    )
                wire.fragments = new_fragments_local
        # Drop wire if any segment became skewed (neither orthogonal nor
        # diagonal) after the endpoint translation.  Cannot be represented
        # by the routing/cost model and is almost always a visual artifact
        # of repeated drag.
        if wire_has_skewed_segment(wire):
            if is_debug_enabled(__name__):
                _log.debug(
                    "drag.update_wires_for_move.drop_skewed",
                    wire_id=str(wire.id),
                    path=[(p.x, p.y) for p in wire.path],
                )
            continue
        new_wires.append(wire)

    if is_debug_enabled(__name__):
        _log.debug(
            "drag.update_wires_for_move.done",
            comp_id=str(comp_id),
            wires_out=[str(w.id) for w in new_wires],
        )
    wire_list[:] = new_wires


@dataclass
class ColocatedFuseRecord:
    """Snapshot of one co-located degree-1 JointKnot merge for revert.

    Two wires whose endpoints land on the same pad but reference distinct
    degree-1 ``JointKnot``s are physically one solder joint — a common
    artifact of legacy projects from the pre-fix ``FlipWireFragmentCommand``
    (see ``memory/project_wire_id_substitution_followup.md``).
    :func:`merge_colocated_joints_capturing` unifies each such pair so the
    main collapse loop sees a proper degree-2 knot; this record lets the
    caller restore ``loser_knot`` and retarget ``moved_wire``'s endpoint
    back to it on undo.
    """

    winner_knot_id: uuid.UUID
    loser_knot_id: uuid.UUID
    loser_position: Point
    moved_wire_id: uuid.UUID
    moved_endpoint: str  # "a" or "b"


def merge_colocated_joints_capturing(
    layout: Layout,
    *,
    scope_wire_ids: frozenset[uuid.UUID] | None = None,
) -> list[ColocatedFuseRecord]:
    """Merge degree-1 ``JointKnot`` pairs sitting on the same pad, undo-aware.

    Unlike :func:`merge_colocated_joints` (which fuses per-bucket and returns
    only a count), this captures a :class:`ColocatedFuseRecord` per merge so
    the caller can roll each one back. For every cluster of co-located
    degree-1 joints the lex-smallest knot id wins; each other knot's wire is
    retargeted to the winner and the loser knot is dropped.

    ``scope_wire_ids``: when given, only pairs whose BOTH wires are in the set
    are merged. Pairs that disagree on ``side`` or ``insulated`` are physically
    distinct conductors and stay separate.
    """
    records: list[ColocatedFuseRecord] = []
    # Bucket degree-1 JointKnots by snap position.
    by_pos: dict[tuple[float, float], list[uuid.UUID]] = defaultdict(list)
    for knot in list(layout.knots.values()):
        if not isinstance(knot, JointKnot):
            continue
        if len(knot.wire_ids) != 1:
            continue
        key = rounded_point(knot.position)
        by_pos[key].append(knot.id)

    for pos_key, knot_ids in by_pos.items():
        if len(knot_ids) < 2:
            continue
        # Sort for determinism — winner is the lex-smallest id.
        knot_ids_sorted = sorted(knot_ids, key=lambda k: str(k))
        winner_id = knot_ids_sorted[0]
        winner_knot = layout.knots.get(winner_id)
        if not isinstance(winner_knot, JointKnot):
            continue
        winner_wire_id = next(iter(winner_knot.wire_ids))
        winner_wire = layout.wires.get(winner_wire_id)
        if winner_wire is None:
            continue
        # Try to absorb each loser into the winner.
        for loser_id in knot_ids_sorted[1:]:
            loser_knot = layout.knots.get(loser_id)
            if not isinstance(loser_knot, JointKnot):
                continue
            if len(loser_knot.wire_ids) != 1:
                continue
            loser_wire_id = next(iter(loser_knot.wire_ids))
            loser_wire = layout.wires.get(loser_wire_id)
            if loser_wire is None:
                continue
            # Scope filter — both wires must be in the caller-supplied set
            # (when one was given). Same contract as the main collapse pass.
            if scope_wire_ids is not None and not (
                winner_wire_id in scope_wire_ids and loser_wire_id in scope_wire_ids
            ):
                continue
            # Side / insulated agreement. Different side means the joints are
            # on different layers (not the same solder joint), and mismatched
            # insulated means physically incompatible conductors.
            if winner_wire.side != loser_wire.side:
                continue
            if winner_wire.insulated != loser_wire.insulated:
                continue
            # Find which endpoint of loser_wire references loser_id.
            moved_endpoint: str | None = None
            if loser_wire.a is not None and loser_wire.a.id == loser_id:
                moved_endpoint = "a"
            elif loser_wire.b is not None and loser_wire.b.id == loser_id:
                moved_endpoint = "b"
            if moved_endpoint is None:
                continue
            # Retarget loser_wire's endpoint to the winner knot.
            if moved_endpoint == "a":
                loser_wire.a = KnotRef(id=winner_id)
            else:
                loser_wire.b = KnotRef(id=winner_id)
            # Update winner's wire_ids to include the moved wire. Keep the
            # rebuilt knot in a local so the next loop iteration sees the
            # updated wire_ids (re-fetching from layout.knots widens to Knot).
            winner_knot = winner_knot.with_wire_ids(
                winner_knot.wire_ids | {loser_wire_id}
            )
            layout.knots[winner_id] = winner_knot
            # Drop the loser knot.
            del layout.knots[loser_id]
            records.append(
                ColocatedFuseRecord(
                    winner_knot_id=winner_id,
                    loser_knot_id=loser_id,
                    loser_position=loser_knot.position,
                    moved_wire_id=loser_wire_id,
                    moved_endpoint=moved_endpoint,
                )
            )
            _log.info(
                "wire.fuse.colocated_merged",
                pos=pos_key,
                winner=str(winner_id)[:8],
                loser=str(loser_id)[:8],
                moved_wire=str(loser_wire_id)[:8],
                moved_endpoint=moved_endpoint,
            )
    return records


def restore_colocated_fuse(layout: Layout, rec: ColocatedFuseRecord) -> None:
    """Inverse of one :func:`merge_colocated_joints_capturing` merge.

    Revives the loser ``JointKnot`` at its original position with the moved
    wire as its sole incident, removes the moved wire from the winner's
    ``wire_ids``, and re-points the wire's endpoint back to the loser knot.
    """
    layout.knots[rec.loser_knot_id] = JointKnot(
        id=rec.loser_knot_id,
        position=rec.loser_position,
        wire_ids=frozenset({rec.moved_wire_id}),
    )
    winner = layout.knots.get(rec.winner_knot_id)
    if isinstance(winner, JointKnot):
        layout.knots[rec.winner_knot_id] = JointKnot(
            id=winner.id,
            position=winner.position,
            wire_ids=winner.wire_ids - {rec.moved_wire_id},
        )
    moved_wire = layout.wires.get(rec.moved_wire_id)
    if moved_wire is not None:
        if rec.moved_endpoint == "a":
            moved_wire.a = KnotRef(id=rec.loser_knot_id)
        else:
            moved_wire.b = KnotRef(id=rec.loser_knot_id)


def snapshot_wire_geometry(
    layout: Layout,
    wire_ids: Iterable[uuid.UUID] | None = None,
) -> dict[uuid.UUID, tuple[list[Point], list[Fragment]]]:
    """Snapshot ``{wire_id: (path, fragments)}`` for undo.

    Captures the path + fragment list of each wire so a command's revert can
    restore the geometry verbatim (via :func:`restore_wire_geometry`).  Pass
    ``wire_ids`` to snapshot a subset (e.g. a drag's internal wires); omit it
    to snapshot every wire on the layout.  Missing ids are skipped.

    Single source of truth for the ``{wid: path}`` + ``{wid: fragments}``
    snapshot that ``TranslateAnchoredWiresCommand`` / ``MoveDragSetCommand``
    previously inlined identically.
    """
    if wire_ids is None:
        wires = list(layout.wires)
    else:
        wires = [w for wid in wire_ids if (w := layout.wires.get(wid)) is not None]
    return {
        w.id: (list(w.path), list(w.fragments) if w.fragments else []) for w in wires
    }


def restore_wire_geometry(
    layout: Layout,
    saved: dict[uuid.UUID, tuple[list[Point], list[Fragment]]],
) -> None:
    """Restore wire path + fragments from a :func:`snapshot_wire_geometry`.

    Wires that have since vanished from the layout are skipped (a sibling
    command may have removed them mid-macro).
    """
    for wid, (path, fragments) in saved.items():
        wire = layout.wires.get(wid)
        if wire is None:
            continue
        wire.path = list(path)
        wire.fragments = list(fragments)
