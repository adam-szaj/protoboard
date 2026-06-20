# SPDX-License-Identifier: MIT
"""ComponentInstance — a placed-or-placeable copy of a definition.

The `id` is a UUID; `refdes` (e.g. "U1", "R5") is a user-facing label and
may be renamed without affecting connectivity (which references the UUID).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from protoboard.domain.components.appearance import Appearance
from protoboard.domain.schematic.net import PinIdentity, PinRef

if TYPE_CHECKING:
    from protoboard.domain.components.definition import ComponentDefinition


@dataclass(frozen=True, slots=True)
class ComponentInstance:
    """A placed-or-placeable copy of a definition.

    The instance is the factory for this component's pin electrical identity:
    pins that share a name (several ``GND``) get one shared :class:`PinIdentity`
    via :meth:`build_pin_identities`, and :meth:`pin_ref` hands out
    :class:`PinRef`\\ s carrying it. ``PinIdentity`` is internal to the
    component and never escapes except via the refs the instance produces.

    The instance itself stays immutable (``frozen``): ``pin_identities`` is a
    mutable container, but only the ``net_id`` *inside* each identity is ever
    mutated — the field binding never changes, so the frozen contract holds.
    """

    id: uuid.UUID
    definition_ref: str
    refdes: str
    value: str
    type: str = ""          # optional per-instance category override
    # Per-instance override of the package-family variant as
    # ``"{family}/{variant_id}"``; None → use the definition's
    # ``package_family_ref`` (or inline ``package``).
    package_variant: str | None = None
    pin_variant: str | None = None   # variant id from ComponentDefinition.variants or None
    # Per-instance visual-only override (fill colour, corner radius), layered
    # over the definition's appearance and the package/theme defaults. None =
    # inherit. Geometry/mask/DRC/routing are unaffected.
    appearance: Appearance | None = None
    # pin_number → shared PinIdentity for same-name pin groups. Excluded from
    # eq/hash (compare=False): two instances are equal by their value fields,
    # not by transient electrical state. Populated by build_pin_identities.
    pin_identities: dict[int, PinIdentity] = field(
        default_factory=dict, compare=False, hash=False
    )

    def __post_init__(self) -> None:
        if not self.definition_ref:
            raise ValueError("definition_ref must not be empty")
        if not self.refdes:
            raise ValueError("refdes must not be empty")

    @staticmethod
    def new(
        definition_ref: str, refdes: str, value: str, type: str = "",
        pin_variant: str | None = None,
        package_variant: str | None = None,
        appearance: Appearance | None = None,
    ) -> ComponentInstance:
        """Create an instance with a freshly generated UUID."""
        return ComponentInstance(
            id=uuid.uuid4(),
            definition_ref=definition_ref,
            refdes=refdes,
            value=value,
            type=type,
            pin_variant=pin_variant,
            package_variant=package_variant,
            appearance=appearance,
        )

    def build_pin_identities(self, defn: "ComponentDefinition") -> None:
        """(Re)build the shared PinIdentity for each same-name pin group.

        Reads ``defn.pins_grouped_by_name(self.pin_variant)`` and creates one
        identity per group, mapping every pin of the group to it. Mutates the
        ``pin_identities`` dict in place (frozen-safe — the binding is
        unchanged). Idempotent; call again after a pin-variant change, which
        may regroup names. Pins not in any group get no entry.
        """
        self.pin_identities.clear()
        for pins in defn.pins_grouped_by_name(self.pin_variant).values():
            canonical = min(pins)
            identity = PinIdentity(key=(self.id, canonical))
            for pin_num in pins:
                self.pin_identities[pin_num] = identity

    def pin_ref(self, pin_number: int) -> PinRef:
        """Return a PinRef for *pin_number*, carrying its shared PinIdentity.

        Ungrouped pins (no same-name sibling) get a ref with ``identity=None``;
        they are equal to and interchangeable with a bare lookup ref.
        """
        return PinRef(self.id, pin_number, identity=self.pin_identities.get(pin_number))
