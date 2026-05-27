# SPDX-License-Identifier: MIT
"""Renderer for wire segments and ratsnest."""

from __future__ import annotations

import uuid

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainterPath, QPen
from PySide6.QtWidgets import (
    QGraphicsItemGroup, QGraphicsLineItem,
    QGraphicsPathItem, QGraphicsScene,
)

from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.placement import ComponentPlacement, pin_world_position
from protoboard.domain.layout.wire import Fragment, Wire, Wire
from protoboard.domain.schematic.net import PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.gui.layer_state import LayerState, Z_FRONT_WIRES
from protoboard.gui.theme import DEFAULT_THEME, Theme

_Z_OUTLINE_OFFSET = -0.05
_DEFAULT_LAYERS = LayerState()


def _wire_painter_path(pts: list[Point]) -> QPainterPath:
    """Build a single QPainterPath spanning *pts*.

    Rendering a wire as one path (instead of N separate line items) makes
    Qt apply the pen's JoinStyle at interior vertices (so collinear
    pad-nodes inserted by simplify/load don't get an extra RoundCap bulge
    — the user-visible "oblewanie" effect).  Caps still apply at the two
    real endpoints.
    """
    path = QPainterPath()
    if not pts:
        return path
    path.moveTo(pts[0].x, pts[0].y)
    for p in pts[1:]:
        path.lineTo(p.x, p.y)
    return path


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


def wire_fingerprint(
    wire: Wire,
    net_id: uuid.UUID | None,
    color_override: str | None,
    z_base: float,
    base_alpha: float = 1.0,
    is_bold: bool = False,
) -> tuple[object, ...]:
    """Stable key capturing structural/connectivity inputs for one wire.

    Net-alpha dim (the ``highlight_dim_wires`` reduction for non-highlighted
    nets) is applied via setOpacity and stays out of the fingerprint.
    ``is_bold`` IS in the fingerprint — bold is encoded in pen widths on
    scene items, so a rebuild is required to apply or remove it.
    base_alpha is included so layer visibility changes trigger a rebuild.
    """
    return (
        tuple((p.x, p.y) for p in wire.path),
        wire.insulated,
        wire.side,
        net_id,
        color_override,
        round(z_base, 4),
        round(base_alpha, 4),
        is_bold,
    )


def update_wire_groups(
    scene: QGraphicsScene,
    wires: list[Wire],
    wire_net_map: dict[uuid.UUID, uuid.UUID],
    existing_groups: dict[uuid.UUID, QGraphicsItemGroup],
    existing_fingerprints: dict[uuid.UUID, tuple[object, ...]],
    net_color_map: dict[uuid.UUID, str | None] | None = None,
    theme: Theme = DEFAULT_THEME,
    layers: LayerState = _DEFAULT_LAYERS,
    highlighted_net_id: uuid.UUID | None = None,
) -> None:
    """Create/update/remove per-wire QGraphicsItemGroups without full scene clear.

    Groups whose fingerprint is unchanged are left untouched.
    Removed wires have their group removed from the scene.
    New or changed wires get a freshly-built group.
    """
    new_ids = {w.id for w in wires}

    # Remove groups for wires that no longer exist.
    for wid in list(existing_groups):
        if wid not in new_ids:
            scene.removeItem(existing_groups.pop(wid))
            existing_fingerprints.pop(wid, None)

    for wire in wires:
        net_id = wire_net_map.get(wire.id)
        color_override: str | None = (
            net_color_map.get(net_id) if net_id is not None and net_color_map else None
        )
        side = wire.side  # type: ignore[assignment]
        base_alpha = layers.alpha_for_wire(side)
        z_base = layers.z_for_wire(side)
        # Tier-1 bold for wires belonging to the highlighted net.
        is_bold = (
            highlighted_net_id is not None
            and net_id is not None
            and net_id == highlighted_net_id
        )
        fp = wire_fingerprint(wire, net_id, color_override, z_base, base_alpha, is_bold)

        if wire.id in existing_groups:
            grp = existing_groups[wire.id]
            if existing_fingerprints.get(wire.id) != fp:
                # Structure changed — rebuild.
                scene.removeItem(existing_groups.pop(wire.id))
                existing_fingerprints.pop(wire.id, None)
                if base_alpha == 0.0:
                    continue
                grp = _build_wire_group(
                    wire, net_id, color_override, base_alpha, z_base, theme,
                    glow=False, bold=is_bold,
                )
                scene.addItem(grp)
                existing_groups[wire.id] = grp
                existing_fingerprints[wire.id] = fp
        else:
            if base_alpha == 0.0:
                continue
            grp = _build_wire_group(
                wire, net_id, color_override, base_alpha, z_base, theme,
                glow=False, bold=is_bold,
            )
            scene.addItem(grp)
            existing_groups[wire.id] = grp
            existing_fingerprints[wire.id] = fp
            grp = existing_groups[wire.id]

        # Apply non-highlighted-wire dim via opacity (cheap, no rebuild).
        net_alpha = 1.0 if highlighted_net_id is None or is_bold else theme.highlight_dim_wires
        grp.setOpacity(net_alpha)


