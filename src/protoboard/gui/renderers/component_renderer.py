# SPDX-License-Identifier: MIT
"""Renderer for placed components (rectangle + refdes + net-coloured pins)."""

from __future__ import annotations

import uuid

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPen
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsItemGroup,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsTextItem,
)

from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.group import all_component_ids
from protoboard.domain.layout.layout import Layout, ROOT_GROUP_ID
from protoboard.domain.layout.placement import ComponentPlacement, pin_world_position
from protoboard.domain.schematic.net import PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.gui.layer_state import Z_FRONT_WIRES, LayerState, Z_FRONT_COMPONENTS, Z_FRONT_LABELS
from protoboard.gui.renderers.body_shapes import body_shape_for
from protoboard.gui.theme import DEFAULT_THEME, Theme

_ALPHA_ACTIVE = 1.0      # components in the active group
_ALPHA_SUBGROUP = 0.45   # components inside a child group of the active group
_ALPHA_INACTIVE = 0.2    # components outside the active group (parent/siblings)


def _group_alpha_map(
    layout: Layout,
    active_group_id: uuid.UUID,
) -> dict[uuid.UUID, float]:
    """Return a map of comp_id → alpha multiplier based on group context."""
    groups = layout.groups
    active = groups.get(active_group_id)
    if active is None:
        return {}  # No active group — all normal alpha

    # Direct members of active group: full alpha.
    direct: frozenset[uuid.UUID] = active.component_ids

    # Members of child groups: subgroup alpha.
    subgroup: set[uuid.UUID] = set()
    for child_gid in active.child_group_ids:
        subgroup |= all_component_ids(groups, child_gid)

    # Everyone else (in parent or sibling groups): inactive alpha.
    result: dict[uuid.UUID, float] = {}
    for comp_id in layout.placements:
        if comp_id in direct:
            result[comp_id] = _ALPHA_ACTIVE
        elif comp_id in subgroup:
            result[comp_id] = _ALPHA_SUBGROUP
        else:
            result[comp_id] = _ALPHA_INACTIVE
    return result

_REFDES_FONT:        QFont | None = None
_REFDES_FONT_SIZE:   float = -1.0
_PIN_NAME_FONT:      QFont | None = None
_PIN_NAME_FONT_SIZE: float = -1.0

_DEFAULT_LAYERS = LayerState()

_IGN = QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations


def _get_refdes_font(pt: float) -> QFont:
    """Return the refdes QFont at the requested on-screen point size.

    Labels use ItemIgnoresTransformations so the font is rendered at a fixed
    screen size regardless of zoom level.  Cached; recreated when pt changes.
    """
    global _REFDES_FONT, _REFDES_FONT_SIZE
    if _REFDES_FONT is None or _REFDES_FONT_SIZE != pt:
        _REFDES_FONT = QFont("monospace")
        _REFDES_FONT.setPointSizeF(pt)
        _REFDES_FONT_SIZE = pt
    return _REFDES_FONT


def _get_pin_name_font(pt: float) -> QFont:
    global _PIN_NAME_FONT, _PIN_NAME_FONT_SIZE
    if _PIN_NAME_FONT is None or _PIN_NAME_FONT_SIZE != pt:
        _PIN_NAME_FONT = QFont("monospace")
        _PIN_NAME_FONT.setPointSizeF(pt * 0.875)  # pin names slightly smaller
        _PIN_NAME_FONT_SIZE = pt
    return _PIN_NAME_FONT


def _make_overlay_label(
    text: str,
    font: QFont,
    text_color: QColor,
    bg_color: QColor,
    scene_x: float,
    scene_y: float,
    z: float,
) -> list[QGraphicsItem]:
    """Create an overlay text item with a background rect.

    Returns [bg_rect, text_item] both with ItemIgnoresTransformations set and
    positioned at (scene_x, scene_y).  The bg_rect is sized to fit the text
    bounding box with a small padding; it is added first so it renders below.
    """
    text_item = QGraphicsTextItem(text)
    text_item.setFont(font)
    text_item.setDefaultTextColor(text_color)
    text_item.document().setDocumentMargin(0)
    text_item.setFlag(_IGN)
    text_item.setPos(scene_x, scene_y)
    text_item.setZValue(z)

    br = text_item.boundingRect()
    bg = QGraphicsRectItem(br)
    bg.setBrush(QBrush(bg_color))
    bg.setPen(QPen(Qt.GlobalColor.transparent, 0))
    bg.setFlag(_IGN)
    bg.setPos(scene_x, scene_y)
    bg.setZValue(z - 0.01)
    bg.setData(0, "label_bg")

    return [bg, text_item]


