# SPDX-License-Identifier: MIT
"""Post-hoc cost analyzer for placed wires.

Walks one-or-more :class:`Wire` instances and returns a per-category
breakdown of routing costs as the autorouter's cost model would have
charged for them.  The breakdown is intended for inspection in the UI —
"why did this net cost what it did?"  Covers:

- Orthogonal / diagonal steps (charged at ``free_pad`` × ``diagonal_multiplier``)
- Bends 45° / 90° / 135° (granular knobs with ``direction_change`` fallback)
- Layer changes (vias on plated-through pads)
- Under-component-body penalty
- Wire crossings: all 8 granular knobs (insulated × {bare, insulated}) ×
  ({at_pad, off_pad}) × ({same_side, opposite_side}) plus the legacy
  ``crossing_bare_bare`` ban with its ``crossing_bare_bare_diagonal``
  perpendicular exception.

Crossing detection requires the full ``all_board_wires`` list (every wire
on the board, not just the analysed subset) so that crossings with
wires *outside* the selection are still charged.  When that list is
missing, crossings are silently skipped.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import PadAtom, StripAtom
from protoboard.domain.board.pad_state import (
    BareWireCrossing,
    BareWireItem,
    InsulatedWireCrossing,
    InsulatedWireItem,
    Pad,
    PadIndex,
)
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.pad_conflict import footprint_of
from protoboard.domain.layout.placement import component_bbox
from protoboard.domain.layout.wire import Wire
from protoboard.domain.layout.wire_utils import (
    plated_through_pads as board_plated_through_pads,
)
from protoboard.domain.routing.cost_model import RoutingCostModel
from protoboard.geometry import Point
from protoboard.services.routing.astar import (
    _intersection_point,
    _is_at_pad,
    _is_perpendicular_diagonal_crossing,
    _segs_cross,
)


@dataclass
class CostCategory:
    """One row in the breakdown table."""

    key: str  # canonical category name, e.g. "ortho_step"
    label: str  # human-readable label for the UI
    count: int  # how many of these events occurred
    unit_cost: float  # cost charged per event
    total: float  # count × unit_cost (precomputed)


@dataclass
class CostBreakdown:
    """Aggregate routing cost for a set of wires."""

    categories: list[CostCategory] = field(default_factory=list)
    total: float = 0.0
    wire_count: int = 0
    step_count: int = 0  # total ortho + diagonal steps across all wires

    def as_dict(self) -> dict[str, float]:
        """Flat ``{category_key: total}`` dict for sparse comparisons."""
        return {c.key: c.total for c in self.categories}


# ---------------------------------------------------------------------------
# Internal classifiers
# ---------------------------------------------------------------------------

_AXIS_EPS = 1e-9


def _step_kind(a: Point, b: Point) -> str | None:
    """Classify a single grid step.

    Returns ``"ortho"`` for a unit horizontal/vertical move, ``"diagonal"``
    for a 45° unit diagonal, or ``None`` for anything else (zero-length
    segment, multi-cell hop, mid-cell teleport).  The autorouter only
    produces unit steps, but post-hoc analysis runs on user-editable
    wires that may contain longer segments — we count those proportionally
    by splitting them into virtual unit steps below.
    """
    dx = b.x - a.x
    dy = b.y - a.y
    adx = abs(dx)
    ady = abs(dy)
    if adx <= _AXIS_EPS and ady <= _AXIS_EPS:
        return None
    if adx <= _AXIS_EPS or ady <= _AXIS_EPS:
        return "ortho"
    if abs(adx - ady) <= _AXIS_EPS:
        return "diagonal"
    return None  # unusual geometry — skip


def _segment_unit_steps(a: Point, b: Point) -> list[tuple[Point, Point]]:
    """Decompose a possibly multi-cell segment into unit-step pairs.

    For an N-cell ortho segment, returns N consecutive unit pairs.  For
    diagonals likewise.  Mixed segments (rare) fall back to a single pair
    so they're counted once.
    """
    kind = _step_kind(a, b)
    if kind is None:
        return [(a, b)]
    span = b - a  # Point − Point → Vector
    n_steps = int(round(max(abs(span.dx), abs(span.dy))))
    if n_steps <= 1:
        return [(a, b)]
    step = span / n_steps  # per-unit displacement
    out: list[tuple[Point, Point]] = []
    prev = a
    for i in range(1, n_steps + 1):
        nxt = a + step * i
        out.append((prev, nxt))
        prev = nxt
    return out


def _bend_kind(prev: tuple[float, float], cur: tuple[float, float]) -> str | None:
    """Classify the angle change between two consecutive direction vectors.

    Direction vectors are unit-normalised (-1, 0, 1) tuples.  Returns one
    of ``"bend_45"``, ``"bend_90"``, ``"bend_135"`` or ``None`` (no bend,
    or 180° reversal which the router shouldn't produce but we ignore).
    """
    px, py = prev
    cx, cy = cur
    if (px, py) == (cx, cy):
        return None
    # Both unit-length in [-1, 0, 1]; dot product yields one of {-1, 0, 1, ±sqrt(0.5)}
    # depending on whether moves are ortho×ortho, ortho×diag, diag×diag.
    dot = px * cx + py * cy
    mag = math.hypot(px, py) * math.hypot(cx, cy)
    if mag == 0:
        return None
    cos_angle = max(-1.0, min(1.0, dot / mag))
    angle_deg = math.degrees(math.acos(cos_angle))
    if angle_deg < 22.5:
        return None  # straight
    if angle_deg < 67.5:
        return "bend_45"
    if angle_deg < 112.5:
        return "bend_90"
    if angle_deg < 157.5:
        return "bend_135"
    return None  # 180° — silently ignored


def _unit_direction(a: Point, b: Point) -> tuple[float, float]:
    span = b - a  # Point − Point → Vector
    n = span.norm()
    if n == 0:
        return (0.0, 0.0)
    return (round(span.dx / n), round(span.dy / n))


def _under_component_pads(
    board: Board,
    layout: Layout,
    components: dict[str, ComponentDefinition],
    instances: dict[uuid.UUID, ComponentInstance],
) -> set[Point]:
    """Return all (top-layer) pad positions that sit under a placed component body.

    Used for ``under_component_top`` accounting — the autorouter penalises
    routing through these cells on the top layer.  Bottom-mounted components
    are skipped (mirror of the autorouter's behaviour).
    """
    out: set[Point] = set()
    for comp_id, placement in layout.placements.items():
        inst = instances.get(comp_id)
        if inst is None:
            continue
        defn = components.get(inst.definition_ref)
        if defn is None or placement.side != "top":
            continue
        bbox = component_bbox(footprint_of(defn, inst), placement)
        for atom, pos in board.atoms():
            if isinstance(atom, (PadAtom, StripAtom)) and bbox.contains(pos):
                out.add(pos)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Friendly labels for the UI table.  Keys ending in a cost_model knob name
# (e.g. ``crossing_ii_off_pad_opposite_side``) are kept verbatim so users
# tuning the model can see which knob controls which category at a glance.
_LABELS: dict[str, str] = {
    "ortho_step": "Orthogonal step",
    "diagonal_step": "Diagonal step",
    "bend_45": "Bend 45°",
    "bend_90": "Bend 90°",
    "bend_135": "Bend 135°",
    "direction_change": "Direction change",
    "layer_change": "Layer change (via)",
    "under_component_top": "Under-component pad",
    "crossing_bare_bare": "Crossing bare × bare (banned)",
    "crossing_bare_bare_diagonal": "Crossing bare × bare (perp. diag)",
    "crossing_ib_at_pad_same_side": "Crossing ins × bare, at pad, same side",
    "crossing_ib_at_pad_opposite_side": "Crossing ins × bare, at pad, opposite side",
    "crossing_ib_off_pad_same_side": "Crossing ins × bare, off pad, same side",
    "crossing_ib_off_pad_opposite_side": "Crossing ins × bare, off pad, opposite side",
    "crossing_ii_at_pad_same_side": "Crossing ins × ins, at pad, same side",
    "crossing_ii_at_pad_opposite_side": "Crossing ins × ins, at pad, opposite side",
    "crossing_ii_off_pad_same_side": "Crossing ins × ins, off pad, same side",
    "crossing_ii_off_pad_opposite_side": "Crossing ins × ins, off pad, opposite side",
}


# Counter type for the crossing tally: knob_key → count.
_CrossCounter = dict[str, int]


# Wire-bearing pad-state item kinds recognised by the analyzer.
# (Kept as a plain runtime tuple for ``isinstance`` filtering — a
# union type alias here trips Pyright when used as an annotation.)
_WIRE_ITEM_TYPES = (
    BareWireItem,
    BareWireCrossing,
    InsulatedWireItem,
    InsulatedWireCrossing,
)


def _wire_item_is_insulated(item: object) -> bool:
    return isinstance(item, (InsulatedWireItem, InsulatedWireCrossing))


def _count_at_pad_crossings_via_pad_index(
    pad_index: PadIndex,
    analysed_ids: set[uuid.UUID],
    all_board_ids: set[uuid.UUID],
) -> _CrossCounter:
    """At-pad crossings derived from ``PadIndex`` occupancy.

    For each pad, iterate the wire items recorded on each side.  Pairs of
    items on the *same* side classify as same-side at-pad crossings;
    pairs (top × bottom) classify as opposite-side at-pad crossings.
    Items belonging to the same wire (same id) are skipped — a wire
    cannot cross itself.  Pairs are de-duplicated per pad so a wire that
    both has a vertex *and* passes through the same pad doesn't
    double-count against the other wire.

    The function only registers crossings where at least one of the two
    wires is in ``analysed_ids`` (so we attribute the cost to the
    analysed set) and the other is in ``all_board_ids`` (so we don't
    count phantom wires).  Pairs entirely within ``analysed_ids`` are
    counted once per pad-pair (dedup via ``frozenset``).
    """
    counter: _CrossCounter = {}

    def _wire_items_on(
        pad: Pad, side: str
    ) -> list[
        BareWireItem | BareWireCrossing | InsulatedWireItem | InsulatedWireCrossing
    ]:
        out: list[
            BareWireItem | BareWireCrossing | InsulatedWireItem | InsulatedWireCrossing
        ] = []
        seen_ids: set[uuid.UUID] = set()
        for it in pad.physically_blocked_on(side):
            if not isinstance(it, _WIRE_ITEM_TYPES):
                continue
            if it.id in seen_ids:
                # A wire that has both a vertex and a crossing recorded
                # on the same pad-side counts as one occupant for
                # crossing-classification purposes.
                continue
            seen_ids.add(it.id)
            out.append(it)
        return out

    # Only iterate pads that actually have at least one wire recorded —
    # `iter_pads()` walks every pad on the board (thousands on a large
    # perfboard) just to skip the empty ones.  `iter_pads_with_wires`
    # is bounded by O(total wire occupancy) instead of O(board area).
    for pad in pad_index.iter_pads_with_wires():
        top_items = _wire_items_on(pad, "top")
        bot_items = _wire_items_on(pad, "bottom")
        if not top_items and not bot_items:
            continue

        # Helper: register a single crossing pair classified by side relation.
        # ``a`` and ``b`` are pad_state.Item subclass instances (filtered to
        # wire kinds above), so both expose ``.id`` and ``.net`` attributes.
        def _register(a, b, *, opposite: bool) -> None:  # type: ignore[no-untyped-def]
            if a.id == b.id:
                return
            # Attribute only when at least one side of the pair is in the
            # analysed set; otherwise the cost belongs to a different
            # selection and we ignore it.
            if a.id not in analysed_ids and b.id not in analysed_ids:
                return
            if a.id not in all_board_ids or b.id not in all_board_ids:
                return
            a_ins = _wire_item_is_insulated(a)
            b_ins = _wire_item_is_insulated(b)
            if not a_ins and not b_ins:
                # Bare × bare — only meaningful on the same side; opposite
                # bare wires on the two layers physically cannot short.
                if opposite:
                    return
                # The bare×bare ban is the legacy knob; classify as such
                # (perpendicular-diagonal exception is geometric, handled
                # in the off-pad path).
                key = "crossing_bare_bare"
            elif a_ins and b_ins:
                key = (
                    "crossing_ii_at_pad_opposite_side"
                    if opposite
                    else "crossing_ii_at_pad_same_side"
                )
            else:
                key = (
                    "crossing_ib_at_pad_opposite_side"
                    if opposite
                    else "crossing_ib_at_pad_same_side"
                )
            counter[key] = counter.get(key, 0) + 1

        # Same-side pairs on top.
        for i in range(len(top_items)):
            for j in range(i + 1, len(top_items)):
                _register(top_items[i], top_items[j], opposite=False)
        # Same-side pairs on bottom.
        for i in range(len(bot_items)):
            for j in range(i + 1, len(bot_items)):
                _register(bot_items[i], bot_items[j], opposite=False)
        # Opposite-side pairs (top × bottom).
        for a in top_items:
            for b in bot_items:
                _register(a, b, opposite=True)

    return counter


def _point_on_segment_interior(
    p: Point,
    a: Point,
    b: Point,
    *,
    eps: float = 1e-9,
) -> bool:
    """True if *p* lies strictly between *a* and *b* (collinear + interior).

    Excludes endpoints (``a`` and ``b``).  Used to detect "wire X passes
    through wire Y's bend point" cases that ``_segs_cross`` rejects:
    when two wires meet at a vertex of one of them, the vertex sits on
    the segment endpoint, not its interior.
    """
    # Collinearity: cross product (b-a) × (p-a) must be zero.
    ab = b - a
    ap = p - a
    if abs(ab.cross(ap)) > eps:
        return False
    # Collinear, so t = (ap · ab) / |ab|² is the projection parameter
    # (degenerate ab guarded by the near-zero length check).
    ab_sq = ab.dot(ab)
    if ab_sq == 0:
        return False
    t = ap.dot(ab) / ab_sq
    return eps < t < 1.0 - eps


def _classify_crossing(
    our_seg: tuple[Point, Point, str, bool],
    other_seg: tuple[Point, Point, str, bool],
    cost_model: RoutingCostModel,
) -> str | None:
    """Identify which cost-model knob a crossing falls under, or None.

    Each segment is ``(p1, p2, side, insulated)``.  The function does not
    do geometric intersection (caller pre-filters with ``_segs_cross``);
    it only classifies the crossing kind and returns the knob key that
    the autorouter would charge.  Returns ``None`` when the crossing
    falls into a bucket that the cost model handles for free (e.g. two
    insulated wires crossing on the same side when the corresponding
    knob is zero — still tracked, but with knob_key for the row).
    """
    p1, p2, side_a, ins_a = our_seg
    q1, q2, side_b, ins_b = other_seg

    same_side = side_a == side_b

    # Bare × bare: legacy ban with a single perpendicular-diagonal escape.
    if not ins_a and not ins_b:
        if not same_side:
            return None  # bare wires on opposite layers never short
        if _is_perpendicular_diagonal_crossing(p1, p2, q1, q2):
            return "crossing_bare_bare_diagonal"
        return "crossing_bare_bare"

    # At-pad vs off-pad classification needs the geometric intersection.
    ix, iy = _intersection_point(p1, p2, q1, q2)
    at_pad = _is_at_pad(ix, iy)

    if ins_a and ins_b:
        # Insulated × insulated.
        if same_side:
            return (
                "crossing_ii_at_pad_same_side"
                if at_pad
                else "crossing_ii_off_pad_same_side"
            )
        return (
            "crossing_ii_at_pad_opposite_side"
            if at_pad
            else "crossing_ii_off_pad_opposite_side"
        )

    # Insulated × bare (one side or the other).
    if same_side:
        return (
            "crossing_ib_at_pad_same_side"
            if at_pad
            else "crossing_ib_off_pad_same_side"
        )
    return (
        "crossing_ib_at_pad_opposite_side"
        if at_pad
        else "crossing_ib_off_pad_opposite_side"
    )


def _count_crossings(
    analysed: list[Wire],
    all_board_wires: list[Wire],
    cost_model: RoutingCostModel,
    pad_index: PadIndex | None = None,
) -> _CrossCounter:
    """Tally crossings between *analysed* wires and the rest of the board.

    Two-stage detection:

    1. **At-pad crossings** (intersection point coincides with a pad
       centre) — derived from ``PadIndex`` occupancy when available.
       This catches every wire that has a vertex *or* mid-segment pass
       through any pad, including the bend-on-pad case that pure
       geometric ``_segs_cross`` rejects (endpoints don't count as
       interior intersections).
    2. **Off-pad crossings** (intersection between pads, mesh-cell
       centres) — geometric ``_segs_cross`` on merged directional
       segments.  PadIndex doesn't index these because they're not at
       pad positions.

    Returns a dict ``{knob_key: count}``.
    """
    counter: _CrossCounter = {}
    analysed_ids = {w.id for w in analysed}
    all_board_ids = {w.id for w in all_board_wires}

    # ── Stage 1: at-pad crossings via PadIndex ─────────────────────
    # When PadIndex is available, delegate every at-pad crossing to it.
    # PadIndex already knows which wires occupy which side of each pad,
    # so we get a count without any geometric intersection guesswork.
    # Falls through to the geometric path when PadIndex isn't supplied
    # (back-compat for callers that don't have a session/PadIndex
    # around).
    if pad_index is not None:
        counter.update(
            _count_at_pad_crossings_via_pad_index(
                pad_index,
                analysed_ids,
                all_board_ids,
            )
        )

    # For cross-check we want *maximal straight segments* (consecutive
    # collinear path points merged), not unit steps.  ``_segs_cross``
    # uses strict-interior semantics: a unit step ending at the same
    # cell as the next wire's unit step would never register as a
    # crossing, even when the two wires geometrically cross at that
    # point.  Merging collinear runs first sidesteps the issue.
    def _merged_segments(w: Wire) -> list[tuple[Point, Point, str, bool]]:
        out: list[tuple[Point, Point, str, bool]] = []
        if len(w.path) < 2:
            return out
        start = w.path[0]
        prev_dir: tuple[float, float] | None = None
        for i in range(1, len(w.path)):
            cur_dir = _unit_direction(w.path[i - 1], w.path[i])
            if prev_dir is not None and cur_dir != prev_dir:
                # Direction changed — emit the run that ended at path[i-1].
                out.append((start, w.path[i - 1], w.side, w.insulated))
                start = w.path[i - 1]
            prev_dir = cur_dir
        # Tail run.
        out.append((start, w.path[-1], w.side, w.insulated))
        return out

    # Cache merged-segment lists per wire so we don't recompute when the
    # outer loop revisits a wire from a different pairing.
    cache: dict[uuid.UUID, list[tuple[Point, Point, str, bool]]] = {}

    def _steps_for(w: Wire) -> list[tuple[Point, Point, str, bool]]:
        v = cache.get(w.id)
        if v is None:
            v = _merged_segments(w)
            cache[w.id] = v
        return v

    seen_pairs: set[frozenset[uuid.UUID]] = set()

    for our in analysed:
        our_steps = _steps_for(our)
        for other in all_board_wires:
            if other.id == our.id:
                continue
            if other.id in analysed_ids:
                key = frozenset({our.id, other.id})
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
            other_steps = _steps_for(other)
            # Per-wire-pair dedup of crossings that fall exactly on the bend
            # point of one wire — both adjacent segments would otherwise
            # report the same physical crossing (entry and exit at the bend).
            pair_seen_points: set[tuple[float, float]] = set()
            for op1, op2, oside, oins in our_steps:
                ox_lo = op1.x if op1.x < op2.x else op2.x
                ox_hi = op2.x if op1.x < op2.x else op1.x
                oy_lo = op1.y if op1.y < op2.y else op2.y
                oy_hi = op2.y if op1.y < op2.y else op1.y
                for q1, q2, qside, qins in other_steps:
                    qx_lo = q1.x if q1.x < q2.x else q2.x
                    qx_hi = q2.x if q1.x < q2.x else q1.x
                    qy_lo = q1.y if q1.y < q2.y else q2.y
                    qy_hi = q2.y if q1.y < q2.y else q1.y
                    if qx_hi < ox_lo or qx_lo > ox_hi or qy_hi < oy_lo or qy_lo > oy_hi:
                        continue
                    crosses_strict = _segs_cross(op1, op2, q1, q2)
                    crossing_pt: tuple[float, float] | None = None
                    if crosses_strict:
                        ix, iy = _intersection_point(op1, op2, q1, q2)
                        crossing_pt = (ix, iy)
                    else:
                        # Touchpoint test — does one segment pass through
                        # an endpoint that is the *bend* of the other?
                        # Excluded: pure endpoint-of-both touches (wires
                        # meeting at a pad they both genuinely end on — not
                        # a crossing, that's a connection point).  We check
                        # both directions because either wire could be the
                        # one with a bend on the shared point.
                        for cand in (q1, q2):
                            if _point_on_segment_interior(cand, op1, op2):
                                crossing_pt = (cand.x, cand.y)
                                break
                        if crossing_pt is None:
                            for cand in (op1, op2):
                                if _point_on_segment_interior(cand, q1, q2):
                                    crossing_pt = (cand.x, cand.y)
                                    break
                    if crossing_pt is None:
                        continue
                    if crossing_pt in pair_seen_points:
                        continue
                    pair_seen_points.add(crossing_pt)
                    # When PadIndex handled stage 1, skip at-pad crossings
                    # in the geometric path — otherwise they'd double-count.
                    # `_is_at_pad` reads half-integer coordinates as a pad
                    # centre (col+0.5, row+0.5).
                    if pad_index is not None and _is_at_pad(*crossing_pt):
                        continue
                    key_knob = _classify_crossing(
                        (op1, op2, oside, oins),
                        (q1, q2, qside, qins),
                        cost_model,
                    )
                    if key_knob is None:
                        continue
                    counter[key_knob] = counter.get(key_knob, 0) + 1
    return counter


def analyze_wires(
    wires: Iterable[Wire],
    *,
    board: Board | None = None,
    layout: Layout | None = None,
    cost_model: RoutingCostModel,
    components: dict[str, ComponentDefinition] | None = None,
    instances: dict[uuid.UUID, ComponentInstance] | None = None,
    under_component_pads: set[Point] | None = None,
    plated_through_pads: set[Point] | None = None,
    all_board_wires: list[Wire] | None = None,
    pad_index: PadIndex | None = None,
) -> CostBreakdown:
    """Return a cost breakdown for *wires* under *cost_model*.

    The same wire path may map to many unit steps when the user has
    drawn long straight segments by hand; we decompose each into unit
    steps so the count column matches "steps a router would have taken."

    Layer changes (vias) are charged once per pair of consecutive
    ``Wire`` objects sharing an endpoint on a plated-through pad
    but differing in ``side``.  The check is per-wire-list, so passing
    a single wire's segments still yields zero vias.

    Performance — the under-component and plated-through pad sets both
    require an O(atoms) walk of the board.  Callers that analyse many
    wire groups in sequence (e.g. NetInspectorPanel: one call per net)
    must precompute these sets *once* and pass them in.  When omitted,
    they are computed lazily from *board* + *layout* — convenient for
    one-shot callers but a perf trap when iterated.
    """
    wires_list = list(wires)
    cm = cost_model

    # Bend penalties — fall back to direction_change when the granular
    # bend_45/90/135 knobs are zero, mirroring the router's behaviour.
    dc = cm.get("direction_change", 0.0)
    b45 = cm.get("bend_45", 0.0) or dc
    b90 = cm.get("bend_90", 0.0) or dc
    b135 = cm.get("bend_135", 0.0) or dc

    free_pad = cm.get("free_pad", 1.0)
    diag_mult = cm.get("diagonal_multiplier", math.sqrt(2))
    layer_change = cm.get("layer_change", 0.0)
    under_pen = cm.get("under_component_top", 0.0)

    if under_pen <= 0.0:
        _under_set: set[Point] = set()
    elif under_component_pads is not None:
        _under_set = under_component_pads
    else:
        if board is None or layout is None:
            raise ValueError(
                "analyze_wires needs board+layout or a precomputed "
                "under_component_pads set when under_component_top > 0"
            )
        _under_set = _under_component_pads(
            board,
            layout,
            components or {},
            instances or {},
        )

    if layer_change <= 0.0:
        _plated_set: set[Point] = set()
    elif plated_through_pads is not None:
        _plated_set = plated_through_pads
    else:
        if board is None:
            raise ValueError(
                "analyze_wires needs board or a precomputed "
                "plated_through_pads set when layer_change > 0"
            )
        _plated_set = board_plated_through_pads(board)

    n_ortho = 0
    n_diag = 0
    bend_counts = {"bend_45": 0, "bend_90": 0, "bend_135": 0}
    n_under = 0
    n_layer_change = 0

    # Per-wire walk; bends reset at wire boundaries (a new wire starts
    # with no prior direction, so its first step never charges a bend).
    for w in wires_list:
        prev_dir: tuple[float, float] | None = None
        for i in range(len(w.path) - 1):
            a = w.path[i]
            b = w.path[i + 1]
            for ua, ub in _segment_unit_steps(a, b):
                kind = _step_kind(ua, ub)
                if kind is None:
                    continue
                if kind == "ortho":
                    n_ortho += 1
                elif kind == "diagonal":
                    n_diag += 1
                # Bend detection runs on the unit-step vectors so that a
                # long straight segment doesn't get charged a bend at
                # every intermediate cell.
                cur_dir = _unit_direction(ua, ub)
                if prev_dir is not None:
                    bk = _bend_kind(prev_dir, cur_dir)
                    if bk is not None:
                        bend_counts[bk] += 1
                prev_dir = cur_dir
                # Under-component penalty: pay it when stepping onto a
                # pad under a top-mounted component body (top layer only).
                if w.side == "top" and ub in _under_set:
                    n_under += 1

    # Layer changes: pairs of consecutive wires sharing an endpoint on a
    # plated-through pad but on different sides.  We pair them by exact
    # endpoint match (the autorouter splits a multi-layer path into one
    # Wire per layer at the via).
    if _plated_set and len(wires_list) >= 2:
        # Build endpoint→(side, wire) index.
        endpoints: dict[Point, list[tuple[str, Wire]]] = {}
        for w in wires_list:
            for ep in (w.path[0], w.path[-1]):
                endpoints.setdefault(ep, []).append((w.side, w))
        for pt, entries in endpoints.items():
            if pt not in _plated_set or len(entries) < 2:
                continue
            sides = {s for s, _ in entries}
            if "top" in sides and "bottom" in sides:
                # Count the via once per distinct top/bottom transition
                # touching this pad (rare to have more than one).
                n_layer_change += 1

    # Cross-wire interactions: requires the full board's wire list (so
    # crossings with wires *outside* the analysed selection are still
    # counted).  When the caller didn't supply it, fall back to
    # ``layout.wires`` if a layout is available — keeps the one-shot
    # convenience path working.  When neither is available, crossings
    # are silently skipped.
    cross_counter: _CrossCounter = {}
    board_wires_for_cross: list[Wire] | None = all_board_wires
    if board_wires_for_cross is None and layout is not None:
        board_wires_for_cross = list(layout.wires)
    if board_wires_for_cross is not None and wires_list:
        cross_counter = _count_crossings(
            wires_list,
            board_wires_for_cross,
            cm,
            pad_index=pad_index,
        )

    cats: list[CostCategory] = []

    def _add(
        key: str, count: int, unit: float, *, keep_when_zero_unit: bool = False
    ) -> None:
        if count == 0:
            return
        # Track infinite unit cost specially — keep total as math.inf
        # so users tuning a forbidden-crossing knob see the row in the
        # breakdown table (with "∞") instead of having it silently
        # disappear via the zero-unit guard.
        if math.isinf(unit):
            total_val = math.inf
        elif unit == 0.0:
            if not keep_when_zero_unit:
                # No-op knob: don't waste a table row.
                return
            total_val = 0.0
        else:
            total_val = count * unit
        cats.append(
            CostCategory(
                key=key,
                label=_LABELS.get(key, key),
                count=count,
                unit_cost=unit,
                total=total_val,
            )
        )

    _add("ortho_step", n_ortho, free_pad)
    _add("diagonal_step", n_diag, free_pad * diag_mult)
    _add("bend_45", bend_counts["bend_45"], b45)
    _add("bend_90", bend_counts["bend_90"], b90)
    _add("bend_135", bend_counts["bend_135"], b135)
    _add("layer_change", n_layer_change, layer_change)
    _add("under_component_top", n_under, under_pen)

    # Crossings — emit one row per detected crossing kind, even when the
    # corresponding knob is currently zero.  Tuning workflow: the user
    # needs to see what crossings exist BEFORE assigning costs to them,
    # otherwise the table hides exactly the rows they want to tune.
    for knob_key in sorted(cross_counter):
        _add(
            knob_key,
            cross_counter[knob_key],
            cm.get(knob_key, 0.0),
            keep_when_zero_unit=True,
        )

    total = sum(c.total for c in cats)
    return CostBreakdown(
        categories=cats,
        total=total,
        wire_count=len(wires_list),
        step_count=n_ortho + n_diag,
    )
