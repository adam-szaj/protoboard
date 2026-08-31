# SPDX-License-Identifier: MIT
"""Renderer for wire segments and ratsnest."""

from __future__ import annotations

import uuid

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainterPath, QPen
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsItemGroup,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsScene,
)

from protoboard.controller.view_options.theme import DEFAULT_THEME, Theme
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package import (
    package_pins_for_meta_pin,
    resolved_footprint,
)
from protoboard.domain.layout.placement import (
    ComponentPlacement,
    PlacementMap,
    pin_world_position,
)
from protoboard.domain.layout.wire import Wire
from protoboard.domain.schematic.net import PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.gui.layer_state import Z_BACK_WIRES, Z_FRONT_WIRES, LayerState

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


# ---------------------------------------------------------------------------
# v2 — Wire / Fragment-aware rendering
# ---------------------------------------------------------------------------


class WireSceneGroup(QGraphicsItemGroup):
    """One wire's scene presence: this (front) group + a top-level back sibling.

    Children of a QGraphicsItemGroup can never interleave with items outside
    the group, so per-fragment zValues inside one group cannot put a back-side
    fragment under the board while front fragments stay above it.  Back-side
    fragments therefore live in ``self.back`` — a SIBLING top-level group at
    the back-wires Z.  The group-level operations consumers apply per wire
    (live-drag ``setPos``, highlight ``setOpacity``) fan out to both, so the
    pair behaves as one item everywhere else (``isinstance`` checks against
    ``QGraphicsItemGroup`` still hold).

    Use ``add_to_scene`` / ``remove_from_scene`` so the pair's scene
    membership stays in lockstep.
    """

    def __init__(self) -> None:
        super().__init__()
        self.back = QGraphicsItemGroup()

    def setOpacity(self, opacity: float) -> None:
        super().setOpacity(opacity)
        self.back.setOpacity(opacity)

    def setPos(self, *args: object) -> None:  # (QPointF) or (x, y)
        super().setPos(*args)  # type: ignore[call-overload]
        self.back.setPos(*args)  # type: ignore[call-overload]

    def sceneBoundingRect(self) -> QRectF:
        """The WIRE's scene footprint — union of both groups.

        A wire entirely on the far side has an empty front group; returning
        only the front rect would make the footprint appear to move when the
        camera flips (fragments swap groups on rebuild).
        """
        if not self.childItems():
            return self.back.sceneBoundingRect()
        if not self.back.childItems():
            return super().sceneBoundingRect()
        return super().sceneBoundingRect().united(self.back.sceneBoundingRect())

    def add_to_scene(self, scene: QGraphicsScene) -> None:
        scene.addItem(self)
        scene.addItem(self.back)

    def remove_from_scene(self, scene: QGraphicsScene) -> None:
        scene.removeItem(self)
        scene.removeItem(self.back)


def _remove_wire_group(scene: QGraphicsScene, grp: QGraphicsItemGroup) -> None:
    if isinstance(grp, WireSceneGroup):
        grp.remove_from_scene(scene)
    else:
        scene.removeItem(grp)


