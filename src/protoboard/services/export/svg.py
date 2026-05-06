# SPDX-License-Identifier: MIT
"""SVG board exporter — renders top layer to a standalone SVG file."""

from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import PadAtom, StripAtom
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement, pin_world_position
from protoboard.domain.layout.wire import WireSegment
from protoboard.gui.theme import DEFAULT_THEME, Theme

_NS = "http://www.w3.org/2000/svg"


def export_svg(
    board: Board,
    layout: Layout,
    components: dict[str, ComponentDefinition],
    instances: dict[uuid.UUID, ComponentInstance],
    scale: float | None = None,
    theme: Theme = DEFAULT_THEME,
) -> str:
    """Render the top layer of the board to an SVG string."""
    if scale is None:
        scale = theme.svg_scale
    bounds = board.bounds
    w = (bounds.width + 2) * scale
    h = (bounds.height + 2) * scale
    ox = (bounds.x - 1) * scale  # scene origin offset
    oy = (bounds.y - 1) * scale

    def sx(x: float) -> str:
        return f"{(x * scale - ox):.3f}"

    def sy(y: float) -> str:
        return f"{(y * scale - oy):.3f}"

    ET.register_namespace("", _NS)
    svg = ET.Element(
        "svg",
        xmlns=_NS,
        width=f"{w:.0f}",
        height=f"{h:.0f}",
        viewBox=f"0 0 {w:.3f} {h:.3f}",
    )

    # Background rect.
    ET.SubElement(
        svg, "rect", x="0", y="0", width=f"{w:.0f}", height=f"{h:.0f}",
        fill=theme.svg_bg,
    )

    # Pads.
    pad_r = theme.pad_radius * scale
    hole_r = pad_r * 0.45
    for atom, pos in board.atoms():
        if isinstance(atom, (PadAtom, StripAtom)):
            ET.SubElement(
                svg,
                "circle",
                attrib={
                    "cx": sx(pos.x),
                    "cy": sy(pos.y),
                    "r": f"{pad_r:.3f}",
                    "fill": theme.strip_color,
                    "stroke": "#000",
                    "stroke-width": f"{theme.pad_outline_width * scale:.3f}",
                },
            )
            ET.SubElement(
                svg,
                "circle",
                cx=sx(pos.x),
                cy=sy(pos.y),
                r=f"{hole_r:.3f}",
                fill=theme.hole_color,
            )

    # Wires.
    for wire in layout.wires:
        if wire.side != "top":
            continue
        color = _wire_color(wire, theme)
        pts = wire.path
        for i in range(len(pts) - 1):
            ET.SubElement(
                svg,
                "line",
                attrib={
                    "x1": sx(pts[i].x),
                    "y1": sy(pts[i].y),
                    "x2": sx(pts[i + 1].x),
                    "y2": sy(pts[i + 1].y),
                    "stroke": color,
                    "stroke-width": f"{theme.ins_width * scale:.3f}",
                    "stroke-linecap": "round",
                },
            )

    # Components.
    for comp_id, placement in layout.placements.items():
        if placement.side != "top":
            continue
        inst = instances.get(comp_id)
        if inst is None:
            continue
        defn = components.get(inst.definition_ref)
        if defn is None:
            continue
        _render_component(svg, placement, inst, defn, sx, sy, scale, theme)

    ET.indent(svg)
    return ET.tostring(svg, encoding="unicode", xml_declaration=False)


def _render_component(
    svg: ET.Element,
    placement: ComponentPlacement,
    inst: ComponentInstance,
    defn: ComponentDefinition,
    sx: Callable[[float], str],
    sy: Callable[[float], str],
    scale: float,
    theme: Theme,
) -> None:
    positions = [
        pin_world_position(defn.footprint, placement, n + 1)
        for n in range(defn.footprint.pin_count())
    ]
    if not positions:
        return

    margin = 0.35
    xs = [p.x for p in positions]
    ys = [p.y for p in positions]
    x0 = min(xs) - margin
    y0 = min(ys) - margin
    rw = max(xs) - min(xs) + 2 * margin
    rh = max(ys) - min(ys) + 2 * margin

    from PySide6.QtGui import QColor as _QColor
    _cf = _QColor(theme.comp_fill_color)
    comp_fill = f"rgba({_cf.red()},{_cf.green()},{_cf.blue()},{_cf.alpha() / 255:.2f})"
    ET.SubElement(
        svg,
        "rect",
        attrib={
            "x": sx(x0),
            "y": sy(y0),
            "width": f"{rw * scale:.3f}",
            "height": f"{rh * scale:.3f}",
            "fill": comp_fill,
            "stroke": theme.comp_border,
            "stroke-width": f"{0.06 * scale:.3f}",
        },
    )
    text = ET.SubElement(
        svg,
        "text",
        attrib={
            "x": sx(x0 + 0.05),
            "y": sy(y0 + 0.6),
            "fill": theme.svg_text_color,
            "font-size": f"{theme.refdes_font_size * scale:.1f}",
            "font-family": "monospace",
        },
    )
    text.text = inst.refdes


def _wire_color(wire: WireSegment, theme: Theme) -> str:
    if not wire.insulated:
        return theme.bare_outline_color
    if wire.color:
        return wire.color
    idx = abs(hash(wire.id)) % len(theme.net_colors)
    return theme.net_colors[idx]
