# SPDX-License-Identifier: MIT
"""Renderer for placed components (rectangle + refdes + net-coloured pins)."""

from __future__ import annotations

import uuid

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPen
from PySide6.QtWidgets import (
    QAbstractGraphicsShapeItem,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsItemGroup,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsTextItem,
)

from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.group_tree import GroupTree
from protoboard.domain.layout.layout import Layout, ROOT_GROUP_ID
from protoboard.domain.layout.placement import (
    ComponentPlacement,
    PlacementMap,
    pin_world_position,
)
from protoboard.domain.schematic.net import PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point, Rect, Vector
from protoboard.gui.qt_geometry import to_qrectf
from protoboard.gui.layer_state import Z_FRONT_WIRES, LayerState, Z_FRONT_COMPONENTS, Z_FRONT_LABELS, Z_LABELS_TOP
from protoboard.gui.renderers.body_shapes import body_shape_for
from protoboard.controller.theme import DEFAULT_THEME, Theme

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
    _tree = GroupTree(groups)
    for child_gid in active.child_group_ids:
        subgroup |= _tree.all_component_ids(child_gid)

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


def _compose_overlay_label(refdes: str, value: str) -> str:
    """Build the text rendered above a component's pin 1.

    Convention: ``refdes`` alone when ``value`` is empty, otherwise
    ``"refdes value"``.  For concrete parts (BC547, 1N4148, NE555 …)
    ``value`` carries the part-number by default; for generic templates
    (R, C, L, F …) it carries the user-supplied value (e.g. "4.7k") —
    or ``"?"`` when the user hasn't filled it in yet.
    """
    return f"{refdes} {value}" if value else refdes


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
    anchor: str = "top_left",
) -> list[QGraphicsItem]:
    """Create an overlay text item with a background rect.

    Layer alpha (e.g. back-side dimming, ghost) should be applied by the
    caller to ``bg_color`` only — the text stays at its full theme alpha
    so dimmed labels remain readable rather than fading into invisibility.

    Returns [bg_rect, text_item] both with ItemIgnoresTransformations set.
    anchor='top_left'    — (scene_x, scene_y) is the top-left corner (default).
    anchor='bottom_right' — (scene_x, scene_y) is the bottom-right corner;
                            the label is shifted so its bottom-right lands there.

    NOTE: a pixel-perfect "center" anchor is intentionally not provided here.
    ``QGraphicsTextItem`` with ``ItemIgnoresTransformations`` reports
    ``boundingRect()`` in *device pixels* but ``setPos()`` takes *scene*
    coordinates — mixing them via ``scene_x - br.width()/2`` produces an
    offset roughly 60× too large at the default zoom.  Callers that want
    centring should compute the offset in scene units themselves (e.g.
    using the current view scale) and pass ``anchor='top_left'``.
    """
    text_item = QGraphicsTextItem(text)
    text_item.setFont(font)
    text_item.setDefaultTextColor(text_color)
    text_item.document().setDocumentMargin(0)
    text_item.setFlag(_IGN)
    text_item.setZValue(z)

    br = text_item.boundingRect()
    if anchor == "bottom_right":
        ox = scene_x - br.width()
        oy = scene_y - br.height()
    else:
        ox, oy = scene_x, scene_y

    text_item.setPos(ox, oy)

    bg = QGraphicsRectItem(br)
    bg.setBrush(QBrush(bg_color))
    bg.setPen(QPen(Qt.GlobalColor.transparent, 0))
    bg.setFlag(_IGN)
    bg.setPos(ox, oy)
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
    bold_pin_nets: frozenset[uuid.UUID] = frozenset(),
    refdes: str = "",
    value: str = "",
) -> tuple[object, ...]:
    """Stable key capturing structural/connectivity inputs for one component group.

    *bold_pin_nets* is the set of net ids whose pins on this component
    should be drawn with a thicker bold-pen.  It enters the fingerprint
    because the bold state is encoded in scene items (pen widths) — a
    rebuild is the only way to apply or remove it.  Group-level alpha
    (``alpha_map``/``highlight_dim``) still bypasses the cache and is
    applied via setOpacity at the end of update_component_groups.

    *refdes* and *value* enter the fingerprint because the overlay
    label composes ``"refdes value"``; editing either in the Properties
    panel must trigger a group rebuild so the new label paints.
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
        bold_pin_nets,
        refdes,
        value,
    )


def update_component_groups(
    scene: QGraphicsScene,
    placements: PlacementMap,
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
    existing_label_groups: dict[uuid.UUID, QGraphicsItemGroup] | None = None,
) -> None:
    """Create/update/remove per-component QGraphicsItemGroups without scene clear.

    *existing_label_groups* (when provided) is the parallel map of the
    separate label groups (refdes / pin names / lock) rendered at
    ``Z_LABELS_TOP``; it is created/updated/removed in lock-step with the body
    groups so labels follow drags and live above the pin-modifier glyphs.

    Groups whose fingerprint is unchanged are left untouched in the scene.

    Net-highlight opacity (dim non-highlighted components, boost
    highlighted ones) is **not** applied here — the canonical place is
    ``BoardView._apply_highlight_opacity`` which reads the active
    HighlightProfile (``highlight_alpha`` for highlighted comps,
    ``dim_components`` for the rest) and runs after every refresh.
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
        ov.labels.value,
        ov.pin_names.value,
    )

    label_groups: dict[uuid.UUID, QGraphicsItemGroup] = (
        existing_label_groups if existing_label_groups is not None else {}
    )

    def _drop_label_group(cid: uuid.UUID) -> None:
        old = label_groups.pop(cid, None)
        if old is not None:
            scene.removeItem(old)

    new_ids = set(placements.keys())

    # Remove groups for components that no longer exist.
    for cid in list(existing_groups):
        if cid not in new_ids:
            scene.removeItem(existing_groups.pop(cid))
            existing_fingerprints.pop(cid, None)
            _drop_label_group(cid)

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
        # If the highlighted net touches this component, that net id enters
        # the fingerprint so a change in highlight rebuilds the group with
        # bold pen widths on the affected pins.
        bold_pin_nets: frozenset[uuid.UUID] = (
            frozenset({highlighted_net_id})
            if highlighted_net_id is not None and comp_id in highlighted_comp_ids
            else frozenset()
        )
        fp = comp_fingerprint(
            placement, inst.definition_ref,
            pin_net_for_comp, frozen_routed, layers_key,
            bold_pin_nets=bold_pin_nets,
            refdes=inst.refdes,
            value=inst.value,
        )

        if comp_id in existing_groups:
            grp = existing_groups[comp_id]
            if existing_fingerprints.get(comp_id) == fp:
                # Structure unchanged — just reset drag offset and apply opacity.
                grp.setPos(0.0, 0.0)
                lbl = label_groups.get(comp_id)
                if lbl is not None:
                    lbl.setPos(0.0, 0.0)
            else:
                # Structure changed — rebuild the group (and its labels).
                scene.removeItem(existing_groups.pop(comp_id))
                existing_fingerprints.pop(comp_id, None)
                _drop_label_group(comp_id)
                result = _render_one(scene, placement, inst, defn, comp_id, pin_net,
                                     routed_nets, theme, layers, group_alpha=1.0,
                                     highlighted_net_id=highlighted_net_id,
                                     is_highlighted_comp=comp_id in highlighted_comp_ids)
                if result is None:
                    continue
                grp, label_grp = result
                existing_groups[comp_id] = grp
                existing_fingerprints[comp_id] = fp
                if label_grp is not None:
                    label_groups[comp_id] = label_grp
        else:
            result = _render_one(scene, placement, inst, defn, comp_id, pin_net,
                                 routed_nets, theme, layers, group_alpha=1.0,
                                 highlighted_net_id=highlighted_net_id,
                                 is_highlighted_comp=comp_id in highlighted_comp_ids)
            if result is None:
                continue
            grp, label_grp = result
            existing_groups[comp_id] = grp
            existing_fingerprints[comp_id] = fp
            if label_grp is not None:
                label_groups[comp_id] = label_grp

        # Apply group-context dimming — O(1) per group.  Net-highlight
        # opacity (highlighted vs dimmed comps) is layered on top by
        # ``BoardView._apply_highlight_opacity`` after every refresh, so
        # we deliberately don't touch it here.  Doing so previously
        # clobbered the route-active dim profile every time a refresh
        # fired during a wire drag.
        _op = alpha_map.get(comp_id, 1.0)
        grp.setOpacity(_op)
        lbl = label_groups.get(comp_id)
        if lbl is not None:
            lbl.setOpacity(_op)


