# SPDX-License-Identifier: MIT
"""Pad state model — single source of truth for what is on every pad.

Replaces the scattered per-algorithm "is this pad occupied?" reasoning
(`find_wires_crossing_pads`, `find_conflicts`, simplify's safe-checks,
autoroute's pad maps, DRC) with one runtime structure (``PadIndex``)
indexed by board pad position.  Each pad answers two questions per side:

* **electrical net** — which net touches the copper on this side?
* **physically blocked** — which items (wires, pin, component body) sit
  on this side and prevent another wire from being routed through?

Propagation rules between top and bottom sides depend on the pad's
physical structure (single-sided, isolated double-sided, plated-through).
Three concrete `Pad` subclasses encapsulate those rules so algorithms
just ask the pad and never reason about board type themselves.

Items contributing to a pad's state are tracked via a discriminated
union:

* ``PinItem`` — component pin lutowany w padzie.
* ``BareWireItem`` — vertex of a bare wire (endpoint or intermediate).
* ``BareWireCrossing`` — bare wire passes through this pad as part of a
  segment that does NOT terminate here.  Distinguishes "wire ends at
  this pad" from "wire lies across this pad" — both are electrical
  contact for bare wires but only the first is also a vertex.
* ``InsulatedWireItem`` — endpoint of an insulated wire (electrical
  contact at solder joint).
* ``InsulatedWireCrossing`` — interior vertex of an insulated wire
  (physical blocker only; the wire floats above the board at this point
  but cannot share the pad with another insulated wire).
* ``ComponentBody`` — bbox of a component covers this pad on this side
  (physical blocker only; no electrical contact).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import ClassVar, Literal, TypeAlias

from protoboard.domain.board.layer import PHYSICAL_LAYERS, LayerId
from protoboard.domain.board.pads import PadLayers
from protoboard.geometry import Point

NetId = uuid.UUID

# ``Side`` is the historical alias for "which copper layer". Kept as a
# type alias for backward compatibility — every signature using ``side``
# accepts an arbitrary ``LayerId`` now, but the runtime values in MVP
# are still ``"top"`` / ``"bottom"`` (`PHYSICAL_LAYERS`). Future kapton
# layers will use ids like ``"tape:top+1"`` — the same APIs accept them
# without changes.
Side: TypeAlias = LayerId
_SIDES: tuple[LayerId, LayerId] = PHYSICAL_LAYERS

# Sentinel returned by ``Pad.electrical_net_on`` when more than one distinct
# non-None net touches the same side at the same time.  Algorithms (DRC
# above all) treat this as a short circuit.
SHORT_CIRCUIT: object = object()


# ---------------------------------------------------------------------------
# Item discriminated union
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PinItem:
    """A component pin soldered into the pad — through-hole contact."""

    id: uuid.UUID            # component instance id
    pin_number: int
    net: NetId | None
    kind: ClassVar[str] = "pin"


@dataclass(frozen=True, slots=True)
class WirePinItem:
    """A wire-pin (virtual goldpin) soldered into the pad.

    Electrically and physically equivalent to :class:`PinItem` — the
    user solidered a wire endpoint as a deliberate terminal.  Pierces
    the hole, propagates the net to every physical layer (rule 5),
    blocks foreign wire interiors over the pad (rule 6).

    Distinguished from ``PinItem`` only so callers that care about
    component identity (e.g. labels, refdes, group exports) can
    skip it.  See ``docs/archive/wire-pin-implementation.md``.
    """

    id: uuid.UUID            # WirePinKnot id
    net: NetId | None
    kind: ClassVar[str] = "wire_pin"


@dataclass(frozen=True, slots=True)
class BareWireItem:
    """A bare wire's path vertex (endpoint or intermediate) on this pad."""

    id: uuid.UUID            # wire id
    net: NetId | None
    kind: ClassVar[str] = "bare_wire"


@dataclass(frozen=True, slots=True)
class BareWireCrossing:
    """A bare wire passes THROUGH this pad mid-segment (no vertex here).

    The wire's recorded path skips this pad, but the conductor physically
    rests on it — bare wires merge electrically with every pad they
    cross.  Simplify and autoroute distinguish this from ``BareWireItem``
    so they know that the wire's recorded vertices can be edited without
    losing the implicit pad contact (the contact will simply be
    re-detected from the new path).
    """

    id: uuid.UUID
    net: NetId | None
    kind: ClassVar[str] = "bare_crossing"