def _net_color(net_id: uuid.UUID, theme: Theme = DEFAULT_THEME) -> QColor:
    colors = theme.pin_net_colors
    return QColor(colors[abs(hash(net_id)) % len(colors)])


def comp_fingerprint(
    placement: ComponentPlacement,
    definition_ref: str,
    pin_net_for_comp: dict[int, uuid.UUID],
    routed_nets: frozenset[uuid.UUID] | None,
    layers_key: tuple[object, ...],
) -> tuple[object, ...]:
    """Stable key capturing structural/connectivity inputs for one component group.

    Highlight state (highlighted_net_id, group_alpha from active_group_id) is
    intentionally excluded — those are applied via setOpacity() on existing
    groups rather than triggering a group rebuild.
    """
    return (
        placement.position,
        placement.rotation,
        placement.side,
        getattr(placement, "vertical_flipped", False),
        tuple(sorted(getattr(placement, "pin_offsets", {}).items())),
        placement.label_offset,
        placement.locked,
        definition_ref,
        tuple(sorted(pin_net_for_comp.items())),
        routed_nets,
        layers_key,
    )


def update_component_groups(
    scene: QGraphicsScene,
    placements: dict[uuid.UUID, ComponentPlacement],
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    existing_groups: dict[uuid.UUID, QGraphicsItemGroup],
    existing_fingerprints: dict[uuid.UUID, tuple[object, ...]],
    schematic: Schematic | None,
    routed_nets: set[uuid.UUID] | None,
    theme: Theme,
    layers: LayerState,
    active_group_id: uuid.UUID | None,
    layout: Layout | None,
    highlighted_net_id: uuid.UUID | None,
) -> None:
    """Create/update/remove per-component QGraphicsItemGroups without scene clear.

    Groups whose fingerprint is unchanged are left untouched in the scene.
    """
    pin_net: dict[PinRef, uuid.UUID] = {}
    if schematic is not None:
        for net in schematic.nets.values():
            for ref in net.pins:
                pin_net[ref] = net.id

    alpha_map: dict[uuid.UUID, float] = {}
    effective_gid = active_group_id if active_group_id is not None else ROOT_GROUP_ID
    if layout is not None and effective_gid != ROOT_GROUP_ID:
        alpha_map = _group_alpha_map(layout, effective_gid)

    highlighted_comp_ids: set[uuid.UUID] = set()
    if highlighted_net_id is not None and schematic is not None:
        net_obj = schematic.nets.get(highlighted_net_id)
        if net_obj is not None:
            highlighted_comp_ids = {ref.component_id for ref in net_obj.pins}

    frozen_routed = frozenset(routed_nets) if routed_nets is not None else None
    ov = layers.overrides
    layers_key = (
        layers.camera_side,
        layers.view_transform,
        layers.ghost_components,
        ov.components_top.value,
        ov.components_bottom.value,
        ov.labels_top.value,
        ov.labels_bottom.value,
        ov.pin_names.value,
    )

    new_ids = set(placements.keys())

    # Remove groups for components that no longer exist.
    for cid in list(existing_groups):
        if cid not in new_ids:
            scene.removeItem(existing_groups.pop(cid))
            existing_fingerprints.pop(cid, None)

    for comp_id, placement in placements.items():
        inst = instances.get(comp_id)
        if inst is None:
            continue
        defn = components.get(inst.definition_ref)
        if defn is None:
            continue

        pin_net_for_comp: dict[int, uuid.UUID] = {
            ref.pin_number: nid
            for ref, nid in pin_net.items()
            if ref.component_id == comp_id
        }
        fp = comp_fingerprint(
            placement, inst.definition_ref,
            pin_net_for_comp, frozen_routed, layers_key,
        )

        if comp_id in existing_groups:
            grp = existing_groups[comp_id]
            if existing_fingerprints.get(comp_id) == fp:
                # Structure unchanged — just reset drag offset and apply opacity.
                grp.setPos(0.0, 0.0)
            else:
                # Structure changed — rebuild the group.
                scene.removeItem(existing_groups.pop(comp_id))
                existing_fingerprints.pop(comp_id, None)
                grp = _render_one(scene, placement, inst, defn, comp_id, pin_net,
                                  routed_nets, theme, layers, group_alpha=1.0,
                                  highlighted_net_id=None)
                if grp is None:
                    continue
                existing_groups[comp_id] = grp
                existing_fingerprints[comp_id] = fp
        else:
            grp = _render_one(scene, placement, inst, defn, comp_id, pin_net,
                              routed_nets, theme, layers, group_alpha=1.0,
                              highlighted_net_id=None)
            if grp is None:
                continue
            existing_groups[comp_id] = grp
            existing_fingerprints[comp_id] = fp

        # Apply opacity for group-context dimming and net highlight — O(1) per group.
        group_alpha = alpha_map.get(comp_id, 1.0)
        if highlighted_net_id is not None:
            net_dim = 1.0 if comp_id in highlighted_comp_ids else theme.highlight_dim_components
            group_alpha = max(0.04, group_alpha * net_dim)
        grp.setOpacity(group_alpha)


