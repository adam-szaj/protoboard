#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Render a PNG gallery of every footprint + body-shape combination.

For 2-pin axial parts (resistors, diodes, axial inductors, axial caps)
both a horizontal and a vertical mount are drawn into a single PNG so
the user can compare the two orientations.

Run:  uv run python tools/render_footprints.py

Output: docs/footprints/<slug>.png + docs/footprints/README.md (overview).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

# Use the offscreen QPA so the script runs without a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPen  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QGraphicsEllipseItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
)

from protoboard.domain.components.definition import (  # noqa: E402
    ComponentClass,
    ComponentDefinition,
)
from protoboard.domain.components.footprints import (  # noqa: E402
    DIP,
    SIL,
    DualRow,
    FootprintTemplate,
    GridFootprint,
    QuadRow,
    TO220Footprint,
)
from protoboard.domain.components.instance import ComponentInstance  # noqa: E402
from protoboard.domain.layout.placement import ComponentPlacement  # noqa: E402
from protoboard.geometry import Point  # noqa: E402
from protoboard.gui.renderers.body_shapes import body_shape_for  # noqa: E402
from protoboard.gui.theme import DEFAULT_THEME, Theme  # noqa: E402
from protoboard.services.library.body_type_loader import (  # noqa: E402
    load_body_types,
)

OUT_DIR = REPO_ROOT / "docs" / "footprints"


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Spec:
    slug: str
    title: str
    description: str
    footprint: FootprintTemplate
    component_class: ComponentClass
    pin_names: dict[int, str]
    # When non-empty, render N panels in one PNG, each with these pin
    # positions overrides (None = use the footprint's default).
    panels: tuple[tuple[str, dict[int, Point] | None], ...] = (("",  None),)
    # Layout hint for pin number labels:
    # - "auto": radial outward from panel centre (default)
    # - "below": always below the pad (good for grids)
    # - "outer_columns_only": labels on the outermost columns only
    #   (used by quad-row to avoid overlapping inner column labels)
    label_layout: str = "auto"


def _axial_panels(extent: float) -> tuple[tuple[str, dict[int, Point] | None], ...]:
    """Two-panel layout for cylindrical 2-pin parts.

    * "Horizontal mount" — body laid flat on the board, pin 2 along X.
    * "Vertical mount" — body standing on the pin-1 pad, pin 2 one
      raster away (typical close-mount layout for tall caps / R-stands-up).
    """
    return (
        ("Horizontal mount", {2: Point(extent, 0.0)}),
        ("Vertical mount", {2: Point(0.0, 1.0)}),
    )


