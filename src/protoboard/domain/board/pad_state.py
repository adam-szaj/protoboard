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
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from typing import ClassVar, Literal

from protoboard.domain.board.pads import PadLayers
from protoboard.geometry import Point

NetId = uuid.UUID
Side = Literal["top", "bottom"]
_SIDES: tuple[Side, Side] = ("top", "bottom")

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
    """The bbox of a component covers this pad on this side."""

    id: uuid.UUID            # component instance id
    kind: ClassVar[str] = "component_body"


# Union type alias — for type hints in the rest of the codebase.
Item = (
    PinItem
    | BareWireItem
    | BareWireCrossing
    | InsulatedWireItem
    | InsulatedWireCrossing
    | ComponentBody
)


def _item_contributes_electrically(item: Item) -> bool:
    """True if *item* contributes its net to the side it is placed on.

    Pins, bare wires (vertex or crossing), and insulated wire endpoints
    contact copper; insulated mid-path vertices and component bodies do
    not.
    """
    return isinstance(item, (PinItem, BareWireItem, BareWireCrossing,
                              InsulatedWireItem))


def _item_is_through_hole(item: Item) -> bool:
    """True if *item* exposes a conductor in the through-hole and thus
    propagates electrically to both pad sides regardless of board type.

    Pins are physically pushed through the hole; a wire that changes
    sides at this pad has its conductor in the hole too.  Bare and
    insulated wire endpoints sit on top of the copper ring on their own
    side only — they propagate only per board-type rules.
    """
    return isinstance(item, PinItem)


# ---------------------------------------------------------------------------
# Pad base + subclasses
# ---------------------------------------------------------------------------


@dataclass
class Pad(ABC):
    """One physical hole on the board with its current occupancy state.

    Subclasses encapsulate the side-propagation rule for electrical
    contributions.  The base class owns all mutation bookkeeping; the
    only abstract hook is ``_side_propagation``.
    """

    position: Point
    layers: PadLayers
    strip_group_id: int | None = None

    # Per-side electrical net.  ``SHORT_CIRCUIT`` sentinel marks a pad
    # whose recorded sources include more than one distinct non-None net.
    electrical_net: tuple[NetId | None, NetId | None] = (None, None)

    # Per-side physical occupants.  Each side keeps the list of Items
    # whose physical presence prevents another wire being routed
    # through this pad on that side.
    physically_blocked: tuple[list[Item], list[Item]] = field(
        default_factory=lambda: ([], [])
    )

    # Internal bookkeeping: which Item id contributed which net per side.
    # When multiple items share the same net, removing one of them must
    # not drop the net — only when the LAST contributor of that net is
    # gone.  Map item.id → contributed net (or None if the item only
    # blocks physically).
    _net_sources: tuple[dict[uuid.UUID, NetId | None],
                         dict[uuid.UUID, NetId | None]] = field(
        default_factory=lambda: ({}, {})
    )

    # ------------------------------------------------------------------
    # Side-propagation hook (subclass-specific)
    # ------------------------------------------------------------------

    @abstractmethod
    def _side_propagation(self, item: Item, source_side: Side) -> set[Side]:
        """Return the set of sides on which *item* contributes its net.

        For items that don't contribute electrically (insulated crossing,
        component body), return ``set()``.
        """

    # ------------------------------------------------------------------
    # Side-physical hook (which sides receive the physical blocker)
    # ------------------------------------------------------------------

    def _physical_propagation(self, item: Item, source_side: Side) -> set[Side]:
        """Return the set of sides on which *item* physically blocks.

        Default: a wire/pin on side X physically occupies side X only.
        Pins are special — they pierce the board, so they block both
        sides regardless of board type.  Subclasses may override if
        needed (none currently do).
        """
        if isinstance(item, PinItem):
            return set(_SIDES)
        return {source_side}

    # ------------------------------------------------------------------
    # Public properties (read-only views)
    # ------------------------------------------------------------------

    def electrical_net_on(self, side: Side) -> NetId | None | object:
        """Net touching the copper on *side*, ``None``, or
        ``SHORT_CIRCUIT``."""
        idx = _SIDES.index(side)
        return self.electrical_net[idx]

    def physically_blocked_on(self, side: Side) -> list[Item]:
        """Items physically occupying this side."""
        idx = _SIDES.index(side)
        return self.physically_blocked[idx]

    def is_free(self, side: Side) -> bool:
        """No electrical net AND no physical blockers on *side*."""
        return (
            self.electrical_net_on(side) is None
            and not self.physically_blocked_on(side)
        )

    # ------------------------------------------------------------------
    # Internal mutation API (called by PadIndex)
    # ------------------------------------------------------------------

    def _add_item(self, item: Item, source_side: Side) -> None:
        """Record *item* as occupying this pad, propagating per rules."""
        # Physical placement
        phys_sides = self._physical_propagation(item, source_side)
        new_phys = list(self.physically_blocked)
        for s in phys_sides:
            idx = _SIDES.index(s)
            new_phys[idx] = list(new_phys[idx])
            new_phys[idx].append(item)
        self.physically_blocked = (new_phys[0], new_phys[1])

        # Electrical contribution
        if _item_contributes_electrically(item):
            elec_sides = self._side_propagation(item, source_side)
            new_sources = list(self._net_sources)
            for s in elec_sides:
                idx = _SIDES.index(s)
                new_sources[idx] = dict(new_sources[idx])
                new_sources[idx][item.id] = item.net  # type: ignore[attr-defined]
            self._net_sources = (new_sources[0], new_sources[1])
            self._recompute_electrical()

    def _remove_item(self, item_id: uuid.UUID) -> None:
        """Remove every record of *item_id* from this pad."""
        new_phys = list(self.physically_blocked)
        for idx in (0, 1):
            new_phys[idx] = [
                it for it in new_phys[idx] if it.id != item_id
            ]
        self.physically_blocked = (new_phys[0], new_phys[1])

        new_sources = list(self._net_sources)
        for idx in (0, 1):
            if item_id in new_sources[idx]:
                new_sources[idx] = dict(new_sources[idx])
                del new_sources[idx][item_id]
        self._net_sources = (new_sources[0], new_sources[1])
        self._recompute_electrical()

    def _recompute_electrical(self) -> None:
        """Update ``electrical_net`` from ``_net_sources``."""
        new_net: list[NetId | None | object] = [None, None]
        for idx in (0, 1):
            nets = {n for n in self._net_sources[idx].values() if n is not None}
            if len(nets) == 0:
                # No netted source — but unconnected sources (pin with no
                # net assigned, floating wire) may still mark the side as
                # "claimed" for routing purposes.  For now we mirror
                # legacy semantics: electrical_net stays None.
                new_net[idx] = None
            elif len(nets) == 1:
                new_net[idx] = next(iter(nets))
            else:
                new_net[idx] = SHORT_CIRCUIT
        self.electrical_net = (
            new_net[0],  # type: ignore[assignment]
            new_net[1],  # type: ignore[assignment]
        )

    # ------------------------------------------------------------------
    # Iteration & misc.
    # ------------------------------------------------------------------

    def iter_items(self, side: Side | None = None):
        """Iterate Items on *side* (or both sides if None)."""
        if side is None:
            seen: set[uuid.UUID] = set()
            for items in self.physically_blocked:
                for it in items:
                    if it.id not in seen:
                        seen.add(it.id)
                        yield it
        else:
            yield from self.physically_blocked_on(side)


