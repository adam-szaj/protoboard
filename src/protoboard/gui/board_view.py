# SPDX-License-Identifier: MIT
"""BoardView — QGraphicsView showing the board with all layers."""

from __future__ import annotations

import math
import os
import time
import uuid
from typing import TYPE_CHECKING, override

import structlog

if TYPE_CHECKING:
    from protoboard.gui.keymap import KeyMap

_click_log = structlog.get_logger("protoboard.gui.board_view.click")
_CLICK_PROFILE = os.environ.get("PROTOBOARD_CLICK_PROFILE") not in (None, "", "0")
# PROTOBOARD_PAINT_PROFILE logging lives in gui/paint_pipeline.py.

_DRAG_REFRESH_INTERVAL = 0.033  # 30 fps max during live drag

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QBrush,
    QColor,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QResizeEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import QGraphicsItem as _QGItem  # noqa: E402 – used in refresh()
from PySide6.QtWidgets import (
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QWidget,
)

from protoboard.domain.board.boxes import StripAtom
from protoboard.geometry import Point
from protoboard.gui.layer_state import (
    LayerOverrides,
    LayerState,
    layer_state_for_tool,
)
from protoboard.gui.renderers.board_renderer import BOARD_BG
from protoboard.gui.renderers.component_renderer import (
    render_groups,
    update_component_groups,
)
from protoboard.gui.renderers.wire_renderer import (
    render_ratsnest,
    update_wire_groups,
)
from protoboard.gui.highlight.renderer import HighlightRenderer
from protoboard.gui.paint_pipeline import PaintPipeline
from protoboard.gui.tool_host import ToolHost
from protoboard.gui.view_camera import GRID_PX as _GRID_PX
from protoboard.gui.view_camera import ViewCamera
from protoboard.gui.session import EditorSession
from protoboard.gui.theme import DEFAULT_THEME, Theme
from protoboard.services.debug_trace import traced
from protoboard.services.routing.net_status import routed_net_ids
from protoboard.gui.board_view_tool import AbstractBoardView, _Tool



