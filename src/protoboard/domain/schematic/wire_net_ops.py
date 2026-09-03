# SPDX-License-Identifier: MIT
"""Pure functions for classifying wire endpoints relative to the schematic netlist.

No Qt, no side effects — safe to use in unit tests without a display.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Protocol

from protoboard.domain.components.resolution import iter_placed_components
from protoboard.domain.layout.placement import iter_footprint_pin_positions

if TYPE_CHECKING:
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.layout.layout import Layout
    from protoboard.domain.layout.wire import Wire
    from protoboard.domain.schematic.net import Net
    from protoboard.domain.schematic.schematic import Schematic
    from protoboard.geometry import Point

_EPS = 1e-4


class _SessionLike(Protocol):
    layout: Layout
    instances: dict[uuid.UUID, ComponentInstance]
    components: dict[str, ComponentDefinition]
    schematic: Schematic


def find_pin_at(
    pt: Point,
    session: _SessionLike,
) -> tuple[uuid.UUID, int] | None:
    """Return (component_id, pin_number) if *pt* coincides with a placed pin."""
    for component in iter_placed_components(
        session.layout.placements,
        session.instances,
        session.components,
    ):
        comp_id = component.placement.component_id
        placement = component.placement
        from protoboard.domain.components.package import resolved_footprint

        fp = resolved_footprint(
            component.definition,
            component.instance,
            component.instance.families,
        )
        for pin_num, pos in iter_footprint_pin_positions(fp, placement):
            if abs(pos.x - pt.x) < _EPS and abs(pos.y - pt.y) < _EPS:
                return (comp_id, pin_num)
    return None


def find_wire_net_at(
    pt: Point,
    session: _SessionLike,
    *,
    exclude_wire_id: uuid.UUID | None = None,
) -> Net | None:
    """Return the schematic net of any existing wire whose path passes
    through ``pt``; ``None`` if no wire touches ``pt`` or if the
    touching wire has no net assignment.

    Used when a new wire's endpoint lands on the interior (or endpoint)
    of an already-placed wire belonging to some net — the new wire
    inherits that net.  ``exclude_wire_id`` skips a wire by id; pass
    the wire being added so it doesn't self-match in same-Layout
    membership checks.
    """
    # Build a (comp_id, pin_num) → net id map from the schematic once.
    pin_to_net: dict[tuple[uuid.UUID, int], Net] = {}
    for net in session.schematic.nets.values():
        for pin_ref in net.pins:
            pin_to_net[(pin_ref.component_id, pin_ref.pin_number)] = net
    for wire in session.layout.wires:
        if exclude_wire_id is not None and wire.id == exclude_wire_id:
            continue
        for vertex in wire.path:
            if abs(vertex.x - pt.x) < _EPS and abs(vertex.y - pt.y) < _EPS:
                # Match — try to derive the net of this wire from any
                # of its other vertices that coincide with a pin.
                for v2 in wire.path:
                    pin = find_pin_at(v2, session)
                    if pin is None:
                        continue
                    matched_net = pin_to_net.get(pin)
                    if matched_net is not None:
                        return matched_net
                # Wire has no pin endpoints with net — match-but-no-net.
                return None
    return None


class WireNetKind(Enum):
    NO_PINS = auto()  # at least one end not on a component pin
    BOTH_UNCONNECTED = auto()  # both pins exist but neither is in a net
    ONE_UNCONNECTED = auto()  # one pin is in a net, the other is not
    SAME_NET = auto()  # both pins are already in the same net
    DIFFERENT_NETS = auto()  # both pins are in different nets


@dataclass(frozen=True)
class WireNetClassification:
    kind: WireNetKind
    pin_a: tuple[uuid.UUID, int] | None = None  # (comp_id, pin_num) at wire.path[0]
    pin_b: tuple[uuid.UUID, int] | None = None  # (comp_id, pin_num) at wire.path[-1]
    net_a: Net | None = None  # schematic net of pin_a, if any
    net_b: Net | None = None  # schematic net of pin_b, if any


def classify_wire_nets(
    wire: Wire,
    session: _SessionLike,
) -> WireNetClassification:
    """Classify the schematic relationship of *wire*'s two endpoints.

    Each endpoint contributes a net source in priority order:

    1. **Pin** at the endpoint position — the canonical case (pin has
       a schematic net or doesn't).
    2. **Existing wire** whose path passes through that position —
       the new wire inherits the touching wire's net (auto-net for
       connect-to-wire-mid / connect-to-wire-end T-junctions).

    When only a wire (no pin) contributes net X at endpoint A and a
    pin at endpoint B has no net, the classification reports
    ``ONE_UNCONNECTED`` with ``net_a=X``, ``pin_a=None``,
    ``pin_b=(comp, n)`` so ``_extend_net`` can add B's pin to net X.
    When neither endpoint has a pin and at least one has a wire net,
    the new wire still "joins" that net without changing the pin list
    (handled by callers checking ``pin_*`` ``None`` before mutating).
    """
    from protoboard.domain.schematic.net import PinRef

    pt_a = wire.path[0]
    pt_b = wire.path[-1]

    pin_a = find_pin_at(pt_a, session)
    pin_b = find_pin_at(pt_b, session)

    net_a: Net | None = None
    net_b: Net | None = None
    if pin_a is not None:
        net_a = session.schematic.net_for_pin(
            PinRef(component_id=pin_a[0], pin_number=pin_a[1])
        )
    if pin_b is not None:
        net_b = session.schematic.net_for_pin(
            PinRef(component_id=pin_b[0], pin_number=pin_b[1])
        )
    # Wire-touch fallback: when an endpoint has no pin (or has a pin
    # without a net), let any existing wire passing through that point
    # contribute its net.  This handles the "endpoint lands on another
    # wire's interior / endpoint" auto-net case.
    if net_a is None:
        net_a = find_wire_net_at(pt_a, session, exclude_wire_id=wire.id)
    if net_b is None:
        net_b = find_wire_net_at(pt_b, session, exclude_wire_id=wire.id)

    # Without any pin AND any wire net at both endpoints, there's
    # nothing schematic to do — leave the wire pure-geometric.
    if pin_a is None and pin_b is None and net_a is None and net_b is None:
        return WireNetClassification(kind=WireNetKind.NO_PINS)

    # When a pin endpoint is missing on BOTH sides but a wire-net was
    # found on at least one side, the classification still reports
    # NO_PINS — there's no pin to mutate.  The new wire joining the
    # net is reflected in the connectivity graph (touching geometry
    # is enough); ``_extend_net`` only fires when there IS a pin to
    # add.
    if pin_a is None and pin_b is None:
        return WireNetClassification(kind=WireNetKind.NO_PINS)

    if net_a is None and net_b is None:
        # BOTH_UNCONNECTED only makes sense when both endpoints have a
        # pin we can add to a new net.  One-pin-one-empty + no nets =
        # nothing schematic to do.
        if pin_a is None or pin_b is None:
            return WireNetClassification(kind=WireNetKind.NO_PINS)
        return WireNetClassification(
            kind=WireNetKind.BOTH_UNCONNECTED,
            pin_a=pin_a,
            pin_b=pin_b,
        )
    if net_a is None or net_b is None:
        return WireNetClassification(
            kind=WireNetKind.ONE_UNCONNECTED,
            pin_a=pin_a,
            pin_b=pin_b,
            net_a=net_a,
            net_b=net_b,
        )
    if net_a.id == net_b.id:
        return WireNetClassification(
            kind=WireNetKind.SAME_NET,
            pin_a=pin_a,
            pin_b=pin_b,
            net_a=net_a,
        )
    return WireNetClassification(
        kind=WireNetKind.DIFFERENT_NETS,
        pin_a=pin_a,
        pin_b=pin_b,
        net_a=net_a,
        net_b=net_b,
    )
