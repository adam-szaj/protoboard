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
from protoboard.gui.theme import DEFAULT_THEME, Theme

# Z-values — bottom wires sit below board pads; top wires above.
_Z_BOTTOM = 0.6
_Z_TOP = 1.0
_Z_OUTLINE_OFFSET = -0.05


def _net_color(
    net_id: uuid.UUID | None,
    override: str | None = None,
    theme: Theme = DEFAULT_THEME,
) -> QColor:
    if override:
        return QColor(override)
    if net_id is None:
        return QColor(theme.net_color_unassigned)
    idx = abs(hash(net_id)) % len(theme.net_colors)
    return QColor(theme.net_colors[idx])


def _dim(color: QColor, opacity: float) -> QColor:
    c = QColor(color)
    c.setAlphaF(color.alphaF() * opacity)
    return c


def render_wires_with_nets(
    scene: QGraphicsScene,
    wires: list[WireSegment],
    wire_net_map: dict[uuid.UUID, uuid.UUID],
    net_color_map: dict[uuid.UUID, str | None] | None = None,
    theme: Theme = DEFAULT_THEME,
) -> None:
    """Render wires with correct per-net colouring.

    *wire_net_map* maps wire.id → net.id.
    *net_color_map* maps net.id → user-set hex color (or None for auto palette).
    """
    for wire in wires:
        net_id = wire_net_map.get(wire.id)
        color_override: str | None = None
        if net_id is not None and net_color_map:
            color_override = net_color_map.get(net_id)
        _draw_wire(scene, wire, net_id, color_override, theme)


def _draw_wire(
    scene: QGraphicsScene,
    wire: WireSegment,
    net_id: uuid.UUID | None,
    color_override: str | None = None,
    theme: Theme = DEFAULT_THEME,
) -> None:
    pts = wire.path
    color = _net_color(net_id, color_override, theme)
    is_bottom = wire.side == "bottom"
    z_base = _Z_BOTTOM if is_bottom else _Z_TOP
    if is_bottom:
        color = _dim(color, theme.bottom_opacity)

    if wire.insulated:
        _draw_insulated(scene, pts, color, z_base, theme)
    else:
        _draw_bare(scene, pts, color, z_base, theme)


def _draw_insulated(
    scene: QGraphicsScene,
    pts: list[Point],
    color: QColor,
    z_base: float,
    theme: Theme,
) -> None:
    outline_pen = QPen(QColor(theme.ins_outline_color), theme.ins_outline_width)
    outline_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    outline_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    core_pen = QPen(color, theme.ins_width)
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
    theme: Theme,
) -> None:
    outline_pen = QPen(QColor(theme.bare_outline_color), theme.bare_outline_width)
    outline_pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    outline_pen.setJoinStyle(Qt.PenJoinStyle.BevelJoin)
    pen = QPen(color, theme.bare_width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    for i in range(len(pts) - 1):
        x1, y1 = pts[i].x, pts[i].y
        x2, y2 = pts[i + 1].x, pts[i + 1].y
        outline = QGraphicsLineItem(x1, y1, x2, y2)
        outline.setPen(outline_pen)
        outline.setZValue(z_base + _Z_OUTLINE_OFFSET)
        scene.addItem(outline)
        line = QGraphicsLineItem(x1, y1, x2, y2)
        line.setPen(pen)
        line.setZValue(z_base - 0.05)
        scene.addItem(line)


# ── Ratsnest ─────────────────────────────────────────────────────────────────


def render_ratsnest(
    scene: QGraphicsScene,
    schematic: Schematic,
    placements: dict[uuid.UUID, ComponentPlacement],
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    routed_nets: set[uuid.UUID] | None,
    theme: Theme = DEFAULT_THEME,
) -> None:
    """Draw crow-flies lines for unrouted pin pairs in incomplete nets."""
    if routed_nets is None:
        return

    color = QColor(theme.ratsnest_color)
    color.setAlpha(theme.ratsnest_alpha)
    pen = QPen(color, theme.ratsnest_width)
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
