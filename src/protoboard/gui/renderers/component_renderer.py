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
from protoboard.domain.layout.placement import ComponentPlacement, pin_world_position
from protoboard.domain.schematic.net import PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point

COMP_FILL = QColor(255, 255, 200, 180)  # pale yellow, semi-transparent
COMP_BORDER = QColor("#444444")
PIN_COLOR = QColor("#CC2222")  # fallback when no schematic
PIN_RADIUS = 0.18
REFDES_COLOR = QColor("#AAAAAA")
_REFDES_FONT = QFont("monospace", 1)
_REFDES_FONT.setPointSizeF(0.55)

# Net colour palette — distinct, moderately saturated
_NET_COLORS = [
    QColor("#E63B3B"),  # red
    QColor("#3B8AE6"),  # blue
    QColor("#2ECC71"),  # green
    QColor("#E6A020"),  # amber
    QColor("#A020E6"),  # purple
    QColor("#20C8E6"),  # cyan
    QColor("#E6207A"),  # pink
    QColor("#7AE620"),  # lime
]


def _net_color(net_id: uuid.UUID) -> QColor:
    return _NET_COLORS[abs(hash(net_id)) % len(_NET_COLORS)]


def render_components(
    scene: QGraphicsScene,
    placements: dict[uuid.UUID, ComponentPlacement],
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    schematic: Schematic | None = None,
    routed_nets: set[uuid.UUID] | None = None,
) -> None:
    # Build pin→net map once for all components if schematic is available.
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
        _render_one(scene, placement, inst, defn, comp_id, pin_net, routed_nets)


def _render_one(
    scene: QGraphicsScene,
    placement: ComponentPlacement,
    inst: ComponentInstance,
    defn: ComponentDefinition,
    comp_id: uuid.UUID,
    pin_net: dict[PinRef, uuid.UUID],
    routed_nets: set[uuid.UUID] | None,
) -> None:
    fp = defn.footprint
    positions = [
        pin_world_position(fp, placement, n + 1) for n in range(fp.pin_count())
    ]
    if not positions:
        return

    # Bounding box of all pins (with a small margin).
    margin = 0.35
    xs = [p.x for p in positions]
    ys = [p.y for p in positions]
    x0, y0 = min(xs) - margin, min(ys) - margin
    x1, y1 = max(xs) + margin, max(ys) + margin

    rect_item = QGraphicsRectItem(QRectF(x0, y0, x1 - x0, y1 - y0))
    rect_item.setBrush(QBrush(COMP_FILL))
    rect_item.setPen(QPen(COMP_BORDER, 0.06))
    scene.addItem(rect_item)

    # Pin dots — coloured by net if schematic is present.
    for pin_idx, pos in enumerate(positions):
        pin_number = pin_idx + 1
        ref = PinRef(component_id=comp_id, pin_number=pin_number)
        net_id = pin_net.get(ref)

        if net_id is not None:
            color = QColor(_net_color(net_id))
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
            fill_color = PIN_COLOR
            pen = QPen(Qt.GlobalColor.transparent, 0)

        if pin_number == 1:
            _add_pin1_marker(scene, pos.x, pos.y, fill_color)
        else:
            dot = QGraphicsEllipseItem(
                QRectF(
                    pos.x - PIN_RADIUS, pos.y - PIN_RADIUS,
                    2 * PIN_RADIUS, 2 * PIN_RADIUS,
                )
            )
            dot.setBrush(QBrush(fill_color))
            dot.setPen(pen)
            scene.addItem(dot)

    # refdes label — centred inside bbox unless label_offset differs from default.
    label = QGraphicsTextItem(inst.refdes)
    label.setDefaultTextColor(REFDES_COLOR)
    label.setFont(_REFDES_FONT)
    _default_offset = Point(0.05, 0.05)
    if placement.label_offset == _default_offset:
        # Auto-centre: place so text centre aligns with component bbox centre.
        br = label.boundingRect()
        cx = (x0 + x1) / 2.0 - br.width() / 2.0
        cy = (y0 + y1) / 2.0 - br.height() / 2.0
        label.setPos(cx, cy)
    else:
        label.setPos(x0 + placement.label_offset.x, y0 + placement.label_offset.y)
    scene.addItem(label)


_PIN1_COLOR = QColor("#FFFFFF")   # white triangle — visible on any background
_PIN1_R = PIN_RADIUS * 1.1        # slightly larger than regular pin


def _add_pin1_marker(
    scene: QGraphicsScene, cx: float, cy: float, fill_color: QColor
) -> None:
    """Draw a square marker for pin 1 (instead of the usual circle)."""
    r = _PIN1_R
    square = QGraphicsRectItem(QRectF(cx - r, cy - r, 2 * r, 2 * r))
    square.setBrush(QBrush(fill_color))
    square.setPen(QPen(_PIN1_COLOR, 0.06))
    scene.addItem(square)


def make_ghost_item(
    defn: ComponentDefinition,
    rotation: int = 0,
) -> QGraphicsItemGroup:
    """Return a semi-transparent QGraphicsItemGroup previewing *defn* at origin.

    The group is positioned at (0, 0) — caller moves it by setting pos().
    It is NOT added to any scene; caller must do scene.addItem(group).
    """
    # Synthetic placement at origin for position calculation.
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

    ghost_fill = QColor(255, 255, 200, 80)
    ghost_border = QColor(255, 255, 100, 160)
    ghost_pin = QColor(200, 80, 80, 160)

    group = QGraphicsItemGroup()

    rect_item = QGraphicsRectItem(QRectF(x0, y0, x1 - x0, y1 - y0))
    rect_item.setBrush(QBrush(ghost_fill))
    rect_item.setPen(QPen(ghost_border, 0.06, Qt.PenStyle.DashLine))
    group.addToGroup(rect_item)

    for pos in positions:
        r = PIN_RADIUS
        dot = QGraphicsEllipseItem(
            QRectF(pos.x - r, pos.y - r, 2 * r, 2 * r)
        )
        dot.setBrush(QBrush(ghost_pin))
        dot.setPen(QPen(Qt.GlobalColor.transparent, 0))
        group.addToGroup(dot)

    label = QGraphicsTextItem(defn.name)
    label.setDefaultTextColor(QColor(220, 220, 100, 180))
    label.setFont(_REFDES_FONT)
    label.setPos(x0 + 0.05, y0 + 0.05)
    group.addToGroup(label)

    return group
