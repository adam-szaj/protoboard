# SPDX-License-Identifier: MIT
"""Pin-pads layer renderer.

Two scene-level entry points:

* :func:`render_pin_pads` — schematic-aware overlay above the board
  layer (Z_PIN_PADS, 3.5).  PIN_PAD = filled disc in net colour with
  modifier shape carved out of the centre (board background fill);
  CONTACT_ONLY = thin outline ring in net colour with transparent
  centre so the board pad below stays visible.

* :func:`render_pin_modifiers` — top-most overlay (Z_PIN_MODIFIERS,
  9.5).  Repeats the modifier glyph (circle / square / diamond / star)
  above every wire / component / label in the net colour so every pin
  marker remains visible even when something covers the pin pad.  The
  default CIRCLE is included so plain pins are marked consistently with
  the special ones; only CONTACT_ONLY classifications get no glyph.

Routing target (:attr:`session.routing_net_id`) overrides the net
colour with :attr:`Theme.routing_target_color` — a neon-yellow signal
applied uniformly to fill, outline, and modifier glyph for every pad
of the active net.
"""

from __future__ import annotations

import uuid

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainterPath, QPen
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsScene,
)

from protoboard.domain.layout.pin_pad_classification import (
    ModifierShape,
    PinPadClass,
    PinPadInfo,
)
from protoboard.gui.layer_state import (
    LayerState,
    Z_PIN_MODIFIERS,
    Z_PIN_PADS,
)
from protoboard.controller.theme import DEFAULT_THEME, Theme
from protoboard.services.debug_trace import traced


# ---------------------------------------------------------------------------
# Shape geometry
# ---------------------------------------------------------------------------


def _square_path(cx: float, cy: float, r: float) -> QPainterPath:
    """Axis-aligned square, half-edge length *r*."""
    path = QPainterPath()
    path.addRect(QRectF(cx - r, cy - r, 2 * r, 2 * r))
    return path


def _diamond_path(cx: float, cy: float, r: float) -> QPainterPath:
    """Square rotated 45° — vertices at (cx±r, cy) and (cx, cy±r)."""
    path = QPainterPath()
    # make diamond have the same area as a square (2r)^2: r = r * sqrt(2)
    r = r*1.4142 
    path.moveTo(cx, cy - r)
    path.lineTo(cx + r, cy)
    path.lineTo(cx, cy + r)
    path.lineTo(cx - r, cy)
    path.closeSubpath()
    return path


def _star_path(cx: float, cy: float, r: float) -> QPainterPath:
    """Union of axis-aligned square and 45° diamond — 8-pointed star."""
    sq = _square_path(cx, cy, r)
    di = _diamond_path(cx, cy, r)
    return sq.united(di)


def _circle_path(cx: float, cy: float, r: float) -> QPainterPath:
    path = QPainterPath()
    # make circle have the same area as a square (2r)^2: r = r * 2*sqrt(1/pi)
    r = r*1.13
    path.addEllipse(QRectF(cx - r, cy - r, 2 * r, 2 * r))
    return path

def _modifier_path(
    shape: ModifierShape, cx: float, cy: float, r: float,
) -> QPainterPath | None:
    """Return the modifier shape, or None for CIRCLE (no carve / no glyph)."""
    if shape == ModifierShape.SQUARE:
        return _square_path(cx, cy, r)
    if shape == ModifierShape.DIAMOND:
        return _diamond_path(cx, cy, r)
    if shape == ModifierShape.STAR:
        return _star_path(cx, cy, r)
    return _circle_path(cx, cy, r)


def _resolve_color(
    net_id: uuid.UUID,
    net_color_map: dict[uuid.UUID, str | None] | None,
    theme: Theme,
    *,
    routing_target_net_id: uuid.UUID | None,
) -> QColor:
    """Pick the visual colour for a pin-pad / modifier.

    Routing-target overrides everything else (neon yellow signal).
    Otherwise look up the resolved net colour from the shared
    ``net_color_map`` (palette + manual ``net.color`` overrides),
    falling back to the theme palette by hash if unspecified.
    """
    if routing_target_net_id is not None and net_id == routing_target_net_id:
        return QColor(theme.routing_target_color)
    if net_color_map is not None:
        entry = net_color_map.get(net_id)
        if entry is not None:
            return QColor(entry)
    # Fallback: deterministic palette pick.
    if not theme.net_colors:
        return QColor(theme.net_color_unassigned)
    idx = abs(hash(net_id)) % len(theme.net_colors)
    return QColor(theme.net_colors[idx])


# ---------------------------------------------------------------------------
# Scene rendering
# ---------------------------------------------------------------------------


