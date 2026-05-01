# SPDX-License-Identifier: MIT
"""Renderer for placed components (rectangle + refdes + net-coloured pins)."""

from __future__ import annotations

import uuid

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPen
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItemGroup,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsTextItem,
)

from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement, pin_world_position
from protoboard.domain.schematic.net import PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.gui.theme import DEFAULT_THEME, Theme

_REFDES_FONT = QFont("monospace", 1)
_REFDES_FONT.setPointSizeF(DEFAULT_THEME.refdes_font_size)


def _net_color(net_id: uuid.UUID, theme: Theme = DEFAULT_THEME) -> QColor:
    colors = theme.pin_net_colors
    return QColor(colors[abs(hash(net_id)) % len(colors)])


def render_components(
    scene: QGraphicsScene,
    placements: dict[uuid.UUID, ComponentPlacement],
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    schematic: Schematic | None = None,
    routed_nets: set[uuid.UUID] | None = None,
    theme: Theme = DEFAULT_THEME,
) -> None:
    pin_net: dict[PinRef, uuid.UUID] = {}
    if schematic is not None:
        for net in schematic.nets.values():
            for ref in net.pins:
                pin_net[ref] = net.id

    for comp_id, placement in placements.items():
        inst = instances.get(comp_id)
        if inst is None:
            continue
        defn = components.get(inst.definition_ref)
        if defn is None:
            continue
        _render_one(scene, placement, inst, defn, comp_id, pin_net, routed_nets, theme)


def _render_one(
    scene: QGraphicsScene,
    placement: ComponentPlacement,
    inst: ComponentInstance,
    defn: ComponentDefinition,
    comp_id: uuid.UUID,
    pin_net: dict[PinRef, uuid.UUID],
    routed_nets: set[uuid.UUID] | None,
    theme: Theme,
) -> None:
    fp = defn.footprint
    positions = [
        pin_world_position(fp, placement, n + 1) for n in range(fp.pin_count())
    ]
    if not positions:
        return

    margin = 0.35
    xs = [p.x for p in positions]
    ys = [p.y for p in positions]
    x0, y0 = min(xs) - margin, min(ys) - margin
    x1, y1 = max(xs) + margin, max(ys) + margin

    comp_fill = QColor(theme.comp_fill_r, theme.comp_fill_g, theme.comp_fill_b, theme.comp_fill_a)
    comp_border = QColor(theme.comp_border)
    rect_item = QGraphicsRectItem(QRectF(x0, y0, x1 - x0, y1 - y0))
    rect_item.setBrush(QBrush(comp_fill))
    rect_item.setPen(QPen(comp_border, 0.06))
    scene.addItem(rect_item)

    pin_color_fallback = QColor(theme.pin_color_fallback)
    for pin_idx, pos in enumerate(positions):
        pin_number = pin_idx + 1
        ref = PinRef(component_id=comp_id, pin_number=pin_number)
        net_id = pin_net.get(ref)

        if net_id is not None:
            color = _net_color(net_id, theme)
            complete = routed_nets is None or net_id in routed_nets
            if complete:
                fill_color = color
                pen = QPen(color, 0.04)
                pen.setStyle(Qt.PenStyle.SolidLine)
            else:
                fill_color = QColor(color)
                fill_color.setAlpha(130)
                pen = QPen(color, 0.06)
                pen.setStyle(Qt.PenStyle.DashLine)
        else:
            fill_color = pin_color_fallback
            pen = QPen(Qt.GlobalColor.transparent, 0)

        if pin_number == 1:
            _add_pin1_marker(scene, pos.x, pos.y, fill_color, theme)
        else:
            r = theme.pin_radius
            dot = QGraphicsEllipseItem(QRectF(pos.x - r, pos.y - r, 2 * r, 2 * r))
            dot.setBrush(QBrush(fill_color))
            dot.setPen(pen)
            scene.addItem(dot)

    label = QGraphicsTextItem(inst.refdes)
    label.setDefaultTextColor(QColor(theme.refdes_color))
    label.setFont(_REFDES_FONT)
    _default_offset = Point(0.05, 0.05)
    if placement.label_offset == _default_offset:
        br = label.boundingRect()
        cx = (x0 + x1) / 2.0 - br.width() / 2.0
        cy = (y0 + y1) / 2.0 - br.height() / 2.0
        label.setPos(cx, cy)
    else:
        label.setPos(x0 + placement.label_offset.x, y0 + placement.label_offset.y)
    scene.addItem(label)


