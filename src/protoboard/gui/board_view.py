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

_DRAG_REFRESH_INTERVAL = 0.033  # 30 fps max during live drag

from PySide6.QtCore import QPoint, QPointF, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QWheelEvent,
)
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView, QWidget

from protoboard.domain.board.boxes import StripAtom
from protoboard.domain.connectivity.graph import ConnectivityGraph
from protoboard.geometry import Point
from protoboard.gui.layer_state import LayerOverrides, LayerState, layer_state_for_tool
from protoboard.gui.renderers.board_renderer import BOARD_BG, render_board
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
    spread_requested = Signal()  # canvas context menu → Spread all components

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
        # Antialiasing × 14k items × 4K viewport was ~800 ms per paint frame
        # (measured via click_profile.boardview.paintEvent).  Toggling it off
        # is the single biggest paint-cost lever — geometry is still
        # rasterised correctly, edges are just less smooth.  Antialiased
        # rendering remains available for static publication exports.
        self.setRenderHint(QPainter.RenderHint.Antialiasing, False)
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
        # comp_id → placement.position at last full refresh (drag delta origin).
        self._drag_orig_group_pos: dict[uuid.UUID, object] = {}
        # Board layer: rebuilt only when board geometry / theme / alpha changes.
        self._board_layer_dirty: bool = True
        self._last_board_alpha: float = -1.0   # sentinel: never matches
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
            return

        # Determine dim values from whichever profile we ended up with.
        if isinstance(profile_obj, HighlightProfile):
            dim_c = profile_obj.dim_components
            dim_w = profile_obj.dim_wires
        else:
            dim_c = _base.highlight_dim_components
            dim_w = _base.highlight_dim_wires

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
                    grp.setOpacity(1.0)
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
        from PySide6.QtWidgets import QGraphicsItemGroup
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
        """Set the visual transform: 'normal', 'mirror_h', or 'mirror_v'."""
        if transform == self._view_transform:
            return
        self._view_transform = transform
        self._board_layer_dirty = True
        self._apply_view_transform()
        self.refresh()

    def _apply_view_transform(self) -> None:
        """Recompute the QGraphicsView scale matrix from current state."""
        sx = -1.0 if self._view_transform == "mirror_h" else 1.0
        sy = -1.0 if self._view_transform == "mirror_v" else 1.0
        # Preserve current zoom magnitude, apply sign flips.
        mag = abs(self.transform().m11()) or _GRID_PX
        # Remember the scene point currently at the viewport centre so we can
        # restore it after setTransform (which resets scroll position).
        vp = self.viewport()
        cx, cy = vp.width() / 2, vp.height() / 2
        center_scene = self.mapToScene(int(cx), int(cy))
        from PySide6.QtGui import QTransform
        self.setTransform(QTransform(sx * mag, 0.0, 0.0, sy * mag, 0.0, 0.0))
        # Re-centre on the same scene point.
        self.centerOn(center_scene)

    def _sync_tool_side(self, side: str) -> None:
        from protoboard.gui.tools.route_tool import RouteTool
        if isinstance(self._active_tool, RouteTool):
            self._active_tool._side = side  # type: ignore[assignment]
        win = self.window()
        updater = getattr(win, "_set_wire_side", None)
        if updater is not None:
            updater(side)

    # Convenience toggles (used by menu/keyboard).
    def toggle_mirror_h(self) -> None:
        self.set_view_transform(
            "normal" if self._view_transform == "mirror_h" else "mirror_h"
        )

    def toggle_mirror_v(self) -> None:
        self.set_view_transform(
            "normal" if self._view_transform == "mirror_v" else "mirror_v"
        )

    def toggle_camera(self) -> None:
        self.set_camera_side(
            "bottom" if self._camera_side == "top" else "top"
        )

    # Legacy alias used by MainWindow._mirror_view (now means Mirror H).
    def toggle_mirror(self) -> None:
        self.toggle_mirror_h()

    @property
    def is_mirrored(self) -> bool:
        return self._view_transform == "mirror_h"

    def fit_board(self) -> None:
        """Scale and center the view to fit the board bounds."""
        b = self._session.board.bounds
        self.fitInView(
            QRectF(b.x - 1, b.y - 1, b.width + 2, b.height + 2),
            Qt.AspectRatioMode.KeepAspectRatio,
        )
        self._apply_label_visibility()

    def fit_area(self) -> None:
        """Fit view to the active group's components; falls back to fit_board() for ROOT."""
        from protoboard.domain.layout.group import all_component_ids
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        from protoboard.domain.layout.placement import component_bbox

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
            pl = session.layout.placements.get(cid)
            inst = session.instances.get(cid)
            if pl is None or inst is None:
                continue
            defn = session.components.get(inst.definition_ref)
            if defn is None:
                continue
            bbox = component_bbox(defn.footprint, pl, defn.component_class)
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
        self._apply_label_visibility()

    def fit_selection(self) -> None:
        """Fit view to the bounding box of selected components and wires."""
        from protoboard.domain.layout.placement import component_bbox

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
            pl = session.layout.placements.get(cid)
            inst = session.instances.get(cid)
            if pl is None or inst is None:
                continue
            defn = session.components.get(inst.definition_ref)
            if defn is None:
                continue
            bbox = component_bbox(defn.footprint, pl, defn.component_class)
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
        current_board_alpha = layers.alpha_for_board()
        if self._board_layer_dirty or current_board_alpha != self._last_board_alpha:
            self._scene.clear()
            self._invalidate_all_groups()
            self._fire_on_scene_cleared()
            render_board(self._scene, self._session.board, theme=_theme, layers=layers)
            self._board_layer_dirty = False
            self._last_board_alpha = current_board_alpha

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
        net_color_map = {
            net_id: net.color
            for net_id, net in self._session.schematic.nets.items()
        }
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
        )

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

        # ── Overlays (always recreated — fast, few items) ────────────────────
        new_overlays: list[object] = []
        from protoboard.gui.layer_state import LayerVisibility
        if (self._session.schematic.nets
                and self._layer_overrides.ratsnest != LayerVisibility.HIDDEN):
            new_overlays.extend(render_ratsnest(
                self._scene,
                self._session.schematic,
                self._session.layout.placements,
                self._session.instances,
                self._session.components,
                routed_nets=routed,
                theme=_theme,
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
        act_center = menu.addAction("Wyśrodkuj widok tutaj")
        act_escape = menu.addAction("Anuluj narzędzie (Esc)")
        menu.addSeparator()
        act_spread = menu.addAction("Spread all components")
        act_spread.setEnabled(bool(self._session.layout.placements))
        chosen = menu.exec(self.mapToGlobal(vp_pos))
        if chosen is act_center:
            self.center_on_scene_point(scene_pt)
        elif chosen is act_escape:
            self.escape_tool.emit()
        elif chosen is act_spread:
            self.spread_requested.emit()

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

        old_scene = self.mapToScene(pivot)
        self.scale(factor, factor)
        new_scene = self.mapToScene(pivot)
        delta_scene = new_scene - old_scene
        new_scale = self.transform().m11()
        hbar = self.horizontalScrollBar()
        vbar = self.verticalScrollBar()
        hbar.setValue(hbar.value() - round(delta_scene.x() * new_scale))
        vbar.setValue(vbar.value() - round(delta_scene.y() * new_scale))
        # Defer the O(scene items) visibility sweep — same reasoning
        # as before, just relative to the (now batched) zoom paint.
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
        _t0 = time.monotonic() if _CLICK_PROFILE else 0.0
        super().paintEvent(event)  # type: ignore[arg-type]
        if _CLICK_PROFILE:
            paint_ms = round((time.monotonic() - _t0) * 1000, 1)
            press_ts = getattr(self, "_click_press_ts", None)
            if press_ts is not None and (time.monotonic() - press_ts) < 1.0:
                # Only log paints temporally close to a click — otherwise
                # every viewport update spams the console.
                since_press_ms = round((time.monotonic() - press_ts) * 1000, 1)
                _click_log.info(
                    "click_profile.boardview.paintEvent",
                    paint_ms=paint_ms,
                    since_press_ms=since_press_ms,
                )

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
