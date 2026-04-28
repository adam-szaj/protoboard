# SPDX-License-Identifier: MIT
"""Schematic — logical netlist: PinRef, Schematic.

`Net` lives in `net.py` to avoid circular imports; both are re-exported
from this module for convenience.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from protoboard.domain.schematic.net import Net, PinRef


@dataclass
class Schematic:
    """Pure logical netlist — no geometry."""

    nets: dict[uuid.UUID, Net]

    def pins_for_component(self, component_id: uuid.UUID) -> set[PinRef]:
        pins: set[PinRef] = set()
        for net in self.nets.values():
            for ref in net.pins:
                if ref.component_id == component_id:
                    pins.add(ref)
        return pins

    def net_for_pin(self, ref: PinRef) -> Net | None:
        for net in self.nets.values():
            if ref in net.pins:
                return net
        return None


__all__ = ["Net", "PinRef", "Schematic"]