@dataclass(frozen=True, slots=True)
class InsulatedWireItem:
    """An insulated wire's endpoint soldered to this pad."""

    id: uuid.UUID
    net: NetId | None
    kind: ClassVar[str] = "insulated_wire"


@dataclass(frozen=True, slots=True)
class InsulatedWireCrossing:
    """An interior vertex of an insulated wire passes over this pad.

    Physical blocker only — the wire floats above the board at this point.
    No electrical contact, but a second insulated wire cannot share the
    same overhead path (would tangle physically).
    """

    id: uuid.UUID
    net: NetId | None
    kind: ClassVar[str] = "insulated_crossing"


@dataclass(frozen=True, slots=True)
class ComponentBody:
    """The bbox of a component covers this pad on this side.

    ``blocks_routing`` controls whether a wire may pass through this pad.
    Most through-hole components sit on standoffs / leads and leave a gap
    between body and PCB, so wires (insulated *and* bare on the free pad)
    can route under them — sometimes deliberately, with the component
    acting as a jumper.  SMD and tight-fit packages override this via
    the per-component definition flag.

    Values:
      * ``False`` (default) — body presence is informational; routing is
        allowed.  Autoroute may still soft-penalise via ``under_component_top``.
      * ``True`` — body is a hard block on the side it sits on; wires on
        that side cannot use this pad.
    """

    id: uuid.UUID            # component instance id
    blocks_routing: bool = False
    kind: ClassVar[str] = "component_body"


# Union type alias — for type hints in the rest of the codebase.
Item = (
    PinItem
    | WirePinItem
    | BareWireItem
    | BareWireCrossing
    | InsulatedWireItem
    | InsulatedWireCrossing
    | ComponentBody
)


def _item_contributes_electrically(item: Item) -> bool:
    """True if *item* contributes its net to the side it is placed on.

    Pins, wire-pins, bare wires (vertex or crossing), and insulated
    wire endpoints contact copper; insulated mid-path vertices and
    component bodies do not.
    """
    return isinstance(item, (PinItem, WirePinItem, BareWireItem,
                              BareWireCrossing, InsulatedWireItem))


def _item_is_through_hole(item: Item) -> bool:
    """True if *item* exposes a conductor in the through-hole and thus
    propagates electrically to both pad sides regardless of board type.

    Pins and wire-pins are physically pushed through the hole; a wire
    that changes sides at this pad has its conductor in the hole too.
    Bare and insulated wire endpoints sit on top of the copper ring on
    their own side only — they propagate only per board-type rules.
    """
    return isinstance(item, (PinItem, WirePinItem))


# ---------------------------------------------------------------------------
# Pad base + subclasses
# ---------------------------------------------------------------------------


