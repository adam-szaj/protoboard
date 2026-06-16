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

    # ------------------------------------------------------------------
    # Mutators — the single place that adds/removes pins from Net.pins.
    #
    # All net-membership changes funnel through these methods so that a
    # later layer (per-pin electrical identity, caches) has exactly one
    # hook to keep in sync. They intentionally preserve today's behaviour:
    # empty nets are dropped, missing nets are no-ops.
    # ------------------------------------------------------------------

    @staticmethod
    def _set_identity_net(ref: PinRef, net_id: uuid.UUID | None) -> None:
        """Mirror net membership onto the pin's shared PinIdentity, if any.

        The ref's identity is shared across same-name pins of the component
        (the instance is its factory), so setting it from one pin makes every
        sibling see the net immediately — the propagation. A bare lookup ref
        (``identity is None``) or an ungrouped pin is a no-op; its net lives
        in ``Net.pins`` only.
        """
        if ref.identity is not None:
            ref.identity.net_id = net_id

    def add_pin_to_net(self, ref: PinRef, net_id: uuid.UUID) -> None:
        """Add *ref* to an existing net. No-op if the net is unknown."""
        net = self.nets.get(net_id)
        if net is not None:
            net.pins.add(ref)
            self._set_identity_net(ref, net_id)

    def remove_pin_from_net(self, ref: PinRef, net_id: uuid.UUID) -> None:
        """Remove *ref* from a specific net without deleting the net.

        Used by speculative-assignment rollback where the caller must not
        drop the net even if it becomes empty.
        """
        net = self.nets.get(net_id)
        if net is not None:
            net.pins.discard(ref)
            self._set_identity_net(ref, None)

    def unassign_pin(self, ref: PinRef) -> tuple[uuid.UUID, str, set[PinRef]] | None:
        """Remove *ref* from whichever net holds it; drop the net if empty.

        Returns ``(net_id, net_name, pins_before)`` for the caller's revert,
        or ``None`` if the pin was not on any net. ``pins_before`` is a copy
        of the net's pins *before* removal.
        """
        net = self.net_for_pin(ref)
        if net is None:
            return None
        pins_before = net.pins.copy()
        result = (net.id, net.name, pins_before)
        net.pins.discard(ref)
        self._set_identity_net(ref, None)
        if not net.pins:
            del self.nets[net.id]
        return result

    def remove_component_pins(
        self, component_id: uuid.UUID
    ) -> dict[uuid.UUID, tuple[str, set[PinRef]]]:
        """Remove every pin of *component_id* from all nets; drop empty nets.

        Returns ``{net_id: (net_name, removed_pins)}`` for the caller's revert.
        """
        removed_by_net: dict[uuid.UUID, tuple[str, set[PinRef]]] = {}
        for net_id, net in list(self.nets.items()):
            removed = {p for p in net.pins if p.component_id == component_id}
            if not removed:
                continue
            removed_by_net[net_id] = (net.name, removed)
            net.pins -= removed
            for p in removed:
                self._set_identity_net(p, None)
            if not net.pins:
                del self.nets[net_id]
        return removed_by_net

    def restore_pins(self, net_id: uuid.UUID, name: str, pins: set[PinRef]) -> None:
        """Re-add *pins* to *net_id*, recreating the net if it was dropped."""
        net = self.nets.get(net_id)
        if net is not None:
            net.pins |= pins
        else:
            self.nets[net_id] = Net(id=net_id, name=name, pins=set(pins))
        for p in pins:
            self._set_identity_net(p, net_id)

    def merge_nets(
        self, survivor_id: uuid.UUID, merged_id: uuid.UUID, new_name: str | None = None
    ) -> None:
        """Fold *merged_id*'s pins into *survivor_id* and delete *merged_id*.

        Optionally renames the survivor. No-op if either net is unknown.
        """
        survivor = self.nets.get(survivor_id)
        merged = self.nets.get(merged_id)
        if survivor is None or merged is None:
            return
        if new_name is not None:
            survivor.name = new_name
        merged_pins = merged.pins
        survivor.pins |= merged_pins
        for p in merged_pins:
            self._set_identity_net(p, survivor_id)
        del self.nets[merged_id]

    def assign_pin(
        self, ref: PinRef, target_net_id: uuid.UUID
    ) -> tuple[uuid.UUID, str] | None:
        """Move *ref* to *target_net_id* (removing it from its current net).

        Returns ``(source_net_id, source_net_name)`` if the pin was moved off
        an existing net (for revert), else ``None``. The target net is assumed
        to exist; the pin is added to it.
        """
        source: tuple[uuid.UUID, str] | None = None
        prev = self.unassign_pin(ref)
        if prev is not None:
            source = (prev[0], prev[1])
        self.add_pin_to_net(ref, target_net_id)
        return source


__all__ = ["Net", "PinRef", "Schematic"]