def render_components(
    scene: QGraphicsScene,
    placements: PlacementMap,
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    schematic: Schematic | None = None,
    routed_nets: set[uuid.UUID] | None = None,
    theme: Theme = DEFAULT_THEME,
    layers: LayerState = _DEFAULT_LAYERS,
    active_group_id: uuid.UUID | None = None,
    layout: Layout | None = None,
    highlighted_net_id: uuid.UUID | None = None,
) -> tuple[
    dict[uuid.UUID, QGraphicsItemGroup], dict[uuid.UUID, QGraphicsItemGroup]
]:
    """Render all placed components.

    Returns ``(comp_groups, label_groups)`` — two maps keyed by comp_id.  The
    body/pins are in *comp_groups* at the component Z; the text labels are in
    *label_groups* at ``Z_LABELS_TOP`` (above pin modifiers).  Both groups are
    wrapped so live drag can call ``group.setPos()`` without rebuilding the
    scene; BoardView translates both.  A component with no labels has no entry
    in *label_groups*.
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
    label_groups: dict[uuid.UUID, QGraphicsItemGroup] = {}
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
        result = _render_one(scene, placement, inst, defn, comp_id, pin_net,
                             routed_nets, theme, layers, group_alpha=group_alpha,
                             highlighted_net_id=highlighted_net_id)
        if result is not None:
            grp, label_grp = result
            comp_groups[comp_id] = grp
            if label_grp is not None:
                label_groups[comp_id] = label_grp
    return comp_groups, label_groups


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
    is_highlighted_comp: bool = False,
) -> tuple[QGraphicsItemGroup, QGraphicsItemGroup | None] | None:
    """Render one component; return ``(body_group, label_group)`` or None.

    The component body, pins and bold-net dots go into *body_group* at the
    component Z.  The text labels (refdes / value, pin names, lock) go into a
    separate *label_group* at ``Z_LABELS_TOP`` so they paint above the
    pin-modifier glyphs (a child item's Z is relative to its parent group, so
    labels nested in the body group could never rise above a standalone
    modifier item — hence the split).  *label_group* is None when no labels
    are drawn.  Both groups share the live-drag translation in BoardView.

    When *is_highlighted_comp* is True, the per-layer ghost factor
    (alpha_for_component) is bypassed so a component belonging to the
    routed/highlighted net stays fully opaque even while a routing or
    placement tool dims the rest of the components layer.  The setOpacity
    pass in update_component_groups still applies on top, so users keep
    control via group-level overrides.
    """
    side = placement.side  # type: ignore[assignment]
    if is_highlighted_comp:
        alpha = group_alpha  # skip ghost-component dimming for highlighted nets
    else:
        alpha = layers.alpha_for_component(side) * group_alpha
    if alpha == 0.0:
        return None
    alpha_label = layers.alpha_for_label(side) * group_alpha
    z_comp = layers.z_for_component(side)

    # The placed part's pins follow its CHOSEN variant, not the definition
    # default — an R placed as SMD-0805 has a 1-hole pitch, not the default
    # axial 3.2-hole pitch. Resolve the variant footprint (falls back to
    # the definition's default package when none resolves).
    from protoboard.domain.components.package import resolved_footprint
    fp = resolved_footprint(defn, inst, theme.package_families)
    positions = [
        pin_world_position(fp, placement, n + 1) for n in range(fp.pin_count())
    ]
    if not positions:
        return None

    # Wrap all items in a group so live drag can move them with setPos().
    grp = QGraphicsItemGroup()
    grp.setZValue(z_comp)
    # Separate group for text labels — lifted to Z_LABELS_TOP so they paint
    # above the pin-modifier glyphs.  Items are added lazily; the group is
    # only returned (and added to the scene) if it ends up non-empty.
    label_grp = QGraphicsItemGroup()
    label_grp.setZValue(layers.z_for_label(side))
    _label_count = 0

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
    comp_border.setAlphaF(comp_border.alphaF() * alpha)

    shape = body_shape_for(
        defn, placement, inst,
        package_families=theme.package_families,
    )
    body_items, label_items = shape.draw(
        positions, comp_fill, comp_border, alpha, z_comp, theme
    )
    for item in body_items:
        grp.addToGroup(item)
    # Value-label 50% copy rides the label group (Z_LABELS_TOP), above the
    # pin-modifier glyphs, so the text stays legible where a modifier covers it.
    for item in label_items:
        label_grp.addToGroup(item)
        _label_count += 1
    skip_pins = shape.drawn_pin_numbers(positions, theme)

    # Per-pin colouring: the board layer renders each pad in its net
    # colour and the pin-pads / pin-modifiers layers carry the pin1 /
    # exported / star markers, so the regular per-pin dot drawn here is
    # redundant.  We only keep the bold-highlight case: pin belongs to
    # the highlighted (Net Inspector / routing target) net → large
    # opaque dot on top so the net stays prominent regardless of how
    # the pad underneath is painted.
    pin_color_fallback = QColor(theme.pin_color_fallback)
    pin_color_fallback.setAlphaF(pin_color_fallback.alphaF() * alpha)
    for pin_idx, pos in enumerate(positions):
        pin_number = pin_idx + 1
        if pin_number in skip_pins:
            continue
        ref = PinRef(component_id=comp_id, pin_number=pin_number)
        net_id = pin_net.get(ref)

        is_bold = (
            highlighted_net_id is not None
            and net_id is not None
            and net_id == highlighted_net_id
        )

        # Compute the fill colour the pin1 marker / bold dot should use.
        # Mirrors the previous palette choice so the pin1 square keeps its
        # accent colour matching the net it belongs to.
        if net_id is not None:
            color = _net_color(net_id, theme)
            color.setAlphaF(color.alphaF() * alpha)
            complete = routed_nets is None or net_id in routed_nets
            if complete:
                fill_color = color
            else:
                fill_color = QColor(color)
                fill_color.setAlpha(_a(130))
        else:
            fill_color = pin_color_fallback

        if is_bold:
            dot_fill = QColor(fill_color)
            dot_fill.setAlphaF(alpha)  # opaque modulo layer-ghost
            dot_radius = theme.pad_radius * theme.bold_pin_dot_radius_factor
            # Above body overlays (DIP/quad-DIP draw their own pin circles
            # at z_comp); 0.5 margin keeps the bold dot decisively on top.
            dot_z = z_comp + 0.5
        else:
            dot_fill = fill_color
            dot_radius = theme.pin_radius
            dot_z = z_comp - 0.05

        if is_bold:
            dot = QGraphicsEllipseItem(
                to_qrectf(Rect.of(
                    pos.x - dot_radius, pos.y - dot_radius,
                    2 * dot_radius, 2 * dot_radius,
                ))
            )
            dot.setBrush(QBrush(dot_fill))
            dot.setPen(QPen(Qt.PenStyle.NoPen))
            dot.setZValue(dot_z)
            grp.addToGroup(dot)
        # else: no dot — pin1 marker comes from the pin-modifiers layer,
        # net colour from the pin-pads layer.

    fpt = theme.overlay_font_size
    bg_color = QColor(theme.label_bg_color)
    if alpha_label > 0.0:
        # Text colour kept at full theme alpha; only the background pill
        # fades with layer alpha so dimmed labels remain readable.
        label_color = QColor(theme.refdes_color)
        bg = QColor(bg_color)
        bg.setAlphaF(bg.alphaF() * alpha_label)

        _default_offset = Vector(0.05, 0.05)
        if placement.label_offset != _default_offset:
            lx = x0 + placement.label_offset.dx
            ly = y0 + placement.label_offset.dy
        else:
            # Place refdes 0.5 raster units above and to the left of pin 1.
            pin1 = positions[0]
            lx = pin1.x - 0.5
            ly = pin1.y - 0.5
        label_text = _compose_overlay_label(inst.refdes, inst.value)
        # Child Z is relative to label_grp (whose own Z is z_label); use small
        # offsets only to order labels among themselves.
        for item in _make_overlay_label(
            label_text, _get_refdes_font(fpt), label_color, bg, lx, ly, 0.0,
        ):
            label_grp.addToGroup(item)
            _label_count += 1

    # Pin-name labels (controlled by the pin_names layer) — independent of the
    # labels layer, so they render even when the refdes/value labels are hidden.
    alpha_pin_names = layers.alpha_for_pin_names(side, cid=comp_id) * group_alpha
    if alpha_pin_names > 0.0 and defn.pin_names:
        # Text colour stays at full theme alpha; only the background pill
        # fades.  Keeps pin names readable even on ghosted/back layers.
        pin_name_color = QColor(theme.pin_label_fallback_color)
        pin_bg = QColor(bg_color)
        pin_bg.setAlphaF(pin_bg.alphaF() * alpha_pin_names)
        for pin_idx, pos in enumerate(positions):
            pin_number = pin_idx + 1
            name = defn.pin_names.get(pin_number)
            if not name:
                continue
            for item in _make_overlay_label(
                name, _get_pin_name_font(fpt), pin_name_color, pin_bg,
                pos.x, pos.y, 0.1,
            ):
                label_grp.addToGroup(item)
                _label_count += 1

    if placement.locked and alpha > 0.05:
        # Lock emoji stays at full alpha; only the bg pill fades with layer.
        lock_col = QColor("#e07000")
        lock_bg = QColor(bg_color)
        lock_bg.setAlphaF(lock_bg.alphaF() * min(alpha, 1.0))
        for item in _make_overlay_label(
            "\U0001f512", _get_refdes_font(fpt), lock_col, lock_bg,
            x1, y0, 0.5,
        ):
            label_grp.addToGroup(item)
            _label_count += 1

    scene.addItem(grp)
    if _label_count > 0:
        scene.addItem(label_grp)
        return grp, label_grp
    return grp, None


def make_ghost_item(
    defn: ComponentDefinition,
    rotation: int = 0,
    theme: Theme = DEFAULT_THEME,
    pin_offsets: dict[int, Vector] | None = None,
    package_variant: str | None = None,
) -> QGraphicsItemGroup:
    """Return a semi-transparent QGraphicsItemGroup previewing *defn* at origin.

    *package_variant* is the picker's chosen body address (``"family/variant"``);
    when set, the preview draws that body and its pin spacing, not the
    definition's default footprint. (Without it the default is used — a part with
    a single body, or a netlist-imported instance, is unaffected.)
    """
    placement = ComponentPlacement(
        component_id=uuid.UUID(int=0),
        position=Point(0.0, 0.0),
        rotation=rotation,
        side="top",
        pin_offsets=pin_offsets or {},
    )
    # The chosen variant drives both the body shape and the pin spacing: a
    # ghost instance carries the address, resolved against the theme catalogue.
    ghost_inst: ComponentInstance | None = None
    fp = defn.default_footprint(theme.package_families or {})
    if package_variant is not None:
        ghost_inst = ComponentInstance.new(
            definition_ref=defn.name, refdes="?", value="",
            package_variant=package_variant,
        )
        from protoboard.domain.components.package import (
            resolve_component_package,
        )
        pkg = resolve_component_package(
            defn, ghost_inst, theme.package_families or {})
        if pkg is not None:
            fp = pkg
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
    # Pass the placement so the body knows the rotation / vertical-flip the pin
    # positions already carry — otherwise the silhouette and polarity markers
    # draw in an unrotated frame while the pins are rotated, and the mismatch
    # grows with the rotation angle.
    shape = body_shape_for(
        defn, placement, ghost_inst,
        package_families=theme.package_families,
    )
    # Ghost preview shows only the body silhouette — the value label(s) are
    # dropped (the label channel is irrelevant for a drag ghost).
    body_items, _labels = shape.draw(
        positions, ghost_fill, ghost_border, ghost_alpha, 0.0, theme
    )
    for item in body_items:
        # Ghost body: dashed border
        if isinstance(item, QAbstractGraphicsShapeItem):
            pen = item.pen()
            pen.setStyle(Qt.PenStyle.DashLine)
            item.setPen(pen)
        group.addToGroup(item)

    r = theme.pin_radius
    for pos in positions:
        dot = QGraphicsEllipseItem(to_qrectf(Rect.of(
            pos.x - r, pos.y - r, 2 * r, 2 * r,
        )))
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
    placements: PlacementMap,
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    wires: list[object] | None = None,
    theme: Theme = DEFAULT_THEME,
    active_group_id: uuid.UUID | None = None,
    view_scale: float = 1.0,
    wire_net_map: dict[uuid.UUID, uuid.UUID] | None = None,
    schematic: object | None = None,
    label_z: float = Z_LABELS_TOP,
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

    _tree = GroupTree(layout.groups)
    for group in layout.groups.values():
        if group.id == ROOT_GROUP_ID:
            continue
        is_active = group.id == effective_active

        comp_ids = frozenset(_tree.all_component_ids(group.id))
        positions: list[Point] = []
        for cid in comp_ids:
            pl = placements.get(cid)
            inst = instances.get(cid)
            if pl is None or inst is None:
                continue
            defn = components.get(inst.definition_ref)
            if defn is None:
                continue
            from protoboard.domain.components.package import resolved_footprint
            fp = resolved_footprint(defn, inst, theme.package_families)
            for n in range(fp.pin_count()):
                positions.append(pin_world_position(fp, pl, n + 1))
        if not positions:
            continue
        # Extend bbox to include all points of owned wires (net-aware
        # classification — half-orphan tails of *internal* nets count
        # as owned, tails of *exported* nets do not).
        if wires:
            from protoboard.domain.layout.group_ownership import OwnershipResolver
            from protoboard.domain.layout.wire import Wire
            from protoboard.domain.schematic.schematic import Schematic as _Schematic
            typed_wires = [w for w in wires if isinstance(w, Wire)]
            sch_obj = schematic if isinstance(schematic, _Schematic) else None
            # Bind the LOCAL render context directly (this is a renderer
            # given explicit dicts, not session state).
            owned_ids = OwnershipResolver(
                knots=layout.knots,  # type: ignore[arg-type]
                wire_net_map=wire_net_map or {},
                schematic=sch_obj,
                placements=placements,
                instances=instances,
                definitions=components,
            ).owned_wire_ids(typed_wires, group, comp_ids)
            for w in typed_wires:
                if w.id in owned_ids:
                    positions.extend(w.path)
        xs = [p.x for p in positions]
        ys = [p.y for p in positions]
        m = t.group_margin
        x0 = min(xs) - m
        y0 = min(ys) - m
        bw = max(xs) - min(xs) + 2 * m
        bh = max(ys) - min(ys) + 2 * m

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

        rect = QGraphicsRectItem(to_qrectf(Rect.of(x0, y0, bw, bh)))
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
            label_font = _get_refdes_font(theme.overlay_font_size)
            # Place the label so its bottom edge sits flush with the top
            # edge of the outline (y0).  ItemIgnoresTransformations keeps
            # font-pixel size constant, so the label's scene-space height
            # is metric_height / view_scale.
            from PySide6.QtGui import QFontMetricsF
            label_h_scene = QFontMetricsF(label_font).height() / max(view_scale, 1e-6)
            for item in _make_overlay_label(
                group.name, label_font,
                name_col, grp_bg, x0, y0 - label_h_scene, label_z,
            ):
                scene.addItem(item)
                added.append(item)

        # Exported-pin markers used to be drawn here as small green
        # diamonds; that responsibility moved to the pin-modifiers
        # rendering layer (see ``gui/renderers/pin_pad_renderer.py``),
        # which uses the net colour and reserves DIAMOND for exported
        # pins / STAR for pin1+exported.  Group-level exported-pin
        # geometry is no longer painted here.

    return added
