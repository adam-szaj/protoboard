# SPDX-License-Identifier: MIT
"""Knots — endpoint hubs where Wires terminate.

A Knot is the *named* end of a Wire.  Two flavours:

* ``PinKnot`` — anchored to a component pin (``PinRef``).  Its world
  position is resolved lazily from the layout's placement of the
  component.
* ``JointKnot`` — a free t-joint where multiple wires of the same net
  meet.  Its world position is explicit and **must** equal a board pad
  position (off-pad joints are forbidden — validated in DRC).

Knots hold back-references (``wire_ids``) to all wires terminating at
them.  Mutating ``wire_ids`` is the responsibility of ``Layout`` helpers
(``add_wire``, ``remove_wire``); consumers must not poke at the back-refs
directly.

The ``Knot`` protocol allows code that doesn't care about the flavour to
type-check uniformly.  ``KnotRef`` (in ``wire.py``) is the stable id-only
reference held inside ``Wire``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from protoboard.domain.schematic.net import PinRef
from protoboard.geometry import Point


@runtime_checkable
class Knot(Protocol):
    """Common interface for endpoint hubs."""

    id: uuid.UUID
    wire_ids: frozenset[uuid.UUID]


@dataclass
class PinKnot:
    """Knot anchored to a component pin.

    ``position`` is *not* stored — it is resolved through the layout via
    ``Layout.pin_world_position(self.pin)`` to keep the knot stable across
    component moves/rotations.  Storing only ``pin`` (a ``PinRef``) means
    the knot survives layout edits to the underlying placement.
    """

    id: uuid.UUID
    pin: PinRef
    wire_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PinKnot):
            return NotImplemented
        return self.id == other.id


@dataclass
class JointKnot:
    """T-joint where multiple wires of one net meet.

    ``position`` is the world coordinate of the joint.  It *must* coincide
    with a board pad (enforced by DRC's ``_check_off_pad_joint``).  Joints
    with one or zero incident wires are orphan and removed by
    ``Layout.remove_wire``.
    """

    id: uuid.UUID
    position: Point
    wire_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, JointKnot):
            return NotImplemented
        return self.id == other.id