def _build_wire_group(
    wire: Wire,
    net_id: uuid.UUID | None,
    color_override: str | None,
    alpha: float,
    z_base: float,
    theme: Theme,
    glow: bool,
    bold: bool = False,
) -> QGraphicsItemGroup:
    """Build a QGraphicsItemGroup containing all line items for one wire.

    *glow* — tier-2 highlight (reserved for direct wire selection).
    *bold* — tier-1 highlight (routing-in-progress, Net Inspector selection).
    Bold thickens both the core stroke (kept in the net colour) and the
    outline (kept in its theme colour — outline distinguishes bare from
    insulated, not nets).
    """
    grp = QGraphicsItemGroup()
    pts = wire.path

    color = _net_color(net_id, color_override, theme)
    if alpha < 1.0:
        color = _dim(color, alpha)

    if glow:
        glow_color = _net_color(net_id, color_override, theme)
        glow_color.setAlphaF(theme.route_glow_wire_alpha * alpha)
        glow_pen = QPen(glow_color, theme.route_glow_wire_width)
        glow_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        glow_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        for i in range(len(pts) - 1):
            x1, y1 = pts[i].x, pts[i].y
            x2, y2 = pts[i + 1].x, pts[i + 1].y
            g = QGraphicsLineItem(x1, y1, x2, y2)
            g.setPen(glow_pen)
            g.setZValue(z_base - 0.1)
            grp.addToGroup(g)

    if wire.insulated:
        _fill_insulated(grp, pts, color, z_base, theme, bold=bold)
    else:
        _fill_bare(grp, pts, color, z_base, theme, bold=bold)

    grp.setZValue(z_base)
    return grp


# ---------------------------------------------------------------------------
# v2 — Wire / Fragment-aware rendering
# ---------------------------------------------------------------------------


def wire_fingerprint_v2(
    wire: Wire,
    net_id: uuid.UUID | None,
    color_override: str | None,
    z_base: float,
    base_alpha: float = 1.0,
    is_bold: bool = False,
) -> tuple[object, ...]:
    """Fingerprint for a v2 Wire — accounts for every fragment's side+path.

    The whole wire is one render group, so a single rebuild trigger
    captures all fragments at once.  ``z_base``/``base_alpha`` come from
    the *first* fragment's side; per-fragment Z is applied during build
    (so a top fragment in a mostly-bottom wire still renders above the
    board layer).
    """
    return (
        tuple(
            (f.side, tuple((p.x, p.y) for p in f.path))
            for f in wire.fragments
        ),
        wire.insulated,
        net_id,
        color_override,
        round(z_base, 4),
        round(base_alpha, 4),
        is_bold,
    )


def update_wire_groups_v2(
    scene: QGraphicsScene,
    wires: list[Wire],
    wire_net_map: dict[uuid.UUID, uuid.UUID],
    existing_groups: dict[uuid.UUID, QGraphicsItemGroup],
    existing_fingerprints: dict[uuid.UUID, tuple[object, ...]],
    net_color_map: dict[uuid.UUID, str | None] | None = None,
    theme: Theme = DEFAULT_THEME,
    layers: LayerState = _DEFAULT_LAYERS,
    highlighted_net_id: uuid.UUID | None = None,
) -> None:
    """v2 counterpart of ``update_wire_groups`` operating on ``Wire`` objects."""
    new_ids = {w.id for w in wires}
    for wid in list(existing_groups):
        if wid not in new_ids:
            scene.removeItem(existing_groups.pop(wid))
            existing_fingerprints.pop(wid, None)

    for wire in wires:
        net_id = wire_net_map.get(wire.id)
        color_override: str | None = (
            net_color_map.get(net_id) if net_id is not None and net_color_map else None
        )
        # Z and alpha follow the FIRST fragment's side; per-fragment Z is
        # applied during build so other-side fragments render at the
        # correct depth.
        side = wire.fragments[0].side
        base_alpha = layers.alpha_for_wire(side)
        z_base = layers.z_for_wire(side)
        is_bold = (
            highlighted_net_id is not None
            and net_id is not None
            and net_id == highlighted_net_id
        )
        fp = wire_fingerprint_v2(wire, net_id, color_override, z_base, base_alpha, is_bold)

        if wire.id in existing_groups:
            if existing_fingerprints.get(wire.id) != fp:
                scene.removeItem(existing_groups.pop(wire.id))
                existing_fingerprints.pop(wire.id, None)
                if base_alpha == 0.0:
                    continue
                grp = _build_wire_group_v2(
                    wire, net_id, color_override, base_alpha, z_base, theme,
                    layers, glow=False, bold=is_bold,
                )
                scene.addItem(grp)
                existing_groups[wire.id] = grp
                existing_fingerprints[wire.id] = fp
            else:
                grp = existing_groups[wire.id]
        else:
            if base_alpha == 0.0:
                continue
            grp = _build_wire_group_v2(
                wire, net_id, color_override, base_alpha, z_base, theme,
                layers, glow=False, bold=is_bold,
            )
            scene.addItem(grp)
            existing_groups[wire.id] = grp
            existing_fingerprints[wire.id] = fp

        net_alpha = 1.0 if highlighted_net_id is None or is_bold else theme.highlight_dim_wires
        existing_groups[wire.id].setOpacity(net_alpha)


