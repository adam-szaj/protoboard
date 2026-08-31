from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGraphicsItem

from protoboard.geometry import Point
from protoboard.services.rendering.packages import PrismRenderer, register_renderer


@register_renderer(component_class="led_bar")
class LedRenderer(PrismRenderer):

    def render_body(self, positions: list[Point]) -> list[QGraphicsItem]:
        with self.paint_override(fill=QColor(0, 0, 0)):
            items = super().render_body(positions)

        canvas = self.body_canvas(positions)
        zled = canvas.zAbove()

        for i in range(10):
            led = canvas.addRect((0.0, (i) - 0.35), (3.0, (i) + 0.35), z=zled)
            items.append(led)
        return items