INVENTORY: list[Spec] = [
    # ---- 2-pin axial cylinders — both orientations ------------------
    Spec(
        slug="resistor_axial",
        title="Resistor — axial 2-pin",
        description="`SingleRow(n=2)`, `component_class=resistor`. "
        "Renders via `CylinderBody` (variant from `resistor_axial.yaml`). "
        "Pin 2 is bendable (reach 1..23 raster units).",
        footprint=SIL(2),
        component_class=ComponentClass.RESISTOR,
        pin_names={},
        panels=_axial_panels(6.0),
    ),
    Spec(
        slug="capacitor_nonpolar",
        title="Capacitor — non-polar (axial)",
        description="`SingleRow(n=2)`, `component_class=capacitor_nonpolar`. "
        "Cylinder body without polarity marker.",
        footprint=SIL(2),
        component_class=ComponentClass.CAPACITOR_NONPOLAR,
        pin_names={},
        panels=_axial_panels(3.0),
    ),
    Spec(
        slug="capacitor_polar",
        title="Capacitor — polar (electrolytic)",
        description="`SingleRow(n=2)`, `component_class=capacitor_polar`. "
        "Cylinder body with `−` stripe on the cathode side; pin 1 = +, pin 2 = −.",
        footprint=SIL(2),
        component_class=ComponentClass.CAPACITOR_POLAR,
        pin_names={1: "+", 2: "-"},
        panels=_axial_panels(3.0),
    ),
    Spec(
        slug="diode_axial",
        title="Diode — axial (1N4148 / 1N4007 …)",
        description="`SingleRow(n=2)`, `component_class=diode`. "
        "Cylinder body with cathode stripe; pin 1 = anode, pin 2 = cathode.",
        footprint=SIL(2),
        component_class=ComponentClass.DIODE,
        pin_names={1: "A", 2: "K"},
        panels=_axial_panels(4.0),
    ),
    Spec(
        slug="diode_led",
        title="LED",
        description="`SingleRow(n=2)`, `component_class=diode_led`. "
        "Same cylinder body family; renderer treats it as polar.",
        footprint=SIL(2),
        component_class=ComponentClass.DIODE_LED,
        pin_names={1: "A", 2: "K"},
        panels=_axial_panels(4.0),
    ),
    Spec(
        slug="inductor_axial",
        title="Inductor — axial",
        description="`SingleRow(n=2)`, `component_class=inductor`. "
        "Cylinder body, no polarity.",
        footprint=SIL(2),
        component_class=ComponentClass.INDUCTOR,
        pin_names={},
        panels=_axial_panels(5.0),
    ),
    Spec(
        slug="fuse_axial",
        title="Fuse — axial",
        description="`SingleRow(n=2)`, `component_class=fuse`. "
        "Resistor cylinder family.",
        footprint=SIL(2),
        component_class=ComponentClass.FUSE,
        pin_names={},
        panels=_axial_panels(5.0),
    ),

    # ---- 3-pin SIL components (TO-92, TO-220) -----------------------
    Spec(
        slug="transistor_to92",
        title="Transistor — TO-92 (BC547 …)",
        description="`SingleRow(n=3)`, `component_class=transistor_npn`. "
        "Rendered with `TO92Body`. Half-circle, three legs.",
        footprint=SIL(3),
        component_class=ComponentClass.TRANSISTOR_NPN,
        pin_names={1: "C", 2: "B", 3: "E"},
    ),
    Spec(
        slug="transistor_to220",
        title="Transistor / regulator — TO-220",
        description="`TO220Footprint(n=3)`. Wider tab body.",
        footprint=TO220Footprint(n=3),
        component_class=ComponentClass.TRANSISTOR_NMOS,
        pin_names={1: "G", 2: "D", 3: "S"},
    ),

    # ---- SIL — generic single in-line ------------------------------
    Spec(
        slug="sil_pin_header_4",
        title="Pin header — SIL × 4",
        description="`SingleRow(n=4)`, `component_class=connector`. "
        "Generic SIL body (no axial / TO-92 dispatch).",
        footprint=SIL(4),
        component_class=ComponentClass.CONNECTOR,
        pin_names={1: "1", 2: "2", 3: "3", 4: "4"},
    ),
    Spec(
        slug="sil_pin_header_8",
        title="Pin header — SIL × 8",
        description="`SingleRow(n=8)`, longer SIL strip.",
        footprint=SIL(8),
        component_class=ComponentClass.CONNECTOR,
        pin_names={i: str(i) for i in range(1, 9)},
    ),
    Spec(
        slug="resistor_network_sil5",
        title="Resistor network — SIL × 5",
        description="`SingleRow(n=5)`, `component_class=resistor`. "
        "Bus pin 1 (common) + four resistors. Renders as a generic SIL.",
        footprint=SIL(5),
        component_class=ComponentClass.RESISTOR,
        pin_names={1: "COM", 2: "R1", 3: "R2", 4: "R3", 5: "R4"},
    ),

    # ---- DIP packages ----------------------------------------------
    Spec(
        slug="dip8",
        title="DIP-8 (LM358, NE555 …)",
        description="`DualRow(n_per_row=4, rows_spacing=3.0)`. "
        "Pin 1 marker is drawn by `SvgDIPBody`.",
        footprint=DIP(8),
        component_class=ComponentClass.IC,
        pin_names={1: "1OUT", 2: "1IN-", 3: "1IN+", 4: "GND",
                   5: "2IN+", 6: "2IN-", 7: "2OUT", 8: "VCC"},
    ),
    Spec(
        slug="dip14",
        title="DIP-14 (74HC04 …)",
        description="`DualRow(n_per_row=7, rows_spacing=3.0)`.",
        footprint=DIP(14),
        component_class=ComponentClass.IC,
        pin_names={i: str(i) for i in range(1, 15)},
    ),
    Spec(
        slug="dip16",
        title="DIP-16",
        description="`DualRow(n_per_row=8, rows_spacing=3.0)`.",
        footprint=DIP(16),
        component_class=ComponentClass.IC,
        pin_names={i: str(i) for i in range(1, 17)},
    ),
    Spec(
        slug="dip28_wide",
        title="DIP-28 wide (ATmega328 …)",
        description="`DualRow(n_per_row=14, rows_spacing=6.0)`. "
        "Wide-body DIP draws a wider rectangle.",
        footprint=DualRow(n_per_row=14, rows_spacing=6.0),
        component_class=ComponentClass.IC,
        pin_names={},
    ),

    # ---- Quad-row (dev modules) ------------------------------------
    Spec(
        slug="quad_row_module",
        title="Quad in-line module (CoreBoard-style)",
        description="`QuadRow(n_per_col=10, col_spacings=(1, 8, 1))`. "
        "Four parallel pin columns; outer pads connect via flying leads, "
        "inner pads expose module signals. "
        "Inner-column pin numbers are omitted in this gallery render — "
        "they would overlap the body or the outer-column labels. "
        "Inner pins follow the spec: left-inner is `n+1..2n`, "
        "right-inner is `2n+1..3n` (numbered bottom-to-top).",
        footprint=QuadRow(n_per_col=10),
        component_class=ComponentClass.IC,
        pin_names={},
        label_layout="outer_columns_only",
    ),

    # ---- Grid / mechanical -----------------------------------------
    Spec(
        slug="grid_3x4",
        title="Grid footprint 3 × 4",
        description="`GridFootprint(rows=3, cols=4)`. Generic body; used "
        "for tactile switches, headers, and one-off mechanical parts.",
        footprint=GridFootprint(rows=3, cols=4),
        component_class=ComponentClass.MECHANICAL,
        pin_names={},
        label_layout="outer_ring_only",
    ),
]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _make_definition(spec: Spec) -> ComponentDefinition:
    return ComponentDefinition(
        name=spec.title,
        footprint=spec.footprint,
        pin_names=dict(spec.pin_names),
        description=spec.description,
        component_class=spec.component_class,
    )


