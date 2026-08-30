# SPDX-License-Identifier: MIT
"""Concrete :class:`~protoboard.controller.selection.selectable.Selectable` adapters.

One thin wrapper per board entity the user can select or pick.  Each wraps a
pure domain value object and supplies the polymorphic behaviour the legacy
``Hit.kind`` switch used to scatter across SelectTool.

Phase 0 implements the two members the selection core needs now:

* :meth:`identity` — the dedup/equality key, and
* :meth:`to_hit` — the bridge to the still-unchanged pure FSM.

The remaining members (:meth:`bbox`, :meth:`net_ids`, :meth:`to_drag_member`)
gain bodies when their first consumer lands — ``bbox`` with rubber-band
hit-test (Phase 2), ``to_drag_member`` with the drag mode (Phase 3).  Until
then they raise :class:`NotImplementedError`, so an adapter is structurally a
``Selectable`` (every signature present) and a premature call fails loudly
instead of silently mis-behaving.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from protoboard.controller.selection_fsm import Hit
from protoboard.geometry import Point

if TYPE_CHECKING:
    from protoboard.controller.selection.selectable import (
        ConnQuery,
        SelectableKey,
    )
    from protoboard.domain.layout.group import ComponentGroup
    from protoboard.domain.layout.placement import ComponentPlacement
    from protoboard.domain.layout.wire import Wire
    from protoboard.domain.schematic.net import PinRef
    from protoboard.geometry import Rect
    from protoboard.types import NetId

_PHASE2 = "Selectable.bbox lands with rubber-band hit-test (Phase 2)"
_PHASE3 = "Selectable.to_drag_member lands with the drag mode (Phase 3)"
_PICK_ONLY = "PointSelectable is a pick-only coordinate — read .point, not this"


@dataclass(frozen=True, slots=True)
class ComponentSelectable:
    """A placed component, identified by its component id."""

    component_id: uuid.UUID
    placement: ComponentPlacement
    # The specific pin a hit landed on (nearest-pin path), else None.
    pin_number: int | None = None

    def identity(self) -> SelectableKey:
        return ("component", self.component_id)

    def to_hit(self) -> Hit:
        return Hit("component", self.component_id, pin_number=self.pin_number)

    def bbox(self) -> Rect:
        raise NotImplementedError(_PHASE2)

    def net_ids(self, conn: ConnQuery) -> frozenset[NetId]:
        # A pin-scoped hit narrows to that pin's net; a whole-component hit
        # unions every net the component participates in.
        if self.pin_number is not None:
            return conn.nets_for_pin(self.component_id, self.pin_number)
        return conn.nets_for_component(self.component_id)

    def to_drag_member(self) -> object | None:
        raise NotImplementedError(_PHASE3)


@dataclass(frozen=True, slots=True)
class WireSelectable:
    """A wire (whole-wire selection), identified by its wire id."""

    wire: Wire

    def identity(self) -> SelectableKey:
        return ("wire", self.wire.id)

    def to_hit(self) -> Hit:
        return Hit("wire", self.wire.id)

    def bbox(self) -> Rect:
        raise NotImplementedError(_PHASE2)

    def net_ids(self, conn: ConnQuery) -> frozenset[NetId]:
        nid = conn.net_for_wire(self.wire.id)
        return frozenset({nid}) if nid is not None else frozenset()

    def to_drag_member(self) -> object | None:
        raise NotImplementedError(_PHASE3)


@dataclass(frozen=True, slots=True)
class GroupSelectable:
    """A component group, identified by its group id."""

    group: ComponentGroup

    def identity(self) -> SelectableKey:
        return ("group", self.group.id)

    def to_hit(self) -> Hit:
        return Hit("group", self.group.id)

    def bbox(self) -> Rect:
        raise NotImplementedError(_PHASE2)

    def net_ids(self, conn: ConnQuery) -> frozenset[NetId]:
        # Union of every net the group's direct members touch.  (Nested-group
        # members come in as their own ComponentSelectables; the group adapter
        # carries only the ComponentGroup, which lists direct members.)
        out: frozenset[NetId] = frozenset()
        for cid in self.group.component_ids:
            out |= conn.nets_for_component(cid)
        return out

    def to_drag_member(self) -> object | None:
        raise NotImplementedError(_PHASE3)


@dataclass(frozen=True, slots=True)
class PinSelectable:
    """A single component pin (pick-only — never a persistent selection).

    Bridges to a component :class:`Hit` carrying the pin number, so the FSM
    treats a picked pin as a pin-scoped component hit.
    """

    pin: PinRef

    def identity(self) -> SelectableKey:
        return ("pin", self.pin.component_id, self.pin.pin_number)

    def to_hit(self) -> Hit:
        return Hit("component", self.pin.component_id, pin_number=self.pin.pin_number)

    def bbox(self) -> Rect:
        raise NotImplementedError(_PHASE2)

    def net_ids(self, conn: ConnQuery) -> frozenset[NetId]:
        return conn.nets_for_pin(self.pin.component_id, self.pin.pin_number)

    def to_drag_member(self) -> object | None:
        raise NotImplementedError(_PHASE3)


@dataclass(frozen=True, slots=True)
class WireVertexSelectable:
    """One vertex of a wire path (pick-only — e.g. EditWire's two endpoints).

    Identified by ``(wire_id, vertex_index)``.  Bridges to a ``wire`` Hit so
    the FSM/highlight treat the parent wire as the hit object; the vertex
    index lives only in :meth:`identity` (the FSM has no vertex concept).
    """

    wire_id: uuid.UUID
    index: int

    def identity(self) -> SelectableKey:
        return ("wire-vertex", self.wire_id, self.index)

    def to_hit(self) -> Hit:
        return Hit("wire", self.wire_id)

    def bbox(self) -> Rect:
        raise NotImplementedError(_PHASE2)

    def net_ids(self, conn: ConnQuery) -> frozenset[NetId]:
        nid = conn.net_for_wire(self.wire_id)
        return frozenset({nid}) if nid is not None else frozenset()

    def to_drag_member(self) -> object | None:
        raise NotImplementedError(_PHASE3)


@dataclass(frozen=True, slots=True)
class PointSelectable:
    """A bare board coordinate (pick-only — e.g. redrag's target pad).

    Some picks resolve to a *location*, not an entity: redrag asks the user for
    the pad a bendable lead should reach, and EditWire's reroute collects pad
    waypoints.  This adapter carries the snapped :class:`Point`; its consumer
    reads :attr:`point` from the picked list.  It never enters the FSM selection
    (a coordinate is not a ``Hit``), so :meth:`to_hit` and the rest raise.
    """

    point: Point

    def identity(self) -> SelectableKey:
        return ("point", self.point.x, self.point.y)

    def to_hit(self) -> Hit:
        raise NotImplementedError(_PICK_ONLY)

    def bbox(self) -> Rect:
        raise NotImplementedError(_PICK_ONLY)

    def net_ids(self, conn: ConnQuery) -> frozenset[NetId]:
        raise NotImplementedError(_PICK_ONLY)

    def to_drag_member(self) -> object | None:
        raise NotImplementedError(_PICK_ONLY)