def wire_fingerprint_v2(
    wire: Wire,
    net_id: uuid.UUID | None,
    color_override: str | None,
    z_base: float,
    base_alpha: float = 1.0,
    is_bold: bool = False,
    side_alphas: tuple[float, float] | None = None,
) -> tuple[object, ...]:
    """Fingerprint for a v2 Wire — accounts for every fragment's side+path.

    ``side_alphas`` is the (top, bottom) wire-alpha pair — per-fragment
    colour depends on both sides' alphas, so a layer-visibility change on
    EITHER side must rebuild a mixed wire even when the first fragment's
    side (and thus ``base_alpha``/``z_base``) is unaffected.

    The whole wire is one render group, so a single rebuild trigger
    captures all fragments at once.  ``z_base``/``base_alpha`` come from
    the *first* fragment's side; per-fragment Z is applied during build
    (so a top fragment in a mostly-bottom wire still renders above the
    board layer).
    """
    return (
        tuple((f.side, tuple((p.x, p.y) for p in f.path)) for f in wire.fragments),
        wire.insulated,
        net_id,
        color_override,
        round(z_base, 4),
        round(base_alpha, 4),
        is_bold,
        side_alphas,
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
    routing_target_net_id: uuid.UUID | None = None,
) -> None:
    """Create/update/remove per-wire groups without a full scene clear.

    Fragment-aware: each fragment paints on its own side (alpha + Z) with a
    via marker at every side transition.  Groups whose fingerprint is
    unchanged are left untouched; removed wires lose their group; new or
    changed wires get a freshly-built one.
    """
    new_ids = {w.id for w in wires}
    for wid in list(existing_groups):
        if wid not in new_ids:
            _remove_wire_group(scene, existing_groups.pop(wid))
            existing_fingerprints.pop(wid, None)

    for wire in wires:
        net_id = wire_net_map.get(wire.id)
        color_override: str | None = (
            net_color_map.get(net_id) if net_id is not None and net_color_map else None
        )
        # Routing target neon — overrides whatever colour we'd otherwise
        # pick.  Applies to every wire belonging to the active routing
        # net, regardless of palette / manual net.color.  The override is
        # part of the fingerprint, so entering/leaving routing rebuilds.
        if (
            routing_target_net_id is not None
            and net_id is not None
            and net_id == routing_target_net_id
        ):
            color_override = theme.routing_target_color
        # z_base anchors the fingerprint / via-marker Z on the FIRST
        # fragment's side; each fragment's own alpha and Z are applied
        # during build.
        side = wire.fragments[0].side
        base_alpha = layers.alpha_for_wire(side)
        z_base = layers.z_for_wire(side)
        side_alphas = (
            round(layers.alpha_for_wire("top"), 4),
            round(layers.alpha_for_wire("bottom"), 4),
        )
        # A wire disappears only when EVERY side it touches is hidden —
        # hiding the first fragment's side must not erase the rest.
        visible = any(layers.alpha_for_wire(f.side) > 0.0 for f in wire.fragments)
        is_bold = (
            highlighted_net_id is not None
            and net_id is not None
            and net_id == highlighted_net_id
        )
        fp = wire_fingerprint_v2(
            wire,
            net_id,
            color_override,
            z_base,
            base_alpha,
            is_bold,
            side_alphas,
        )

        if wire.id in existing_groups:
            if existing_fingerprints.get(wire.id) != fp:
                _remove_wire_group(scene, existing_groups.pop(wire.id))
                existing_fingerprints.pop(wire.id, None)
                if not visible:
                    continue
                grp = _build_wire_group_v2(
                    wire,
                    net_id,
                    color_override,
                    theme,
                    layers,
                    glow=False,
                    bold=is_bold,
                )
                grp.add_to_scene(scene)
                existing_groups[wire.id] = grp
                existing_fingerprints[wire.id] = fp
        else:
            if not visible:
                continue
            grp = _build_wire_group_v2(
                wire,
                net_id,
                color_override,
                theme,
                layers,
                glow=False,
                bold=is_bold,
            )
            grp.add_to_scene(scene)
            existing_groups[wire.id] = grp
            existing_fingerprints[wire.id] = fp

        net_alpha = (
            1.0 if highlighted_net_id is None or is_bold else theme.highlight_dim_wires
        )
        existing_groups[wire.id].setOpacity(net_alpha)