@dataclass
class Pad:
    """One physical hole on the board with its current occupancy state.

    Storage is keyed by ``LayerId`` so the model accepts arbitrary
    layers (the MVP populates only ``"top"`` and ``"bottom"`` — the
    physical copper layers; the kapton tape follow-up will add
    additional entries like ``"tape:top+1"`` without changing this
    class).

    Side-propagation rules are derived from ``layers`` directly via
    :meth:`PadLayers.plated_neighbours` — no subclass dispatch is
    required. The three legacy subclasses
    (:class:`SingleSidedBoardPad`, :class:`IsolatedDoubleSidedBoardPad`,
    :class:`ConnectedDoubleSidedBoardPad`) remain as thin aliases
    purely so existing ``isinstance(...)`` checks (used in
    ``tests/unit/test_pad_state.py``) continue to type-check. They
    carry no extra behaviour and will be removed once callers stop
    asking for the specific class.
    """

    position: Point
    layers: PadLayers
    strip_group_id: int | None = None

    # Per-layer electrical net.  ``SHORT_CIRCUIT`` sentinel marks a
    # layer whose recorded sources include more than one distinct
    # non-None net. Populated lazily — only layers that have been
    # touched appear as keys; query helpers treat absence as ``None``.
    electrical_net: dict[LayerId, NetId | None | object] = field(
        default_factory=dict
    )

    # Per-layer physical occupants. Each layer keeps the list of Items
    # whose physical presence prevents another wire being routed
    # through this pad on that layer.
    physically_blocked: dict[LayerId, list[Item]] = field(
        default_factory=dict
    )

    # Internal bookkeeping: which Item id contributed which net per
    # layer. When multiple items share the same net, removing one of
    # them must not drop the net — only when the LAST contributor of
    # that net is gone. Map item.id → contributed net (or None if the
    # item only blocks physically).
    _net_sources: dict[LayerId, dict[uuid.UUID, NetId | None]] = field(
        default_factory=dict
    )

    # ------------------------------------------------------------------
    # Side-propagation hook
    # ------------------------------------------------------------------

    def _propagation(self, item: Item, source_side: LayerId) -> set[LayerId]:
        """Return the set of layers on which *item* contributes its net.

        Pin (through-hole) **always** bonds every physical side of the
        board, even ones with no copper — the metal lead pierces the
        whole stack and exposes a conductor on both faces (rule 5 of
        pad/pin/wire/junction semantics, footnote *).

        Other electrical items contribute to their own layer plus any
        layers galvanically bonded to it (``plated_through`` →
        ``BOTTOM`` is bonded to ``TOP`` and vice-versa).

        Items that don't contribute electrically (insulated crossing,
        component body) are filtered out by callers via
        :func:`_item_contributes_electrically` before reaching here.
        """
        if _item_is_through_hole(item):
            # Pin always bonds every physical layer of the board, no
            # matter which sides actually have copper. The kapton
            # follow-up will need to revisit this — a pin still
            # pierces tape over hole, but tape between two copper
            # layers blocks the lead path.
            return set(PHYSICAL_LAYERS)
        if not self.layers.has_layer(source_side):
            # A wire on the unarmed side of a single-sided pad has no
            # copper to bond to — physical presence is recorded
            # (caller invokes _physical_propagation separately) but no
            # electrical contribution.
            return set()
        return {source_side} | set(self.layers.plated_neighbours(source_side))

    # ------------------------------------------------------------------
    # Side-physical hook (which layers receive the physical blocker)
    # ------------------------------------------------------------------

    def _physical_propagation(
        self, item: Item, source_side: LayerId,
    ) -> set[LayerId]:
        """Return the set of layers on which *item* physically blocks.

        Default: a wire on layer X physically occupies layer X only.
        Pins and wire-pins are special — they pierce the board, so they
        block every physical layer (rule 5).
        """
        if isinstance(item, (PinItem, WirePinItem)):
            return set(PHYSICAL_LAYERS)
        return {source_side}

    # ------------------------------------------------------------------
    # Public properties (read-only views)
    # ------------------------------------------------------------------

    def electrical_net_on(self, side: LayerId) -> NetId | None | object:
        """Net touching the copper on *side*, ``None``, or
        ``SHORT_CIRCUIT``."""
        return self.electrical_net.get(side)

    def physically_blocked_on(self, side: LayerId) -> list[Item]:
        """Items physically occupying this side."""
        return self.physically_blocked.get(side, [])

    def is_free(self, side: LayerId) -> bool:
        """No electrical net AND no physical blockers on *side*."""
        return (
            self.electrical_net.get(side) is None
            and not self.physically_blocked.get(side)
        )

    # ------------------------------------------------------------------
    # Internal mutation API (called by PadIndex)
    # ------------------------------------------------------------------

    def _add_item(self, item: Item, source_side: LayerId) -> None:
        """Record *item* as occupying this pad, propagating per rules."""
        # Physical placement
        for s in self._physical_propagation(item, source_side):
            self.physically_blocked.setdefault(s, []).append(item)

        # Electrical contribution
        if _item_contributes_electrically(item):
            for s in self._propagation(item, source_side):
                self._net_sources.setdefault(s, {})[item.id] = (
                    item.net  # type: ignore[attr-defined]
                )
            self._recompute_electrical()

    def _remove_item(self, item_id: uuid.UUID) -> None:
        """Remove every record of *item_id* from this pad."""
        for side, items in list(self.physically_blocked.items()):
            kept = [it for it in items if it.id != item_id]
            if kept:
                self.physically_blocked[side] = kept
            else:
                del self.physically_blocked[side]
        for side, sources in list(self._net_sources.items()):
            if item_id in sources:
                del sources[item_id]
                if not sources:
                    del self._net_sources[side]
        self._recompute_electrical()

    def _recompute_electrical(self) -> None:
        """Update ``electrical_net`` from ``_net_sources``."""
        # Drop stale layer entries (e.g. all sources removed) and
        # rebuild from the current ``_net_sources`` map.
        self.electrical_net = {}
        for side, sources in self._net_sources.items():
            nets = {n for n in sources.values() if n is not None}
            if len(nets) == 1:
                self.electrical_net[side] = next(iter(nets))
            elif len(nets) > 1:
                self.electrical_net[side] = SHORT_CIRCUIT
            # No netted source — but unconnected sources (pin with no
            # net assigned, floating wire) may still mark the layer as
            # "claimed" for routing purposes. We mirror legacy
            # semantics here: electrical_net stays absent (treated as
            # ``None`` by readers).

    # ------------------------------------------------------------------
    # Iteration & misc.
    # ------------------------------------------------------------------

    def iter_items(self, side: LayerId | None = None):
        """Iterate Items on *side* (or every layer if None)."""
        if side is None:
            seen: set[uuid.UUID] = set()
            for items in self.physically_blocked.values():
                for it in items:
                    if it.id not in seen:
                        seen.add(it.id)
                        yield it
        else:
            yield from self.physically_blocked_on(side)