# ---------------------------------------------------------------------------
# Subclasses — concrete propagation rules
# ---------------------------------------------------------------------------


@dataclass
class SingleSidedBoardPad(Pad):
    """A pad on a single-sided board.

    Only one of ``layers.top``/``layers.bottom`` is True; the other side
    has no copper and cannot carry an electrical net via a wire physically
    lying on it.  Pins still pierce both sides.
    """

    def _side_propagation(self, item: Item, source_side: Side) -> set[Side]:
        if _item_is_through_hole(item):
            # Pin / via — exposes conductor on both sides regardless of
            # whether copper is physically there on the other side.  See
            # footnote `*` in the user's table.
            return set(_SIDES)
        # A wire on the armed side contributes its net only to that side.
        # A wire on the unarmed side has no copper to bond to: physical
        # presence is recorded (so other wires can't route here) but no
        # electrical contribution.
        if source_side == "top" and self.layers.top:
            return {"top"}
        if source_side == "bottom" and self.layers.bottom:
            return {"bottom"}
        return set()


@dataclass
class IsolatedDoubleSidedBoardPad(Pad):
    """A double-sided pad whose through-hole is NOT plated.

    Both top and bottom carry copper, but they are electrically
    independent at this pad — only a pin or wire-side-change that
    physically bridges the hole can connect them.
    """

    def _side_propagation(self, item: Item, source_side: Side) -> set[Side]:
        if _item_is_through_hole(item):
            return set(_SIDES)
        return {source_side}


@dataclass
class ConnectedDoubleSidedBoardPad(Pad):
    """A plated-through pad — top and bottom are one electrical node."""

    def _side_propagation(self, item: Item, source_side: Side) -> set[Side]:
        # Any electrical contribution on either side merges to both.
        return set(_SIDES)


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
]