def _build_wire_group_v2(
    wire: Wire,
    net_id: uuid.UUID | None,
    color_override: str | None,
    theme: Theme,
    layers: LayerState,
    glow: bool,
    bold: bool = False,
) -> WireSceneGroup:
    """Build one wire's scene groups — front-side fragments in the returned
    group, back-side fragments in its top-level ``back`` sibling, so a back
    fragment genuinely stacks under the board layer (Z), not merely dimmed.
    """
    grp = WireSceneGroup()
    front_side = layers.front_side()
    color = _net_color(net_id, color_override, theme)

    for frag in wire.fragments:
        # Each fragment's alpha comes from ITS OWN side, applied to the
        # undimmed base colour — never derived from fragments[0]'s alpha,
        # which would dim a camera-side fragment of a wire that starts on
        # the far side.
        frag_alpha = layers.alpha_for_wire(frag.side)
        if frag_alpha == 0.0:
            continue  # this side is hidden; other fragments still render
        pts = list(frag.path)
        # The fragment's own side decides which top-level group (and thus
        # which scene Z layer) its items land in.
        target = grp if frag.side == front_side else grp.back
        frag_z = layers.z_for_wire(frag.side)
        frag_color = QColor(color)
        if frag_alpha < 1.0:
            frag_color = _dim(frag_color, frag_alpha)

        if glow:
            glow_color = _net_color(net_id, color_override, theme)
            glow_color.setAlphaF(theme.route_glow_wire_alpha * frag_alpha)
            glow_pen = QPen(glow_color, theme.route_glow_wire_width)
            glow_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            glow_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            for i in range(len(pts) - 1):
                x1, y1 = pts[i].x, pts[i].y
                x2, y2 = pts[i + 1].x, pts[i + 1].y
                g = QGraphicsLineItem(x1, y1, x2, y2)
                g.setPen(glow_pen)
                g.setZValue(frag_z - 0.1)
                target.addToGroup(g)

        if wire.insulated:
            _fill_insulated(target, pts, frag_color, frag_z, theme, bold=bold)
        else:
            _fill_bare(target, pts, frag_color, frag_z, theme, bold=bold)

    # Via markers at fragment boundaries (side transitions) — always on the
    # front group (a plated-through via is visible from the camera side),
    # always UNDER the wire strokes (glow −0.1, strokes −0.05): the wire
    # crosses over its pad, on both view sides alike.
    via_color = QColor(theme.via_marker_color)
    via_ring = QColor(theme.via_marker_ring_color)
    via_radius = theme.via_marker_radius
    for i in range(len(wire.fragments) - 1):
        if wire.fragments[i].side == wire.fragments[i + 1].side:
            continue
        boundary = wire.fragments[i].path[-1]
        marker = _build_via_marker(
            boundary,
            via_radius,
            via_color,
            via_ring,
            Z_FRONT_WIRES - 0.2,
        )
        grp.addToGroup(marker)

    grp.setZValue(Z_FRONT_WIRES)
    grp.back.setZValue(Z_BACK_WIRES)
    return grp


def _build_via_marker(
    pos: Point,
    radius: float,
    color: QColor,
    ring_color: QColor,
    z: float,
) -> QGraphicsPathItem:
    """Filled circle marking a plated-through via point.

    Light disc + dark ring — a shape/contrast cue that reads on any net
    colour and does not rely on colour perception alone.
    """
    path = QPainterPath()
    path.addEllipse(pos.x - radius, pos.y - radius, 2 * radius, 2 * radius)
    item = QGraphicsPathItem(path)
    item.setPen(QPen(ring_color, 0.08))
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
        net_alpha = (
            1.0
            if highlighted_net_id is None or is_highlighted
            else theme.highlight_dim_wires
        )
        _draw_wire(
            scene,
            wire,
            net_id,
            color_override,
            theme,
            layers,
            net_alpha=net_alpha,
            glow=is_highlighted,
        )


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
    # Same conventions as update_wire_groups_v2: z anchors on the first
    # fragment's side; each fragment's own alpha/Z are applied during build,
    # and the wire disappears only when every side it touches is hidden.
    visible = any(layers.alpha_for_wire(f.side) > 0.0 for f in wire.fragments)
    if not visible or net_alpha == 0.0:
        return
    grp = _build_wire_group_v2(
        wire,
        net_id,
        color_override,
        theme,
        layers,
        glow=glow,
    )
    grp.setOpacity(net_alpha)
    grp.add_to_scene(scene)


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
_draw_insulated = _fill_insulated
_draw_bare = _fill_bare


