# SPDX-License-Identifier: MIT
"""ViewCamera — zoom, pan-burst, mirror and fit for the board canvas.

Extracted from ``BoardView`` (phase 3 of the decomposition).  Owns the camera
state (side, visual transform), the wheel-zoom accumulator with its flush
timer, the pan-burst detector, zoom limits derived from physical DPI + board
bounds, and the fit_* framing helpers.  The Qt input overrides
(``wheelEvent`` / ``scrollContentsBy`` / ``resizeEvent``) stay on
``BoardView`` and delegate here; the camera talks to the canvas only through
:class:`~protoboard.gui.board_view_tool.AbstractBoardView`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from protoboard.geometry import Point, Rect, Transform2D
from protoboard.services.debug_trace import traced

if TYPE_CHECKING:
    from protoboard.gui.board_view_tool import AbstractBoardView
    from protoboard.controller.ports import ViewportPort, WheelInput

# Scene units = raster units. 1 raster unit = 1 "pixel" in scene space.
GRID_PX = 60.0  # initial pixels-per-raster-unit


class ViewCamera:
    """Camera state + zoom/pan/mirror/fit mechanics for the board canvas."""

    def __init__(self, view: AbstractBoardView) -> None:
        self._view = view
        self._port: ViewportPort = view.viewport_port
        # View state — camera side and visual transform are independent.
        self._camera_side: str = "top"           # "top" | "bottom"
        self._view_transform: str = "normal"     # "normal" | "mirror_h" | "mirror_v"
        # Wheel-zoom accumulator.  Each wheel event adds its delta
        # (in 1/120-of-a-degree units, Qt's angleDelta) into the buffer
        # and parks the most-recent cursor viewport position.  A short
        # timer collapses bursts of ticks into a single scale + scroll
        # pass, so over SSH the wheel handler returns in microseconds
        # and Qt processes the next tick without waiting for a paint.
        # Wheel-zoom coalescing: accumulate the raw angleDelta, keep the latest
        # cursor pivot, and fold the burst into one scale + scroll pass ~1 frame
        # later.  The parked sample is ``(delta, pivot)``; combine sums the delta
        # while the latest pivot wins.
        from protoboard.gui.throttle import ParkLatestThrottle, TimerWrap
        self._wheel_throttle: ParkLatestThrottle[tuple[int, Point]] = (
            ParkLatestThrottle(
                16,  # ~1 frame
                self._flush_wheel_zoom,
                combine=lambda old, new: (old[0] + new[0], new[1]),
            )
        )
        # Pan-burst detector — restarted on each scrollContentsBy.  While
        # this timer is active we treat the view as in mid-pan and paint
        # the board pixmap by stretching the anchor instead of rebuilding.
        # When it fires (settle), the next paint rebuilds the cache fresh.
        self._pan_burst_timer = TimerWrap(
            120, self._on_pan_settled, restart_on_arm=True,  # settle detector
        )

    # -- state -------------------------------------------------------------------

    @property
    def camera_side(self) -> str:
        return self._camera_side

    @property
    def view_transform(self) -> str:
        return self._view_transform

    def in_transform_burst(self) -> bool:
        """True while a wheel-zoom or pan-scroll burst is in flight."""
        return self._wheel_throttle.is_active() or self._pan_burst_timer.is_active()

    def mirror_flags(self) -> tuple[bool, bool]:
        """(mirror_h, mirror_v) of the current view transform."""
        return (self._has_mirror_h(), self._has_mirror_v())

    @traced()
    def _has_mirror_h(self) -> bool:
        return self._view_transform in ("mirror_h", "mirror_hv")

    @traced()
    def _has_mirror_v(self) -> bool:
        return self._view_transform in ("mirror_v", "mirror_hv")

    @property
    def is_mirrored(self) -> bool:
        return self._has_mirror_h()

    # -- camera / transform --------------------------------------------------------

    @traced()
    def set_camera_side(self, side: str) -> None:
        """Switch between top and bottom camera.  Syncs tool side."""
        if side == self._camera_side:
            return
        self._camera_side = side
        self._view.mark_board_layer_dirty()
        self._sync_tool_side(side)
        self._apply_view_transform()
        self._view.refresh()

    @traced()
    def set_view_transform(self, transform: str) -> None:
        """Set the transform: 'normal', 'mirror_h', 'mirror_v' or 'mirror_hv'."""
        if transform == self._view_transform:
            return
        self._view_transform = transform
        self._view.mark_board_layer_dirty()
        self._apply_view_transform()
        self._view.refresh()

    @staticmethod
    @traced()
    def _compose_view_transform(*, h: bool, v: bool) -> str:
        if h and v:
            return "mirror_hv"
        if h:
            return "mirror_h"
        if v:
            return "mirror_v"
        return "normal"

    @traced()
    def _apply_view_transform(self) -> None:
        """Recompute the view's scale matrix from current state."""
        view = self._view
        port = self._port
        sx = -1.0 if self._has_mirror_h() else 1.0
        sy = -1.0 if self._has_mirror_v() else 1.0
        # Preserve current zoom magnitude, apply sign flips.  Clamp to
        # current viewport-derived limits so a window resize that shrank
        # the allowed window doesn't leave us out of range.
        mag = port.view_scale() or GRID_PX
        try:
            m11_min, m11_max = self._compute_zoom_limits()
            mag = max(m11_min, min(m11_max, mag))
        except Exception:  # noqa: BLE001 — session may be detached during shutdown
            pass
        # Remember the scene point currently at the viewport centre so we can
        # restore it after set_view_transform (which resets scroll position).
        center_scene = port.map_view_to_scene(port.viewport_rect().center)
        port.set_view_transform(
            Transform2D(sx * mag, 0.0, 0.0, sy * mag, 0.0, 0.0)
        )
        # Re-centre on the same scene point.
        port.center_on(center_scene)
        # Transform changed → bg cache pixmap is in the wrong space, but
        # the pixmap *content* is still valid — the bridge transform
        # handles the mirror.  Keep the anchor for a smooth visual jump.
        view.invalidate_bg_cache(keep_anchor=True)

    @traced()
    def _sync_tool_side(self, side: str) -> None:
        from protoboard.gui.tools.route_tool import RouteTool
        tool = self._view.active_tool
        if isinstance(tool, RouteTool):
            tool._side = side  # type: ignore[assignment]
        win = self._view.window()
        updater = getattr(win, "_set_wire_side", None)
        if updater is not None:
            updater(side)

    # Convenience toggles (used by menu/keyboard).  Each axis flips
    # independently so combining H + V produces ``mirror_hv``.
    @traced()
    def toggle_mirror_h(self) -> None:
        self.set_view_transform(self._compose_view_transform(
            h=not self._has_mirror_h(), v=self._has_mirror_v(),
        ))

    @traced()
    def toggle_mirror_v(self) -> None:
        self.set_view_transform(self._compose_view_transform(
            h=self._has_mirror_h(), v=not self._has_mirror_v(),
        ))

    @traced()
    def toggle_camera(self) -> None:
        self.set_camera_side(
            "bottom" if self._camera_side == "top" else "top"
        )

    # Legacy alias used by MainWindow._mirror_view (now means Mirror H).
    @traced()
    def toggle_mirror(self) -> None:
        self.toggle_mirror_h()

    # -- fit ------------------------------------------------------------------------

    @traced()
    def fit_board(self) -> None:
        """Scale and center the view to fit the board bounds."""
        view = self._view
        b = view.session.board.bounds
        self._port.fit_in_view(Rect(b.x - 1, b.y - 1, b.width + 2, b.height + 2))
        # fit_* / mirror: transform jumps but the pixmap content is still
        # valid — keep the anchor so the next paint can stretch-blit it
        # while the fresh rebuild happens.
        view.invalidate_bg_cache(keep_anchor=True)
        view.apply_label_visibility()

    @traced()
    def fit_area(self) -> None:
        """Fit view to the active group's components (ROOT → whole board)."""
        from protoboard.domain.layout.layout import ROOT_GROUP_ID

        view = self._view
        session = view.session
        active = view.active_group_id

        import uuid as _uuid
        if active == ROOT_GROUP_ID:
            comp_ids: frozenset[_uuid.UUID] = frozenset(
                session.layout.placements.keys()
            )
        else:
            comp_ids = session.group_tree().all_component_ids(active)

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
        self._port.fit_in_view(Rect(
            min(xs) - margin, min(ys) - margin,
            max(xs) - min(xs) + 2 * margin,
            max(ys) - min(ys) + 2 * margin,
        ))
        # fit_* / mirror: transform jumps but the pixmap content is still
        # valid — keep the anchor so the next paint can stretch-blit it
        # while the fresh rebuild happens.
        view.invalidate_bg_cache(keep_anchor=True)
        view.apply_label_visibility()

    @traced()
    def fit_selection(self) -> None:
        """Fit view to the bounding box of selected components and wires."""
        view = self._view
        tool = view.active_tool
        sel_comp_ids: set = getattr(tool, "_selected_ids", set())
        sel_wire_ids: set = getattr(tool, "_selected_wire_ids", set())

        if not sel_comp_ids and not sel_wire_ids:
            self.fit_area()
            return

        session = view.session
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
        self._port.fit_in_view(Rect(
            min(xs) - margin, min(ys) - margin,
            max(xs) - min(xs) + 2 * margin,
            max(ys) - min(ys) + 2 * margin,
        ))
        # fit_* / mirror: transform jumps but the pixmap content is still
        # valid — keep the anchor so the next paint can stretch-blit it
        # while the fresh rebuild happens.
        view.invalidate_bg_cache(keep_anchor=True)
        view.apply_label_visibility()

    # -- wheel zoom -------------------------------------------------------------------

    def on_wheel(self, inp: WheelInput) -> None:
        """Accumulate wheel ticks; defer the scale + scroll to a timer.

        Each ``view.scale()`` triggers a QGraphicsScene repaint, and at
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
        self._wheel_throttle.note((inp.delta_y, inp.view_pos))

    def _flush_wheel_zoom(self, parked: tuple[int, Point]) -> None:
        """Apply the accumulated wheel-zoom delta in one pass.

        Translates the buffered angleDelta into a number of asymmetric
        zoom steps (1.1 in, 1.0/1.08 out — preserved from the user's
        tuned values; see memory ``feedback_zoom_factor``) and applies
        the net factor as a single ``view.scale(...)``.  Cursor-pivot
        correction is identical to the legacy code path; the only
        difference is that *N ticks* of wheel input cost one paint
        instead of N.  *parked* is ``(summed delta, latest pivot)``.
        """
        view = self._view
        delta, pivot = parked
        if delta == 0:
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

        port = self._port
        old_scene = port.map_view_to_scene(pivot)
        self._scale_by(factor)
        if hit_floor:
            # Snap to the board's centre on the zoom-out floor so the
            # user doesn't end up parked over canvas margin.
            b = view.session.board.bounds
            port.center_on(Point(b.x + b.width / 2.0, b.y + b.height / 2.0))
        else:
            new_scene = port.map_view_to_scene(pivot)
            new_scale = port.view_transform().a
            port.pan_by(-((new_scene - old_scene) * new_scale))
        # Zoom changed the world→viewport transform; the cached pixmap
        # was rendered for the previous mapping.  Keep the anchor so a
        # follow-up tick of the same burst can stretch-blit it instead
        # of waiting for the new full-quality rebuild.
        view.invalidate_bg_cache(keep_anchor=True)
        # Defer the O(scene items) visibility sweep — same reasoning
        # as before, just relative to the (now batched) zoom paint.
        view.apply_label_visibility(deferred=True)

    def _scale_by(self, factor: float) -> None:
        """Multiply the view transform's linear part by *factor*.

        Equivalent to Qt's ``view.scale(factor, factor)`` (right-multiplied
        diagonal): scroll position is untouched — callers apply their own
        pivot correction via :meth:`ViewportPort.pan_by`.
        """
        t = self._port.view_transform()
        self._port.set_view_transform(Transform2D(
            t.a * factor, t.b * factor, t.c * factor, t.d * factor, t.tx, t.ty,
        ))

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
        view = self._view
        metrics = self._port.screen_metrics()
        # Physical DPI when the platform reports one; 96 headless.
        phys_dpi = metrics.dpi if metrics is not None else 96.0
        pitch_mm = float(view.session.board.raster_pitch_mm)
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
        b = view.session.board.bounds
        board_w = max(float(b.width), 1.0)
        board_h = max(float(b.height), 1.0)
        vp = self._port.viewport_rect()
        vp_w = max(vp.width, 1.0)
        vp_h = max(vp.height, 1.0)
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
        current = self._port.view_scale() or GRID_PX
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
        ``view.scale(...)`` zooms around scene origin (0,0) because the
        view's TransformationAnchor is ``NoAnchor`` — for wheel zoom we
        compensate around the cursor; here we compensate around the
        viewport centre so the user keeps looking at the same spot on the
        board.  Also invalidates the rasterised background which would
        otherwise stay frozen at the previous zoom level while scene
        items rescale.
        """
        view = self._view
        port = self._port
        if factor <= 0.0:
            return
        factor, hit_floor = self._clamp_zoom_factor(factor)
        if abs(factor - 1.0) < 1e-6 and not hit_floor:
            return  # already at limit — no-op
        pivot_vp = port.viewport_rect().center
        old_scene = port.map_view_to_scene(pivot_vp)
        self._scale_by(factor)
        if hit_floor:
            # Snap to board centre when hitting the zoom-out floor.
            b = view.session.board.bounds
            port.center_on(Point(b.x + b.width / 2.0, b.y + b.height / 2.0))
        else:
            new_scene = port.map_view_to_scene(pivot_vp)
            new_scale = port.view_transform().a
            port.pan_by(-((new_scene - old_scene) * new_scale))
        # Ctrl+/- zoom: keep the anchor so successive presses paint
        # cheaply during the burst, then settle to a fresh pixmap.
        view.invalidate_bg_cache(keep_anchor=True)
        view.apply_label_visibility(deferred=True)

    # -- pan burst / resize --------------------------------------------------------

    def note_pan_scroll(self) -> None:
        """Restart the pan-burst detector (called from ``scrollContentsBy``)."""
        self._pan_burst_timer.arm()

    def _on_pan_settled(self) -> None:
        """Force a paint right after a pan burst ends so the user sees the
        fresh full-quality pixmap immediately instead of waiting for the
        next event-driven repaint."""
        self._port.request_repaint()

    def on_resize(self) -> None:
        """Snap zoom back inside the (viewport-derived) limit window.

        Resizing changes the half-viewport-fit zoom-out floor; if the
        current zoom has slid below the new floor, snap back to it so
        the user doesn't end up with a board lost in canvas chrome.
        """
        try:
            m11_min, m11_max = self._compute_zoom_limits()
            current = self._port.view_scale()
            if current and (current < m11_min - 1e-6 or current > m11_max + 1e-6):
                self._apply_view_transform()
        except Exception:  # noqa: BLE001
            pass