def render_components(
    scene: QGraphicsScene,
    placements: dict[uuid.UUID, ComponentPlacement],
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    schematic: Schematic | None = None,
    routed_nets: set[uuid.UUID] | None = None,
    theme: Theme = DEFAULT_THEME,
    layers: LayerState = _DEFAULT_LAYERS,
    active_group_id: uuid.UUID | None = None,
    layout: Layout | None = None,
    highlighted_net_id: uuid.UUID | None = None,
) -> dict[uuid.UUID, QGraphicsItemGroup]:
    """Render all placed components and return a map of comp_id → scene group.

    Each component's items are wrapped in a ``QGraphicsItemGroup`` so that
    live drag can call ``group.setPos()`` without rebuilding the scene.
    """
    pin_net: dict[PinRef, uuid.UUID] = {}
    if schematic is not None:
        for net in schematic.nets.values():
            for ref in net.pins:
                pin_net[ref] = net.id

    # Build group alpha map when root-level group context is given.
    alpha_map: dict[uuid.UUID, float] = {}
    effective_gid = active_group_id if active_group_id is not None else ROOT_GROUP_ID
    if layout is not None and effective_gid != ROOT_GROUP_ID:
        alpha_map = _group_alpha_map(layout, effective_gid)

    # Build set of component IDs that are part of the highlighted net.
    highlighted_comp_ids: set[uuid.UUID] = set()
    if highlighted_net_id is not None and schematic is not None:
        net = schematic.nets.get(highlighted_net_id)
        if net is not None:
            highlighted_comp_ids = {ref.component_id for ref in net.pins}

    comp_groups: dict[uuid.UUID, QGraphicsItemGroup] = {}
    for comp_id, placement in placements.items():
        inst = instances.get(comp_id)
        if inst is None:
            continue
        defn = components.get(inst.definition_ref)
        if defn is None:
            continue
        group_alpha = alpha_map.get(comp_id, 1.0)
        if highlighted_net_id is not None:
            net_dim = 1.0 if comp_id in highlighted_comp_ids else theme.highlight_dim_components
            group_alpha = max(0.04, group_alpha * net_dim)
        grp = _render_one(scene, placement, inst, defn, comp_id, pin_net,
                          routed_nets, theme, layers, group_alpha=group_alpha,
                          highlighted_net_id=highlighted_net_id)
        if grp is not None:
            comp_groups[comp_id] = grp
    return comp_groups


