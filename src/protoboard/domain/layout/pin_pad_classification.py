# SPDX-License-Identifier: MIT
"""Pure-function classification of pads for the pin-pads rendering layer.

The pin-pads layer is a *schematic-aware* overlay drawn above the board
layer (pads, strips, frames) but below wires.  Each (pad, side) is
classified into one of three classes:

* ``NONE`` — pad has no electrical relation to any net on this side.
  No element rendered on this layer; the bare board pad below remains
  visible.

* ``CONTACT_ONLY`` — pad is electrically part of a net on this side via
  a bare wire vertex, bare-wire crossing, or galvanic propagation from
  the other side (plated-through hole touched by a pin / wire-pin / via
  / bare wire on the opposite side).  No component pin is soldered
  directly into it on this side.  Rendered as a circle outline in the
  net colour.

* ``PIN_PAD`` — pad has a component pin (``PinItem``) or wire-pin
  (``WirePinItem``) terminating into it.  Rendered as a filled circle
  in the net colour with the modifier shape cut out of the centre.

Modifier shapes (only for ``PIN_PAD``):

* ``CIRCLE`` — default; pin1=False, exported=False.
* ``SQUARE`` — pin 1 of its component.
* ``DIAMOND`` — pin exported by an ancestor group (interface pin).
* ``STAR`` — both pin1 and exported.

Pure function — no Qt, no scene state, no side effects.  Suitable for
both the GUI rendering pipeline and headless export.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from protoboard.domain.board.layer import PHYSICAL_LAYERS, LayerId
from protoboard.domain.board.pad_state import (
    SHORT_CIRCUIT,
    BareWireCrossing,
    BareWireItem,
    PinItem,
    WirePinItem,
)
from protoboard.domain.layout.group import ExportedPin
from protoboard.geometry import Point

if TYPE_CHECKING:
    from protoboard.domain.board.pad_state import Pad, PadIndex
    from protoboard.domain.layout.layout import Layout


NetId = uuid.UUID


class PinPadClass(Enum):
    """Classification of a (pad, side) for the pin-pads render layer."""

    PIN_PAD = "pin_pad"
    CONTACT_ONLY = "contact_only"


class ModifierShape(Enum):
    """Shape carved into a pin-pad and repeated as the top overlay glyph."""

    CIRCLE = "circle"      # default — no modifier
    SQUARE = "square"      # pin 1 of its component
    DIAMOND = "diamond"    # exported pin (group interface)
    STAR = "star"          # pin 1 AND exported


@dataclass(frozen=True, slots=True)
class PinPadInfo:
    """One classified (pad, side) record for the pin-pads layer.

    ``net_id`` is the resolved electrical net on this side.  For the
    PIN_PAD class ``modifier`` reflects pin1/exported flags; for
    CONTACT_ONLY it is always ``CIRCLE`` (modifiers are reserved for
    component pin terminals).
    """

    position: Point
    side: LayerId
    net_id: NetId
    klass: PinPadClass
    modifier: ModifierShape = ModifierShape.CIRCLE


def _collect_exported_pin_set(
    layout: "Layout",
) -> frozenset[ExportedPin]:
    """Return the union of ``exported_pins`` across every group.

    The pin-pads layer treats *any* pin reachable on a group's interface
    as "exported" — there's no need to walk only ancestors of the
    pin's owning group, because the modifier visual is purely a flag
    that says "this pin participates in some group's interface".
    """
    out: set[ExportedPin] = set()
    for group in layout.groups.values():
        out |= group.exported_pins
    return frozenset(out)


def _resolve_modifier(
    pin_items: list[PinItem],
    exported_pin_set: frozenset[ExportedPin],
) -> ModifierShape:
    """Pick the modifier shape from the pin items on a pad+side.

    Multiple pins on one pad (rare — typically a single pin per hole)
    combine flags: if *any* of the soldered pins is pin1, the square
    flag is set; if *any* is exported, the diamond flag is set.
    """
    is_pin1 = any(p.pin_number == 1 for p in pin_items)
    is_exported = any(
        ExportedPin(component_id=p.id, pin_number=p.pin_number)
        in exported_pin_set
        for p in pin_items
    )
    if is_pin1 and is_exported:
        return ModifierShape.STAR
    if is_pin1:
        return ModifierShape.SQUARE
    if is_exported:
        return ModifierShape.DIAMOND
    return ModifierShape.CIRCLE


def _classify_pad_side(
    pad: "Pad",
    side: LayerId,
    exported_pin_set: frozenset[ExportedPin],
) -> PinPadInfo | None:
    """Return classification for one (pad, side) pair, or None for NONE.

    Returns None when:
    * pad has no copper on this side (single-sided pad's empty side), or
    * the side carries no net (free pad or short-circuit error case).
    """
    if not pad.layers.has_layer(side):
        return None

    net = pad.electrical_net_on(side)
    if net is None or net is SHORT_CIRCUIT:
        return None

    # PinItem / WirePinItem on this side → PIN_PAD (pin terminal).
    pin_items: list[PinItem] = []
    has_wire_pin = False
    has_bare_contact = False
    for item in pad.iter_items(side):
        if isinstance(item, PinItem):
            pin_items.append(item)
        elif isinstance(item, WirePinItem):
            has_wire_pin = True
        elif isinstance(item, (BareWireItem, BareWireCrossing)):
            has_bare_contact = True

    if pin_items:
        modifier = _resolve_modifier(pin_items, exported_pin_set)
        return PinPadInfo(
            position=pad.position,
            side=side,
            net_id=net,  # type: ignore[arg-type]
            klass=PinPadClass.PIN_PAD,
            modifier=modifier,
        )

    if has_wire_pin:
        # Wire-pin behaves as a pin terminal but has no pin_number, so
        # no pin1 or exported flag — always CIRCLE modifier.
        return PinPadInfo(
            position=pad.position,
            side=side,
            net_id=net,  # type: ignore[arg-type]
            klass=PinPadClass.PIN_PAD,
            modifier=ModifierShape.CIRCLE,
        )

    # No pin / wire-pin on this side.  Either a bare-wire contact on
    # this side, or galvanic propagation from the opposite side (e.g.
    # plated-through hole with a pin on the other side).  Both render
    # as CONTACT_ONLY (outline-only circle in the net colour).
    _ = has_bare_contact  # explicit: bare contact handled by net check
    return PinPadInfo(
        position=pad.position,
        side=side,
        net_id=net,  # type: ignore[arg-type]
        klass=PinPadClass.CONTACT_ONLY,
        modifier=ModifierShape.CIRCLE,
    )


def classify_pin_pads(
    pad_index: "PadIndex",
    layout: "Layout",
) -> list[PinPadInfo]:
    """Walk every pad and produce a flat list of pin-pads classifications.

    One entry per (pad, side) that carries a net.  Side iteration uses
    :data:`PHYSICAL_LAYERS` (top, bottom) — kapton layers added later
    will need to extend this if they participate in the rendered view.

    Pure function: depends only on the snapshot view of ``PadIndex`` +
    ``Layout.groups`` provided.  Caller is responsible for cache
    invalidation when these inputs change.
    """
    exported_pin_set = _collect_exported_pin_set(layout)
    out: list[PinPadInfo] = []
    for pad in pad_index.iter_pads():
        for side in PHYSICAL_LAYERS:
            info = _classify_pad_side(pad, side, exported_pin_set)
            if info is not None:
                out.append(info)
    return out


__all__ = [
    "PinPadClass",
    "ModifierShape",
    "PinPadInfo",
    "classify_pin_pads",
]
