# SPDX-License-Identifier: MIT
"""HighlightRenderer — the *mechanism* half of the highlight pipeline.

:class:`~protoboard.controller.highlight_controller.HighlightController` (policy)
decides WHAT to highlight and publishes it to the ``HighlightStack``; this
renderer decides HOW that looks on the scene: opacity dimming, the cyan
selection outline, neon overlays on selected wires, bright net-pad discs,
the clicked-free-pad ring and the dashed selection bbox.

Extracted from ``BoardView`` (phase 1 of the BoardView decomposition).  The
renderer owns all highlight state and talks to the canvas only through
:class:`~protoboard.gui.board_view.BoardView` — everything it
needs from the view is part of that sanctioned surface.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import TYPE_CHECKING

import structlog
from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QPen
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsItemGroup,
    QGraphicsRectItem,
)

from protoboard.geometry import Insets
from protoboard.gui.qt_geometry import to_qrectf
from protoboard.instrumentation.debug_trace import traced

if TYPE_CHECKING:
    from protoboard.controller.theme import Theme
    from protoboard.geometry import Point
    from protoboard.gui.board_view import BoardView

_click_log = structlog.get_logger("protoboard.gui.highlight.click")
_CLICK_PROFILE = os.environ.get("PROTOBOARD_CLICK_PROFILE") not in (None, "", "0")


class HighlightRenderer:
    """Scene-side highlight mechanism, driven by BoardView and the stack."""

    def __init__(self, view: BoardView) -> None:
        self._view = view
        # Net highlight: legacy fields kept for set_highlighted_net_silent().
        self._highlighted_net_id: uuid.UUID | None = None
        self._highlight_profile: object | None = None
        # Snapshot of (comp_ids, wire_ids, profile_id) baked into the view's
        # current back-cache pixmap.  ``apply_impl`` compares this to the new
        # highlight set; on a mismatch it invalidates the back cache so the
        # next paint rasterises the bottom-side wires with the fresh
        # bright/dim state.  Without this, opacity changes on items already
        # hidden + cached produced a stale pixmap — wires across the camera
        # kept their old highlight until the user pressed Esc (see memory
        # ``back-cache-highlight-stale-2026-06-08``).
        self._last_highlight_signature: int = 0
        # Cyan selection outline: comp_ids whose body outline we recoloured
        # to the select colour (so we can restore them when they leave the
        # selection).
        self._cyan_outlined: set[uuid.UUID] = set()
        # Thin dashed bounding box around each selected component (separate
        # from the bright body outline above).  Toggled by AppSettings.
        # show_selection_bbox via set_show_selection_bbox().
        self._selection_bbox_overlays: dict[uuid.UUID, QGraphicsRectItem] = {}
        self._show_selection_bbox: bool = True
        # Selection-outline colour state.  Normally the theme select colour
        # (cyan); during a drag with a live conflict the SelectTool flips it
        # to amber (SOFT) / red (HARD) so the *selection highlight itself*
        # signals the conflict — one recolour pass, no competing overlay.
        # The applied RGB is tracked so the restore step matches whatever was
        # last painted even as the colour changes mid-drag.
        self._select_outline_override: QColor | None = None
        self._applied_outline_rgb: int | None = None
        # Group outline rects (tagged "group_outline") recoloured to the
        # select colour for the currently-selected groups — restored when
        # deselected.
        self._cyan_group_outlined: set[uuid.UUID] = set()
        # Bright-yellow overlay items drawn on top of *selected wires*, keyed
        # by wire id.  The dim-others pipeline can't tell the user *which*
        # wire was picked (everything else just fades); these neon overlays
        # do — the same role the cyan outline plays for components.  Rebuilt
        # by _redraw_selected_wire_overlays whenever the selection or scene
        # changes.
        self._wire_select_overlays: dict[uuid.UUID, QGraphicsItemGroup] = {}
        # Bright-yellow discs drawn on the pads of the *highlighted net* so
        # the user can see which pads the selection touches (the pin-pad
        # layer alone never reacts to selection).  Standalone scene items,
        # rebuilt by _redraw_net_pad_highlights.  The records they read are
        # cached from the last refresh's classify_pin_pads (avoids a
        # recompute per highlight).
        self._net_pad_highlight_items: list[object] = []
        self._pin_pad_records: list[object] = []
        # World position of the single *clicked free pin-pad* (no net), drawn
        # as a light ring — click feedback that the pad is active (and its
        # context menu offers "assign to net").  None = no free pad clicked.
        # A free pad has no net context, so it never gets the neon net disc;
        # this ring is the only highlight it can carry, and only while it is
        # the click target.
        self._free_pad_ring_pos: Point | None = None

    # -- state fed by the view ------------------------------------------------

    @traced()
    def set_pin_pad_records(self, records: list[object]) -> None:
        """Cache the refresh's ``classify_pin_pads`` records for disc drawing."""
        self._pin_pad_records = records

    @traced()
    def on_scene_cleared(self) -> None:
        """Drop refs to overlay items the scene clear just destroyed.

        Do NOT removeItem — the C++ objects are already gone.  Without this,
        the next recolour pass touches dead wrappers (libshiboken "already
        deleted").
        """
        self._selection_bbox_overlays.clear()
        self._cyan_outlined.clear()
        self._cyan_group_outlined.clear()
        self._wire_select_overlays.clear()
        self._net_pad_highlight_items.clear()

    # -- net-highlight API ------------------------------------------------------

    @traced()
    def effective_highlighted_net(self) -> uuid.UUID | None:
        """Pick the net id that should drive glow-ring rendering.

        Priority: the actively-drawn routing net (``session.routing_net_id``)
        wins over the legacy ``_highlighted_net_id`` set by Net Inspector.
        Routing is a stronger UI signal — when the user is mid-route, the
        pads they're targeting must be unambiguously called out.
        """
        rn = self._view.session.routing_net_id
        if rn is not None:
            return rn
        return self._highlighted_net_id

    @traced()
    def set_highlighted_net(self, net_id: uuid.UUID | None) -> None:
        """Legacy: update local net_id and apply opacity (SelectTool internals)."""
        if net_id == self._highlighted_net_id and self._highlight_profile is None:
            return
        self._highlighted_net_id = net_id
        self._highlight_profile = None
        self.apply()

    @traced()
    def set_highlighted_net_with_profile(
        self, net_id: uuid.UUID | None, profile: object
    ) -> None:
        """Legacy: update local net_id+profile and apply opacity."""
        self._highlighted_net_id = net_id
        self._highlight_profile = profile
        self.apply()

    @traced()
    def set_highlighted_net_silent(self, net_id: uuid.UUID | None) -> None:
        """Set the highlighted net without triggering a refresh.

        Use when the caller will redraw overlays itself (e.g. RouteTool) and
        wants the dim to take effect on the *next* natural refresh only.
        """
        self._highlighted_net_id = net_id

    @traced()
    def set_clicked_free_pad(self, pos: Point | None) -> None:
        """Set (or clear) the clicked free-pin pad whose ring is drawn.

        Called by SelectTool when a press lands on a netless pin-pad (``pos``)
        or on anything else (``None``).  Re-runs the highlight pass so the ring
        appears / disappears immediately.
        """
        same = (pos is None and self._free_pad_ring_pos is None) or (
            pos is not None
            and self._free_pad_ring_pos is not None
            and abs(pos.x - self._free_pad_ring_pos.x) < 1e-6
            and abs(pos.y - self._free_pad_ring_pos.y) < 1e-6
        )
        if same:
            return
        self._free_pad_ring_pos = pos
        self.apply()

    @traced()
    def set_selection_outline_override(self, color: str | None) -> None:
        """Override the selection-outline colour (None → back to theme cyan).

        *color* is hex ``#AARRGGBB`` — the seam's colour format (qt-ports
        §2.7); the QColor conversion happens here, Qt-side.  Used by
        SelectTool during a drag: amber for a SOFT (removable) conflict,
        red for a HARD (blocked) one, so the selection highlight itself shows
        the drop's consequence — instead of a separate overlay.  Re-runs the
        highlight pass so the change paints immediately.
        """
        qcolor = QColor(color) if color is not None else None
        new_rgb = qcolor.rgb() & 0x00FFFFFF if qcolor is not None else None
        cur_rgb = (
            self._select_outline_override.rgb() & 0x00FFFFFF
            if self._select_outline_override is not None
            else None
        )
        if new_rgb == cur_rgb:
            return  # no change → skip the repaint
        self._select_outline_override = qcolor
        self.apply()

    @traced()
    def set_show_selection_bbox(self, enabled: bool) -> None:
        """Toggle the thin dashed bounding box drawn around selections.

        Off → existing bbox overlays are dropped; on → they reappear on the next
        selection recolour.  The bright body outline is unaffected either way.
        """
        enabled = bool(enabled)
        if enabled == self._show_selection_bbox:
            return
        self._show_selection_bbox = enabled
        if not enabled:
            for cid in list(self._selection_bbox_overlays):
                self._remove_selection_bbox_overlay(cid)
        self._recolour_selected_outlines()
        self._view.viewport().update()

    @traced()
    def set_wire_drag_state(
        self,
        states: dict[uuid.UUID, str],
    ) -> None:
        """Apply per-wire drag ghosting overlay (2026-06-02 unified drag).

        states maps ``wire.id → "drop" | "disconnect" | "normal"`` where:

        * ``"drop"`` — opacity 0.3 (wire will be removed on commit)
        * ``"disconnect"`` — opacity 0.5 (cross-group; user decides drop/reroute)
        * ``"normal"`` — opacity 1.0 (cleared / no drag state)

        Called from ``SelectTool._update_drag_overlay`` whenever the
        debounced classifier produces a new decision.  Wires not in
        the map are left untouched — callers can use ``"normal"``
        explicitly to clear a previously-applied state.
        """
        wire_groups = self._view.wire_groups()
        for wire_id, state in states.items():
            grp = wire_groups.get(wire_id)
            if not isinstance(grp, QGraphicsItemGroup):
                continue
            if state == "drop":
                grp.setOpacity(0.3)
            elif state == "disconnect":
                grp.setOpacity(0.5)
            else:
                grp.setOpacity(1.0)

    # -- the dimming pass -------------------------------------------------------

    @traced()
    def apply(self) -> None:
        """Run the highlight pass (with optional click-profiling)."""
        _t0 = time.monotonic() if _CLICK_PROFILE else 0.0
        try:
            self.apply_impl()
        finally:
            if _CLICK_PROFILE:
                _click_log.info(
                    "click_profile.apply_highlight_opacity",
                    ms=round((time.monotonic() - _t0) * 1000, 1),
                    n_comp_groups=len(self._view.component_groups()),
                    n_wire_groups=len(self._view.wire_groups()),
                )

    @traced()
    def apply_impl(self) -> None:
        """Apply dimming to existing comp/wire groups without a full refresh.

        Three sources may contribute (unioned, no overrides):
          1. ``HighlightStack.active()`` — top-of-tier entry (persistent /
             transient / auto, in that precedence order).
          2. ``session.routing_net_id`` — RouteTool's actively-drawn net.
          3. Legacy ``_highlighted_net_id`` / ``_highlight_profile`` — fallback
             when neither stack nor routing net is set.

        Comps/wires belonging to *any* contributing set are shown at full
        opacity; everything else is dimmed by the active profile.
        """
        from protoboard.controller.interaction_mode import HighlightProfile
        from protoboard.gui.highlight_state import PROFILE_DIMMED_1, HighlightEntry

        session = self._view.session
        _base = self._view.theme

        stack_entry: HighlightEntry | None = session.highlight_stack.active()
        routing_net_id: uuid.UUID | None = session.routing_net_id

        # Aggregate highlighted ids across all sources.
        highlighted_comp_ids: set[uuid.UUID] = set()
        highlighted_wire_ids: set[uuid.UUID] = set()
        net_ids: set[uuid.UUID] = set()
        profile_obj: object | None = None
        any_source = False

        if stack_entry is not None:
            any_source = True
            highlighted_comp_ids |= set(stack_entry.comp_ids)
            highlighted_wire_ids |= set(stack_entry.wire_ids)
            if stack_entry.net_id is not None:
                net_ids.add(stack_entry.net_id)
            profile_obj = stack_entry.profile

        if routing_net_id is not None:
            any_source = True
            net_ids.add(routing_net_id)
            if profile_obj is None:
                profile_obj = PROFILE_DIMMED_1

        if not any_source:
            # Legacy fallback for callers still using set_highlighted_net*.
            if self._highlighted_net_id is not None:
                any_source = True
                net_ids.add(self._highlighted_net_id)
                profile_obj = self._highlight_profile

        if not any_source:
            self._clear_highlight_dimming()
            return

        # Determine dim values from whichever profile we ended up with.
        if isinstance(profile_obj, HighlightProfile):
            dim_c = profile_obj.dim_components
            dim_w = profile_obj.dim_wires
            hi_alpha = profile_obj.highlight_alpha
        else:
            dim_c = _base.highlight_dim_components
            dim_w = _base.highlight_dim_wires
            hi_alpha = 1.0

        # Expand each net into its member component ids.
        for nid in net_ids:
            net_obj = session.schematic.nets.get(nid)
            if net_obj is not None:
                highlighted_comp_ids |= {ref.component_id for ref in net_obj.pins}

        # wire_id → net_id map, so we know which wires belong to any active net.
        wire_net_map = session.cached_wire_net_map()

        label_groups = self._view.component_label_groups()
        for comp_id, grp in self._view.component_groups().items():
            if isinstance(grp, QGraphicsItemGroup):
                _alpha = (
                    hi_alpha if comp_id in highlighted_comp_ids else max(0.04, dim_c)
                )
                grp.setOpacity(_alpha)
                # Mirror the opacity onto the component's label group so
                # labels dim / boost together with their component.
                lbl = label_groups.get(comp_id)
                if isinstance(lbl, QGraphicsItemGroup):
                    lbl.setOpacity(_alpha)

        for wire_id, grp in self._view.wire_groups().items():
            if isinstance(grp, QGraphicsItemGroup):
                if wire_id in highlighted_wire_ids:
                    grp.setOpacity(1.0)
                elif wire_net_map.get(wire_id) in net_ids:
                    grp.setOpacity(1.0)
                else:
                    grp.setOpacity(dim_w)

        # Recolour the body outline of *selected* components to the select
        # colour (cyan).  Distinct from the net-dim above: dimming says "this
        # net is in context", the cyan outline says "this is what you picked".
        selected_comp_ids = self._recolour_selected_outlines()

        # Bright neon discs on the pads of every net the *selection* actually
        # touches.  Driving this off the single published ``net_id`` was wrong:
        # a multi-net component publishes only one arbitrary net (frozenset
        # iteration order), so the same pad lit regardless of what was selected.
        # Derive the net set from the tool's real selection (before the net
        # expansion above inflates highlighted_comp_ids) so a selected component
        # lights exactly the nets it sits on.
        self._redraw_net_pad_highlights(self._selection_disc_net_ids(net_ids))

        # Invalidate the back-cache pixmap when the *set* of highlighted
        # items changes — opacity-only shifts (same set, different alpha
        # from a profile tweak) do not require a rebuild because the
        # cached pixmap is rasterised with whatever opacity the items
        # had at render time and a setOpacity on a hidden item is
        # cheap.  The signature here is sensitive only to membership
        # changes plus the active profile id (different profile = items
        # rendered at a different intrinsic alpha during build).  The
        # selected set joins it so a cyan-outline change rebuilds the cache.
        new_signature = hash(
            (
                frozenset(highlighted_comp_ids),
                frozenset(highlighted_wire_ids),
                frozenset(net_ids),
                frozenset(selected_comp_ids),
                id(profile_obj),
            )
        )
        if new_signature != self._last_highlight_signature:
            self._last_highlight_signature = new_signature
            self._view.invalidate_back_cache()
            # Re-trigger a paint so the back cache gets rebuilt now
            # rather than on the next mouse move; otherwise the user
            # sees the stale pixmap until they wiggle the cursor.
            self._view.viewport().update()

    @traced()
    def _clear_highlight_dimming(self) -> None:
        """Restore every comp/wire/label group to full opacity (no highlight).

        The no-source branch of :meth:`apply_impl`, pulled out so that method
        reads as "aggregate sources → if none, clear → else dim".  Keeps the
        cyan selection outline in sync (a component can be selected with no
        net context) and invalidates the back cache when we just left a
        non-empty highlight state — otherwise the cached pixmap keeps stale
        dimmed back-side items (the "third Esc looks like a no-op" bug).
        """
        for groups in (
            self._view.component_groups(),
            self._view.component_label_groups(),
            self._view.wire_groups(),
        ):
            for grp in groups.values():
                if isinstance(grp, QGraphicsItemGroup):
                    grp.setOpacity(1.0)
        # Selected-but-no-net components keep their cyan outline.
        self._recolour_selected_outlines()
        # No net in context → no net-pad discs.
        self._redraw_net_pad_highlights(set())
        cleared_signature = hash(
            (
                frozenset(),
                frozenset(),
                frozenset(),
                id(None),
            )
        )
        if cleared_signature != self._last_highlight_signature:
            self._last_highlight_signature = cleared_signature
            self._view.invalidate_back_cache()
            self._view.viewport().update()

    # -- selection outline (cyan body outline + group outline) -----------------

    @traced()
    def _recolour_selected_outlines(self) -> set[uuid.UUID]:
        """Paint the selected components' body outline in the select colour.

        Reads the active tool's ``_selected_ids`` (the SelectTool compat view)
        and, for each selected component, recolours its body-outline pen from
        the neutral ``comp_border`` to the theme ``select_color`` (cyan).  The
        fill and every other item (leads, pins, labels) are left untouched.

        Native body shapes (PackageBody / GenericBody / TO92 / TO220) carry the
        outline as a real ``QGraphicsItem`` pen, so we recolour it in place.
        SVG-backed bodies (DIP / SIL / quad-DIP) bake the stroke into a
        rasterised ``QGraphicsSvgItem`` that cannot be recoloured — for those we
        draw a cyan bounding-box rectangle overlay instead.

        Idempotent: components that left the selection are restored to
        ``comp_border`` and their SVG overlay removed.  Returns the selected
        component-id set so the caller can fold it into the back-cache
        signature.
        """
        theme = self._view.theme
        tool = self._view.active_tool
        selected: set[uuid.UUID] = set(getattr(tool, "_selected_ids", set()))

        comp_groups = self._view.component_groups()
        border_rgb = QColor(theme.comp_border).rgb() & 0x00FFFFFF
        # Cyan normally; amber/red while a drag conflict is live (the override).
        select_rgb = (
            self._select_outline_override
            if self._select_outline_override is not None
            else QColor(theme.select_color)
        )
        select_rgb = QColor(select_rgb)
        # Restore matches whatever colour was last painted (may differ from the
        # new target when the override just changed cyan→amber→red mid-drag).
        prev_rgb = (
            self._applied_outline_rgb
            if self._applied_outline_rgb is not None
            else QColor(theme.select_color).rgb() & 0x00FFFFFF
        )

        # 1. Restore components that are no longer selected.
        for cid in self._cyan_outlined - selected:
            grp = comp_groups.get(cid)
            if isinstance(grp, QGraphicsItemGroup):
                self._set_outline_colour(
                    grp,
                    select_rgb,
                    QColor(theme.comp_border),
                    match_rgb=prev_rgb,
                )
        self._cyan_outlined &= selected

        # 2. Apply the select colour to the selected components.  Match either
        #    the neutral border (fresh selection) or the previously-applied
        #    select colour (re-paint when the override changed mid-drag).  Every
        #    component renders a parametric ``PackageBody`` whose outline pen can
        #    be recoloured in place, so the selection highlight is always the
        #    cyan body outline; the separate thin dashed bbox (step 5) is the
        #    only bbox overlay.
        for cid in selected:
            grp = comp_groups.get(cid)
            if not isinstance(grp, QGraphicsItemGroup):
                continue
            self._set_outline_colour(
                grp, QColor(theme.comp_border), select_rgb, match_rgb=border_rgb
            )
            new_rgb = select_rgb.rgb() & 0x00FFFFFF
            if cid in self._cyan_outlined and prev_rgb != new_rgb:
                # Already outlined in the previous colour — repaint to the new one.
                self._set_outline_colour(
                    grp, select_rgb, select_rgb, match_rgb=prev_rgb
                )
            self._cyan_outlined.add(cid)
        self._applied_outline_rgb = select_rgb.rgb() & 0x00FFFFFF

        # 3. Selected *groups* get their outline rect recoloured to cyan too.
        self._recolour_selected_group_outlines(theme, select_rgb)
        # 4. Selected *wires* get a bright neon overlay (components use the cyan
        #    outline above; wires can't be "outlined", so we overdraw instead).
        self._redraw_selected_wire_overlays()
        # 5. Thin dashed bounding box around each selected component (opt-in,
        #    in addition to the bright body outline).
        self._sync_selection_bbox_overlays(selected)
        return selected

    @traced()
    def _recolour_selected_group_outlines(
        self, theme: Theme, select_rgb: QColor
    ) -> None:
        """Recolour the outline rect of every selected group to the select colour.

        Group outlines are standalone scene items tagged ``"group_outline"``
        (with the group id in ``data(0)``), so they are addressed by tag rather
        than via the component groups.  Restores groups that left the selection.
        """
        tool = self._view.active_tool
        sel_groups: set[uuid.UUID] = {
            uuid.UUID(str(g)) for g in getattr(tool, "_selected_group_ids", set())
        }
        if not sel_groups and not self._cyan_group_outlined:
            return

        # Index group_outline rects by their group id (one scene scan).
        outline_by_gid: dict[str, QGraphicsRectItem] = {}
        for item in self._view.scene().items():
            if isinstance(item, QGraphicsRectItem) and item.data(1) == "group_outline":
                gid_str = item.data(0)
                if isinstance(gid_str, str):
                    outline_by_gid[gid_str] = item

        # Restore groups no longer selected to the normal group pen colour.
        # (render_groups rebuilds the exact active/dim pen on the next refresh;
        # this keeps the colour sane until then.)
        normal_rgb = QColor(theme.group_pen_color)
        for gid in self._cyan_group_outlined - sel_groups:
            rect = outline_by_gid.get(str(gid))
            if isinstance(rect, QGraphicsRectItem):
                self._recolour_pen(rect, normal_rgb)
        self._cyan_group_outlined &= sel_groups

        # Apply cyan to selected groups.
        for gid in sel_groups:
            rect = outline_by_gid.get(str(gid))
            if not isinstance(rect, QGraphicsRectItem):
                continue
            self._recolour_pen(rect, select_rgb)
            self._cyan_group_outlined.add(gid)

    @staticmethod
    @traced()
    def _recolour_pen(item: QGraphicsRectItem, to_colour: QColor) -> None:
        """Recolour *item*'s pen to *to_colour* (alpha and style preserved)."""
        p = item.pen()
        new_pen = QPen(p)
        keep = QColor(to_colour)
        keep.setAlphaF(p.color().alphaF())
        new_pen.setColor(keep)
        item.setPen(new_pen)

    @staticmethod
    @traced()
    def _set_outline_colour(
        grp: QGraphicsItemGroup,
        from_colour: QColor,
        to_colour: QColor,
        *,
        match_rgb: int | None = None,
    ) -> bool:
        """Recolour every child pen matching the body outline; return True if any.

        Matches on RGB only (the dimming pass scales pen alpha, so an exact
        ``QColor`` compare would miss dimmed outlines).  ``match_rgb`` overrides
        which colour to look for (used when restoring, where the current pen is
        cyan, not ``from_colour``).
        """
        from PySide6.QtWidgets import QAbstractGraphicsShapeItem

        want = match_rgb if match_rgb is not None else from_colour.rgb() & 0x00FFFFFF
        changed = False
        for child in grp.childItems():
            if not isinstance(child, QAbstractGraphicsShapeItem):
                continue  # only shape items (rect / path / ellipse) carry a pen
            p = child.pen()
            if (p.color().rgb() & 0x00FFFFFF) != want:
                continue
            new_pen = QPen(p)
            keep = QColor(to_colour)
            keep.setAlphaF(p.color().alphaF())  # preserve the dim level
            new_pen.setColor(keep)
            child.setPen(new_pen)
            changed = True
        return changed

    # -- selection bounding box (thin dashed rect, opt-in) -----------------
    #
    # A faint dashed rectangle around the whole component, drawn in addition to
    # the bright body outline.  It uses the same management lifecycle as the SVG
    # outline overlay (add / re-sync / remove on selection change) but is gated
    # by ``show_selection_bbox``.

    @traced()
    def _selection_bbox_pen(self) -> QPen:
        theme = self._view.theme
        pen = QPen(QColor(theme.selection_bbox_color), theme.selection_bbox_width)
        pen.setStyle(Qt.PenStyle.DashLine)
        return pen

    @traced()
    def _add_selection_bbox_overlay(self, comp_id: uuid.UUID) -> None:
        from protoboard.gui.layer_state import Z_SELECTION

        bbox = self._view.session.component_bbox(comp_id)
        if bbox is None:
            return
        m = 0.06
        rect = QGraphicsRectItem(to_qrectf(bbox.inflate(Insets.all(m))))
        rect.setPen(self._selection_bbox_pen())
        rect.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        rect.setZValue(Z_SELECTION)
        self._view.scene().addItem(rect)
        self._selection_bbox_overlays[comp_id] = rect

    @traced()
    def _update_selection_bbox_overlay(self, comp_id: uuid.UUID) -> None:
        rect = self._selection_bbox_overlays.get(comp_id)
        if rect is None:
            return
        bbox = self._view.session.component_bbox(comp_id)
        if bbox is None:
            return
        m = 0.06
        try:
            rect.setRect(to_qrectf(bbox.inflate(Insets.all(m))))
            rect.setPen(self._selection_bbox_pen())
        except RuntimeError:
            self._selection_bbox_overlays.pop(comp_id, None)

    @traced()
    def _remove_selection_bbox_overlay(self, comp_id: uuid.UUID) -> None:
        rect = self._selection_bbox_overlays.pop(comp_id, None)
        if rect is None:
            return
        try:
            if rect.scene() is self._view.scene():
                self._view.scene().removeItem(rect)
        except RuntimeError:
            pass

    @traced()
    def _sync_selection_bbox_overlays(self, selected: set[uuid.UUID]) -> None:
        """Add/refresh a dashed bbox for each selected component, drop the rest.

        No-op (and clears any leftovers) when ``show_selection_bbox`` is off."""
        target = selected if self._show_selection_bbox else set()
        for cid in set(self._selection_bbox_overlays) - target:
            self._remove_selection_bbox_overlay(cid)
        for cid in target:
            if cid in self._selection_bbox_overlays:
                self._update_selection_bbox_overlay(cid)
            else:
                self._add_selection_bbox_overlay(cid)

    # -- selected-wire neon overlays -------------------------------------------

    @traced()
    def _redraw_selected_wire_overlays(self) -> set[uuid.UUID]:
        """Draw a bright-yellow overlay over every *selected* wire.

        The dimming pipeline fades unselected items, but a selected wire then
        looks identical to any other in-context wire — there is no positive "you
        picked *this*" signal (the exact complaint in bug #3).  This draws a
        thick neon-yellow polyline on top of each selected wire's path, the same
        treatment the legacy SelectTool gave (``routing_target_color``, round
        caps), so the clicked wire is unmistakable even when colours are hard to
        tell apart.

        Reads the active tool's ``_selected_wire_ids`` and is idempotent: wires
        that left the selection have their overlay removed.  Skips wires the
        tool is actively editing (``[E]``) — that mode draws its own cyan
        overlay and a second one on top would only clutter.  Returns the
        selected wire-id set for the caller's back-cache signature.
        """
        from PySide6.QtWidgets import QGraphicsLineItem

        from protoboard.gui.layer_state import Z_WIRE_HIGHLIGHT

        theme = self._view.theme
        tool = self._view.active_tool
        selected: set[uuid.UUID] = set(getattr(tool, "_selected_wire_ids", set()))
        # Don't double-draw over a wire the tool is editing in place.
        editing = bool(getattr(tool, "is_wire_editing", lambda: False)())
        edit_wire = getattr(tool, "_wire_edit_wire_id", None) if editing else None

        # 1. Drop overlays for wires no longer selected (or now being edited).
        for wid in set(self._wire_select_overlays):
            if wid not in selected or wid == edit_wire:
                self._remove_wire_select_overlay(wid)

        # 2. Build a bright overlay for each freshly-selected wire.
        yellow = QColor(theme.routing_target_color)
        yellow.setAlpha(220)
        pen = QPen(yellow, 0.38)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        wires_by_id = {w.id: w for w in self._view.session.layout.wires}
        for wid in selected:
            if wid == edit_wire or wid in self._wire_select_overlays:
                continue
            wire = wires_by_id.get(wid)
            if wire is None or len(wire.path) < 2:
                continue
            group = QGraphicsItemGroup()
            pts = wire.path
            for i in range(len(pts) - 1):
                seg = QGraphicsLineItem(pts[i].x, pts[i].y, pts[i + 1].x, pts[i + 1].y)
                seg.setPen(pen)
                group.addToGroup(seg)
            # Below the pin-modifier glyphs (Z 9.5) so pin markers on the
            # wire's route stay legible on top of the highlight.
            group.setZValue(Z_WIRE_HIGHLIGHT)
            self._view.scene().addItem(group)
            self._wire_select_overlays[wid] = group
        return selected

    @traced()
    def _remove_wire_select_overlay(self, wire_id: uuid.UUID) -> None:
        group = self._wire_select_overlays.pop(wire_id, None)
        if group is None:
            return
        try:
            if group.scene() is self._view.scene():
                self._view.scene().removeItem(group)
        except RuntimeError:
            # C++ item already destroyed by a scene rebuild — nothing to do.
            pass

    # -- net-pad discs + free-pad ring -------------------------------------------

    @traced()
    def _selection_disc_net_ids(self, fallback: set[uuid.UUID]) -> set[uuid.UUID]:
        """Nets whose pads get the bright highlight (bhl), per the spec:

        * the selection's **net context** (``SelectTool.context_net_id``) — set
          only by a click on a *pin-pad* (that pin's net) or a *wire* (the wire's
          net); a body click / rubber-band / group carries none, so no discs;
        * plus the nets of any **selected wires** — rule 5 "pads at those wires".

        Components selected by body click contribute **nothing** (no net context).
        Falls back to *fallback* (e.g. RouteTool's routing net) when the active
        tool exposes no context (non-SelectTool tools).
        """
        tool = self._view.active_tool
        ctx = getattr(tool, "context_net_id", "__missing__")
        if ctx == "__missing__":
            return set(fallback)  # not a SelectTool — keep the routing fallback
        # SelectTool: discs come ONLY from the click context + selected wires —
        # NOT the published net_id (which still drives dimming and is the union
        # for a body click).  An empty result → no discs (body click / multi).
        out: set[uuid.UUID] = set()
        if isinstance(ctx, uuid.UUID):
            out.add(ctx)
        sel_wires = getattr(tool, "_selected_wire_ids", None)
        if sel_wires:
            wire_net_map = self._view.session.cached_wire_net_map()
            for wid in sel_wires:
                wnid = wire_net_map.get(wid)
                if wnid is not None:
                    out.add(wnid)
        return out

    @traced()
    def _redraw_net_pad_highlights(self, net_ids: set[uuid.UUID]) -> None:
        """Draw bright-yellow discs on every pad of the *highlighted nets*.

        Ported from the legacy SelectTool's ``_draw_net_pad_highlights`` (kept
        as-is — it read well): a fully-opaque neon-yellow disc
        (``routing_target_color``) on each pin-pad position belonging to a
        highlighted net, at ``Z_NET_PAD_HIGHLIGHT`` (above the coloured pads /
        wires / back wash, below the pin-modifier glyph so the modifier shape
        stays visible on top).  The pin-pad layer itself never reacts to
        selection, so without this nothing tells the user which pads the picked
        net touches.

        Reuses the records cached from the last refresh's ``classify_pin_pads``
        so no reclassification happens on a plain selection click.  An empty
        *net_ids* just tears the discs down.
        """
        from PySide6.QtWidgets import QGraphicsEllipseItem

        from protoboard.domain.layout.pin_pad_classification import PinPadInfo
        from protoboard.gui.layer_state import Z_NET_PAD_HIGHLIGHT

        self._clear_net_pad_highlights()
        self._draw_free_pad_ring()  # the clicked free-pad ring (net-independent)
        if not net_ids:
            return
        theme = self._view.theme
        # Disc radius = 70% of the pad radius (sits inside the pad rather than
        # swamping it); fill at ~50% alpha so the pad colour underneath still
        # reads, while the ring stays opaque for a crisp high-contrast edge.
        r = 0.7 * theme.pad_radius
        ring = QColor(theme.routing_target_color)
        ring.setAlpha(255)
        fill = QColor(theme.routing_target_color)
        fill.setAlpha(128)
        pen = QPen(ring, 0.12)
        brush = QBrush(fill)
        for rec in self._pin_pad_records:
            if not isinstance(rec, PinPadInfo) or rec.net_id not in net_ids:
                continue
            pt = rec.position
            item = QGraphicsEllipseItem(pt.x - r, pt.y - r, 2 * r, 2 * r)
            item.setPen(pen)
            item.setBrush(brush)
            item.setZValue(Z_NET_PAD_HIGHLIGHT)
            self._view.scene().addItem(item)
            self._net_pad_highlight_items.append(item)

    @traced()
    def _draw_free_pad_ring(self) -> None:
        """Draw the light ring on the clicked free pin-pad (if any).

        A bright, near-white ring (brighter than the resting ``pin_free_color``)
        at ``_free_pad_ring_pos`` — feedback that a netless pad was clicked.
        Tracked in ``_net_pad_highlight_items`` so it is torn down with the discs
        on the next highlight pass.  No-op when no free pad is the click target.
        """
        from PySide6.QtWidgets import QGraphicsEllipseItem

        from protoboard.gui.layer_state import Z_NET_PAD_HIGHLIGHT

        pos = self._free_pad_ring_pos
        if pos is None:
            return
        theme = self._view.theme
        r = 0.7 * theme.pad_radius
        ring_col = QColor("#F0F4F8")  # near-white, clearly brighter than grey free pads
        pen = QPen(ring_col, 0.1)
        item = QGraphicsEllipseItem(pos.x - r, pos.y - r, 2 * r, 2 * r)
        item.setPen(pen)
        item.setBrush(QBrush(Qt.BrushStyle.NoBrush))  # ring only, no fill
        item.setZValue(Z_NET_PAD_HIGHLIGHT)
        self._view.scene().addItem(item)
        self._net_pad_highlight_items.append(item)

    @traced()
    def _clear_net_pad_highlights(self) -> None:
        scene = self._view.scene()
        for item in self._net_pad_highlight_items:
            try:
                if isinstance(item, QGraphicsItem) and item.scene() is scene:
                    scene.removeItem(item)
            except RuntimeError:
                pass  # C++ item already gone (scene cleared)
        self._net_pad_highlight_items.clear()