def _make_placement(
    pin_offsets: dict[int, Point] | None = None,
) -> ComponentPlacement:
    return ComponentPlacement(
        component_id=__import__("uuid").uuid4(),
        position=Point(0.0, 0.0),
        rotation=0,
        side="top",
        pin_offsets=pin_offsets or {},
    )


def _pin_world_positions(
    footprint: FootprintTemplate,
    placement: ComponentPlacement,
) -> list[Point]:
    base = footprint.pin_positions()
    return [
        Point(b.x + placement.pin_offsets.get(i + 1, Point(0, 0)).x,
              b.y + placement.pin_offsets.get(i + 1, Point(0, 0)).y)
        for i, b in enumerate(base)
    ]


def _add_pad_marker(
    scene: QGraphicsScene,
    pt: Point,
    pin_number: int,
    pad_radius: float,
    is_pin1: bool,
    theme: Theme,
    skip: bool,
    label_anchor: Point,
    label_layout: str = "auto",
    is_outer_column: bool = True,
    on_outer_ring: bool = True,
) -> None:
    """Render a board-hole + pin-net dot + optional pin1 marker + number.

    *label_anchor* is the bbox centre of the panel; the pin label is
    pushed radially away from this point so it lands outside the body.
    """
    # Board hole — light grey ring.
    hole_r = pad_radius * 0.85
    hole = QGraphicsEllipseItem(pt.x - hole_r, pt.y - hole_r,
                                2 * hole_r, 2 * hole_r)
    hole.setPen(QPen(QColor("#7f7f7f"), 0.04))
    hole.setBrush(QBrush(QColor(theme.board_bg)))
    hole.setZValue(0.5)
    scene.addItem(hole)

    if skip:
        dot_r = pad_radius * 0.18
    else:
        dot_r = pad_radius * 0.45

    dot = QGraphicsEllipseItem(pt.x - dot_r, pt.y - dot_r,
                               2 * dot_r, 2 * dot_r)
    dot.setBrush(QBrush(QColor("#2a8")))
    dot.setPen(QPen(Qt.PenStyle.NoPen))
    dot.setZValue(20.0)
    scene.addItem(dot)

    if is_pin1:
        side = pad_radius * 0.6
        sq = QGraphicsRectItem(pt.x - side, pt.y - side, 2 * side, 2 * side)
        sq.setBrush(QBrush(QColor(255, 200, 0, 220)))
        sq.setPen(QPen(QColor("#a05000"), 0.04))
        sq.setZValue(21.0)
        scene.addItem(sq)

    # Pin number — pushed away from the panel centre so the body
    # doesn't cover it.
    label = QGraphicsSimpleTextItem(str(pin_number))
    font = QFont()
    font.setPointSizeF(0.55)
    label.setFont(font)
    label.setBrush(QBrush(QColor("#202020")))
    br = label.boundingRect()
    import math as _m
    dx = pt.x - label_anchor.x
    dy = pt.y - label_anchor.y
    out_dist = pad_radius + 0.7
    if label_layout == "outer_columns_only" and not is_outer_column:
        return
    if label_layout == "outer_ring_only" and not on_outer_ring:
        return
    if label_layout == "corners_only":
        is_corner = (
            is_outer_column
            and on_outer_ring
            and pt.x != label_anchor.x
            and pt.y != label_anchor.y
        )
        if not is_corner:
            return

    if label_layout == "below":
        off_x, off_y = 0.0, out_dist
    else:
        norm = _m.hypot(dx, dy)
        if norm < 0.5:
            off_x = -out_dist
            off_y = 0.0
        elif abs(dx) < 1e-6:
            off_x = -out_dist
            off_y = 0.0
        elif abs(dy) > abs(dx) and label_layout in ("outer_ring_only", "corners_only"):
            # On the outer ring of a grid, prefer vertical offset for
            # top/bottom edge pins so labels of adjacent pins don't crash.
            off_x = 0.0
            off_y = _m.copysign(out_dist, dy)
        else:
            off_x = _m.copysign(out_dist, dx)
            off_y = 0.0
    label.setPos(
        pt.x + off_x - br.width() / 2,
        pt.y + off_y - br.height() / 2,
    )
    label.setZValue(22.0)
    scene.addItem(label)