def _build_wire_group_v2(
    wire: Wire,
    net_id: uuid.UUID | None,
    color_override: str | None,
    alpha: float,
    z_base: float,
    theme: Theme,
    layers: LayerState,
    glow: bool,
    bold: bool = False,
) -> QGraphicsItemGroup:
    """Build a QGraphicsItemGroup for one v2 Wire — one group, N fragments."""
    grp = QGraphicsItemGroup()
    color = _net_color(net_id, color_override, theme)
    if alpha < 1.0:
        color = _dim(color, alpha)

    for frag in wire.fragments:
        pts = list(frag.path)
        # Use the fragment's own side for Z so a top fragment in a wire
        # whose first side is bottom still renders above the board layer.
        frag_alpha = layers.alpha_for_wire(frag.side)
        frag_z = layers.z_for_wire(frag.side)
        frag_color = QColor(color)
        if frag_alpha < 1.0:
            frag_color = _dim(frag_color, frag_alpha / max(alpha, 1e-6))

        if glow:
            glow_color = _net_color(net_id, color_override, theme)
            glow_color.setAlphaF(theme.route_glow_wire_alpha * alpha)
            glow_pen = QPen(glow_color, theme.route_glow_wire_width)
            glow_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            glow_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            for i in range(len(pts) - 1):
                x1, y1 = pts[i].x, pts[i].y
                x2, y2 = pts[i + 1].x, pts[i + 1].y
                g = QGraphicsLineItem(x1, y1, x2, y2)
                g.setPen(glow_pen)
                g.setZValue(frag_z - 0.1)
                grp.addToGroup(g)

        if wire.insulated:
            _fill_insulated(grp, pts, frag_color, frag_z, theme, bold=bold)
        else:
            _fill_bare(grp, pts, frag_color, frag_z, theme, bold=bold)

    # Via markers at fragment boundaries (side transitions).
    via_color = QColor(getattr(theme, "via_marker_color", "#E0E0E0"))
    via_radius = float(getattr(theme, "via_marker_radius", 0.18))
    for i in range(len(wire.fragments) - 1):
        if wire.fragments[i].side == wire.fragments[i + 1].side:
            continue
        boundary = wire.fragments[i].path[-1]
        marker = _build_via_marker(boundary, via_radius, via_color, z_base + 0.01)
        grp.addToGroup(marker)

    grp.setZValue(z_base)
    return grp


def _build_via_marker(
    pos: Point, radius: float, color: QColor, z: float,
) -> QGraphicsPathItem:
    """Small filled circle marking a plated-through via point."""
    path = QPainterPath()
    path.addEllipse(pos.x - radius, pos.y - radius, 2 * radius, 2 * radius)
    item = QGraphicsPathItem(path)
    item.setPen(QPen(color, 0.02))
    item.setBrush(color)
    item.setZValue(z)
    return item