class BoardView(AbstractBoardView):
    """Main board canvas. Handles zoom/pan and forwards mouse events to tools.

    The ``cursor_moved`` / ``pad_clicked`` / ``escape_tool`` signals are
    declared on :class:`AbstractBoardView`.
    """

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
        # Tool lifecycle + input dispatch + canvas menu + [?] overlay —
        # extracted service (see gui/tool_host.py).  Owns the active tool,
        # the permanent-tool list and the namespaced KeyMap.
        self.tools = ToolHost(self)
        # Camera: zoom, pan-burst, mirror + fit — extracted service
        # (see gui/view_camera.py).  Owns the wheel/pan timers behind
        # ``in_transform_burst()``.
        self.camera = ViewCamera(self)
        self._layer_overrides: LayerOverrides = LayerOverrides()
        self._pad_positions: list[Point] = []
        # Active group for rendering context (set by SelectTool on navigation).
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        self._active_group_id: uuid.UUID = ROOT_GROUP_ID
        # Highlight mechanism (dimming, cyan outlines, wire/net-pad overlays,
        # selection bbox) — extracted service; owns all highlight state.
        self.highlight = HighlightRenderer(self)
        # Subscribe to highlight stack changes.
        session.highlight_stack.changed.connect(self.highlight.apply)
        # Subscribe to routing-net changes (RouteTool while-drawing union with
        # stack).  One slot with a matching (net_id) arity: it applies opacity
        # immediately for the live feedback while drawing, AND schedules a full
        # refresh so the per-pin glow ring is rebuilt — the ring is drawn inside
        # the component group at render time, so a mere opacity tweak can't add
        # or remove it.  (``_apply_highlight_opacity`` itself must not be
        # connected here: it takes no argument, and its @traced wrapper hides
        # the arity from Qt, so Qt would pass the net id and blow up the call.)
        session.routing_net_changed.connect(self._on_routing_net_changed)
        # Panel -> board selection: a panel asks the session to select something
        # (component / net / pin); the session re-broadcasts it here and we drive
        # SelectTool.  This is the single subscription point — no panel-awareness
        # leaks into the view; it only ever knows the session it already holds.
        # The bound slot is parked once: the @traced descriptor mints a fresh
        # partial per attribute access, and set_session must hand Qt the very
        # same object again or disconnect() silently misses.
        self._selection_requested_slot = self._on_selection_requested
        session.selection_requested.connect(self._selection_requested_slot)
        # Theme — overridden by MainWindow after Preferences apply.
        self._theme_override: Theme | None = None
        self._last_drag_refresh: float = 0.0
        # comp_id → QGraphicsItemGroup; persists across refreshes for reuse.
        self._comp_groups: dict[uuid.UUID, object] = {}
        # comp_id → label QGraphicsItemGroup (refdes / pin names / lock) drawn
        # at Z_LABELS_TOP, above the pin-modifier glyphs.  Kept parallel to
        # _comp_groups so it follows live drag and group dimming.
        self._comp_label_groups: dict[uuid.UUID, object] = {}
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
        self._drag_orig_group_pos: dict[uuid.UUID, Point] = {}
        # wire_id → wire.path[0] at last full refresh (drag delta origin for wires).
        self._drag_orig_wire_pose: dict[uuid.UUID, Point] = {}
        # Board layer: rebuilt only when board geometry / theme / alpha changes.
        self._board_layer_dirty: bool = True
        self._last_board_alpha: float = -1.0   # sentinel: never matches
        # Layered background composition + bg/back pixmap caches + the
        # stale-blit anchor — extracted service (see gui/paint_pipeline.py).
        # ``paintEvent`` / ``drawBackground`` stay as Qt overrides below and
        # delegate into it.
        self.paint = PaintPipeline(self)
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
        session.layout_changed.connect(self._schedule_refresh)
        session.layout_changed.connect(session.invalidate_graph)
        session.drag_moved.connect(self._on_drag_moved)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    @traced()
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

    @traced()
    def set_ratsnest_mode(self, mode: str) -> None:
        """Switch the Connections layer between ``"full"`` (K_n graph)
        and ``"closest"`` (nearest-other-pin) line modes.  No-op when
        the mode is already active; otherwise refresh the overlay."""
        if mode not in ("full", "closest") or mode == self._ratsnest_mode:
            return
        self._ratsnest_mode = mode
        self.refresh()

    @traced()
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
        self.invalidate_bg_cache()
        self.viewport().update()

    @traced()
    def set_show_selection_bbox(self, enabled: bool) -> None:
        """Toggle the thin dashed selection bbox (delegates to the renderer)."""
        self.highlight.set_show_selection_bbox(enabled)

    @property
    @traced()
    def camera_side(self) -> str:
        return self.camera.camera_side

    @property
    @traced()
    def view_transform(self) -> str:
        return self.camera.view_transform

    @property
    @traced()
    def active_group_id(self) -> uuid.UUID:
        return self._active_group_id

    @traced()
    def set_active_group(self, group_id: uuid.UUID) -> None:
        """Called by SelectTool on group navigation to trigger a re-render."""
        self._active_group_id = group_id
        self.refresh()

    @traced()
    def _on_routing_net_changed(self, _net_id: object) -> None:
        """Apply opacity now + invalidate group caches so glow rings rebuild.

        The opacity pass gives live feedback while drawing.  Group
        fingerprints intentionally exclude highlight state (it's
        usually applied via opacity-only updates), so a change in
        ``routing_net_id`` would not trigger a rebuild by itself.  Clear
        the caches and ask for a refresh — the next paint pass will
        render the glow rings on pads belonging to the routed net.
        """
        self.highlight.apply()
        self._comp_fingerprints.clear()
        self._wire_fingerprints.clear()
        self._schedule_refresh()

    @traced()
    def set_highlighted_net(self, net_id: uuid.UUID | None) -> None:
        """Legacy: delegate to the highlight renderer (used by panels)."""
        self.highlight.set_highlighted_net(net_id)

    @traced()
    def set_selection_outline_override(self, color: QColor | None) -> None:
        """Delegate the drag-conflict outline colour to the renderer."""
        self.highlight.set_selection_outline_override(color)

    @traced()
    def set_clicked_free_pad(self, pos: Point | None) -> None:
        """Delegate the clicked free-pad ring to the renderer."""
        self.highlight.set_clicked_free_pad(pos)

    _LABEL_MIN_SCENE_UNITS: float = 0.8

    @traced()
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

    @traced()
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
            delta = pl.position - orig
            grp.setPos(delta.x, delta.y)
            # Move the matching label group by the same delta so labels track
            # the drag (they live in a separate group at Z_LABELS_TOP).
            lbl = self._comp_label_groups.get(comp_id)
            if isinstance(lbl, QGraphicsItemGroup):
                lbl.setPos(delta.x, delta.y)

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
            if not isinstance(grp, QGraphicsItemGroup) or not wire.path:
                continue
            orig = self._drag_orig_wire_pose.get(wire.id)
            if orig is None:
                continue
            delta = wire.path[0] - orig
            grp.setPos(delta.x, delta.y)

        self._update_group_outlines_on_drag()

    @traced()
    def _update_group_outlines_on_drag(self) -> None:
        """Recompute each group's outline rect from live member geometry.

        Outline overlay rects live in ``self._overlay_items`` (not the moved
        comp groups), so a drag's ``setPos`` doesn't touch them — this resizes
        each in place to the bbox of its members' pins plus owned-wire
        vertices, so the live outline matches the statically-rendered one
        instead of shrinking at drag-start and re-growing at release.  Pulled
        out of ``_on_drag_moved`` so that method reads as "move groups, move
        wires, refresh outlines".
        """
        from PySide6.QtWidgets import QGraphicsRectItem

        from protoboard.domain.layout.group_ownership import OwnershipResolver
        from protoboard.domain.layout.group_tree import GroupTree
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        layout = self._session.layout
        # Used by the OwnershipResolver below (group-outline ownership classifier).
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
        # Resolve wire→net once for this paint; cached on session.
        try:
            wire_net_map = self._session.cached_wire_net_map() if self._session is not None else {}
        except Exception:  # noqa: BLE001
            wire_net_map = {}
        schematic = getattr(self._session, "schematic", None) if self._session is not None else None
        _tree = GroupTree(layout.groups)
        # Bind the LOCAL outline context (may be a drag-time snapshot, not
        # session state) — build the resolver directly, not from_session.
        _ownership = OwnershipResolver(
            knots=layout.knots,  # type: ignore[arg-type]
            wire_net_map=wire_net_map,
            schematic=schematic,
            placements=placements,
            instances=instances,
            definitions=components,
        )
        for gid, rect_item in outline_by_gid.items():
            grp_obj = layout.groups.get(gid)
            if grp_obj is None or gid == ROOT_GROUP_ID:
                continue
            comp_ids = _tree.all_component_ids(gid)
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
                owned_wire_ids = _ownership.owned_wire_ids(
                    list(layout.wires), grp_obj, comp_ids,
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
            rect_w = max(xs) - min(xs) + 2 * margin
            rect_h = max(ys) - min(ys) + 2 * margin
            rect_item.setRect(QRectF(x0, y0, rect_w, rect_h))

    @traced()
    def set_wire_drag_state(
        self,
        states: dict[uuid.UUID, str],
    ) -> None:
        """Delegate per-wire drag ghosting to the renderer."""
        self.highlight.set_wire_drag_state(states)

    @traced()
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

    @traced()
    def set_camera_side(self, side: str) -> None:
        """Switch between top and bottom camera (delegates to the camera)."""
        self.camera.set_camera_side(side)

    @traced()
    def set_view_transform(self, transform: str) -> None:
        """Set the visual transform (delegates to the camera)."""
        self.camera.set_view_transform(transform)

    @traced()
    def toggle_mirror_h(self) -> None:
        self.camera.toggle_mirror_h()

    @traced()
    def toggle_mirror_v(self) -> None:
        self.camera.toggle_mirror_v()

    @traced()
    def toggle_camera(self) -> None:
        self.camera.toggle_camera()

    # Legacy alias used by MainWindow._mirror_view (now means Mirror H).
    @traced()
    def toggle_mirror(self) -> None:
        self.camera.toggle_mirror()

    @property
    @traced()
    def is_mirrored(self) -> bool:
        return self.camera.is_mirrored

    @traced()
    def fit_board(self) -> None:
        """Scale and center the view to fit the board bounds."""
        self.camera.fit_board()

    @traced()
    def fit_area(self) -> None:
        """Fit view to the active group's components (ROOT → whole board)."""
        self.camera.fit_area()

    @traced()
    def fit_selection(self) -> None:
        """Fit view to the selected components and wires."""
        self.camera.fit_selection()

    @traced()
    def set_session(self, session: EditorSession) -> None:
        """Replace the current session (used when opening a recent project)."""
        self.tools.detach_active_tool()
        # Drop the old session's selection-request hook before swapping, so a
        # lingering old session can't drive this view alongside the new one.
        # Must be the parked slot object from __init__ — a fresh
        # ``self._on_selection_requested`` access is a different partial and
        # disconnect() would silently miss.
        try:
            self._session.selection_requested.disconnect(
                self._selection_requested_slot
            )
        except (TypeError, RuntimeError):
            pass
        self._session = session
        self.highlight.set_highlighted_net_silent(None)
        self._board_layer_dirty = True
        session.layout_changed.connect(self._schedule_refresh)
        session.layout_changed.connect(session.invalidate_graph)
        session.drag_moved.connect(self._on_drag_moved)
        session.selection_requested.connect(self._selection_requested_slot)
        self.refresh()

    @traced()
    def set_tool(self, tool: _Tool | None) -> None:
        """Swap the active tool (delegates to the tool host)."""
        self.tools.set_tool(tool)

    @property
    def _active_tool(self) -> _Tool | None:
        """Alias for ``tools.active_tool`` (read-only; legacy internal name)."""
        return self.tools.active_tool

    @property
    @traced()
    def keymap(self) -> KeyMap:
        """The namespaced keybindings the active tool consults for the veto."""
        return self.tools.keymap

    # -- AbstractBoardView surface consumed by extracted services -------------

    @property
    def session(self) -> EditorSession:
        """The editor session this canvas currently renders."""
        return self._session

    @property
    def theme(self) -> Theme:
        """The resolved theme (Preferences override or the default)."""
        return self._theme_override if self._theme_override is not None else DEFAULT_THEME

    @property
    def active_tool(self) -> _Tool | None:
        """The tool currently receiving delegated input events."""
        return self.tools.active_tool

    def component_groups(self) -> dict[uuid.UUID, object]:
        """comp_id → its persistent ``QGraphicsItemGroup`` (or stand-in)."""
        return self._comp_groups

    def component_label_groups(self) -> dict[uuid.UUID, object]:
        """comp_id → its label ``QGraphicsItemGroup`` (refdes / pin names)."""
        return self._comp_label_groups

    def wire_groups(self) -> dict[uuid.UUID, object]:
        """wire_id → its persistent ``QGraphicsItemGroup`` (or stand-in)."""
        return self._wire_groups

    @property
    def antialiasing(self) -> bool:
        """Whether render passes (scene + cached pixmaps) antialias."""
        return self._antialiasing

    def layer_state(self) -> LayerState:
        """The resolved per-layer visibility/alpha state for this paint."""
        return layer_state_for_tool(
            self._active_tool,
            self.camera.camera_side,  # type: ignore[arg-type]
            self.camera.view_transform,  # type: ignore[arg-type]
            overrides=self._layer_overrides,
            theme=self.theme,
        )

    def mirror_flags(self) -> tuple[bool, bool]:
        """(mirror_h, mirror_v) of the current view transform."""
        return self.camera.mirror_flags()

    def in_transform_burst(self) -> bool:
        """True while a wheel-zoom or pan-scroll burst is in flight."""
        return self.camera.in_transform_burst()

    def mark_board_layer_dirty(self) -> None:
        """Force a board-layer rebuild on the next refresh()."""
        self._board_layer_dirty = True

    def apply_label_visibility(self, *, deferred: bool = False) -> None:
        """Re-evaluate label visibility now, or debounced after a zoom."""
        if deferred:
            if not self._label_vis_timer.isActive():
                self._label_vis_timer.start()
            return
        self._apply_label_visibility()

    @traced()
    def board_pad_positions(self) -> list[Point]:
        """Cached list of all board pad positions (raster units)."""
        return self._pad_positions

    @traced()
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

    @traced()
    def _schedule_refresh(self) -> None:
        """Schedule a refresh for the next event-loop tick (debounce)."""
        if not self._refresh_timer.isActive():
            self._refresh_timer.start()

    @traced()
    def _invalidate_all_groups(self) -> None:
        """Drop all persistent group caches (called when scene is fully cleared)."""
        self._comp_groups.clear()
        self._comp_label_groups.clear()
        self._comp_fingerprints.clear()
        self._wire_groups.clear()
        self._wire_fingerprints.clear()
        self._overlay_items.clear()
        # Highlight overlays reference items the scene clear just destroyed —
        # the renderer drops those refs (do NOT removeItem; the C++ objects are
        # already gone).
        self.highlight.on_scene_cleared()

    @traced()
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
            self.camera.camera_side,  # type: ignore[arg-type]
            self.camera.view_transform,  # type: ignore[arg-type]
            overrides=self._layer_overrides,
            theme=_base,
        )
        _theme = _dc_replace(_base,
                             raster_pitch_mm=self._session.board.raster_pitch_mm,
                             package_families=self._session.package_families)

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
            self.tools.fire_on_scene_cleared()
            # Board state changed — drop both pixmap and display list so
            # the next drawBackground rebuilds from current board state.
            self.paint.invalidate_display_list()
            self.invalidate_bg_cache()
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
                self.paint.invalidate_display_list()
                self.invalidate_bg_cache()

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
        net_color_map: dict[uuid.UUID, str | None] = dict(
            build_net_color_map(self._session.schematic.nets, theme=_theme)
        )
        routed: set[uuid.UUID] | None = None
        if self._session.schematic.nets:
            routed = routed_net_ids(
                self._session.schematic, self._session.connectivity_graph()
            )

        # ── Wires (group reuse) ───────────────────────────────────────────────
        update_wire_groups(
            self._scene,
            list(self._session.layout.wires),
            wire_net_map,
            self._wire_groups,        # type: ignore[arg-type]
            self._wire_fingerprints,  # type: ignore[arg-type]
            net_color_map=net_color_map,
            theme=_theme,
            layers=layers,
            highlighted_net_id=self.highlight.effective_highlighted_net(),
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
            highlighted_net_id=self.highlight.effective_highlighted_net(),
            existing_label_groups=self._comp_label_groups,  # type: ignore[arg-type]
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
        # Cache the records so the highlight pass can draw bright net-pad discs
        # without recomputing the classification.
        self.highlight.set_pin_pad_records(list(pp_records))

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
                wires=list(self._session.layout.wires),
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
        # Suppress the modifier glyphs while a wire is being edited: the
        # editable vertex handles sit above the modifiers, so the markers
        # would clutter / occlude the handles the user is dragging.
        _wire_editing = (
            hasattr(self._active_tool, "is_wire_editing")
            and self._active_tool.is_wire_editing()  # type: ignore[attr-defined]
        )
        if pp_records and not _wire_editing:
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
        self.invalidate_back_cache()

        # ── Final highlight pass ─────────────────────────────────────────────
        # ``update_component_groups`` no longer applies net-highlight dimming
        # (it would clobber the route-active profile during a wire drag).
        # The canonical highlight pass therefore has to run after every
        # refresh, otherwise Net Inspector / Selection highlights would be
        # lost the moment any unrelated refresh fires (drag, undo, theme
        # change, etc.).
        self.highlight.apply()

        _ms = (time.monotonic() - _t0) * 1000
        self._session.perf.record_refresh(_ms)
        self._session.perf.scene_items = len(self._scene.items())

    @traced()
    def select_component(self, comp_id: uuid.UUID) -> None:
        """Switch to SelectTool and select the given component."""
        from protoboard.gui.tools.select_tool import SelectTool

        if not isinstance(self._active_tool, SelectTool):
            self.set_tool(SelectTool())
        tool = self._active_tool
        if isinstance(tool, SelectTool):
            tool._select_primary(comp_id)

    @traced()
    def _on_selection_requested(
        self, comp_ids: object, wire_ids: object, net_id: object
    ) -> None:
        """Drive SelectTool from a session-level selection request (panel -> board).

        The mirror of the board -> panel signal: a panel asked the session to
        select something; the session re-broadcast it here.  We switch to
        SelectTool (so the request is honoured even from Route/Place mode) and
        rebuild its selection through the SAME setters the context menu and
        ``select_component`` use, so the result loops back out through
        ``_notify_selection`` and every panel syncs.

        A ``net_id`` pulls in the whole net's comps + wires (via
        ``SessionNetSource``); any explicit component/wire ids are unioned on top
        so a *pin* request (comp id + the pin's net) lights the net AND focuses
        the component.  The full target set is built first and applied in a
        single ``_set_selection_from_ids`` — re-running a net-select then a union
        would re-pass already-selected items to ``set_many``, which toggles them
        back OFF (the documented FSM trap).
        """
        from protoboard.gui.selection.queries import SessionNetSource
        from protoboard.gui.tools.select_tool import SelectTool

        if not isinstance(self._active_tool, SelectTool):
            self.set_tool(SelectTool())
        tool = self._active_tool
        if not isinstance(tool, SelectTool):
            return
        nid = net_id if isinstance(net_id, uuid.UUID) else None
        target_c: set[uuid.UUID] = (
            set(comp_ids) if isinstance(comp_ids, frozenset) else set()
        )
        target_w: set[uuid.UUID] = (
            set(wire_ids) if isinstance(wire_ids, frozenset) else set()
        )
        if nid is not None:
            src = SessionNetSource(self._session)
            target_c |= set(src.net_component_ids(nid))
            target_w |= set(src.wire_ids_on_net(nid))
        # Clear first: _set_selection_from_ids -> set_many -> set(first), and the
        # FSM SET op no-ops / toggles-off a still-selected first item, so applying
        # onto a non-empty selection can silently drop it.
        tool._clear_selection_to_empty()
        tool._set_selection_from_ids(target_c, target_w, set())
        tool._notify_selection()

    @traced()
    def snap(self, scene_pt: QPointF, exclude_ids: frozenset[uuid.UUID] | None = None) -> Point:
        """Snap scene-space point to nearest accessible pad or component pin.

        Pins belonging to child groups are only accessible if they are exported.
        ``exclude_ids`` — component IDs whose pins are skipped (e.g. currently dragged).
        """

        # Build set of component IDs that are locked inside child groups,
        # and their exported pins.
        groups = self._session.layout.groups
        tree = self._session.group_tree()
        active_group = groups.get(self._active_group_id)
        locked_comp_ids: set[uuid.UUID] = set()
        exported_pins: set[tuple[uuid.UUID, int]] = set()
        if active_group is not None:
            for child_gid in active_group.child_group_ids:
                child_group = groups.get(child_gid)
                if child_group is None:
                    continue
                for cid in tree.all_component_ids(child_gid):
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

    @traced()
    def is_locked_pin(self, scene_pt: QPointF, tol: float = 0.6) -> bool:
        """Return True if *scene_pt* is near a pin locked inside a child group
        (i.e. not exported). Used by RouteTool to show a status message."""
        groups = self._session.layout.groups
        tree = self._session.group_tree()
        active_group = groups.get(self._active_group_id)
        if active_group is None:
            return False
        locked_comp_ids: set[uuid.UUID] = set()
        exported_pins: set[tuple[uuid.UUID, int]] = set()
        for child_gid in active_group.child_group_ids:
            child_group = groups.get(child_gid)
            if child_group is None:
                continue
            for cid in tree.all_component_ids(child_gid):
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

    @traced()
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

    @traced()
    def _add_tool_action_menu(self, menu: object) -> dict[object, str]:
        """Delegate to the tool host (SelectTool reaches this via getattr)."""
        return self.tools.add_tool_action_menu(menu)

    @traced()
    def trigger_tool_action(self, action_id: str) -> None:
        """Run *action_id* by synthesising its resolved key into the tool."""
        self.tools.trigger_tool_action(action_id)

    # ------------------------------------------------------------------
    # Qt event handlers
    # ------------------------------------------------------------------

    def wheelEvent(self, event: QWheelEvent) -> None:
        """Tool wheel veto, then camera zoom accumulation.

        An armed tool (e.g. float-rotate) may claim the wheel to step its
        own state instead of zooming — consulted before any accumulation so
        the zoom path is byte-for-byte unchanged when no tool opts in.
        """
        tool = self._active_tool
        if tool is not None and tool.wants_wheel(event):
            tool.on_wheel(event)
            event.accept()
            return
        self.camera.on_wheel(event)

    def zoom_by(self, factor: float) -> None:
        """Keyboard / toolbar zoom (delegates to the camera)."""
        self.camera.zoom_by(factor)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self.tools.handle_double_click(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        _t0 = time.monotonic() if _CLICK_PROFILE else 0.0
        try:
            if not self.tools.handle_mouse_press(event):
                super().mousePressEvent(event)
        finally:
            if _CLICK_PROFILE:
                self._click_press_ts = time.monotonic()
                _click_log.info(
                    "click_profile.boardview.mousePressEvent",
                    ms=round((time.monotonic() - _t0) * 1000, 1),
                )

    def paintEvent(self, event: object) -> None:  # type: ignore[override]
        # Always measure paintEvent wall time — PerformancePanel reads it
        # via session.perf.paint_ms (recorded by the pipeline bracket).
        _t0 = time.monotonic()
        self.paint.begin_paint_event()
        super().paintEvent(event)  # type: ignore[arg-type]
        self.paint.end_paint_event(
            started_at=_t0,
            click_press_ts=getattr(self, "_click_press_ts", None),
        )

    # ------------------------------------------------------------------
    # Background cache (board layer)
    # ------------------------------------------------------------------

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:  # type: ignore[override]
        """Delegate the layered background composition to the pipeline."""
        self.paint.draw_background(painter)

    def invalidate_bg_cache(self, *, keep_anchor: bool = False) -> None:
        """Delegate board-pixmap invalidation to the pipeline."""
        self.paint.invalidate_bg_cache(keep_anchor=keep_anchor)

    def invalidate_back_cache(self) -> None:
        """Mark only the back-side cache stale."""
        self.paint.invalidate_back_cache()

    def resizeEvent(self, event: QResizeEvent) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        # Viewport changed size — cache pixmap no longer matches; just
        # mark dirty so the next paint rebuilds it.  Display list is
        # geometry-only and survives viewport size changes.  Keep the
        # anchor so a burst of resize events (Qt batches window-drag)
        # can fall back on the previous full-quality pixmap stretched
        # to the new viewport.
        self.invalidate_bg_cache(keep_anchor=True)
        # The camera snaps zoom back inside the new limit window.
        self.camera.on_resize()

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
        self.invalidate_bg_cache(keep_anchor=True)
        self.camera.note_pan_scroll()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self.tools.handle_mouse_move(event):
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        _t0 = time.monotonic() if _CLICK_PROFILE else 0.0
        try:
            if not self.tools.handle_mouse_release(event):
                super().mouseReleaseEvent(event)
        finally:
            if _CLICK_PROFILE:
                _click_log.info(
                    "click_profile.boardview.mouseReleaseEvent",
                    ms=round((time.monotonic() - _t0) * 1000, 1),
                )

    def event(self, event: QEvent) -> bool:
        """ShortcutOverride → the active tool may veto a global shortcut."""
        if event.type() == QEvent.Type.ShortcutOverride and (
            self.tools.wants_shortcut_override(event)
        ):
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        self.tools.handle_key_press(event)

    @override
    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        self.tools.handle_key_release(event)
        super().keyReleaseEvent(event)

    def leaveEvent(self, event: object) -> None:
        """Reset cursor when mouse leaves the viewport."""
        self.setCursor(Qt.CursorShape.ArrowCursor)
        super().leaveEvent(event)  # type: ignore[arg-type]


# build_wire_net_map moved to protoboard.domain.connectivity.wire_net_map;
# GUI code reaches it through EditorSession.cached_wire_net_map().


