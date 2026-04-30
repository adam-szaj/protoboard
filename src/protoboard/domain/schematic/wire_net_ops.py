# SPDX-License-Identifier: MIT
"""Pure functions for classifying wire endpoints relative to the schematic netlist.

No Qt, no side effects — safe to use in unit tests without a display.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Protocol

from protoboard.domain.layout.placement import pin_world_position

if TYPE_CHECKING:
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.layout.layout import Layout
    from protoboard.domain.layout.wire import WireSegment
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
    for comp_id, placement in session.layout.placements.items():
        inst = session.instances.get(comp_id)
        if inst is None:
            continue
        defn = session.components.get(inst.definition_ref)
        if defn is None:
            continue
        for pin_num in range(1, defn.footprint.pin_count() + 1):
            pos = pin_world_position(defn.footprint, placement, pin_num)
            if abs(pos.x - pt.x) < _EPS and abs(pos.y - pt.y) < _EPS:
                return (comp_id, pin_num)
    return None


class WireNetKind(Enum):
    NO_PINS = auto()            # at least one end not on a component pin
    BOTH_UNCONNECTED = auto()   # both pins exist but neither is in a net
    ONE_UNCONNECTED = auto()    # one pin is in a net, the other is not
    SAME_NET = auto()           # both pins are already in the same net
    DIFFERENT_NETS = auto()     # both pins are in different nets


@dataclass(frozen=True)
class WireNetClassification:
    kind: WireNetKind
    pin_a: tuple[uuid.UUID, int] | None = None  # (comp_id, pin_num) at wire.path[0]
    pin_b: tuple[uuid.UUID, int] | None = None  # (comp_id, pin_num) at wire.path[-1]
    net_a: Net | None = None                    # schematic net of pin_a, if any
    net_b: Net | None = None                    # schematic net of pin_b, if any


def classify_wire_nets(
    wire: WireSegment,
    session: _SessionLike,
) -> WireNetClassification:
    """Classify the schematic relationship of *wire*'s two endpoints."""
    from protoboard.domain.schematic.net import PinRef

    pt_a = wire.path[0]
    pt_b = wire.path[-1]

    pin_a = find_pin_at(pt_a, session)
    pin_b = find_pin_at(pt_b, session)

    if pin_a is None or pin_b is None:
        return WireNetClassification(kind=WireNetKind.NO_PINS)

    net_a = session.schematic.net_for_pin(
        PinRef(component_id=pin_a[0], pin_number=pin_a[1])
    )
    net_b = session.schematic.net_for_pin(
        PinRef(component_id=pin_b[0], pin_number=pin_b[1])
    )

    if net_a is None and net_b is None:
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
