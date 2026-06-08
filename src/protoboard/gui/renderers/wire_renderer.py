# SPDX-License-Identifier: MIT
"""Renderer for wire segments and ratsnest."""

from __future__ import annotations

import uuid

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QPainterPath, QPen
from PySide6.QtWidgets import (
    QGraphicsEllipseItem, QGraphicsItem, QGraphicsItemGroup, QGraphicsLineItem,
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


# ── Wire-pin glyph ───────────────────────────────────────────────────────────


def render_wire_pins(
    scene: QGraphicsScene,
    wire_pins: "list[tuple[Point, uuid.UUID | None]]",
    net_color_map: "dict[uuid.UUID, str | None] | None" = None,
    theme: Theme = DEFAULT_THEME,
    layers: LayerState = _DEFAULT_LAYERS,
) -> "list[QGraphicsItem]":
    """Draw wire-pin glyphs on top of their pads.

    A wire-pin renders as a concentric ring in the owning wire's net
    colour with an inner mark distinguishable from a regular component
    pin (which is rendered by the board/component renderer as a solid
    dot).  Visual idiom: "this pad has a soldered wire terminal but no
    component body".

    *wire_pins* is a list of ``(position, net_id)`` pairs — caller is
    expected to walk ``layout.knots.values()`` filtering on
    :class:`WirePinKnot` and snapshotting ``knot.position`` and
    ``knot.net_id`` (the wire-pin's own net snapshot).

    Returns the list of scene items added so the caller can track and
    remove them on incremental redraw.
    """
    if not wire_pins:
        return []
    added: "list[QGraphicsItem]" = []
    outer_r = theme.pad_radius * 1.1
    inner_r = theme.pad_radius * 0.45
    # Wire-pins live on the topmost layer the board has — render with
    # the larger of the two side alphas so they remain visible from
    # either camera side.
    alpha = max(layers.alpha_for_wire("top"), layers.alpha_for_wire("bottom"))
    if alpha == 0.0:
        return []
    z_base = max(layers.z_for_wire("top"), layers.z_for_wire("bottom")) + 0.1
    for pos, net_id in wire_pins:
        color_str = theme.bare_outline_color
        if net_id is not None and net_color_map:
            override = net_color_map.get(net_id)
            if override:
                color_str = override
        net_color = QColor(color_str)
        net_color.setAlphaF(alpha)
        inner_color = QColor(theme.pad_color)
        inner_color.setAlphaF(alpha)

        # Outer ring in net colour.
        ring = QGraphicsEllipseItem(
            pos.x - outer_r, pos.y - outer_r, outer_r * 2.0, outer_r * 2.0,
        )
        ring.setBrush(QBrush(net_color))
        ring.setPen(QPen(QColor(0, 0, 0, 0), 0))
        ring.setZValue(z_base)
        scene.addItem(ring)
        added.append(ring)

        # Inner dot in pad colour to give the concentric "donut" look —
        # the wire-pin is distinct from a solid component-pin dot.
        dot = QGraphicsEllipseItem(
            pos.x - inner_r, pos.y - inner_r, inner_r * 2.0, inner_r * 2.0,
        )
        dot.setBrush(QBrush(inner_color))
        dot.setPen(QPen(QColor(0, 0, 0, 0), 0))
        dot.setZValue(z_base + 0.01)
        scene.addItem(dot)
        added.append(dot)
    return added


# ── Ratsnest ─────────────────────────────────────────────────────────────────


def render_ratsnest(
    scene: QGraphicsScene,
    schematic: Schematic,
    placements: dict[uuid.UUID, ComponentPlacement],
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    routed_nets: set[uuid.UUID] | None,
    theme: Theme = DEFAULT_THEME,
    *,
    graph: "object | None" = None,
    mode: str = "full",
    ghost: bool = False,
) -> list[QGraphicsLineItem]:
    """Render the Connections (ratsnest) overlay.

    Args:
        scene: target scene; lines are added with ``scene.addItem``.
        schematic, placements, instances, components: standard render
            inputs (pin world positions are derived from these).
        routed_nets: net ids considered "fully routed" — used only to
            gate Ghost mode (Ghost skips them, Full does not).  Pass
            None to draw nothing (caller signalled the overlay is off).
        theme: source of ``ratsnest_color`` and ``ratsnest_islands_color``.
        graph: optional ``ConnectivityGraph``; required for **island
            detection**.  When None, every unrouted net is treated as a
            single bag of pins (legacy behaviour).
        mode: ``"full"`` draws all pairs (K_n) inside each "to connect"
            point set; ``"closest"`` draws each pin to its nearest other
            pin only.  Multi-island nets use the same mode but
            inter-island, picking the nearest knot pair between each
            island pair.
        ghost: when True, only unrouted nets are drawn (current Ghost
            mode semantics).  When False (Full mode), every net is
            drawn — even fully routed ones still get island-warning
            lines if their electrical sets are split.

    Returns the list of added QGraphicsLineItems so the caller can
    remove them on the next refresh.
    """
    if routed_nets is None:
        return []

    base_color = QColor(theme.ratsnest_color)
    base_pen = QPen(base_color, theme.ratsnest_width)
    base_pen.setStyle(Qt.PenStyle.DotLine)
    base_pen.setCapStyle(Qt.PenCapStyle.RoundCap)

    islands_color = QColor(theme.ratsnest_islands_color)
    islands_pen = QPen(islands_color, theme.ratsnest_width)
    islands_pen.setStyle(Qt.PenStyle.DotLine)
    islands_pen.setCapStyle(Qt.PenCapStyle.RoundCap)

    use_closest = (mode == "closest")
    added: list[QGraphicsLineItem] = []
    for net in schematic.nets.values():
        net_routed = net.id in routed_nets
        if ghost and net_routed:
            # Ghost mode hides anything that's already a single
            # electrical set.  Multi-island nets stay visible because
            # ``net_is_complete`` already marked them as unrouted.
            continue

        # Partition this net's pin positions by electric set.  When no
        # graph is supplied the whole net is one "island" — keeps the
        # function usable from tests / save-as-svg paths.
        islands = _pin_islands_for_net(
            net.pins, placements, instances, components, graph,
        )
        # Strip empty islands (pins not yet placed) — happens whenever
        # a schematic references a component still floating in the
        # library panel.
        islands = [isl for isl in islands if isl]
        if not islands:
            continue

        if len(islands) == 1:
            # Plain "to be routed" net.  In Full mode we still want to
            # show the missing links; in Ghost mode we only got here
            # because the net is unrouted.
            pen = base_pen
            for line in _lines_within_island(islands[0], use_closest):
                a, b = line
                item = QGraphicsLineItem(a.x, a.y, b.x, b.y)
                item.setPen(pen)
                scene.addItem(item)
                added.append(item)
            continue

        # Multi-island: at least one electrical set per net pair needs
        # an inter-island connection.  Use the islands warning colour
        # regardless of mode.
        pen = islands_pen
        for line in _lines_between_islands(islands, use_closest):
            a, b = line
            item = QGraphicsLineItem(a.x, a.y, b.x, b.y)
            item.setPen(pen)
            scene.addItem(item)
            added.append(item)
    return added


def _pin_islands_for_net(
    pins: "set[PinRef]",
    placements: dict[uuid.UUID, ComponentPlacement],
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    graph: "object | None",
) -> list[list[Point]]:
    """Partition the net's placed pin positions by electric set.

    Returns a list of pin-position lists, one per electric set.  Pins
    whose component is not placed (no entry in ``placements``) are
    dropped.  Without ``graph`` the whole net is returned as a single
    list — legacy single-island behaviour.
    """
    if graph is None:
        return [_placed_pin_positions(pins, placements, instances, components)]

    node_for_pin = getattr(graph, "node_for_pin", None)
    if node_for_pin is None:
        return [_placed_pin_positions(pins, placements, instances, components)]

    by_island: dict[object, list[Point]] = {}
    unassigned: list[Point] = []
    for ref in pins:
        pl = placements.get(ref.component_id)
        inst = instances.get(ref.component_id)
        if pl is None or inst is None:
            continue
        defn = components.get(inst.definition_ref)
        if defn is None or ref.pin_number > defn.pin_count:
            continue
        pos = pin_world_position(defn.footprint, pl, ref.pin_number)
        node = node_for_pin(ref)
        if node is None:
            unassigned.append(pos)
        else:
            by_island.setdefault(node, []).append(pos)
    result: list[list[Point]] = list(by_island.values())
    if unassigned:
        # Each pad outside the connectivity graph becomes its own
        # singleton island so the inter-island pass still links it up.
        result.extend([p] for p in unassigned)
    return result


def _lines_within_island(
    points: list[Point], use_closest: bool,
) -> list[tuple[Point, Point]]:
    """Lines drawn *inside* a single island — only used when the net is
    not yet routed (1 island, missing links).

    ``use_closest`` switches between K_n graph (every pair) and the
    nearest-other-pin rule (one line per pin to its closest neighbour;
    deduped so the same pair isn't drawn twice).
    """
    n = len(points)
    if n < 2:
        return []
    if not use_closest:
        return [
            (points[i], points[j])
            for i in range(n)
            for j in range(i + 1, n)
        ]
    drawn: set[tuple[int, int]] = set()
    lines: list[tuple[Point, Point]] = []
    for i in range(n):
        best_j: int | None = None
        best_d2: float = float("inf")
        for j in range(n):
            if j == i:
                continue
            d2 = _dist_sq(points[i], points[j])
            if d2 < best_d2:
                best_d2 = d2
                best_j = j
        if best_j is None:
            continue
        pair = (min(i, best_j), max(i, best_j))
        if pair in drawn:
            continue
        drawn.add(pair)
        lines.append((points[i], points[best_j]))
    return lines


def _lines_between_islands(
    islands: list[list[Point]], use_closest: bool,
) -> list[tuple[Point, Point]]:
    """Lines connecting multiple electrical islands of the same net.

    For each island pair, find the closest cross-island knot pair and
    draw one line.  In ``"full"`` mode we connect every island pair
    (still a K_n in island-space, but with at most one line per pair,
    not one per pin pair).  In ``"closest"`` mode we connect each
    island only to its single nearest neighbour island, also collapsed
    by the pair-dedup so the result is a forest, not a multigraph.
    """
    k = len(islands)
    if k < 2:
        return []
    if not use_closest:
        lines: list[tuple[Point, Point]] = []
        for i in range(k):
            for j in range(i + 1, k):
                pair = _closest_cross_pair(islands[i], islands[j])
                if pair is not None:
                    lines.append(pair)
        return lines

    drawn: set[tuple[int, int]] = set()
    lines = []
    for i in range(k):
        best_j: int | None = None
        best_d2: float = float("inf")
        best_pair: tuple[Point, Point] | None = None
        for j in range(k):
            if j == i:
                continue
            pair = _closest_cross_pair(islands[i], islands[j])
            if pair is None:
                continue
            d2 = _dist_sq(pair[0], pair[1])
            if d2 < best_d2:
                best_d2 = d2
                best_j = j
                best_pair = pair
        if best_j is None or best_pair is None:
            continue
        key = (min(i, best_j), max(i, best_j))
        if key in drawn:
            continue
        drawn.add(key)
        lines.append(best_pair)
    return lines


def _closest_cross_pair(
    a: list[Point], b: list[Point],
) -> tuple[Point, Point] | None:
    """Return the closest (pa, pb) where pa ∈ a, pb ∈ b."""
    if not a or not b:
        return None
    best: tuple[Point, Point] | None = None
    best_d2: float = float("inf")
    for pa in a:
        for pb in b:
            d2 = _dist_sq(pa, pb)
            if d2 < best_d2:
                best_d2 = d2
                best = (pa, pb)
    return best


def _dist_sq(a: Point, b: Point) -> float:
    dx = a.x - b.x
    dy = a.y - b.y
    return dx * dx + dy * dy


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
