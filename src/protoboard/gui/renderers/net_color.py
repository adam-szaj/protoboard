# SPDX-License-Identifier: MIT
"""Single source of truth for net colour resolution.

Used by wire / pad / pin renderers so a net's colour is identical
regardless of which surface displays it.

Priority:

1. **Manual override** — ``net.color`` set via the Nets panel.  Always wins.
2. **Theme palette** — first 8 nets cycle through ``theme.net_colors``.
3. **Golden-ratio hue spinner** — once the palette is exhausted, additional
   nets get a hue computed from a deterministic golden-ratio walk through
   the HSV wheel, keeping consecutive nets visually distinct without
   repeating.  Saturation/value are inherited from ``theme.net_colors[0]``
   so the extended palette matches the user-tuned palette character.

The spinner uses the net's UUID as input so the same net always gets the
same colour across sessions — without storing it in the schematic.

Unassigned (wire with no net) falls back to ``theme.net_color_unassigned``.

Follow-up (not implemented here): rule-based heuristics that detect
power / ground / bus signals by name (GND, VCC, V+, V-, +5V, SDA, SCL,
MISO, MOSI, CS, CLK, TIMx, INTx, RX, TX, RST) and assign distinctive
colours from a dedicated palette family.  Tracked separately.
"""

from __future__ import annotations

import colorsys
import uuid
from typing import cast

from PySide6.QtGui import QColor

from protoboard.domain.schematic.net import Net
from protoboard.gui.theme import Theme

# Golden ratio conjugate — gives maximally-spaced hues when added
# successively modulo 1.0.  ~0.618; classic "golden hue rotation" trick.
_GOLDEN_HUE_STEP = 0.6180339887498949


def _golden_color_for(net_id: uuid.UUID, base: QColor, step_index: int) -> QColor:
    """Generate a fresh QColor by spinning hue *step_index* golden steps
    away from *base*, keeping saturation and value from *base*.

    *step_index* is the net's position past the end of the static palette
    — 0 for the first overflow net, 1 for the second, etc.  Using an
    integer index instead of hashing makes the assignment stable per
    schematic without depending on net id ordering.
    """
    # PySide6 stubs type ``getHsvF`` as returning ``object``; at runtime it is
    # a 4-tuple of floats (h, s, v, a).  Cast so we can slice/unpack it.
    h, s, v = cast("tuple[float, float, float, float]", base.getHsvF())[:3]
    new_h = (h + _GOLDEN_HUE_STEP * (step_index + 1)) % 1.0
    # Keep saturation/value bright enough to read on the board; clamp
    # away from the washed-out lower end if the user picked a pale base.
    s = max(0.55, s)
    v = max(0.70, v)
    r, g, b = colorsys.hsv_to_rgb(new_h, s, v)
    return QColor.fromRgbF(r, g, b)


def color_for_net(
    net: Net,
    *,
    theme: Theme,
    overflow_index: int = 0,
) -> QColor:
    """Resolve a net to a QColor.

    - ``net.color`` set by the user → that colour.
    - Otherwise: ``theme.net_colors[hash(net.id) % len]`` for the first
      ``len(theme.net_colors)`` nets, then golden-ratio-spun hues from
      ``theme.net_colors[0]`` for the rest.

    *overflow_index* is the net's index past the static palette window;
    callers that don't track ordering can pass 0 (each net then maps to
    a palette colour by hash, with no extension — the legacy behaviour).
    Callers that want the extended palette must pre-assign indices —
    e.g. :func:`build_net_color_map` iterates the schematic in stable
    order and computes the per-net index.
    """
    if net.color:
        return QColor(net.color)
    palette = theme.net_colors
    n = len(palette)
    if not palette:
        return QColor("#808080")
    if overflow_index <= 0:
        # Legacy hash-based slot.
        idx = abs(hash(net.id)) % n
        return QColor(palette[idx])
    # Net beyond the static palette — spin hue from the first palette colour.
    base = QColor(palette[0])
    return _golden_color_for(net.id, base, overflow_index - 1)


def build_net_color_map(
    nets: dict[uuid.UUID, Net],
    *,
    theme: Theme,
) -> dict[uuid.UUID, str]:
    """Return ``net_id → hex string`` for every net in *nets*.

    Nets sorted by ``Net.name`` for stable assignment: the same net keeps
    its colour across sessions as long as the netlist names are stable.
    The first ``len(theme.net_colors)`` nets land on palette slots via
    hash (preserves legacy behaviour for small projects); overflow nets
    use golden-ratio hue spinning.
    """
    palette = theme.net_colors
    n = len(palette)
    result: dict[uuid.UUID, str] = {}
    if not nets:
        return result

    # Stable order: by name, then by id as tiebreaker.
    ordered = sorted(nets.values(), key=lambda nt: (nt.name, str(nt.id)))
    palette_taken: set[int] = set()
    overflow_counter = 0

    for net in ordered:
        if net.color:
            result[net.id] = net.color
            continue
        # First N nets: try hash slot, fall back to first free slot
        # if hash collides with a previous net's slot.  This keeps small
        # netlists looking like the legacy hash-based scheme while
        # guaranteeing distinct slots for the first N nets.
        if len(palette_taken) < n:
            slot = abs(hash(net.id)) % n
            if slot in palette_taken:
                slot = next(i for i in range(n) if i not in palette_taken)
            palette_taken.add(slot)
            result[net.id] = palette[slot]
            continue
        # Overflow: golden-ratio hue spin from palette[0].
        overflow_counter += 1
        color = _golden_color_for(net.id, QColor(palette[0]), overflow_counter - 1)
        result[net.id] = color.name()  # canonical "#RRGGBB"

    return result


__all__ = ["color_for_net", "build_net_color_map"]