def _add_panel_title(
    scene: QGraphicsScene,
    text: str,
    x: float,
    y: float,
    width: float,
) -> None:
    item = QGraphicsSimpleTextItem(text)
    font = QFont()
    font.setPointSizeF(0.4)
    font.setBold(True)
    item.setFont(font)
    item.setBrush(QBrush(QColor("#202020")))
    br = item.boundingRect()
    item.setPos(x + (width - br.width()) / 2, y)
    item.setZValue(20.0)
    scene.addItem(item)


def _add_origin_marker(
    scene: QGraphicsScene,
    pt: Point,
) -> None:
    """Tiny crosshair at the component origin so we can see pin 1's anchor."""
    r = 0.15
    pen = QPen(QColor(255, 80, 80, 200), 0.04)
    h = QGraphicsRectItem(pt.x - r, pt.y - 0.02, 2 * r, 0.04)
    h.setPen(pen)
    h.setBrush(QBrush(QColor(255, 80, 80, 200)))
    h.setZValue(11.0)
    scene.addItem(h)
    v = QGraphicsRectItem(pt.x - 0.02, pt.y - r, 0.04, 2 * r)
    v.setPen(pen)
    v.setBrush(QBrush(QColor(255, 80, 80, 200)))
    v.setZValue(11.0)
    scene.addItem(v)


