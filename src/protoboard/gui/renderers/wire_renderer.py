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

WIRE_BARE_COLOR = QColor("#C0C0C0")       # light grey — clearly bare metal
WIRE_BARE_WIDTH = 0.08                    # thin — no insulation jacket
WIRE_INSULATED_COLORS = [
    QColor("#E03030"),  # red
    QColor("#3060E0"),  # blue
    QColor("#30A030"),  # green
    QColor("#E0A020"),  # yellow
    QColor("#A030A0"),  # purple
    QColor("#20B0B0"),  # cyan
    QColor("#E06020"),  # orange
]
WIRE_INSULATED_WIDTH = 0.18              # thicker — has insulation jacket
WIRE_INSULATED_OUTLINE_COLOR = QColor("#000000")
WIRE_INSULATED_OUTLINE_WIDTH = 0.28     # black outline behind colour fill


def render_wires(scene: QGraphicsScene, wires: list[WireSegment]) -> None:
    for wire in wires:
        pts = wire.path
        if wire.insulated:
            # Draw outline first (behind), then coloured core on top.
            outline_pen = QPen(WIRE_INSULATED_OUTLINE_COLOR, WIRE_INSULATED_OUTLINE_WIDTH)
            outline_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            outline_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            color = _wire_color(wire)
            core_pen = QPen(color, WIRE_INSULATED_WIDTH)
            core_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            core_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            for i in range(len(pts) - 1):
                x1, y1 = pts[i].x, pts[i].y
                x2, y2 = pts[i + 1].x, pts[i + 1].y
                outline = QGraphicsLineItem(x1, y1, x2, y2)
                outline.setPen(outline_pen)
                outline.setZValue(1.0)
                scene.addItem(outline)
                core = QGraphicsLineItem(x1, y1, x2, y2)
                core.setPen(core_pen)
                core.setZValue(1.1)
                scene.addItem(core)
        else:
            # Bare wire: single thin grey line.
            pen = QPen(WIRE_BARE_COLOR, WIRE_BARE_WIDTH)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
            for i in range(len(pts) - 1):
                line = QGraphicsLineItem(pts[i].x, pts[i].y, pts[i + 1].x, pts[i + 1].y)
                line.setPen(pen)
                line.setZValue(0.9)
                scene.addItem(line)


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
        return  # no schematic loaded

    pen = QPen(_RATSNEST_COLOR, _RATSNEST_WIDTH)
    pen.setStyle(Qt.PenStyle.DotLine)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)

    for net in schematic.nets.values():
        if net.id in routed_nets:
            continue  # fully routed — skip
        pin_positions = _placed_pin_positions(
            net.pins, placements, instances, components
        )
        if len(pin_positions) < 2:
            continue
        # Draw a line from each pin to the next — simple chain (not full mesh).
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


def _wire_color(wire: WireSegment) -> QColor:
    if not wire.insulated:
        return WIRE_BARE_COLOR
    if wire.color:
        return QColor(wire.color)
    # Cycle through palette based on object id for visual variety.
    idx = abs(hash(wire.id)) % len(WIRE_INSULATED_COLORS)
    return WIRE_INSULATED_COLORS[idx]