# ── Wire-pin glyph ───────────────────────────────────────────────────────────


def render_wire_pins(
    scene: QGraphicsScene,
    wire_pins: list[tuple[Point, uuid.UUID | None]],
    net_color_map: dict[uuid.UUID, str | None] | None = None,
    theme: Theme = DEFAULT_THEME,
    layers: LayerState = _DEFAULT_LAYERS,
    routing_target_net_id: uuid.UUID | None = None,
) -> list[QGraphicsItem]:
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
    added: list[QGraphicsItem] = []
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
        # Routing-target neon overrides the resolved net colour.
        if (
            routing_target_net_id is not None
            and net_id is not None
            and net_id == routing_target_net_id
        ):
            color_str = theme.routing_target_color
        net_color = QColor(color_str)
        net_color.setAlphaF(alpha)
        inner_color = QColor(theme.pad_color)
        inner_color.setAlphaF(alpha)

        # Outer ring in net colour.
        ring = QGraphicsEllipseItem(
            pos.x - outer_r,
            pos.y - outer_r,
            outer_r * 2.0,
            outer_r * 2.0,
        )
        ring.setBrush(QBrush(net_color))
        ring.setPen(QPen(QColor(0, 0, 0, 0), 0))
        ring.setZValue(z_base)
        scene.addItem(ring)
        added.append(ring)

        # Inner dot in pad colour to give the concentric "donut" look —
        # the wire-pin is distinct from a solid component-pin dot.
        dot = QGraphicsEllipseItem(
            pos.x - inner_r,
            pos.y - inner_r,
            inner_r * 2.0,
            inner_r * 2.0,
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
    placements: PlacementMap,
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    routed_nets: set[uuid.UUID] | None,
    theme: Theme = DEFAULT_THEME,
    *,
    graph: object | None = None,
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

    # Ghost mode always renders the minimum (closest-pair) skeleton —
    # K_n clutter is unreadable when overlaid on dimmed components,
    # and applies to both yellow (within-island missing links) and
    # purple (between-island warning) lines.  Outside ghost we honour
    # the user-configured ratsnest mode.
    use_closest = ghost or (mode == "closest")
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
            net.pins,
            placements,
            instances,
            components,
            graph,
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
    pins: set[PinRef],
    placements: PlacementMap,
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    graph: object | None,
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
        if defn is None or ref.pin_number > len(defn.meta_pins):
            continue
        positions = _physical_positions_for_ref(ref, pl, inst, defn)
        node = node_for_pin(ref)
        if node is None:
            unassigned.extend(positions)
        else:
            by_island.setdefault(node, []).extend(positions)
    result: list[list[Point]] = list(by_island.values())
    if unassigned:
        # Each pad outside the connectivity graph becomes its own
        # singleton island so the inter-island pass still links it up.
        result.extend([p] for p in unassigned)
    return result


def _lines_within_island(
    points: list[Point],
    use_closest: bool,
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
        return [(points[i], points[j]) for i in range(n) for j in range(i + 1, n)]
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
    islands: list[list[Point]],
    use_closest: bool,
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
    a: list[Point],
    b: list[Point],
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
    placements: PlacementMap,
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
        if defn is None or ref.pin_number > len(defn.meta_pins):
            continue
        result.extend(_physical_positions_for_ref(ref, pl, inst, defn))
    return result


def _physical_positions_for_ref(
    ref: PinRef,
    placement: ComponentPlacement,
    instance: ComponentInstance,
    definition: ComponentDefinition,
) -> list[Point]:
    """Expand one logical PinRef to positions of its mapped package pins."""
    footprint = resolved_footprint(definition, instance, instance.families)
    return [
        pin_world_position(footprint, placement, package_pin_number)
        for package_pin_number in package_pins_for_meta_pin(
            definition, instance, ref.meta_pin_index
        )
    ]