def render_wires_with_nets(
    scene: QGraphicsScene,
    wires: list[Wire],
    wire_net_map: dict[uuid.UUID, uuid.UUID],
    net_color_map: dict[uuid.UUID, str | None] | None = None,
    theme: Theme = DEFAULT_THEME,
    layers: LayerState = _DEFAULT_LAYERS,
    highlighted_net_id: uuid.UUID | None = None,
) -> None:
    """Render wires directly to scene (cold path / full rebuild)."""
    for wire in wires:
        net_id = wire_net_map.get(wire.id)
        color_override: str | None = None
        if net_id is not None and net_color_map:
            color_override = net_color_map.get(net_id)
        is_highlighted = highlighted_net_id is not None and net_id == highlighted_net_id
        net_alpha = 1.0 if highlighted_net_id is None or is_highlighted else theme.highlight_dim_wires
        _draw_wire(scene, wire, net_id, color_override, theme, layers,
                   net_alpha=net_alpha, glow=is_highlighted)


def _draw_wire(
    scene: QGraphicsScene,
    wire: Wire,
    net_id: uuid.UUID | None,
    color_override: str | None = None,
    theme: Theme = DEFAULT_THEME,
    layers: LayerState = _DEFAULT_LAYERS,
    net_alpha: float = 1.0,
    glow: bool = False,
) -> None:
    pts = wire.path
    side = wire.side  # type: ignore[assignment]
    alpha = layers.alpha_for_wire(side) * net_alpha
    if alpha == 0.0:
        return
    z_base = layers.z_for_wire(side)
    grp = _build_wire_group(wire, net_id, color_override, alpha, z_base, theme, glow)
    scene.addItem(grp)


def _fill_insulated(
    grp: QGraphicsItemGroup,
    pts: list[Point],
    color: QColor,
    z_base: float,
    theme: Theme,
    bold: bool = False,
) -> None:
    core_w = theme.ins_width * (theme.bold_wire_core_width_factor if bold else 1.0)
    outline_w = theme.ins_outline_width * (
        theme.bold_wire_outline_width_factor if bold else 1.0
    )
    outline_pen = QPen(QColor(theme.ins_outline_color), outline_w)
    outline_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    outline_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    core_pen = QPen(color, core_w)
    core_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    core_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    for i in range(len(pts) - 1):
        x1, y1 = pts[i].x, pts[i].y
        x2, y2 = pts[i + 1].x, pts[i + 1].y
        outline = QGraphicsLineItem(x1, y1, x2, y2)
        outline.setPen(outline_pen)
        outline.setZValue(z_base + _Z_OUTLINE_OFFSET)
        grp.addToGroup(outline)
        core = QGraphicsLineItem(x1, y1, x2, y2)
        core.setPen(core_pen)
        core.setZValue(z_base)
        grp.addToGroup(core)


def _fill_bare(
    grp: QGraphicsItemGroup,
    pts: list[Point],
    color: QColor,
    z_base: float,
    theme: Theme,
    bold: bool = False,
) -> None:
    core_w = theme.bare_width * (theme.bold_wire_core_width_factor if bold else 1.0)
    outline_w = theme.bare_outline_width * (
        theme.bold_wire_outline_width_factor if bold else 1.0
    )
    outline_pen = QPen(QColor(theme.bare_outline_color), outline_w)
    outline_pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    outline_pen.setJoinStyle(Qt.PenJoinStyle.BevelJoin)
    pen = QPen(color, core_w)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    for i in range(len(pts) - 1):
        x1, y1 = pts[i].x, pts[i].y
        x2, y2 = pts[i + 1].x, pts[i + 1].y
        outline = QGraphicsLineItem(x1, y1, x2, y2)
        outline.setPen(outline_pen)
        outline.setZValue(z_base + _Z_OUTLINE_OFFSET)
        grp.addToGroup(outline)
        line = QGraphicsLineItem(x1, y1, x2, y2)
        line.setPen(pen)
        line.setZValue(z_base - 0.05)
        grp.addToGroup(line)


# Keep old names as aliases so existing call-sites in tests/RouteTool still work.
_draw_insulated = _fill_insulated  # type: ignore[assignment]
_draw_bare = _fill_bare            # type: ignore[assignment]


# ── Ratsnest ─────────────────────────────────────────────────────────────────


def render_ratsnest(
    scene: QGraphicsScene,
    schematic: Schematic,
    placements: dict[uuid.UUID, ComponentPlacement],
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    routed_nets: set[uuid.UUID] | None,
    theme: Theme = DEFAULT_THEME,
) -> list[QGraphicsLineItem]:
    """Draw crow-flies lines for unrouted pin pairs in incomplete nets.

    Returns the list of added items so the caller can track and remove them.
    """
    if routed_nets is None:
        return []

    color = QColor(theme.ratsnest_color)
    pen = QPen(color, theme.ratsnest_width)
    pen.setStyle(Qt.PenStyle.DotLine)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)

    added: list[QGraphicsLineItem] = []
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
            added.append(line)
    return added


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