def _add_pin1_marker(
    scene: QGraphicsScene, cx: float, cy: float, fill_color: QColor, theme: Theme
) -> None:
    r = theme.pin_radius * 1.1
    square = QGraphicsRectItem(QRectF(cx - r, cy - r, 2 * r, 2 * r))
    square.setBrush(QBrush(fill_color))
    square.setPen(QPen(QColor("#FFFFFF"), 0.06))
    scene.addItem(square)


def make_ghost_item(
    defn: ComponentDefinition,
    rotation: int = 0,
    theme: Theme = DEFAULT_THEME,
) -> QGraphicsItemGroup:
    """Return a semi-transparent QGraphicsItemGroup previewing *defn* at origin."""
    placement = ComponentPlacement(
        component_id=uuid.UUID(int=0),
        position=Point(0.0, 0.0),
        rotation=rotation,
        side="top",
    )
    fp = defn.footprint
    positions = [
        pin_world_position(fp, placement, n + 1) for n in range(fp.pin_count())
    ]
    if not positions:
        return QGraphicsItemGroup()

    margin = 0.35
    xs = [p.x for p in positions]
    ys = [p.y for p in positions]
    x0, y0 = min(xs) - margin, min(ys) - margin
    x1, y1 = max(xs) + margin, max(ys) + margin

    t = theme
    ghost_fill = QColor(t.ghost_fill_r, t.ghost_fill_g, t.ghost_fill_b, t.ghost_fill_a)
    ghost_border = QColor(t.ghost_border_r, t.ghost_border_g, t.ghost_border_b, t.ghost_border_a)
    ghost_pin = QColor(t.ghost_pin_r, t.ghost_pin_g, t.ghost_pin_b, t.ghost_pin_a)
    ghost_label = QColor(t.ghost_label_r, t.ghost_label_g, t.ghost_label_b, t.ghost_label_a)

    group = QGraphicsItemGroup()

    rect_item = QGraphicsRectItem(QRectF(x0, y0, x1 - x0, y1 - y0))
    rect_item.setBrush(QBrush(ghost_fill))
    rect_item.setPen(QPen(ghost_border, 0.06, Qt.PenStyle.DashLine))
    group.addToGroup(rect_item)

    r = theme.pin_radius
    for pos in positions:
        dot = QGraphicsEllipseItem(QRectF(pos.x - r, pos.y - r, 2 * r, 2 * r))
        dot.setBrush(QBrush(ghost_pin))
        dot.setPen(QPen(Qt.GlobalColor.transparent, 0))
        group.addToGroup(dot)

    label = QGraphicsTextItem(defn.name)
    label.setDefaultTextColor(ghost_label)
    label.setFont(_REFDES_FONT)
    label.setPos(x0 + 0.05, y0 + 0.05)
    group.addToGroup(label)
    return group


# ---------------------------------------------------------------------------
# Group rendering
# ---------------------------------------------------------------------------


def render_groups(
    scene: QGraphicsScene,
    layout: Layout,
    placements: dict[uuid.UUID, ComponentPlacement],
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    theme: Theme = DEFAULT_THEME,
) -> None:
    """Render a dotted outline around each group's bounding box."""
    from protoboard.domain.layout.group import all_component_ids

    t = theme
    group_pen = QPen(
        QColor(t.group_pen_r, t.group_pen_g, t.group_pen_b, t.group_pen_a),
        t.group_pen_width,
    )
    group_pen.setStyle(Qt.PenStyle.DotLine)
    group_fill = QColor(t.group_fill_r, t.group_fill_g, t.group_fill_b, t.group_fill_a)
    group_name_color = QColor(t.group_name_r, t.group_name_g, t.group_name_b, t.group_name_a)

    for group in layout.groups.values():
        comp_ids = all_component_ids(layout.groups, group.id)
        positions: list[Point] = []
        for cid in comp_ids:
            pl = placements.get(cid)
            inst = instances.get(cid)
            if pl is None or inst is None:
                continue
            defn = components.get(inst.definition_ref)
            if defn is None:
                continue
            for n in range(defn.footprint.pin_count()):
                positions.append(pin_world_position(defn.footprint, pl, n + 1))
        if not positions:
            continue
        xs = [p.x for p in positions]
        ys = [p.y for p in positions]
        m = t.group_margin
        x0 = min(xs) - m
        y0 = min(ys) - m
        w = max(xs) - min(xs) + 2 * m
        h = max(ys) - min(ys) + 2 * m
        rect = QGraphicsRectItem(QRectF(x0, y0, w, h))
        rect.setPen(group_pen)
        rect.setBrush(QBrush(group_fill))
        scene.addItem(rect)
        name_label = QGraphicsTextItem(group.name)
        name_label.setDefaultTextColor(group_name_color)
        name_label.setFont(_REFDES_FONT)
        name_label.setPos(x0 + 0.1, y0 + 0.05)
        scene.addItem(name_label)
