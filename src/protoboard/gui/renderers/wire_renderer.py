# SPDX-License-Identifier: MIT
"""Renderer for wire segments and ratsnest."""

from __future__ import annotations

import uuid

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import QGraphicsLineItem, QGraphicsScene

from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.placement import ComponentPlacement, pin_world_position
from protoboard.domain.layout.wire import WireSegment
from protoboard.domain.schematic.net import PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point

# ── Visual constants ─────────────────────────────────────────────────────────

# Net colours — same colour used for all wires of the same net, regardless of
# insulation or side.  Palette is cycle-indexed by net UUID hash.
_NET_COLORS = [
    QColor("#E03030"),  # red
    QColor("#3060E0"),  # blue
    QColor("#30A030"),  # green
    QColor("#E0A020"),  # amber
    QColor("#A030A0"),  # purple
    QColor("#20B0B0"),  # cyan
    QColor("#E06020"),  # orange
    QColor("#B08020"),  # olive
]

# Bare wire: thin, no jacket.  Drawn as a dashed line over grey tint so it is
# visually lighter than an insulated wire of the same colour.
_BARE_WIDTH        = 0.08
_BARE_TINT         = QColor(255, 255, 255, 60)   # white overlay on colour
_BARE_TINT_WIDTH   = _BARE_WIDTH + 0.04

# Insulated wire: thicker, solid, with a thin black outline.
_INS_WIDTH         = 0.18
_INS_OUTLINE_COLOR = QColor("#000000")
_INS_OUTLINE_WIDTH = 0.28

# Bottom-side wires: same style but dimmed (lower opacity).
_BOTTOM_OPACITY    = 0.45   # applied to colour alpha

# Z-values.  Bottom wires sit below board pads; top wires above.
_Z_BOTTOM = 0.6
_Z_TOP    = 1.0
_Z_OUTLINE_OFFSET = -0.05   # outline slightly behind fill


def _net_color(net_id: uuid.UUID | None) -> QColor:
    """Return the colour assigned to *net_id*, or a neutral grey for None."""
    if net_id is None:
        return QColor("#A0A0A0")
    idx = abs(hash(net_id)) % len(_NET_COLORS)
    return _NET_COLORS[idx]


def _dim(color: QColor, opacity: float) -> QColor:
    c = QColor(color)
    c.setAlphaF(color.alphaF() * opacity)
    return c


def render_wires_with_nets(
    scene: QGraphicsScene,
    wires: list[WireSegment],
    wire_net_map: dict[uuid.UUID, uuid.UUID],
) -> None:
    """Render wires with correct per-net colouring.

    *wire_net_map* maps wire.id → net.id and is built by the caller who has
    access to both the schematic and the layout.
    """
    for wire in wires:
        net_id = wire_net_map.get(wire.id)
        _draw_wire(scene, wire, net_id)


def _draw_wire(
    scene: QGraphicsScene,
    wire: WireSegment,
    net_id: uuid.UUID | None,
) -> None:
    pts = wire.path
    color = _net_color(net_id)
    is_bottom = wire.side == "bottom"
    z_base = _Z_BOTTOM if is_bottom else _Z_TOP
    if is_bottom:
        color = _dim(color, _BOTTOM_OPACITY)

    if wire.insulated:
        _draw_insulated(scene, pts, color, z_base)
    else:
        _draw_bare(scene, pts, color, z_base)


def _draw_insulated(
    scene: QGraphicsScene,
    pts: list[Point],
    color: QColor,
    z_base: float,
) -> None:
    outline_pen = QPen(_INS_OUTLINE_COLOR, _INS_OUTLINE_WIDTH)
    outline_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    outline_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    core_pen = QPen(color, _INS_WIDTH)
    core_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    core_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    for i in range(len(pts) - 1):
        x1, y1 = pts[i].x, pts[i].y
        x2, y2 = pts[i + 1].x, pts[i + 1].y
        outline = QGraphicsLineItem(x1, y1, x2, y2)
        outline.setPen(outline_pen)
        outline.setZValue(z_base + _Z_OUTLINE_OFFSET)
        scene.addItem(outline)
        core = QGraphicsLineItem(x1, y1, x2, y2)
        core.setPen(core_pen)
        core.setZValue(z_base)
        scene.addItem(core)


def _draw_bare(
    scene: QGraphicsScene,
    pts: list[Point],
    color: QColor,
    z_base: float,
) -> None:
    # Bare wire: dashed line in net colour, thin, with flat caps.
    pen = QPen(color, _BARE_WIDTH)
    pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
    pen.setStyle(Qt.PenStyle.DashLine)
    for i in range(len(pts) - 1):
        line = QGraphicsLineItem(pts[i].x, pts[i].y, pts[i + 1].x, pts[i + 1].y)
        line.setPen(pen)
        line.setZValue(z_base - 0.05)
        scene.addItem(line)


# ── Ratsnest ─────────────────────────────────────────────────────────────────

_RATSNEST_COLOR = QColor(255, 80, 80, 160)
_RATSNEST_WIDTH = 0.08


def render_ratsnest(
    scene: QGraphicsScene,
    schematic: Schematic,
    placements: dict[uuid.UUID, ComponentPlacement],
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    routed_nets: set[uuid.UUID] | None,
) -> None:
    """Draw crow-flies lines for unrouted pin pairs in incomplete nets."""
    if routed_nets is None:
        return

    pen = QPen(_RATSNEST_COLOR, _RATSNEST_WIDTH)
    pen.setStyle(Qt.PenStyle.DotLine)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)

    for net in schematic.nets.values():
        if net.id in routed_nets:
            continue
        pin_positions = _placed_pin_positions(
            net.pins, placements, instances, components
        )
        if len(pin_positions) < 2:
            continue
        for i in range(len(pin_positions) - 1):
            a, b = pin_positions[i], pin_positions[i + 1]
            line = QGraphicsLineItem(a.x, a.y, b.x, b.y)
            line.setPen(pen)
            scene.addItem(line)


def _placed_pin_positions(
    pins: set[PinRef],
    placements: dict[uuid.UUID, ComponentPlacement],
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
) -> list[Point]:
    result: list[Point] = []
    for ref in pins:
        pl = placements.get(ref.component_id)
        inst = instances.get(ref.component_id)
        if pl is None or inst is None:
            continue
        defn = components.get(inst.definition_ref)
        if defn is None or ref.pin_number > defn.pin_count:
            continue
        result.append(pin_world_position(defn.footprint, pl, ref.pin_number))
    return result