def render_pin_pads(
    scene: QGraphicsScene,
    classifications: list[PinPadInfo],
    *,
    theme: Theme = DEFAULT_THEME,
    layers: LayerState,
    net_color_map: dict[uuid.UUID, str | None] | None = None,
    routing_target_net_id: uuid.UUID | None = None,
) -> list[QGraphicsItem]:
    """Add pin-pads layer items to *scene* and return them for tracking.

    Items are tagged so callers can filter back-side hit tests; tag is
    set via ``setData(0, "pin_pad")``.

    Returns a list of every scene item created.
    """
    pad_r = theme.pad_radius
    mod_r = pad_r * theme.pin_pad_modifier_radius_factor*0.5
    bg = QColor(theme.board_bg)
    out: list[QGraphicsItem] = []

    for info in classifications:
        alpha = layers.alpha_for_pin_pads(info.side)  # type: ignore[arg-type]
        if alpha <= 0.0:
            continue
        z = Z_PIN_PADS - (0.001 if info.side != layers.camera_side else 0.0)
        cx, cy = info.position.x, info.position.y

        if info.klass == PinPadClass.FREE:
            # Unassigned pin terminal — neutral outline ring (no net colour)
            # plus the modifier glyph as an outline so pin1 / exported pins
            # stay recognisable.  Distinct from CONTACT_ONLY (net colour)
            # and from an empty hole (nothing rendered).
            free_color = QColor(theme.pin_free_color)
            free_color.setAlphaF(free_color.alphaF() * alpha)
            ring = QGraphicsEllipseItem(cx - pad_r, cy - pad_r, 2 * pad_r, 2 * pad_r)
            ring.setPen(QPen(free_color, theme.pin_free_width))
            ring.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            ring.setZValue(z)
            ring.setData(0, "pin_pad_free")
            scene.addItem(ring)
            out.append(ring)
            glyph = _modifier_path(info.modifier, cx, cy, mod_r)
            if glyph is not None:
                marker = QGraphicsPathItem(glyph)
                marker.setBrush(QBrush(Qt.BrushStyle.NoBrush))
                marker.setPen(QPen(free_color, theme.pin_free_width))
                marker.setZValue(z + 0.0005)
                marker.setData(0, "pin_pad_free_modifier")
                scene.addItem(marker)
                out.append(marker)
            continue

        # Non-FREE classes always carry a net id (FREE handled + continued above).
        assert info.net_id is not None
        color = _resolve_color(
            info.net_id, net_color_map, theme,
            routing_target_net_id=routing_target_net_id,
        )
        _alpha = color.alphaF() * alpha
        color.setAlphaF(_alpha)

        if info.klass == PinPadClass.PIN_PAD:
            # Filled disc in net colour.  Modifier shape carved as a
            # second item (board_bg fill) sits on top of the disc.
            disc = QGraphicsEllipseItem(cx - pad_r, cy - pad_r, 2 * pad_r, 2 * pad_r)
            disc.setBrush(QBrush(color))
            disc.setPen(QPen(Qt.PenStyle.NoPen))
            disc.setZValue(z)
            disc.setData(0, "pin_pad")
            scene.addItem(disc)
            out.append(disc)
            carve = _modifier_path(info.modifier, cx, cy, mod_r)
            if carve is not None:
                # Modifier carved as the board background — visually a
                # hole shaped like the modifier.  Slightly above the
                # disc.  A thin outline in the standard pad outline
                # colour keeps the modifier visually consistent with the
                # plain pad hole around it.
                carve_color = QColor(bg)
                carve_color.setAlphaF(_alpha*0.3)
                outline_color = QColor(theme.pad_outline_color)
                outline_color.setAlphaF(_alpha*0.3)
                carved = QGraphicsPathItem(carve)
                carved.setBrush(QBrush(carve_color))
                carved.setPen(QPen(outline_color, theme.pad_outline_width))
                carved.setZValue(z + 0.0005)
                carved.setData(0, "pin_pad_modifier_carve")
                scene.addItem(carved)
                out.append(carved)
        else:  # CONTACT_ONLY
            # Thin outline ring in net colour, transparent centre.
            ring = QGraphicsEllipseItem(cx - pad_r, cy - pad_r, 2 * pad_r, 2 * pad_r)
            col = QColor(color)
            col.setAlphaF(0.3)
            pen = QPen(col, theme.pin_pad_contact_only_width)

            pen.setCosmetic(False)
            ring.setPen(pen)
            ring.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            ring.setZValue(z)
            ring.setData(0, "pin_pad_contact_only")
            scene.addItem(ring)
            out.append(ring)
    return out


@traced()
def render_pin_modifiers(
    scene: QGraphicsScene,
    classifications: list[PinPadInfo],
    *,
    theme: Theme = DEFAULT_THEME,
    layers: LayerState,
    net_color_map: dict[uuid.UUID, str | None] | None = None,
    routing_target_net_id: uuid.UUID | None = None,
) -> list[QGraphicsItem]:
    """Add top-most modifier glyphs (filled).

    A glyph is drawn for every PIN_PAD — including the default CIRCLE —
    so all pin markers share one look above the component and the pin
    positions stay visible even when the body fully covers them.  Only
    CONTACT_ONLY is skipped: it has no pin of its own, just net presence.
    """
    pad_r = theme.pad_radius
    mod_r = pad_r * theme.pin_pad_modifier_radius_factor*0.5
    out: list[QGraphicsItem] = []

    for info in classifications:
        if info.klass != PinPadClass.PIN_PAD:
            continue
        assert info.net_id is not None  # PIN_PAD always carries a net
        alpha = layers.alpha_for_pin_modifiers(info.side)  # type: ignore[arg-type]
        if alpha <= 0.0:
            continue
        path = _modifier_path(info.modifier, info.position.x, info.position.y, mod_r)
        if path is None:
            continue
        color = _resolve_color(
            info.net_id, net_color_map, theme,
            routing_target_net_id=routing_target_net_id,
        )
        color.setAlphaF(color.alphaF() * alpha)
        outline_color = QColor(theme.pad_outline_color)
        outline_color.setAlphaF(color.alphaF() * alpha)
        item = QGraphicsPathItem(path)
        item.setBrush(QBrush(color))
        item.setPen(QPen(outline_color, theme.pad_outline_width))
        item.setZValue(Z_PIN_MODIFIERS)
        item.setData(0, "pin_modifier")
        scene.addItem(item)
        out.append(item)
    return out


__all__ = [
    "render_pin_pads",
    "render_pin_modifiers",
]
