# SPDX-License-Identifier: MIT
"""The single place that expands a pin to its same-name siblings.

Same-name pins of one component (several ``GND``) are one electrical node:
they share a :class:`PinIdentity`, so a net assigned to one is a net on all.
Every net-membership operation — assigning a pin to a net (panel), wiring a
pin (route tool), disconnecting a pin — must act on the *whole* sibling group,
not the single clicked pin. This module is that expansion, in one function, so
no caller re-derives it.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from protoboard.domain.schematic.net import PinRef

if TYPE_CHECKING:
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance


def same_name_refs(
    component_id: uuid.UUID,
    pin_number: int,
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
) -> list[PinRef]:
    """The pin plus every same-name sibling of its component (one node).

    Returns :class:`PinRef`\\ s carrying the shared :class:`PinIdentity`, so a
    downstream ``Schematic`` mutator writes the group's ``net_id`` once for all.

    Builds the instance's identities on demand when empty: an instance may have
    entered the project before identities were wired (legacy placement, paste,
    replace), so this never relies on a prior ``build_pin_identities`` call.
    Falls back to the single (bare) pin when the component or its identity is
    unknown, or when the pin has no same-name sibling.
    """
    inst = instances.get(component_id)
    if inst is None:
        return [PinRef(component_id=component_id, pin_number=pin_number)]
    if not inst.pin_identities:
        defn = components.get(inst.definition_ref)
        if defn is not None:
            inst.build_pin_identities(defn)
    identity = inst.pin_identities.get(pin_number)
    if identity is None:
        return [inst.pin_ref(pin_number)]
    return [
        inst.pin_ref(pin)
        for pin, ident in inst.pin_identities.items()
        if ident is identity
    ]


__all__ = ["same_name_refs"]
