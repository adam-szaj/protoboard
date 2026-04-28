# SPDX-License-Identifier: MIT
"""Renderer for placed components (rectangle + refdes + pin dots)."""

from __future__ import annotations

import uuid

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPen
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsTextItem,
)

from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.placement import ComponentPlacement, pin_world_position

COMP_FILL = QColor(255, 255, 200, 180)  # pale yellow, semi-transparent
COMP_BORDER = QColor("#444444")
PIN_COLOR = QColor("#CC2222")
PIN_RADIUS = 0.18
REFDES_COLOR = QColor("#111111")
_REFDES_FONT = QFont("monospace", 1)
_REFDES_FONT.setPointSizeF(0.55)


def render_components(
    scene: QGraphicsScene,
    placements: dict[uuid.UUID, ComponentPlacement],
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
) -> None:
    for comp_id, placement in placements.items():
        inst = instances.get(comp_id)
        if inst is None:
            continue
        defn = components.get(inst.definition_ref)
        if defn is None:
            continue
        _render_one(scene, placement, inst, defn)


def _render_one(
    scene: QGraphicsScene,
    placement: ComponentPlacement,
    inst: ComponentInstance,
    defn: ComponentDefinition,
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

    # Pin dots.
    for pos in positions:
        dot = QGraphicsEllipseItem(
            QRectF(
                pos.x - PIN_RADIUS, pos.y - PIN_RADIUS, 2 * PIN_RADIUS, 2 * PIN_RADIUS
            )
        )
        dot.setBrush(QBrush(PIN_COLOR))
        dot.setPen(QPen(Qt.GlobalColor.transparent, 0))
        scene.addItem(dot)

    # refdes label.
    label = QGraphicsTextItem(inst.refdes)
    label.setDefaultTextColor(REFDES_COLOR)
    label.setFont(_REFDES_FONT)
    label.setPos(x0 + 0.05, y0 + 0.05)
    scene.addItem(label)