# ---------------------------------------------------------------------------
# Subclass aliases — kept for backward compatibility with isinstance() callers
# ---------------------------------------------------------------------------
#
# The unified :class:`Pad` derives every propagation rule from
# ``PadLayers.plated_neighbours`` — no specialised subclass logic is
# needed. The three classes below remain so existing isinstance checks
# (notably in ``tests/unit/test_pad_state.py``) continue to compile.
# Once those callers stop discriminating by class, the aliases can be
# removed in a follow-up.


@dataclass
class SingleSidedBoardPad(Pad):
    """A pad on a single-sided board.

    Deprecated discriminator — behaviour lives on :class:`Pad`. Only
    one of ``layers.top``/``layers.bottom`` is True; the other side has
    no copper. Pins still pierce both sides.
    """


@dataclass
class IsolatedDoubleSidedBoardPad(Pad):
    """A double-sided pad whose through-hole is NOT plated.

    Deprecated discriminator — behaviour lives on :class:`Pad`. Both
    top and bottom carry copper, but they are electrically independent
    until a pin or wire-side-change physically bridges the hole.
    """


@dataclass
class ConnectedDoubleSidedBoardPad(Pad):
    """A plated-through pad — top and bottom are one electrical node.

    Deprecated discriminator — behaviour lives on :class:`Pad`. The
    galvanic bond between sides is encoded in
    ``PadLayers.plated_through``.
    """


# ---------------------------------------------------------------------------
# PadIndex — runtime index of all pads on the board with their occupancy
# ---------------------------------------------------------------------------

import math
from collections import defaultdict
from typing import TYPE_CHECKING, Iterable, Iterator

from protoboard.domain.board.pad_geometry import SNAP_TOL, qpair

if TYPE_CHECKING:
    from protoboard.domain.board.board import Board
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.layout.layout import Layout
    from protoboard.domain.layout.placement import ComponentPlacement
    from protoboard.domain.layout.wire import Wire
    from protoboard.domain.schematic.schematic import Schematic


# Half-raster tolerance for "bare wire passes through pad" detection.
_CROSS_EPS = 0.5

# Quantised position keys.  PadIndex keys pads by (qx, qy) — side-agnostic,
# because both sides of one physical pad share the same hole.
_PadKey = tuple[float, float]


def _seg_point_dist(
    px: float, py: float,
    ax: float, ay: float,
    bx: float, by: float,
) -> float:
    """Shortest distance from point (px, py) to segment (ax, ay)→(bx, by)."""
    abx, aby = bx - ax, by - ay
    ab_sq = abx * abx + aby * aby
    if ab_sq < 1e-12:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * abx + (py - ay) * aby) / ab_sq
    t = max(0.0, min(1.0, t))
    return math.hypot(px - ax - t * abx, py - ay - t * aby)