def _render_one(
    scene: QGraphicsScene,
    placement: ComponentPlacement,
    inst: ComponentInstance,
    defn: ComponentDefinition,
    comp_id: uuid.UUID,
    pin_net: dict[PinRef, uuid.UUID],
    routed_nets: set[uuid.UUID] | None,
    theme: Theme,
    layers: LayerState,
    group_alpha: float = 1.0,
    highlighted_net_id: uuid.UUID | None = None,
) -> QGraphicsItemGroup | None:
    """Render one component; return a scene group (for live drag) or None."""
    side = placement.side  # type: ignore[assignment]
    alpha = layers.alpha_for_component(side) * group_alpha
    if alpha == 0.0:
        return None
    alpha_label = layers.alpha_for_label(side) * group_alpha
    z_comp = layers.z_for_component(side)
    z_label = layers.z_for_label(side)

    fp = defn.footprint
    positions = [
        pin_world_position(fp, placement, n + 1) for n in range(fp.pin_count())
    ]
    if not positions:
        return None

    # Wrap all items in a group so live drag can move them with setPos().
    grp = QGraphicsItemGroup()
    grp.setZValue(z_comp)

    # Bbox used for label positioning.
    margin = 0.35
    xs = [p.x for p in positions]
    ys = [p.y for p in positions]
    x0, y0 = min(xs) - margin, min(ys) - margin
    x1, y1 = max(xs) + margin, max(ys) + margin

    def _a(base_alpha: int) -> int:
        return max(0, min(255, int(base_alpha * alpha)))

    _base_fill = QColor(theme.comp_fill_color)
    comp_fill = QColor(_base_fill.red(), _base_fill.green(), _base_fill.blue(),
                       _a(_base_fill.alpha()))
    comp_border = QColor(theme.comp_border)
    comp_border.setAlphaF(alpha)

    shape = body_shape_for(defn, placement, inst, theme.body_types)
    for item in shape.draw(positions, comp_fill, comp_border, alpha, z_comp, theme):
        grp.addToGroup(item)
    skip_pins = shape.drawn_pin_numbers(positions, theme)

    pin_color_fallback = QColor(theme.pin_color_fallback)
    pin_color_fallback.setAlphaF(alpha)
    for pin_idx, pos in enumerate(positions):
        pin_number = pin_idx + 1
        if pin_number in skip_pins:
            continue
        ref = PinRef(component_id=comp_id, pin_number=pin_number)
        net_id = pin_net.get(ref)

        if net_id is not None:
            color = _net_color(net_id, theme)
            color.setAlphaF(color.alphaF() * alpha)
            complete = routed_nets is None or net_id in routed_nets
            if complete:
                fill_color = color
                pen = QPen(color, theme.pin_width_complete)
                pen.setStyle(Qt.PenStyle.SolidLine)
            else:
                fill_color = QColor(color)
                fill_color.setAlpha(_a(130))
                pen = QPen(color, theme.pin_width_incomplete)
                pen.setStyle(Qt.PenStyle.DashLine)
        else:
            fill_color = pin_color_fallback
            fallback_pen_color = QColor(theme.pin_label_fallback_color)
            fallback_pen_color.setAlphaF(fill_color.alphaF())
            pen = QPen(fallback_pen_color, theme.pin_width_incomplete)

        # Glow ring for highlighted net pins.
        if highlighted_net_id is not None and net_id == highlighted_net_id:
            gr = theme.route_glow_pin_radius
            glow_color = _net_color(net_id, theme)
            glow_color.setAlphaF(theme.route_glow_pin_alpha * alpha)
            glow_pen = QPen(glow_color, gr * 0.55)
            glow_pen.setStyle(Qt.PenStyle.SolidLine)
            halo = QGraphicsEllipseItem(QRectF(pos.x - gr, pos.y - gr, 2 * gr, 2 * gr))
            halo.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            halo.setPen(glow_pen)
            halo.setZValue(z_comp - 0.08)
            grp.addToGroup(halo)

        if pin_number == 1:
            _add_pin1_marker(grp, pos.x, pos.y, fill_color, theme, z=z_comp - 0.05)
        else:
            r = theme.pin_radius
            dot = QGraphicsEllipseItem(QRectF(pos.x - r, pos.y - r, 2 * r, 2 * r))
            dot.setBrush(QBrush(fill_color))
            dot.setPen(pen)
            dot.setZValue(z_comp - 0.05)
            grp.addToGroup(dot)

    fpt = theme.overlay_font_size
    bg_color = QColor(theme.label_bg_color)
    if alpha_label > 0.0:
        label_color = QColor(theme.refdes_color)
        label_color.setAlphaF(alpha_label)
        bg = QColor(bg_color)
        bg.setAlphaF(bg.alphaF() * alpha_label)

        # Single-column footprint (SIL, TO-92): move refdes left of bbox to avoid
        # overlap with pin-name labels which anchor at the pin position.
        single_col = len({round(p.x, 6) for p in positions}) == 1

        _default_offset = Point(0.05, 0.05)
        if placement.label_offset != _default_offset:
            lx, ly = x0 + placement.label_offset.x, y0 + placement.label_offset.y
        elif single_col:
            lx, ly = x0 - 0.05, (y0 + y1) / 2.0
        else:
            lx, ly = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        for item in _make_overlay_label(
            inst.refdes, _get_refdes_font(fpt), label_color, bg, lx, ly, z_label
        ):
            grp.addToGroup(item)

        # Pin-name labels (controlled by the pin_names layer).
        alpha_pin_names = layers.alpha_for_pin_names(side) * group_alpha
        if alpha_pin_names > 0.0 and defn.pin_names:
            pin_name_color = QColor(theme.pin_label_fallback_color)
            pin_name_color.setAlphaF(alpha_pin_names)
            pin_bg = QColor(bg_color)
            pin_bg.setAlphaF(pin_bg.alphaF() * alpha_pin_names)
            for pin_idx, pos in enumerate(positions):
                pin_number = pin_idx + 1
                name = defn.pin_names.get(pin_number)
                if not name:
                    continue
                for item in _make_overlay_label(
                    name, _get_pin_name_font(fpt), pin_name_color, pin_bg,
                    pos.x, pos.y, z_label + 0.1,
                ):
                    grp.addToGroup(item)

    if placement.locked and alpha > 0.05:
        lock_col = QColor("#e07000")
        lock_col.setAlphaF(min(alpha, 1.0))
        lock_bg = QColor(bg_color)
        lock_bg.setAlphaF(lock_bg.alphaF() * min(alpha, 1.0))
        for item in _make_overlay_label(
            "\U0001f512", _get_refdes_font(fpt), lock_col, lock_bg,
            x1, y0, z_label + 0.5,
        ):
            grp.addToGroup(item)

    scene.addItem(grp)
    return grp