def _render_panel(
    scene: QGraphicsScene,
    spec: Spec,
    title: str,
    pin_offsets_override: dict[int, Point] | None,
    offset_x: float,
    width_hint: float,
    theme: Theme,
) -> tuple[float, float, float, float]:
    """Render one footprint panel; return (x0, y0, x1, y1) world bbox."""
    defn = _make_definition(spec)
    placement = _make_placement(pin_offsets_override)
    inst = ComponentInstance(
        id=placement.component_id,
        definition_ref=spec.title,
        refdes="?",
        value="",
    )
    positions = _pin_world_positions(spec.footprint, placement)

    # Compute panel bbox so we can position multiple panels side by side.
    xs = [p.x for p in positions]
    ys = [p.y for p in positions]
    pad_r = theme.pad_radius
    margin = 2.0
    x0_local = min(xs) - margin
    x1_local = max(xs) + margin
    y0_local = min(ys) - margin - 1.0  # extra for title
    y1_local = max(ys) + margin

    # Measure title width so the panel is wide enough to fit it.
    title_w = 0.0
    if title:
        probe = QGraphicsSimpleTextItem(title)
        f = QFont()
        f.setPointSizeF(0.4)
        f.setBold(True)
        probe.setFont(f)
        title_w = probe.boundingRect().width() + 1.0
    panel_w = max(x1_local - x0_local, width_hint, title_w)
    # Expand so the centre of the panel sits over the centre of the pins.
    extra = panel_w - (x1_local - x0_local)
    if extra > 0:
        x0_local -= extra / 2.0
        x1_local += extra / 2.0
    dx = offset_x - x0_local

    # Title
    if title:
        _add_panel_title(
            scene, title,
            x=x0_local + dx, y=y0_local,
            width=panel_w,
        )

    # Shift positions by dx so this panel sits next to the previous one.
    shifted = [Point(p.x + dx, p.y) for p in positions]
    shifted_placement = ComponentPlacement(
        component_id=placement.component_id,
        position=Point(dx, 0.0),
        rotation=0,
        side="top",
        pin_offsets=placement.pin_offsets,
    )

    # Body shape (must take the shifted absolute positions).
    shape = body_shape_for(defn, shifted_placement, inst, theme.body_types)
    fill = QColor(theme.comp_fill_color)
    border = QColor(theme.comp_border)
    for item in shape.draw(shifted, fill, border, 1.0, 5.0, theme):
        scene.addItem(item)
    skip_pins = shape.drawn_pin_numbers(shifted, theme)

    # Compute panel centre for radial label placement.
    cx = sum(p.x for p in shifted) / len(shifted)
    cy = sum(p.y for p in shifted) / len(shifted)
    centre = Point(cx, cy)

    # Determine which pins are on the outermost column (max |x - cx|).
    xs_set = sorted({p.x for p in shifted})
    if len(xs_set) >= 2:
        outer_xs = {xs_set[0], xs_set[-1]}
    else:
        outer_xs = set(xs_set)
    ys_set = sorted({p.y for p in shifted})
    if len(ys_set) >= 2:
        outer_ys = {ys_set[0], ys_set[-1]}
    else:
        outer_ys = set(ys_set)

    # Pads + dots + numbers.
    for i, p in enumerate(shifted):
        pin_num = i + 1
        on_outer_ring = (p.x in outer_xs) or (p.y in outer_ys)
        _add_pad_marker(
            scene, p, pin_num,
            pad_radius=pad_r,
            is_pin1=(pin_num == 1),
            theme=theme,
            skip=(pin_num in skip_pins),
            label_anchor=centre,
            label_layout=spec.label_layout,
            is_outer_column=(p.x in outer_xs),
            on_outer_ring=on_outer_ring,
        )

    # Origin marker (pin 1 by default).
    _add_origin_marker(scene, shifted[0] if shifted else Point(dx, 0.0))

    # Expand bbox so the now-outward labels still fit.
    return (
        x0_local + dx - 0.5,
        y0_local,
        x1_local + dx + 0.5,
        y1_local + 0.5,
    )


def _render_spec_to_png(spec: Spec, theme: Theme, out_path: Path) -> None:
    scene = QGraphicsScene()
    scene.setBackgroundBrush(QBrush(QColor("#fafafa")))

    panel_gap = 3.0
    cursor_x = 0.0
    bboxes: list[tuple[float, float, float, float]] = []
    for title, override in spec.panels:
        bbox = _render_panel(
            scene, spec, title, override,
            offset_x=cursor_x,
            width_hint=8.0,
            theme=theme,
        )
        bboxes.append(bbox)
        cursor_x = bbox[2] + panel_gap

    if not bboxes:
        return

    x0 = min(b[0] for b in bboxes)
    y0 = min(b[1] for b in bboxes)
    x1 = max(b[2] for b in bboxes)
    y1 = max(b[3] for b in bboxes)
    source_rect = QRectF(x0, y0, x1 - x0, y1 - y0)

    # Render to a QImage at ~120 px / raster unit so cylinders look smooth.
    scale = 120.0
    img_w = int((x1 - x0) * scale)
    img_h = int((y1 - y0) * scale)
    img = QImage(img_w, img_h, QImage.Format.Format_ARGB32)
    img.fill(QColor("#fafafa"))
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    scene.render(painter, QRectF(0, 0, img_w, img_h), source_rect)
    painter.end()

    img.save(str(out_path))


