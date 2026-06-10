# SPDX-License-Identifier: MIT
"""BoardView — QGraphicsView showing the board with all layers."""

from __future__ import annotations

import math
import os
import time
import uuid

import structlog
_click_log = structlog.get_logger("protoboard.gui.board_view.click")
_CLICK_PROFILE = os.environ.get("PROTOBOARD_CLICK_PROFILE") not in (None, "", "0")
# PROTOBOARD_PAINT_PROFILE — log per-phase paint timings on every paintEvent.
# Unlike PROTOBOARD_CLICK_PROFILE this is NOT gated on a recent mouse press,
# so it captures the ghost-follow paints in PlaceTool too.
_paint_log = structlog.get_logger("protoboard.gui.board_view.paint")
_PAINT_PROFILE = os.environ.get("PROTOBOARD_PAINT_PROFILE") not in (None, "", "0")

_DRAG_REFRESH_INTERVAL = 0.033  # 30 fps max during live drag

from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, QTimer, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPixmap,
    QResizeEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QGraphicsItemGroup,
    QGraphicsScene,
    QGraphicsView,
    QWidget,
)

from protoboard.domain.board.boxes import StripAtom
from protoboard.domain.connectivity.graph import ConnectivityGraph
from protoboard.geometry import Point
from protoboard.gui.layer_state import LayerOverrides, LayerState, layer_state_for_tool
from protoboard.gui.renderers.board_renderer import (
    BOARD_BG,
    build_board_display_list,
    paint_board,
)
from protoboard.gui.renderers.component_renderer import (
    render_components,
    render_groups,
    update_component_groups,
)
from protoboard.gui.renderers.wire_renderer import (
    render_ratsnest,
    render_wires_with_nets,
    update_wire_groups,
)
from PySide6.QtWidgets import QGraphicsItem as _QGItem  # noqa: E402 – used in refresh()
from protoboard.gui.session import EditorSession
from protoboard.gui.theme import DEFAULT_THEME
from protoboard.services.routing.net_status import routed_net_ids

# Scene units = raster units. 1 raster unit = 1 "pixel" in scene space.
_GRID_PX = 60.0  # initial pixels-per-raster-unit


