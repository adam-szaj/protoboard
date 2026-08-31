# SPDX-License-Identifier: MIT
"""PaintPipeline — BoardView's layered background composition + pixmap caches.

Extracted from ``BoardView`` (phase 2 of the decomposition).  Owns the two
viewport-sized pixmap caches and the stale-blit anchor:

* ``_bg_cache`` — the rasterised board layer (pads, holes, strip tracks,
  frames), painted by blitting in ``draw_background`` and never entering the
  scene's per-item paint dispatch.
* ``_back_cache`` — rasterised back-side scene items (z below ``Z_BOARD``);
  while valid, the corresponding scene items stay hidden so Qt does not paint
  them a second time on top of the board-mid blit.
* the *anchor* — the most recent fresh board pixmap + the viewport transform
  it was built under.  During a wheel-zoom / pan burst ``draw_background``
  blits the anchor under the inverse transform delta (cheap Qt-side
  scale/translate) instead of rebuilding the board pixmap every frame.

The Qt overrides (``paintEvent`` / ``drawBackground``) stay on ``BoardView``
and delegate here; the pipeline talks to the canvas only through
:class:`~protoboard.gui.board_view.BoardView`.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

import structlog
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QPainter, QPixmap, QTransform
from PySide6.QtWidgets import QGraphicsItem

if TYPE_CHECKING:
    from protoboard.gui.board_view import BoardView
    from protoboard.gui.renderers.board_renderer import BoardDisplayList

_click_log = structlog.get_logger("protoboard.gui.board_view.click")
_CLICK_PROFILE = os.environ.get("PROTOBOARD_CLICK_PROFILE") not in (None, "", "0")
# PROTOBOARD_PAINT_PROFILE — log per-phase paint timings on every paintEvent.
# Unlike PROTOBOARD_CLICK_PROFILE this is NOT gated on a recent mouse press,
# so it captures the ghost-follow paints in PlaceTool too.
_paint_log = structlog.get_logger("protoboard.gui.board_view.paint")
_PAINT_PROFILE = os.environ.get("PROTOBOARD_PAINT_PROFILE") not in (None, "", "0")


class PaintPipeline:
    """Layered background composition + bg/back pixmap caches."""

    def __init__(self, view: BoardView) -> None:
        self._view = view
        # Board layer: rebuilt only when board geometry / theme / alpha changes.
        self._bg_cache: QPixmap | None = None
        self._bg_cache_dirty: bool = True
        # ``BoardDisplayList`` is rebuilt alongside the cache pixmap but
        # cached separately so a viewport-only resize (no board change)
        # can re-rasterise without re-walking ``board.atoms()``.
        self._bg_display_list: BoardDisplayList | None = None
        # ``_back_cache`` holds the rasterised scene items whose zValue lies
        # in the back band (0..Z_BOARD).  ``_back_cache_dirty`` is set
        # whenever back-side items change, viewport resizes, or transform
        # changes.
        self._back_cache: QPixmap | None = None
        self._back_cache_dirty: bool = True
        # Scene items kept hidden while a valid _back_cache shows them
        # as a rasterised blit instead.  Restored on cache invalidation.
        self._back_hidden_items: list[QGraphicsItem] = []
        # Stale-blit anchor: the most recent _bg_cache + its viewportTransform
        # at the moment we built it.
        self._bg_cache_anchor: QPixmap | None = None
        self._bg_cache_anchor_transform: QTransform | None = None
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
            "back_cache_empty": 0,  # back items list empty, no pixmap built
            "blit_base": 0,
            "blit_back": 0,
            "blit_back_filter": 0,
            "blit_mid": 0,
            "anchor_blit": 0,
            "anchor_blit_back": 0,
        }
        # drawBackground wall time of the current paintEvent — paintEvent
        # subtracts it from its own total to get the scene front-pass time.
        self._paint_db_ms: float = 0.0

    # -- invalidation ----------------------------------------------------------

    def invalidate_bg_cache(self, *, keep_anchor: bool = False) -> None:
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

    def invalidate_back_cache(self) -> None:
        """Mark only the back-side cache stale."""
        self._back_cache_dirty = True

    def invalidate_display_list(self) -> None:
        """Drop the board display list (board / theme / alpha changed)."""
        self._bg_display_list = None

    # -- paintEvent bracketing ---------------------------------------------------

    def begin_paint_event(self) -> None:
        """Reset the per-paint drawBackground timer; count the paint."""
        self._paint_db_ms = 0.0  # filled by draw_background
        self._paint_counters["paintEvent"] += 1

    def end_paint_event(self, started_at: float, click_press_ts: float | None) -> None:
        """Record total paint wall time and emit the profile logs."""
        session = self._view.session
        paint_ms = (time.monotonic() - started_at) * 1000.0
        scene_pass_ms = max(0.0, paint_ms - self._paint_db_ms)
        # Persist total paint wall time on the perf metrics WITHOUT
        # touching the per-layer fields draw_background already filled in.
        session.perf.paint_ms = paint_ms
        if _PAINT_PROFILE:
            _paint_log.info(
                "paint_profile.boardview.paintEvent",
                total_ms=round(paint_ms, 2),
                drawbg_ms=round(self._paint_db_ms, 2),
                scene_pass_ms=round(scene_pass_ms, 2),
                bg_build_ms=round(session.perf.bg_build_ms, 2),
                back_build_ms=round(session.perf.back_build_ms, 2),
                blit_ms=round(session.perf.blit_ms, 2),
            )
        if _CLICK_PROFILE:
            if click_press_ts is not None and (time.monotonic() - click_press_ts) < 1.0:
                # Only log paints temporally close to a click — otherwise
                # every viewport update spams the console.
                since_press_ms = round((time.monotonic() - click_press_ts) * 1000, 1)
                _click_log.info(
                    "click_profile.boardview.paintEvent",
                    paint_ms=round(paint_ms, 1),
                    since_press_ms=since_press_ms,
                )

    # -- the layered background ---------------------------------------------------

    def draw_background(self, painter: QPainter) -> None:
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
        view = self._view
        _db_t0 = time.monotonic()
        self._paint_counters["drawBackground"] += 1
        _fr_t0 = time.monotonic()
        painter.save()
        painter.resetTransform()
        painter.fillRect(view.viewport().rect(), view.backgroundBrush())
        painter.restore()
        self._paint_counters["fillRect"] += 1
        _fill_ms = (time.monotonic() - _fr_t0) * 1000.0

        # Per-layer timings — surface in PerformancePanel.  Build times
        # come from the _ensure_* helpers (0.0 when cache was fresh),
        # blit_ms wraps the actual drawPixmap calls.
        bg_build_ms = 0.0
        back_build_ms = 0.0
        _blit_t0 = time.monotonic()

        in_burst = view.in_transform_burst()
        if in_burst and self._bg_cache_anchor is not None:
            # Cheap preview: blit the previous full-quality pixmap under
            # the inverse transform delta.  Qt handles the scale/translate
            # in the GPU/blit stage so the pan/zoom feels instant.  We
            # explicitly do NOT call ``_ensure_bg_cache`` here — that would
            # negate the whole point of the fast preview.
            anchor_t = self._bg_cache_anchor_transform
            assert anchor_t is not None
            cur_t = view.viewportTransform()
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
                painter.drawPixmap(0, 0, self._bg_cache_anchor)
                self._paint_counters["anchor_blit"] += 1
                # The back-side cache was rasterised under the SAME
                # anchor transform as the board pixmap, so the same
                # bridge maps it correctly — without this the back-side
                # items (back wires + group outlines, both zValue<Z_BOARD)
                # appear at the old transform while the rest of the
                # scene rescales, which looks like they're "drawn late".
                if self._back_cache is not None:
                    painter.setOpacity(1.0)
                    painter.drawPixmap(0, 0, self._back_cache)
                    self._paint_counters["anchor_blit_back"] += 1
                painter.restore()
                # Back-side filter + mid pad blit — kept in the burst
                # branch too so the picture stays consistent during pan
                # / zoom.  Without this the filter would "blink off" and
                # the mid pad blit would skip every burst frame, making
                # the board look noticeably brighter / lower-contrast
                # whenever the burst timer is active.
                burst_layers = view.layer_state()
                burst_filter_alpha = burst_layers.alpha_for_back_filter()
                burst_alpha_mid = burst_layers.alpha_for_board_mid()
                if burst_filter_alpha > 0.0:
                    from PySide6.QtGui import QColor as _QColor

                    filter_color = _QColor(view.theme.back_filter_color)
                    filter_color.setAlphaF(burst_filter_alpha)
                    # QGraphicsView calls drawBackground with the live
                    # viewport transform already applied.  resetTransform
                    # so the viewport rect maps to device pixels 1:1 —
                    # otherwise fillRect is mapped through the zoom and
                    # only fills a tiny scaled rectangle instead of the
                    # whole viewport.
                    painter.save()
                    painter.resetTransform()
                    painter.setOpacity(1.0)
                    painter.fillRect(view.viewport().rect(), filter_color)
                    painter.restore()
                    self._paint_counters["blit_back_filter"] += 1
                # Mid blit — re-paint the anchor pixmap through the same
                # bridge so pads sit on top of the filter, matching the
                # non-burst stack ordering.
                if burst_alpha_mid > 0.0:
                    painter.save()
                    painter.setTransform(bridge)
                    painter.setOpacity(burst_alpha_mid)
                    painter.drawPixmap(0, 0, self._bg_cache_anchor)
                    self._paint_counters["blit_mid"] += 1
                    painter.restore()
                painter.setOpacity(1.0)
            view.session.perf.record_paint(
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
            self._bg_cache_anchor_transform = view.viewportTransform()

        # Resolve current layer alphas — single LayerState round-trip per paint.
        layers = view.layer_state()
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
            painter.drawPixmap(0, 0, self._bg_cache)
            self._paint_counters["blit_base"] += 1
            _blit_base_ms = (time.monotonic() - _bt) * 1000.0
        if self._back_cache is not None:
            _bt = time.monotonic()
            painter.setOpacity(1.0)
            painter.drawPixmap(0, 0, self._back_cache)
            self._paint_counters["blit_back"] += 1
            _blit_back_ms = (time.monotonic() - _bt) * 1000.0
        # Back-side filter — half-transparent black wash painted between
        # back-side items and the mid board blit.  Sits over back wires /
        # back components but under the mid pad blit, so pads in FULL
        # mode stay readable while back-side items get visually pushed
        # away.  Painted on the entire viewport so back items that
        # overhang the board outline (e.g. a wire stub) still get the
        # filter; axis labels live at Z_OVERLAY and are repainted by Qt
        # on top of this sandwich so they keep their colour.
        if alpha_back_filter > 0.0:
            from PySide6.QtGui import QColor as _QColor

            filter_color = _QColor(view.theme.back_filter_color)
            filter_color.setAlphaF(alpha_back_filter)
            painter.setOpacity(1.0)
            # Painter is in identity / device coords here (the enclosing
            # save / resetTransform pair above the base blit took care
            # of that).  Use the live viewport rect rather than the
            # cache pixmap rect so the filter always covers the visible
            # area, even if the cache is briefly out of sync with a
            # recent resize.
            painter.fillRect(view.viewport().rect(), filter_color)
            self._paint_counters["blit_back_filter"] += 1
        if self._bg_cache is not None and alpha_mid > 0.0:
            _bt = time.monotonic()
            painter.setOpacity(alpha_mid)
            painter.drawPixmap(0, 0, self._bg_cache)
            self._paint_counters["blit_mid"] += 1
            _blit_mid_ms = (time.monotonic() - _bt) * 1000.0
        painter.setOpacity(1.0)
        painter.restore()

        blit_total_ms = (time.monotonic() - _blit_t0) * 1000.0
        view.session.perf.record_paint(
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
                vp_w=view.viewport().width(),
                vp_h=view.viewport().height(),
                counters=dict(self._paint_counters),
            )

    # -- cache builders ------------------------------------------------------------

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
        view = self._view
        session = view.session
        if session is None:
            return 0.0
        vp_size = view.viewport().size()
        if vp_size.width() <= 0 or vp_size.height() <= 0:
            return 0.0

        need_rebuild = (
            self._bg_cache_dirty
            or self._bg_cache is None
            or (self._bg_cache.size() != vp_size)
        )
        if not need_rebuild:
            return 0.0
        self._paint_counters["bg_cache_rebuild"] += 1
        _t0 = time.monotonic()

        # Build display list if missing (board / theme / alpha changes
        # clear _bg_display_list; pure viewport resize does not).
        if self._bg_display_list is None:
            from dataclasses import replace as _dc_replace

            from protoboard.gui.renderers.board_renderer import (
                build_board_display_list,
                build_pad_net_map,
            )

            _base = view.theme
            layers = view.layer_state()
            _theme = _dc_replace(
                _base,
                raster_pitch_mm=session.board.raster_pitch_mm,
                package_families=session.package_families,
            )
            # Reuse the same resolved colour map the wire renderer uses,
            # so a wire and the pad it lands on share an identical colour.
            from protoboard.gui.renderers.net_color import build_net_color_map

            pad_color_map = build_net_color_map(
                session.schematic.nets,
                theme=_theme,
            )
            pad_net_map = build_pad_net_map(
                session.layout.placements,
                session.instances,
                session.components,
                session.schematic,
                theme=_theme,
                net_color_map=pad_color_map,
            )
            self._bg_display_list = build_board_display_list(
                session.board,
                theme=_theme,
                layers=layers,
                pad_net_map=pad_net_map,
            )

        # Allocate a fresh pixmap matching the viewport device size.
        from protoboard.gui.renderers.board_renderer import paint_board

        pix = QPixmap(vp_size)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, view.antialiasing)
            # Apply the same world→viewport transform that QGraphicsView
            # uses for scene items so display-list coordinates land in
            # the right place on the pixmap.
            painter.setTransform(view.viewportTransform())
            paint_board(painter, self._bg_display_list)
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
        from protoboard.controller.view_options.layer_state import Z_BOARD

        view = self._view
        if view.session is None:
            return 0.0
        vp_size = view.viewport().size()
        if vp_size.width() <= 0 or vp_size.height() <= 0:
            return 0.0

        need_rebuild = (
            self._back_cache_dirty
            or self._back_cache is None
            or (self._back_cache.size() != vp_size)
        )
        if not need_rebuild:
            return 0.0
        self._paint_counters["back_cache_rebuild"] += 1
        _t0 = time.monotonic()

        # Restore previously-hidden items so a rebuild on changed content
        # picks up the new state.
        for it in self._back_hidden_items:
            try:
                it.setVisible(True)
            except RuntimeError:
                pass  # underlying C++ object gone
        self._back_hidden_items = []

        scene = view.scene()
        # Collect back-side items in scene Z order.  Skip child items (those
        # whose top-level parent group has a different effective z) — only
        # the parent's z determines stacking, and selecting orphaned
        # children would bake them at default z=0 even when their parent
        # sits well above Z_BOARD (e.g. PlaceTool ghost at z=20).
        back_items = [
            it
            for it in scene.items()
            if 0.0 <= it.zValue() < Z_BOARD - 1e-6 and it.parentItem() is None
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

        def _has_back_ancestor(it: QGraphicsItem) -> bool:
            # Walk up until a top-level item; back-cache should render the
            # whole subtree of any back-side group (e.g. a wire's outline +
            # core children), so children of back items must stay VISIBLE.
            parent = it.parentItem() if hasattr(it, "parentItem") else None
            while parent is not None:
                if id(parent) in back_set:
                    return True
                parent = parent.parentItem()
            return False

        temporarily_hidden: list[QGraphicsItem] = []
        try:
            for it in scene.items():
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
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, view.antialiasing)
            # Source rect = current visible scene area in scene coords.
            source = view.mapToScene(view.viewport().rect()).boundingRect()
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
            mirror_h, mirror_v = view.mirror_flags()
            sx = -1.0 if mirror_h else 1.0
            sy = -1.0 if mirror_v else 1.0
            if sx >= 0 and sy >= 0:
                target = QRectF(0.0, 0.0, vp_w, vp_h)
            else:
                # Build the inverted target by swapping left↔right or
                # top↔bottom so width/height stay positive but Qt's
                # source→target mapping picks up the negative scale.
                left = vp_w if sx < 0 else 0.0
                right = 0.0 if sx < 0 else vp_w
                top = vp_h if sy < 0 else 0.0
                bottom = 0.0 if sy < 0 else vp_h
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
            scene.render(painter, target, source)
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