class PadIndex:
    """Per-pad occupancy index — single source of truth for pad state.

    Algorithms (simplify, autoroute, drag-conflict, DRC) query
    ``pad_at(pos)`` instead of re-deriving "is this pad occupied?" from
    layout + schematic each time.

    Lifetime: tied to ``EditorSession``; rebuilt on
    ``invalidate_graph()`` (full rebuild) but supports incremental
    ``add_wire``/``remove_wire``/``add_component``/``remove_component``
    for callers that prefer faster updates.

    Coordinate convention: pad centers at ``(col+0.5, row+0.5)`` in raster
    units.  Positions within ±SNAP_TOL are treated as the same pad.
    """

    def __init__(
        self,
        board: "Board",
        layout: "Layout",
        schematic: "Schematic",
        components: dict[str, "ComponentDefinition"],
        instances: dict[uuid.UUID, "ComponentInstance"],
    ) -> None:
        self._board = board
        self._components = components
        self._instances = instances

        # Core state: pad position → Pad
        self._pads: dict[_PadKey, Pad] = {}

        # Strip group bookkeeping: id → list of pad keys.
        self._strips: dict[int, list[_PadKey]] = defaultdict(list)
        self._next_strip_id: int = 0

        # Spatial grid for fast "pads near segment" queries.  Key: integer
        # cell (floor(x), floor(y)); value: list of pad keys whose pad
        # center falls in that cell.
        self._pad_grid: dict[tuple[int, int], list[_PadKey]] = defaultdict(list)

        # Per-mutator records for undo without rebuild.
        self._wire_records: dict[uuid.UUID, _WireFootprint] = {}
        self._component_records: dict[uuid.UUID, _ComponentFootprint] = {}

        # Build from scratch.
        self._build_board_pads()
        self._build_components(layout, schematic)
        self._build_wires(layout, schematic)

    # ------------------------------------------------------------------
    # Build phase
    # ------------------------------------------------------------------

    def _build_board_pads(self) -> None:
        """Walk board atoms and instantiate one Pad per hole."""
        from protoboard.domain.board.boxes import PadAtom, StripAtom

        for atom, center in self._board.atoms():
            if isinstance(atom, PadAtom):
                self._add_pad(center, atom.layers, strip_group_id=None)
            elif isinstance(atom, StripAtom):
                if atom.isolated:
                    continue  # holes exist but no copper — skip for now
                # Each hole of the strip = its own Pad, sharing a strip_group_id.
                strip_id = self._next_strip_id
                self._next_strip_id += 1
                n = int(atom.length) if isinstance(atom.length, int) else 1
                if atom.orientation == "horizontal":
                    start_x = center.x - n / 2.0 + 0.5
                    positions = [Point(start_x + i, center.y) for i in range(n)]
                else:
                    start_y = center.y - n / 2.0 + 0.5
                    positions = [Point(center.x, start_y + i) for i in range(n)]
                for pt in positions:
                    self._add_pad(pt, atom.layers, strip_group_id=strip_id)
                # Strip's holes are galvanically connected — represent that
                # as a synthetic "strip bare wire" item shared across all
                # holes.  Use a sentinel id and None net (the strip carries
                # no net until a pin or user wire lands on it).
                strip_item_id = uuid.UUID(int=0x5731_0000_0000_0000 | strip_id)
                strip_item = BareWireItem(id=strip_item_id, net=None)
                # All armed sides of every hole receive the item.
                for pt in positions:
                    key = qpair(pt)
                    pad = self._pads[key]
                    # Strip's copper is on whatever sides PadLayers indicate.
                    for s in _SIDES:
                        if (s == "top" and atom.layers.top) or \
                           (s == "bottom" and atom.layers.bottom):
                            pad._add_item(strip_item, s)

    def _add_pad(
        self,
        position: Point,
        layers: PadLayers,
        strip_group_id: int | None,
    ) -> None:
        key = qpair(position)
        if key in self._pads:
            return  # dedup — same pad referenced multiple times
        pad = self._board.make_pad(position, layers, strip_group_id=strip_group_id)
        self._pads[key] = pad
        cell = (int(math.floor(position.x)), int(math.floor(position.y)))
        self._pad_grid[cell].append(key)
        if strip_group_id is not None:
            self._strips[strip_group_id].append(key)

    def _build_components(
        self, layout: "Layout", schematic: "Schematic",
    ) -> None:
        for comp_id, placement in layout.placements.items():
            inst = self._instances.get(comp_id)
            if inst is None:
                continue
            defn = self._components.get(inst.definition_ref)
            if defn is None:
                continue
            self.add_component(comp_id, placement, defn, schematic=schematic)

    def _build_wires(
        self, layout: "Layout", schematic: "Schematic",
    ) -> None:
        # Components/pins were added before wires; pads carrying a pinned
        # net already report ``electrical_net_on(side)``.  A wire's net is
        # the first such pad it touches.
        for wire in layout.wires:
            wire_net = self._wire_net_from_existing_pads(wire)
            self.add_wire(wire, net=wire_net)

    def _wire_net_from_existing_pads(
        self, wire: "Wire",
    ) -> "NetId | None":
        """First path-vertex whose pad already carries a netted PinItem
        determines the wire's net.  Defensive fallback returns None.
        """
        for pt in wire.path:
            pad = self.pad_at(pt)
            if pad is None:
                continue
            net = pad.electrical_net_on(wire.side)
            if net is not None and net is not SHORT_CIRCUIT:
                return net  # type: ignore[return-value]
        return None

    # ------------------------------------------------------------------
    # Public queries
    # ------------------------------------------------------------------

    def pad_at(self, position: Point) -> Pad | None:
        """Return the Pad at *position*, or None if no pad there."""
        return self._pads.get(qpair(position))

    def electrical_net_at(
        self, position: Point, side: Side,
    ) -> "NetId | None | object":
        """Net on *side* of pad at *position*, ``None``, or ``SHORT_CIRCUIT``."""
        pad = self.pad_at(position)
        if pad is None:
            return None
        return pad.electrical_net_on(side)

    def physically_blocked_at(
        self, position: Point, side: Side,
    ) -> list[Item]:
        pad = self.pad_at(position)
        if pad is None:
            return []
        return list(pad.physically_blocked_on(side))

    def is_free(self, position: Point, side: Side) -> bool:
        pad = self.pad_at(position)
        if pad is None:
            return True  # off-board → not blocked
        return pad.is_free(side)

    def can_route_through(
        self,
        position: Point,
        side: Side,
        our_net: "NetId | None",
        wire_type: Literal["bare", "insulated"],
    ) -> bool:
        """May a wire of *wire_type* on *side* pass through pad at *position*?

        A bare wire is rejected when:
        - the pad's electrical_net on this side is a *different* non-None
          net (foreign-net conflict), or SHORT_CIRCUIT;
        - the pad has a ComponentBody on this side blocking physical access.

        An insulated wire is rejected only when:
        - the pad has a ComponentBody on this side, or
        - an InsulatedWireCrossing/InsulatedWireItem of a *different* net
          already occupies the air above this pad on this side.

        Mid-segment crossing of *bare* wires over a pad is treated as
        electrical contact (the conductor rests on the copper); algorithms
        wanting "may my new bare wire vertex land here" should pass
        ``wire_type="bare"``.
        """
        pad = self.pad_at(position)
        if pad is None:
            # Off-board — nothing to short, nothing to block.  Algorithms
            # typically only call this for on-board candidates anyway.
            return True
        # ComponentBody blocks only when the definition opted in.  Most
        # through-hole packages leave a gap to the PCB and wires can route
        # under them (sometimes deliberately, using the component as a
        # jumper).  Autoroute soft-penalises via ``under_component_top``
        # regardless; this gate only prevents *hard* rejection.
        for it in pad.physically_blocked_on(side):
            if isinstance(it, ComponentBody) and it.blocks_routing:
                return False
        net = pad.electrical_net_on(side)
        if net is SHORT_CIRCUIT:
            return False
        if wire_type == "bare":
            if net is None:
                return True
            if our_net is None:
                # We don't know our net — be conservative: any netted pad
                # blocks (caller may relax for legitimate cases).
                return False
            return net == our_net
        # Insulated:
        for it in pad.physically_blocked_on(side):
            if isinstance(it, (InsulatedWireItem, InsulatedWireCrossing)):
                if our_net is not None and it.net == our_net:
                    continue  # same net — sharing the air is OK
                return False
        return True

    def iter_pads(self) -> Iterator[Pad]:
        return iter(self._pads.values())

    def iter_pads_with_wires(self) -> Iterator[Pad]:
        """Yield only pads that have at least one wire item recorded.

        Significantly faster than ``iter_pads`` for analyses that pair
        wires meeting at a pad (e.g. cost-analyzer crossing detection).
        Most pads on a perfboard never see any wire; iterating them
        all to check ``physically_blocked`` is O(board_size) per call.
        """
        seen: set[_PadKey] = set()
        for rec in self._wire_records.values():
            for key, _side, _item in rec.items:
                if key in seen:
                    continue
                seen.add(key)
                pad = self._pads.get(key)
                if pad is not None:
                    yield pad

    def pads_for_strip(self, strip_group_id: int) -> list[Pad]:
        return [self._pads[k] for k in self._strips.get(strip_group_id, [])]

    def pads_near_segment(
        self,
        ax: float, ay: float, bx: float, by: float,
        eps: float = _CROSS_EPS,
    ) -> list[Pad]:
        """Pads whose center lies within *eps* raster units of segment a→b."""
        x0 = int(math.floor(min(ax, bx) - eps))
        x1 = int(math.floor(max(ax, bx) + eps))
        y0 = int(math.floor(min(ay, by) - eps))
        y1 = int(math.floor(max(ay, by) + eps))
        out: list[Pad] = []
        seen: set[_PadKey] = set()
        for cx in range(x0, x1 + 1):
            for cy in range(y0, y1 + 1):
                for k in self._pad_grid.get((cx, cy), []):
                    if k in seen:
                        continue
                    pad = self._pads.get(k)
                    if pad is None:
                        continue
                    if _seg_point_dist(pad.position.x, pad.position.y,
                                       ax, ay, bx, by) <= eps:
                        seen.add(k)
                        out.append(pad)
        return out

    # ------------------------------------------------------------------
    # Mutation API — incremental updates
    # ------------------------------------------------------------------

    def add_wire(
        self,
        wire: "Wire",
        net: "NetId | None" = None,
    ) -> None:
        """Place wire items on every pad the wire path touches.

        * For bare wires: every path vertex gets a ``BareWireItem`` on
          that side; every pad strictly between two adjacent vertices
          and on the segment (within _CROSS_EPS) gets a
          ``BareWireCrossing``.
        * For insulated wires: only endpoints get ``InsulatedWireItem``;
          interior vertices get ``InsulatedWireCrossing``.

        Idempotent — calling twice with the same wire is a no-op
        (records are keyed by ``wire.id``).
        """
        if wire.id in self._wire_records:
            return
        footprint: list[tuple[_PadKey, Side, Item]] = []
        side = wire.side
        path = list(wire.path)
        n = len(path)
        # Vertex items.
        for i, pt in enumerate(path):
            key = qpair(pt)
            pad = self._pads.get(key)
            if pad is None:
                continue
            is_endpoint = (i == 0 or i == n - 1)
            if wire.insulated:
                item: Item = (
                    InsulatedWireItem(id=wire.id, net=net)
                    if is_endpoint
                    else InsulatedWireCrossing(id=wire.id, net=net)
                )
            else:
                item = BareWireItem(id=wire.id, net=net)
            pad._add_item(item, side)
            footprint.append((key, side, item))
        # Bare wire mid-segment pad crossings.
        if not wire.insulated:
            path_pts = {qpair(p) for p in path}
            for i in range(n - 1):
                a, b = path[i], path[i + 1]
                for pad in self.pads_near_segment(a.x, a.y, b.x, b.y):
                    key = qpair(pad.position)
                    if key in path_pts:
                        continue  # already covered by vertex item
                    # Use a synthetic id per (wire, pad) pair so removing
                    # the wire properly clears every crossing.
                    item_xing = BareWireCrossing(id=wire.id, net=net)
                    pad._add_item(item_xing, side)
                    footprint.append((key, side, item_xing))
        self._wire_records[wire.id] = _WireFootprint(items=footprint)

    def remove_wire(self, wire: "Wire") -> None:
        """Undo every contribution of *wire* recorded in the footprint."""
        rec = self._wire_records.pop(wire.id, None)
        if rec is None:
            return
        seen: set[tuple[_PadKey, Side]] = set()
        for key, side, item in rec.items:
            pad = self._pads.get(key)
            if pad is None:
                continue
            if (key, side) in seen:
                continue  # _remove_item removes ALL records of this id
            pad._remove_item(item.id)
            seen.add((key, side))

    def add_component(
        self,
        comp_id: uuid.UUID,
        placement: "ComponentPlacement",
        defn: "ComponentDefinition",
        schematic: "Schematic | None" = None,
    ) -> None:
        """Place pin items on each pad the component's pins land on, plus
        component-body blockers on every pad covered by the component bbox.
        """
        if comp_id in self._component_records:
            return
        from protoboard.domain.layout.placement import (
            component_bbox, pin_world_position,
        )
        footprint: list[tuple[_PadKey, Side, Item]] = []
        side = placement.side
        # Pin items.
        pin_net_map: dict[int, NetId] = {}
        if schematic is not None:
            for net in schematic.nets.values():
                for ref in net.pins:
                    if ref.component_id == comp_id:
                        pin_net_map[ref.pin_number] = net.id
        for pin_num in range(1, defn.footprint.pin_count() + 1):
            pin_pos = pin_world_position(defn.footprint, placement, pin_num)
            key = qpair(pin_pos)
            pad = self._pads.get(key)
            if pad is None:
                continue
            pin_item = PinItem(
                id=comp_id,
                pin_number=pin_num,
                net=pin_net_map.get(pin_num),
            )
            pad._add_item(pin_item, side)
            footprint.append((key, side, pin_item))
        # Component body — every pad inside the bbox (excluding pin pads
        # which already have PinItem) gets a ComponentBody on this side.
        # ``blocks_routing`` propagates from the definition (default False —
        # see ComponentDefinition.body_blocks_routing docstring).
        bbox = component_bbox(defn.footprint, placement, defn.component_class)
        body = ComponentBody(
            id=comp_id,
            blocks_routing=getattr(defn, "body_blocks_routing", False),
        )
        pin_keys = {f[0] for f in footprint}
        for pad in self._pads_in_bbox(bbox):
            key = qpair(pad.position)
            if key in pin_keys:
                continue
            pad._add_item(body, side)
            footprint.append((key, side, body))
        self._component_records[comp_id] = _ComponentFootprint(items=footprint)

    def remove_component(self, comp_id: uuid.UUID) -> None:
        rec = self._component_records.pop(comp_id, None)
        if rec is None:
            return
        seen: set[tuple[_PadKey, Side, uuid.UUID]] = set()
        for key, side, item in rec.items:
            pad = self._pads.get(key)
            if pad is None:
                continue
            marker = (key, side, item.id)
            if marker in seen:
                continue
            pad._remove_item(item.id)
            seen.add(marker)

    def update_component_pin_nets(
        self,
        comp_id: uuid.UUID,
        new_nets_by_pin: dict[int, "NetId | None"],
    ) -> None:
        """When schematic changes pin↔net assignment without moving the
        component, refresh the recorded PinItem.net on each affected pad.
        """
        rec = self._component_records.get(comp_id)
        if rec is None:
            return
        new_items: list[tuple[_PadKey, Side, Item]] = []
        for key, side, item in rec.items:
            pad = self._pads.get(key)
            if pad is None:
                new_items.append((key, side, item))
                continue
            if isinstance(item, PinItem):
                new_net = new_nets_by_pin.get(item.pin_number, item.net)
                if new_net != item.net:
                    pad._remove_item(item.id)
                    new_item = PinItem(
                        id=item.id, pin_number=item.pin_number, net=new_net,
                    )
                    pad._add_item(new_item, side)
                    new_items.append((key, side, new_item))
                    continue
            new_items.append((key, side, item))
        self._component_records[comp_id] = _ComponentFootprint(items=new_items)

    def _pads_in_bbox(self, bbox) -> list[Pad]:  # noqa: ANN001  (Rect is geometry)
        """Pads whose center lies inside *bbox* (inclusive on both bounds)."""
        out: list[Pad] = []
        # Brute-force iterate the grid cells the bbox covers.
        x0 = int(math.floor(bbox.x))
        x1 = int(math.floor(bbox.x + bbox.width))
        y0 = int(math.floor(bbox.y))
        y1 = int(math.floor(bbox.y + bbox.height))
        for cx in range(x0, x1 + 1):
            for cy in range(y0, y1 + 1):
                for k in self._pad_grid.get((cx, cy), []):
                    pad = self._pads.get(k)
                    if pad is None:
                        continue
                    px, py = pad.position.x, pad.position.y
                    if (
                        bbox.x - SNAP_TOL <= px <= bbox.x + bbox.width + SNAP_TOL
                        and bbox.y - SNAP_TOL <= py <= bbox.y + bbox.height + SNAP_TOL
                    ):
                        out.append(pad)
        return out

    # ------------------------------------------------------------------
    # Clone — for drag-conflict trial state
    # ------------------------------------------------------------------

    def clone(self) -> "PadIndex":
        """Deep-copy enough state to allow trial mutations.

        Used by drag-conflict classifier: clone, apply hypothetical move,
        query, discard.  ``Board`` and component definitions are shared
        (immutable); pads + records are deep-copied.
        """
        import copy as _copy

        new = PadIndex.__new__(PadIndex)
        new._board = self._board
        new._components = self._components
        new._instances = self._instances
        new._pads = {k: _copy.deepcopy(p) for k, p in self._pads.items()}
        new._strips = {k: list(v) for k, v in self._strips.items()}
        new._next_strip_id = self._next_strip_id
        new._pad_grid = {k: list(v) for k, v in self._pad_grid.items()}
        new._wire_records = {
            k: _WireFootprint(items=list(v.items))
            for k, v in self._wire_records.items()
        }
        new._component_records = {
            k: _ComponentFootprint(items=list(v.items))
            for k, v in self._component_records.items()
        }
        return new


@dataclass
class _WireFootprint:
    items: list[tuple[_PadKey, Side, Item]]


@dataclass
class _ComponentFootprint:
    items: list[tuple[_PadKey, Side, Item]]


__all__ = [
    "NetId",
    "Side",
    "SHORT_CIRCUIT",
    "PinItem",
    "BareWireItem",
    "BareWireCrossing",
    "InsulatedWireItem",
    "InsulatedWireCrossing",
    "ComponentBody",
    "Item",
    "Pad",
    "SingleSidedBoardPad",
    "IsolatedDoubleSidedBoardPad",
    "ConnectedDoubleSidedBoardPad",
    "PadIndex",
]