class BoardView(QGraphicsView):
    """Main board canvas. Handles zoom/pan and forwards mouse events to tools."""

    cursor_moved = Signal(float, float)  # raster x, y of cursor
    pad_clicked = Signal(float, float)  # snapped raster x, y
    escape_tool = Signal()  # right-click or Esc — return to Select

    def __init__(self, session: EditorSession, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._session = session
        self._scene = QGraphicsScene(self)
        # Disable BSP tree index — at 14k+ items a BSP rebuild every time
        # setOpacity / setVisible runs (which happens on every click via
        # _apply_highlight_opacity) dominates paint time.  Our own spatial
        # indexes (_pad_spatial, session.wire_spatial_index) cover hit-tests;
        # Qt only needs to walk items for paint, where O(N) iteration beats
        # O(log N) lookup + O(N) BSP refresh.
        self._scene.setItemIndexMethod(QGraphicsScene.ItemIndexMethod.NoIndex)
        self.setScene(self._scene)
        # Anti-aliasing is a user preference (AppSettings.antialiasing,
        # default True).  On very large boards or weak GPUs the user can
        # turn it off to claw back paint time at the cost of jagged
        # pad / wire edges.  ``set_antialiasing`` flips this flag at
        # runtime and invalidates cached pixmaps.
        self._antialiasing: bool = True
        self.setRenderHint(QPainter.RenderHint.Antialiasing, self._antialiasing)
        # Connections (ratsnest) layer render mode — mirrors
        # ``AppSettings.ratsnest_mode``.  "full" = K_n graph per island;
        # "closest" = nearest-other-pin per pin.  MainWindow pushes
        # changes via ``set_ratsnest_mode`` so the dialog edit lands
        # without a full session reload.
        self._ratsnest_mode: str = "full"
        # DontSavePainterState avoids QPainter.save()/restore() around every
        # item — meaningful at 14k items per paint.  Items in this app set
        # their own pen/brush per draw, so the saved state was unused anyway.
        self.setOptimizationFlag(
            QGraphicsView.OptimizationFlag.DontSavePainterState, True,
        )
        # FullViewportUpdate skips the per-item bounding-rect intersection
        # pass that MinimalViewportUpdate runs to compute dirty regions.
        # For dense boards where most items are within the viewport every
        # repaint anyway, paying for the bounding-rect math is wasted work.
        self.setViewportUpdateMode(
            QGraphicsView.ViewportUpdateMode.FullViewportUpdate,
        )
        self.setBackgroundBrush(QBrush(BOARD_BG))
        # Tools (PlaceTool ghost, RouteTool live line, hover cursor) need
        # mouseMoveEvent when no button is pressed.  Without tracking the
        # viewport Qt only emits mouse-move while a button is held — the
        # ghost would stay at (0,0) until first click.
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self._scale = _GRID_PX
        self.scale(_GRID_PX, _GRID_PX)
        self._panning = False
        self._ctrl_panning = False
        self._pan_start = QPointF()
        self._active_tool: _Tool | None = None
        self._permanent_tools: list[_Tool] = []  # always notified on scene_cleared
        # View state — camera side and visual transform are independent.
        self._camera_side: str = "top"           # "top" | "bottom"
        self._view_transform: str = "normal"     # "normal" | "mirror_h" | "mirror_v"
        self._layer_overrides: LayerOverrides = LayerOverrides()
        self._pad_positions: list[Point] = []
        # Active group for rendering context (set by SelectTool on navigation).
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        self._active_group_id: uuid.UUID = ROOT_GROUP_ID
        # Net highlight: legacy fields kept for set_highlighted_net_silent().
        self._highlighted_net_id: uuid.UUID | None = None
        self._highlight_profile: object | None = None
        # Subscribe to highlight stack changes.
        session.highlight_stack.changed.connect(self._apply_highlight_opacity)
        # Subscribe to routing-net changes (RouteTool while-drawing union with stack).
        # Apply opacity immediately for the live feedback while drawing, AND
        # schedule a full refresh so the per-pin glow ring is rebuilt — the
        # ring is drawn inside the component group at render time, so a
        # mere opacity tweak can't add or remove it.
        session.routing_net_changed.connect(self._apply_highlight_opacity)
        session.routing_net_changed.connect(self._on_routing_net_changed)
        # Theme — overridden by MainWindow after Preferences apply.
        from protoboard.gui.theme import Theme
        self._theme_override: Theme | None = None
        self._last_drag_refresh: float = 0.0
        # comp_id → QGraphicsItemGroup; persists across refreshes for reuse.
        self._comp_groups: dict[uuid.UUID, object] = {}
        self._comp_fingerprints: dict[uuid.UUID, tuple[object, ...]] = {}
        # wire_id → QGraphicsItemGroup; persists across refreshes for reuse.
        self._wire_groups: dict[uuid.UUID, object] = {}
        self._wire_fingerprints: dict[uuid.UUID, tuple[object, ...]] = {}
        # Wire-pin glyph items — rebuilt from scratch on each refresh
        # since the set is typically small.
        self._wire_pin_items: list[object] = []
        # Pin-pads + modifier overlay items — rebuilt every refresh because
        # they're cheap to compute from pad_index and the layer reflects
        # schematic-aware state that fingerprinting wire/comp groups
        # would not capture cleanly.
        self._pin_pad_items: list[object] = []
        self._pin_modifier_items: list[object] = []
        # comp_id → placement.position at last full refresh (drag delta origin).
        self._drag_orig_group_pos: dict[uuid.UUID, object] = {}
        # wire_id → wire.path[0] at last full refresh (drag delta origin for wires).
        self._drag_orig_wire_pose: dict[uuid.UUID, object] = {}
        # Board layer: rebuilt only when board geometry / theme / alpha changes.
        self._board_layer_dirty: bool = True
        self._last_board_alpha: float = -1.0   # sentinel: never matches
        # Background cache — viewport-sized QPixmap holding the rasterised
        # board layer (pads, holes, strip tracks, frames).  Painted in
        # ``drawBackground`` by blitting and never participates in the
        # scene's per-item paint dispatch.  Invalidated on any change to
        # board geometry, theme, layer alpha, viewport size, or transform.
        self._bg_cache: object | None = None  # QPixmap, kept loose-typed
        self._bg_cache_dirty: bool = True
        # ``BoardDisplayList`` is rebuilt alongside the cache pixmap but
        # cached separately so a viewport-only resize (no board change)
        # can re-rasterise without re-walking ``board.atoms()``.
        self._bg_display_list: object | None = None  # BoardDisplayList
        # Side-layer pixmaps for the layered "sandwich" composition.
        # Each holds the rasterised scene items whose zValue lies in the
        # given side band (back = 0..2.999, front = 3.001..6.999).
        # Composited in drawBackground in Z order:
        #   board_base (alpha=alpha_for_board)
        #   back       (one blit)
        #   board_mid  (same pixmap as base, alpha=alpha_for_board_mid)
        #   front      (one blit)
        # Front-side scene items remain visible above this stack at their
        # own Z so live drag / selection overlays still update naturally.
        # ``_back_cache`` is the only cache active in MVP — front items
        # stay in scene paint dispatch.  ``_back_dirty`` is set whenever
        # back-side items change, viewport resizes, or transform changes.
        self._back_cache: object | None = None
        self._back_cache_dirty: bool = True
        # Snapshot of (comp_ids, wire_ids, profile_id) baked into the
        # current ``_back_cache`` pixmap.  ``_apply_highlight_opacity``
        # compares this to the new highlight set; on a mismatch we
        # invalidate the back cache so the next paint rasterises the
        # bottom-side wires with the fresh bright/dim state.  Without
        # this, opacity changes on items already hidden + cached
        # produced a stale pixmap — wires across the camera kept their
        # old highlight until the user pressed Esc (see memory
        # ``back-cache-highlight-stale-2026-06-08``).
        self._last_highlight_signature: int = 0
        # Scene items kept hidden while a valid _back_cache shows them
        # as a rasterised blit instead.  Restored on cache invalidation.
        self._back_hidden_items: list[object] = []
        # Stale-blit anchor: the most recent _bg_cache + its viewportTransform
        # at the moment we built it.  While the user is in a wheel-zoom or
        # pan-scroll burst we paint the anchor pixmap under the inverse
        # transform delta (cheap Qt-side scale/translate) instead of
        # rebuilding the board pixmap on every frame.  After the burst
        # settles we rebuild at the new transform and refresh the anchor.
        self._bg_cache_anchor: object | None = None
        self._bg_cache_anchor_transform: object | None = None  # QTransform
        # Paint profiling counters — incremented each time a phase actually
        # runs.  Reported alongside per-paint timings when
        # PROTOBOARD_PAINT_PROFILE is set so we can tell e.g. "blit_base
        # ran 50× but blit_mid only 2×" or "back-cache rebuilt 50× over
        # 50 paints" (which is the smell we're hunting).
        self._paint_counters: dict[str, int] = {
            "paintEvent": 0,
            "drawBackground": 0,
            "drawBackground_burst": 0,
            "fillRect": 0,
            "bg_cache_rebuild": 0,
            "back_cache_rebuild": 0,
            "back_cache_empty": 0,   # back items list empty, no pixmap built
            "blit_base": 0,
            "blit_back": 0,
            "blit_back_filter": 0,
            "blit_mid": 0,
            "anchor_blit": 0,
            "anchor_blit_back": 0,
        }
        # Spatial index over board pad positions for fast snap().
        self._pad_spatial: dict[tuple[int, int], list[Point]] = {}
        # Overlay items (ratsnest + group outlines) for targeted removal.
        self._overlay_items: list[object] = []
        # Debounce refresh so rapid layout_changed bursts only redraw once.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(16)   # ~1 frame
        self._refresh_timer.timeout.connect(self.refresh)
        # Label-visibility refresh after zoom is O(scene items) — on a
        # busy board it costs tens of ms.  Running it synchronously
        # inside ``wheelEvent`` made Qt drop / queue subsequent wheel
        # ticks because the event handler hadn't returned yet.  We
        # debounce to ~80 ms so a burst of wheel ticks (typical user
        # scroll: 4–8 ticks in <100 ms) collapses to a single
        # visibility refresh after the burst settles.
        self._label_vis_timer = QTimer(self)
        self._label_vis_timer.setSingleShot(True)
        self._label_vis_timer.setInterval(80)
        self._label_vis_timer.timeout.connect(self._apply_label_visibility)
        # Rubber-band throttle interval — read by SelectTool when it
        # builds its own timer.  Stored on the view so PreferencesDialog
        # can adjust the value live (call ``apply_throttle_settings``)
        # without rebuilding the tool.
        self._rubber_band_throttle_ms: int = 33
        # Wheel-zoom accumulator.  Each ``wheelEvent`` adds its delta
        # (in 1/120-of-a-degree units, Qt's angleDelta) into the buffer
        # and parks the most-recent cursor viewport position.  A short
        # timer collapses bursts of ticks into a single scale + scroll
        # pass, so over SSH the wheel handler returns in microseconds
        # and Qt processes the next tick without waiting for a paint.
        self._wheel_accum_delta: int = 0
        self._wheel_pivot_vp: object | None = None
        self._wheel_timer = QTimer(self)
        self._wheel_timer.setSingleShot(True)
        self._wheel_timer.setInterval(16)  # ~1 frame; tunable via apply_throttle_settings
        self._wheel_timer.timeout.connect(self._flush_wheel_zoom)
        # Pan-burst detector — restarted on each scrollContentsBy.  While
        # this timer is active we treat the view as in mid-pan and paint
        # the board pixmap by stretching the anchor instead of rebuilding.
        # When it fires (settle), the next paint rebuilds the cache fresh.
        self._pan_burst_timer = QTimer(self)
        self._pan_burst_timer.setSingleShot(True)
        self._pan_burst_timer.setInterval(120)
        self._pan_burst_timer.timeout.connect(self._on_pan_settled)
        session.layout_changed.connect(self._schedule_refresh)
        session.layout_changed.connect(session.invalidate_graph)
        session.drag_moved.connect(self._on_drag_moved)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def apply_throttle_settings(
        self,
        *,
        rubber_band_ms: int,
        zoom_refresh_ms: int,
    ) -> None:
        """Update render-throttle intervals at runtime.

        Called from MainWindow after PreferencesDialog applies a new
        :class:`AppSettings`.  SelectTool rebuilds its rubber-band timer
        lazily, so the new value takes effect on the next rubber-band
        start; the post-zoom refresh timer is updated immediately.
        """
        rubber_band_ms = max(0, int(rubber_band_ms))
        zoom_refresh_ms = max(0, int(zoom_refresh_ms))
        self._rubber_band_throttle_ms = rubber_band_ms
        self._label_vis_timer.setInterval(zoom_refresh_ms)

    def set_ratsnest_mode(self, mode: str) -> None:
        """Switch the Connections layer between ``"full"`` (K_n graph)
        and ``"closest"`` (nearest-other-pin) line modes.  No-op when
        the mode is already active; otherwise refresh the overlay."""
        if mode not in ("full", "closest") or mode == self._ratsnest_mode:
            return
        self._ratsnest_mode = mode
        self.refresh()

    def set_antialiasing(self, enabled: bool) -> None:
        """Toggle Antialiasing render hint and rebuild raster caches.

        Affects three paint surfaces: the view's own scene paint
        (front-side items), the board pixmap, and the back-side cache.
        Cached pixmaps were rasterised under the previous setting, so
        we invalidate them — the next paint rebuilds with the new hint.
        """
        enabled = bool(enabled)
        if enabled == self._antialiasing:
            return
        self._antialiasing = enabled
        self.setRenderHint(QPainter.RenderHint.Antialiasing, enabled)
        self._invalidate_bg_cache()
        self.viewport().update()

    @property
    def camera_side(self) -> str:
        return self._camera_side

    @property
    def view_transform(self) -> str:
        return self._view_transform

    @property
    def active_group_id(self) -> uuid.UUID:
        return self._active_group_id

    def set_active_group(self, group_id: uuid.UUID) -> None:
        """Called by SelectTool on group navigation to trigger a re-render."""
        self._active_group_id = group_id
        self.refresh()

    def _effective_highlighted_net(self) -> "uuid.UUID | None":
        """Pick the net id that should drive glow-ring rendering.

        Priority: the actively-drawn routing net (``session.routing_net_id``)
        wins over the legacy ``_highlighted_net_id`` set by Net Inspector.
        Routing is a stronger UI signal — when the user is mid-route, the
        pads they're targeting must be unambiguously called out.
        """
        rn = self._session.routing_net_id
        if rn is not None:
            return rn
        return self._highlighted_net_id

    def _on_routing_net_changed(self, _net_id: object) -> None:
        """Invalidate component/wire group caches so glow rings rebuild.

        Group fingerprints intentionally exclude highlight state (it's
        usually applied via opacity-only updates), so a change in
        ``routing_net_id`` would not trigger a rebuild by itself.  Clear
        the caches and ask for a refresh — the next paint pass will
        render the glow rings on pads belonging to the routed net.
        """
        self._comp_fingerprints.clear()
        self._wire_fingerprints.clear()
        self._schedule_refresh()

    def set_highlighted_net(self, net_id: uuid.UUID | None) -> None:
        """Legacy: update local net_id and apply opacity (used by SelectTool internals)."""
        if net_id == self._highlighted_net_id and self._highlight_profile is None:
            return
        self._highlighted_net_id = net_id
        self._highlight_profile = None
        self._apply_highlight_opacity()

    def set_highlighted_net_with_profile(
        self, net_id: uuid.UUID | None, profile: object
    ) -> None:
        """Legacy: update local net_id+profile and apply opacity."""
        self._highlighted_net_id = net_id
        self._highlight_profile = profile
        self._apply_highlight_opacity()

    def _apply_highlight_opacity(self) -> None:
        _t0 = time.monotonic() if _CLICK_PROFILE else 0.0
        try:
            self._apply_highlight_opacity_impl()
        finally:
            if _CLICK_PROFILE:
                _click_log.info(
                    "click_profile.apply_highlight_opacity",
                    ms=round((time.monotonic() - _t0) * 1000, 1),
                    n_comp_groups=len(self._comp_groups),
                    n_wire_groups=len(self._wire_groups),
                )

    def _apply_highlight_opacity_impl(self) -> None:
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
        from PySide6.QtWidgets import QGraphicsItemGroup
        from protoboard.gui.highlight_state import HighlightEntry, PROFILE_DIMMED_1
        from protoboard.gui.interaction_mode import HighlightProfile

        session = self._session
        _base = self._theme_override if self._theme_override is not None else DEFAULT_THEME

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
            for grp in self._comp_groups.values():
                if isinstance(grp, QGraphicsItemGroup):
                    grp.setOpacity(1.0)
            for grp in self._wire_groups.values():
                if isinstance(grp, QGraphicsItemGroup):
                    grp.setOpacity(1.0)
            # No-highlight branch — also invalidate the back cache when
            # we just transitioned out of a non-empty highlight state.
            # Without this, the cached pixmap keeps the old dimmed
            # back-side items even though every front item is now back
            # at opacity 1.0 (user-visible: third Esc looks like a
            # no-op because the back-cache pixmap is stale).
            cleared_signature = hash((
                frozenset(), frozenset(), frozenset(), id(None),
            ))
            if cleared_signature != self._last_highlight_signature:
                self._last_highlight_signature = cleared_signature
                self._invalidate_back_cache()
                self.viewport().update()
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

        for comp_id, grp in self._comp_groups.items():
            if isinstance(grp, QGraphicsItemGroup):
                if comp_id in highlighted_comp_ids:
                    grp.setOpacity(hi_alpha)
                else:
                    grp.setOpacity(max(0.04, dim_c))

        for wire_id, grp in self._wire_groups.items():
            if isinstance(grp, QGraphicsItemGroup):
                if wire_id in highlighted_wire_ids:
                    grp.setOpacity(1.0)
                elif wire_net_map.get(wire_id) in net_ids:
                    grp.setOpacity(1.0)
                else:
                    grp.setOpacity(dim_w)

        # Invalidate the back-cache pixmap when the *set* of highlighted
        # items changes — opacity-only shifts (same set, different alpha
        # from a profile tweak) do not require a rebuild because the
        # cached pixmap is rasterised with whatever opacity the items
        # had at render time and a setOpacity on a hidden item is
        # cheap.  The signature here is sensitive only to membership
        # changes plus the active profile id (different profile = items
        # rendered at a different intrinsic alpha during build).
        new_signature = hash((
            frozenset(highlighted_comp_ids),
            frozenset(highlighted_wire_ids),
            frozenset(net_ids),
            id(profile_obj),
        ))
        if new_signature != self._last_highlight_signature:
            self._last_highlight_signature = new_signature
            self._invalidate_back_cache()
            # Re-trigger a paint so the back cache gets rebuilt now
            # rather than on the next mouse move; otherwise the user
            # sees the stale pixmap until they wiggle the cursor.
            self.viewport().update()

    _LABEL_MIN_SCENE_UNITS: float = 0.8

    def _apply_label_visibility(self) -> None:
        """Show/hide overlay text labels based on current view scale.

        Labels use ItemIgnoresTransformations so they render at a fixed screen
        size.  Below _LABEL_MIN_SCENE_UNITS (viewport-pixels per raster unit)
        they would overlap each other and are hidden.  Background rects (tagged
        "label_bg") are shown/hidden together with their text items.
        """
        _t0 = time.monotonic() if _CLICK_PROFILE else 0.0
        from PySide6.QtWidgets import QGraphicsRectItem, QGraphicsTextItem
        scene_scale = abs(self.transform().m11())
        show = scene_scale >= self._LABEL_MIN_SCENE_UNITS
        n_items = 0
        for item in self._scene.items():
            n_items += 1
            if isinstance(item, (QGraphicsTextItem, QGraphicsRectItem)):
                if isinstance(item, QGraphicsRectItem) and item.data(0) != "label_bg":
                    continue
                if item.isVisible() != show:
                    item.setVisible(show)
        if _CLICK_PROFILE:
            _click_log.info(
                "click_profile.apply_label_visibility",
                ms=round((time.monotonic() - _t0) * 1000, 1),
                n_scene_items=n_items,
            )

    def set_highlighted_net_silent(self, net_id: uuid.UUID | None) -> None:
        """Set the highlighted net without triggering a refresh.

        Use when the caller will redraw overlays itself (e.g. RouteTool) and
        wants the dim to take effect on the *next* natural refresh only.
        """
        self._highlighted_net_id = net_id

    def _on_drag_moved(self) -> None:
        """Live drag: move scene groups to current placement positions (no rebuild).

        Items inside each group are rendered at absolute world coordinates, so the
        group itself sits at (0,0).  We track the *last rendered* position per group
        and call moveBy(delta) when the placement changes — this translates all child
        items without rebuilding the scene.
        """
        from PySide6.QtWidgets import QGraphicsItemGroup, QGraphicsRectItem
        placements = self._session.layout.placements
        for comp_id, grp in self._comp_groups.items():
            if not isinstance(grp, QGraphicsItemGroup):
                continue
            pl = placements.get(comp_id)
            orig = self._drag_orig_group_pos.get(comp_id)
            if pl is None or orig is None:
                continue
            # Delta from the position when items were baked into the scene.
            dx = pl.position.x - orig.x
            dy = pl.position.y - orig.y
            grp.setPos(dx, dy)

        # Recompute outline rect for each group from the live pin positions
        # of its members.  Outline overlay items live outside _comp_groups
        # (they're owned by self._overlay_items), so a setPos on a comp
        # group does not move them.  We update each outline rect in-place;
        # the matching group-name label is moved alongside.
        # Wires: items inside each wire group sit at world coords (just like
        # components), so a setPos(delta) translates the visible polyline.
        # Live drag mutates wire.path in place — we compare current path[0]
        # against the snapshot taken at the previous refresh.  After drag
        # release, the upcoming refresh() rebuilds the wire group from the
        # new path and resets pos to (0, 0).
        for wire in self._session.layout.wires:
            grp = self._wire_groups.get(wire.id)
            if grp is None or not wire.path:
                continue
            orig = self._drag_orig_wire_pose.get(wire.id)
            if orig is None:
                continue
            dx = wire.path[0].x - orig.x
            dy = wire.path[0].y - orig.y
            grp.setPos(dx, dy)

        from protoboard.domain.layout.group import all_component_ids
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        layout = self._session.layout
        # Used by find_owned_wires below (group-outline ownership classifier).
        placements = layout.placements
        instances = self._session.instances
        components = self._session.components
        theme = self._theme_override if self._theme_override is not None else DEFAULT_THEME
        margin = theme.group_margin
        # Index outline rects + labels by group id.
        outline_by_gid: dict[uuid.UUID, QGraphicsRectItem] = {}
        for item in self._overlay_items:
            if not isinstance(item, QGraphicsRectItem):
                continue
            tag = item.data(1) if hasattr(item, "data") else None
            if tag != "group_outline":
                continue
            raw = item.data(0)
            if not raw:
                continue
            try:
                outline_by_gid[uuid.UUID(str(raw))] = item
            except (ValueError, TypeError):
                continue
        if not outline_by_gid:
            return
        # Pre-resolve once: which wires are internal to each group?
        # Net-aware classifier (group_ownership.find_owned_wires) handles
        # T-junction half-orphans correctly: tails of internal nets are
        # owned, tails of exported nets are not — so the outline neither
        # shrinks at drag-start nor stretches over interface wires.
        from protoboard.domain.layout.group_ownership import find_owned_wires
        # Resolve wire→net once for this paint; cached on session.
        try:
            wire_net_map = self._session.cached_wire_net_map() if self._session is not None else {}
        except Exception:  # noqa: BLE001
            wire_net_map = {}
        schematic = getattr(self._session, "schematic", None) if self._session is not None else None
        for gid, rect_item in outline_by_gid.items():
            grp_obj = layout.groups.get(gid)
            if grp_obj is None or gid == ROOT_GROUP_ID:
                continue
            comp_ids = all_component_ids(layout.groups, gid)
            xs: list[float] = []
            ys: list[float] = []
            for cid in comp_ids:
                for _pn, p in self._session.iter_pin_positions(cid):
                    xs.append(p.x)
                    ys.append(p.y)
            if not xs:
                continue
            # Include owned-wire vertices so the live outline matches
            # the statically-rendered one (otherwise outline shrinks
            # at drag-start and re-grows at release).
            try:
                owned_wire_ids = find_owned_wires(
                    list(layout.wires),
                    grp_obj,
                    wire_net_map=wire_net_map,
                    knots=layout.knots,  # type: ignore[arg-type]
                    group_comp_ids=comp_ids,
                    schematic=schematic,
                    placements=placements,
                    instances=instances,
                    definitions=components,
                )
                for w in layout.wires:
                    if w.id not in owned_wire_ids:
                        continue
                    for p in w.path:
                        xs.append(p.x)
                        ys.append(p.y)
            except Exception:  # noqa: BLE001
                # Defensive: never let a wire-classification glitch
                # break live drag — fall back to pin-only bbox.
                pass
            x0 = min(xs) - margin
            y0 = min(ys) - margin
            w = max(xs) - min(xs) + 2 * margin
            h = max(ys) - min(ys) + 2 * margin
            rect_item.setRect(QRectF(x0, y0, w, h))

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
        for wire_id, state in states.items():
            grp = self._wire_groups.get(wire_id)
            if not isinstance(grp, QGraphicsItemGroup):
                continue
            if state == "drop":
                grp.setOpacity(0.3)
            elif state == "disconnect":
                grp.setOpacity(0.5)
            else:
                grp.setOpacity(1.0)

    def set_theme(self, theme: object) -> None:
        from protoboard.gui.theme import Theme
        if isinstance(theme, Theme):
            self._theme_override = theme
            # board_bg is applied as the QGraphicsView background brush, not
            # as a scene item — re-apply it explicitly so the canvas around
            # the board picks up theme changes without an app restart.
            self.setBackgroundBrush(QBrush(QColor(theme.board_bg)))
            self._board_layer_dirty = True
            self.refresh()

    # ------------------------------------------------------------------
    # View control — camera and transform are independent.
    # ------------------------------------------------------------------

    def set_camera_side(self, side: str) -> None:
        """Switch between top and bottom camera.  Syncs tool side."""
        if side == self._camera_side:
            return
        self._camera_side = side
        self._board_layer_dirty = True
        self._sync_tool_side(side)
        self._apply_view_transform()
        self.refresh()

    def set_view_transform(self, transform: str) -> None:
        """Set the visual transform: 'normal', 'mirror_h', 'mirror_v', or 'mirror_hv'."""
        if transform == self._view_transform:
            return
        self._view_transform = transform
        self._board_layer_dirty = True
        self._apply_view_transform()
        self.refresh()

    def _has_mirror_h(self) -> bool:
        return self._view_transform in ("mirror_h", "mirror_hv")

    def _has_mirror_v(self) -> bool:
        return self._view_transform in ("mirror_v", "mirror_hv")

    @staticmethod
    def _compose_view_transform(*, h: bool, v: bool) -> str:
        if h and v:
            return "mirror_hv"
        if h:
            return "mirror_h"
        if v:
            return "mirror_v"
        return "normal"

    def _apply_view_transform(self) -> None:
        """Recompute the QGraphicsView scale matrix from current state."""
        sx = -1.0 if self._has_mirror_h() else 1.0
        sy = -1.0 if self._has_mirror_v() else 1.0
        # Preserve current zoom magnitude, apply sign flips.  Clamp to
        # current viewport-derived limits so a window resize that shrank
        # the allowed window doesn't leave us out of range.
        mag = abs(self.transform().m11()) or _GRID_PX
        try:
            m11_min, m11_max = self._compute_zoom_limits()
            mag = max(m11_min, min(m11_max, mag))
        except Exception:  # noqa: BLE001 — session may be detached during shutdown
            pass
        # Remember the scene point currently at the viewport centre so we can
        # restore it after setTransform (which resets scroll position).
        vp = self.viewport()
        cx, cy = vp.width() / 2, vp.height() / 2
        center_scene = self.mapToScene(int(cx), int(cy))
        from PySide6.QtGui import QTransform
        self.setTransform(QTransform(sx * mag, 0.0, 0.0, sy * mag, 0.0, 0.0))
        # Re-centre on the same scene point.
        self.centerOn(center_scene)
        # Transform changed → bg cache pixmap is in the wrong space, but
        # the pixmap *content* is still valid — the bridge transform
        # handles the mirror.  Keep the anchor for a smooth visual jump.
        self._invalidate_bg_cache(keep_anchor=True)

    def _sync_tool_side(self, side: str) -> None:
        from protoboard.gui.tools.route_tool import RouteTool
        if isinstance(self._active_tool, RouteTool):
            self._active_tool._side = side  # type: ignore[assignment]
        win = self.window()
        updater = getattr(win, "_set_wire_side", None)
        if updater is not None:
            updater(side)

    # Convenience toggles (used by menu/keyboard).  Each axis flips
    # independently so combining H + V produces ``mirror_hv``.
    def toggle_mirror_h(self) -> None:
        self.set_view_transform(self._compose_view_transform(
            h=not self._has_mirror_h(), v=self._has_mirror_v(),
        ))

    def toggle_mirror_v(self) -> None:
        self.set_view_transform(self._compose_view_transform(
            h=self._has_mirror_h(), v=not self._has_mirror_v(),
        ))

    def toggle_camera(self) -> None:
        self.set_camera_side(
            "bottom" if self._camera_side == "top" else "top"
        )

    # Legacy alias used by MainWindow._mirror_view (now means Mirror H).
    def toggle_mirror(self) -> None:
        self.toggle_mirror_h()

    @property
    def is_mirrored(self) -> bool:
        return self._has_mirror_h()

    def fit_board(self) -> None:
        """Scale and center the view to fit the board bounds."""
        b = self._session.board.bounds
        self.fitInView(
            QRectF(b.x - 1, b.y - 1, b.width + 2, b.height + 2),
            Qt.AspectRatioMode.KeepAspectRatio,
        )
        # fit_* / mirror: transform jumps but the pixmap content is still
        # valid — keep the anchor so the next paint can stretch-blit it
        # while the fresh rebuild happens.
        self._invalidate_bg_cache(keep_anchor=True)
        self._apply_label_visibility()

    def fit_area(self) -> None:
        """Fit view to the active group's components; falls back to fit_board() for ROOT."""
        from protoboard.domain.layout.group import all_component_ids
        from protoboard.domain.layout.layout import ROOT_GROUP_ID

        session = self._session
        active = self._active_group_id

        import uuid as _uuid
        if active == ROOT_GROUP_ID:
            comp_ids: frozenset[_uuid.UUID] = frozenset(session.layout.placements.keys())
        else:
            comp_ids = all_component_ids(session.layout.groups, active)

        xs: list[float] = []
        ys: list[float] = []
        for cid in comp_ids:
            bbox = session.component_bbox(cid)
            if bbox is None:
                continue
            xs += [bbox.x, bbox.x + bbox.width]
            ys += [bbox.y, bbox.y + bbox.height]

        if not xs:
            self.fit_board()
            return

        margin = 1.0
        self.fitInView(
            QRectF(min(xs) - margin, min(ys) - margin,
                   max(xs) - min(xs) + 2 * margin,
                   max(ys) - min(ys) + 2 * margin),
            Qt.AspectRatioMode.KeepAspectRatio,
        )
        # fit_* / mirror: transform jumps but the pixmap content is still
        # valid — keep the anchor so the next paint can stretch-blit it
        # while the fresh rebuild happens.
        self._invalidate_bg_cache(keep_anchor=True)
        self._apply_label_visibility()

    def fit_selection(self) -> None:
        """Fit view to the bounding box of selected components and wires."""
        tool = self._active_tool
        sel_comp_ids: set = getattr(tool, "_selected_ids", set())
        sel_wire_ids: set = getattr(tool, "_selected_wire_ids", set())

        if not sel_comp_ids and not sel_wire_ids:
            self.fit_area()
            return

        session = self._session
        xs: list[float] = []
        ys: list[float] = []

        for cid in sel_comp_ids:
            bbox = session.component_bbox(cid)
            if bbox is None:
                continue
            xs += [bbox.x, bbox.x + bbox.width]
            ys += [bbox.y, bbox.y + bbox.height]

        for wid in sel_wire_ids:
            wire = next((w for w in session.layout.wires if w.id == wid), None)
            if wire is None:
                continue
            for pt in wire.path:
                xs.append(pt.x)
                ys.append(pt.y)

        if not xs:
            self.fit_area()
            return

        margin = 1.0
        self.fitInView(
            QRectF(min(xs) - margin, min(ys) - margin,
                   max(xs) - min(xs) + 2 * margin,
                   max(ys) - min(ys) + 2 * margin),
            Qt.AspectRatioMode.KeepAspectRatio,
        )
        # fit_* / mirror: transform jumps but the pixmap content is still
        # valid — keep the anchor so the next paint can stretch-blit it
        # while the fresh rebuild happens.
        self._invalidate_bg_cache(keep_anchor=True)
        self._apply_label_visibility()

    def set_session(self, session: EditorSession) -> None:
        """Replace the current session (used when opening a recent project)."""
        if self._active_tool is not None:
            self._active_tool.deactivate()
            self._active_tool = None
        self._session = session
        self._highlighted_net_id = None
        self._board_layer_dirty = True
        session.layout_changed.connect(self._schedule_refresh)
        session.layout_changed.connect(session.invalidate_graph)
        session.drag_moved.connect(self._on_drag_moved)
        self.refresh()

    def set_tool(self, tool: _Tool | None) -> None:
        # While autoroute is running, mutating tools (Place / Route / Cut)
        # are not allowed — fall back to the SelectTool instance (which is
        # purely read-only).  SelectTool itself, or None, is always allowed.
        if tool is not None and self._session.autoroute_active:
            from protoboard.gui.tools.select_tool import SelectTool as _ST
            if not isinstance(tool, _ST):
                tool = None
        if self._active_tool is not None:
            self._active_tool.deactivate()
        self._active_tool = tool
        self._highlighted_net_id = None
        # Tool change may affect board_alpha (e.g. ghost mode) — rebuild board layer.
        self._board_layer_dirty = True
        if tool is not None:
            tool.activate(self._session, self)
        self.refresh()

    def board_pad_positions(self) -> list[Point]:
        """Cached list of all board pad positions (raster units)."""
        return self._pad_positions

    def _rebuild_pad_cache(self) -> None:
        """Rebuild _pad_positions and _pad_spatial from the current board layout."""
        from collections import defaultdict
        positions: list[Point] = []
        for atom, center in self._session.board.atoms():
            if isinstance(atom, StripAtom):
                n = int(atom.length) if atom.length != "fill" else 0
                if atom.orientation == "horizontal":
                    start_x = center.x - n / 2.0 + 0.5
                    for i in range(n):
                        positions.append(Point(start_x + i, center.y))
                else:
                    start_y = center.y - n / 2.0 + 0.5
                    for i in range(n):
                        positions.append(Point(center.x, start_y + i))
            else:
                from protoboard.domain.board.boxes import GapAtom
                if not isinstance(atom, GapAtom):
                    positions.append(center)
        self._pad_positions = positions
        # Build spatial index: grid cell → pad points (for fast snap).
        spatial: dict[tuple[int, int], list[Point]] = defaultdict(list)
        for p in positions:
            spatial[(int(round(p.x)), int(round(p.y)))].append(p)
        self._pad_spatial = dict(spatial)

    def _schedule_refresh(self) -> None:
        """Schedule a refresh for the next event-loop tick (debounce)."""
        if not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def _fire_on_scene_cleared(self) -> None:
        """Notify tools that the scene was wiped (board layer rebuild)."""
        notified: set[int] = set()
        for tool in self._permanent_tools:
            tool.on_scene_cleared()
            notified.add(id(tool))
        if self._active_tool is not None and id(self._active_tool) not in notified:
            self._active_tool.on_scene_cleared()

    def _invalidate_all_groups(self) -> None:
        """Drop all persistent group caches (called when scene is fully cleared)."""
        self._comp_groups.clear()
        self._comp_fingerprints.clear()
        self._wire_groups.clear()
        self._wire_fingerprints.clear()
        self._overlay_items.clear()

    def refresh(self) -> None:
        """Update the scene to reflect current session state.

        On the warm path (most layout_changed events) only changed items are
        recreated; unchanged component/wire groups survive untouched.
        The board layer (pads/strips) is rebuilt only when its visual inputs change.
        """
        _t0 = time.monotonic()
        self._refresh_timer.stop()
        self._rebuild_pad_cache()

        from dataclasses import replace as _dc_replace
        _base = self._theme_override if self._theme_override is not None else DEFAULT_THEME
        layers = layer_state_for_tool(
            self._active_tool,
            self._camera_side,  # type: ignore[arg-type]
            self._view_transform,  # type: ignore[arg-type]
            overrides=self._layer_overrides,
            theme=_base,
        )
        _theme = _dc_replace(_base,
                             raster_pitch_mm=self._session.board.raster_pitch_mm,
                             body_types=self._session.body_types)

        # ── Board layer ──────────────────────────────────────────────────────
        # Board pads/strips/frames no longer live in the scene — they are
        # rasterised into ``self._bg_cache`` and blitted by
        # ``drawBackground``.  Here we just invalidate the cache when
        # geometry/theme/alpha change and clear any leftover scene items
        # that legacy code may still create.
        current_board_alpha = layers.alpha_for_board()
        if self._board_layer_dirty or current_board_alpha != self._last_board_alpha:
            self._scene.clear()
            self._invalidate_all_groups()
            self._fire_on_scene_cleared()
            # Board state changed — drop both pixmap and display list so
            # the next drawBackground rebuilds from current board state.
            self._bg_display_list = None
            self._invalidate_bg_cache()
            self._board_layer_dirty = False
            self._last_board_alpha = current_board_alpha
        else:
            # Placement-only change (drag-move commit, undo, paste, etc.):
            # don't tear down the scene, but the pad-net colour map depends
            # on which pad each pin lands on — drop the bg display list +
            # pixmap so they rebuild from the current placements.  Cheap
            # detection: compare per-component positions against the snapshot
            # taken at the end of the previous refresh.
            current_positions = {
                cid: (pl.position.x, pl.position.y)
                for cid, pl in self._session.layout.placements.items()
            }
            prev_positions = {
                cid: (p.x, p.y) for cid, p in self._drag_orig_group_pos.items()
            }
            if current_positions != prev_positions:
                self._bg_display_list = None
                self._invalidate_bg_cache()

        # ── Remove stale overlay items (ratsnest + group outlines) ───────────
        for item in self._overlay_items:
            if not isinstance(item, _QGItem):
                continue
            try:
                if item.scene() is self._scene:
                    self._scene.removeItem(item)
            except RuntimeError:
                pass  # C++ object already deleted by scene.clear()
        self._overlay_items.clear()

        # ── Shared render inputs ─────────────────────────────────────────────
        wire_net_map = self._session.cached_wire_net_map()
        # Resolved per-net colour: manual ``net.color`` wins, then static
        # palette, then golden-ratio hue spin once palette is exhausted.
        # Same map drives wire colour AND pad colour so they always match.
        from protoboard.gui.renderers.net_color import build_net_color_map
        net_color_map = build_net_color_map(
            self._session.schematic.nets, theme=_theme,
        )
        routed: set[uuid.UUID] | None = None
        if self._session.schematic.nets:
            routed = routed_net_ids(
                self._session.schematic, self._session.connectivity_graph()
            )

        # ── Wires (group reuse) ───────────────────────────────────────────────
        update_wire_groups(
            self._scene,
            self._session.layout.wires,
            wire_net_map,
            self._wire_groups,        # type: ignore[arg-type]
            self._wire_fingerprints,  # type: ignore[arg-type]
            net_color_map=net_color_map,
            theme=_theme,
            layers=layers,
            highlighted_net_id=self._effective_highlighted_net(),
            routing_target_net_id=self._session.routing_net_id,
        )

        # ── Wire-pin glyphs ───────────────────────────────────────────────────
        # Clear and rebuild the small wire-pin overlay each refresh —
        # the set is typically tiny and fingerprinting would be overkill.
        from protoboard.domain.layout.knot import WirePinKnot
        from protoboard.gui.renderers.wire_renderer import render_wire_pins

        for it in self._wire_pin_items:
            try:
                self._scene.removeItem(it)  # type: ignore[arg-type]
            except Exception:
                pass
        self._wire_pin_items.clear()
        wire_pins: list[tuple[Point, uuid.UUID | None]] = [
            (k.position, k.net_id)
            for k in self._session.layout.knots.values()
            if isinstance(k, WirePinKnot)
        ]
        if wire_pins:
            self._wire_pin_items = list(render_wire_pins(  # type: ignore[arg-type]
                self._scene,
                wire_pins,
                net_color_map=net_color_map,
                theme=_theme,
                layers=layers,
                routing_target_net_id=self._session.routing_net_id,
            ))

        # ── Components (group reuse) ──────────────────────────────────────────
        update_component_groups(
            self._scene,
            self._session.layout.placements,
            self._session.instances,
            self._session.components,
            self._comp_groups,        # type: ignore[arg-type]
            self._comp_fingerprints,  # type: ignore[arg-type]
            schematic=self._session.schematic if self._session.schematic.nets else None,
            routed_nets=routed,
            theme=_theme,
            layers=layers,
            active_group_id=self._active_group_id,
            layout=self._session.layout,
            highlighted_net_id=self._effective_highlighted_net(),
        )

        # Snapshot positions so _on_drag_moved can compute deltas.
        self._drag_orig_group_pos = {
            cid: pl.position
            for cid, pl in self._session.layout.placements.items()
        }
        # Per-wire pose anchor — first point of the wire's path at the time
        # the scene group was built.  Live drag mutates wire.path in place
        # (translation only for group drag, free-form for single-wire drag);
        # _on_drag_moved compares current path[0] against this anchor and
        # calls setPos(delta) so the wire glides with the drag, without
        # rebuilding the scene group on every snap step.
        self._drag_orig_wire_pose = {
            w.id: w.path[0] for w in self._session.layout.wires if w.path
        }

        # ── Pin-pads layer (schematic-aware overlay above board) ─────────────
        # Cherry-on-top of the board layer: every pad carrying a net is
        # repainted in the net colour with the modifier shape (pin1 /
        # exported / star) carved out.  Routing target gets neon yellow.
        for it in self._pin_pad_items:
            try:
                self._scene.removeItem(it)  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001
                pass
        self._pin_pad_items.clear()
        try:
            from protoboard.domain.layout.pin_pad_classification import (
                classify_pin_pads,
            )
            from protoboard.gui.renderers.pin_pad_renderer import (
                render_pin_pads,
            )
            pp_records = classify_pin_pads(
                self._session.pad_index(), self._session.layout,
            )
            self._pin_pad_items = list(render_pin_pads(  # type: ignore[arg-type]
                self._scene,
                pp_records,
                theme=_theme,
                layers=layers,
                net_color_map=net_color_map,
                routing_target_net_id=self._session.routing_net_id,
            ))
        except Exception:  # noqa: BLE001
            # Defensive: never let pin-pads classification glitch crash
            # the whole paint pipeline.  The layer is purely visual.
            self._pin_pad_items = []
            pp_records = []

        # ── Overlays (always recreated — fast, few items) ────────────────────
        new_overlays: list[object] = []
        from protoboard.gui.layer_state import LayerVisibility
        ratsnest_vis = self._layer_overrides.ratsnest
        if (self._session.schematic.nets
                and ratsnest_vis != LayerVisibility.HIDDEN):
            # Ghost mode: only unrouted nets are drawn (current
            # legacy behaviour).  Full mode: every net gets at least
            # its missing-link or multi-island warning lines.
            ghost_mode = (ratsnest_vis == LayerVisibility.GHOST)
            rmode = self._ratsnest_mode
            new_overlays.extend(render_ratsnest(
                self._scene,
                self._session.schematic,
                self._session.layout.placements,
                self._session.instances,
                self._session.components,
                routed_nets=routed,
                theme=_theme,
                graph=self._session.connectivity_graph(),
                mode=rmode,
                ghost=ghost_mode,
            ))
        if self._session.layout.groups:
            new_overlays.extend(render_groups(
                self._scene,
                self._session.layout,
                self._session.layout.placements,
                self._session.instances,
                self._session.components,
                wires=self._session.layout.wires,
                theme=_theme,
                active_group_id=self._active_group_id,
                view_scale=self._scale,
                wire_net_map=self._session.cached_wire_net_map(),
                schematic=self._session.schematic,
            ))
        if self._session.axis_label_settings.any_enabled():
            from protoboard.gui.renderers.axis_labels_renderer import (
                render_axis_labels,
            )
            new_overlays.extend(render_axis_labels(
                self._scene,
                self._session.board,
                self._session.axis_label_settings,
                theme=_theme,
                alpha=layers.alpha_for_axis_labels(),
                view_scale=self._scale,
            ))
        self._overlay_items = new_overlays

        # ── Scene rect ───────────────────────────────────────────────────────
        b = self._session.board.bounds
        mx, my = b.width * 0.5, b.height * 0.5
        self._scene.setSceneRect(QRectF(b.x - mx, b.y - my, b.width + 2 * mx, b.height + 2 * my))

        # ── Label visibility (hide text that would be too small to read) ────────
        self._apply_label_visibility()

        # ── Pin modifiers (top-most overlay) ─────────────────────────────────
        # Repeats pin1 / exported / star glyphs above wires + components in
        # the net colour so the marker remains visible when something
        # covers the underlying pin-pad.
        for it in self._pin_modifier_items:
            try:
                self._scene.removeItem(it)  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001
                pass
        self._pin_modifier_items.clear()
        if pp_records:
            try:
                from protoboard.gui.renderers.pin_pad_renderer import (
                    render_pin_modifiers,
                )
                self._pin_modifier_items = list(render_pin_modifiers(  # type: ignore[arg-type]
                    self._scene,
                    pp_records,
                    theme=_theme,
                    layers=layers,
                    net_color_map=net_color_map,
                    routing_target_net_id=self._session.routing_net_id,
                ))
            except Exception:  # noqa: BLE001
                self._pin_modifier_items = []

        # Scene contents changed — drop the back-side raster cache so the
        # next paint re-rasterises with current items.  Restores any items
        # we'd previously hidden so they're available to be re-rendered.
        self._invalidate_back_cache()

        # ── Final highlight pass ─────────────────────────────────────────────
        # ``update_component_groups`` no longer applies net-highlight dimming
        # (it would clobber the route-active profile during a wire drag).
        # The canonical highlight pass therefore has to run after every
        # refresh, otherwise Net Inspector / Selection highlights would be
        # lost the moment any unrelated refresh fires (drag, undo, theme
        # change, etc.).
        self._apply_highlight_opacity()

        _ms = (time.monotonic() - _t0) * 1000
        self._session.perf.record_refresh(_ms)
        self._session.perf.scene_items = len(self._scene.items())

    def select_component(self, comp_id: uuid.UUID) -> None:
        """Switch to SelectTool and select the given component."""
        from protoboard.gui.tools.select_tool import SelectTool

        if not isinstance(self._active_tool, SelectTool):
            self.set_tool(SelectTool())
        tool = self._active_tool
        if isinstance(tool, SelectTool):
            tool._select_primary(comp_id)

    def snap(self, scene_pt: QPointF, exclude_ids: frozenset[uuid.UUID] | None = None) -> Point:
        """Snap scene-space point to nearest accessible pad or component pin.

        Pins belonging to child groups are only accessible if they are exported.
        ``exclude_ids`` — component IDs whose pins are skipped (e.g. currently dragged).
        """
        from protoboard.domain.layout.group import all_component_ids
        from protoboard.domain.layout.group import ExportedPin
        from protoboard.domain.layout.layout import ROOT_GROUP_ID

        # Build set of component IDs that are locked inside child groups,
        # and their exported pins.
        groups = self._session.layout.groups
        active_group = groups.get(self._active_group_id)
        locked_comp_ids: set[uuid.UUID] = set()
        exported_pins: set[tuple[uuid.UUID, int]] = set()
        if active_group is not None:
            for child_gid in active_group.child_group_ids:
                child_group = groups.get(child_gid)
                if child_group is None:
                    continue
                for cid in all_component_ids(groups, child_gid):
                    locked_comp_ids.add(cid)
                for ep in child_group.exported_pins:
                    exported_pins.add((ep.component_id, ep.pin_number))

        sx, sy = scene_pt.x(), scene_pt.y()
        best: Point | None = None
        best_d = math.inf

        def _check(px: float, py: float) -> None:
            nonlocal best, best_d
            d = (sx - px) * (sx - px) + (sy - py) * (sy - py)
            if d < best_d:
                best_d = d
                best = Point(px, py)

        # Board pads — use spatial index (3×3 cells around cursor).
        # Ensure index is built even if refresh() hasn't been called yet.
        if not self._pad_spatial and self._pad_positions:
            from collections import defaultdict
            _sp: dict[tuple[int, int], list[Point]] = defaultdict(list)
            for _p in self._pad_positions:
                _sp[(int(round(_p.x)), int(round(_p.y)))].append(_p)
            self._pad_spatial = dict(_sp)
        elif not self._pad_spatial:
            self._rebuild_pad_cache()
        cx, cy = int(round(sx)), int(round(sy))
        for dcx in (-1, 0, 1):
            for dcy in (-1, 0, 1):
                for p in self._pad_spatial.get((cx + dcx, cy + dcy), []):
                    _check(p.x, p.y)

        # Component pin positions — use comp spatial index; skip locked pins.
        candidates: set[uuid.UUID] = set()
        comp_idx = self._session.comp_spatial_index()
        for dcx in (-1, 0, 1):
            for dcy in (-1, 0, 1):
                for cid in comp_idx.get((cx + dcx, cy + dcy), []):
                    candidates.add(cid)

        for comp_id in candidates:
            if exclude_ids and comp_id in exclude_ids:
                continue
            if comp_id in locked_comp_ids:
                for pin_num, p in self._session.iter_pin_positions(comp_id):
                    if (comp_id, pin_num) not in exported_pins:
                        continue
                    _check(p.x, p.y)
            else:
                for _pin_num, p in self._session.iter_pin_positions(comp_id):
                    _check(p.x, p.y)

        if best is not None:
            return best
        # No real pad or pin within the 3×3 search window — return the
        # nearest hypothetical pad-grid centre so off-board cursors still
        # snap to a half-integer grid.  Prevents drag-then-return from
        # leaving components at fractional positions.
        return Point(math.floor(sx) + 0.5, math.floor(sy) + 0.5)

    def is_locked_pin(self, scene_pt: QPointF, tol: float = 0.6) -> bool:
        """Return True if *scene_pt* is near a pin locked inside a child group
        (i.e. not exported). Used by RouteTool to show a status message."""
        from protoboard.domain.layout.group import all_component_ids
        groups = self._session.layout.groups
        active_group = groups.get(self._active_group_id)
        if active_group is None:
            return False
        locked_comp_ids: set[uuid.UUID] = set()
        exported_pins: set[tuple[uuid.UUID, int]] = set()
        for child_gid in active_group.child_group_ids:
            child_group = groups.get(child_gid)
            if child_group is None:
                continue
            for cid in all_component_ids(groups, child_gid):
                locked_comp_ids.add(cid)
            for ep in child_group.exported_pins:
                exported_pins.add((ep.component_id, ep.pin_number))
        if not locked_comp_ids:
            return False
        for comp_id in locked_comp_ids:
            for pin_num, p in self._session.iter_pin_positions(comp_id):
                if (comp_id, pin_num) in exported_pins:
                    continue
                if (scene_pt.x() - p.x) ** 2 + (scene_pt.y() - p.y) ** 2 < tol ** 2:
                    return True
        return False

    def center_on_scene_point(self, scene_pt: QPointF) -> None:
        """Scroll the view so that scene_pt appears at the centre of the viewport."""
        vp = self.viewport()
        cx = vp.width() / 2.0
        cy = vp.height() / 2.0
        current = self.mapToScene(int(cx), int(cy))
        delta_scene = scene_pt - current
        scale = self.transform().m11()
        hbar = self.horizontalScrollBar()
        vbar = self.verticalScrollBar()
        hbar.setValue(hbar.value() + round(delta_scene.x() * scale))
        vbar.setValue(vbar.value() + round(delta_scene.y() * scale))

    def _show_canvas_menu(self, vp_pos: QPoint, scene_pt: QPointF) -> None:
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        act_center = menu.addAction("Center view here")
        act_escape = menu.addAction("Cancel tool (Esc)")
        chosen = menu.exec(self.mapToGlobal(vp_pos))
        if chosen is act_center:
            self.center_on_scene_point(scene_pt)
        elif chosen is act_escape:
            self.escape_tool.emit()

    # ------------------------------------------------------------------
    # Qt event handlers
    # ------------------------------------------------------------------

    def wheelEvent(self, event: QWheelEvent) -> None:
        """Accumulate wheel ticks; defer the scale + scroll to a timer.

        Each ``self.scale()`` triggers a QGraphicsScene repaint, and at
        Qt's default ``SmartViewportUpdate`` the next ``wheelEvent`` is
        not delivered until that paint completes — over SSH that means
        a burst of fast wheel ticks gets queued behind hundreds of ms
        of X11 round-trips.  We park the *raw* angleDelta into a buffer
        and let a short single-shot timer fold the burst into one
        geometric pass.  Qt's wheelEvent handler returns in microseconds.

        Cursor pivot uses the *latest* event's position: a user spinning
        the wheel intends to zoom around wherever the cursor currently
        is, not the position where the burst started.
        """
        self._wheel_accum_delta += event.angleDelta().y()
        self._wheel_pivot_vp = event.position().toPoint()
        if not self._wheel_timer.isActive():
            self._wheel_timer.start()

    def _flush_wheel_zoom(self) -> None:
        """Apply the accumulated wheel-zoom delta in one pass.

        Translates the buffered angleDelta into a number of asymmetric
        zoom steps (1.1 in, 1.0/1.08 out — preserved from the user's
        tuned values; see memory ``feedback_zoom_factor``) and applies
        the net factor as a single ``self.scale(...)``.  Cursor-pivot
        correction is identical to the legacy code path; the only
        difference is that *N ticks* of wheel input cost one paint
        instead of N.
        """
        delta = self._wheel_accum_delta
        pivot = self._wheel_pivot_vp
        self._wheel_accum_delta = 0
        self._wheel_pivot_vp = None
        if delta == 0 or pivot is None:
            return
        # Qt reports angleDelta in 1/8-degree units; a standard mouse
        # tick is 15° = 120.  Use the sign-counted tick count so the
        # geometric factors stay exactly the same as the per-event
        # path (1.1 in, 1.0/1.08 out) — burst of 3 ticks zooms in by
        # 1.1^3 ≈ 1.331, identical to three separate calls.
        ticks = delta // 120 if delta > 0 else -((-delta) // 120)
        # Sub-tick fractional residue is rare on standard mice but
        # common on high-resolution scroll wheels; treat any positive
        # remainder as a partial in-tick to avoid losing input.
        if ticks == 0:
            ticks = 1 if delta > 0 else -1
        if ticks > 0:
            factor = 1.1 ** ticks
        else:
            factor = (1.0 / 1.08) ** (-ticks)
        factor, hit_floor = self._clamp_zoom_factor(factor)
        if abs(factor - 1.0) < 1e-6 and not hit_floor:
            return  # at limit — no scale change, no scroll-bar correction needed

        old_scene = self.mapToScene(pivot)
        self.scale(factor, factor)
        if hit_floor:
            # Snap to the board's centre on the zoom-out floor so the
            # user doesn't end up parked over canvas margin.
            b = self._session.board.bounds
            self.centerOn(b.x + b.width / 2.0, b.y + b.height / 2.0)
        else:
            new_scene = self.mapToScene(pivot)
            delta_scene = new_scene - old_scene
            new_scale = self.transform().m11()
            hbar = self.horizontalScrollBar()
            vbar = self.verticalScrollBar()
            hbar.setValue(hbar.value() - round(delta_scene.x() * new_scale))
            vbar.setValue(vbar.value() - round(delta_scene.y() * new_scale))
        # Zoom changed the world→viewport transform; the cached pixmap
        # was rendered for the previous mapping.  Keep the anchor so a
        # follow-up tick of the same burst can stretch-blit it instead
        # of waiting for the new full-quality rebuild.
        self._invalidate_bg_cache(keep_anchor=True)
        # Defer the O(scene items) visibility sweep — same reasoning
        # as before, just relative to the (now batched) zoom paint.
        if not self._label_vis_timer.isActive():
            self._label_vis_timer.start()

    # ------------------------------------------------------------------
    # Zoom limits — derived from physical DPI + board bounds
    # ------------------------------------------------------------------
    #
    # Two anchors:
    #   * MAX (zoom in cap): 1 raster cell on screen ≈ 10 × cell-in-real-mm.
    #     i.e. 10 mm of board occupies ~100 mm of screen.  Equivalent to
    #     ``m11 == 10 × (px/mm) × pitch_mm`` which simplifies to
    #     ``physicalDpi`` (since pitch_mm == 2.54 and 1 inch == 25.4 mm).
    #     For a non-2.54 pitch it scales accordingly.
    #
    #   * MIN (zoom out cap): the LATER of two conditions:
    #       (a) board's long edge spans half the viewport's long edge
    #           (so the user never zooms out far enough to lose the board
    #           in canvas chrome);
    #       (b) board renders at real physical size (1:1 mm-on-screen ==
    #           mm-on-board) — relevant for tiny boards on big monitors.
    #     "Later" == whichever gives a LARGER m11 (kicks in first while
    #     zooming out).
    #
    # Limits are cached and recomputed on resizeEvent / board change.

    def _compute_zoom_limits(self) -> tuple[float, float]:
        """Return ``(m11_min, m11_max)`` for the current viewport + board."""
        screen = self.screen() or (self.window().screen() if self.window() else None)
        # ``physicalDotsPerInch`` reports the *true* hardware DPI Qt can
        # see; falls back to logicalDpi if unavailable (e.g. headless).
        if screen is not None:
            phys_dpi = float(screen.physicalDotsPerInch() or 96.0)
        else:
            phys_dpi = 96.0
        pitch_mm = float(self._session.board.raster_pitch_mm)
        px_per_mm = phys_dpi / 25.4
        # m11 such that 1 cell == pitch_mm × px_per_mm (real size).
        m11_real = px_per_mm * pitch_mm
        # Cap at 10× real size for zoom in.
        m11_max = 10.0 * m11_real

        # Half-viewport-fit cap: at this scale ``fitInView(board)`` would
        # show the board centred and filling the viewport exactly along
        # the *more constrained* axis (width or height, whichever runs
        # out first).  Use HALF of that — i.e. board fills 50 % of the
        # visible window — as the zoom-out floor.  Computing it as
        # ``min(vp_w/board_w, vp_h/board_h)`` matches Qt's own KeepAspectRatio
        # logic, so the floor and ``fit_board()`` agree visually.
        b = self._session.board.bounds
        board_w = max(float(b.width), 1.0)
        board_h = max(float(b.height), 1.0)
        vp = self.viewport().rect()
        vp_w = max(vp.width(), 1)
        vp_h = max(vp.height(), 1)
        m11_fit = min(vp_w / board_w, vp_h / board_h)
        m11_half = 0.5 * m11_fit

        # Both conditions must HOLD for zoom-out to continue → floor is
        # the EARLIER kicker = the one with the lower m11 (more zoom-out
        # allowed).  Concretely: stop zoom-out as soon as either
        # "half viewport" OR "real size" is reached.
        #   Big board on small window  → m11_half kicks in first (small).
        #   Small board on big window  → m11_real kicks in first (small).
        m11_min = min(m11_half, m11_real)
        # Safety: never let min exceed max (degenerate small viewport).
        if m11_min > m11_max:
            m11_min = m11_max
        return m11_min, m11_max

    def _clamp_zoom_factor(self, factor: float) -> tuple[float, bool]:
        """Cap *factor* so applying it keeps m11 inside the limit window.

        Returns ``(clamped_factor, hit_floor)`` where *hit_floor* is True
        when the user pushed zoom-out into (or already past) the minimum
        scale.  Callers use that to auto-centre the board so the viewport
        doesn't end up parked over the canvas-margin area.
        """
        if factor <= 0.0:
            return 1.0, False
        current = abs(self.transform().m11()) or _GRID_PX
        target = current * factor
        m11_min, m11_max = self._compute_zoom_limits()
        hit_floor = False
        if target > m11_max:
            target = m11_max
        elif target < m11_min:
            target = m11_min
            hit_floor = True
        if current <= 0.0:
            return 1.0, hit_floor
        return target / current, hit_floor

    def zoom_by(self, factor: float) -> None:
        """Scale the view by *factor*, keeping the viewport centre fixed.

        Used by keyboard / toolbar zoom actions (Ctrl++ / Ctrl+-).  Plain
        ``self.scale(...)`` zooms around scene origin (0,0) because the
        view's TransformationAnchor is ``NoAnchor`` — for wheel zoom we
        compensate around the cursor; here we compensate around the
        viewport centre so the user keeps looking at the same spot on the
        board.  Also invalidates the rasterised background which would
        otherwise stay frozen at the previous zoom level while scene
        items rescale.
        """
        if factor <= 0.0:
            return
        factor, hit_floor = self._clamp_zoom_factor(factor)
        if abs(factor - 1.0) < 1e-6 and not hit_floor:
            return  # already at limit — no-op
        vp = self.viewport().rect()
        pivot_vp = vp.center()
        old_scene = self.mapToScene(pivot_vp)
        self.scale(factor, factor)
        if hit_floor:
            # Snap to board centre when hitting the zoom-out floor.
            b = self._session.board.bounds
            self.centerOn(b.x + b.width / 2.0, b.y + b.height / 2.0)
        else:
            new_scene = self.mapToScene(pivot_vp)
            delta_scene = new_scene - old_scene
            new_scale = self.transform().m11()
            hbar = self.horizontalScrollBar()
            vbar = self.verticalScrollBar()
            hbar.setValue(hbar.value() - round(delta_scene.x() * new_scale))
            vbar.setValue(vbar.value() - round(delta_scene.y() * new_scale))
        # Ctrl+/- zoom: keep the anchor so successive presses paint
        # cheaply during the burst, then settle to a fresh pixmap.
        self._invalidate_bg_cache(keep_anchor=True)
        if not self._label_vis_timer.isActive():
            self._label_vis_timer.start()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        left = Qt.MouseButton.LeftButton
        if event.button() == left and self._active_tool is not None:
            scene_pt = self.mapToScene(event.pos())
            handler = getattr(self._active_tool, "on_double_press", None)
            if handler is not None:
                handler(scene_pt)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        _t0 = time.monotonic() if _CLICK_PROFILE else 0.0
        try:
            self._mousePressEvent_impl(event)
        finally:
            if _CLICK_PROFILE:
                self._click_press_ts = time.monotonic()
                _click_log.info(
                    "click_profile.boardview.mousePressEvent",
                    ms=round((time.monotonic() - _t0) * 1000, 1),
                )

    def paintEvent(self, event: object) -> None:  # type: ignore[override]
        # Always measure paintEvent wall time — PerformancePanel reads it
        # via session.perf.paint_ms.  The click-profile log is gated on
        # the env var so the always-on cost is one ``monotonic()`` call.
        _t0 = time.monotonic()
        # ``super().paintEvent`` covers BOTH the drawBackground call (which
        # we already time per-phase below) AND Qt's own scene paint pass
        # over all visible front-pass scene items.  We slice the two by
        # remembering how long drawBackground took: paintEvent_total minus
        # drawBackground_total = scene front-pass time.
        self._paint_db_ms = 0.0  # filled by drawBackground
        self._paint_counters["paintEvent"] += 1
        super().paintEvent(event)  # type: ignore[arg-type]
        paint_ms = (time.monotonic() - _t0) * 1000.0
        scene_pass_ms = max(0.0, paint_ms - self._paint_db_ms)
        # Persist total paint wall time on the perf metrics WITHOUT
        # touching the per-layer fields drawBackground already filled in.
        self._session.perf.paint_ms = paint_ms
        if _PAINT_PROFILE:
            _paint_log.info(
                "paint_profile.boardview.paintEvent",
                total_ms=round(paint_ms, 2),
                drawbg_ms=round(self._paint_db_ms, 2),
                scene_pass_ms=round(scene_pass_ms, 2),
                bg_build_ms=round(self._session.perf.bg_build_ms, 2),
                back_build_ms=round(self._session.perf.back_build_ms, 2),
                blit_ms=round(self._session.perf.blit_ms, 2),
            )
        if _CLICK_PROFILE:
            press_ts = getattr(self, "_click_press_ts", None)
            if press_ts is not None and (time.monotonic() - press_ts) < 1.0:
                # Only log paints temporally close to a click — otherwise
                # every viewport update spams the console.
                since_press_ms = round((time.monotonic() - press_ts) * 1000, 1)
                _click_log.info(
                    "click_profile.boardview.paintEvent",
                    paint_ms=round(paint_ms, 1),
                    since_press_ms=since_press_ms,
                )

    # ------------------------------------------------------------------
    # Background cache (board layer)
    # ------------------------------------------------------------------

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:  # type: ignore[override]
        """Paint the layered background — board sandwich + back-side cache.

        Composition order (bottom → top):

        1. ``backgroundBrush`` — fill viewport.
        2. ``_bg_cache`` (board base) — full board at ``alpha_for_board()``.
        3. ``_back_cache`` — rasterised back-side scene items.  While the
           cache is valid the corresponding scene items are hidden so Qt
           does not paint them a second time on top of the board mid.
        4. ``_bg_cache`` (board mid) — same pixmap as board base, blitted
           again at ``alpha_for_board_mid()`` so front-side items visually
           sit on the pads while back items underneath remain readable.

        Front-side scene items continue through Qt's scene paint pass on
        top of this stack.

        Fills the background brush first so transparent regions of the
        board cache (zoomed-out view) show the canvas colour.
        """
        _db_t0 = time.monotonic()
        self._paint_counters["drawBackground"] += 1
        _fr_t0 = time.monotonic()
        painter.save()
        painter.resetTransform()
        painter.fillRect(self.viewport().rect(), self.backgroundBrush())
        painter.restore()
        self._paint_counters["fillRect"] += 1
        _fill_ms = (time.monotonic() - _fr_t0) * 1000.0

        # Per-layer timings — surface in PerformancePanel.  Build times
        # come from the _ensure_* helpers (0.0 when cache was fresh),
        # blit_ms wraps the actual drawPixmap calls.
        bg_build_ms = 0.0
        back_build_ms = 0.0
        _blit_t0 = time.monotonic()

        in_burst = self._wheel_timer.isActive() or self._pan_burst_timer.isActive()
        if in_burst and self._bg_cache_anchor is not None:
            # Cheap preview: blit the previous full-quality pixmap under
            # the inverse transform delta.  Qt handles the scale/translate
            # in the GPU/blit stage so the pan/zoom feels instant.  We
            # explicitly do NOT call ``_ensure_bg_cache`` here — that would
            # negate the whole point of the fast preview.
            from PySide6.QtGui import QTransform
            anchor_t: QTransform = self._bg_cache_anchor_transform  # type: ignore[assignment]
            cur_t = self.viewportTransform()
            # World-space coords mapped by current transform must land where
            # anchor-rendered pixels were placed; the bridging transform is
            # ``cur_t × anchor_t.inverted()`` applied in device coords.
            # PySide6 returns (QTransform, bool) — opposite to the Qt
            # C++ docs (which read like ``bool inverted(*ok)``).  Unpack
            # accordingly.
            anchor_inv, inv_ok = anchor_t.inverted()
            if inv_ok:
                bridge = anchor_inv * cur_t
                painter.save()
                painter.setTransform(bridge)
                painter.drawPixmap(0, 0, self._bg_cache_anchor)  # type: ignore[arg-type]
                self._paint_counters["anchor_blit"] += 1
                # The back-side cache was rasterised under the SAME
                # anchor transform as the board pixmap, so the same
                # bridge maps it correctly — without this the back-side
                # items (back wires + group outlines, both zValue<Z_BOARD)
                # appear at the old transform while the rest of the
                # scene rescales, which looks like they're "drawn late".
                if self._back_cache is not None:
                    painter.setOpacity(1.0)
                    painter.drawPixmap(0, 0, self._back_cache)  # type: ignore[arg-type]
                    self._paint_counters["anchor_blit_back"] += 1
                painter.restore()
                # Back-side filter + mid pad blit — kept in the burst
                # branch too so the picture stays consistent during pan
                # / zoom.  Without this the filter would "blink off" and
                # the mid pad blit would skip every burst frame, making
                # the board look noticeably brighter / lower-contrast
                # whenever the burst timer is active.
                burst_layers = layer_state_for_tool(
                    self._active_tool,
                    self._camera_side,  # type: ignore[arg-type]
                    self._view_transform,  # type: ignore[arg-type]
                    overrides=self._layer_overrides,
                    theme=self._theme_override or DEFAULT_THEME,
                )
                burst_filter_alpha = burst_layers.alpha_for_back_filter()
                burst_alpha_mid = burst_layers.alpha_for_board_mid()
                if burst_filter_alpha > 0.0:
                    from PySide6.QtCore import QRectF as _QRectF
                    from PySide6.QtGui import QColor as _QColor
                    theme_for_filter = self._theme_override or DEFAULT_THEME
                    filter_color = _QColor(theme_for_filter.back_filter_color)
                    filter_color.setAlphaF(burst_filter_alpha)
                    painter.setOpacity(1.0)
                    b = self._session.board.bounds
                    board_scene_rect = _QRectF(b.x, b.y, b.width, b.height)
                    board_device_rect = self.mapFromScene(
                        board_scene_rect
                    ).boundingRect()
                    painter.fillRect(board_device_rect, filter_color)
                    self._paint_counters["blit_back_filter"] += 1
                # Mid blit — re-paint the anchor pixmap through the same
                # bridge so pads sit on top of the filter, matching the
                # non-burst stack ordering.
                if burst_alpha_mid > 0.0:
                    painter.save()
                    painter.setTransform(bridge)
                    painter.setOpacity(burst_alpha_mid)
                    painter.drawPixmap(0, 0, self._bg_cache_anchor)  # type: ignore[arg-type]
                    self._paint_counters["blit_mid"] += 1
                    painter.restore()
                painter.setOpacity(1.0)
            self._session.perf.record_paint(
                bg_build_ms=0.0,
                back_build_ms=0.0,
                blit_ms=(time.monotonic() - _blit_t0) * 1000.0,
                paint_ms=0.0,  # paintEvent overwrites with total
            )
            self._paint_db_ms = (time.monotonic() - _db_t0) * 1000.0
            self._paint_counters["drawBackground_burst"] += 1
            if _PAINT_PROFILE:
                _paint_log.info(
                    "paint_profile.drawBackground.burst",
                    db_total_ms=round(self._paint_db_ms, 2),
                    fill_ms=round(_fill_ms, 2),
                    counters=dict(self._paint_counters),
                )
            return

        bg_build_ms = self._ensure_bg_cache()
        back_build_ms = self._ensure_back_cache()
        # Reset blit timer so the per-layer build cost above doesn't
        # leak into the "blit only" metric reported below.
        _blit_t0 = time.monotonic()

        # Snapshot the freshly-rebuilt pixmap + transform as the anchor
        # for the next burst.  Cheap — we're already holding both refs.
        if self._bg_cache is not None:
            self._bg_cache_anchor = self._bg_cache
            self._bg_cache_anchor_transform = self.viewportTransform()

        # Resolve current layer alphas — single LayerState round-trip per paint.
        layers = layer_state_for_tool(
            self._active_tool,
            self._camera_side,  # type: ignore[arg-type]
            self._view_transform,  # type: ignore[arg-type]
            overrides=self._layer_overrides,
            theme=self._theme_override or DEFAULT_THEME,
        )
        alpha_base = layers.alpha_for_board()
        alpha_mid = layers.alpha_for_board_mid()
        alpha_back_filter = layers.alpha_for_back_filter()

        # Pixmaps are sized to and aligned with the viewport (device coords).
        painter.save()
        painter.resetTransform()
        _blit_base_ms = 0.0
        _blit_back_ms = 0.0
        _blit_mid_ms = 0.0
        if self._bg_cache is not None and alpha_base > 0.0:
            _bt = time.monotonic()
            painter.setOpacity(alpha_base)
            painter.drawPixmap(0, 0, self._bg_cache)  # type: ignore[arg-type]
            self._paint_counters["blit_base"] += 1
            _blit_base_ms = (time.monotonic() - _bt) * 1000.0
        if self._back_cache is not None:
            _bt = time.monotonic()
            painter.setOpacity(1.0)
            painter.drawPixmap(0, 0, self._back_cache)  # type: ignore[arg-type]
            self._paint_counters["blit_back"] += 1
            _blit_back_ms = (time.monotonic() - _bt) * 1000.0
        # Back-side filter — half-transparent black wash painted between
        # back-side items and the mid board blit.  Sits over back wires /
        # back components but under the mid pad blit, so pads in FULL
        # mode stay readable while back-side items get visually pushed
        # away.  Restricted to the board rectangle so the surrounding
        # canvas (axis labels, panning margin) keeps the base brightness.
        if alpha_back_filter > 0.0 and self._bg_cache is not None:
            from PySide6.QtCore import QRectF as _QRectF
            from PySide6.QtGui import QColor as _QColor
            theme_for_filter = self._theme_override or DEFAULT_THEME
            filter_color = _QColor(theme_for_filter.back_filter_color)
            filter_color.setAlphaF(alpha_back_filter)
            painter.setOpacity(1.0)
            b = self._session.board.bounds
            board_scene_rect = _QRectF(b.x, b.y, b.width, b.height)
            # Painter is in device (viewport) coords after resetTransform;
            # project the world-space board rect through the current
            # viewport transform to land in the same pixel space.
            board_device_rect = self.mapFromScene(
                board_scene_rect
            ).boundingRect()
            painter.fillRect(board_device_rect, filter_color)
            self._paint_counters["blit_back_filter"] += 1
        if self._bg_cache is not None and alpha_mid > 0.0:
            _bt = time.monotonic()
            painter.setOpacity(alpha_mid)
            painter.drawPixmap(0, 0, self._bg_cache)  # type: ignore[arg-type]
            self._paint_counters["blit_mid"] += 1
            _blit_mid_ms = (time.monotonic() - _bt) * 1000.0
        painter.setOpacity(1.0)
        painter.restore()

        blit_total_ms = (time.monotonic() - _blit_t0) * 1000.0
        self._session.perf.record_paint(
            bg_build_ms=bg_build_ms,
            back_build_ms=back_build_ms,
            blit_ms=blit_total_ms,
            paint_ms=0.0,  # paintEvent overwrites with total
        )
        self._paint_db_ms = (time.monotonic() - _db_t0) * 1000.0
        if _PAINT_PROFILE:
            _paint_log.info(
                "paint_profile.drawBackground",
                db_total_ms=round(self._paint_db_ms, 2),
                fill_ms=round(_fill_ms, 2),
                bg_build_ms=round(bg_build_ms, 2),
                back_build_ms=round(back_build_ms, 2),
                blit_total_ms=round(blit_total_ms, 2),
                blit_base_ms=round(_blit_base_ms, 2),
                blit_back_ms=round(_blit_back_ms, 2),
                blit_mid_ms=round(_blit_mid_ms, 2),
                alpha_base=round(alpha_base, 2),
                alpha_mid=round(alpha_mid, 2),
                vp_w=self.viewport().width(),
                vp_h=self.viewport().height(),
                counters=dict(self._paint_counters),
            )

    def _on_pan_settled(self) -> None:
        """Force a paint right after a pan burst ends so the user sees the
        fresh full-quality pixmap immediately instead of waiting for the
        next event-driven repaint."""
        self.viewport().update()

    def _invalidate_bg_cache(self, *, keep_anchor: bool = False) -> None:
        """Mark the background cache stale; next paint will rebuild it.

        *keep_anchor* preserves ``_bg_cache_anchor`` (the previous fresh
        pixmap + its viewport transform) so a stale-blit during the
        wheel/pan burst can still show something useful.  Set False for
        invalidations where the anchor itself is wrong — theme change,
        camera-side flip, layer overrides that alter the pixmap content,
        board geometry edits.  Set True for pan/zoom/resize where the
        anchor pixmap is still a valid (just mis-scaled) snapshot.
        """
        self._bg_cache_dirty = True
        # Back cache shares viewport transform with board cache — any
        # invalidation that affects board cache also affects back cache.
        self._back_cache_dirty = True
        if not keep_anchor:
            self._bg_cache_anchor = None
            self._bg_cache_anchor_transform = None

    def _invalidate_back_cache(self) -> None:
        """Mark only the back-side cache stale."""
        self._back_cache_dirty = True

    def _ensure_bg_cache(self) -> float:
        """Rebuild the cached pixmap if dirty or viewport-resized.

        The pixmap is sized to the current viewport in device pixels
        and rendered by walking the board display list through a
        ``QPainter`` whose transform matches the view's world→viewport
        mapping.  A viewport resize or any board-state change marks the
        cache dirty.

        Returns the build wall time in milliseconds (0.0 when the cache
        was already fresh) so the paint pipeline can surface it in the
        PerformancePanel.
        """
        if self._session is None:
            return 0.0
        vp_size = self.viewport().size()
        if vp_size.width() <= 0 or vp_size.height() <= 0:
            return 0.0

        need_rebuild = (
            self._bg_cache_dirty
            or self._bg_cache is None
            or (isinstance(self._bg_cache, QPixmap)
                and self._bg_cache.size() != vp_size)
        )
        if not need_rebuild:
            return 0.0
        self._paint_counters["bg_cache_rebuild"] += 1
        _t0 = time.monotonic()

        # Build display list if missing (board / theme / alpha changes
        # clear _bg_display_list; pure viewport resize does not).
        if self._bg_display_list is None:
            from protoboard.gui.layer_state import LayerState as _LayerState
            from protoboard.gui.renderers.board_renderer import build_pad_net_map
            _base = self._theme_override if self._theme_override is not None else DEFAULT_THEME
            from dataclasses import replace as _dc_replace
            layers = layer_state_for_tool(
                self._active_tool,
                self._camera_side,  # type: ignore[arg-type]
                self._view_transform,  # type: ignore[arg-type]
                overrides=self._layer_overrides,
                theme=_base,
            )
            _theme = _dc_replace(_base,
                                 raster_pitch_mm=self._session.board.raster_pitch_mm,
                                 body_types=self._session.body_types)
            # Reuse the same resolved colour map the wire renderer uses,
            # so a wire and the pad it lands on share an identical colour.
            from protoboard.gui.renderers.net_color import build_net_color_map
            pad_color_map = build_net_color_map(
                self._session.schematic.nets, theme=_theme,
            )
            pad_net_map = build_pad_net_map(
                self._session.layout.placements,
                self._session.instances,
                self._session.components,
                self._session.schematic,
                theme=_theme,
                net_color_map=pad_color_map,
            )
            self._bg_display_list = build_board_display_list(
                self._session.board, theme=_theme, layers=layers,
                pad_net_map=pad_net_map,
            )

        # Allocate a fresh pixmap matching the viewport device size.
        pix = QPixmap(vp_size)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, self._antialiasing)
            # Apply the same world→viewport transform that QGraphicsView
            # uses for scene items so display-list coordinates land in
            # the right place on the pixmap.
            painter.setTransform(self.viewportTransform())
            paint_board(painter, self._bg_display_list)  # type: ignore[arg-type]
        finally:
            painter.end()
        self._bg_cache = pix
        self._bg_cache_dirty = False
        return (time.monotonic() - _t0) * 1000.0

    def _ensure_back_cache(self) -> float:
        """Rebuild the back-side cache pixmap if dirty.

        Walks scene items with ``zValue`` in the back band (0 ≤ z < Z_BOARD),
        hides them, renders the scene to a pixmap, then leaves them hidden
        so Qt's own scene paint pass doesn't draw them a second time on top
        of the board mid blit.  The list of hidden items is kept on
        ``self._back_hidden_items`` so the next invalidation can restore
        their visibility before rebuilding (their content may have changed).

        Returns build wall time in milliseconds (0.0 when cache was fresh
        or there were no back items to draw).
        """
        from protoboard.gui.layer_state import Z_BOARD
        if self._session is None:
            return 0.0
        vp_size = self.viewport().size()
        if vp_size.width() <= 0 or vp_size.height() <= 0:
            return 0.0

        need_rebuild = (
            self._back_cache_dirty
            or self._back_cache is None
            or (isinstance(self._back_cache, QPixmap)
                and self._back_cache.size() != vp_size)
        )
        if not need_rebuild:
            return 0.0
        self._paint_counters["back_cache_rebuild"] += 1
        _t0 = time.monotonic()

        # Restore previously-hidden items so a rebuild on changed content
        # picks up the new state.
        for it in getattr(self, "_back_hidden_items", []):
            try:
                it.setVisible(True)
            except RuntimeError:
                pass  # underlying C++ object gone
        self._back_hidden_items = []

        # Collect back-side items in scene Z order.  Skip child items (those
        # whose top-level parent group has a different effective z) — only
        # the parent's z determines stacking, and selecting orphaned
        # children would bake them at default z=0 even when their parent
        # sits well above Z_BOARD (e.g. PlaceTool ghost at z=20).
        back_items = [
            it for it in self._scene.items()
            if 0.0 <= it.zValue() < Z_BOARD - 1e-6
            and it.parentItem() is None
            # Group outlines stay as live scene items — hit-testing
            # (_hit_test_group_outline → scene.items(scene_pt)) skips
            # invisible items, so baking them into the back pixmap would
            # break group selection.
            and (it.data(1) if hasattr(it, "data") else None) != "group_outline"
        ]
        if not back_items:
            self._back_cache = None
            self._back_cache_dirty = False
            self._paint_counters["back_cache_empty"] += 1
            return (time.monotonic() - _t0) * 1000.0

        pix = QPixmap(vp_size)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        # Hide front + overlay items during the render pass.  Easiest
        # approach: temporarily flip visibility on EVERYTHING that is NOT
        # in back_items, render, then restore.  Using setVisible on
        # currently-invisible items is cheap (just a flag flip).
        back_set = set(map(id, back_items))

        def _has_back_ancestor(it: object) -> bool:
            # Walk up until a top-level item; back-cache should render the
            # whole subtree of any back-side group (e.g. a wire's outline +
            # core children), so children of back items must stay VISIBLE.
            parent = it.parentItem() if hasattr(it, "parentItem") else None
            while parent is not None:
                if id(parent) in back_set:
                    return True
                parent = parent.parentItem()
            return False

        temporarily_hidden: list[object] = []
        try:
            for it in self._scene.items():
                if id(it) in back_set:
                    continue
                if not it.isVisible():
                    continue
                if _has_back_ancestor(it):
                    # Child of a back-side group — keep visible so it paints
                    # into the back pixmap through its parent's paint pass.
                    continue
                it.setVisible(False)
                temporarily_hidden.append(it)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, self._antialiasing)
            # Source rect = current visible scene area in scene coords.
            source = self.mapToScene(self.viewport().rect()).boundingRect()
            vp_w = float(vp_size.width())
            vp_h = float(vp_size.height())
            # ``scene.render(painter, target, source)`` maps source→target
            # as a positive-scale affine — it cannot reproduce the
            # mirror_h / mirror_v sign that ``viewportTransform`` carries
            # for the front paint pass.  Flipping the target rect itself
            # achieves the mirror: an inverted target tells Qt to draw
            # the source's left edge at what would have been the right
            # edge of the un-mirrored target, etc.  The blit step in
            # paintEvent then copies the already-mirrored pixmap at
            # identity, aligning back items with the front pass.
            sx = -1.0 if self._has_mirror_h() else 1.0
            sy = -1.0 if self._has_mirror_v() else 1.0
            if sx >= 0 and sy >= 0:
                target = QRectF(0.0, 0.0, vp_w, vp_h)
            else:
                # Build the inverted target by swapping left↔right or
                # top↔bottom so width/height stay positive but Qt's
                # source→target mapping picks up the negative scale.
                left   = vp_w if sx < 0 else 0.0
                right  = 0.0  if sx < 0 else vp_w
                top    = vp_h if sy < 0 else 0.0
                bottom = 0.0  if sy < 0 else vp_h
                target = QRectF(
                    min(left, right),
                    min(top, bottom),
                    abs(right - left),
                    abs(bottom - top),
                )
                # Apply the sign via painter transform around the target
                # centre so the rasteriser stays inside the pixmap.
                cx = target.center().x()
                cy = target.center().y()
                painter.translate(cx, cy)
                painter.scale(sx, sy)
                painter.translate(-cx, -cy)
            self._scene.render(painter, target, source)
        finally:
            painter.end()
            for it in temporarily_hidden:
                try:
                    it.setVisible(True)
                except RuntimeError:
                    pass
        # Hide back items permanently until the next invalidation so Qt
        # doesn't repaint them on top of board_mid in the scene pass.
        for it in back_items:
            try:
                it.setVisible(False)
                self._back_hidden_items.append(it)
            except RuntimeError:
                pass

        self._back_cache = pix
        self._back_cache_dirty = False
        return (time.monotonic() - _t0) * 1000.0

    def resizeEvent(self, event: QResizeEvent) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        # Viewport changed size — cache pixmap no longer matches; just
        # mark dirty so the next paint rebuilds it.  Display list is
        # geometry-only and survives viewport size changes.  Keep the
        # anchor so a burst of resize events (Qt batches window-drag)
        # can fall back on the previous full-quality pixmap stretched
        # to the new viewport.
        self._invalidate_bg_cache(keep_anchor=True)
        # Resizing changes the half-viewport-fit zoom-out floor; if the
        # current zoom has slid below the new floor, snap back to it so
        # the user doesn't end up with a board lost in canvas chrome.
        try:
            m11_min, m11_max = self._compute_zoom_limits()
            current = abs(self.transform().m11())
            if current and (current < m11_min - 1e-6 or current > m11_max + 1e-6):
                self._apply_view_transform()
        except Exception:  # noqa: BLE001
            pass

    def scrollContentsBy(self, dx: int, dy: int) -> None:  # type: ignore[override]
        """Invalidate the bg cache on every pan, restart the pan-burst timer.

        The cached pixmap was rasterised at the previous scroll offset
        (the world→viewport transform encodes scroll position).  Without
        invalidation the user would see the board frozen at the
        scroll-start position while items above pan correctly.  We keep
        the anchor pixmap so ``drawBackground`` can blit-and-stretch it
        during the active burst, then rebuild fresh once the burst ends.
        """
        super().scrollContentsBy(dx, dy)
        self._invalidate_bg_cache(keep_anchor=True)
        self._pan_burst_timer.start()

    def _mousePressEvent_impl(self, event: QMouseEvent) -> None:
        ctrl = event.modifiers() & Qt.KeyboardModifier.ControlModifier
        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = True
            self._pan_start = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif event.button() == Qt.MouseButton.LeftButton and ctrl:
            # Ctrl+LeftButton: check if tool wants to handle it first (component
            # drag); if the tool consumed it (returns True), skip view pan.
            scene_pt = self.mapToScene(event.pos())
            if self._active_tool is not None and self._active_tool.on_press(
                event, scene_pt
            ):
                return
            # No tool consumed it — pan the view.
            self._ctrl_panning = True
            self._panning = True
            self._pan_start = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif event.button() == Qt.MouseButton.RightButton:
            scene_pt = self.mapToScene(event.pos())
            handler = getattr(self._active_tool, "on_right_press", None)
            consumed = handler(scene_pt) if handler is not None else False
            if not consumed:
                self._show_canvas_menu(event.pos(), scene_pt)
        elif self._active_tool is not None:
            scene_pt = self.mapToScene(event.pos())
            self._active_tool.on_press(event, scene_pt)
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._panning:
            delta = event.position() - self._pan_start
            self._pan_start = event.position()
            self.horizontalScrollBar().setValue(
                int(self.horizontalScrollBar().value() - delta.x())
            )
            self.verticalScrollBar().setValue(
                int(self.verticalScrollBar().value() - delta.y())
            )
        scene_pt = self.mapToScene(event.pos())
        self.cursor_moved.emit(scene_pt.x(), scene_pt.y())
        if not self._panning and self._active_tool is not None:
            self._active_tool.on_move(event, scene_pt)
        elif not self._panning:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        _t0 = time.monotonic() if _CLICK_PROFILE else 0.0
        try:
            self._mouseReleaseEvent_impl(event)
        finally:
            if _CLICK_PROFILE:
                _click_log.info(
                    "click_profile.boardview.mouseReleaseEvent",
                    ms=round((time.monotonic() - _t0) * 1000, 1),
                )

    def _mouseReleaseEvent_impl(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton and self._panning:
            self._panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
        elif event.button() == Qt.MouseButton.LeftButton and self._ctrl_panning:
            self._panning = False
            self._ctrl_panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
        elif self._active_tool is not None:
            scene_pt = self.mapToScene(event.pos())
            self._active_tool.on_release(event, scene_pt)
        else:
            super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.isAutoRepeat():
            event.ignore()
            return
        key = event.key()
        if key == Qt.Key.Key_Escape:
            if self._active_tool is not None:
                self._active_tool.on_key(event)
            self.escape_tool.emit()
            return
        # Arrow keys — scroll by one raster unit (only without modifier).
        if not event.modifiers() and key in (
            Qt.Key.Key_Left, Qt.Key.Key_Right,
            Qt.Key.Key_Up, Qt.Key.Key_Down,
        ):
            # Convert 1 raster unit to viewport pixels using current transform.
            px = max(int(self.transform().m11()), 1)
            py = max(int(self.transform().m22()), 1)
            if key == Qt.Key.Key_Left:
                self.horizontalScrollBar().setValue(
                    self.horizontalScrollBar().value() - px
                )
            elif key == Qt.Key.Key_Right:
                self.horizontalScrollBar().setValue(
                    self.horizontalScrollBar().value() + px
                )
            elif key == Qt.Key.Key_Up:
                self.verticalScrollBar().setValue(
                    self.verticalScrollBar().value() - py
                )
            elif key == Qt.Key.Key_Down:
                self.verticalScrollBar().setValue(
                    self.verticalScrollBar().value() + py
                )
            return
        if self._active_tool is not None:
            self._active_tool.on_key(event)
        # Forward to MainWindow so global shortcuts (S/W/C/H/F5/F6/Ctrl+0…) work
        # regardless of which widget has focus.  Tools call event.accept() for keys
        # they handle exclusively (R, Del, A, Enter, F) to prevent double-dispatch.
        if not event.isAccepted():
            self.window().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if self._active_tool is not None:
            self._active_tool.on_key_release(event)
        super().keyReleaseEvent(event)

    def leaveEvent(self, event: object) -> None:
        """Reset cursor when mouse leaves the viewport."""
        self.setCursor(Qt.CursorShape.ArrowCursor)
        super().leaveEvent(event)  # type: ignore[arg-type]


def _build_wire_net_map(session: EditorSession) -> dict[uuid.UUID, uuid.UUID]:
    """Return a mapping wire.id → net.id using ConnectivityGraph.

    Uses the cached graph so strips, bare-wire chains, and plated-through
    holes are all handled correctly — not just wires whose endpoints happen
    to land directly on a component pin.
    """
    from protoboard.domain.schematic.net import PinRef

    if not session.schematic.nets or not session.layout.wires:
        return {}

    graph = session.connectivity_graph()

    # canonical_key → net.id  (one net per electric set that has named pins)
    canonical_to_net: dict[object, uuid.UUID] = {}
    for net in session.schematic.nets.values():
        for ref in net.pins:
            ck = graph.node_for_pin(ref)
            if ck is not None:
                canonical_to_net[ck] = net.id

    result: dict[uuid.UUID, uuid.UUID] = {}
    for wire in session.layout.wires:
        # Insulated wires only make electrical contact at their two endpoints;
        # bare wires contact every pad they pass through.
        contact_pts = (
            ([wire.path[0], wire.path[-1]] if len(wire.path) >= 2 else wire.path)
            if wire.insulated
            else wire.path
        )
        for pt in contact_pts:
            ck = graph.node_for_pad(pt, wire.side)
            if ck is not None:
                net_id = canonical_to_net.get(ck)
                if net_id is not None:
                    result[wire.id] = net_id
                    break
    return result


class _Tool:
    """Base class for board-view interaction tools."""

    def __init__(self) -> None:
        # Bound on activate(); set to None up-front so that pre-activate code
        # paths (e.g. MainWindow applies user preferences to a freshly built
        # SelectTool) can ``if self._session is not None`` without AttributeError.
        # Type is narrowed to the real EditorSession after activate(); existing
        # callers all access these only via the event-handler paths that run
        # *after* activate().
        self._session: EditorSession = None  # type: ignore[assignment]
        self._view: BoardView = None  # type: ignore[assignment]

    def activate(self, session: EditorSession, view: BoardView) -> None:
        self._session = session
        self._view = view

    def _theme(self) -> "Theme":
        from protoboard.gui.theme import DEFAULT_THEME, Theme
        t = getattr(self._view, "_theme_override", None)
        return t if isinstance(t, Theme) else DEFAULT_THEME

    def deactivate(self) -> None:
        pass

    def on_press(self, event: QMouseEvent, scene_pt: QPointF) -> bool:
        """Handle mouse press. Return True if the event was consumed."""
        return False

    def on_move(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        pass

    def on_release(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        pass

    def on_key(self, event: QKeyEvent) -> None:
        pass

    def on_key_release(self, event: QKeyEvent) -> None:
        pass

    def on_scene_cleared(self) -> None:
        """Called after scene.clear() so tool can drop stale item references."""
        pass