def _add_pin1_marker(
    grp: QGraphicsItemGroup, cx: float, cy: float, fill_color: QColor,
    theme: Theme, z: float = Z_FRONT_WIRES,
) -> None:
    r = theme.pin_radius * 1.1
    square = QGraphicsRectItem(QRectF(cx - r, cy - r, 2 * r, 2 * r))
    square.setBrush(QBrush(fill_color))
    marker_col = QColor(theme.pin1_marker_color)
    marker_col.setAlphaF(fill_color.alphaF())
    square.setPen(QPen(marker_col, theme.pin_width_incomplete))
    square.setZValue(z)
    grp.addToGroup(square)


def make_ghost_item(
    defn: ComponentDefinition,
    rotation: int = 0,
    theme: Theme = DEFAULT_THEME,
    pin_offsets: dict[int, Point] | None = None,
) -> QGraphicsItemGroup:
    """Return a semi-transparent QGraphicsItemGroup previewing *defn* at origin."""
    placement = ComponentPlacement(
        component_id=uuid.UUID(int=0),
        position=Point(0.0, 0.0),
        rotation=rotation,
        side="top",
        pin_offsets=pin_offsets or {},
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

    # Use the same body shape as the real component, semi-transparent.
    ghost_alpha = ghost_fill.alphaF()
    shape = body_shape_for(defn)
    for item in shape.draw(positions, ghost_fill, ghost_border, ghost_alpha, 0.0, theme):
        # Ghost body: dashed border
        if hasattr(item, 'pen'):
            pen = item.pen()
            pen.setStyle(Qt.PenStyle.DashLine)
            item.setPen(pen)
        group.addToGroup(item)

    r = theme.pin_radius
    for pos in positions:
        dot = QGraphicsEllipseItem(QRectF(pos.x - r, pos.y - r, 2 * r, 2 * r))
        dot.setBrush(QBrush(ghost_pin))
        dot.setPen(QPen(Qt.GlobalColor.transparent, 0))
        group.addToGroup(dot)

    # Label positioned near first pin or bbox top-left
    xs = [p.x for p in positions]
    ys = [p.y for p in positions]
    ghost_bg = QColor(theme.label_bg_color)
    ghost_bg.setAlphaF(ghost_bg.alphaF() * 0.5)
    for item in _make_overlay_label(
        defn.name, _get_refdes_font(theme.overlay_font_size),
        ghost_label, ghost_bg, min(xs) + 0.05, min(ys) + 0.05, 1.0,
    ):
        group.addToGroup(item)
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
    wires: list[object] | None = None,
    theme: Theme = DEFAULT_THEME,
    active_group_id: uuid.UUID | None = None,
) -> list[QGraphicsItem]:
    """Render a dotted outline around each group's bounding box.

    Only groups that are direct children of *active_group_id* (or top-level
    non-root groups when active == root) are rendered at full opacity.
    The root group itself is never rendered.

    Returns the list of added items so the caller can track and remove them.
    """
    added: list[QGraphicsItem] = []
    t = theme
    effective_active = active_group_id if active_group_id is not None else ROOT_GROUP_ID
    active_grp = layout.groups.get(effective_active)
    # IDs of child groups visible at the current level.
    visible_child_ids: frozenset[uuid.UUID] = (
        active_grp.child_group_ids if active_grp is not None else frozenset()
    )

    _pen_col = QColor(t.group_pen_color)
    group_pen_full = QPen(_pen_col, t.group_pen_width)
    group_pen_full.setStyle(Qt.PenStyle.DotLine)
    group_fill_full = QColor(t.group_fill_color)
    group_name_color = QColor(t.group_name_color)

    for group in layout.groups.values():
        if group.id == ROOT_GROUP_ID:
            continue
        is_active = group.id == effective_active

        comp_ids = frozenset(all_component_ids(layout.groups, group.id))
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
        # Extend bbox to include all points of internal wires.
        if wires:
            from protoboard.domain.commands.group_cmds import (
                _find_internal_wires,
                _pin_positions_for,
            )
            from protoboard.domain.layout.wire import WireSegment
            typed_wires = [w for w in wires if isinstance(w, WireSegment)]
            all_ids = frozenset(placements.keys())
            group_pins = _pin_positions_for(comp_ids, placements, instances, components)  # type: ignore[arg-type]
            ext_pins = _pin_positions_for(all_ids - comp_ids, placements, instances, components)  # type: ignore[arg-type]
            internal_ids = _find_internal_wires(typed_wires, group_pins, ext_pins)
            for w in typed_wires:
                if w.id in internal_ids:
                    positions.extend(w.path)
        xs = [p.x for p in positions]
        ys = [p.y for p in positions]
        m = t.group_margin
        x0 = min(xs) - m
        y0 = min(ys) - m
        w = max(xs) - min(xs) + 2 * m
        h = max(ys) - min(ys) + 2 * m

        # Choose style: active group = solid; child = normal; others = dimmed.
        if is_active:
            pen = QPen(QColor(t.group_active_color), t.group_pen_width)
            pen.setStyle(Qt.PenStyle.SolidLine)
            fill = QColor(t.group_active_fill_color)
            name_col = QColor(t.group_active_color)
        elif group.id in visible_child_ids:
            pen = group_pen_full
            fill = group_fill_full
            name_col = group_name_color
        else:
            dim_pen_color = QColor(t.group_pen_color)
            dim_pen_color.setAlpha(t.group_dim_pen_alpha)
            pen = QPen(dim_pen_color, t.group_pen_width)
            pen.setStyle(Qt.PenStyle.DotLine)
            fill = QColor(t.group_fill_color)
            fill.setAlpha(t.group_dim_fill_alpha)
            name_col = QColor(t.group_name_color)
            name_col.setAlpha(t.group_dim_name_alpha)

        rect = QGraphicsRectItem(QRectF(x0, y0, w, h))
        rect.setPen(pen)
        rect.setBrush(QBrush(fill))
        # Tag the rect so SelectTool can identify it as a group outline.
        rect.setData(0, str(group.id))
        rect.setData(1, "group_outline")
        rect.setZValue(1.0)
        scene.addItem(rect)
        added.append(rect)
        if group.name:
            grp_bg = QColor(theme.label_bg_color)
            for item in _make_overlay_label(
                group.name, _get_refdes_font(theme.overlay_font_size),
                name_col, grp_bg, x0 + 0.1, y0 + 0.05, 1.1,
            ):
                scene.addItem(item)
                added.append(item)

        # Render exported pins as small green diamonds (visible from parent).
        if group.id in visible_child_ids and group.exported_pins:
            from PySide6.QtGui import QPainterPath
            from PySide6.QtWidgets import QGraphicsPathItem
            ep_fill = QColor(t.exported_pin_color)
            ep_pen = QPen(QColor(t.exported_pin_pen_color), t.exported_pin_pen_width)
            r = 0.18
            for ep in group.exported_pins:
                pl = placements.get(ep.component_id)
                inst = instances.get(ep.component_id)
                if pl is None or inst is None:
                    continue
                defn = components.get(inst.definition_ref)
                if defn is None:
                    continue
                wp = pin_world_position(defn.footprint, pl, ep.pin_number)
                diamond = QPainterPath()
                diamond.moveTo(wp.x, wp.y - r)
                diamond.lineTo(wp.x + r, wp.y)
                diamond.lineTo(wp.x, wp.y + r)
                diamond.lineTo(wp.x - r, wp.y)
                diamond.closeSubpath()
                item = QGraphicsPathItem(diamond)
                item.setBrush(QBrush(ep_fill))
                item.setPen(ep_pen)
                item.setZValue(12.0)
                scene.addItem(item)
                added.append(item)

    return added