def main() -> int:
    if QApplication.instance() is None:
        QApplication(sys.argv)

    body_types = load_body_types()
    from dataclasses import replace as _replace
    theme = _replace(DEFAULT_THEME, body_types=body_types)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    overview_lines: list[str] = [
        "# Footprint gallery",
        "",
        "Every entry below is rendered headless via "
        "`tools/render_footprints.py`. Re-generate with:",
        "",
        "```bash",
        "uv run python tools/render_footprints.py",
        "```",
        "",
        "## How footprints map to body shapes",
        "",
        "`body_shape_for(defn, placement, instance, body_types)` "
        "in `gui/renderers/body_shapes.py` is the dispatcher. The choice of "
        "renderer depends on **footprint type** *and* **component class**:",
        "",
        "| Footprint | Component class | Body shape |",
        "|---|---|---|",
        "| `TO220Footprint` | any | `TO220Body` (metal-tab body) |",
        "| `SingleRow(n=2)` | resistor / inductor / fuse / transformer | `CylinderBody` (axial, non-polar) — falls back to `AxialBody` when no body-type YAML is loaded |",
        "| `SingleRow(n=2)` | diode / diode_zener / diode_led | `CylinderBody` (axial, polar, dark cathode stripe) — fallback `DiodeBody` |",
        "| `SingleRow(n=2)` | capacitor_polar / capacitor_nonpolar | `CylinderBody` (radial-family variant) — fallback `RadialBody` |",
        "| `SingleRow(n=3)` | any transistor class | `TO92Body` (half-circle) |",
        "| `SingleRow(n>2)` (other classes) | — | `SvgSILBody` (filled strip) |",
        "| `DualRow(n_per_row, rows_spacing)` | any | `SvgDIPBody` (DIP with notch + pin-1 marker) |",
        "| `QuadRow(n_per_col, col_spacings)` | any | `SvgQuadDIPBody` |",
        "| `GridFootprint` / anything else | — | `GenericBody` (plain rectangle) |",
        "",
        "**CylinderBody** picks horizontal vs vertical based on the actual "
        "pin-to-pin distance: when `dist < (body_length_mm + 1.0) / pitch_mm`, "
        "it switches to vertical-mount (a filled circle on the anchor pad with "
        "a thin lead to the second pad). Otherwise it draws the cylinder lying "
        "flat between the leads.",
        "",
        "## Conventions in these images",
        "",
        "- Green dot = pin centre.",
        "- Yellow square on the pin-1 pad = pin-1 marker (same as in the GUI).",
        "- Tiny red cross-hair = component origin "
        "(`placement.position`, i.e. local (0, 0) — always pin 1 in this gallery).",
        "- For axial 2-pin parts (resistor / diode / cap / inductor / fuse) "
        "the left panel is the typical *horizontal* mount and the right "
        "panel is the *vertical* (close-mount) variant. Vertical mode is "
        "triggered by setting `pin_offsets[2]` so pin 2 sits next to pin 1.",
        "",
        "## Index",
        "",
        "| Slug | Title | Footprint | Class |",
        "|---|---|---|---|",
    ]

    for spec in INVENTORY:
        out_path = OUT_DIR / f"{spec.slug}.png"
        _render_spec_to_png(spec, theme, out_path)
        overview_lines.append(
            f"| [`{spec.slug}`](#{spec.slug}) | {spec.title} | "
            f"`{type(spec.footprint).__name__}` | "
            f"`{spec.component_class.value}` |"
        )
        print(f"  rendered {spec.slug}.png", file=sys.stderr)

    overview_lines.append("")
    overview_lines.append("## Entries")
    overview_lines.append("")

    for spec in INVENTORY:
        overview_lines.append(f"### {spec.slug}")
        overview_lines.append("")
        overview_lines.append(f"**{spec.title}**")
        overview_lines.append("")
        overview_lines.append(spec.description)
        overview_lines.append("")
        overview_lines.append(f"![{spec.slug}]({spec.slug}.png)")
        overview_lines.append("")

    (OUT_DIR / "README.md").write_text("\n".join(overview_lines), encoding="utf-8")
    print(f"wrote {OUT_DIR}/README.md", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
