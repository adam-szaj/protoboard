# SPDX-License-Identifier: MIT
"""Net and PinRef — kept in one module to avoid circular imports."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass(eq=False)
class PinIdentity:
    """Internal electrical identity of a pin — the node it belongs to *inside*
    a component.

    Pins of one component that share a name (several ``GND``) point at one
    shared ``PinIdentity`` instance, so they are electrically one node even
    before any wire is drawn. This is distinct from :class:`Net` membership
    (a project-level decision, between components): identity is innate, net
    is acquired.

    ``net_id`` is a *live* field — assigning a net to one pin mutates the
    shared identity, so all pins of the same name immediately see it. The
    sole place that writes it is ``Schematic``'s assignment methods.

    Identity is compared by object id (``eq=False``): two distinct identities
    are never equal even if their fields match, because they represent
    different physical nodes. ``key`` is a stable label for debugging/lookups.
    """

    key: tuple[uuid.UUID, int]  # (component_id, canonical_pin_number)
    net_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class PinRef:
    """Reference to a specific pin of a specific component instance.

    Identity (hash/eq) is ``(component_id, pin_number)`` only — ``identity``
    is excluded (``compare=False``) so a bare ``PinRef(comp, pin)`` built for
    a lookup compares equal to the fully-resolved ref that carries a shared
    :class:`PinIdentity`. Net membership is read/written through the resolved
    ref's ``identity``, never a bare lookup ref.
    """

    component_id: uuid.UUID
    pin_number: int
    identity: PinIdentity | None = field(default=None, compare=False, hash=False)

    def __post_init__(self) -> None:
        if self.pin_number < 1:
            raise ValueError(f"pin_number must be >= 1; got {self.pin_number}")

    @property
    def meta_pin_index(self) -> int:
        """The 1-based MetaPin ordinal represented by this reference."""
        return self.pin_number


@dataclass
class Net:
    """A logical network: the set of pins that must be electrically connected."""

    id: uuid.UUID
    name: str
    pins: set[PinRef] = field(default_factory=set)
    color: str | None = None  # hex color string e.g. "#E03030"; None = auto from palette
    global_net: bool = False  # True = exclude from hug/spread neighbour graph and autorouter priority

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Net):
            return NotImplemented
        return self.id == other.id

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Net.name must not be empty")
