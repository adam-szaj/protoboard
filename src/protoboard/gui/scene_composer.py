# SPDX-License-Identifier: MIT
"""SceneComposer — rebuilds the QGraphicsScene from the session state.

Extracted from ``BoardView`` (phase 4 of the decomposition).  Owns the
persistent per-item scene registries (component / label / wire groups with
their fingerprints), the transient overlay items (wire pins, pin-pads, pin
modifiers, ratsnest + group outlines, axis labels), the board-layer dirty
flag, the refresh debounce timer, the drag-delta anchors and the live-drag
group translation.  ``BoardView.refresh()`` delegates here; the composer
talks to the canvas only through
:class:`~protoboard.gui.board_view.BoardView`.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, cast

from PySide6.QtCore import QRectF
from PySide6.QtWidgets import QGraphicsItem, QGraphicsItemGroup, QGraphicsScene

from protoboard.gui.renderers.component_renderer import (
    render_groups,
    update_component_groups,
)
from protoboard.gui.renderers.wire_renderer import (
    render_ratsnest,
    update_wire_groups_v2,
)
from protoboard.gui.view_camera import GRID_PX
from protoboard.services.debug_trace import traced
from protoboard.services.routing.net_status import routed_net_ids
from protoboard.geometry import Insets
from protoboard.gui.qt_geometry import to_qrectf

if TYPE_CHECKING:
    from protoboard.controller.theme import Theme
    from protoboard.domain.layout.layout import Layout
    from protoboard.domain.layout.pin_pad_classification import PinPadInfo
    from protoboard.geometry import Point
    from protoboard.gui.board_view import BoardView
    from protoboard.gui.layer_state import LayerState
    from protoboard.gui.session import EditorSession


class SceneComposer:
    """Scene (re)construction + live-drag group translation."""

    def __init__(self, view: BoardView) -> None:
        self._view = view
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
        # Connections (ratsnest) layer render mode — mirrors
        # ``AppSettings.ratsnest_mode``.  "full" = K_n graph per island;
        # "closest" = nearest-other-pin per pin.  MainWindow pushes
        # changes via ``set_ratsnest_mode`` so the dialog edit lands
        # without a full session reload.
        self._ratsnest_mode: str = "full"
        # Overlay items (ratsnest + group outlines) for targeted removal.
        self._overlay_items: list[object] = []
        # Debounce refresh so rapid layout_changed bursts only redraw once.
        from protoboard.gui.throttle import TimerWrap
        self._refresh_timer = TimerWrap(16, self.refresh)  # ~1 frame, arm-if-idle

    # -- small state hooks --------------------------------------------------------

    @traced()
    def set_ratsnest_mode(self, mode: str) -> None:
        """Switch the Connections layer between ``"full"`` (K_n graph)
        and ``"closest"`` (nearest-other-pin) line modes.  No-op when
        the mode is already active; otherwise refresh the overlay."""
        if mode not in ("full", "closest") or mode == self._ratsnest_mode:
            return
        self._ratsnest_mode = mode
        self.refresh()

    def mark_board_layer_dirty(self) -> None:
        """Force a board-layer rebuild on the next refresh()."""
        self._board_layer_dirty = True

    @traced()
    def clear_group_fingerprints(self) -> None:
        """Invalidate component/wire group caches (e.g. glow-ring rebuild)."""
        self._comp_fingerprints.clear()
        self._wire_fingerprints.clear()

    @traced()
    def schedule_refresh(self) -> None:
        """Schedule a refresh for the next event-loop tick (debounce)."""
        self._refresh_timer.arm()

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
        self._view.highlight.on_scene_cleared()

    # -- the refresh pass -----------------------------------------------------------

    def _render_layout(self, session: EditorSession) -> Layout:
        """The layout to draw: the armed transform's preview pose, else live.

        E5: an armed rotate/move/flip no longer mutates ``session.layout``; the
        float pose lives in the transform's preview layout.  The composer draws
        that while a transform is armed, so the live layout can stay pristine
        until ``accept`` commits a command.
        """
        armed = session.edit_controller.armed_transform
        if armed is not None and not armed.finished:
            return armed.preview_layout()
        return session.layout

    @traced()
    def refresh(self) -> None:
        """Update the scene to reflect current session state.

        On the warm path (most layout_changed events) only changed items are
        recreated; unchanged component/wire groups survive untouched.
        The board layer (pads/strips) is rebuilt only when its visual inputs change.

        Reads as a table of contents: each ``_*`` phase below owns one banner
        section of the old monolith.  The shared render inputs (theme / layers /
        net-colour map / routed set) are computed once here and threaded into
        the phases that need them, rather than living on ``self``.
        """
        view = self._view
        session = view.session
        scene = view.scene()
        _t0 = time.monotonic()
        self._refresh_timer.stop()
        view.rebuild_pad_cache()

        # While a transform is armed the session's layout stays pristine (E5);
        # the float pose lives in the armed transform's *preview* layout.  Draw
        # the components / wires / knots / ratsnest / groups from whichever is
        # the current pose — the preview when armed, else the live layout.
        render_layout = self._render_layout(session)

        # Shared render inputs, computed once and passed down to the phases.
        from dataclasses import replace as _dc_replace
        layers = view.layer_state()
        theme = _dc_replace(view.theme,
                            raster_pitch_mm=session.board.raster_pitch_mm,
                            package_families=session.package_families)
        from protoboard.gui.renderers.net_color import build_net_color_map
        # Resolved per-net colour: manual ``net.color`` wins, then static
        # palette, then golden-ratio hue spin once palette is exhausted.
        # Same map drives wire colour AND pad colour so they always match.
        net_color_map: dict[uuid.UUID, str | None] = dict(
            build_net_color_map(session.schematic.nets, theme=theme)
        )
        routed: set[uuid.UUID] | None = None
        if session.schematic.nets:
            routed = routed_net_ids(
                session.schematic, session.connectivity_graph()
            )

        self._sync_board_layer(session, scene, layers)
        self._clear_overlay_items(scene)
        self._render_wires(session, scene, render_layout, theme, layers,
                           net_color_map)
        self._render_wire_pins(session, scene, render_layout, theme, layers,
                              net_color_map)
        self._render_components(session, scene, render_layout, theme, layers,
                               routed)
        self._snapshot_drag_anchors(session)
        pp_records = self._render_pin_pads(session, scene, theme, layers,
                                           net_color_map)
        self._render_overlays(session, scene, render_layout, theme, layers,
                             routed)

        # ── Scene rect ───────────────────────────────────────────────────────
        b = session.board.bounds
        margin = Insets(top=b.ly * 0.5, right=b.lx * 0.5,
                        bottom=b.ly * 0.5, left=b.lx * 0.5)
        scene.setSceneRect(to_qrectf(b.inflate(margin)))

        # ── Label visibility (hide text that would be too small to read) ────────
        view.apply_label_visibility()

        self._render_pin_modifiers(session, scene, theme, layers, net_color_map,
                                  pp_records)

        # Scene contents changed — drop the back-side raster cache so the
        # next paint re-rasterises with current items.  Restores any items
        # we'd previously hidden so they're available to be re-rendered.
        view.invalidate_back_cache()

        # ── Final highlight pass ─────────────────────────────────────────────
        # ``update_component_groups`` no longer applies net-highlight dimming
        # (it would clobber the route-active profile during a wire drag).
        # The canonical highlight pass therefore has to run after every
        # refresh, otherwise Net Inspector / Selection highlights would be
        # lost the moment any unrelated refresh fires (drag, undo, theme
        # change, etc.).
        view.highlight.apply()

        _ms = (time.monotonic() - _t0) * 1000
        session.perf.record_refresh(_ms)
        session.perf.scene_items = len(scene.items())

    # -- refresh phases -------------------------------------------------------------

    @staticmethod
    def _rebuild_item_list(
        scene: QGraphicsScene,
        old_items: list[object],
        factory: Callable[[], Iterable[object]],
    ) -> list[object]:
        """Remove ``old_items`` from the scene, then render + return the new set.

        Collapses the repeated "clear a small overlay list, rebuild it from
        scratch" pattern shared by the wire-pin, pin-pad and pin-modifier
        phases.  ``factory`` renders the fresh items (it also adds them to the
        scene); its result is materialised into the returned list.
        """
        for it in old_items:
            try:
                scene.removeItem(cast("QGraphicsItem", it))
            except Exception:  # noqa: BLE001
                pass
        return list(factory())

    def _sync_board_layer(
        self, session: EditorSession, scene: QGraphicsScene, layers: LayerState,
    ) -> None:
        """Invalidate the board-layer caches when its visual inputs change.

        Board pads/strips/frames no longer live in the scene — they are
        rasterised into the paint pipeline's bg cache and blitted by
        ``drawBackground``.  On a geometry/theme/alpha change we clear the
        scene and drop both caches; on a placement-only change we keep the
        scene but drop the bg caches (the pad-net colour map depends on which
        pad each pin lands on).
        """
        view = self._view
        current_board_alpha = layers.alpha_for_board()
        if self._board_layer_dirty or current_board_alpha != self._last_board_alpha:
            scene.clear()
            self._invalidate_all_groups()
            view.tools.fire_on_scene_cleared()
            # Board state changed — drop both pixmap and display list so
            # the next drawBackground rebuilds from current board state.
            view.paint.invalidate_display_list()
            view.invalidate_bg_cache()
            self._board_layer_dirty = False
            self._last_board_alpha = current_board_alpha
            return
        # Placement-only change (drag-move commit, undo, paste, etc.):
        # don't tear down the scene, but the pad-net colour map depends
        # on which pad each pin lands on — drop the bg display list +
        # pixmap so they rebuild from the current placements.  Cheap
        # detection: compare per-component positions against the snapshot
        # taken at the end of the previous refresh.
        current_positions = {
            cid: (pl.position.x, pl.position.y)
            for cid, pl in session.layout.placements.items()
        }
        prev_positions = {
            cid: (p.x, p.y) for cid, p in self._drag_orig_group_pos.items()
        }
        if current_positions != prev_positions:
            view.paint.invalidate_display_list()
            view.invalidate_bg_cache()

    def _clear_overlay_items(self, scene: QGraphicsScene) -> None:
        """Remove stale overlay items (ratsnest + group outlines)."""
        for item in self._overlay_items:
            if not isinstance(item, QGraphicsItem):
                continue
            try:
                if item.scene() is scene:
                    scene.removeItem(item)
            except RuntimeError:
                pass  # C++ object already deleted by scene.clear()
        self._overlay_items.clear()

    def _render_wires(
        self, session: EditorSession, scene: QGraphicsScene, render_layout: Layout,
        theme: Theme, layers: LayerState, net_color_map: dict[uuid.UUID, str | None],
    ) -> None:
        """Wires (group reuse) — unchanged wire groups survive untouched.

        Fragment-aware path: each fragment paints on its own side (alpha+Z)
        with a via marker at every side transition, so a flipped fragment
        is visible immediately.
        """
        update_wire_groups_v2(
            scene,
            list(render_layout.wires),
            session.cached_wire_net_map(),
            self._wire_groups,        # type: ignore[arg-type]
            self._wire_fingerprints,
            net_color_map=net_color_map,
            theme=theme,
            layers=layers,
            highlighted_net_id=self._view.highlight.effective_highlighted_net(),
            routing_target_net_id=session.routing_net_id,
        )

    def _render_wire_pins(
        self, session: EditorSession, scene: QGraphicsScene, render_layout: Layout,
        theme: Theme, layers: LayerState, net_color_map: dict[uuid.UUID, str | None],
    ) -> None:
        """Wire-pin glyphs — rebuilt from scratch each refresh (tiny set)."""
        from protoboard.domain.layout.knot import WirePinKnot
        from protoboard.gui.renderers.wire_renderer import render_wire_pins

        wire_pins: list[tuple[Point, uuid.UUID | None]] = [
            (k.position, k.net_id)
            for k in render_layout.knots.values()
            if isinstance(k, WirePinKnot)
        ]
        self._wire_pin_items = self._rebuild_item_list(
            scene, self._wire_pin_items,
            lambda: render_wire_pins(
                scene,
                wire_pins,
                net_color_map=net_color_map,
                theme=theme,
                layers=layers,
                routing_target_net_id=session.routing_net_id,
            ) if wire_pins else (),
        )

    def _render_components(
        self, session: EditorSession, scene: QGraphicsScene, render_layout: Layout,
        theme: Theme, layers: LayerState, routed: set[uuid.UUID] | None,
    ) -> None:
        """Components (group reuse) — unchanged comp groups survive untouched."""
        update_component_groups(
            scene,
            render_layout.placements,
            session.instances,
            session.components,
            self._comp_groups,        # type: ignore[arg-type]
            self._comp_fingerprints,
            schematic=session.schematic if session.schematic.nets else None,
            routed_nets=routed,
            theme=theme,
            layers=layers,
            active_group_id=self._view.active_group_id,
            layout=render_layout,
            highlighted_net_id=self._view.highlight.effective_highlighted_net(),
            existing_label_groups=self._comp_label_groups,  # type: ignore[arg-type]
        )

    def _snapshot_drag_anchors(self, session: EditorSession) -> None:
        """Snapshot component + wire poses so ``on_drag_moved`` can compute deltas.

        Anchor to the *live* (committed) pose: the glide path (DragController)
        never runs during an armed transform, and anchoring to the live pose
        keeps the next refresh's position-change detector comparing like with
        like.  The per-wire anchor is the first point of the wire's path at the
        time the scene group was built; live drag mutates ``wire.path`` in place
        (translation only for group drag, free-form for single-wire drag) and
        ``on_drag_moved`` compares current ``path[0]`` against this anchor.
        """
        self._drag_orig_group_pos = {
            cid: pl.position
            for cid, pl in session.layout.placements.items()
        }
        self._drag_orig_wire_pose = {
            w.id: w.path[0] for w in session.layout.wires if w.path
        }

    def _render_pin_pads(
        self, session: EditorSession, scene: QGraphicsScene, theme: Theme,
        layers: LayerState, net_color_map: dict[uuid.UUID, str | None],
    ) -> list[PinPadInfo]:
        """Pin-pads layer (schematic-aware overlay above board).

        Cherry-on-top of the board layer: every pad carrying a net is repainted
        in the net colour with the modifier shape (pin1 / exported / star)
        carved out.  Routing target gets neon yellow.  Returns the classified
        pin-pad records so the caller can feed the pin-modifier phase and the
        highlight pass without recomputing the classification.
        """
        pp_records: list[PinPadInfo] = []
        try:
            from protoboard.domain.layout.pin_pad_classification import (
                classify_pin_pads,
            )
            from protoboard.gui.renderers.pin_pad_renderer import (
                render_pin_pads,
            )
            pp_records = list(classify_pin_pads(
                session.pad_index(), session.layout,
            ))
            self._pin_pad_items = self._rebuild_item_list(
                scene, self._pin_pad_items,
                lambda: render_pin_pads(
                    scene,
                    pp_records,
                    theme=theme,
                    layers=layers,
                    net_color_map=net_color_map,
                    routing_target_net_id=session.routing_net_id,
                ),
            )
        except Exception:  # noqa: BLE001
            # Defensive: never let pin-pads classification glitch crash
            # the whole paint pipeline.  The layer is purely visual.
            self._rebuild_item_list(scene, self._pin_pad_items, tuple)
            self._pin_pad_items = []
            pp_records = []
        # Cache the records so the highlight pass can draw bright net-pad discs
        # without recomputing the classification.
        self._view.highlight.set_pin_pad_records(list(pp_records))
        return pp_records

    def _render_overlays(
        self, session: EditorSession, scene: QGraphicsScene, render_layout: Layout,
        theme: Theme, layers: LayerState, routed: set[uuid.UUID] | None,
    ) -> None:
        """Overlays — ratsnest + group outlines + axis labels (always recreated)."""
        new_overlays: list[object] = []
        from protoboard.gui.layer_state import LayerVisibility
        ratsnest_vis = self._view.layer_overrides.ratsnest
        if (session.schematic.nets
                and ratsnest_vis != LayerVisibility.HIDDEN):
            # Ghost mode: only unrouted nets are drawn (current
            # legacy behaviour).  Full mode: every net gets at least
            # its missing-link or multi-island warning lines.
            ghost_mode = (ratsnest_vis == LayerVisibility.GHOST)
            new_overlays.extend(render_ratsnest(
                scene,
                session.schematic,
                render_layout.placements,
                session.instances,
                session.components,
                routed_nets=routed,
                theme=theme,
                graph=session.connectivity_graph(),
                mode=self._ratsnest_mode,
                ghost=ghost_mode,
            ))
        if render_layout.groups:
            new_overlays.extend(render_groups(
                scene,
                render_layout,
                render_layout.placements,
                session.instances,
                session.components,
                wires=list(render_layout.wires),
                theme=theme,
                active_group_id=self._view.active_group_id,
                view_scale=GRID_PX,
                wire_net_map=session.cached_wire_net_map(),
                schematic=session.schematic,
            ))
        if session.axis_label_settings.any_enabled():
            from protoboard.gui.renderers.axis_labels_renderer import (
                render_axis_labels,
            )
            new_overlays.extend(render_axis_labels(
                scene,
                session.board,
                session.axis_label_settings,
                theme=theme,
                alpha=layers.alpha_for_axis_labels(),
                view_scale=GRID_PX,
            ))
        self._overlay_items = new_overlays

    def _render_pin_modifiers(
        self, session: EditorSession, scene: QGraphicsScene, theme: Theme,
        layers: LayerState, net_color_map: dict[uuid.UUID, str | None],
        pp_records: list[PinPadInfo],
    ) -> None:
        """Pin modifiers (top-most overlay).

        Repeats pin1 / exported / star glyphs above wires + components in the
        net colour so the marker remains visible when something covers the
        underlying pin-pad.  Suppressed while a wire is being edited: the
        editable vertex handles sit above the modifiers, so the markers would
        clutter / occlude the handles the user is dragging.
        """
        tool = self._view.active_tool
        # ``is_wire_editing`` is WireEditTool-specific — not on the _Tool
        # protocol; probe it dynamically (a non-wire tool simply lacks it).
        _is_wire_editing = getattr(tool, "is_wire_editing", None)
        _wire_editing = bool(_is_wire_editing() if callable(_is_wire_editing) else False)
        if not (pp_records and not _wire_editing):
            self._rebuild_item_list(scene, self._pin_modifier_items, tuple)
            self._pin_modifier_items = []
            return
        try:
            from protoboard.gui.renderers.pin_pad_renderer import (
                render_pin_modifiers,
            )
            self._pin_modifier_items = self._rebuild_item_list(
                scene, self._pin_modifier_items,
                lambda: render_pin_modifiers(
                    scene,
                    pp_records,
                    theme=theme,
                    layers=layers,
                    net_color_map=net_color_map,
                    routing_target_net_id=session.routing_net_id,
                ),
            )
        except Exception:  # noqa: BLE001
            self._pin_modifier_items = []

    # -- live drag ---------------------------------------------------------------

    @traced()
    def on_drag_moved(self) -> None:
        """Live drag: move scene groups to current placement positions (no rebuild).

        Items inside each group are rendered at absolute world coordinates, so the
        group itself sits at (0,0).  We track the *last rendered* position per group
        and call moveBy(delta) when the placement changes — this translates all child
        items without rebuilding the scene.
        """
        session = self._view.session
        placements = session.layout.placements
        for comp_id, grp in self._comp_groups.items():
            if not isinstance(grp, QGraphicsItemGroup):
                continue
            pl = placements.get(comp_id)
            orig = self._drag_orig_group_pos.get(comp_id)
            if pl is None or orig is None:
                continue
            # Delta from the position when items were baked into the scene.
            delta = pl.position - orig
            grp.setPos(delta.dx, delta.dy)
            # Move the matching label group by the same delta so labels track
            # the drag (they live in a separate group at Z_LABELS_TOP).
            lbl = self._comp_label_groups.get(comp_id)
            if isinstance(lbl, QGraphicsItemGroup):
                lbl.setPos(delta.dx, delta.dy)

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
        for wire in session.layout.wires:
            grp = self._wire_groups.get(wire.id)
            if not isinstance(grp, QGraphicsItemGroup) or not wire.path:
                continue
            orig = self._drag_orig_wire_pose.get(wire.id)
            if orig is None:
                continue
            delta = wire.path[0] - orig
            grp.setPos(delta.dx, delta.dy)

        self._update_group_outlines_on_drag()

    @traced()
    def _update_group_outlines_on_drag(self) -> None:
        """Recompute each group's outline rect from live member geometry.

        Outline overlay rects live in ``self._overlay_items`` (not the moved
        comp groups), so a drag's ``setPos`` doesn't touch them — this resizes
        each in place to the bbox of its members' pins plus owned-wire
        vertices, so the live outline matches the statically-rendered one
        instead of shrinking at drag-start and re-growing at release.  Pulled
        out of ``on_drag_moved`` so that method reads as "move groups, move
        wires, refresh outlines".
        """
        from PySide6.QtWidgets import QGraphicsRectItem

        from protoboard.domain.layout.group_ownership import OwnershipResolver
        from protoboard.domain.layout.group_tree import GroupTree
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        session = self._view.session
        layout = session.layout
        # Used by the OwnershipResolver below (group-outline ownership classifier).
        placements = layout.placements
        instances = session.instances
        components = session.components
        theme = self._view.theme
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
            wire_net_map = (
                session.cached_wire_net_map() if session is not None else {}
            )
        except Exception:  # noqa: BLE001
            wire_net_map = {}
        schematic = (
            getattr(session, "schematic", None) if session is not None else None
        )
        _tree = GroupTree(layout.groups)
        # Bind the LOCAL outline context (may be a drag-time snapshot, not
        # session state) — build the resolver directly, not from_session.
        _ownership = OwnershipResolver(
            knots=layout.knots,
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
                for _pn, p in session.iter_pin_positions(cid):
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
