# SPDX-License-Identifier: MIT
"""Select tool — click to select, drag to move, R to rotate, Del to delete.

Multi-selection:
  Drag left-to-right (rubber band) = only components fully inside the rect.
  Drag right-to-left = all components whose bbox intersects the rect.
  Ctrl+click = add/remove one component to/from the selection.

Wire editing:
  After selecting a single wire, Ctrl+click on a node dot marks the edit
  start.  Ctrl+click on a second node marks the end.  Then:
    - Press A to auto-route the fragment via A*.
    - Or click new intermediate points and press Enter.
  Esc cancels the edit and restores the original wire.
"""

from __future__ import annotations

import math
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from protoboard.gui.selection_axes import (
        RelationClass,
        RelationProfile,
        SelectionFilter,
    )
    from protoboard.gui.selection_fsm import BasisScope, Hit, SetOp

import os
import time

import structlog
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QKeyEvent, QMouseEvent, QPen

# Per-click timing logs.  Enabled with ``PROTOBOARD_CLICK_PROFILE=1``.
# Off by default so we don't spam the console in normal runs.
_CLICK_PROFILE = os.environ.get("PROTOBOARD_CLICK_PROFILE") not in (None, "", "0")
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsItemGroup,
    QGraphicsLineItem,
    QGraphicsRectItem,
)

from protoboard.domain.commands.base import MacroCommand
from protoboard.domain.commands.component_cmds import (
    ComponentState,
    MoveComponentCommand,
    RemoveComponentCommand,
    RemoveWireCommand,
    ReplaceWireFragmentsCommand,
)
from protoboard.domain.commands.drag_cmds import MoveDragSetCommand
from protoboard.gui.drag_preferences import (
    DragPreferences,
    DragSessionOverrides,
)
from protoboard.domain.layout.group import (
    ExportedPin,
    all_component_ids,
    top_level_group_for,
)
from protoboard.domain.layout.placement import component_bbox, flip_component, pin_world_position, unrotate
from protoboard.domain.layout.wire import Fragment, Wire
from protoboard.domain.layout.wire_utils import rebuild_fragments_from_path


def _replace_wire_path_cmd(
    layout: object,
    wire: Wire,
    new_path: list[Point],
    old_path: list[Point] | None = None,
) -> "ReplaceWireFragmentsCommand":
    """Build a ReplaceWireFragmentsCommand for an in-place path edit.

    Ensures the wire has v2 endpoint knots, rebuilds fragments from
    ``new_path`` along the existing via boundaries, and snapshots
    ``old_path`` (defaulting to a single-fragment view of the current
    wire.side) into ``old_fragments`` for undo.  Callers that mutated
    wire.path in place before issuing the command must pass the
    pre-mutation path as ``old_path``.
    """
    layout.ensure_wire_knots(wire)  # type: ignore[attr-defined]
    if old_path is None:
        old_fragments = tuple(
            Fragment(path=list(f.path), side=f.side) for f in wire.fragments
        )
    elif len(wire.fragments) <= 1:
        old_fragments = (Fragment(path=list(old_path), side=wire.side),)
    else:
        # Multi-fragment wire whose path was changed in place — derive the
        # pre-edit fragments from the old path.  Same boundary positions
        # as the current fragments (drag preserves vias).
        old_fragments = tuple(
            rebuild_fragments_from_path(wire, list(old_path))
        )
    new_fragments = tuple(rebuild_fragments_from_path(wire, list(new_path)))
    return ReplaceWireFragmentsCommand(
        wire_id=wire.id,
        new_fragments=new_fragments,
        old_fragments=old_fragments,
    )
from protoboard.geometry import Point
from protoboard.gui.board_view import _Tool
from protoboard.gui.panels.properties_panel import PropertiesPanel

_SELECT_PEN = QPen(Qt.GlobalColor.cyan, 0.08)
_SELECT_PEN.setStyle(Qt.PenStyle.DashLine)
_MULTI_SELECT_PEN = QPen(Qt.GlobalColor.cyan, 0.06)
_MULTI_SELECT_PEN.setStyle(Qt.PenStyle.DashLine)


def _make_select_pen(color_str: str, width: float) -> QPen:
    pen = QPen(QColor(color_str), width)
    pen.setStyle(Qt.PenStyle.DashLine)
    return pen
_RUBBER_BAND_PEN_LR = QPen(QColor(0, 200, 255, 180), 0.06)   # left-to-right: solid
_RUBBER_BAND_PEN_RL = QPen(QColor(0, 200, 255, 180), 0.06)   # right-to-left: dashed

# Minimum scene-space distance (raster units) the cursor must travel
# from the press point before on_move starts mutating placements/wire
# paths.  A bare click on a pin keeps a few sub-raster jitter pixels;
# that must not be misread as the start of a drag.  Half a raster
# (0.5) is below the snap step so any genuine drag immediately
# crosses it.
_DRAG_START_THRESHOLD = 0.5
# Long-press timing for group selection.  500ms of stationary press on
# the empty area of a group's bbox selects the group as a whole;
# holding for the full 1000ms additionally advances into that group's
# edit mode.  Mouse motion above _GROUP_LONGPRESS_CANCEL_PX (viewport
# pixels) before either timer fires cancels the long-press cleanly.
_GROUP_LONGPRESS_SELECT_MS = 500
_GROUP_LONGPRESS_EDIT_MS = 1000
_GROUP_LONGPRESS_CANCEL_PX = 6
_RUBBER_BAND_PEN_RL.setStyle(Qt.PenStyle.DashLine)
_RUBBER_BAND_FILL_LR = QColor(0, 200, 255, 20)
_RUBBER_BAND_FILL_RL = QColor(0, 200, 255, 10)
_WIRE_SELECT_PEN = QPen(QColor(255, 220, 0, 200), 0.38)
_WIRE_SELECT_PEN.setCapStyle(Qt.PenCapStyle.RoundCap)
_WIRE_SELECT_PEN.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
_NODE_PEN = QPen(Qt.GlobalColor.yellow, 0.04)
_NODE_BRUSH = QColor(255, 220, 0, 200)
_NODE_SELECTED_PEN = QPen(Qt.GlobalColor.red, 0.06)
_NODE_SELECTED_BRUSH = QColor(255, 80, 0, 230)
_EDIT_PREVIEW_PEN = QPen(QColor(255, 120, 0, 200), 0.12)
_EDIT_PREVIEW_PEN.setStyle(Qt.PenStyle.DashLine)

_HIT_RADIUS = 0.6
_BBOX_MARGIN = 0.45
_WIRE_HIT_DIST = 0.35
_NODE_HIT_RADIUS = 0.32
_NODE_RADIUS = 0.13

_log = structlog.get_logger(__name__)


def _dbg_enabled() -> bool:
    """Cheap gate for hot-path debug logs.

    structlog forwards to stdlib logging, so the stdlib level filter is
    authoritative.  Guarding ``_log.debug(...)`` calls whose payload
    builds list/dict comprehensions avoids paying that cost when the
    logger is off (drag overlay runs at ~60 fps; an unguarded debug
    payload with 6 comprehensions burns measurable cycles).
    """
    import logging
    return logging.getLogger(__name__).isEnabledFor(logging.DEBUG)


class SelectTool(_Tool):
    """Click/rubber-band to select; drag to move; R to rotate; Del to delete."""

    def __init__(self) -> None:
        super().__init__()
        # Single component selection (for Properties panel and drag).
        self._selected_id: uuid.UUID | None = None
        self._highlight: QGraphicsRectItem | None = None
        self._origin_cross: list[QGraphicsLineItem] = []
        # Multi-selection.
        self._selected_ids: set[uuid.UUID] = set()
        self._multi_highlights: list[QGraphicsRectItem] = []
        # Drag state (works for both single and multi).
        self._dragging = False
        self._drag_orig_positions: dict[uuid.UUID, Point] = {}
        self._drag_orig_wire_paths: dict[uuid.UUID, list[Point]] = {}
        self._drag_anchor_offset: tuple[float, float] = (0.0, 0.0)
        # Scene-space point captured on LPM press.  on_move ignores
        # cursor motion until the user has dragged ~half a raster from
        # this point, so a plain click on a pin (small jitter) doesn't
        # mutate anything.  Cleared on release.
        self._drag_press_scene_pt: QPointF | None = None
        # Latched once the threshold has been crossed for the current
        # drag — prevents the gate from re-arming if the user wiggles
        # back to within the threshold mid-drag.
        self._drag_threshold_crossed: bool = False
        # Phase 2b: armed when on_press lands LMB on the *exact* primary
        # (`hit == self._selected_id` for components, same for wires).
        # If on_release fires before the drag threshold is crossed, the
        # release path treats this as a same-object click and pops one
        # FSM layer (SELECTION → HIGHLIGHT).  Cleared in on_move when
        # the threshold trips so a drag never doubles as a deselect.
        self._pending_deselect_on_release: bool = False
        # Blocking pad positions captured by _build_group_drag_commands
        # when a group drag would land on a different-net pin.  Consumed
        # by on_release to drive the floating-state HARD UX, then reset.
        self._group_drag_block_pads: list[Point] = []
        # Rubber-band state.
        self._rubber_band_start: QPointF | None = None
        self._rubber_band_item: QGraphicsRectItem | None = None
        # Throttle: mouseMoveEvent fires up to ~120 Hz on a fast trackpad
        # and every move that mutates the QGraphicsScene triggers an
        # X11 round-trip — fatal over SSH.  We coalesce moves into a
        # single repaint per ~30 fps interval.  Latest scene-pt is
        # parked on ``_pending_rubber_band_pt``; the timer reads it
        # once when it fires, then clears it.  ``_finish_rubber_band``
        # flushes any pending pt synchronously so the final rect
        # matches the cursor's release position exactly.
        self._rubber_band_timer: QTimer | None = None
        self._pending_rubber_band_pt: QPointF | None = None
        # Cache the last LR/RL direction so we only re-apply the pen
        # and brush when the rubber-band actually flips orientation,
        # avoiding an unconditional setPen/setBrush per frame.
        self._rubber_band_last_lr: bool | None = None
        # Wire selection (single — for editing).
        self._selected_wire_id: uuid.UUID | None = None
        self._wire_highlight: QGraphicsItemGroup | None = None
        # Multi-wire selection.
        self._selected_wire_ids: set[uuid.UUID] = set()
        self._wire_multi_highlights: list[QGraphicsItemGroup] = []
        # Wire click cycling: track last-clicked wire and cycle count.
        # 1st click = full net + pads, 2nd = full net only, 3rd = segment only.
        self._wire_click_id: uuid.UUID | None = None   # wire hit on last click
        self._wire_click_cycle: int = 0                # 1, 2, or 3
        # Net-pad highlights shown on cycle 1 (red circles on connected pins).
        self._net_pad_highlight_items: list[QGraphicsEllipseItem] = []
        self._net_pad_highlight_positions: list[Point] = []
        # Wire segment editing.
        self._edit_start_wire_id: uuid.UUID | None = None  # wire containing the start node
        self._edit_end_wire_id: uuid.UUID | None = None    # wire containing the end node (may differ)
        self._edit_start_idx: int | None = None
        self._edit_end_idx: int | None = None
        self._edit_points: list[Point] = []
        self._edit_preview_items: list[QGraphicsLineItem] = []
        self._edit_live_line: QGraphicsLineItem | None = None
        # Group navigation stack: list of group IDs currently entered (innermost last).
        # Starts at root group; never empty — use _current_group_id for safe access.
        from protoboard.domain.layout.layout import ROOT_GROUP_ID as _ROOT_GID
        self._group_stack: list[uuid.UUID] = [_ROOT_GID]
        # Set when the user clicks a group outline — enables group-as-monolith rotation.
        self._selected_group_id: uuid.UUID | None = None
        # Re-drag state (Ctrl+R on a component with bendable_pins).
        self._redrag_comp_id: uuid.UUID | None = None
        self._redrag_pin_num: int | None = None
        self._redrag_items: list[QGraphicsItem] = []
        # Live drag conflict overlay.
        from protoboard.gui.tools.drag_conflict_overlay import DragConflictOverlay
        self._drag_overlay = DragConflictOverlay()
        # Connectivity overlay state (pad markers, neighbor rects).  Dimming
        # itself is driven by the HighlightStack auto-entry pushed from
        # _refresh_auto_highlight(); these items are only the overlay graphics.
        self._highlight_net_id: uuid.UUID | None = None
        self._highlight_comp_ids: set[uuid.UUID] = set()
        self._highlight_items: list[QGraphicsItem] = []
        # Orthogonal axes driving click semantics.
        from protoboard.gui.selection_axes import RelationClass, SelectionFilter
        self._filter: SelectionFilter = SelectionFilter.ALL
        self._relation: RelationClass = RelationClass.SAME_LOGICAL_NET
        # Explicit click-FSM (docs/highlight-state-machine.txt).  The
        # FSM mirrors what the legacy selection code does, then drives
        # the per-layer pop semantics for Esc + deselect-on-same-object.
        # Full Shift+extends migration is Phase 2b — for now the FSM
        # shadows legacy state and exposes step_esc / step_lmb hooks
        # used by the Esc handler and the on_press "click same primary
        # deselects" branch.
        from protoboard.gui.selection_fsm import SelectionFSM
        self._fsm: SelectionFSM = SelectionFSM(
            same_relation=self._fsm_same_relation,
            initial_relation=self._relation,
        )
        # Last click source — used for auto-highlight refresh after relation change.
        self._hl_source_wire: uuid.UUID | None = None
        self._hl_source_comp: uuid.UUID | None = None
        self._hl_source_pin: int | None = None
        # Floating state: component placed but not yet committed to CommandStack.
        self._floating_ids: set[uuid.UUID] = set()
        self._floating_orig_placements: dict[uuid.UUID, ComponentPlacement] = {}
        self._floating_extra_cmds: list[object] = []
        self._floating_dragged_wire_ids: frozenset[uuid.UUID] = frozenset()
        self._floating_highlight_items: list[QGraphicsItem] = []
        # Drag-commit decision pipeline: classifier result + corner text +
        # debounced full-validation timer.  The cheap inline pad-pad check in
        # _update_drag_overlay still runs every snap for instant red feedback;
        # the timer adds a full classify_drag_commit pass 1 s after the last
        # snap change so soft conflicts surface without slowing the drag.
        from protoboard.gui.tools.drag_corner_text import DragCornerText
        self._corner_text = DragCornerText()
        self._drag_last_decision: object | None = None  # DragCommitDecision | None
        self._floating_decision: object | None = None   # DragCommitDecision | None
        self._floating_severity: str = "clear"          # "clear" | "soft" | "hard"
        # Per-session "Don't ask again" overrides for unified drag model
        # soft-conflict resolution (2026-06-02).  Reset on app restart;
        # never persisted.
        self._drag_session_overrides: DragSessionOverrides = (
            DragSessionOverrides()
        )
        self._drag_timer: QTimer | None = None  # built lazily on first drag
        # PadIndex trial state — cloned from session at drag-start and
        # mutated incrementally per snap so we don't rebuild the full
        # index on every grid step (~3 ms × N snaps becomes ~3 ms +
        # ~0.2 ms × N).  None when no drag is active.
        self._drag_pad_index: object | None = None

        # Long-press group-selection state.  When the user presses on a
        # group's bbox empty area, we start a 500ms timer (phase=select).
        # On fire we select the group and start a second 500ms timer
        # (phase=edit) — on its fire we enter group edit mode.  Mouse
        # motion above _GROUP_LONGPRESS_CANCEL_PX before either timer
        # fires cancels the long-press.
        self._longpress_timer: QTimer | None = None
        self._longpress_phase: str = "idle"   # "idle" | "select" | "edit"
        self._longpress_group_id: uuid.UUID | None = None
        self._longpress_press_view_pt: QPoint | None = None

    # ------------------------------------------------------------------
    # _Tool interface
    # ------------------------------------------------------------------

    @property
    def _current_group_id(self) -> uuid.UUID:
        """Return top of group stack, repairing empty stack defensively."""
        if not self._group_stack:
            from protoboard.domain.layout.layout import ROOT_GROUP_ID
            self._group_stack = [ROOT_GROUP_ID]
        return self._group_stack[-1]

    def activate(self, session: object, view: object) -> None:
        super().activate(session, view)  # type: ignore[arg-type]
        # Ensure group stack is never empty (guard against stale instances).
        if not self._group_stack:
            from protoboard.domain.layout.layout import ROOT_GROUP_ID
            self._group_stack = [ROOT_GROUP_ID]

    def _notify_selection(self) -> None:
        """Broadcast current selection state to all panels via session signal."""
        if self._session is None:
            return
        comp_ids: frozenset[uuid.UUID] = frozenset(
            self._selected_ids | ({self._selected_id} if self._selected_id else set())
        )
        wire_ids: frozenset[uuid.UUID] = frozenset(
            self._selected_wire_ids | (
                {self._selected_wire_id} if self._selected_wire_id else set()
            )
        )
        self._session.notify_selection(comp_ids, wire_ids, self._highlight_net_id)

    # ------------------------------------------------------------------
    # Selection filter (which object types may be selected)
    # ------------------------------------------------------------------

    def set_filter(self, f: "SelectionFilter") -> None:
        """Replace the selection filter wholesale."""
        from protoboard.gui.selection_axes import SelectionFilter
        if not isinstance(f, SelectionFilter):
            return
        self._filter = f

    @property
    def filter(self) -> "SelectionFilter":
        return self._filter

    def _filter_components(self) -> bool:
        from protoboard.gui.selection_axes import SelectionFilter
        return bool(self._filter & SelectionFilter.COMPONENTS)

    def _filter_wires(self) -> bool:
        from protoboard.gui.selection_axes import SelectionFilter
        return bool(self._filter & SelectionFilter.WIRES)

    def _filter_groups(self) -> bool:
        from protoboard.gui.selection_axes import SelectionFilter
        return bool(self._filter & SelectionFilter.GROUPS)

    def _filter_mutable_ids(self, ids: "set[uuid.UUID] | frozenset[uuid.UUID] | list[uuid.UUID]") -> "set[uuid.UUID]":
        """Drop components that are locked inside a group from a mutation set.

        Used by every layout-mutating action (drag, delete, rotate, flip) so
        a selection that mixes free and grouped components only affects the
        free ones.  Pure-selection operations (highlight, properties) don't
        call this — grouped components stay selectable.
        """
        return {cid for cid in ids if not self._is_locked_in_group(cid)}

    def _is_locked_in_group(self, comp_id: uuid.UUID) -> bool:
        """True iff *comp_id* belongs to a group that the user is currently
        viewing from the outside.

        A group is a "hand-crafted IC": from the outside it's a sealed
        package — selection (for net inspection, properties) is allowed,
        but layout-mutating operations (drag, delete, rotate, flip) are
        not.  Double-clicking the group enters its context
        (``_current_group_id`` advances) and unlocks its direct members
        for editing.

        Returns False when:
        - the component is not in any non-root group, OR
        - the active group context already contains the component as a
          direct member (we're "inside" the group).
        """
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        groups = self._session.layout.groups
        active_gid = self._current_group_id
        active = groups.get(active_gid)
        for gid, g in groups.items():
            if gid == ROOT_GROUP_ID:
                continue
            if comp_id not in g.component_ids:
                continue
            # comp_id is a direct member of group `g`.  Locked unless we've
            # stepped inside that group (or a descendant whose chain leads
            # back to `g`).  The simple, correct check: locked iff `g` is
            # NOT the active group.
            if gid == active_gid:
                return False
            return True
        return False

    def _wire_is_locked(self, wire_id: uuid.UUID) -> bool:
        """True iff *wire_id* is structurally owned by a group the user
        is currently viewing from the outside.

        Mirrors :meth:`_is_locked_in_group` for components.  Selection
        of locked wires (for highlight / inspection) remains allowed
        — mutating operations (delete, edit) gate on this method and
        skip locked wires.  Walks every non-root group and uses the
        net-aware classifier from
        :mod:`protoboard.domain.layout.group_ownership`.
        """
        from protoboard.domain.layout.group_ownership import wire_is_owned_by_group
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        wire = self._session.wire_by_id(wire_id)
        if wire is None:
            return False
        groups = self._session.layout.groups
        active_gid = self._current_group_id
        wire_net_map = self._session.cached_wire_net_map()
        for gid, group in groups.items():
            if gid == ROOT_GROUP_ID:
                continue
            if gid == active_gid:
                # We're inside this group → its wires are editable.
                continue
            comp_ids = frozenset(all_component_ids(groups, gid))
            if not comp_ids:
                continue
            if wire_is_owned_by_group(
                wire, group,
                wire_net_map=wire_net_map,
                knots=self._session.layout.knots,  # type: ignore[arg-type]
                group_comp_ids=comp_ids,
                schematic=self._session.schematic,
                placements=self._session.layout.placements,
                instances=self._session.instances,
                definitions=self._session.components,
            ):
                return True
        return False

    def _filter_lockable_wires(
        self, wire_ids: "set[uuid.UUID] | frozenset[uuid.UUID]",
    ) -> set[uuid.UUID]:
        """Drop wire IDs that are locked inside a non-active group."""
        return {wid for wid in wire_ids if not self._wire_is_locked(wid)}

    def _wire_safe_to_drag(
        self, wire: "Wire", drag_ids: "set[uuid.UUID]",
    ) -> bool:
        """True iff *wire* can be translated by the same delta as *drag_ids*
        without producing endpoints that disagree with PinKnot positions.

        A wire is safe when every PinKnot endpoint resolves to a pin of a
        component inside *drag_ids* — those pins follow the drag, so the
        translated path still terminates on them.  When any endpoint
        anchors to a component **outside** *drag_ids* (typically a
        locked sub-group member that the group filter dropped from the
        drag set), the path slides off-pad and a subsequent
        ``ReplaceWireFragmentsCommand.apply`` raises ``ValueError`` in
        ``replace_wire_fragments`` (endpoint vs knot mismatch).

        Selecting a wire that's anchored to a locked component is fine
        for highlight / inspection, but it must **not** join the drag
        translation set — drop it here.
        """
        from protoboard.domain.layout.knot import PinKnot
        for ref in (wire.a, wire.b):
            if ref is None:
                continue
            knot = self._session.layout.knots.get(ref.id)
            if isinstance(knot, PinKnot):
                if knot.pin.component_id not in drag_ids:
                    return False
        return True

    # ------------------------------------------------------------------
    # Relation class (which correlated set the auto-highlight reflects)
    # ------------------------------------------------------------------

    def set_relation(self, r: "RelationClass") -> None:
        """Set the relation class and re-dispatch auto-highlight to last click source."""
        from protoboard.gui.selection_axes import RelationClass, RELATION_LABELS
        if not isinstance(r, RelationClass):
            return
        if r is self._relation:
            return
        self._relation = r
        # Re-render auto-highlight if we have a remembered source.
        if self._hl_source_wire is not None or self._hl_source_comp is not None:
            self._refresh_auto_highlight(
                wire_id=self._hl_source_wire,
                comp_id=self._hl_source_comp,
                pin_number=self._hl_source_pin,
            )
        else:
            # No source — clear any stale auto entry from a previous click.
            if self._session is not None:
                self._session.highlight_stack.clear_auto("select_tool")
                self._clear_connectivity_highlight()
        if self._session is not None:
            self._session.notify_highlight_mode(RELATION_LABELS.get(r, r.name))

    @property
    def relation(self) -> "RelationClass":
        return self._relation

    def _apply_interaction(
        self,
        *,
        wire_id: uuid.UUID | None = None,
        comp_id: uuid.UUID | None = None,
        pin_number: int | None = None,
        shift_held: bool = False,
    ) -> None:
        """Apply selection + auto-highlight to a click on (comp_id/pin_number) or wire_id.

        LPM:           selection = clicked object (component selection is done
                       by on_press, wire selection here); highlight = full
                       relation set ignoring the filter.
        Shift+LPM:     selection extended to relation ∩ filter (components and
                       wires both contribute); highlight = same as LPM.
        """
        _t_enter = time.monotonic() if _CLICK_PROFILE else 0.0
        self._hl_source_wire = wire_id
        self._hl_source_comp = comp_id
        self._hl_source_pin = pin_number

        correlated_comps, correlated_wires = self._resolve_relation(
            self._relation,
            wire_id=wire_id,
            comp_id=comp_id,
            pin_number=pin_number,
        )
        if _CLICK_PROFILE:
            _log.info(
                "click_profile.apply_interaction.resolve_relation",
                ms=round((time.monotonic() - _t_enter) * 1000, 1),
                n_correlated_comps=len(correlated_comps),
                n_correlated_wires=len(correlated_wires),
            )

        if shift_held:
            # Selection = relation ∩ filter (replace any prior multi-selection).
            self._deselect()
            self._deselect_wire()
            self._clear_multi_selection()
            self._clear_multi_wire_selection()
            if self._filter_components():
                for cid in correlated_comps:
                    self._selected_ids.add(cid)
                    self._draw_multi_highlight(cid)
            if self._filter_wires():
                for wid in correlated_wires:
                    self._selected_wire_ids.add(wid)
                    self._draw_multi_wire_highlight(wid)
            _log.info(
                "selection.multi_wire",
                via="shift_apply_interaction",
                n_wires=len(self._selected_wire_ids),
                n_comps=len(self._selected_ids),
                wire_ids=[str(w) for w in self._selected_wire_ids],
            )
        else:
            # LPM without Shift: select only the clicked object.  Component
            # selection is handled by on_press before us; wire selection is
            # delegated here.
            if wire_id is not None:
                self._select_wire(wire_id)

        _t_before_refresh = time.monotonic() if _CLICK_PROFILE else 0.0
        self._refresh_auto_highlight(
            wire_id=wire_id,
            comp_id=comp_id,
            pin_number=pin_number,
            correlated_comps=correlated_comps,
            correlated_wires=correlated_wires,
        )
        if _CLICK_PROFILE:
            _log.info(
                "click_profile.apply_interaction.refresh_auto_highlight",
                ms=round((time.monotonic() - _t_before_refresh) * 1000, 1),
            )
        _t_before_notify = time.monotonic() if _CLICK_PROFILE else 0.0
        self._notify_selection()
        if _CLICK_PROFILE:
            _log.info(
                "click_profile.apply_interaction.notify_selection",
                ms=round((time.monotonic() - _t_before_notify) * 1000, 1),
                total_ms=round((time.monotonic() - _t_enter) * 1000, 1),
            )

    # ----------------------------------------------------------------
    # Phase 2b: Shift+LMB → FSM-style extend / reset / collapse
    # ----------------------------------------------------------------

    def _apply_shift_interaction_fsm(
        self,
        *,
        wire_id: uuid.UUID | None = None,
        comp_id: uuid.UUID | None = None,
        pin_number: int | None = None,
    ) -> None:
        """Shift+LMB dispatcher implementing the FSM transitions.

        Replaces the legacy ``_apply_interaction(shift_held=True)`` REPLACE
        semantics (where every Shift+LMB rebuilt selection from
        ``relation ∩ filter``).  The new behaviour:

        * **Same relation, hit ∉ multi+primary** → *extend* the existing
          multi-selection by ``hit`` (and its relation set, intersected
          with the active filter).
        * **Cross relation** → *reset* to a fresh MULTI layer: clear the
          old multi, replace primary, push the new relation's highlight.
          Equivalent to the user's legacy Shift+LMB on a different net.
        * **Hit == primary or hit ∈ multi** → *collapse* to HIGHLIGHT:
          drop primary + multi, keep the highlight auto-entry.

        The function does not touch the click-profile timing path — Shift
        clicks are rare enough that the extra branch isn't worth the
        instrumentation noise.
        """
        from protoboard.gui.selection_fsm import Hit

        # Build the Hit currency the FSM speaks.
        if wire_id is not None:
            hit = Hit("wire", wire_id)
        elif comp_id is not None:
            hit = Hit("component", comp_id)
        else:
            if _dbg_enabled():
                _log.debug("shift_interaction.no_hit_bail")
            return

        primary_hit = self._fsm_current_hit()
        # Membership check — is the hit already inside the visible
        # multi-set?  Use string-typed comparison so wire and component
        # ids never collide.
        in_multi = False
        if hit.kind == "component" and isinstance(hit.id, uuid.UUID):
            in_multi = (
                hit.id in self._selected_ids
                and hit.id != self._selected_id
            )
        elif hit.kind == "wire" and isinstance(hit.id, uuid.UUID):
            in_multi = (
                hit.id in self._selected_wire_ids
                and hit.id != self._selected_wire_id
            )

        if primary_hit is not None and hit == primary_hit:
            # Collapse — Shift on the primary pops back to HIGHLIGHT
            # without losing the auto-entry.
            if _dbg_enabled():
                _log.debug("shift_interaction.collapse.primary", hit=str(hit.id)[:8])
            self._collapse_to_highlight_layer()
            return
        if in_multi:
            if _dbg_enabled():
                _log.debug("shift_interaction.collapse.in_multi", hit=str(hit.id)[:8])
            self._collapse_to_highlight_layer()
            return

        # Same relation?  Only meaningful when we already have a primary.
        same_rel = (
            primary_hit is not None
            and self._fsm_same_relation(hit, primary_hit, self._relation)
        )
        if primary_hit is not None and same_rel:
            if _dbg_enabled():
                _log.debug("shift_interaction.extend.same_relation", hit=str(hit.id)[:8])
            self._extend_multi_with_hit(
                wire_id=wire_id, comp_id=comp_id, pin_number=pin_number,
            )
            return

        # Cross-relation or no prior primary → reset to a fresh MULTI
        # layer around the new hit's relation.
        if _dbg_enabled():
            _log.debug(
                "shift_interaction.reset.cross_or_fresh",
                hit=str(hit.id)[:8],
                had_primary=primary_hit is not None,
            )
        self._reset_to_fresh_multi(
            wire_id=wire_id, comp_id=comp_id, pin_number=pin_number,
        )

    def _collapse_to_highlight_layer(self) -> None:
        """Drop MULTI + SELECTION but keep the auto highlight entry."""
        self._clear_multi_selection()
        self._clear_multi_wire_selection()
        self._deselect()
        self._deselect_wire()
        self._notify_selection()

    def _extend_multi_with_hit(
        self,
        *,
        wire_id: uuid.UUID | None = None,
        comp_id: uuid.UUID | None = None,
        pin_number: int | None = None,
    ) -> None:
        """Add the new hit's relation set to the existing MULTI layer.

        The relation set is intersected with the active filter — Shift+LMB
        on a component while the filter is ``WIRES_ONLY`` extends only
        the wires of the new relation.
        """
        correlated_comps, correlated_wires = self._resolve_relation(
            self._relation,
            wire_id=wire_id,
            comp_id=comp_id,
            pin_number=pin_number,
        )
        if self._filter_components():
            for cid in correlated_comps:
                if cid not in self._selected_ids:
                    self._selected_ids.add(cid)
                    self._draw_multi_highlight(cid)
        if self._filter_wires():
            for wid in correlated_wires:
                if wid not in self._selected_wire_ids:
                    self._selected_wire_ids.add(wid)
                    self._draw_multi_wire_highlight(wid)
        # Keep highlight in sync with the (possibly newly enlarged) net.
        self._refresh_auto_highlight(
            wire_id=wire_id,
            comp_id=comp_id,
            pin_number=pin_number,
            correlated_comps=correlated_comps,
            correlated_wires=correlated_wires,
        )
        self._notify_selection()

    def _reset_to_fresh_multi(
        self,
        *,
        wire_id: uuid.UUID | None = None,
        comp_id: uuid.UUID | None = None,
        pin_number: int | None = None,
    ) -> None:
        """Cross-relation Shift+LMB: replace selection with the new
        relation ∩ filter.  Mirrors the legacy shift behaviour, but
        only fires on a cross-relation jump rather than on every Shift.
        """
        self._deselect()
        self._deselect_wire()
        self._clear_multi_selection()
        self._clear_multi_wire_selection()
        correlated_comps, correlated_wires = self._resolve_relation(
            self._relation,
            wire_id=wire_id,
            comp_id=comp_id,
            pin_number=pin_number,
        )
        # Promote the freshly-hit object to primary FIRST — _select_wire
        # calls _deselect_wire → _clear_multi_wire_selection, which would
        # wipe a just-populated multi set if we promoted after.  Same
        # logic for the component path keeps the ordering symmetric.
        if comp_id is not None:
            self._select_primary(comp_id)
        elif wire_id is not None:
            self._select_wire(wire_id)
        if self._filter_components():
            for cid in correlated_comps:
                self._selected_ids.add(cid)
                self._draw_multi_highlight(cid)
        if self._filter_wires():
            for wid in correlated_wires:
                self._selected_wire_ids.add(wid)
                self._draw_multi_wire_highlight(wid)
        self._refresh_auto_highlight(
            wire_id=wire_id,
            comp_id=comp_id,
            pin_number=pin_number,
            correlated_comps=correlated_comps,
            correlated_wires=correlated_wires,
        )
        self._notify_selection()

    # ================================================================
    # Phase 2c — orthogonal selection-op dispatcher
    # ----------------------------------------------------------------
    # Spec: docs/highlight-state-machine.txt (rev 2).
    # Implements the modifier matrix Alt(basis_scope) × Ctrl/Shift(op).
    # Called from on_press once Etap 3 lands; meanwhile coexists with
    # the legacy Phase 2b machinery (`_apply_shift_interaction_fsm` and
    # its helpers) which are deleted in Etap 4.
    # ================================================================

    def _apply_selection_op(
        self,
        hit: "Hit | None",
        basis_scope: "BasisScope",
        op: "SetOp",
    ) -> None:
        """Central dispatcher for every LMB click in Phase 2c.

        Resolves the basis (single hit or relation set) and delegates
        to the matching helper.  Empty-canvas semantics follow spec
        rev 2: SET pops one FSM layer, ADD/REMOVE are no-ops.
        """
        from protoboard.gui.selection_fsm import SetOp

        if hit is None:
            # Spec rev 2, empty canvas:
            #   SET    → pop one layer
            #   ADD    → A ∪ ∅ = A  (no-op)
            #   REMOVE → A \ ∅ = A  (no-op)
            if op == SetOp.SET:
                self._esc_pop_one_layer()
            return

        # Build the basis sets (separated for components vs wires
        # because the legacy storage fields are split too).
        comp_basis, wire_basis = self._resolve_basis(hit, basis_scope)

        if op == SetOp.SET:
            self._set_selection(comp_basis, wire_basis, new_primary=hit)
        elif op == SetOp.ADD:
            self._add_to_selection(comp_basis, wire_basis, new_primary=hit)
        elif op == SetOp.REMOVE:
            self._remove_from_selection(comp_basis, wire_basis)

    def _resolve_basis(
        self,
        hit: "Hit",
        basis_scope: "BasisScope",
    ) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
        """Materialise (comp_basis, wire_basis) from a Hit + scope.

        SINGLE basis → just the hit itself (single-element set in the
        matching bucket, empty in the other).
        RELATION basis → resolve via the existing `_resolve_relation`
        with the current `RelationClass`, which respects RelationClass.
        NONE by collapsing to SINGLE.
        """
        from protoboard.gui.selection_fsm import BasisScope
        from protoboard.gui.selection_axes import RelationClass

        if basis_scope == BasisScope.SINGLE or self._relation == RelationClass.NONE:
            comps: set[uuid.UUID] = set()
            wires: set[uuid.UUID] = set()
            if hit.kind == "component" and isinstance(hit.id, uuid.UUID):
                comps.add(hit.id)
            elif hit.kind == "wire" and isinstance(hit.id, uuid.UUID):
                wires.add(hit.id)
            return comps, wires

        # RELATION scope: reuse the same resolver the legacy code uses.
        wire_id: uuid.UUID | None = None
        comp_id: uuid.UUID | None = None
        pin_number: int | None = None
        if hit.kind == "wire" and isinstance(hit.id, uuid.UUID):
            wire_id = hit.id
        elif hit.kind == "component" and isinstance(hit.id, uuid.UUID):
            comp_id = hit.id
        correlated_comps, correlated_wires = self._resolve_relation(
            self._relation,
            wire_id=wire_id,
            comp_id=comp_id,
            pin_number=pin_number,
        )
        return correlated_comps, correlated_wires

    def _set_selection(
        self,
        comp_basis: set[uuid.UUID],
        wire_basis: set[uuid.UUID],
        new_primary: "Hit | None",
    ) -> None:
        """SET op — selection := basis.

        Decision 5 (no-op on same primary): if the click resolves to
        the exact same singleton already at SELECTION, skip the rebuild.
        Otherwise clear everything, populate the basis sets, then
        promote the new primary so the multi-rect highlights survive
        (see [[selection-fsm-refactor-2026-06-08]] fix on promotion
        ordering).
        """
        # Decision 5 (extended): no-op-on-set when basis is already
        # contained in the current selection — but still re-anchor the
        # primary to the clicked hit if it changed.  Classic UX:
        # clicking on an element of a multi-selection makes that
        # element the new Properties-panel anchor (and the drag-anchor)
        # without dropping the multi.  The multi sets and highlight
        # stack stay untouched.
        #
        # NOTE: legacy convention is `primary ∈ _selected_ids` when a
        # multi-selection is active (rubber-band sets `_selected_ids`
        # to the full set then calls `_select_primary` on one of them,
        # leaving the new primary inside the multi).  Drag-arm reads
        # `_selected_ids` as the canonical drag set, so we must not
        # discard the new primary from it here.
        if new_primary is not None and self._basis_already_in_selection(
            comp_basis, wire_basis,
        ):
            saved_multi_comps = set(self._selected_ids)
            saved_multi_wires = set(self._selected_wire_ids)
            if new_primary.kind == "component" and isinstance(new_primary.id, uuid.UUID):
                if self._selected_id != new_primary.id:
                    self._promote_primary(new_primary)
                    self._selected_ids = saved_multi_comps
                    self._selected_wire_ids = saved_multi_wires
                    self._redraw_multi_highlights()
            elif new_primary.kind == "wire" and isinstance(new_primary.id, uuid.UUID):
                if self._selected_wire_id != new_primary.id:
                    self._promote_primary(new_primary)
                    self._selected_wire_ids = saved_multi_wires
                    self._selected_ids = saved_multi_comps
                    self._redraw_multi_wire_highlights()
            self._notify_selection()
            return

        self._deselect()
        self._deselect_wire()
        self._clear_multi_selection()
        self._clear_multi_wire_selection()

        # Promote first (decision: _select_wire/_deselect_wire wipes
        # the multi wire set; promoting before population keeps the
        # subsequent _draw_multi_* calls authoritative).
        self._promote_primary(new_primary)

        # Multi-set semantics: legacy code keeps the primary in its
        # own field (`_selected_id` / `_selected_wire_id`) and the
        # multi sets list the EXTRA members.  When the basis is a
        # singleton equal to the new primary, both multi sets stay
        # empty — depth is SELECTION, not MULTI.  Only "RELATION basis"
        # with more than one element populates the multi sets, and
        # even then we skip the primary id to avoid drawing a multi
        # rect on top of the SELECTION highlight.
        primary_comp_id = new_primary.id if (
            new_primary is not None
            and new_primary.kind == "component"
            and isinstance(new_primary.id, uuid.UUID)
        ) else None
        primary_wire_id = new_primary.id if (
            new_primary is not None
            and new_primary.kind == "wire"
            and isinstance(new_primary.id, uuid.UUID)
        ) else None
        if self._filter_components():
            for cid in comp_basis:
                if cid == primary_comp_id:
                    continue
                self._selected_ids.add(cid)
                self._draw_multi_highlight(cid)
        if self._filter_wires():
            for wid in wire_basis:
                if wid == primary_wire_id:
                    continue
                self._selected_wire_ids.add(wid)
                self._draw_multi_wire_highlight(wid)

        # Highlight (decision 4, A.1): reset to relation(new_primary).
        self._refresh_highlight_from_hit(new_primary)
        self._notify_selection()

    def _add_to_selection(
        self,
        comp_basis: set[uuid.UUID],
        wire_basis: set[uuid.UUID],
        new_primary: "Hit | None",
    ) -> None:
        """ADD op — selection := selection ∪ basis.

        If the current state is NORMAL (empty selection), ADD is
        equivalent to SET — push to SELECTION with new_primary as
        anchor.  Otherwise extend the multi sets, switch primary to
        the new hit, and grow the highlight union.
        """
        if (
            not self._selected_ids
            and not self._selected_wire_ids
            and self._selected_id is None
            and self._selected_wire_id is None
        ):
            # ADD into empty → behave like SET.
            self._set_selection(comp_basis, wire_basis, new_primary)
            return

        # Capture the old primary before promotion — once it's no
        # longer the anchor we need to move it into the multi set so
        # the bright selection rect stays visible.
        prior_comp_primary = self._selected_id
        prior_wire_primary = self._selected_wire_id

        # Promote first so the about-to-be-added wire basis isn't
        # wiped by _select_wire's _deselect_wire side-effect.
        self._promote_primary(new_primary)

        new_primary_comp_id = new_primary.id if (
            new_primary is not None
            and new_primary.kind == "component"
            and isinstance(new_primary.id, uuid.UUID)
        ) else None
        new_primary_wire_id = new_primary.id if (
            new_primary is not None
            and new_primary.kind == "wire"
            and isinstance(new_primary.id, uuid.UUID)
        ) else None

        # The previous primary is now "just a multi member" — record
        # it explicitly so it keeps its bright rect.
        if (
            prior_comp_primary is not None
            and prior_comp_primary != new_primary_comp_id
            and prior_comp_primary not in self._selected_ids
            and self._filter_components()
        ):
            self._selected_ids.add(prior_comp_primary)
            self._draw_multi_highlight(prior_comp_primary)
        if (
            prior_wire_primary is not None
            and prior_wire_primary != new_primary_wire_id
            and prior_wire_primary not in self._selected_wire_ids
            and self._filter_wires()
        ):
            self._selected_wire_ids.add(prior_wire_primary)
            self._draw_multi_wire_highlight(prior_wire_primary)

        if self._filter_components():
            for cid in comp_basis:
                if cid == new_primary_comp_id:
                    continue
                if cid not in self._selected_ids:
                    self._selected_ids.add(cid)
                    self._draw_multi_highlight(cid)
        if self._filter_wires():
            for wid in wire_basis:
                if wid == new_primary_wire_id:
                    continue
                if wid not in self._selected_wire_ids:
                    self._selected_wire_ids.add(wid)
                    self._draw_multi_wire_highlight(wid)

        # Highlight UNION — relation of the new hit joins the existing
        # highlight without clearing it (decision 2, spec rev 2).
        self._extend_highlight_with_hit(new_primary)
        self._notify_selection()

    def _remove_from_selection(
        self,
        comp_basis: set[uuid.UUID],
        wire_basis: set[uuid.UUID],
    ) -> None:
        """REMOVE op — selection := selection \\ basis.

        Subtracts from multi sets; drops primary if it was in the
        removed basis.  Highlight is *not* touched (decision 4, A.1).
        If the selection collapses to empty, depth becomes HIGHLIGHT
        (highlight union stays so user retains visual context).
        """
        # Drop primary if it falls inside the basis.
        if self._selected_id is not None and self._selected_id in comp_basis:
            self._deselect()
        if self._selected_wire_id is not None and self._selected_wire_id in wire_basis:
            self._deselect_wire()

        # Subtract from multi sets.
        removed_comps = self._selected_ids & comp_basis
        removed_wires = self._selected_wire_ids & wire_basis
        self._selected_ids -= comp_basis
        self._selected_wire_ids -= wire_basis

        # Redraw multi highlights from scratch — cheaper than
        # individually removing the right scene items.
        if removed_comps:
            self._redraw_multi_highlights()
        if removed_wires:
            self._redraw_multi_wire_highlights()

        # Decision 1: primary becomes None after REMOVE.  Note the
        # _deselect()/_deselect_wire() calls above already nulled
        # _selected_id / _selected_wire_id when applicable.  We do
        # NOT auto-promote a surviving lone multi member — user must
        # click it explicitly to make it the anchor.
        self._notify_selection()

    # ----------------------------------------------------------------
    # Helpers shared by the three op functions
    # ----------------------------------------------------------------

    def _basis_already_in_selection(
        self,
        comp_basis: set[uuid.UUID],
        wire_basis: set[uuid.UUID],
    ) -> bool:
        """True when every basis member is already in the selection.

        Powers the extended decision-5 no-op: plain LMB on an element
        that is currently part of the multi-selection (primary or
        otherwise) leaves the entire selection intact, so the user
        can drag the group.  Distinct from `_is_selection_exactly`
        which only matches the singleton-primary case.
        """
        current_comps: set[uuid.UUID] = set(self._selected_ids)
        if self._selected_id is not None:
            current_comps.add(self._selected_id)
        current_wires: set[uuid.UUID] = set(self._selected_wire_ids)
        if self._selected_wire_id is not None:
            current_wires.add(self._selected_wire_id)
        return comp_basis <= current_comps and wire_basis <= current_wires

    def _is_selection_exactly(
        self,
        comp_basis: set[uuid.UUID],
        wire_basis: set[uuid.UUID],
    ) -> bool:
        """True when current selection is exactly the basis singleton.

        Used by decision 5 (plain LMB on current primary is a no-op).
        """
        if len(comp_basis) == 1 and not wire_basis:
            (cid,) = comp_basis
            return (
                self._selected_id == cid
                and not self._selected_ids
                and not self._selected_wire_ids
                and self._selected_wire_id is None
            )
        if len(wire_basis) == 1 and not comp_basis:
            (wid,) = wire_basis
            return (
                self._selected_wire_id == wid
                and not self._selected_wire_ids
                and not self._selected_ids
                and self._selected_id is None
            )
        return False

    def _promote_primary(self, hit: "Hit | None") -> None:
        """Set hit as the Properties-panel anchor.

        Mirrors the legacy `_select_primary` / `_select_wire` split
        based on hit kind.  Safe to call with None (no-op).
        """
        if hit is None:
            return
        if hit.kind == "component" and isinstance(hit.id, uuid.UUID):
            self._select_primary(hit.id)
        elif hit.kind == "wire" and isinstance(hit.id, uuid.UUID):
            self._select_wire(hit.id)

    def _refresh_highlight_from_hit(self, hit: "Hit | None") -> None:
        """Reset highlight to relation(hit), or clear if hit is None.

        Wrapper over the existing `_refresh_auto_highlight` so callers
        can pass a Hit instead of fishing out comp_id / wire_id /
        pin_number themselves.
        """
        if hit is None:
            self._clear_connectivity_highlight()
            session = self._session
            if session is not None:
                session.highlight_stack.clear_auto("select_tool")
            return
        if hit.kind == "wire" and isinstance(hit.id, uuid.UUID):
            self._refresh_auto_highlight(wire_id=hit.id)
        elif hit.kind == "component" and isinstance(hit.id, uuid.UUID):
            self._refresh_auto_highlight(comp_id=hit.id)

    def _extend_highlight_with_hit(self, hit: "Hit | None") -> None:
        """Grow the auto-highlight stack entry by relation(hit).

        Decision 2 (spec rev 2): ADD ops grow the highlight union
        instead of replacing it.  Implementation reads the current
        auto entry, merges relation(hit) into it, and pushes the
        merged entry back.
        """
        if hit is None:
            return
        session = self._session
        if session is None:
            return

        # Resolve the relation set for the new hit.
        wire_id: uuid.UUID | None = None
        comp_id: uuid.UUID | None = None
        if hit.kind == "wire" and isinstance(hit.id, uuid.UUID):
            wire_id = hit.id
        elif hit.kind == "component" and isinstance(hit.id, uuid.UUID):
            comp_id = hit.id
        rel_comps, rel_wires = self._resolve_relation(
            self._relation, wire_id=wire_id, comp_id=comp_id, pin_number=None,
        )

        # Read current auto entry; union the new relation into it.
        current = session.highlight_stack.active()
        if current is None:
            # No prior entry → behaves like reset to relation(hit).
            self._refresh_highlight_from_hit(hit)
            return

        merged_comps = frozenset(current.comp_ids) | frozenset(rel_comps)
        merged_wires = frozenset(current.wire_ids) | frozenset(rel_wires)
        if (
            merged_comps == frozenset(current.comp_ids)
            and merged_wires == frozenset(current.wire_ids)
        ):
            # No new content — nothing to do.
            return

        # Build a new HighlightEntry with the union; profile stays
        # whatever was on the active entry.
        from protoboard.gui.highlight_state import HighlightEntry
        merged = HighlightEntry(
            comp_ids=merged_comps,
            wire_ids=merged_wires,
            net_id=current.net_id,
            profile=current.profile,
            label=current.label,
        )
        session.highlight_stack.push_auto(merged, source_id="select_tool")

    # ----------------------------------------------------------------
    # Modifier matrix decoder + Hit builder
    # ----------------------------------------------------------------

    def _decode_modifier_matrix(
        self,
        *,
        ctrl: bool,
        shift: bool,
        alt: bool,
    ) -> "tuple[BasisScope, SetOp]":
        """Translate Qt modifier flags → (basis_scope, op) per spec rev 2.

        Spec table:
          | ctrl | shift | alt | op     | basis    |
          |  -   |   -   |  -  | SET    | SINGLE   |
          |  -   |   -   |  +  | SET    | RELATION |
          |  +   |   -   |  -  | ADD    | SINGLE   |
          |  +   |   -   |  +  | ADD    | RELATION |
          |  +   |   +   |  -  | REMOVE | SINGLE   |
          |  +   |   +   |  +  | REMOVE | RELATION |

        Note Shift without Ctrl currently falls through to plain LMB
        (SET, SINGLE).  This frees Shift for tool-specific overrides
        like SimplifyTool's draft/apply gesture without colliding
        with the FSM.
        """
        from protoboard.gui.selection_fsm import BasisScope, SetOp
        op = (
            SetOp.REMOVE if (ctrl and shift) else
            SetOp.ADD    if ctrl else
            SetOp.SET
        )
        basis_scope = BasisScope.RELATION if alt else BasisScope.SINGLE
        return basis_scope, op

    def _build_hit_from_press(self, scene_pt: "QPointF") -> "Hit | None":
        """Run the existing hit-tests in priority order and return a Hit.

        Priority:
          1. component pin (`_hit_test`)
          2. wire body (`_hit_test_wire`)
          3. component bbox (`_hit_test_bbox`)
          4. group outline (long-press only — out of scope here)

        Returns None for empty canvas.  Pin number is intentionally
        not preserved on the Hit — relation resolution can recover it
        from scene_pt if needed via `_hit_test_pin_for_comp`.  This
        keeps Hit hashable + comparable by id alone.
        """
        from protoboard.gui.selection_fsm import Hit

        if self._filter_components():
            pin_hit = self._hit_test(scene_pt)
            if pin_hit is not None:
                return Hit("component", pin_hit)
        if self._filter_wires():
            wire_hit = self._hit_test_wire(scene_pt)
            if wire_hit is not None:
                return Hit("wire", wire_hit)
        if self._filter_components():
            bbox_hit = self._hit_test_bbox(scene_pt)
            if bbox_hit is not None:
                return Hit("component", bbox_hit)
        return None

    def _refresh_auto_highlight(
        self,
        *,
        wire_id: uuid.UUID | None = None,
        comp_id: uuid.UUID | None = None,
        pin_number: int | None = None,
        correlated_comps: set[uuid.UUID] | None = None,
        correlated_wires: set[uuid.UUID] | None = None,
    ) -> None:
        """Push an auto HighlightEntry reflecting the current relation class.

        Clears the previous SelectTool auto entry first.  NONE relation just
        clears.  Pad-marker / neighbor-rect overlays are redrawn here too —
        they are scene items, not part of the dimming pipeline.
        """
        from protoboard.gui.selection_axes import RelationClass

        session = self._session
        if session is None:
            return

        # Always start by clearing the previous overlay and auto entry.
        self._clear_connectivity_highlight()
        session.highlight_stack.clear_auto("select_tool")

        if self._relation == RelationClass.NONE:
            return
        if comp_id is None and wire_id is None:
            return

        if correlated_comps is None or correlated_wires is None:
            correlated_comps, correlated_wires = self._resolve_relation(
                self._relation,
                wire_id=wire_id,
                comp_id=comp_id,
                pin_number=pin_number,
            )

        # Collect every net touched by the click (bbox click on a component
        # may span multiple nets — we want overlay markers on all of them).
        net_ids = self._resolve_relation_net_ids(
            wire_id=wire_id, comp_id=comp_id, pin_number=pin_number,
        )
        # Keep the legacy single-net field populated for downstream consumers
        # (panels, _notify_selection) that still expect a representative id.
        self._highlight_net_id = next(iter(net_ids), None)

        # Build the HighlightEntry — translate RelationProfile to HighlightProfile.
        from protoboard.gui.highlight_state import HighlightEntry
        from protoboard.gui.interaction_mode import HighlightProfile
        from protoboard.gui.selection_axes import RELATION_PROFILES
        rprof = RELATION_PROFILES[self._relation]
        hprof = HighlightProfile(
            pad_marker_rgba=rprof.pad_marker_rgba,
            pad_marker_radius=rprof.pad_marker_radius,
            unconnected_rgba=rprof.unconnected_rgba,
            unconnected_dashed=rprof.unconnected_dashed,
            neighbor_rect_rgba=rprof.neighbor_rect_rgba,
            neighbor_pen_rgba=rprof.neighbor_pen_rgba,
            dim_components=rprof.dim_components,
            dim_wires=rprof.dim_wires,
        )
        # net_id on the entry stays None — comp_ids/wire_ids are already the
        # fully resolved superset, BoardView doesn't need to expand a net.
        entry = HighlightEntry(
            comp_ids=frozenset(correlated_comps),
            wire_ids=frozenset(correlated_wires),
            net_id=None,
            profile=hprof,
            label=f"Auto ({self._relation.value})",
        )
        session.highlight_stack.push_auto(entry, source_id="select_tool")

        # Overlay items (pad markers / neighbor rects / unconnected circles).
        self._draw_relation_overlay(
            net_ids=net_ids,
            correlated_comps=correlated_comps,
            profile=rprof,
        )

    # ----------------------------------------------------------------
    # FSM glue (Phase 2a)
    # ----------------------------------------------------------------

    def _fsm_same_relation(
        self,
        hit: "object",  # protoboard.gui.selection_fsm.Hit
        relative_to: "object",
        relation: "RelationClass",
    ) -> bool:
        """Same-relation predicate consumed by the SelectionFSM.

        Maps each ``Hit`` to its logical net id (via the existing
        helpers) and compares.  When either side has no net (e.g. a
        bbox click on a no-net component), default to "different
        relation" so Shift+LMB falls back to a fresh multi-set.
        """
        from protoboard.gui.selection_axes import RelationClass

        if relation == RelationClass.NONE:
            return False
        net_a = self._fsm_hit_net_id(hit)
        net_b = self._fsm_hit_net_id(relative_to)
        if net_a is None or net_b is None:
            return False
        return net_a == net_b

    def _fsm_hit_net_id(self, hit: "object") -> "uuid.UUID | None":
        """Map a ``Hit`` to a logical net id when one exists."""
        # Local import keeps this module independent of selection_fsm
        # at construction time; selection_fsm has no Qt deps so this
        # is purely about ordering.
        kind = getattr(hit, "kind", None)
        hit_id = getattr(hit, "id", None)
        if kind == "wire" and isinstance(hit_id, uuid.UUID):
            return self._resolve_logical_net(wire_id=hit_id)
        if kind == "component" and isinstance(hit_id, uuid.UUID):
            # Component bbox click — no pin context; use first net.
            session = self._session
            if session is None:
                return None
            for net_obj in session.schematic.nets.values():
                if any(pr.component_id == hit_id for pr in net_obj.pins):
                    return net_obj.id
        return None

    def _esc_pop_one_layer(self) -> None:
        """Pop one layer of the FSM-style selection stack.

        Layers (top → bottom):
          ``MULTI_SELECTION`` — multi-selection rectangle set is non-empty
          ``SELECTION``       — single primary highlighted
          ``HIGHLIGHT``       — auto-highlight entry still on the stack
          ``NORMAL``          — nothing to pop

        Esc once removes the topmost layer only.  This mirrors
        ``SelectionFSM.step_esc`` but operates directly on the legacy
        fields until Phase 2b lifts the FSM into the authoritative role.
        """
        n_multi_comps = len(self._selected_ids)
        n_multi_wires = len(self._selected_wire_ids)
        has_primary = (
            self._selected_id is not None or self._selected_wire_id is not None
        )
        has_hl = self._has_active_highlight()
        if _dbg_enabled():
            _log.debug(
                "esc.pop_layer.enter",
                n_multi_comps=n_multi_comps,
                n_multi_wires=n_multi_wires,
                primary_comp=str(self._selected_id)[:8] if self._selected_id else None,
                primary_wire=str(self._selected_wire_id)[:8] if self._selected_wire_id else None,
                has_active_highlight=has_hl,
            )
        # 1. Pop MULTI_SELECTION — keep primary, drop the extension set.
        if n_multi_comps or n_multi_wires:
            if _dbg_enabled():
                _log.debug("esc.pop_layer.drop_multi")
            self._clear_multi_selection()
            self._clear_multi_wire_selection()
            self._notify_selection()
            return
        # 2. Pop SELECTION — keep highlight set, drop primary.
        if has_primary:
            if _dbg_enabled():
                _log.debug("esc.pop_layer.drop_selection")
            self._deselect()
            self._deselect_wire()
            self._notify_selection()
            return
        # 3. Pop HIGHLIGHT — clear connectivity highlight + auto stack
        #    entry.  ``_clear_connectivity_highlight`` only removes the
        #    scene overlay items; the auto entry on
        #    ``session.highlight_stack`` MUST be cleared explicitly so
        #    BoardView's ``_apply_highlight_opacity`` stops dimming
        #    everything else on the next paint.  Before this fix the
        #    third Esc looked like a no-op visually because the auto
        #    entry kept the bright/dim split alive.
        if has_hl:
            if _dbg_enabled():
                _log.debug("esc.pop_layer.drop_highlight")
            self._clear_connectivity_highlight()
            session = self._session
            if session is not None:
                session.highlight_stack.clear_auto("select_tool")
            self._notify_selection()
            return
        # 4. NORMAL — nothing to pop; still notify so listeners can resync.
        if _dbg_enabled():
            _log.debug("esc.pop_layer.normal_noop")
        self._notify_selection()

    def _has_active_highlight(self) -> bool:
        """True when the auto-highlight stack entry from this tool is
        still up — i.e. we are at ``HIGHLIGHT`` layer in FSM terms."""
        session = self._session
        if session is None:
            return False
        entry = session.highlight_stack.active()
        if entry is None:
            return False
        # ``_clear_connectivity_highlight`` clears this tool's auto entry
        # (source_id="select_tool").  Any active entry whose comp/wire
        # set is non-empty counts.
        return bool(entry.comp_ids or entry.wire_ids or entry.net_id)

    def _fsm_current_hit(self) -> "object | None":
        """Build a ``Hit`` from the legacy ``_selected_*`` fields.

        Used to keep the FSM in step with legacy mutations until Phase
        2b takes the FSM as authoritative.  Returns the primary
        selection if any, else ``None``.
        """
        from protoboard.gui.selection_fsm import Hit

        if self._selected_id is not None:
            return Hit("component", self._selected_id)
        if self._selected_wire_id is not None:
            return Hit("wire", self._selected_wire_id)
        return None

    def _resolve_relation_net_ids(
        self,
        *,
        wire_id: uuid.UUID | None = None,
        comp_id: uuid.UUID | None = None,
        pin_number: int | None = None,
    ) -> set[uuid.UUID]:
        """All logical nets touched by the click source.

        Pin click / wire click → at most one net.  Bbox click on a component
        → every net that component participates in.
        """
        session = self._session
        if session is None:
            return set()
        if comp_id is not None and pin_number is not None:
            single = self._resolve_logical_net(comp_id=comp_id, pin_number=pin_number)
            return {single} if single is not None else set()
        if wire_id is not None:
            wnm = session.cached_wire_net_map()
            single = wnm.get(wire_id)
            return {single} if single is not None else set()
        if comp_id is not None:
            out: set[uuid.UUID] = set()
            for net_obj in session.schematic.nets.values():
                if any(pr.component_id == comp_id for pr in net_obj.pins):
                    out.add(net_obj.id)
            return out
        return set()

    def _resolve_logical_net(
        self,
        *,
        wire_id: uuid.UUID | None = None,
        comp_id: uuid.UUID | None = None,
        pin_number: int | None = None,
    ) -> uuid.UUID | None:
        """Find the logical net id associated with the click source, if any."""
        from protoboard.domain.schematic.net import PinRef

        session = self._session
        if session is None:
            return None
        if comp_id is not None and pin_number is not None:
            ref = PinRef(component_id=comp_id, pin_number=pin_number)
            net = session.schematic.net_for_pin(ref)
            if net is not None:
                return net.id
        if wire_id is not None:
            wnm = session.cached_wire_net_map()
            return wnm.get(wire_id)
        return None

    def _resolve_relation(
        self,
        relation: "RelationClass",
        *,
        wire_id: uuid.UUID | None,
        comp_id: uuid.UUID | None,
        pin_number: int | None,
    ) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
        """Return (comp_ids, wire_ids) correlated to the click source by *relation*.

        Clicking a component bbox (without a specific pin) expands across
        *every* net the component participates in — the user intent is "show
        me this component's context," which is the union of its nets.
        Clicking a specific pin or a wire narrows to that single net.

        The clicked object itself is always included in the result so the
        BoardView dimming pipeline never wipes it out.
        """
        from protoboard.gui.selection_axes import RelationClass

        if relation == RelationClass.NONE:
            return set(), set()

        session = self._session
        if session is None:
            return set(), set()

        # Collect every net touched by the click source.
        net_ids: set[uuid.UUID] = set()
        if comp_id is not None and pin_number is not None:
            single = self._resolve_logical_net(comp_id=comp_id, pin_number=pin_number)
            if single is not None:
                net_ids.add(single)
        elif wire_id is not None:
            wnm0 = session.cached_wire_net_map()
            single = wnm0.get(wire_id)
            if single is not None:
                net_ids.add(single)
        elif comp_id is not None:
            # Bbox click — gather every net this component participates in.
            for n in session.schematic.nets.values():
                if any(pr.component_id == comp_id for pr in n.pins):
                    net_ids.add(n.id)

        correlated_comps: set[uuid.UUID] = set()
        correlated_wires: set[uuid.UUID] = set()

        # Always seed with the clicked object so it never gets dimmed.
        if comp_id is not None:
            correlated_comps.add(comp_id)
        if wire_id is not None:
            correlated_wires.add(wire_id)

        if relation in (
            RelationClass.SAME_LOGICAL_NET,
            RelationClass.SAME_PHYSICAL_NET,
            RelationClass.UNCONNECTED_PINS,
        ):
            wnm = session.cached_wire_net_map()
            for nid in net_ids:
                net_obj = session.schematic.nets.get(nid)
                if net_obj is not None:
                    correlated_comps |= {ref.component_id for ref in net_obj.pins}
                correlated_wires |= {
                    wid for wid, mapped in wnm.items() if mapped == nid
                }
        elif relation == RelationClass.NEIGHBORS and comp_id is not None:
            for nid in net_ids:
                net_obj = session.schematic.nets.get(nid)
                if net_obj is None:
                    continue
                correlated_comps |= {pr.component_id for pr in net_obj.pins}

        return correlated_comps, correlated_wires

    def _draw_relation_overlay(
        self,
        *,
        net_ids: set[uuid.UUID],
        correlated_comps: set[uuid.UUID],
        profile: "RelationProfile",
    ) -> None:
        """Draw the scene overlay (pad markers, neighbor rects, unconnected
        circles) appropriate for the current relation class.

        Dimming itself is handled by the HighlightStack — this method only adds
        overlay graphics that flag *which* pins/components are relevant.
        Always preceded by _clear_connectivity_highlight() in callers.
        """
        from protoboard.domain.schematic.net import PinRef
        from protoboard.gui.layer_state import Z_OVERLAY
        from protoboard.gui.selection_axes import RelationClass

        scene = self._view.scene() if self._view is not None else None
        if scene is None or self._session is None:
            return
        session = self._session
        relation = self._relation

        if relation in (RelationClass.SAME_LOGICAL_NET, RelationClass.SAME_PHYSICAL_NET):
            if not net_ids:
                return
            r = profile.pad_marker_radius
            rgba = profile.pad_marker_rgba
            pen = QPen(QColor(*rgba[:3], rgba[3]), 0.05)
            brush = QBrush(QColor(*rgba))
            for nid in net_ids:
                net_obj = session.schematic.nets.get(nid)
                if net_obj is None:
                    continue
                for ref in net_obj.pins:
                    pos = session.get_pin_position(ref.component_id, ref.pin_number)
                    if pos is None:
                        continue
                    circle = QGraphicsEllipseItem(
                        QRectF(pos.x - r, pos.y - r, 2 * r, 2 * r)
                    )
                    circle.setBrush(brush)
                    circle.setPen(pen)
                    circle.setZValue(Z_OVERLAY + 1.0)
                    scene.addItem(circle)
                    self._highlight_items.append(circle)

        elif relation == RelationClass.UNCONNECTED_PINS:
            if not net_ids:
                return
            graph = session.connectivity_graph()
            r = profile.pad_marker_radius if profile.pad_marker_radius > 0 else 0.22
            rgba = profile.unconnected_rgba
            pen = QPen(QColor(*rgba[:3], rgba[3]), 0.07)
            if profile.unconnected_dashed:
                pen.setStyle(Qt.PenStyle.DashLine)
            brush = QBrush(QColor(*rgba[:3], rgba[3] // 3))
            for nid in net_ids:
                net_obj = session.schematic.nets.get(nid)
                if net_obj is None:
                    continue
                # Precompute pin→electric-set bucket once per net.  The
                # previous implementation re-queried `node_for_pin` for
                # every pair of pins (O(N²) graph lookups) — for a net
                # with 50 pins that was 2500 graph queries on the main
                # thread per click, easily visible as a 300-500ms freeze.
                pin_to_ck: dict[PinRef, object] = {}
                bucket_counts: dict[object, int] = {}
                for ref in net_obj.pins:
                    pref = PinRef(ref.component_id, ref.pin_number)
                    ck = graph.node_for_pin(pref)
                    pin_to_ck[pref] = ck  # type: ignore[assignment]
                    if ck is not None:
                        bucket_counts[ck] = bucket_counts.get(ck, 0) + 1
                for ref in net_obj.pins:
                    pref = PinRef(ref.component_id, ref.pin_number)
                    ck = pin_to_ck.get(pref)
                    if ck is not None and bucket_counts.get(ck, 0) > 1:
                        continue  # physically connected to another pin in net
                    pos = session.get_pin_position(ref.component_id, ref.pin_number)
                    if pos is None:
                        continue
                    circle = QGraphicsEllipseItem(
                        QRectF(pos.x - r, pos.y - r, 2 * r, 2 * r)
                    )
                    circle.setBrush(brush)
                    circle.setPen(pen)
                    circle.setZValue(Z_OVERLAY + 1.0)
                    scene.addItem(circle)
                    self._highlight_items.append(circle)

        elif relation == RelationClass.NEIGHBORS:
            rgba_fill = profile.neighbor_rect_rgba
            rgba_pen = profile.neighbor_pen_rgba
            for other_id in correlated_comps:
                bbox = session.component_bbox(other_id)
                if bbox is None:
                    continue
                rect_item = QGraphicsRectItem(
                    QRectF(bbox.x, bbox.y, bbox.width, bbox.height)
                )
                rect_item.setPen(QPen(QColor(*rgba_pen), 0.07))
                rect_item.setBrush(QBrush(QColor(*rgba_fill)))
                rect_item.setZValue(Z_OVERLAY + 1.0)
                scene.addItem(rect_item)
                self._highlight_items.append(rect_item)

    # ------------------------------------------------------------------
    # Floating state
    # ------------------------------------------------------------------

    def _is_floating(self) -> bool:
        return bool(self._floating_ids)

    def _enter_floating(
        self,
        comp_ids: set[uuid.UUID],
        orig_placements: dict[uuid.UUID, ComponentPlacement],
        extra_cmds: list[object] | None = None,
        dragged_wire_ids: frozenset[uuid.UUID] = frozenset(),
        decision: object | None = None,
    ) -> None:
        """Enter floating state — component is in new position but not committed.

        *decision*: optional DragCommitDecision driving overlay color, corner
        text, and whether commit is permitted (HARD blocks commit).
        """
        from protoboard.gui.tools.drag_commit_decision import (
            DragCommitDecision,
            DragSeverity,
        )

        self._floating_ids = comp_ids
        self._floating_orig_placements = orig_placements
        self._floating_extra_cmds = extra_cmds or []
        self._floating_dragged_wire_ids = dragged_wire_ids
        self._floating_decision = decision
        if isinstance(decision, DragCommitDecision):
            self._floating_severity = decision.severity.value
        else:
            self._floating_severity = DragSeverity.CLEAR.value
        self._draw_floating_highlights()
        self._update_floating_status()
        # Preserve / refresh overlay + corner banner so the user can keep
        # seeing what conflicts.
        if isinstance(decision, DragCommitDecision):
            self._update_corner_text_from_decision(decision)

    def _commit_floating(self) -> None:
        """Commit floating state to CommandStack.  Refuses on HARD severity.

        Two paths:

        * **Unified drag model (2026-06-02)** — when
          ``self._floating_decision.wire_classification`` is not None,
          dispatch to :meth:`_commit_floating_unified` which routes
          through :class:`MoveDragSetCommand` with preference-driven
          soft-conflict resolution.
        * **Legacy** — fall through to the original macro-of-
          ``TranslateAnchoredWires`` + ``MoveComponent`` + per-component
          drop_skewed pipeline.  Used when ``decision.wire_classification``
          is None (test stubs, group rotation, other entry points that
          haven't migrated yet).
        """
        if not self._floating_ids:
            return
        _log.debug(
            "drag.commit_floating.start",
            floating_ids=[str(c) for c in self._floating_ids],
            severity=self._floating_severity,
            extra_cmds=[type(c).__name__ for c in self._floating_extra_cmds],
            extra_remove_wire_ids=[
                str(c.wire_id) for c in self._floating_extra_cmds
                if type(c).__name__ == "RemoveWireCommand"
            ],
            wires_before=[str(w.id) for w in self._session.layout.wires],
        )
        from protoboard.gui.tools.drag_commit_decision import (
            DragCommitDecision,
            DragSeverity,
        )
        if self._floating_severity == DragSeverity.HARD.value:
            win = self._view.window() if self._view is not None else None
            sb = win.statusBar() if win is not None and hasattr(win, "statusBar") else None
            if sb is not None:
                sb.showMessage(
                    "Conflict: cannot commit here (Esc to cancel)",
                    3000,
                )
            return

        # Try the unified drag model path first.  Falls back to legacy
        # if the decision doesn't carry a wire_classification.
        decision = self._floating_decision
        if (
            isinstance(decision, DragCommitDecision)
            and decision.wire_classification is not None
        ):
            self._commit_floating_unified(decision)
            return

        from protoboard.domain.commands.base import Command
        from protoboard.domain.commands.component_cmds import (
            TranslateAnchoredWiresCommand,
        )

        cmds: list[Command[ComponentState]] = []
        # Wire removals (decision.auto_remove_wire_ids) come first so a
        # following TranslateAnchoredWiresCommand doesn't observe wires
        # the user already opted to drop.
        for cmd in self._floating_extra_cmds:
            cmds.append(cmd)  # type: ignore[arg-type]
        n_removed = sum(
            1 for c in self._floating_extra_cmds
            if type(c).__name__ == "RemoveWireCommand"
        )
        # Wires removed above must also be excluded from the
        # ``TranslateAnchoredWiresCommand`` pass below — otherwise its
        # ``_update_wires_for_move`` may re-translate (and drop as
        # skewed) a wire the macro already removed, leaving the later
        # RemoveWireCommand staring at a missing wire (KeyError
        # observed 2026-06-01 on group drag).
        removed_ids = frozenset(
            c.wire_id for c in self._floating_extra_cmds
            if type(c).__name__ == "RemoveWireCommand"
        )
        translate_exclude = self._floating_dragged_wire_ids | removed_ids
        # Each moved component contributes a pair: translate its
        # anchored wires first (so foreign wires whose path merely
        # passes through old pin positions are not touched), then move
        # the component itself with placement_only=True. Splitting the
        # work into discrete commands keeps the macro's undo behaviour
        # transparent and matches the refactor's "no hidden wire
        # mutations in MoveComponentCommand" goal.
        cmd_delta_log: list[dict[str, object]] = []
        for cid in self._floating_ids:
            new_pl = self._session.layout.placements.get(cid)
            old_pl = self._floating_orig_placements.get(cid)
            if new_pl is None or old_pl is None:
                cmd_delta_log.append({
                    "comp_id": str(cid)[:8],
                    "skipped": True,
                    "reason": "missing_placement",
                    "new_pl": (
                        (new_pl.position.x, new_pl.position.y)
                        if new_pl is not None else None
                    ),
                    "old_pl": (
                        (old_pl.position.x, old_pl.position.y)
                        if old_pl is not None else None
                    ),
                })
                continue
            cmd_delta_log.append({
                "comp_id": str(cid)[:8],
                "skipped": False,
                "new_pos": (new_pl.position.x, new_pl.position.y),
                "old_pos": (old_pl.position.x, old_pl.position.y),
                "new_rot": new_pl.rotation,
                "old_rot": old_pl.rotation,
                "delta": (
                    new_pl.position.x - old_pl.position.x,
                    new_pl.position.y - old_pl.position.y,
                ),
            })
            cmds.append(
                TranslateAnchoredWiresCommand(
                    component_id=cid,
                    new_placement=new_pl,
                    old_placement=old_pl,
                    exclude_wire_ids=translate_exclude,
                )
            )
            cmds.append(
                MoveComponentCommand(
                    component_id=cid,
                    new_placement=new_pl,
                    old_placement=old_pl,
                    skip_conflict_check=True,
                    exclude_wire_ids=translate_exclude,
                    placement_only=True,
                )
            )
        _log.info(
            "drag.commit_floating.legacy_macro",
            n_floating=len(self._floating_ids),
            n_translate_exclude=len(translate_exclude),
            n_dragged_wire_ids=len(self._floating_dragged_wire_ids),
            n_removed_wire_ids=len(removed_ids),
            n_extra_cmds=len(self._floating_extra_cmds),
            severity=self._floating_severity,
            decision_has_classification=(
                getattr(self._floating_decision, "wire_classification", None)
                is not None
            ),
            per_component=cmd_delta_log,
            total_cmds=len(cmds),
        )
        self._clear_floating_highlights()
        if self._view is not None:
            self._drag_overlay.reset(self._view.scene())
            self._corner_text.hide(self._view.scene())
        self._floating_ids = set()
        self._floating_orig_placements = {}
        self._floating_extra_cmds = []
        self._floating_dragged_wire_ids = frozenset()
        self._floating_decision = None
        self._floating_severity = "clear"
        # Drop the cached debounced/cheap decision so a follow-up drag
        # starts from a clean slate.
        self._drag_last_decision = None
        self._clear_drag_pad_index()
        if cmds:
            from protoboard.domain.commands.base import CommandRejectedError
            try:
                if len(cmds) == 1:
                    self._session.stack.execute(cmds[0], self._session)
                else:
                    macro = MacroCommand(cmds, description="Move component")
                    self._session.stack.execute(macro, self._session)
            except CommandRejectedError:
                # Autoroute is running — drop the drag silently.  The
                # status bar already explains why mutating actions are
                # disabled; the floating component snaps back to its
                # source position via the next refresh().
                if self._view is not None:
                    self._view.refresh()
                return
            except Exception as exc:
                # Floating commit (legacy path) failed mid-macro.
                # MacroCommand.apply already logged macro.apply.failed;
                # this extra layer pins it specifically to
                # _commit_floating so the bug-reporter can tell legacy
                # path from group-drag path (different builder, same
                # macro shape, different failure mode).
                cmd_summary = [
                    {
                        "i": i,
                        "type": type(c).__name__,
                        **(
                            {"wire_id": str(getattr(c, "wire_id"))[:8]}
                            if hasattr(c, "wire_id") else {}
                        ),
                        **(
                            {"comp_id": str(getattr(c, "component_id"))[:8]}
                            if hasattr(c, "component_id") else {}
                        ),
                    }
                    for i, c in enumerate(cmds)
                ]
                _log.error(
                    "drag.commit_floating.legacy_macro_failed",
                    exc_type=type(exc).__name__,
                    exc_msg=str(exc),
                    n_commands=len(cmds),
                    commands=cmd_summary,
                )
                raise
            self._session.layout_changed.emit()
            _log.debug(
                "drag.commit_floating.done",
                wires_after=[str(w.id) for w in self._session.layout.wires],
                n_removed=n_removed,
            )
            # Toast about removed wires (Ctrl+Z to undo).
            if n_removed:
                win = self._view.window() if self._view is not None else None
                sb = win.statusBar() if win is not None and hasattr(win, "statusBar") else None
                if sb is not None:
                    sb.showMessage(
                        f"Removed {n_removed} wire(s) (Ctrl+Z to undo)",
                        4000,
                    )

    # ------------------------------------------------------------------
    # Unified drag model commit (2026-06-02)
    # ------------------------------------------------------------------

    def _commit_floating_unified(self, decision: object) -> None:
        """Commit using MoveDragSetCommand + preference-driven soft resolution.

        Path:
        1. Load effective drag preferences (app settings + session overrides).
        2. Resolve each soft-conflict bucket through dialogs/preferences.
           Cancel anywhere → stay floating.
        3. Build MoveDragSetCommand with the resolved drop set + the
           classification's travels_with_drag set (internal +
           half_orphan_internal wires that translate atomically).
        4. Execute as a single atomic command on the stack.
        5. Schedule post-commit autoroute for any nets the user opted to
           reroute (cross-group disconnect → Reroute).
        """
        from protoboard.gui.tools.drag_commit_decision import (
            DragCommitDecision,
        )
        assert isinstance(decision, DragCommitDecision)
        cls = decision.wire_classification
        if cls is None:
            # Shouldn't happen — caller verified.  Defensive bail.
            return

        prefs = self._effective_drag_preferences()

        # 1. Resolve each soft-conflict bucket.  Each helper returns
        #    (drop_ids, reroute_net_ids) or None on user cancel.
        free_result = self._resolve_pref_free(
            prefs.free_wire,
            frozenset(decision.free_drop_wire_ids),
        )
        if free_result is None:
            return  # user cancelled — stay floating
        sgroup_result = self._resolve_pref_sgroup(
            prefs.stationary_group_wire,
            frozenset(decision.stationary_group_drop_wire_ids),
        )
        if sgroup_result is None:
            return
        disconnect_result = self._resolve_pref_disconnect(
            prefs.cross_group_disconnect,
            frozenset(decision.cross_group_disconnect_wire_ids),
        )
        if disconnect_result is None:
            return

        free_drop, _ = free_result
        sgroup_drop, _ = sgroup_result
        disconnect_drop, reroute_net_ids = disconnect_result

        # 2. Build MoveDragSetCommand.
        delta = self._compute_drag_delta()
        if delta is None:
            # No moved component yet (drag-end with zero delta) — fall
            # through to legacy path.  Defensive: real drags always
            # have a delta by the time _commit_floating fires.
            return self._commit_floating_legacy_fallback()
        members: dict[
            uuid.UUID,
            "tuple[ComponentPlacement, ComponentPlacement]",
        ] = {}
        for cid in self._floating_ids:
            new_pl = self._session.layout.placements.get(cid)
            old_pl = self._floating_orig_placements.get(cid)
            if new_pl is None or old_pl is None:
                continue
            members[cid] = (old_pl, new_pl)
        dropped = free_drop | sgroup_drop | disconnect_drop

        # ReplaceWireFragmentsCommand entries in _floating_extra_cmds
        # (live-translated wire path changes) are absorbed by
        # MoveDragSetCommand's internal_wire_ids translation — they
        # become redundant.  Filter them out.  RemoveWireCommand
        # entries are equally absorbed by the dropped set.
        # (Some test paths pass through _floating_extra_cmds with
        # non-wire commands like AddWireCommand — those are kept and
        # bundled as a macro after MoveDragSetCommand.)
        side_cmds: list[object] = []
        for cmd in self._floating_extra_cmds:
            name = type(cmd).__name__
            if name in {"ReplaceWireFragmentsCommand", "RemoveWireCommand"}:
                continue
            side_cmds.append(cmd)

        cmd = MoveDragSetCommand(
            members=members,
            internal_wire_ids=cls.travels_with_drag,
            dropped_wire_ids=frozenset(dropped),
            delta=delta,
        )

        # 3. Clear floating state BEFORE executing — execute() may emit
        #    signals that trigger refreshes which read this state.
        n_dropped = len(dropped)
        self._clear_floating_highlights()
        self._clear_drag_ghost_state()
        if self._view is not None:
            self._drag_overlay.reset(self._view.scene())
            self._corner_text.hide(self._view.scene())
        self._floating_ids = set()
        self._floating_orig_placements = {}
        self._floating_extra_cmds = []
        self._floating_dragged_wire_ids = frozenset()
        self._floating_decision = None
        self._floating_severity = "clear"
        self._drag_last_decision = None
        self._clear_drag_pad_index()

        # 4. Execute.  Bundle side_cmds via MacroCommand if any.
        from protoboard.domain.commands.base import CommandRejectedError
        try:
            if side_cmds:
                bundle: list[object] = [cmd, *side_cmds]
                macro: MacroCommand[ComponentState] = MacroCommand(
                    bundle,  # type: ignore[arg-type]
                    description="Move drag set",
                )
                self._session.stack.execute(macro, self._session)
            else:
                self._session.stack.execute(cmd, self._session)
        except CommandRejectedError:
            if self._view is not None:
                self._view.refresh()
            return

        self._session.layout_changed.emit()
        _log.debug(
            "drag.commit_floating.unified.done",
            members=[str(c)[:8] for c in members],
            internal=[str(w)[:8] for w in cls.travels_with_drag],
            dropped=[str(w)[:8] for w in dropped],
            reroute_nets=[str(n)[:8] for n in reroute_net_ids],
            delta=(delta.x, delta.y),
        )

        # 5. Toast + post-commit autoroute.
        if n_dropped:
            win = self._view.window() if self._view is not None else None
            sb = (
                win.statusBar()
                if win is not None and hasattr(win, "statusBar")
                else None
            )
            if sb is not None:
                sb.showMessage(
                    f"Removed {n_dropped} wire(s) (Ctrl+Z to undo)",
                    4000,
                )
        if reroute_net_ids:
            self._dispatch_post_drag_autoroute(reroute_net_ids)

    def _commit_floating_legacy_fallback(self) -> None:
        """Defensive fallback when unified path can't compute a delta.

        Mirror of the legacy ``_commit_floating`` tail-end so test stubs
        with zero-delta drags still terminate cleanly.  Returns
        immediately without mutating state.
        """
        self._clear_floating_highlights()
        if self._view is not None:
            self._drag_overlay.reset(self._view.scene())
            self._corner_text.hide(self._view.scene())
        self._floating_ids = set()
        self._floating_orig_placements = {}
        self._floating_extra_cmds = []
        self._floating_dragged_wire_ids = frozenset()
        self._floating_decision = None
        self._floating_severity = "clear"
        self._drag_last_decision = None
        self._clear_drag_pad_index()

    # ------------------------------------------------------------------
    # Unified drag helpers: preferences, dialogs, delta, autoroute
    # ------------------------------------------------------------------

    def _effective_drag_preferences(self) -> DragPreferences:
        """Merge persistent AppSettings with per-session overrides."""
        from protoboard.gui.app_settings import load_app_settings
        base = DragPreferences.from_app_settings(load_app_settings())
        return self._drag_session_overrides.effective(base)

    def _resolve_pref_ask_remove(
        self,
        preference: str,
        wire_ids: frozenset[uuid.UUID],
        *,
        dialog_title: str,
        dialog_message_template: str,
        block_status_message: str,
        session_override_attr: str,
    ) -> tuple[set[uuid.UUID], set[uuid.UUID]] | None:
        """Shared resolution path for the two ask-remove categories
        (free wire / stationary-group wire).

        Both categories share an identical state machine that only
        diverges in dialog labels and which ``DragSessionOverrides``
        field caches the user's opt-out.  ``dialog_message_template``
        receives the wire count via ``{n}``.

        Returns ``(drop_ids, reroute_net_ids)`` or None on user cancel.
        """
        if not wire_ids:
            return set(), set()
        if preference == "auto-remove":
            return set(wire_ids), set()
        if preference == "auto-reroute":
            return set(wire_ids), self._net_ids_for_wires(wire_ids)
        if preference == "block":
            self._show_status_bar(block_status_message)
            return None
        # ask-remove / ask-reroute → dialog.  ask-remove is simple
        # Yes/Cancel; ask-reroute uses the same dialog and adds the
        # affected nets to the reroute set on accept.
        from protoboard.gui.dialogs.drag_ask_remove_dialog import (
            DragAskRemoveDialog,
        )
        from PySide6.QtWidgets import QDialog
        n = len(wire_ids)
        parent = self._view if self._view is not None else None
        dlg = DragAskRemoveDialog(
            title=dialog_title,
            message=dialog_message_template.format(n=n),
            parent=parent,  # type: ignore[arg-type]
        )
        accepted = dlg.exec() == QDialog.DialogCode.Accepted
        if dlg.session_opt_out() and accepted:
            setattr(
                self._drag_session_overrides,
                session_override_attr,
                "auto-remove",
            )
        if not accepted:
            return None
        if preference == "ask-reroute":
            return set(wire_ids), self._net_ids_for_wires(wire_ids)
        return set(wire_ids), set()

    def _resolve_pref_free(
        self,
        preference: str,
        wire_ids: frozenset[uuid.UUID],
    ) -> tuple[set[uuid.UUID], set[uuid.UUID]] | None:
        """Resolve the ``drag.free_wire_conflict`` preference.

        Free wires (no owning group) — the user just wants a yes/no.
        """
        return self._resolve_pref_ask_remove(
            preference, wire_ids,
            dialog_title="Remove free wires?",
            dialog_message_template=(
                "Drag will remove {n} free wire(s).  Continue?"
            ),
            block_status_message=(
                "Free-wire conflict blocked by preferences (Esc to cancel)"
            ),
            session_override_attr="free_wire",
        )

    def _resolve_pref_sgroup(
        self,
        preference: str,
        wire_ids: frozenset[uuid.UUID],
    ) -> tuple[set[uuid.UUID], set[uuid.UUID]] | None:
        """Resolve the ``drag.stationary_group_wire_conflict`` preference."""
        return self._resolve_pref_ask_remove(
            preference, wire_ids,
            dialog_title="Remove foreign-group wires?",
            dialog_message_template=(
                "Drag requires removing {n} wire(s) belonging to another group.  "
                "Continue?"
            ),
            block_status_message=(
                "Stationary-group wire conflict blocked by preferences"
            ),
            session_override_attr="stationary_group_wire",
        )

    def _resolve_pref_disconnect(
        self,
        preference: str,
        wire_ids: frozenset[uuid.UUID],
    ) -> tuple[set[uuid.UUID], set[uuid.UUID]] | None:
        """Resolve the ``drag.cross_group_disconnect`` preference.

        Cross-group wires offer Reroute / Drop / Cancel — Reroute drops
        the wire AND dispatches autoroute on the affected nets.
        """
        if not wire_ids:
            return set(), set()
        if preference == "auto-remove":
            return set(wire_ids), set()
        if preference == "auto-reroute":
            return set(wire_ids), self._net_ids_for_wires(wire_ids)
        if preference == "block":
            self._show_status_bar(
                "Drag would disconnect a net — blocked by preferences (Esc to cancel)",
            )
            return None
        # ask-remove / ask-reroute → full disconnect dialog.
        from protoboard.gui.dialogs.drag_disconnect_dialog import (
            DragDisconnectAction,
            DragDisconnectDialog,
        )
        from PySide6.QtWidgets import QDialog
        n = len(wire_ids)
        affected_nets = self._net_names_for_wires(wire_ids)
        parent = self._view if self._view is not None else None
        dlg = DragDisconnectDialog(
            n_wires=n,
            affected_nets=affected_nets,
            parent=parent,  # type: ignore[arg-type]
        )
        result = dlg.exec()
        action = dlg.chosen_action()
        if dlg.session_opt_out() and result == QDialog.DialogCode.Accepted:
            mapped = (
                "auto-reroute" if action == DragDisconnectAction.REROUTE
                else "auto-remove"
            )
            self._drag_session_overrides.cross_group_disconnect = mapped
        if action == DragDisconnectAction.CANCEL:
            return None
        if action == DragDisconnectAction.REROUTE:
            return set(wire_ids), self._net_ids_for_wires(wire_ids)
        # DROP
        return set(wire_ids), set()

    def _snapshot_owned_wire_paths(
        self,
        group_id: uuid.UUID,
        group_comp_ids: frozenset[uuid.UUID],
        *,
        include_both_pin_in_set: bool,
    ) -> None:
        """Populate ``self._drag_orig_wire_paths`` with wires that should
        travel with the group's drag.

        Net-aware ownership (via :func:`find_owned_wires`) covers
        structurally-owned wires (both PinKnots in the group) and
        half-orphan tails of internal nets while excluding T-junction
        tails of exported nets.

        When ``include_both_pin_in_set`` is True an additional pass also
        snapshots wires whose two PinKnot endpoints both reference
        components in ``group_comp_ids`` even if the underlying net is
        external (e.g. manually-routed SCL between group members).
        Without that pass, per-component MoveComponentCommand
        translates one endpoint while the other stays, the wire turns
        skewed and gets dropped — bug 2026-06-02 producing SHORTs on
        reference_big_project after a group drag.

        Idempotent against ``self._drag_orig_wire_paths`` for the same
        wire id; safe to call from multiple on_press code paths.
        """
        from protoboard.domain.layout.group_ownership import find_owned_wires
        from protoboard.domain.layout.knot import JointKnot, PinKnot, WirePinKnot
        grp_obj = self._session.layout.groups.get(group_id)
        if grp_obj is None:
            return
        owned_ids = find_owned_wires(
            list(self._session.layout.wires),
            grp_obj,
            wire_net_map=self._session.cached_wire_net_map(),
            knots=self._session.layout.knots,  # type: ignore[arg-type]
            group_comp_ids=group_comp_ids,
            schematic=self._session.schematic,
            placements=self._session.layout.placements,
            instances=self._session.instances,
            definitions=self._session.components,
        )
        knots = self._session.layout.knots
        # Diagnostic: classify each wire that is rejected, so a misowned
        # manual-route wire surfaces in the log with the reason.
        rejected_diag: list[dict[str, object]] = []
        for wire in self._session.layout.wires:
            if wire.id in owned_ids:
                self._drag_orig_wire_paths[wire.id] = list(wire.path)
                continue
            if wire.a is None or wire.b is None:
                if include_both_pin_in_set:
                    rejected_diag.append({
                        "wire_id": str(wire.id)[:8],
                        "reason": "no_knot_refs",
                    })
                continue
            ka = knots.get(wire.a.id)
            kb = knots.get(wire.b.id)
            if include_both_pin_in_set:
                if isinstance(ka, PinKnot) and isinstance(kb, PinKnot):
                    a_in = ka.pin.component_id in group_comp_ids
                    b_in = kb.pin.component_id in group_comp_ids
                    if a_in and b_in:
                        self._drag_orig_wire_paths[wire.id] = list(wire.path)
                        continue
                    rejected_diag.append({
                        "wire_id": str(wire.id)[:8],
                        "reason": "both_pin_but_outside_group",
                        "a_pin": f"{str(ka.pin.component_id)[:8]}/p{ka.pin.pin_number}",
                        "b_pin": f"{str(kb.pin.component_id)[:8]}/p{kb.pin.pin_number}",
                        "a_in_grp": a_in, "b_in_grp": b_in,
                    })
                else:
                    # Manual-route wire that didn't materialise as
                    # PinKnot+PinKnot — surface for investigation.
                    def _kdesc(k: object, ref_id: uuid.UUID) -> dict[str, object]:
                        if isinstance(k, PinKnot):
                            return {
                                "kind": "PinKnot",
                                "pin": f"{str(k.pin.component_id)[:8]}/p{k.pin.pin_number}",
                                "in_grp": k.pin.component_id in group_comp_ids,
                            }
                        elif isinstance(k, JointKnot):
                            return {
                                "kind": "JointKnot",
                                "pos": (k.position.x, k.position.y),
                                "owners": len(k.wire_ids),
                            }
                        elif isinstance(k, WirePinKnot):
                            return {
                                "kind": "WirePinKnot",
                                "pos": (k.position.x, k.position.y),
                                "owners": len(k.wire_ids),
                            }
                        return {"kind": type(k).__name__ if k else None}
                    rejected_diag.append({
                        "wire_id": str(wire.id)[:8],
                        "reason": "half_or_full_orphan",
                        "a": _kdesc(ka, wire.a.id),
                        "b": _kdesc(kb, wire.b.id),
                    })
        if rejected_diag:
            _log.debug(
                "arm_group_drag.rejected_wires",
                group_id=str(group_id)[:8],
                n_owned=len(owned_ids),
                n_rejected=len(rejected_diag),
                rejected=rejected_diag[:15],
            )

    def _net_ids_for_wires(
        self, wire_ids: frozenset[uuid.UUID],
    ) -> set[uuid.UUID]:
        """Map wire IDs to their net IDs via cached_wire_net_map."""
        getter = getattr(self._session, "cached_wire_net_map", None)
        if not callable(getter):
            return set()
        try:
            m = getter()
        except Exception:  # noqa: BLE001
            return set()
        result: set[uuid.UUID] = set()
        for wid in wire_ids:
            nid = m.get(wid)
            if nid is not None:
                result.add(nid)
        return result

    def _net_names_for_wires(
        self, wire_ids: frozenset[uuid.UUID],
    ) -> list[str]:
        """Human-readable net names for dialog display (sorted, deduped)."""
        net_ids = self._net_ids_for_wires(wire_ids)
        names: list[str] = []
        sch = getattr(self._session, "schematic", None)
        if sch is None:
            return names
        for nid in net_ids:
            net = sch.nets.get(nid)
            if net is not None:
                names.append(net.name)
        return sorted(set(names))

    def _compute_drag_delta(self) -> Point | None:
        """Average delta across moved members — every member moves by
        the same vector in a valid drag.

        Returns None if no member has both an old and a new placement.
        """
        for cid in self._floating_ids:
            new_pl = self._session.layout.placements.get(cid)
            old_pl = self._floating_orig_placements.get(cid)
            if new_pl is not None and old_pl is not None:
                return Point(
                    new_pl.position.x - old_pl.position.x,
                    new_pl.position.y - old_pl.position.y,
                )
        return None

    def _dispatch_post_drag_autoroute(
        self, net_ids: set[uuid.UUID],
    ) -> None:
        """Schedule an autoroute pass on *net_ids* after commit.

        Fires via QTimer.singleShot(0, ...) so the drag's stack.execute
        and signal cascade complete before routing starts.
        """
        if not net_ids:
            return
        # Best-effort dispatch — fall back to a no-op if the main
        # window's autoroute entry isn't reachable from the tool.
        win = self._view.window() if self._view is not None else None
        autoroute = getattr(win, "_autoroute_nets", None) if win else None
        if not callable(autoroute):
            _log.info(
                "drag.post_commit_autoroute.skipped_no_entry",
                n_nets=len(net_ids),
            )
            return
        QTimer.singleShot(0, lambda: autoroute(net_ids))

    def _show_status_bar(self, msg: str, timeout_ms: int = 3000) -> None:
        win = self._view.window() if self._view is not None else None
        sb = win.statusBar() if win is not None and hasattr(win, "statusBar") else None
        if sb is not None:
            sb.showMessage(msg, timeout_ms)

    def _cancel_floating(self) -> None:
        """Revert floating state — restore original positions without a command."""
        for cid, orig_pl in self._floating_orig_placements.items():
            self._session.layout.placements[cid] = orig_pl
        # Restore wire paths that were dragged along.
        for wire in self._session.layout.wires:
            orig_path = self._drag_orig_wire_paths.get(wire.id)
            if orig_path is not None:
                wire.path = list(orig_path)
        self._clear_floating_highlights()
        self._clear_drag_ghost_state()
        if self._view is not None:
            self._drag_overlay.reset(self._view.scene())
            self._corner_text.hide(self._view.scene())
        self._floating_ids = set()
        self._floating_orig_placements = {}
        self._floating_extra_cmds = []
        self._floating_dragged_wire_ids = frozenset()
        self._floating_decision = None
        self._floating_severity = "clear"
        # Clear cached debounced decision so the next drag starts
        # fresh (see _commit_floating note).
        self._drag_last_decision = None
        self._clear_drag_pad_index()
        self._session.layout_changed.emit()

    def _clear_floating(self) -> None:
        self._clear_floating_highlights()
        self._floating_ids = set()
        self._floating_orig_placements = {}
        self._floating_extra_cmds = []
        self._floating_dragged_wire_ids = frozenset()

    # ------------------------------------------------------------------
    # PadIndex trial state (Step 5 of drag-pipeline refactor)
    # ------------------------------------------------------------------

    def _ensure_drag_pad_index(
        self, drag_ids: "set[uuid.UUID]",
    ) -> "object | None":
        """Return the trial PadIndex, building it lazily on first use.

        Clones session.pad_index() once per drag operation; subsequent
        calls within the same drag re-sync moved components in-place
        rather than rebuilding from scratch.
        """
        if not drag_ids:
            return None
        from protoboard.domain.board.pad_state import PadIndex
        try:
            if self._drag_pad_index is None:
                session_idx = self._session.pad_index()
                self._drag_pad_index = session_idx.clone()
            assert isinstance(self._drag_pad_index, PadIndex)
            for cid in drag_ids:
                pl = self._session.layout.placements.get(cid)
                inst = self._session.instances.get(cid)
                if pl is None or inst is None:
                    continue
                defn = self._session.components.get(inst.definition_ref)
                if defn is None:
                    continue
                self._drag_pad_index.remove_component(cid)
                self._drag_pad_index.add_component(
                    cid, pl, defn, schematic=self._session.schematic,
                )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "drag.pad_index.refresh_failed",
                error=str(exc),
            )
            self._drag_pad_index = None
        return self._drag_pad_index

    def _clear_drag_pad_index(self) -> None:
        """Discard the trial PadIndex.  Called from commit/cancel paths."""
        self._drag_pad_index = None

    def _draw_floating_highlights(self) -> None:
        if self._view is None:
            return
        self._clear_floating_highlights()
        from protoboard.gui.layer_state import Z_SELECTION
        scene = self._view.scene()
        for cid in self._floating_ids:
            bbox = self._session.component_bbox(cid)
            if bbox is None:
                continue
            rect = QGraphicsRectItem(QRectF(bbox.x, bbox.y, bbox.width, bbox.height))
            pen = QPen(QColor(255, 200, 0, 200), 0.08)
            pen.setStyle(Qt.PenStyle.DashLine)
            rect.setPen(pen)
            rect.setBrush(QBrush(QColor(255, 200, 0, 20)))
            rect.setZValue(Z_SELECTION + 0.5)
            scene.addItem(rect)
            self._floating_highlight_items.append(rect)

    def _clear_floating_highlights(self) -> None:
        if self._view is not None:
            scene = self._view.scene()
            for item in self._floating_highlight_items:
                try:
                    scene.removeItem(item)
                except RuntimeError:
                    pass
        self._floating_highlight_items.clear()

    def _update_floating_status(self) -> None:
        win = self._view.window() if self._view is not None else None
        sb = win.statusBar() if win is not None and hasattr(win, "statusBar") else None
        if sb is not None:
            sb.showMessage("[Enter] Accept  ·  [Esc] Cancel  ·  [R] Rotate", 0)

    def deactivate(self) -> None:
        self._cancel_redrag()
        self._dragging = False
        self._drag_press_scene_pt = None
        self._drag_threshold_crossed = False
        self._drag_orig_positions = {}
        self._drag_orig_wire_paths = {}
        self._clear_drag_pad_index()
        if self._view is not None:
            self._drag_overlay.reset(self._view.scene())
        self._cancel_wire_edit()
        self._deselect_wire()
        self._clear_multi_wire_selection()
        self._clear_rubber_band()

    def on_scene_cleared(self) -> None:
        if self._session is not None:
            # Drop stale wire selections.
            existing_wires = {w.id for w in self._session.layout.wires}
            if self._selected_wire_id is not None and self._selected_wire_id not in existing_wires:
                self._selected_wire_id = None
            self._selected_wire_ids -= (self._selected_wire_ids - existing_wires)

            # Drop stale component selections.
            existing_comps = set(self._session.layout.placements.keys())
            if self._selected_id is not None and self._selected_id not in existing_comps:
                self._selected_id = None
            self._selected_ids -= (self._selected_ids - existing_comps)

            # Invalidate stale group references.
            if self._selected_group_id not in self._session.layout.groups:
                self._selected_group_id = None
            while len(self._group_stack) > 1 and self._current_group_id not in self._session.layout.groups:
                self._group_stack.pop()

        self._highlight = None
        self._origin_cross.clear()
        self._multi_highlights.clear()
        self._wire_highlight = None
        self._wire_multi_highlights.clear()
        self._edit_preview_items.clear()
        self._edit_live_line = None
        self._rubber_band_item = None
        self._redrag_items = []  # scene wiped, items gone
        self._highlight_items.clear()  # scene wiped — items are gone
        # Scene wiped — rerender overlay if drag is in progress.
        self._drag_overlay.rerender(self._view.scene() if self._view is not None else None)  # type: ignore[arg-type]
        # Redraw highlights after scene rebuild.
        if self._selected_id is not None and not self._dragging:
            self._draw_highlight(self._selected_id)
        for cid in self._selected_ids:
            self._draw_multi_highlight(cid)
        if self._selected_wire_id is not None:
            self._draw_wire_highlight(self._selected_wire_id)
        for wid in self._selected_wire_ids:
            self._draw_multi_wire_highlight(wid)
        if self._net_pad_highlight_positions:
            self._draw_net_pad_highlights(list(self._net_pad_highlight_positions))
        # Redraw floating highlights after scene rebuild.
        self._floating_highlight_items.clear()  # items deleted by scene.clear()
        if self._is_floating():
            self._draw_floating_highlights()

    def on_press(self, event: QMouseEvent, scene_pt: QPointF) -> bool:
        _click_t0 = time.monotonic() if _CLICK_PROFILE else 0.0
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        alt = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
        if _CLICK_PROFILE:
            self._click_t0 = _click_t0
        # Reset per-drag "log once" flag so on_move re-logs on next drag.
        self._drag_logged_once = False
        _log.info(
            "on_press.enter",
            scene=(round(scene_pt.x(), 2), round(scene_pt.y(), 2)),
            ctrl=ctrl, shift=shift, alt=alt,
            n_selected_comps=len(self._selected_ids),
            n_selected_wires=len(self._selected_wire_ids),
            primary_comp=str(self._selected_id)[:8] if self._selected_id else None,
            primary_wire=str(self._selected_wire_id)[:8] if self._selected_wire_id else None,
            is_floating=self._is_floating(),
            is_editing_wire=self._is_editing_wire(),
        )

        # --- Floating state: click commits (same as Enter) ---
        if self._is_floating() and not ctrl:
            hit = self._hit_test(scene_pt)
            if hit is not None and hit in self._floating_ids:
                # Click on a floating component — allow drag from current position.
                # Silently discard floating (keep current placement, no commit yet).
                self._clear_floating()
                # Fall through to normal press handling.
            else:
                # Click elsewhere — commit floating then handle click normally.
                self._commit_floating()
                # Fall through to process the click.

        # --- Re-drag mode: confirm new pin2 position ---
        if self._redrag_comp_id is not None:
            snapped = self._view.snap(scene_pt)
            self._commit_redrag(snapped)
            return True

        # --- Wire edit mode: clicking adds intermediate points ---
        if self._is_editing_wire():
            if not ctrl:
                snapped = self._view.snap(scene_pt)
                self._edit_add_point(snapped)
                return True
            # Ctrl+click while editing: re-set the end node (any wire in the net).
            hit_wire, hit_idx = self._hit_test_wire_node_any(scene_pt)
            if hit_wire is not None and hit_idx is not None:
                self._set_edit_end(hit_idx, hit_wire)
            return True

        # --- First node selected, waiting for second (Ctrl+click sets end) ---
        if self._edit_start_idx is not None and ctrl:
            hit_wire, hit_idx = self._hit_test_wire_node_any(scene_pt)
            if hit_wire is not None and hit_idx is not None:
                self._set_edit_end(hit_idx, hit_wire)
            return True

        # --- Ctrl+click: spec rev 2 dispatch (ADD or REMOVE) ---
        # Wire-edit start on Ctrl+LMB-on-wire-node is retained until
        # Etap 5 moves it to the [E] key.  Everything else routes
        # through the orthogonal dispatcher.
        if ctrl:
            # Wire-edit start on Ctrl+LMB-on-wire-node (legacy path,
            # to be moved to [E] in Etap 5).
            if self._selected_wire_id is not None and not shift:
                idx = self._hit_test_wire_node(scene_pt)
                if idx is not None:
                    self._start_wire_edit(idx)
                    return True

            # All other Ctrl[+Shift][+Alt] clicks go through the
            # selection-op dispatcher.  No drag-arm — Ctrl/Ctrl+Shift
            # are explicit set-ops, never drag initiators (spec rev 2).
            self._cancel_wire_edit()
            hit_obj = self._build_hit_from_press(scene_pt)
            basis_scope, op = self._decode_modifier_matrix(
                ctrl=ctrl, shift=shift, alt=alt,
            )
            _log.info(
                "on_press.dispatch.ctrl",
                op=op.name, basis_scope=basis_scope.name,
                hit_kind=hit_obj.kind if hit_obj else None,
                hit_id=str(hit_obj.id)[:8] if hit_obj and isinstance(hit_obj.id, uuid.UUID) else None,
            )
            self._apply_selection_op(hit_obj, basis_scope, op)
            return True

        # --- Klik na obszarze bbox grupy (long-press cycle) ---
        # Bbox is derived geometry, not a hit boundary.  A short click
        # passes through to the normal selection flow under the
        # cursor; a 500ms hold on empty bbox area selects the group as
        # a whole, and a 1000ms hold enters its edit mode.  Exported
        # pins keep their short-circuit so they remain clickable as
        # the group's external interface.
        if self._click_is_on_exported_pin(scene_pt):
            group_outline_hit = None
        else:
            group_outline_hit = (
                self._hit_test_group_bbox_empty(scene_pt)
                if self._filter_groups() else None
            )
        _log.info(
            "on_press.group_outline_decision",
            scene_pt=(round(scene_pt.x(), 2), round(scene_pt.y(), 2)),
            outline_hit=str(group_outline_hit)[:8] if group_outline_hit else None,
            on_exported_pin=self._click_is_on_exported_pin(scene_pt),
            current_group=str(self._current_group_id)[:8],
        )
        if group_outline_hit is not None:
            # Long-press timer takes over — start it and let the user
            # decide via hold duration what they want.  Click is
            # consumed so the normal selection flow doesn't fire.
            view_pt = self._view.mapFromScene(scene_pt) if self._view is not None else QPoint(0, 0)
            self._start_longpress(group_outline_hit, view_pt)
            _log.info(
                "on_press.longpress_started",
                group_id=str(group_outline_hit)[:8],
            )
            return True

        # The legacy outline-drag path is fully replaced by long-press;
        # the block below is reachable only when group_outline_hit is
        # None (preserved branch retained for diff hygiene during the
        # refactor — never executes).
        if False and group_outline_hit is not None:
            self._cancel_wire_edit()
            self._deselect_wire()
            groups = self._session.layout.groups
            grp_members = frozenset(all_component_ids(groups, group_outline_hit))
            _log.info(
                "on_press.group_outline.start_drag",
                group_id=str(group_outline_hit)[:8],
                n_members=len(grp_members),
            )
            if grp_members:
                self._clear_multi_selection()
                self._selected_group_id = group_outline_hit
                self._selected_ids = set(grp_members)
                first = next(iter(grp_members))
                self._select_primary(first)
                for cid in grp_members - {first}:
                    self._draw_multi_highlight(cid)
                # Begin drag of all group members.
                self._drag_overlay.reset(self._view.scene())
                self._corner_text.hide(self._view.scene())
                self._drag_last_decision = None
                self._stop_drag_validation_timer()
                self._dragging = True
                self._drag_press_scene_pt = QPointF(scene_pt)
                self._drag_threshold_crossed = False
                self._drag_orig_positions = {}
                self._drag_orig_wire_paths = {}
                snapped = self._view.snap(scene_pt, exclude_ids=frozenset(grp_members))
                for cid in grp_members:
                    pl = self._session.layout.placements.get(cid)
                    if pl is not None:
                        self._drag_orig_positions[cid] = pl.position
                pl_first = self._session.layout.placements.get(first)
                if pl_first is not None:
                    self._drag_anchor_offset = (
                        pl_first.position.x - snapped.x,
                        pl_first.position.y - snapped.y,
                    )
                # Collect owned wires via net-aware classifier.  This
                # picks up structurally-owned wires (both PinKnots in
                # group) AND half-orphan tails of internal nets, while
                # correctly excluding T-junction tails of exported nets
                # (which are interface wires, not group structure).
                self._snapshot_owned_wire_paths(
                    group_outline_hit, grp_members,
                    include_both_pin_in_set=False,
                )
            return True

        # --- Normal left click: hit-test component or wire ---
        hit = self._hit_test(scene_pt) if self._filter_components() else None
        _log.info(
            "on_press.hit_test_pin",
            hit_comp=str(hit)[:8] if hit else None,
            filter_components=self._filter_components(),
        )
        if hit is not None:
            # Block interaction with components outside the active group context.
            if not self._is_accessible(hit):
                _log.info("on_press.not_accessible", hit=str(hit)[:8])
                return False
            # Determine which pin (if any) was under the cursor — needed for
            # both shift-driven relation∩filter selection and auto-highlight.
            pin_hit = self._hit_test_pin_for_comp(scene_pt, hit) if self._view is not None else None
            pin_comp_id, pin_number = (pin_hit if pin_hit is not None else (hit, None))

            # Spec rev 2: Shift+LMB without Ctrl falls through to plain
            # LMB (SET, SINGLE).  No more "shift = extend".  Ctrl was
            # already routed above.

            # Phase 2c dispatch: build Hit, decode modifiers, apply op.
            # The pin-hit path still arms a drag below when SET-on-self
            # leaves the primary in place, so we keep the dispatcher
            # call inline rather than returning early.
            from protoboard.gui.selection_fsm import Hit
            hit_obj_pin: "Hit | None" = Hit("component", pin_comp_id) if pin_comp_id is not None else None
            basis_scope_pin, op_pin = self._decode_modifier_matrix(
                ctrl=ctrl, shift=shift, alt=alt,
            )
            _log.info(
                "on_press.dispatch.pin",
                op=op_pin.name, basis_scope=basis_scope_pin.name,
                pin_comp=str(pin_comp_id)[:8] if pin_comp_id else None,
                pin_number=pin_number,
            )
            self._apply_selection_op(hit_obj_pin, basis_scope_pin, op_pin)
            # The legacy pin-pin "primary-set + drag-arm" code below
            # remains so a plain LMB that lands on a pin can be dragged
            # into a new position.  The dispatcher already promoted
            # primary + populated multi; the legacy path then snapshots
            # original positions / wire paths for the drag.
            # If the hit component is in a group that we haven't entered yet,
            # redirect the selection to that group's representative ID.
            # Dispatcher above already executed the SET — `_selected_id`
            # is the hit (or proxy-resolved group anchor).  We only
            # need to detect group-locked promotion + drag-arm for
            # plain LMB.  Ctrl/Ctrl+Shift returned earlier so no drag
            # is ever armed under those modifiers.
            if ctrl:
                return True
            hit_pre_resolve = hit
            hit = self._resolve_group_hit(hit)
            _log.info(
                "on_press.normal_lpm.branch_pin",
                hit_pre_resolve=str(hit_pre_resolve)[:8],
                hit_post_resolve=str(hit)[:8],
                resolved_to_proxy=hit != hit_pre_resolve,
                n_selected_ids=len(self._selected_ids),
            )
            self._cancel_wire_edit()
            # If the dispatcher resolved hit-via-group, force the proxy
            # group id to be the primary so drag-arm anchors correctly.
            if hit != hit_pre_resolve:
                self._select_primary(hit)
            # Grouped-component press: click on a component that lives
            # inside a non-active group → arm a group drag.  Selection
            # of the group as a whole was already applied via
            # _resolve_group_hit + _select_primary above; here we
            # configure the drag set + owned-wire snapshot so a
            # subsequent mouse motion (>0.5 raster threshold) moves
            # the entire group, while a plain release leaves the
            # group selected but unmoved.  This replaces the legacy
            # outline-hit drag path with element-driven activation.
            if self._is_locked_in_group(hit):
                from protoboard.domain.layout.group import all_component_ids as _all_cids
                # Find the deepest non-active group containing `hit`.
                target_gid: uuid.UUID | None = None
                groups_map = self._session.layout.groups
                active_gid = self._current_group_id
                for gid_iter, g_iter in groups_map.items():
                    if gid_iter == active_gid:
                        continue
                    if hit in g_iter.component_ids and gid_iter != active_gid:
                        target_gid = gid_iter
                        break
                if target_gid is None:
                    # Conservative fall-back: no draggable group resolved.
                    return True
                grp_members = frozenset(_all_cids(groups_map, target_gid))
                if not grp_members:
                    return True
                # Expand selection to all group members so multi-highlight
                # serves as the visible "drag will move the whole group"
                # warning before threshold is crossed.  The clicked
                # component (`hit`) MUST become the primary — it is the
                # anchor used by `on_move` to compute the drag delta.
                # Picking an arbitrary `next(iter(grp_members))` here
                # left the anchor offset pointing at one component
                # while `_selected_id` referenced a different one,
                # causing the whole group to slide by (anchor_comp -
                # primary_comp) on the first cursor move (user-observed
                # 2026-06-02: group jumps out from under the cursor).
                self._clear_multi_selection()
                self._selected_group_id = target_gid
                self._selected_ids = set(grp_members)
                self._select_primary(hit)
                for cid in grp_members - {hit}:
                    self._draw_multi_highlight(cid)
                # Arm the drag.
                self._drag_overlay.reset(self._view.scene())
                self._corner_text.hide(self._view.scene())
                self._drag_last_decision = None
                self._stop_drag_validation_timer()
                self._dragging = True
                self._drag_press_scene_pt = QPointF(scene_pt)
                self._drag_threshold_crossed = False
                self._drag_orig_positions = {}
                self._drag_orig_wire_paths = {}
                snapped = self._view.snap(scene_pt, exclude_ids=frozenset(grp_members))
                for cid in grp_members:
                    pl = self._session.layout.placements.get(cid)
                    if pl is not None:
                        self._drag_orig_positions[cid] = pl.position
                pl_hit = self._session.layout.placements.get(hit)
                if pl_hit is not None:
                    self._drag_anchor_offset = (
                        pl_hit.position.x - snapped.x,
                        pl_hit.position.y - snapped.y,
                    )
                # Owned-wire snapshot via net-aware classifier.  Element-
                # press drag also adds "both PinKnots on group pins" wires
                # so manually-routed exported-net wires between group
                # members travel with the drag too (otherwise they turn
                # skewed and get dropped — bug 2026-06-02 producing
                # SHORTs on reference_big_project).
                self._snapshot_owned_wire_paths(
                    target_gid, grp_members,
                    include_both_pin_in_set=True,
                )
                _log.info(
                    "on_press.group_drag_armed",
                    group_id=str(target_gid)[:8],
                    n_members=len(grp_members),
                    n_owned_wires=len(self._drag_orig_wire_paths),
                )
                return True
            # Begin drag.
            self._drag_overlay.reset(self._view.scene())
            self._corner_text.hide(self._view.scene())
            self._drag_last_decision = None
            self._stop_drag_validation_timer()
            self._dragging = True
            self._drag_press_scene_pt = QPointF(scene_pt)
            self._drag_threshold_crossed = False
            self._drag_orig_positions = {}
            self._drag_orig_wire_paths = {}
            drag_ids = self._selected_ids if self._selected_ids else {hit}
            n_drag_ids_before_filter = len(drag_ids)
            # Drop grouped-but-not-active components from the drag set so
            # multi-selections that span both free and grouped components
            # only carry the free ones.
            drag_ids = {cid for cid in drag_ids if not self._is_locked_in_group(cid)}
            _log.info(
                "on_press.drag_start.normal_lpm",
                hit=str(hit)[:8],
                n_drag_ids_before_filter=n_drag_ids_before_filter,
                n_drag_ids_after_group_filter=len(drag_ids),
                n_selected_ids=len(self._selected_ids),
                n_selected_wire_ids=len(self._selected_wire_ids),
                hit_in_drag_ids=hit in drag_ids,
            )
            snapped = self._view.snap(scene_pt, exclude_ids=frozenset(drag_ids))
            for cid in drag_ids:
                pl = self._session.layout.placements.get(cid)
                if pl is not None:
                    self._drag_orig_positions[cid] = pl.position
            if hit in drag_ids:
                pl = self._session.layout.placements.get(hit)
                if pl is not None:
                    self._drag_anchor_offset = (
                        pl.position.x - snapped.x,
                        pl.position.y - snapped.y,
                    )
            # Snapshot original wire paths for any selected wires.
            # Iterate selection (typically tiny) and look up by id, not the
            # other way around — the full wire list can be thousands long.
            n_wire_drop_anchor_outside = 0
            for wid in self._selected_wire_ids:
                w = self._session.wire_by_id(wid)
                if w is None:
                    continue
                if not self._wire_safe_to_drag(w, drag_ids):
                    n_wire_drop_anchor_outside += 1
                    continue
                self._drag_orig_wire_paths[wid] = list(w.path)
            _log.info(
                "on_press.drag_ready",
                n_drag_orig_positions=len(self._drag_orig_positions),
                n_drag_orig_wire_paths=len(self._drag_orig_wire_paths),
                n_wire_drop_anchor_outside=n_wire_drop_anchor_outside,
            )
            return True

        wire_hit = self._hit_test_wire(scene_pt) if self._filter_wires() else None
        _log.info(
            "on_press.hit_test_wire",
            wire_hit=str(wire_hit)[:8] if wire_hit else None,
            in_selection=(wire_hit in self._selected_wire_ids) if wire_hit else None,
        )
        if wire_hit is not None:
            self._cancel_wire_edit()
            # Spec rev 2: route plain / Alt LMB through the dispatcher.
            # Ctrl / Ctrl+Shift were already routed above.
            from protoboard.gui.selection_fsm import Hit
            wire_was_primary_before = (wire_hit == self._selected_wire_id)
            basis_scope_w, op_w = self._decode_modifier_matrix(
                ctrl=ctrl, shift=shift, alt=alt,
            )
            _log.info(
                "on_press.dispatch.wire",
                op=op_w.name, basis_scope=basis_scope_w.name,
                wire=str(wire_hit)[:8],
                was_primary=wire_was_primary_before,
            )
            self._apply_selection_op(Hit("wire", wire_hit), basis_scope_w, op_w)

            # Drag-arm: only when SET resolved to "this wire is now
            # the selection" (or already was) — equivalent to the
            # legacy "wire_hit in self._selected_wire_ids" gate.
            if wire_hit != self._selected_wire_id and wire_hit not in self._selected_wire_ids:
                return True
            snapped = self._view.snap(scene_pt)
            self._drag_last_decision = None
            self._stop_drag_validation_timer()
            self._dragging = True
            self._drag_press_scene_pt = QPointF(scene_pt)
            self._drag_threshold_crossed = False
            self._drag_orig_positions = {}
            self._drag_orig_wire_paths = {}
            wire_obj = self._session.wire_by_id(wire_hit)
            if wire_obj and wire_obj.path:
                self._drag_anchor_offset = (
                    wire_obj.path[0].x - snapped.x,
                    wire_obj.path[0].y - snapped.y,
                )
                self._selected_id = None
            drag_ids_local = {
                cid for cid in self._selected_ids
                if not self._is_locked_in_group(cid)
            }
            for cid in drag_ids_local:
                pl = self._session.layout.placements.get(cid)
                if pl is not None:
                    self._drag_orig_positions[cid] = pl.position
            for wid in self._selected_wire_ids:
                wobj = self._session.wire_by_id(wid)
                if wobj is None:
                    continue
                if not self._wire_safe_to_drag(wobj, drag_ids_local):
                    continue
                self._drag_orig_wire_paths[wid] = list(wobj.path)
            # Also include the primary single-wire-selection in the
            # drag snapshot — when SET created a fresh primary the
            # multi-set is empty but the user can still drag it.
            if (
                self._selected_wire_id is not None
                and self._selected_wire_id not in self._drag_orig_wire_paths
            ):
                primary_wire = self._session.wire_by_id(self._selected_wire_id)
                if primary_wire is not None:
                    self._drag_orig_wire_paths[self._selected_wire_id] = list(primary_wire.path)
            return True

        # Check if click is inside bbox of an already-selected component.
        # This allows dragging a multi-selection by clicking anywhere inside
        # a selected component's body, not just near a pin.
        bbox_hit = self._hit_test_bbox(scene_pt) if self._filter_components() else None
        _log.info(
            "on_press.hit_test_bbox",
            bbox_hit=str(bbox_hit)[:8] if bbox_hit else None,
            in_selection=(bbox_hit in self._selected_ids) if bbox_hit else None,
            is_primary=(bbox_hit == self._selected_id) if bbox_hit else None,
        )
        if bbox_hit is not None and (
            bbox_hit in self._selected_ids
            or bbox_hit == self._selected_id
        ):
            self._cancel_wire_edit()
            # Spec rev 2: bbox-on-selected → dispatcher (SET/SINGLE
            # collapses to no-op on same primary), then drag-arm if
            # plain LMB.
            from protoboard.gui.selection_fsm import Hit
            basis_scope_b, op_b = self._decode_modifier_matrix(
                ctrl=ctrl, shift=shift, alt=alt,
            )
            _log.info(
                "on_press.dispatch.bbox",
                op=op_b.name, basis_scope=basis_scope_b.name,
                bbox=str(bbox_hit)[:8],
            )
            self._apply_selection_op(Hit("component", bbox_hit), basis_scope_b, op_b)
            # Promote bbox_hit explicitly — `_apply_selection_op` may
            # have left it untouched in the no-op same-primary branch
            # but downstream drag-arm code expects `_selected_id ==
            # bbox_hit` regardless.
            if self._selected_id != bbox_hit and not ctrl:
                self._select_primary(bbox_hit)
            # Grouped-component protection — see the matching guard above.
            if self._is_locked_in_group(bbox_hit):
                return True
            # Ctrl / Ctrl+Shift never initiate drag (spec rev 2).
            if ctrl:
                return True
            self._drag_last_decision = None
            self._stop_drag_validation_timer()
            self._dragging = True
            self._drag_press_scene_pt = QPointF(scene_pt)
            self._drag_threshold_crossed = False
            self._drag_orig_positions = {}
            self._drag_orig_wire_paths = {}
            drag_ids = self._selected_ids if self._selected_ids else {bbox_hit}
            n_drag_ids_before_filter = len(drag_ids)
            drag_ids = {cid for cid in drag_ids if not self._is_locked_in_group(cid)}
            _log.info(
                "on_press.drag_start.bbox_lpm",
                bbox_hit=str(bbox_hit)[:8],
                n_drag_ids_before_filter=n_drag_ids_before_filter,
                n_drag_ids_after_group_filter=len(drag_ids),
            )
            snapped = self._view.snap(scene_pt, exclude_ids=frozenset(drag_ids))
            for cid in drag_ids:
                pl = self._session.layout.placements.get(cid)
                if pl is not None:
                    self._drag_orig_positions[cid] = pl.position
            pl = self._session.layout.placements.get(bbox_hit)
            if pl is not None:
                self._drag_anchor_offset = (
                    pl.position.x - snapped.x,
                    pl.position.y - snapped.y,
                )
            for wid in self._selected_wire_ids:
                w = self._session.wire_by_id(wid)
                if w is None:
                    continue
                if not self._wire_safe_to_drag(w, drag_ids):
                    continue
                self._drag_orig_wire_paths[wid] = list(w.path)
            return True

        # Nothing hit — empty-canvas click is equivalent to Esc per
        # docs/highlight-state-machine.txt.  Pop one selection layer
        # (multi → selection → highlight → normal) instead of clearing
        # everything; the user can still wipe the slate with two or
        # three Esc presses.  Rubber-band still starts so a click+drag
        # in empty space performs a marquee select.
        _log.info("on_press.nothing_hit_rubber_band")
        self._cancel_wire_edit()
        self._esc_pop_one_layer()
        self._rubber_band_start = scene_pt
        return True

    def on_double_press(self, scene_pt: QPointF) -> bool:
        """Called on double-click by BoardView (if forwarded). Enters a group."""
        # Floating state: double-click anywhere commits.
        if self._is_floating():
            self._commit_floating()
            return True
        hit = self._hit_test(scene_pt)
        if hit is None:
            hit = self._hit_test_bbox(scene_pt)
        if hit is None:
            return False
        groups = self._session.layout.groups
        parent_group = groups.get(self._current_group_id)
        if parent_group is None:
            return False
        # Find which child group of the current group contains this component.
        for child_gid in parent_group.child_group_ids:
            if hit in all_component_ids(groups, child_gid):
                self._group_stack.append(child_gid)
                _log.info(
                    "group.entered",
                    group_id=str(child_gid)[:8],
                    stack_depth=len(self._group_stack),
                )
                self._clear_multi_selection()
                self._deselect()
                self._select_primary(hit)
                self._notify_active_group()
                return True
        return False

    def on_move(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)

        # Long-press jitter cancellation: any cursor motion beyond
        # _GROUP_LONGPRESS_CANCEL_PX viewport pixels aborts the hold.
        if self._longpress_phase != "idle" and self._view is not None:
            view_pt = self._view.mapFromScene(scene_pt)
            self._longpress_check_jitter(view_pt)

        if self._redrag_comp_id is not None:
            snapped = self._view.snap(scene_pt)
            self._update_redrag(snapped)
            return

        if self._is_editing_wire():
            self._update_edit_live(scene_pt)
            return

        if self._rubber_band_start is not None:
            self._update_rubber_band(scene_pt)
            return

        if self._dragging:
            # Drag-start threshold: don't mutate anything until the
            # cursor has travelled at least _DRAG_START_THRESHOLD raster
            # units from the press point.  Otherwise sub-pixel jitter
            # on a plain LPM click is interpreted as a drag and the
            # subsequent on_release path tries to "commit a move that
            # didn't happen" (user-observed 2026-06-01).
            if (
                not self._drag_threshold_crossed
                and self._drag_press_scene_pt is not None
            ):
                dx = scene_pt.x() - self._drag_press_scene_pt.x()
                dy = scene_pt.y() - self._drag_press_scene_pt.y()
                if dx * dx + dy * dy < _DRAG_START_THRESHOLD * _DRAG_START_THRESHOLD:
                    return
                self._drag_threshold_crossed = True
                # Phase 2b: drag started — this click is no longer a
                # candidate for pop-on-release.
                if self._pending_deselect_on_release:
                    if _dbg_enabled():
                        _log.debug("on_move.pending_deselect_cleared")
                    self._pending_deselect_on_release = False
                _log.info(
                    "on_move.threshold_crossed",
                    dist=round((dx * dx + dy * dy) ** 0.5, 3),
                    n_drag_orig_positions=len(self._drag_orig_positions),
                    selected_group=str(self._selected_group_id)[:8] if self._selected_group_id else None,
                )
            anchor_id = self._selected_id
            drag_ids = (
                self._selected_ids if self._selected_ids
                else ({anchor_id} if anchor_id else set())
            )
            # Log once per drag start (when delta first becomes non-zero).
            if not getattr(self, "_drag_logged_once", False):
                _log.info(
                    "on_move.drag_first_tick",
                    n_drag_ids=len(drag_ids),
                    n_drag_orig_positions=len(self._drag_orig_positions),
                    anchor_id=str(anchor_id)[:8] if anchor_id else None,
                    n_selected_ids=len(self._selected_ids),
                )
                self._drag_logged_once = True
            # Snap the ANCHOR (pin 1 / wire start) to the nearest pad, not the
            # raw cursor.  Project where the anchor would land for the raw
            # cursor position, then snap that to the pad grid — this way the
            # anchor always lands on a pad regardless of where the user
            # grabbed the component (off-pin click) or how the component got
            # off-grid (e.g. previous drag that left the board area).
            dx_off, dy_off = self._drag_anchor_offset
            raw_anchor = QPointF(scene_pt.x() + dx_off, scene_pt.y() + dy_off)
            anchor_new = self._view.snap(
                raw_anchor, exclude_ids=frozenset(drag_ids)
            )

            # Compute delta from anchor.
            delta: Point | None = None
            if anchor_id is not None:
                anchor_orig = self._drag_orig_positions.get(anchor_id)
                if anchor_orig is not None:
                    delta = Point(
                        anchor_new.x - anchor_orig.x,
                        anchor_new.y - anchor_orig.y,
                    )
            if delta is None and self._drag_orig_wire_paths:
                # Drag started from a wire — compute delta from first wire's
                # first point vs. anchor_new.
                first_wid = next(iter(self._drag_orig_wire_paths))
                first_orig = self._drag_orig_wire_paths[first_wid]
                if first_orig:
                    delta = Point(
                        anchor_new.x - first_orig[0].x,
                        anchor_new.y - first_orig[0].y,
                    )
            if delta is None:
                return
            _delta: Point = delta  # narrow type for mypy

            # Move components.
            for cid in drag_ids:
                pl = self._session.layout.placements.get(cid)
                orig = self._drag_orig_positions.get(cid)
                if pl is not None and orig is not None:
                    pl.position = Point(orig.x + _delta.x, orig.y + _delta.y)

            # Move wires (translate all path points).
            for wire in self._session.layout.wires:
                if wire.id not in self._drag_orig_wire_paths:
                    continue
                orig_path: list[Point] = self._drag_orig_wire_paths[wire.id]
                wire.path = [
                    Point(p.x + _delta.x, p.y + _delta.y) for p in orig_path
                ]

            self._session.drag_moved.emit()
            # Move selection-rect highlights with the dragged components so
            # the frame tracks the new position instead of staying behind.
            if self._multi_highlights:
                self._redraw_multi_highlights()
            self._update_drag_overlay(drag_ids, anchor_new)
            # Debounce full classify_drag_commit pass: restart on each snap
            # change.  Fires 1 s after the user stops moving.
            self._start_drag_validation_timer()
            return

        if ctrl:
            hit = self._hit_test_bbox(scene_pt)
            if hit is not None:
                self._view.setCursor(Qt.CursorShape.SizeAllCursor)
            else:
                self._view.setCursor(Qt.CursorShape.ArrowCursor)

    def on_release(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        # Long-press release.  If we never fired the timer (release
        # before 500ms), the user wants a regular click — pass through
        # to the normal selection flow without selecting the group.
        # If the select-phase fired but edit-phase did not, the group
        # is selected and we just clean up the timer.
        if self._longpress_phase != "idle":
            phase = self._longpress_phase
            self._cancel_longpress()
            if phase == "select":
                # Already selected the group at the 500ms mark — leave
                # the selection in place, just exit.
                return
            # phase was still "edit" pending; nothing was selected yet
            # (release before 500ms).  Fall through so the user gets
            # a normal click outcome (deselect / hit-test under cursor).
            # We can't replay on_press cleanly, so for now just treat
            # as a no-op release (the user can re-click).
            return

        # Rubber-band release: apply selection.
        if self._rubber_band_start is not None:
            self._finish_rubber_band(scene_pt)
            return

        if not self._dragging:
            return
        # Pure click (threshold never crossed) — selection was already
        # applied in on_press, and nothing was mutated.  Reset the
        # drag-pending state and exit without invoking the floating
        # pipeline.
        threshold_crossed = self._drag_threshold_crossed
        pending_pop = self._pending_deselect_on_release
        self._pending_deselect_on_release = False
        self._dragging = False
        self._drag_press_scene_pt = None
        self._drag_threshold_crossed = False
        self._stop_drag_validation_timer()
        if not threshold_crossed:
            self._drag_orig_positions = {}
            self._drag_orig_wire_paths = {}
            # Phase 2b: same-primary press + release without drag →
            # pop one FSM layer (deselect, keep highlight).  Matches
            # the [normal, highlight, selection] -(lmb-click{same-object})
            # -> [normal, highlight] rule from docs/highlight-state-machine.txt.
            if pending_pop:
                if _dbg_enabled():
                    _log.debug("on_release.pop_one_layer.same_primary")
                self._esc_pop_one_layer()
            return

        drag_ids = (
            self._selected_ids
            if self._selected_ids
            else ({self._selected_id} if self._selected_id else set())
        )
        if not drag_ids and not self._drag_orig_wire_paths:
            return

        from protoboard.domain.commands.base import Command

        commands: list[Command[ComponentState]] = []

        # Resolve effective group for this drag (either from outline-click or
        # by detecting that all drag_ids belong to the same group).
        effective_group_id = self._selected_group_id
        # Guard against stale selected_group_id (e.g. after disband).
        if effective_group_id is not None and effective_group_id not in self._session.layout.groups:
            self._selected_group_id = None
            effective_group_id = None
        if effective_group_id is None and drag_ids:
            effective_group_id = self._common_group_for(drag_ids)

        _log.debug("drag.release",
                   drag_ids=[str(i) for i in drag_ids],
                   selected_group_id=str(self._selected_group_id) if self._selected_group_id else None,
                   effective_group_id=str(effective_group_id) if effective_group_id else None,
                   orig_positions={str(k): (v.x, v.y) for k, v in self._drag_orig_positions.items()},
                   current_positions={str(k): (v.position.x, v.position.y)
                                      for k, v in self._session.layout.placements.items()
                                      if k in drag_ids})

        if effective_group_id is not None:
            # --- Atomic group drag: check all conflicts first ---
            from protoboard.domain.layout.group import all_component_ids
            full_ids = set(all_component_ids(
                self._session.layout.groups, effective_group_id
            ))
            full_ids &= set(self._drag_orig_positions.keys())
            effective_drag = full_ids if full_ids else drag_ids
            _saved_group_id = self._selected_group_id
            self._selected_group_id = effective_group_id
            self._group_drag_block_pads = []
            commands = self._build_group_drag_commands(effective_drag) or []
            self._selected_group_id = _saved_group_id
            # Blocked branch: push the whole group into floating-state
            # HARD so the user gets the red overlay + Esc/Enter UX
            # instead of a silent revert + status-bar message.
            if self._group_drag_block_pads:
                from protoboard.gui.tools.drag_commit_decision import (
                    DragCommitDecision,
                    DragSeverity,
                )
                floating_orig: dict[uuid.UUID, ComponentPlacement] = {}
                for cid in effective_drag:
                    pl = self._session.layout.placements.get(cid)
                    orig = self._drag_orig_positions.get(cid)
                    if pl is None or orig is None:
                        continue
                    old_pl = pl.copy()
                    old_pl.position = orig
                    floating_orig[cid] = old_pl
                hard_decision = DragCommitDecision(
                    severity=DragSeverity.HARD,
                    description="Group drag would short different nets",
                    blocking_pad_positions=tuple(self._group_drag_block_pads),
                )
                blocking_pads_copy = list(self._group_drag_block_pads)
                self._group_drag_block_pads = []
                self._enter_floating(
                    set(effective_drag),
                    floating_orig,
                    extra_cmds=[],
                    dragged_wire_ids=frozenset(self._drag_orig_wire_paths.keys()),
                    decision=hard_decision,
                )
                self._drag_orig_wire_paths = {}
                self._session.layout_changed.emit()
                _log.info(
                    "drag.release.group_blocked_floating",
                    n_blocking_pads=len(blocking_pads_copy),
                )
                return
            # Group drag with wire removals or other SOFT conditions →
            # enter floating SOFT so the user gets a yellow overlay and
            # explicit Enter/Esc/double-click confirmation, matching
            # the single-component drag UX.  This is what tells the
            # user that the commit will delete N wires before they
            # accept it.
            soft_remove_cmds = [
                c for c in commands
                if type(c).__name__ == "RemoveWireCommand"
            ]
            if soft_remove_cmds:
                from protoboard.gui.tools.drag_commit_decision import (
                    DragCommitDecision,
                    DragSeverity,
                )
                floating_orig_soft: dict[uuid.UUID, ComponentPlacement] = {}
                for cid in effective_drag:
                    pl = self._session.layout.placements.get(cid)
                    orig = self._drag_orig_positions.get(cid)
                    if pl is None or orig is None:
                        continue
                    old_pl = pl.copy()
                    old_pl.position = orig
                    floating_orig_soft[cid] = old_pl
                # Non-RemoveWireCommand entries (Move + ReplaceFragments)
                # are rebuilt by _commit_floating from the floating
                # snapshot; the only extras we need to forward are the
                # wire removals the user is being asked to accept.
                n_removable = len(soft_remove_cmds)
                soft_decision = DragCommitDecision(
                    severity=DragSeverity.SOFT,
                    description=(
                        f"Group drop removes {n_removable} wire(s) "
                        f"— [Enter] confirm, [Esc] cancel"
                    ),
                    auto_remove_wire_ids=tuple(
                        c.wire_id for c in soft_remove_cmds  # type: ignore[attr-defined]
                    ),
                )
                self._enter_floating(
                    set(effective_drag),
                    floating_orig_soft,
                    extra_cmds=list(soft_remove_cmds),
                    dragged_wire_ids=frozenset(self._drag_orig_wire_paths.keys()),
                    decision=soft_decision,
                )
                self._drag_orig_wire_paths = {}
                self._session.layout_changed.emit()
                _log.info(
                    "drag.release.group_soft_floating",
                    n_remove=n_removable,
                )
                return
            self._drag_orig_wire_paths = {}
            if commands:
                try:
                    if len(commands) == 1:
                        self._session.stack.execute(commands[0], self._session)
                    else:
                        macro = MacroCommand(commands, description="Move selection")
                        self._session.stack.execute(macro, self._session)
                except Exception as exc:
                    # Group-drag commit raised — log enough context to
                    # tell which sub-command in the macro broke things
                    # and how much of the layout already mutated before
                    # the abort (MacroCommand has no rollback today, so
                    # partial state survives the exception and shows up
                    # as orphan wires / split groups in subsequent
                    # interactions).
                    cmd_summary = [
                        {
                            "i": i,
                            "type": type(c).__name__,
                            **(
                                {"wire_id": str(getattr(c, "wire_id"))[:8]}
                                if hasattr(c, "wire_id") else {}
                            ),
                            **(
                                {"comp_id": str(getattr(c, "component_id"))[:8]}
                                if hasattr(c, "component_id") else {}
                            ),
                        }
                        for i, c in enumerate(commands)
                    ]
                    _log.error(
                        "drag.release.group_macro_failed",
                        exc_type=type(exc).__name__,
                        exc_msg=str(exc),
                        n_commands=len(commands),
                        commands=cmd_summary,
                    )
                    raise
                self._session.layout_changed.emit()
            self._drag_overlay.reset(self._view.scene())
            self._drag_last_decision = None
            self._refresh_all_highlights()
            return

        # --- Non-group drag: classifier-driven pipeline ---
        from protoboard.gui.placement_conflict import (
            build_wire_removal_cmds,
            classify_drag_commit,
        )
        from protoboard.gui.tools.drag_commit_decision import DragSeverity

        # Build floating originals from drag-start snapshot (used for both
        # commit-as-undo-old-state and Esc-revert).
        floating_orig: dict[uuid.UUID, ComponentPlacement] = {}
        floating_moved: set[uuid.UUID] = set()
        for cid in drag_ids:
            pl = self._session.layout.placements.get(cid)
            orig = self._drag_orig_positions.get(cid)
            if pl is None or orig is None:
                continue
            if pl.position == orig:
                continue
            old_pl = pl.copy()
            old_pl.position = orig
            floating_orig[cid] = old_pl
            floating_moved.add(cid)

        dragged_wire_ids = frozenset(self._drag_orig_wire_paths.keys())
        # Only run the drag classifier when *something actually moved*.
        # A bare click (mouse-down + immediate mouse-up at the same
        # snap) used to fall into this branch with
        # ``moved_comp_ids=drag_ids`` — the classifier then reported
        # every wire legitimately soldered to the clicked component's
        # pins as a rule-6-strict conflict and the wire-only branch
        # below silently executed ``RemoveWireCommand`` on each one
        # (user-observed bug 2026-05-31: "klik na pin = odłączenie
        # przewodów").  Skip classification when nothing moved.
        if floating_moved or self._drag_orig_wire_paths:
            # drag_set_members enables the unified drag model in
            # DragDetector — 5-way classification + preference buckets.
            decision = classify_drag_commit(
                session=self._session,
                moved_comp_ids=floating_moved or drag_ids,
                dragged_wire_ids=dragged_wire_ids,
                drag_set_members=frozenset(floating_moved or drag_ids),
            )
        else:
            from protoboard.gui.tools.drag_commit_decision import (
                DragCommitDecision,
                DragSeverity,
            )
            decision = DragCommitDecision(severity=DragSeverity.CLEAR)

        # Wire-path-change commands for live-dragged wires that need a
        # ReplaceWireFragmentsCommand entry in undo history.
        wire_move_cmds: list[object] = []
        for wire in self._session.layout.wires:
            orig_wire_path = self._drag_orig_wire_paths.get(wire.id)
            if orig_wire_path is None:
                continue
            if wire.path == orig_wire_path:
                continue
            wire_move_cmds.append(
                _replace_wire_path_cmd(
                    self._session.layout, wire, list(wire.path), orig_wire_path
                )
            )

        # Auto-actions for SOFT commit: remove crossing/foreign/skewed wires,
        # plus net actions.
        extra_cmds: list[object] = list(wire_move_cmds)
        if decision.auto_remove_wire_ids:
            extra_cmds.extend(
                build_wire_removal_cmds(set(decision.auto_remove_wire_ids))
            )
        # Dedup RemoveWireCommands (same wire may appear as both move-target
        # and auto-remove).
        _seen_remove: set[uuid.UUID] = set()
        deduped: list[object] = []
        for cmd in extra_cmds:
            if isinstance(cmd, RemoveWireCommand):
                if cmd.wire_id in _seen_remove:
                    continue
                _seen_remove.add(cmd.wire_id)
            # If a wire is both being path-replaced AND auto-removed, the
            # remove wins (the replace would re-add the bad path on undo).
            if isinstance(cmd, ReplaceWireFragmentsCommand):
                if cmd.wire_id in decision.auto_remove_wire_ids:
                    continue
            deduped.append(cmd)
        extra_cmds = deduped

        self._drag_orig_wire_paths = {}

        # --- Branch by severity ---
        if not floating_moved:
            # Wire-only drag (no components moved).
            if extra_cmds:
                if len(extra_cmds) == 1:
                    self._session.stack.execute(extra_cmds[0], self._session)  # type: ignore[arg-type]
                else:
                    macro = MacroCommand(extra_cmds, description="Move wires")  # type: ignore[arg-type]
                    self._session.stack.execute(macro, self._session)
                self._session.layout_changed.emit()
            self._drag_overlay.reset(self._view.scene())
            self._corner_text.hide(self._view.scene())
            self._refresh_all_highlights()
            return

        if decision.severity == DragSeverity.CLEAR:
            # Honor auto-accept preference — direct commit when clean.
            from protoboard.gui.app_settings import load_app_settings
            auto_accept = load_app_settings().auto_accept_placement
            if auto_accept:
                all_cmds: list[object] = list(extra_cmds)
                for cid in floating_moved:
                    new_pl = self._session.layout.placements.get(cid)
                    old_pl = floating_orig.get(cid)
                    if new_pl and old_pl:
                        all_cmds.append(MoveComponentCommand(
                            component_id=cid,
                            new_placement=new_pl,
                            old_placement=old_pl,
                            skip_conflict_check=True,
                            exclude_wire_ids=dragged_wire_ids,
                        ))
                if all_cmds:
                    if len(all_cmds) == 1:
                        self._session.stack.execute(all_cmds[0], self._session)  # type: ignore[arg-type]
                    else:
                        macro = MacroCommand(all_cmds, description="Move selection")  # type: ignore[arg-type]
                        self._session.stack.execute(macro, self._session)
                    self._session.layout_changed.emit()
                self._drag_overlay.reset(self._view.scene())
                self._corner_text.hide(self._view.scene())
                self._refresh_all_highlights()
                return
            # CLEAR + auto-accept off → fall through into floating state.

        # CLEAR (no auto-accept), SOFT, or HARD → enter floating state.
        # For HARD, commit is blocked but the user can keep dragging or Esc.
        self._enter_floating(
            floating_moved, floating_orig, extra_cmds,
            dragged_wire_ids=dragged_wire_ids,
            decision=decision,
        )
        self._session.layout_changed.emit()
        if not self._is_floating():
            self._refresh_all_highlights()

    def on_key(self, event: QKeyEvent) -> None:
        key = event.key()

        if self._is_editing_wire():
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._commit_wire_edit()
                event.accept()
            elif key == Qt.Key.Key_Escape:
                self._cancel_wire_edit()
                event.accept()
            elif key == Qt.Key.Key_A:
                self._autoroute_wire_fragment()
                event.accept()
            elif key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
                self._delete_wire_fragment()
                event.accept()
            return

        # Floating state: Enter commits, Esc cancels, R rotates.
        if self._is_floating():
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._commit_floating()
                event.accept()
            elif key == Qt.Key.Key_Escape:
                self._cancel_floating()
                event.accept()
            elif key == Qt.Key.Key_R:
                delta = -90 if bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 90
                if self._selected_ids:
                    self._rotate_multi(delta=delta)
                elif self._selected_id is not None:
                    self._rotate_selected(delta=delta)
                event.accept()
            return

        ctrl  = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        alt   = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)

        # Re-drag mode: Esc cancels
        if self._redrag_comp_id is not None:
            if key == Qt.Key.Key_Escape:
                self._cancel_redrag()
                event.accept()
            return

        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            # Shift+Delete forces deletion through the wire-lock guard
            # — bulk operations like "Select All Wires" + Delete expect
            # to remove every selection regardless of group ownership.
            # Without force, structurally-owned wires of a non-active
            # group are silently skipped (Step 4 of group-selection
            # refactor 2026-06-01).
            if self._selected_wire_ids or self._selected_wire_id is not None:
                self._delete_multi_wires(force=shift)
            elif self._selected_ids:
                self._delete_multi()
            elif self._selected_id is not None:
                self._delete_selected()
            event.accept()
        elif key == Qt.Key.Key_R and ctrl:
            if self._selected_id is not None:
                self._start_redrag(self._selected_id)
            event.accept()
        elif key == Qt.Key.Key_R and alt and not ctrl:
            if self._selected_ids:
                self._flip_multi()
            elif self._selected_id is not None:
                self._flip_selected()
            event.accept()
        elif key == Qt.Key.Key_R and not ctrl and not alt:
            if self._selected_ids:
                self._rotate_multi(delta=-90 if shift else 90)
            elif self._selected_id is not None:
                self._rotate_selected(delta=-90 if shift else 90)
            event.accept()
        elif key == Qt.Key.Key_G and ctrl:
            self._group_for_selection()
        elif key == Qt.Key.Key_Escape:
            from protoboard.domain.layout.layout import ROOT_GROUP_ID
            if len(self._group_stack) > 1:  # inside a subgroup
                self.exit_group()
            else:
                self._esc_pop_one_layer()
        elif key == Qt.Key.Key_QuoteLeft:
            # Backtick: cycle through relation classes.
            from protoboard.gui.selection_axes import RelationClass
            relations = list(RelationClass)
            idx = relations.index(self._relation) if self._relation in relations else 0
            self.set_relation(relations[(idx + 1) % len(relations)])

    def on_key_release(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Control and not self._dragging:
            self._view.setCursor(Qt.CursorShape.ArrowCursor)

    def on_right_press(self, scene_pt: QPointF) -> bool:
        """Show unified context menu for all elements under the cursor."""
        # Resolve all hits independently — pin, body, wire.
        comp_id = self._selected_id
        if comp_id is None:
            comp_id = self._hit_test(scene_pt)
        if comp_id is None:
            comp_id = self._hit_test_bbox(scene_pt)

        wire_id = self._selected_wire_id
        if wire_id is None:
            wire_id = self._hit_test_wire(scene_pt)

        if comp_id is None and wire_id is None:
            return False

        from PySide6.QtWidgets import QMenu
        from PySide6.QtWidgets import QGraphicsEllipseItem
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QPen, QBrush
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        from protoboard.domain.commands.component_cmds import LockComponentCommand
        from protoboard.gui.layer_state import Z_OVERLAY

        menu = QMenu(self._view)
        has_items = True  # View section is always present

        # ── View section (universal) ──────────────────────────────────────────
        act_center = menu.addAction("Center view here")
        act_center.setData(("view_center", scene_pt))
        act_escape = menu.addAction("Cancel tool (Esc)")
        act_escape.setData(("view_escape",))
        menu.addSeparator()

        # Temporary hover-highlight items (pin circle on scene during menu hover).
        _hover_items: list[object] = []

        def _clear_hover() -> None:
            sc = self._view.scene() if self._view is not None else None
            if sc is not None:
                for it in _hover_items:
                    sc.removeItem(it)  # type: ignore[arg-type]
            _hover_items.clear()

        def _hover_pin(cid: uuid.UUID, pn: int) -> None:
            _clear_hover()
            sc = self._view.scene() if self._view is not None else None
            if sc is None:
                return
            _pos = self._session.get_pin_position(cid, pn)
            if _pos is None:
                return
            r = 0.28
            circle = QGraphicsEllipseItem(QRectF(_pos.x - r, _pos.y - r, 2 * r, 2 * r))
            circle.setPen(QPen(QColor(255, 180, 0, 220), 0.06))
            circle.setBrush(QBrush(QColor(255, 180, 0, 100)))
            circle.setZValue(Z_OVERLAY + 2.0)
            sc.addItem(circle)
            _hover_items.append(circle)

        menu.aboutToHide.connect(_clear_hover)

        # Detect the nearest pin to the click for the marker.
        clicked_pin: int | None = None
        if comp_id is not None:
            _pin_hit = self._hit_test_pin_for_comp(scene_pt, comp_id)
            if _pin_hit is not None:
                clicked_pin = _pin_hit[1]

        # ── Component section ─────────────────────────────────────────────────
        if comp_id is not None:
            inst = self._session.instances.get(comp_id)
            defn = self._session.components.get(inst.definition_ref) if inst else None

            if inst is not None and defn is not None:
                has_items = True

                # Lock / Unlock
                lock_targets: set[uuid.UUID] = {comp_id}
                lock_targets.update(self._selected_ids)
                if self._selected_id is not None:
                    lock_targets.add(self._selected_id)
                pl_clicked = self._session.layout.placements.get(comp_id)
                any_locked = any(
                    self._session.layout.placements[t].locked
                    for t in lock_targets if t in self._session.layout.placements
                )
                all_locked = all(
                    self._session.layout.placements[t].locked
                    for t in lock_targets if t in self._session.layout.placements
                )
                if len(lock_targets) == 1:
                    lock_label = "Unlock" if pl_clicked and pl_clicked.locked else "Lock"
                    act_lock = menu.addAction(f"\U0001f512 {lock_label}")
                    act_lock.setData(("lock", lock_targets, not (pl_clicked and pl_clicked.locked)))
                else:
                    act_lock_all = menu.addAction(f"\U0001f512 Lock all ({len(lock_targets)})")
                    act_lock_all.setData(("lock", lock_targets, True))
                    act_lock_all.setEnabled(not all_locked)
                    act_unlock_all = menu.addAction(f"\U0001f513 Unlock all ({len(lock_targets)})")
                    act_unlock_all.setData(("lock", lock_targets, False))
                    act_unlock_all.setEnabled(any_locked)

                # Export pin (inside non-root group)
                active_gid = self._current_group_id
                if active_gid != ROOT_GROUP_ID:
                    active_grp = self._session.layout.groups.get(active_gid)
                    if active_grp is not None:
                        from protoboard.domain.commands.group_cmds import SetExportedPinsCommand
                        from protoboard.domain.layout.group import ExportedPin
                        is_direct_member = comp_id in active_grp.component_ids
                        # Direct child groups that have something to re-export.
                        reexportable_children = [
                            (
                                cid,
                                self._session.layout.groups[cid],
                            )
                            for cid in active_grp.child_group_ids
                            if cid in self._session.layout.groups
                            and self._session.layout.groups[cid].exported_pins
                        ]
                        if is_direct_member or reexportable_children:
                            sm_export = menu.addMenu(
                                f"{inst.refdes} — export/unexport pins…"
                            )
                            # Bulk actions act on the *entire active group*.
                            act_export_all = sm_export.addAction("Export all pins")
                            act_export_all.setData(("export_all_pins", active_gid))
                            act_export_auto = sm_export.addAction(
                                "Export auto (pins on external nets)"
                            )
                            act_export_auto.setData(("export_auto_pins", active_gid))
                            act_export_clear = sm_export.addAction(
                                "Unexport all pins"
                            )
                            act_export_clear.setData(("clear_all_pins", active_gid))
                            sm_export.addSeparator()
                            # Per-pin entries only for direct members of the
                            # active group — the encapsulated re-export model
                            # forbids flat-exporting a pin that lives in a
                            # nested sub-group.
                            if is_direct_member:
                                for pin_num in range(1, defn.footprint.pin_count() + 1):
                                    ep = ExportedPin(
                                        component_id=comp_id, pin_number=pin_num
                                    )
                                    pin_name = defn.pin_names.get(pin_num, str(pin_num))
                                    already = ep in active_grp.exported_pins
                                    marker = " ◀" if pin_num == clicked_pin else ""
                                    label = (
                                        f"✓ Unexport pin {pin_num} ({pin_name}){marker}"
                                        if already
                                        else f"  Export pin {pin_num} ({pin_name}){marker}"
                                    )
                                    act = sm_export.addAction(label)
                                    act.setData(
                                        ("export_pin", active_gid, ep, already)
                                    )
                                    act.hovered.connect(
                                        lambda _cid=comp_id, _pn=pin_num: _hover_pin(_cid, _pn)
                                    )
                            # Re-export submenu per direct child group: each
                            # entry toggles whether one of child.exported_pins
                            # also sits in active_grp.exported_pins — i.e.
                            # whether the active group surfaces that pin to
                            # its own parent.  Plan §3.6.
                            for child_id, child_grp in reexportable_children:
                                sm_child = sm_export.addMenu(
                                    f"Re-export from sub-group "
                                    f"{child_grp.name or str(child_id)[:8]}"
                                )
                                for ep in sorted(
                                    child_grp.exported_pins,
                                    key=lambda e: (str(e.component_id), e.pin_number),
                                ):
                                    ep_inst = self._session.instances.get(ep.component_id)
                                    refdes = ep_inst.refdes if ep_inst else "?"
                                    pin_label = f"{refdes} pin {ep.pin_number}"
                                    already_re = ep in active_grp.exported_pins
                                    re_label = (
                                        f"✓ Un-re-export {pin_label}"
                                        if already_re
                                        else f"  Re-export {pin_label}"
                                    )
                                    act_re = sm_child.addAction(re_label)
                                    act_re.setData(
                                        ("export_pin", active_gid, ep, already_re)
                                    )
                                    act_re.hovered.connect(
                                        lambda _cid=ep.component_id, _pn=ep.pin_number:
                                        _hover_pin(_cid, _pn)
                                    )

                    # Autoroute the active group (acts on the group as a
                    # whole — independent of which component was right-
                    # clicked).  Delegates to MainWindow's slot so the
                    # worker plumbing stays single-sourced.
                    act_ar_grp = menu.addAction(
                        "Autoroute group (internal connections)"
                    )
                    act_ar_grp.setData(("autoroute_group", active_gid))

                # Disconnect pin from net
                from protoboard.domain.schematic.net import PinRef
                netted_pins: list[tuple[int, str, str]] = []
                for pin_num in range(1, defn.footprint.pin_count() + 1):
                    ref = PinRef(component_id=comp_id, pin_number=pin_num)
                    net = self._session.schematic.net_for_pin(ref)
                    if net is not None:
                        pin_name = defn.pin_names.get(pin_num, str(pin_num))
                        netted_pins.append((pin_num, pin_name, net.name))
                if netted_pins:
                    sm_disconnect = menu.addMenu(
                        f"{inst.refdes} — disconnect pin from net…"
                    )
                    for pin_num, pin_name, net_name in netted_pins:
                        marker = " ◀" if pin_num == clicked_pin else ""
                        act = sm_disconnect.addAction(
                            f"Pin {pin_num} ({pin_name})  ←  {net_name}{marker}"
                        )
                        act.setData(("disconnect", comp_id, pin_num))
                        act.hovered.connect(
                            lambda _cid=comp_id, _pn=pin_num: _hover_pin(_cid, _pn)
                        )

        # ── Select section ────────────────────────────────────────────────────
        menu.addSection("Select")
        if comp_id is not None:
            _non_global = self._nets_for_comp(comp_id, exclude_global=True)
            _all_nets   = self._nets_for_comp(comp_id, exclude_global=False)

            # Components in same nets (excl. global)
            act_sel_nets = menu.addAction("Components in same nets (excl. global)")
            act_sel_nets.setData(("select_same_nets", comp_id))
            act_sel_nets.setEnabled(bool(_non_global))

            # Components in same net — one submenu entry per net
            if len(_all_nets) == 1:
                _n0 = _all_nets[0]
                a = menu.addAction(f"Components in net '{_n0.name}'")  # type: ignore[attr-defined]
                a.setData(("select_net_comps_from_wire_logical", None, _n0.id))  # type: ignore[attr-defined]
            elif len(_all_nets) > 1:
                _sm_comps = menu.addMenu("Components in net…")
                for _n in _all_nets:
                    _a = _sm_comps.addAction(_n.name)  # type: ignore[attr-defined]
                    _a.setData(("select_same_net_one", comp_id, _n.id))  # type: ignore[attr-defined]

            # Wires and pins of all nets
            act_wp_all = menu.addAction("Wires + components of all nets")
            act_wp_all.setData(("select_all_net_wires_and_pins", comp_id, None))
            act_wp_all.setEnabled(bool(_all_nets))

            # Wires of net — one or submenu
            if len(_non_global) == 1:
                _net1 = _non_global[0]
                act_w1 = menu.addAction(f"Wires of net '{_net1.name}'")  # type: ignore[attr-defined]
                act_w1.setData(("select_net_wires", _net1.id))  # type: ignore[attr-defined]
            elif len(_non_global) > 1:
                wires_submenu = menu.addMenu("Wires of net…")
                for _n in _non_global:
                    _a = wires_submenu.addAction(_n.name)  # type: ignore[attr-defined]
                    _a.setData(("select_net_wires", _n.id))  # type: ignore[attr-defined]

        if wire_id is not None:
            # Wires
            act_phys = menu.addAction("Physically connected wires")
            act_phys.setData(("select_wires_physical", wire_id))

            act_frag = menu.addAction("To closest pin/junction")
            act_frag.setData(("select_to_pin_joint", wire_id))

            # Components
            act_conn_comps = menu.addAction("Connected components (physical)")
            act_conn_comps.setData(("select_connected_comps", wire_id))

            act_net_comps = menu.addAction("Components in same net (logical)")
            act_net_comps.setData(("select_net_comps_logical", wire_id))

        # ── Highlight section ─────────────────────────────────────────────────
        menu.addSection("Highlight")
        act_hl = menu.addAction("Highlight selection (Dimmed #2)")
        act_hl.setData(("highlight_selection",))
        act_clear_hl = menu.addAction("Clear highlight")
        act_clear_hl.setData(("clear_highlight",))
        act_clear_hl.setEnabled(self._session.highlight_stack.has_persistent())

        # ── Wire section ──────────────────────────────────────────────────────
        if wire_id is not None:
            wire = next((w for w in self._session.layout.wires if w.id == wire_id), None)
            if wire is not None:
                has_items = True
                from protoboard.domain.commands.component_cmds import (
                    SetWireInsulatedCommand, SetWireSideCommand,
                )
                effective_ids: set[uuid.UUID] = set(self._selected_wire_ids)
                if self._selected_wire_id is not None:
                    effective_ids.add(self._selected_wire_id)
                target_wire_ids = effective_ids if wire.id in effective_ids and len(effective_ids) > 1 else {wire.id}
                ins_label = "insulated" if wire.insulated else "bare"
                n_wires = len(target_wire_ids)
                section = f"Wire{'s (' + str(n_wires) + ')' if n_wires > 1 else ''} — {ins_label}, {wire.side}"
                menu.addSection(section)
                if wire.insulated:
                    act = menu.addAction("Set bare (no insulation)")
                    act.setData(("wire_insulated", target_wire_ids, False))
                else:
                    act = menu.addAction("Set insulated (add insulation jacket)")
                    act.setData(("wire_insulated", target_wire_ids, True))
                other_side = "bottom" if wire.side == "top" else "top"
                act_side = menu.addAction(f"Move to {other_side} layer")
                act_side.setData(("wire_side", target_wire_ids, other_side))
                if (wire.id in target_wire_ids and len(target_wire_ids) == 1
                        and self._is_editing_wire()
                        and self._selected_wire_id == wire.id):
                    act_flip = menu.addAction(f"Flip fragment to {other_side} layer  [F]")
                    act_flip.setData(("flip_fragment", other_side))

                # Simplify wire path — use project-default mode.
                simplify_label = (
                    f"Simplify wire{' paths' if n_wires > 1 else ' path'}"
                )
                act_simplify = menu.addAction(simplify_label)
                act_simplify.setData(("simplify_wire", target_wire_ids, None))
                # Submenu for explicit mode override.
                sm_simplify = menu.addMenu("Simplify with mode")
                sm_simplify_act_cheb = sm_simplify.addAction(
                    "Chebyshev (shortcut via diag)"
                )
                sm_simplify_act_cheb.setData(
                    ("simplify_wire", target_wire_ids, "chebyshev")
                )
                sm_simplify_act_parity = sm_simplify.addAction(
                    "Bare-parity (respect anti-short rule)"
                )
                sm_simplify_act_parity.setData(
                    ("simplify_wire", target_wire_ids, "bare_parity")
                )

                # Optimize-with submenu — one item per registered
                # WireOptimizer plugin (excluding the built-in 'simplify',
                # which already has its own entries above).
                import protoboard.services.routing.builtin_optimizers  # noqa: F401
                from protoboard.services.routing.optimizer import (
                    registry as _opt_registry,
                )
                opt_names = [
                    n for n in _opt_registry.list_available() if n != "simplify"
                ]
                if opt_names:
                    sm_opt = menu.addMenu("Optimize with…")
                    for opt_name in opt_names:
                        opt = _opt_registry.get(opt_name)
                        act = sm_opt.addAction(
                            getattr(opt, "display_name", opt_name)
                        )
                        caps = opt.capabilities()
                        if caps.notes:
                            act.setToolTip(caps.notes)
                        act.setData(("optimize_with", target_wire_ids, opt_name))

                # Unweave — only meaningful with ≥2 wires selected.
                if n_wires >= 2:
                    act_unweave = menu.addAction(
                        f"Unweave {n_wires} wires (topological)"
                    )
                    act_unweave.setData(("unweave_wires", target_wire_ids))

                # Merge same-side fragments — only show when at least one
                # selected wire actually has fragments to fuse (avoids a
                # dead menu entry on simple single-fragment wires).
                _mergeable_wires = [
                    w for w in self._session.layout.wires
                    if w.id in target_wire_ids and len(w.fragments) >= 2
                ]
                if _mergeable_wires:
                    merge_label = (
                        f"Merge same-side fragments"
                        f"{' (' + str(len(_mergeable_wires)) + ' wires)' if len(_mergeable_wires) > 1 else ''}"
                    )
                    act_merge = menu.addAction(merge_label)
                    act_merge.setData(("merge_wire_fragments", target_wire_ids))

        if not has_items:
            return False

        chosen = menu.exec(self._view.mapToGlobal(self._view.mapFromScene(scene_pt)))
        if chosen is None:
            return True

        data = chosen.data()
        if not isinstance(data, tuple):
            return True
        action = data[0]

        if action == "view_center":
            _, pt = data
            self._view.center_on_scene_point(pt)

        elif action == "view_escape":
            self._view.escape_tool.emit()

        elif action == "lock":
            _, target_ids, new_locked = data
            from protoboard.domain.commands.base import MacroCommand
            cmds_lock = [
                LockComponentCommand(component_id=t, locked=new_locked)
                for t in target_ids if t in self._session.layout.placements
            ]
            if cmds_lock:
                cmd_lock = (cmds_lock[0] if len(cmds_lock) == 1
                            else MacroCommand(cmds_lock, description=f"{'Lock' if new_locked else 'Unlock'} {len(cmds_lock)} component(s)"))
                self._session.stack.execute(cmd_lock, self._session)
                self._session.layout_changed.emit()

        elif action == "export_pin":
            _, group_id, ep, already = data
            from protoboard.domain.commands.group_cmds import SetExportedPinsCommand
            grp = self._session.layout.groups.get(group_id)
            if grp is not None:
                new_exported = grp.exported_pins - {ep} if already else grp.exported_pins | {ep}
                self._session.stack.execute(
                    SetExportedPinsCommand(group_id=group_id, new_exported=new_exported),
                    self._session,
                )
                self._session.layout_changed.emit()

        elif action == "export_all_pins":
            _, group_id = data
            from protoboard.domain.commands.group_cmds import SetExportedPinsCommand
            from protoboard.domain.layout.group import exposable_surface
            grp = self._session.layout.groups.get(group_id)
            if grp is not None:
                # Encapsulation: "all" = pins of direct members + each direct
                # child's re-exportable interface.  Grandchild pins surface
                # only after the parent of the relevant intermediate group
                # explicitly re-exports them at that level.
                new_exported = exposable_surface(
                    self._session.layout.groups, group_id,
                    self._session.instances,
                    self._session.components,
                )
                if new_exported != grp.exported_pins:
                    self._session.stack.execute(
                        SetExportedPinsCommand(
                            group_id=group_id, new_exported=new_exported
                        ),
                        self._session,
                    )
                    self._session.layout_changed.emit()

        elif action == "export_auto_pins":
            _, group_id = data
            from protoboard.domain.commands.group_cmds import SetExportedPinsCommand
            from protoboard.domain.layout.group import external_pins_for_group
            grp = self._session.layout.groups.get(group_id)
            if grp is not None:
                new_exported = external_pins_for_group(
                    self._session.layout.groups, group_id,
                    self._session.schematic,
                )
                if new_exported != grp.exported_pins:
                    self._session.stack.execute(
                        SetExportedPinsCommand(
                            group_id=group_id, new_exported=new_exported
                        ),
                        self._session,
                    )
                    self._session.layout_changed.emit()

        elif action == "clear_all_pins":
            _, group_id = data
            from protoboard.domain.commands.group_cmds import SetExportedPinsCommand
            grp = self._session.layout.groups.get(group_id)
            if grp is not None and grp.exported_pins:
                self._session.stack.execute(
                    SetExportedPinsCommand(
                        group_id=group_id, new_exported=frozenset()
                    ),
                    self._session,
                )
                self._session.layout_changed.emit()

        elif action == "autoroute_group":
            # Delegate to MainWindow's slot — keeps the worker plumbing
            # single-sourced and the dialog/progress UX consistent with
            # the top-menu entry.
            mw = self._view.window() if self._view is not None else None
            slot = getattr(mw, "_group_autoroute", None)
            if callable(slot):
                slot()

        elif action == "disconnect":
            _, cid, pin_num = data
            from typing import cast
            from protoboard.domain.commands.base import Command
            from protoboard.domain.commands.component_cmds import DisconnectPinCommand
            self._session.stack.execute(
                cast(Command[ComponentState], DisconnectPinCommand(component_id=cid, pin_number=pin_num)),
                self._session,
            )
            self._session.layout_changed.emit()

        elif action == "wire_insulated":
            _, target_wire_ids, insulated = data
            from typing import cast
            from protoboard.domain.commands.base import Command, MacroCommand
            from protoboard.domain.commands.component_cmds import SetWireInsulatedCommand
            raw = [cast(Command[ComponentState], SetWireInsulatedCommand(wire_id=wid, insulated=insulated))
                   for wid in target_wire_ids]
            cmd_w = raw[0] if len(raw) == 1 else MacroCommand(raw, description=f"Set {len(raw)} wires")
            self._session.stack.execute(cmd_w, self._session)
            self._session.layout_changed.emit()

        elif action == "wire_side":
            _, target_wire_ids, side = data
            from typing import cast
            from protoboard.domain.commands.base import Command, MacroCommand
            from protoboard.domain.commands.component_cmds import SetWireSideCommand
            raw = [cast(Command[ComponentState], SetWireSideCommand(wire_id=wid, side=side))
                   for wid in target_wire_ids]
            cmd_w = raw[0] if len(raw) == 1 else MacroCommand(raw, description=f"Move {len(raw)} wires")
            self._session.stack.execute(cmd_w, self._session)
            self._session.layout_changed.emit()

        elif action == "flip_fragment":
            self._flip_wire_fragment()

        elif action == "simplify_wire":
            _, target_wire_ids, mode_str = data
            self._simplify_wires(target_wire_ids, mode_override=mode_str)

        elif action == "optimize_with":
            _, target_wire_ids, opt_name = data
            self._optimize_selected_wires(target_wire_ids, opt_name)

        elif action == "unweave_wires":
            _, target_wire_ids = data
            self._unweave_wires(target_wire_ids)

        elif action == "merge_wire_fragments":
            _, target_wire_ids = data
            self._merge_wires_and_fragments(frozenset(target_wire_ids))

        elif action == "select_same_nets":
            _, cid = data
            self._select_same_net_comps(cid, exclude_global=True)

        elif action == "select_same_net_one":
            _, cid, net_id = data
            self._select_all_net_wires_and_pins(cid, net_id=net_id)

        elif action == "select_all_net_wires_and_pins":
            _, cid, net_id = data
            self._select_all_net_wires_and_pins(cid, net_id=net_id)

        elif action == "select_net_wires":
            _, net_id = data
            self._select_net_wires(net_id)

        elif action == "select_wires_physical":
            _, wid = data
            self._select_wires_physical(wid)

        elif action == "select_to_pin_joint":
            _, wid = data
            self._select_to_closest_pin_or_joint(wid, scene_pt)

        elif action == "select_connected_comps":
            _, wid = data
            self._select_connected_comps(wid)

        elif action == "select_net_comps_logical":
            _, wid = data
            self._select_net_comps_from_wire(wid, logical=True)

        elif action == "select_net_comps_from_wire_logical":
            _, _wire, net_id = data
            net = self._session.schematic.nets.get(net_id)
            if net is not None:
                target = {ref.component_id for ref in net.pins}
                self._deselect()
                self._clear_multi_selection()
                self._selected_ids = target
                self._redraw_multi_highlights()
                self._notify_selection()

        elif action == "highlight_selection":
            self._highlight_selection()

        elif action == "clear_highlight":
            self._session.highlight_stack.clear()

        return True

    def _show_wire_menu(
        self, wire: "Wire", scene_pt: QPointF
    ) -> bool:
        from PySide6.QtWidgets import QMenu
        from protoboard.domain.commands.component_cmds import (
            ConvertEndpointToWirePinCommand,
            ConvertWirePinToEndpointCommand,
            SetWireInsulatedCommand,
            SetWireSideCommand,
        )
        from protoboard.domain.layout.knot import JointKnot, WirePinKnot
        from typing import cast
        from protoboard.domain.commands.base import Command

        # Build the effective selection: rubber-band/_selected_wire_ids (multi)
        # plus _selected_wire_id (primary single click).  If the clicked wire
        # belongs to that combined set, act on the whole set; otherwise act
        # only on the clicked wire.
        effective_ids: set[uuid.UUID] = set(self._selected_wire_ids)
        if self._selected_wire_id is not None:
            effective_ids.add(self._selected_wire_id)

        ins_label = "insulated" if wire.insulated else "bare"
        if wire.id in effective_ids and len(effective_ids) > 1:
            target_ids = effective_ids
            section = f"Wires ({len(target_ids)}) — {ins_label}, {wire.side}"
        else:
            target_ids = {wire.id}
            section = f"Wire — {ins_label}, {wire.side}"

        menu = QMenu(self._view)
        menu.addSection(section)

        if wire.insulated:
            act = menu.addAction("Set bare (no insulation)")
            act.setData(("insulated", False))
        else:
            act = menu.addAction("Set insulated (add insulation jacket)")
            act.setData(("insulated", True))

        other_side = "bottom" if wire.side == "top" else "top"
        act_side = menu.addAction(f"Move to {other_side} layer")
        act_side.setData(("side", other_side))

        # Fragment flip — only available when a fragment is selected (start+end)
        if (wire.id in target_ids and len(target_ids) == 1
                and self._is_editing_wire()
                and self._selected_wire_id == wire.id):
            act_flip = menu.addAction(f"Flip fragment to {other_side} layer  [F]")
            act_flip.setData(("flip_fragment", other_side))

        # Wire-pin conversion — only for the clicked endpoint when it is
        # eligible (single wire under cursor, endpoint within click
        # tolerance).  See docs/wire-pin-implementation.md §3.7.
        if wire.id in target_ids and len(target_ids) == 1:
            endpoint_side = self._closest_wire_endpoint(wire, scene_pt)
            if endpoint_side is not None:
                ref = wire.a if endpoint_side == "a" else wire.b
                if ref is not None:
                    knot = self._session.layout.knots.get(ref.id)
                    if isinstance(knot, JointKnot) and knot.wire_ids == frozenset({wire.id}):
                        # Half-orphan endpoint on a plated-through pad —
                        # offer promotion.
                        if self._endpoint_eligible_for_wire_pin(knot.position):
                            menu.addSeparator()
                            act_wp = menu.addAction(
                                "Convert endpoint to wire-pin"
                            )
                            act_wp.setData(("convert_to_wire_pin", endpoint_side))
                    elif isinstance(knot, WirePinKnot):
                        # Already a wire-pin → offer revert.
                        menu.addSeparator()
                        act_unwp = menu.addAction(
                            "Convert wire-pin back to free endpoint"
                        )
                        act_unwp.setData(("convert_from_wire_pin", knot.id))

        chosen = menu.exec(
            self._view.mapToGlobal(self._view.mapFromScene(scene_pt))
        )
        if chosen is None:
            return True

        action, value = chosen.data()
        if action == "flip_fragment":
            self._flip_wire_fragment()
            return True
        if action == "convert_to_wire_pin":
            wire_net_id = self._session.cached_wire_net_map().get(wire.id)
            cmd_wp: Command[ComponentState] = cast(
                Command[ComponentState],
                ConvertEndpointToWirePinCommand(
                    wire_id=wire.id, endpoint=value, net_id=wire_net_id,
                ),
            )
            self._session.stack.execute(cmd_wp, self._session)
            self._session.layout_changed.emit()
            return True
        if action == "convert_from_wire_pin":
            cmd_unwp: Command[ComponentState] = cast(
                Command[ComponentState],
                ConvertWirePinToEndpointCommand(wire_pin_id=value),
            )
            self._session.stack.execute(cmd_unwp, self._session)
            self._session.layout_changed.emit()
            return True
        raw_cmds: list[Command[ComponentState]]
        if action == "insulated":
            raw_cmds = [
                cast(Command[ComponentState],
                     SetWireInsulatedCommand(wire_id=wid, insulated=value))
                for wid in target_ids
            ]
        else:
            raw_cmds = [
                cast(Command[ComponentState],
                     SetWireSideCommand(wire_id=wid, side=value))
                for wid in target_ids
            ]

        if len(raw_cmds) == 1:
            cmd: Command[ComponentState] = raw_cmds[0]
        else:
            cmd = MacroCommand(raw_cmds, description=f"Set {len(raw_cmds)} wires")
        self._session.stack.execute(cmd, self._session)
        self._session.layout_changed.emit()
        return True

    def _closest_wire_endpoint(
        self, wire: "Wire", scene_pt: QPointF,
    ) -> "str | None":
        """Return "a" or "b" if either endpoint is within click tolerance
        of *scene_pt*, else None.  Helper for the wire-pin PPM entry —
        the action only makes sense when the user clicked near an
        endpoint, not somewhere mid-path.
        """
        if not wire.path or wire.a is None or wire.b is None:
            return None
        sx, sy = scene_pt.x(), scene_pt.y()
        tol = 0.6  # one-half pad pitch — generous, PPM is forgiving
        a_pos = wire.path[0]
        b_pos = wire.path[-1]
        d_a = (a_pos.x - sx) ** 2 + (a_pos.y - sy) ** 2
        d_b = (b_pos.x - sx) ** 2 + (b_pos.y - sy) ** 2
        if d_a <= tol * tol and d_a <= d_b:
            return "a"
        if d_b <= tol * tol:
            return "b"
        return None

    def _endpoint_eligible_for_wire_pin(self, position: "Point") -> bool:
        """True iff *position* coincides with a plated-through pad.

        Wire-pin invariant requires the pad to be plated-through; the
        PPM action must be greyed out (here: omitted) otherwise.  Uses
        the session's cached plated-through set for the hot path.
        """
        pads = self._session.cached_plated_through_pads()
        for p in pads:
            if abs(p.x - position.x) < 1e-6 and abs(p.y - position.y) < 1e-6:
                return True
        return False

    # ------------------------------------------------------------------
    # Single component selection
    # ------------------------------------------------------------------

    def _camera_side(self) -> str:
        """Return the current camera side from the parent BoardView."""
        return getattr(self._view, "_camera_side", "top")

    def _layer_state(self) -> object:
        """Return a minimal LayerState for hit-test alpha checks."""
        from protoboard.gui.layer_state import LayerState, LayerOverrides
        overrides = getattr(self._view, "_layer_overrides", LayerOverrides())
        camera = self._camera_side()
        return LayerState(
            camera_side=camera,  # type: ignore[arg-type]
            overrides=overrides,
        )

    def _hit_test(self, scene_pt: QPointF) -> uuid.UUID | None:
        """Return the nearest component within _HIT_RADIUS.

        Front-side components are preferred.  Hidden components (alpha==0) are skipped.
        Uses a spatial index to avoid scanning all components on every click.
        """
        session = self._session
        camera = self._camera_side()
        layers = self._layer_state()
        front_best: uuid.UUID | None = None
        front_best_d = float("inf")
        back_best: uuid.UUID | None = None
        back_best_d = float("inf")

        px, py = scene_pt.x(), scene_pt.y()
        cell_x, cell_y = round(px), round(py)
        candidates: set[uuid.UUID] = set()
        spatial = session.comp_spatial_index()
        for dcx in (-1, 0, 1):
            for dcy in (-1, 0, 1):
                for cid in spatial.get((cell_x + dcx, cell_y + dcy), []):
                    candidates.add(cid)

        for comp_id in candidates:
            placement = session.layout.placements.get(comp_id)
            if placement is None:
                continue
            side = placement.side  # type: ignore[assignment]
            if layers.alpha_for_component(side) == 0.0:  # type: ignore[union-attr]
                continue
            for _pin_num, pos in session.iter_pin_positions(comp_id):
                dx = px - pos.x
                dy = py - pos.y
                d = math.sqrt(dx * dx + dy * dy)
                if placement.side == camera:
                    if d < front_best_d:
                        front_best_d = d
                        front_best = comp_id
                else:
                    if d < back_best_d:
                        back_best_d = d
                        back_best = comp_id

        if front_best is not None and front_best_d <= _HIT_RADIUS:
            return front_best
        if back_best is not None and back_best_d <= _HIT_RADIUS:
            return back_best
        return None

    def _hit_test_bbox(self, scene_pt: QPointF) -> uuid.UUID | None:
        """Return the first component whose bbox contains scene_pt.

        Front-side first; hidden components skipped.
        Uses a spatial index to narrow candidates before computing bboxes.
        """
        session = self._session
        camera = self._camera_side()
        layers = self._layer_state()
        px, py = scene_pt.x(), scene_pt.y()

        # Collect candidates within ±1 grid cell (covers _BBOX_MARGIN = 0.45).
        cell_x, cell_y = round(px), round(py)
        candidates: set[uuid.UUID] = set()
        spatial = session.comp_spatial_index()
        for dcx in (-1, 0, 1):
            for dcy in (-1, 0, 1):
                for cid in spatial.get((cell_x + dcx, cell_y + dcy), []):
                    candidates.add(cid)

        def _check(comp_id: uuid.UUID, placement: object) -> bool:
            from protoboard.domain.layout.placement import ComponentPlacement as CP
            assert isinstance(placement, CP)
            if layers.alpha_for_component(placement.side) == 0.0:  # type: ignore[union-attr]
                return False
            positions = [pos for _, pos in session.iter_pin_positions(comp_id)]
            if not positions:
                return False
            xs = [p.x for p in positions]
            ys = [p.y for p in positions]
            m = _BBOX_MARGIN
            return (min(xs) - m <= px <= max(xs) + m
                    and min(ys) - m <= py <= max(ys) + m)

        for comp_id in candidates:
            placement = session.layout.placements.get(comp_id)
            if placement is not None and placement.side == camera and _check(comp_id, placement):
                return comp_id
        for comp_id in candidates:
            placement = session.layout.placements.get(comp_id)
            if placement is not None and placement.side != camera and _check(comp_id, placement):
                return comp_id
        return None

    def _select_primary(self, comp_id: uuid.UUID) -> None:
        """Set comp_id as the primary (Properties panel) selection."""
        self._deselect()
        self._selected_id = comp_id
        self._draw_highlight(comp_id)
        inst = self._session.instances.get(comp_id)
        _log.debug("selection.component", comp_id=str(comp_id), refdes=inst.refdes if inst else "?")
        placement = self._session.layout.placements.get(comp_id)
        if inst is None:
            return
        defn = self._session.components.get(inst.definition_ref)
        panel = self._props_panel()
        if panel is not None:
            panel.show_component(inst, placement, defn)
        self._notify_selection()

    def _deselect(self) -> None:
        if self._highlight is not None:
            try:
                self._view.scene().removeItem(self._highlight)
            except RuntimeError:
                pass  # C++ object already deleted by scene.clear()
            self._highlight = None
        for line in self._origin_cross:
            try:
                self._view.scene().removeItem(line)
            except RuntimeError:
                pass
        self._origin_cross.clear()
        self._selected_id = None
        panel = self._props_panel()
        if panel is not None:
            panel.clear_component()
        # Note: no _notify_selection() here — callers that need it call it themselves
        # (e.g. _select_primary calls _deselect then _notify_selection at the end).

    def _draw_highlight(self, comp_id: uuid.UUID) -> None:
        if self._highlight is not None:
            self._view.scene().removeItem(self._highlight)
            self._highlight = None
        session = self._session
        placement = session.layout.placements.get(comp_id)
        if placement is None:
            return
        positions = [pos for _, pos in session.iter_pin_positions(comp_id)]
        if not positions:
            return
        xs = [p.x for p in positions]
        ys = [p.y for p in positions]
        m = _BBOX_MARGIN
        self._highlight = QGraphicsRectItem(
            QRectF(min(xs) - m, min(ys) - m,
                   max(xs) - min(xs) + 2 * m, max(ys) - min(ys) + 2 * m)
        )
        theme = getattr(self._view, "_theme_override", None)
        if theme is not None:
            pen = _make_select_pen(theme.select_color, theme.select_width)
        else:
            pen = _SELECT_PEN
        self._highlight.setPen(pen)
        self._highlight.setBrush(Qt.BrushStyle.NoBrush)
        self._view.scene().addItem(self._highlight)

        # Draw a cross at the component origin (placement.position = local (0,0)).
        for line in self._origin_cross:
            self._view.scene().removeItem(line)
        self._origin_cross.clear()
        ox, oy = placement.position.x, placement.position.y
        arm = 0.35
        t = self._theme()
        cross_pen = QPen(QColor(t.component_origin_cross_color), t.component_origin_cross_width)
        h_line = QGraphicsLineItem(ox - arm, oy, ox + arm, oy)
        h_line.setPen(cross_pen)
        v_line = QGraphicsLineItem(ox, oy - arm, ox, oy + arm)
        v_line.setPen(cross_pen)
        self._view.scene().addItem(h_line)
        self._view.scene().addItem(v_line)
        self._origin_cross = [h_line, v_line]

    def _flip_selected(self) -> None:
        """Alt+R: geometrically swap pin1 and pin2 (anchor moves to old pin2 pad)."""
        comp_id = self._selected_id
        if comp_id is None:
            return
        if self._is_locked_in_group(comp_id):
            return
        placement = self._session.layout.placements.get(comp_id)
        inst = self._session.instances.get(comp_id)
        if placement is None or inst is None:
            return
        defn = self._session.components.get(inst.definition_ref)
        if defn is None or defn.footprint.pin_count() != 2:
            return
        new_pl = flip_component(defn.footprint, placement)
        cmd = MoveComponentCommand(
            component_id=comp_id,
            new_placement=new_pl,
            old_placement=placement.copy(),
        )
        self._session.stack.execute(cmd, self._session)
        self._session.layout_changed.emit()
        self._draw_highlight(comp_id)

    def _rotate_multi(self, delta: int = 90) -> None:
        if self._selected_group_id is not None:
            from protoboard.domain.commands.base import Command
            from protoboard.domain.commands.group_cmds import RotateGroupCommand
            from typing import cast
            rotate_cmd: Command[ComponentState] = cast(
                Command[ComponentState],
                RotateGroupCommand(group_id=self._selected_group_id, delta=delta % 360),
            )
            # Collect group member IDs for crossing-wire check after rotation.
            from protoboard.domain.layout.group import all_component_ids
            grp_comp_ids = all_component_ids(
                self._session.layout.groups, self._selected_group_id
            )
            self._session.stack.execute(rotate_cmd, self._session)
            remove_cmds = self._remove_crossing_wires(grp_comp_ids)
            if remove_cmds:
                self._session.stack.execute(
                    MacroCommand(remove_cmds, description="Remove crossing wires"),
                    self._session,
                )
            self._session.layout_changed.emit()
            self._redraw_multi_highlights()
            return
        ids = [cid for cid in self._selected_ids if not self._is_locked_in_group(cid)]
        if not ids:
            return
        cmds: list[MoveComponentCommand] = []
        for cid in ids:
            placement = self._session.layout.placements.get(cid)
            if placement is None:
                continue
            new_pl = placement.copy()
            new_pl.rotation = (placement.rotation + delta) % 360
            cmds.append(MoveComponentCommand(
                component_id=cid,
                new_placement=new_pl,
                old_placement=placement.copy(),
            ))
        if not cmds:
            return
        from typing import cast
        from protoboard.domain.commands.base import Command
        macro = MacroCommand(
            cast(list[Command[ComponentState]], cmds),
            description=f"Rotate {len(cmds)} components",
        )
        self._session.stack.execute(macro, self._session)
        remove_cmds = self._remove_crossing_wires(set(ids))
        if remove_cmds:
            self._session.stack.execute(
                MacroCommand(remove_cmds, description="Remove crossing wires"),
                self._session,
            )
        self._session.layout_changed.emit()
        self._redraw_multi_highlights()

    def _flip_multi(self) -> None:
        ids = [cid for cid in self._selected_ids if not self._is_locked_in_group(cid)]
        if not ids:
            return
        cmds: list[MoveComponentCommand] = []
        for cid in ids:
            placement = self._session.layout.placements.get(cid)
            inst = self._session.instances.get(cid)
            if placement is None or inst is None:
                continue
            defn = self._session.components.get(inst.definition_ref)
            if defn is None or defn.footprint.pin_count() != 2:
                continue
            new_pl = flip_component(defn.footprint, placement)
            cmds.append(MoveComponentCommand(
                component_id=cid,
                new_placement=new_pl,
                old_placement=placement.copy(),
            ))
        if not cmds:
            return
        from typing import cast
        from protoboard.domain.commands.base import Command
        macro = MacroCommand(
            cast(list[Command[ComponentState]], cmds),
            description=f"Flip {len(cmds)} components",
        )
        self._session.stack.execute(macro, self._session)
        self._session.layout_changed.emit()
        self._redraw_multi_highlights()

    def _rotate_selected(self, delta: int = 90) -> None:
        comp_id = self._selected_id
        if comp_id is None:
            return
        if self._is_locked_in_group(comp_id):
            return
        placement = self._session.layout.placements.get(comp_id)
        if placement is None:
            return
        new_rot = (placement.rotation + delta) % 360

        if self._is_floating() and comp_id in self._floating_ids:
            # Already floating — rotate in-place, keep original for cancel.
            new_pl = placement.copy()
            new_pl.rotation = new_rot
            self._session.layout.placements[comp_id] = new_pl
            # Refresh floating highlights.
            self._clear_floating_highlights()
            self._draw_floating_highlights()
        else:
            # Enter floating after rotation.
            old_pl = placement.copy()
            new_pl = placement.copy()
            new_pl.rotation = new_rot
            self._session.layout.placements[comp_id] = new_pl
            crossing = list(self._remove_crossing_wires({comp_id}))
            from protoboard.gui.app_settings import load_app_settings
            auto_accept = load_app_settings().auto_accept_placement
            if auto_accept and not crossing:
                cmd = MoveComponentCommand(
                    component_id=comp_id,
                    new_placement=new_pl,
                    old_placement=old_pl,
                    skip_conflict_check=True,
                )
                self._session.stack.execute(cmd, self._session)
                if crossing:
                    self._session.stack.execute(
                        MacroCommand(crossing, description="Remove crossing wires"),
                        self._session,
                    )
            else:
                self._enter_floating({comp_id}, {comp_id: old_pl}, crossing)

        self._session.layout_changed.emit()
        self._draw_highlight(comp_id)
        panel = self._props_panel()
        if panel is not None:
            panel._rotation_combo.setCurrentIndex(new_rot // 90)

    def _common_group_for(self, comp_ids: set[uuid.UUID]) -> uuid.UUID | None:
        """Return the group ID if ALL comp_ids belong to the same non-root group, else None."""
        from protoboard.domain.layout.group import all_component_ids
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        groups = self._session.layout.groups
        # Find which group directly contains each component.
        comp_to_group: dict[uuid.UUID, uuid.UUID] = {}
        for gid, g in groups.items():
            if gid == ROOT_GROUP_ID:
                continue
            for cid in g.component_ids:
                if cid in comp_ids:
                    comp_to_group[cid] = gid
        _log.debug("common_group_for",
                   comp_ids=[str(i) for i in comp_ids],
                   comp_to_group={str(k): str(v) for k, v in comp_to_group.items()})
        if not comp_to_group:
            _log.debug("common_group_for.result", result=None, reason="no_group_members")
            return None
        group_ids = set(comp_to_group.values())
        if len(group_ids) != 1:
            _log.debug("common_group_for.result", result=None, reason="multiple_groups",
                       group_ids=[str(i) for i in group_ids])
            return None  # components from different groups
        gid = next(iter(group_ids))
        # Check that ALL drag_ids are members of this group (recursively).
        all_members = all_component_ids(groups, gid)
        if comp_ids <= set(all_members):
            _log.debug("common_group_for.result", result=str(gid))
            return gid
        _log.debug("common_group_for.result", result=None, reason="not_all_members",
                   missing=[str(i) for i in comp_ids - set(all_members)])
        return None

    def _update_drag_overlay(
        self, drag_ids: set[uuid.UUID], snapped: Point
    ) -> None:
        """Recompute and redraw drag conflict overlay (throttled to snapped grid).

        Cheap path: scan moved components for body-bbox overlap +
        pad-on-pad different-net conflicts only.  These are the HARD
        cases that need an instant red marker.  SOFT cases (skewed
        wires, pin-on-foreign-wire, wires crossing alien pads) are
        deferred to ``_run_full_drag_validation`` — that pass runs
        ~1 s after the user stops moving the cursor and produces the
        full DragCommitDecision used by ``on_release``.

        Splitting the work this way restores the per-snap latency
        we lost during the drag-pipeline refactor: a 13-component
        group drag now runs ~0.3 ms per snap (cheap) + 1 full
        classify per second (debounced) instead of a full classify
        on every snap (~3-5 ms × 30 fps).
        """
        if _dbg_enabled():
            _log.debug(
                "drag.overlay.enter",
                drag_ids=[str(c)[:8] for c in drag_ids],
                snapped=(snapped.x, snapped.y),
                needs_update=self._drag_overlay.needs_update(snapped),
            )
        if not self._drag_overlay.needs_update(snapped):
            return
        if _dbg_enabled():
            _log.debug(
                "drag.overlay.update",
                drag_ids=[str(c)[:8] for c in drag_ids],
                snapped=(snapped.x, snapped.y),
            )
        from protoboard.domain.layout.pad_conflict import (
            MoveConflictSeverity,
            MoveConflictResult,
        )
        active_gid = self._selected_group_id
        if active_gid is None and not drag_ids:
            self._drag_overlay.reset(self._view.scene())
            return

        # Cheap per-snap check — HARD first, then SOFT (pin-on-wire).
        hard_pads, soft_pads, soft_wire_ids = self._cheap_conflict_check(
            drag_ids,
        )
        if hard_pads:
            result = MoveConflictResult(
                severity=MoveConflictSeverity.BLOCKED,
                blocking_pad_positions=hard_pads,
                removable_wire_ids=[],
            )
            # Cache the HARD decision so on_release can build a
            # floating-HARD state without re-running the cheap pass.
            from protoboard.gui.tools.drag_commit_decision import (
                DragCommitDecision,
                DragSeverity,
            )
            self._drag_last_decision = DragCommitDecision(
                severity=DragSeverity.HARD,
                description="Drop would short different nets",
                blocking_pad_positions=tuple(hard_pads),
            )
            self._update_corner_text_from_decision(
                self._drag_last_decision,
            )
        elif soft_pads:
            result = MoveConflictResult(
                severity=MoveConflictSeverity.REMOVABLE,
                blocking_pad_positions=soft_pads,
                removable_wire_ids=list(soft_wire_ids),
            )
            from protoboard.gui.tools.drag_commit_decision import (
                DragCommitDecision,
                DragSeverity,
            )
            n_remove = len(soft_wire_ids)
            self._drag_last_decision = DragCommitDecision(
                severity=DragSeverity.SOFT,
                description=(
                    f"Drop removes {n_remove} wire(s) "
                    f"— [Enter] confirm, [Esc] cancel"
                ),
                auto_remove_wire_ids=tuple(soft_wire_ids),
                overlay_pad_positions=tuple(soft_pads),
            )
            self._update_corner_text_from_decision(
                self._drag_last_decision,
            )
        else:
            # No HARD, no SOFT — fully clear at this snap.  Drop any
            # stale cached decision so the overlay resets immediately
            # and a follow-up drag starts fresh.
            self._drag_last_decision = None
            self._update_corner_text_from_decision(None)
            result = MoveConflictResult(
                severity=MoveConflictSeverity.CLEAR,
                blocking_pad_positions=[],
                removable_wire_ids=[],
            )

        if _dbg_enabled():
            _log.debug(
                "drag.overlay.result",
                severity=result.severity.name,
                n_hard=len(hard_pads),
                n_soft=len(soft_pads),
                n_soft_wires=len(soft_wire_ids),
            )

        # Compute drag bbox for outline.
        bbox = self._drag_bbox(drag_ids)
        self._drag_overlay.update(self._view.scene(), result, bbox, snapped)

    def _pin_on_wire_remove_ids(
        self,
        drag_ids: "set[uuid.UUID]",
        pad_index: object,
    ) -> "set[uuid.UUID]":
        """Return wire IDs that should be auto-removed because a moved
        component's pin landed on top of them (rule 6 strict).

        Run as a release-time supplement to ``check_group_move_conflicts``
        which only covers pad-on-pad collisions and wires geometrically
        crossing alien pads — not the "pin lands on wire" case.  The
        wire ids surfaced here are merged into the group drag's
        removable_wire_ids so RemoveWireCommand strips them when the
        macro commits.

        Walks the PadIndex directly (not via ``find_wire_conflicts``)
        because the latter reports at most one wire per pin via
        ``_wire_hit_via_pad_index`` — a pad hosting wire items on
        both top *and* bottom (through-hole pin) would only surface
        one of them and the other would stay behind after commit
        (user-observed 2026-06-01).
        """
        from protoboard.domain.board.pad_state import (
            BareWireCrossing,
            BareWireItem,
            InsulatedWireCrossing,
            InsulatedWireItem,
        )
        from protoboard.domain.layout.knot import PinKnot

        _WIRE_ITEM_TYPES = (
            BareWireItem,
            BareWireCrossing,
            InsulatedWireItem,
            InsulatedWireCrossing,
        )
        # Exempt every wire that is structurally anchored to a moved
        # component — those wires follow the drag automatically (via
        # ``_drag_orig_wire_paths`` for owned wires, or via the
        # per-component MoveComponent / TranslateAnchoredWires pass
        # for shared-endpoint half-orphans).  Without this superset,
        # a wire excluded from owned-set by the group-ownership free-
        # knot guard would be misclassified as a pin-on-wire conflict
        # with its own pin (the moved component's PinKnot endpoint),
        # asking the user to delete a wire that belongs to the group.
        internal_wire_ids: set[uuid.UUID] = set(self._drag_orig_wire_paths.keys())
        knots = self._session.layout.knots
        for w in self._session.layout.wires:
            for ref in (w.a, w.b):
                if ref is None:
                    continue
                k = knots.get(ref.id)
                if not isinstance(k, PinKnot):
                    continue
                if k.pin.component_id in drag_ids:
                    internal_wire_ids.add(w.id)
                    break

        out: set[uuid.UUID] = set()
        # Diagnostic: log every wire that gets flagged so the next
        # "drag removes a wire that should be exempt" report drops out
        # of a single log line.
        hit_diag: list[dict[str, object]] = []
        for cid in drag_ids:
            for _pin_num, pin_pos in self._session.iter_pin_positions(cid):
                pad = pad_index.pad_at(pin_pos) if pad_index is not None else None  # type: ignore[attr-defined]
                if pad is None:
                    continue
                # Scan both physical layers so a through-hole pin
                # catches wires on either side of the board.
                for layer in ("top", "bottom"):
                    for it in pad.physically_blocked_on(layer):
                        if not isinstance(it, _WIRE_ITEM_TYPES):
                            continue
                        wid = getattr(it, "id", None)
                        if wid is None or wid in internal_wire_ids:
                            continue
                        out.add(wid)
                        hit_diag.append({
                            "comp": str(cid)[:8],
                            "pin": _pin_num,
                            "pin_pos": (pin_pos.x, pin_pos.y),
                            "wire_id": str(wid)[:8],
                            "kind": type(it).__name__,
                            "layer": layer,
                        })
        if hit_diag:
            _log.info(
                "pin_on_wire.flagged",
                n_flagged=len(out),
                n_internal_exempt=len(internal_wire_ids),
                hits=hit_diag[:15],
            )
        return out

    def _cheap_conflict_check(
        self, drag_ids: "set[uuid.UUID]",
    ) -> "tuple[list[Point], list[Point], set[uuid.UUID]]":
        """Lightweight per-snap conflict detection — HARD and SOFT.

        Returns ``(hard_pads, soft_pads, soft_wire_ids)``:

        * ``hard_pads`` — positions to paint as RED blocking markers.
          Detected: body-bbox overlap with a non-moved component (no
          pin coincidence) + pad-on-pad different-net contact.

        * ``soft_pads`` — positions to paint as YELLOW removable
          markers.  Detected: pin lands on an alien wire (rule 6
          strict — bare or insulated, any net, any side).  Cheap
          because PadIndex already records which items occupy each
          pad per side; we just iterate moved pins × O(1) pad lookups.

        * ``soft_wire_ids`` — wire IDs to feed into the SOFT
          ``DragCommitDecision.auto_remove_wire_ids`` on release so
          the commit pipeline auto-removes them.

        HARD takes priority over SOFT in the caller; if any hard pad
        is reported, the soft results may still be returned for
        future use but the overlay should render RED.

        Does NOT detect: wires *crossing* alien pads (interior
        segments that pass over a pad without a vertex), skewed
        wires after a drag-translation.  Those remain in the
        debounced ``_run_full_drag_validation`` pass because they
        require a full wire-vs-pad-grid sweep (~ms).

        Performance: ~1 µs per pin lookup through the trial PadIndex.
        For a 13-pin group: ~13 µs HARD + ~13 µs SOFT per snap.
        """
        from protoboard.domain.board.pad_state import (
            BareWireCrossing,
            BareWireItem,
            InsulatedWireCrossing,
            InsulatedWireItem,
        )
        from protoboard.domain.layout.pad_conflict import (
            ConflictKind,
            find_conflicts,
        )
        _drag_idx = self._ensure_drag_pad_index(drag_ids)
        hard_pads: list[Point] = []
        soft_pads: list[Point] = []
        soft_wire_ids: set[uuid.UUID] = set()
        # Internal wires (dragged with the group) must not count as
        # foreign-wire conflicts — they follow the move legitimately.
        internal_wire_ids = set(self._drag_orig_wire_paths.keys())
        _eps = 1e-4
        _WIRE_ITEM_TYPES = (
            BareWireItem,
            BareWireCrossing,
            InsulatedWireItem,
            InsulatedWireCrossing,
        )
        for cid in drag_ids:
            pl = self._session.layout.placements.get(cid)
            inst = self._session.instances.get(cid)
            if pl is None or inst is None:
                continue
            defn = self._session.components.get(inst.definition_ref)
            if defn is None:
                continue
            # --- HARD: body-bbox overlap with non-moved components ---
            incoming_bbox = self._session.component_bbox(cid)
            if incoming_bbox is None:
                continue
            incoming_pin_positions = [
                pos for _pn, pos in self._session.iter_pin_positions(cid)
            ]
            for other_id, _other_pl in self._session.layout.placements.items():
                if other_id == cid or other_id in drag_ids:
                    continue
                other_bbox = self._session.component_bbox(other_id)
                if other_bbox is None or not incoming_bbox.intersects(other_bbox):
                    continue
                other_pin_positions = [
                    pos for _pn, pos in
                    self._session.iter_pin_positions(other_id)
                ]
                has_pin_overlap = any(
                    abs(in_pos.x - ot_pos.x) < _eps
                    and abs(in_pos.y - ot_pos.y) < _eps
                    for in_pos in incoming_pin_positions
                    for ot_pos in other_pin_positions
                )
                if not has_pin_overlap:
                    pin1_pos = self._session.get_pin_position(cid, 1)
                    if pin1_pos is not None:
                        hard_pads.append(pin1_pos)
                    break  # one bbox conflict per component is enough
            # --- HARD: pad-on-pad DIFFERENT_NETS ---
            pad_cs = find_conflicts(
                incoming_placement=pl,
                incoming_defn=defn,
                incoming_instance_id=cid,
                layout_placements=self._session.layout.placements,
                instances=self._session.instances,
                components=self._session.components,
                schematic=self._session.schematic,
                pad_index=_drag_idx,
            )
            for c in pad_cs:
                if c.kind == ConflictKind.DIFFERENT_NETS:
                    hard_pads.append(c.position)
            # --- SOFT: pin-on-foreign-wire (rule 6 strict) ---
            # Pin penetrates the board (rule 5) → scan both sides per
            # pad.  PadIndex.physically_blocked_on() is O(1) and lists
            # are short; cost is dominated by the per-pin loop, not
            # the per-pad scan.
            if _drag_idx is None:
                continue
            for _pin_num, pin_pos in self._session.iter_pin_positions(cid):
                pad = _drag_idx.pad_at(pin_pos)  # type: ignore[attr-defined]
                if pad is None:
                    continue
                # A pin can sit on a pad that hosts wire items on BOTH
                # sides (top + bottom) — e.g. through-hole pad with a
                # bare wire on top *and* an insulated wire on bottom.
                # Collect every foreign-wire id on either side so commit
                # removes all of them, not just the first one scanned.
                pin_hit = False
                for layer in ("top", "bottom"):
                    for it in pad.physically_blocked_on(layer):
                        if not isinstance(it, _WIRE_ITEM_TYPES):
                            continue
                        wid = getattr(it, "id", None)
                        if wid is None or wid in internal_wire_ids:
                            continue
                        if wid in soft_wire_ids:
                            pin_hit = True
                            continue
                        soft_wire_ids.add(wid)
                        pin_hit = True
                if pin_hit:
                    soft_pads.append(pin_pos)
        return hard_pads, soft_pads, soft_wire_ids

    # ------------------------------------------------------------------
    # Debounced full drag-commit validation (1 s after last snap)
    # ------------------------------------------------------------------

    def _ensure_drag_timer(self) -> None:
        """Lazily create the singleshot timer used for debounced validation."""
        if self._drag_timer is not None:
            return
        t = QTimer()
        t.setSingleShot(True)
        t.setInterval(1000)
        t.timeout.connect(self._run_full_drag_validation)
        self._drag_timer = t

    def _start_drag_validation_timer(self) -> None:
        """Restart the 1 s debounce.  Called from on_move on each snap change."""
        self._ensure_drag_timer()
        if self._drag_timer is not None:
            self._drag_timer.start(1000)

    def _stop_drag_validation_timer(self) -> None:
        """Cancel the pending validation (called on release / cancel)."""
        if self._drag_timer is not None:
            self._drag_timer.stop()

    # ------------------------------------------------------------------
    # Long-press group selection / edit-mode entry
    # ------------------------------------------------------------------

    def _ensure_longpress_timer(self) -> None:
        """Lazy-create the singleshot timer driving group long-press."""
        if self._longpress_timer is not None:
            return
        t = QTimer()
        t.setSingleShot(True)
        t.timeout.connect(self._on_longpress_fire)
        self._longpress_timer = t

    def _start_longpress(
        self, group_id: uuid.UUID, view_pt: QPoint,
    ) -> None:
        """Begin the 500ms hold cycle for group *group_id*.

        Sets phase=select, captures the press point for jitter
        detection, and starts the timer.  Caller has already verified
        the click was on the empty bbox area of the group.
        """
        self._ensure_longpress_timer()
        self._longpress_phase = "select"
        self._longpress_group_id = group_id
        self._longpress_press_view_pt = QPoint(view_pt)
        if self._longpress_timer is not None:
            self._longpress_timer.start(_GROUP_LONGPRESS_SELECT_MS)

    def _cancel_longpress(self) -> None:
        """Abort any pending long-press cycle and clear state."""
        if self._longpress_timer is not None:
            self._longpress_timer.stop()
        self._longpress_phase = "idle"
        self._longpress_group_id = None
        self._longpress_press_view_pt = None

    def _on_longpress_fire(self) -> None:
        """Timer callback — advance phase select→edit, or commit edit."""
        gid = self._longpress_group_id
        if gid is None or gid not in self._session.layout.groups:
            self._cancel_longpress()
            return

        if self._longpress_phase == "select":
            # 500ms reached — select the group as a monolith.  This
            # mirrors the existing outline-hit drag-start path but
            # without arming a drag (user explicitly indicated
            # selection intent via hold, not motion).
            from protoboard.domain.layout.group import all_component_ids
            grp_members = frozenset(all_component_ids(self._session.layout.groups, gid))
            if grp_members:
                self._clear_multi_selection()
                self._deselect_wire()
                self._selected_group_id = gid
                self._selected_ids = set(grp_members)
                first = next(iter(grp_members))
                self._select_primary(first)
                for cid in grp_members - {first}:
                    self._draw_multi_highlight(cid)
                self._notify_selection()
            # Phase advances to edit; second 500ms fires the edit-mode entry.
            self._longpress_phase = "edit"
            if self._longpress_timer is not None:
                self._longpress_timer.start(
                    _GROUP_LONGPRESS_EDIT_MS - _GROUP_LONGPRESS_SELECT_MS
                )
            _log.info("longpress.select", group_id=str(gid)[:8])
            return

        if self._longpress_phase == "edit":
            # Full 1000ms held — advance into group edit mode.
            self._group_stack.append(gid)
            self._notify_active_group()
            _log.info("longpress.edit_entered", group_id=str(gid)[:8])
            self._cancel_longpress()
            return

        self._cancel_longpress()

    def _longpress_check_jitter(self, view_pt: QPoint) -> None:
        """Cancel long-press if cursor moved beyond the jitter tolerance."""
        if self._longpress_phase == "idle" or self._longpress_press_view_pt is None:
            return
        dx = view_pt.x() - self._longpress_press_view_pt.x()
        dy = view_pt.y() - self._longpress_press_view_pt.y()
        if dx * dx + dy * dy > _GROUP_LONGPRESS_CANCEL_PX * _GROUP_LONGPRESS_CANCEL_PX:
            _log.debug("longpress.cancelled_jitter", dx=dx, dy=dy)
            self._cancel_longpress()

    def _hit_test_group_bbox_empty(self, scene_pt: QPointF) -> uuid.UUID | None:
        """Return group_id if *scene_pt* falls in a group's outline bbox AND
        no actionable element (pin, wire, component body) lies under the
        cursor.

        Used to gate the long-press: a short click on a wire/component
        inside the bbox passes through to the normal selection flow;
        only empty space triggers group-as-whole long-press.
        """
        # Outline rect hit (gives candidate group).
        outline_gid = self._hit_test_group_outline(scene_pt)
        if outline_gid is None:
            return None
        # Anything under the cursor that would normally be hit?
        if self._hit_test(scene_pt) is not None:
            return None
        if self._hit_test_wire(scene_pt) is not None:
            return None
        return outline_gid

    def _current_drag_ids(self) -> set[uuid.UUID]:
        anchor_id = self._selected_id
        return (
            self._selected_ids
            if self._selected_ids
            else ({anchor_id} if anchor_id else set())
        )

    def _run_full_drag_validation(self) -> None:
        """Compute a full DragCommitDecision and update overlay + corner text.

        Triggered ~1 s after the user stops moving the cursor (and snap stops
        changing).  Cheap pad-pad detection in ``_update_drag_overlay`` still
        provides instant red; this pass surfaces yellow (skewed wires, pin on
        foreign wires, crossing alien pads) with a descriptive corner banner.
        """
        if not self._dragging:
            return
        from protoboard.gui.placement_conflict import classify_drag_commit
        from protoboard.gui.tools.drag_commit_decision import DragSeverity

        drag_ids = self._current_drag_ids()
        if not drag_ids:
            return
        dragged_wires = frozenset(self._drag_orig_wire_paths.keys())
        decision = classify_drag_commit(
            session=self._session,
            moved_comp_ids=drag_ids,
            dragged_wire_ids=dragged_wires,
            drag_set_members=frozenset(drag_ids),
        )
        self._drag_last_decision = decision

        # Update the conflict overlay if it isn't already red. Cheap pad-pad
        # check shows red instantly; a SOFT decision should not downgrade it.
        from protoboard.domain.layout.pad_conflict import (
            MoveConflictResult,
            MoveConflictSeverity,
        )
        if decision.severity == DragSeverity.HARD:
            sev = MoveConflictSeverity.BLOCKED
            blocking = list(decision.blocking_pad_positions)
        elif decision.severity == DragSeverity.SOFT:
            sev = MoveConflictSeverity.REMOVABLE
            blocking = list(decision.overlay_pad_positions)
        else:
            sev = MoveConflictSeverity.CLEAR
            blocking = []
        bbox = self._drag_bbox(drag_ids)
        # Force a redraw at the current snap — bypass needs_update by
        # passing a sentinel position (use the decision's first overlay pos
        # or the bbox center as the "snap" key).
        snap_key: Point | None = None
        if decision.blocking_pad_positions:
            snap_key = decision.blocking_pad_positions[0]
        elif decision.overlay_pad_positions:
            snap_key = decision.overlay_pad_positions[0]
        elif bbox is not None:
            snap_key = Point((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)
        if snap_key is None:
            return
        self._drag_overlay.update(
            self._view.scene(),
            MoveConflictResult(
                severity=sev,
                blocking_pad_positions=blocking,
                removable_wire_ids=list(decision.auto_remove_wire_ids),
            ),
            bbox,
            snap_key,
        )

        # Corner text feedback.
        self._update_corner_text_from_decision(decision)

        # Live drag ghosting (unified drag model, 2026-06-02).  Apply
        # per-wire opacity to surface which wires will be dropped vs
        # rerouted when the user releases.  Wires not in any bucket
        # render at full opacity.
        self._apply_drag_ghost_state(decision)

    def _apply_drag_ghost_state(self, decision: object) -> None:
        """Update wire ghosting per the decision's drag-state buckets.

        * free_drop ∪ stationary_group_drop → opacity 0.3 (silent drop)
        * cross_group_disconnect → opacity 0.5 (user-decision pending)
        * everything else → opacity 1.0 (no drag state)

        Reset (all wires → 1.0) is done by passing ``decision=None`` or
        a decision with empty buckets; callers should also reset when
        the drag ends (``_cancel_floating`` / ``_commit_floating``
        already clear ``_drag_last_decision``, so a subsequent refresh
        cycle wipes the overlay).
        """
        from protoboard.gui.tools.drag_commit_decision import (
            DragCommitDecision,
        )
        if self._view is None:
            return
        states: dict[uuid.UUID, str] = {}
        if isinstance(decision, DragCommitDecision):
            for wid in decision.free_drop_wire_ids:
                states[wid] = "drop"
            for wid in decision.stationary_group_drop_wire_ids:
                states[wid] = "drop"
            for wid in decision.cross_group_disconnect_wire_ids:
                states[wid] = "disconnect"
        setter = getattr(self._view, "set_wire_drag_state", None)
        if callable(setter):
            setter(states)

    def _clear_drag_ghost_state(self) -> None:
        """Reset every wire's opacity to normal (1.0).

        Called from commit/cancel paths so the wire pre-drag style
        returns after the drag is over.
        """
        if self._view is None:
            return
        setter = getattr(self._view, "set_wire_drag_state", None)
        if not callable(setter):
            return
        # Iterate every known wire group and request "normal".
        all_wire_ids = {w.id for w in self._session.layout.wires}
        setter({wid: "normal" for wid in all_wire_ids})

    def _update_corner_text_from_decision(self, decision: object) -> None:
        """Show or hide the corner banner based on *decision*.

        ``decision=None`` hides the banner — used when the cached
        debounced decision expires (snap moved away from the conflict
        zone, see ``_update_drag_overlay``).
        """
        from PySide6.QtGui import QColor
        from protoboard.gui.tools.drag_commit_decision import (
            DragCommitDecision,
            DragSeverity,
        )
        scene = self._view.scene() if self._view is not None else None
        if scene is None:
            return
        if decision is None:
            self._corner_text.hide(scene)
            return
        if not isinstance(decision, DragCommitDecision):
            return
        if decision.severity == DragSeverity.CLEAR or not decision.description:
            self._corner_text.hide(scene)
            return
        color = (
            QColor(220, 50, 30, 220)
            if decision.severity == DragSeverity.HARD
            else QColor(220, 180, 0, 220)
        )
        self._corner_text.show(scene, self._view, decision.description, color)

    def _drag_bbox(
        self, drag_ids: set[uuid.UUID]
    ) -> tuple[float, float, float, float] | None:
        """Current bounding box of all dragged component pads."""
        from protoboard.domain.layout.pad_conflict import _compute_bbox
        from protoboard.domain.components.definition import ComponentDefinition
        from protoboard.domain.components.instance import ComponentInstance
        return _compute_bbox(
            frozenset(drag_ids),
            self._session.layout.placements,
            self._session.instances,
            self._session.components,
        )

    def _build_group_drag_commands(
        self, drag_ids: set[uuid.UUID]
    ) -> list[object] | None:
        """Build commands for an atomic group drag.

        Returns the command list (possibly including RemoveWireCommands for
        removable wire conflicts), or None when the move is fully blocked and
        all components have been reverted to their original positions.
        """
        from protoboard.domain.commands.base import Command
        from protoboard.domain.layout.pad_conflict import (
            MoveConflictSeverity,
            check_group_move_conflicts,
        )

        active_gid = self._selected_group_id
        if active_gid is None:
            _log.debug("build_group_drag.no_group")
            return []

        _log.debug("build_group_drag.start",
                   drag_ids=[str(i) for i in drag_ids],
                   active_gid=str(active_gid))

        # PadIndex over live (post-drag) layout — session.pad_index() is
        # stale until the next layout_changed.emit() at commit time.
        # Shared helper with placement_conflict.classify_drag_commit.
        from protoboard.gui.placement_conflict import _build_drag_pad_index
        _drag_idx = _build_drag_pad_index(self._session)
        result = check_group_move_conflicts(
            moved_ids=drag_ids,
            active_group_id=active_gid,
            layout_placements=self._session.layout.placements,
            layout_wires=self._session.layout.wires,
            layout_groups=self._session.layout.groups,
            instances=self._session.instances,
            components=self._session.components,
            schematic=self._session.schematic,
            moved_wire_ids=set(self._drag_orig_wire_paths.keys()),
            pad_index=_drag_idx,
        )
        # check_group_move_conflicts only knows about pad-on-pad
        # collisions and wires *crossing* pads after the move.  It does
        # NOT cover pin-on-wire conflicts (rule 6 strict: a pin landing
        # on top of an alien wire's vertex / mid-segment).  The
        # debounced live overlay catches these via DragDetector, but
        # the release-time path missed them — a group dropped with its
        # pins on a foreign wire used to commit silently, leaving the
        # wire behind (user-observed 2026-06-01, Group6 case).  Run the
        # same DragDetector pass here and merge its auto_remove_wire_ids
        # into the result.
        wire_remove_ids = self._pin_on_wire_remove_ids(drag_ids, _drag_idx)
        if wire_remove_ids:
            merged = list({*result.removable_wire_ids, *wire_remove_ids})
            from protoboard.domain.layout.pad_conflict import (
                MoveConflictResult,
            )
            result = MoveConflictResult(
                severity=(
                    result.severity
                    if result.severity == MoveConflictSeverity.BLOCKED
                    else MoveConflictSeverity.REMOVABLE
                ),
                blocking_pad_positions=result.blocking_pad_positions,
                removable_wire_ids=merged,
            )

        _log.info("build_group_drag.conflict_result",
                  severity=result.severity.value,
                  blocking_pads=[(p.x, p.y) for p in result.blocking_pad_positions],
                  removable_wires=[str(i) for i in result.removable_wire_ids])

        if result.severity == MoveConflictSeverity.BLOCKED:
            # Do not revert placements here — leave the group where the
            # user dropped it and let the caller (on_release) push the
            # group into the floating-state UX: red overlay markers on
            # the blocking pads, [Esc] cancels and rolls back, dragging
            # further moves the group to a legal spot.  Matches the
            # single-component drag pipeline behaviour for HARD severity
            # (user-requested 2026-06-01).
            object.__setattr__(
                self, "_group_drag_block_pads",
                list(result.blocking_pad_positions),
            )
            _log.info(
                "build_group_drag.blocked_to_floating",
                drag_ids=[str(i) for i in drag_ids],
                blocking_pads=[(p.x, p.y) for p in result.blocking_pad_positions],
            )
            return None

        # Build move + wire-replace commands.
        cmds: list[Command[ComponentState]] = []
        # RemoveWireCommand for any wire the conflict scan marked as
        # removable goes FIRST in the macro.  Otherwise the subsequent
        # MoveComponentCommand → ``_update_wires_for_move`` may
        # translate an endpoint of one of those wires (because the wire
        # is anchored to a moved component via a PinKnot) and drop the
        # wire as skewed, leaving the RemoveWireCommand later in the
        # macro with nothing to remove (KeyError, user-observed
        # 2026-06-01).
        removable_wire_ids: set[uuid.UUID] = set()
        if result.severity == MoveConflictSeverity.REMOVABLE:
            id_set = set(result.removable_wire_ids)
            for wire in self._session.layout.wires:
                if wire.id in id_set:
                    cmds.append(RemoveWireCommand(wire_id=wire.id))
                    removable_wire_ids.add(wire.id)
            sb = getattr(self._view.window(), "statusBar", lambda: None)()
            if sb is not None:
                sb.showMessage(
                    f"Removed {len(result.removable_wire_ids)} colliding wire(s) after group move.",
                    4000,
                )

        # Wires already-removed must also be excluded from Move's
        # ``_update_wires_for_move`` pass — otherwise the helper still
        # sees the (now-deleted) wire entries during enumeration when
        # the layout mutation hasn't been flushed.  Use the union of
        # internal-drag wires and removable wires.
        move_exclude = frozenset(
            self._drag_orig_wire_paths.keys()
        ) | frozenset(removable_wire_ids)

        for cid in drag_ids:
            pl = self._session.layout.placements.get(cid)
            orig = self._drag_orig_positions.get(cid)
            if pl is None or orig is None:
                continue
            if pl.position == orig:
                continue
            old_pl = pl.copy()
            old_pl.position = orig
            cmds.append(MoveComponentCommand(
                component_id=cid,
                new_placement=pl,
                old_placement=old_pl,
                skip_conflict_check=True,  # already verified by check_group_move_conflicts
                exclude_wire_ids=move_exclude,
            ))

        for wire in self._session.layout.wires:
            if wire.id not in self._drag_orig_wire_paths:
                continue
            orig_path = self._drag_orig_wire_paths[wire.id]
            if wire.path == orig_path:
                continue
            cmds.append(_replace_wire_path_cmd(
                self._session.layout, wire, list(wire.path), orig_path,
            ))
            # Diagnostic: log wire endpoint state at command construction
            # so a replace_wire_fragments mismatch (raised inside the
            # macro apply) can be traced back to the per-wire decision
            # this group-drag made.  Looking for: knot kinds, knot
            # positions vs the new path endpoints, which moving
            # component anchors each endpoint.
            from protoboard.domain.layout.knot import (
                JointKnot as _JK,
                PinKnot as _PK,
                WirePinKnot as _WK,
            )
            ka = (
                self._session.layout.knots.get(wire.a.id)
                if wire.a is not None else None
            )
            kb = (
                self._session.layout.knots.get(wire.b.id)
                if wire.b is not None else None
            )

            def _knot_desc(k: object, ref_id: uuid.UUID | None) -> dict[str, object]:
                if k is None:
                    return {"kind": None, "ref_id": str(ref_id)[:8] if ref_id else None}
                kind = (
                    "PinKnot" if isinstance(k, _PK)
                    else "WirePinKnot" if isinstance(k, _WK)
                    else "JointKnot" if isinstance(k, _JK)
                    else type(k).__name__
                )
                desc: dict[str, object] = {
                    "kind": kind,
                    "id": str(k.id)[:8],
                    "n_owners": len(k.wire_ids),
                }
                if isinstance(k, _PK):
                    desc["pin"] = (
                        f"{str(k.pin.component_id)[:8]}/p{k.pin.pin_number}"
                    )
                    desc["anchor_moved"] = (
                        k.pin.component_id in drag_ids
                    )
                elif isinstance(k, (_JK, _WK)):
                    desc["pos"] = (k.position.x, k.position.y)
                return desc

            _log.debug(
                "build_group_drag.wire_replace_cmd",
                wire_id=str(wire.id)[:8],
                old_path_a=(orig_path[0].x, orig_path[0].y) if orig_path else None,
                old_path_b=(orig_path[-1].x, orig_path[-1].y) if orig_path else None,
                new_path_a=(wire.path[0].x, wire.path[0].y) if wire.path else None,
                new_path_b=(wire.path[-1].x, wire.path[-1].y) if wire.path else None,
                a=_knot_desc(ka, wire.a.id if wire.a else None),
                b=_knot_desc(kb, wire.b.id if wire.b else None),
            )

        _log.info(
            "build_group_drag.macro_summary",
            active_gid=str(active_gid)[:8],
            n_drag_ids=len(drag_ids),
            n_drag_orig_wire_paths=len(self._drag_orig_wire_paths),
            n_remove_cmds=len(removable_wire_ids),
            n_move_cmds=sum(
                1 for c in cmds
                if type(c).__name__ == "MoveComponentCommand"
            ),
            n_replace_cmds=sum(
                1 for c in cmds
                if type(c).__name__ == "ReplaceWireFragmentsCommand"
            ),
            total_cmds=len(cmds),
        )
        return cmds  # type: ignore[return-value]

    def _remove_crossing_wires(
        self, moved_comp_ids: set[uuid.UUID]
    ) -> list[RemoveWireCommand]:
        """Return RemoveWireCommand list for wires that cross alien pads after a move.

        Does not execute the commands — caller folds them into a MacroCommand or
        executes them directly.  Shows a status-bar message when wires are removed.
        """
        from protoboard.domain.layout.pad_conflict import find_wires_crossing_pads

        bad_ids = find_wires_crossing_pads(
            wires=self._session.layout.wires,
            moved_comp_ids=moved_comp_ids,
            layout_placements=self._session.layout.placements,
            instances=self._session.instances,
            components=self._session.components,
            schematic=self._session.schematic,
        )
        if not bad_ids:
            return []
        id_set = set(bad_ids)
        cmds = [
            RemoveWireCommand(wire_id=w.id)
            for w in self._session.layout.wires
            if w.id in id_set
        ]
        sb = getattr(self._view.window(), "statusBar", lambda: None)()
        if sb is not None:
            sb.showMessage(
                f"Removed {len(cmds)} colliding wire(s) after move.", 4000
            )
        return cmds

    def _delete_selected(self) -> None:
        comp_id = self._selected_id
        if comp_id is None:
            return
        if self._is_locked_in_group(comp_id):
            return  # grouped — see _is_locked_in_group docstring
        from typing import cast
        from protoboard.domain.commands.base import Command
        cmd = cast(Command[ComponentState], RemoveComponentCommand(component_id=comp_id))
        self._session.stack.execute(cmd, self._session)
        self._deselect()
        self._session.layout_changed.emit()

    # ------------------------------------------------------------------
    # Ctrl+R re-drag — reposition pin 2 interactively
    # ------------------------------------------------------------------

    def _start_redrag(self, comp_id: uuid.UUID) -> None:
        """Enter re-drag mode: the anchor pin is frozen, cursor picks a new pad
        for the bendable (free-range) pin.

        For 2-pin components after flip (vertical_flipped=True) pin 1 holds the
        bend offset, so we redrag pin 1 instead of pin 2.  This keeps the
        semantics consistent: the user always moves the "far" end of the lead.
        """
        if self._is_locked_in_group(comp_id):
            return
        placement = self._session.layout.placements.get(comp_id)
        inst = self._session.instances.get(comp_id)
        if placement is None or inst is None:
            return
        defn = self._session.components.get(inst.definition_ref)
        if defn is None or not defn.bendable_pins:
            return

        if placement.vertical_flipped and defn.footprint.pin_count() == 2:
            # After flip the bend offset lives on pin 1 — that is the "far" end
            # the user wants to reposition.  Use pin 2's spec for reach limits
            # (the library only declares bendable_pins[2] for axial parts).
            pin_num: int = 1
            spec = defn.bendable_pins.get(1) or defn.bendable_pins.get(2)
            if spec is None or not spec.is_free():
                return
        else:
            free_pin = next(
                (p for p, s in sorted(defn.bendable_pins.items()) if s.is_free()), None
            )
            if free_pin is None:
                return
            pin_num = free_pin
        self._redrag_comp_id = comp_id
        self._redrag_pin_num = pin_num
        self._draw_redrag_overlay(comp_id, pin_num, placement)

    def _draw_redrag_overlay(
        self,
        comp_id: uuid.UUID,
        pin_num: int,
        placement: "ComponentPlacement",  # type: ignore[name-defined]
    ) -> None:
        self._clear_redrag_overlay()
        inst = self._session.instances.get(comp_id)
        if inst is None:
            return
        defn = self._session.components.get(inst.definition_ref)
        if defn is None:
            return
        spec = defn.bendable_pins.get(pin_num) or next(
            (s for s in defn.bendable_pins.values() if s.is_free()), None
        )
        if spec is None or not spec.is_free():
            return
        # Reach is measured from the component origin (placement.position = local
        # (0,0) = rotation centre), not from any specific pin world position.
        origin_world = placement.position
        r = 0.18
        from PySide6.QtGui import QBrush, QPen
        from PySide6.QtWidgets import QGraphicsEllipseItem
        green = QColor(0, 200, 80, 120)
        for pt in self._view.board_pad_positions():
            if spec.is_reachable_from_origin(origin_world, pt):
                item = QGraphicsEllipseItem(pt.x - r, pt.y - r, 2 * r, 2 * r)
                item.setBrush(QBrush(green))
                item.setPen(QPen(Qt.GlobalColor.transparent, 0))
                item.setZValue(20.0)
                self._view.scene().addItem(item)
                self._redrag_items.append(item)

    def _update_redrag(self, cursor: Point) -> None:
        """Update the feedback line during re-drag."""
        comp_id = self._redrag_comp_id
        pin_num = self._redrag_pin_num
        if comp_id is None or pin_num is None:
            return
        # Remove old feedback line (last item if it's a line).
        if self._redrag_items and isinstance(self._redrag_items[-1], QGraphicsLineItem):
            self._view.scene().removeItem(self._redrag_items.pop())
        placement = self._session.layout.placements.get(comp_id)
        inst = self._session.instances.get(comp_id)
        if placement is None or inst is None:
            return
        defn = self._session.components.get(inst.definition_ref)
        if defn is None:
            return
        # Feedback line from component origin to cursor.
        origin = placement.position
        line = QGraphicsLineItem(origin.x, origin.y, cursor.x, cursor.y)
        from PySide6.QtGui import QPen
        pen = QPen(QColor(0, 200, 80, 180), 0.05)
        pen.setStyle(Qt.PenStyle.DashLine)
        line.setPen(pen)
        line.setZValue(20.0)
        self._view.scene().addItem(line)
        self._redrag_items.append(line)

    def _commit_redrag(self, cursor: Point) -> None:
        """Commit the re-drag: compute new pin_offsets and save via command."""
        comp_id = self._redrag_comp_id
        pin_num = self._redrag_pin_num
        if comp_id is None or pin_num is None:
            self._cancel_redrag()
            return
        placement = self._session.layout.placements.get(comp_id)
        inst = self._session.instances.get(comp_id)
        if placement is None or inst is None:
            self._cancel_redrag()
            return
        defn = self._session.components.get(inst.definition_ref)
        if defn is None:
            self._cancel_redrag()
            return
        spec = defn.bendable_pins.get(pin_num) or next(
            (s for s in defn.bendable_pins.values() if s.is_free()), None
        )
        # Reach is measured from the component origin (placement.position).
        origin_world = placement.position
        # Snap to nearest board pad
        from PySide6.QtCore import QPointF as _QPointF
        snapped = self._view.snap(_QPointF(cursor.x, cursor.y))
        # Validate reach from origin
        if spec is not None and spec.is_free() and not spec.is_reachable_from_origin(origin_world, snapped):
            self._cancel_redrag()
            return
        # Convert snapped world position to local offset for the pin.
        temp_no_off = placement.copy()
        temp_no_off.pin_offsets = {k: v for k, v in placement.pin_offsets.items()
                                   if k != pin_num}
        baseline = pin_world_position(defn.footprint, temp_no_off, pin_num)
        world_delta = Point(snapped.x - baseline.x, snapped.y - baseline.y)
        # Invert the full pipeline: unrotate, then undo X-mirror for bottom side.
        local_off = unrotate(world_delta, placement.rotation)
        if placement.side == "bottom":
            local_off = Point(-local_off.x, local_off.y)
        # Build new placement
        old_pl = placement.copy()
        new_pl = placement.copy()
        new_offsets = dict(placement.pin_offsets)
        if local_off.x == 0.0 and local_off.y == 0.0:
            new_offsets.pop(pin_num, None)
        else:
            new_offsets[pin_num] = local_off
        new_pl.pin_offsets = new_offsets
        cmd = MoveComponentCommand(
            component_id=comp_id,
            new_placement=new_pl,
            old_placement=old_pl,
        )
        self._session.stack.execute(cmd, self._session)
        self._cancel_redrag()
        self._session.layout_changed.emit()

    def _cancel_redrag(self) -> None:
        self._clear_redrag_overlay()
        self._redrag_comp_id = None
        self._redrag_pin_num = None

    def _clear_redrag_overlay(self) -> None:
        for item in self._redrag_items:
            self._view.scene().removeItem(item)
        self._redrag_items = []

    # ------------------------------------------------------------------
    # Multi-selection
    # ------------------------------------------------------------------

    def _draw_multi_highlight(self, comp_id: uuid.UUID) -> None:
        session = self._session
        positions = [pos for _, pos in session.iter_pin_positions(comp_id)]
        if not positions:
            return
        xs = [p.x for p in positions]
        ys = [p.y for p in positions]
        m = _BBOX_MARGIN + 0.05
        rect = QGraphicsRectItem(
            QRectF(min(xs) - m, min(ys) - m,
                   max(xs) - min(xs) + 2 * m, max(ys) - min(ys) + 2 * m)
        )
        theme = getattr(self._view, "_theme_override", None)
        if theme is not None:
            multi_pen = _make_select_pen(theme.multi_select_color, theme.multi_select_width)
        else:
            multi_pen = _MULTI_SELECT_PEN
        rect.setPen(multi_pen)
        rect.setBrush(Qt.BrushStyle.NoBrush)
        self._view.scene().addItem(rect)
        self._multi_highlights.append(rect)

    def _redraw_multi_highlights(self) -> None:
        for item in self._multi_highlights:
            self._view.scene().removeItem(item)
        self._multi_highlights.clear()
        for cid in self._selected_ids:
            self._draw_multi_highlight(cid)

    def _refresh_all_highlights(self) -> None:
        """Redraw every selection highlight (primary + multi components +
        primary + multi wires) from the current selection state.

        Used after drag-commit / autoroute / unweave / simplify when the
        scene's positions changed but the tool kept its selection — the
        existing highlight items still sit in stale positions.  Each
        sub-redraw clears its current items and re-creates them from
        ``_selected_*`` state, so the result is always consistent with
        the current model.

        Prunes stale ids: any selection that no longer maps to a real
        component / wire in the layout is dropped first.  This handles
        commands like ``unweave_wires`` which replace wires with new ids.
        """
        existing_comps = set(self._session.layout.placements.keys())
        existing_wires = {w.id for w in self._session.layout.wires}
        if self._selected_id is not None and self._selected_id not in existing_comps:
            self._selected_id = None
            self._highlight = None
        self._selected_ids &= existing_comps
        if self._selected_wire_id is not None and self._selected_wire_id not in existing_wires:
            self._selected_wire_id = None
            self._wire_highlight = None
        self._selected_wire_ids &= existing_wires

        if self._selected_id is not None:
            self._select_primary(self._selected_id)
        self._redraw_multi_highlights()
        if self._selected_wire_id is not None:
            self._draw_wire_highlight(self._selected_wire_id)
        self._redraw_multi_wire_highlights()

    def _select_group(self, group_id: uuid.UUID) -> None:
        """Select a group as a unit: clears individual selection, sets group highlight."""
        from protoboard.domain.layout.group import all_component_ids
        groups = self._session.layout.groups
        members = frozenset(all_component_ids(groups, group_id))
        self._deselect()
        self._clear_multi_selection()
        self._selected_group_id = group_id
        self._selected_ids = set(members)
        self._redraw_multi_highlights()
        self._notify_selection()

    # ------------------------------------------------------------------
    # Selection-by-net helpers
    # ------------------------------------------------------------------

    def _nets_for_comp(
        self, comp_id: uuid.UUID, exclude_global: bool = False
    ) -> list[object]:
        """Return all Net objects for a component's pins (unique, ordered by pin)."""
        from protoboard.domain.schematic.net import PinRef
        inst = self._session.instances.get(comp_id)
        defn = self._session.components.get(inst.definition_ref) if inst else None
        if defn is None:
            return []
        seen: set[uuid.UUID] = set()
        nets = []
        for pin_num in range(1, defn.footprint.pin_count() + 1):
            net = self._session.schematic.net_for_pin(
                PinRef(component_id=comp_id, pin_number=pin_num)
            )
            if net is None or net.id in seen:
                continue
            if exclude_global and net.global_net:  # type: ignore[attr-defined]
                continue
            seen.add(net.id)  # type: ignore[attr-defined]
            nets.append(net)
        return nets

    def _select_same_net_comps(
        self, comp_id: uuid.UUID, exclude_global: bool = True
    ) -> None:
        """Select all components sharing any net with comp_id (excl. global nets)."""
        target: set[uuid.UUID] = set()
        for net in self._nets_for_comp(comp_id, exclude_global=exclude_global):
            for pin_ref in net.pins:  # type: ignore[attr-defined]
                target.add(pin_ref.component_id)
        target.discard(comp_id)
        self._deselect()
        self._clear_multi_selection()
        self._selected_ids = target
        self._redraw_multi_highlights()
        self._notify_selection()

    def _select_all_net_wires_and_pins(
        self, comp_id: uuid.UUID, net_id: uuid.UUID | None = None
    ) -> None:
        """Select all wires and components for all (or one) net of comp_id."""
        nets = (
            [n for n in self._nets_for_comp(comp_id) if n.id == net_id]  # type: ignore[attr-defined]
            if net_id is not None
            else self._nets_for_comp(comp_id)
        )
        wire_net_map = self._session.cached_wire_net_map()
        rev: dict[uuid.UUID, uuid.UUID] = {nid: nid for nid in {v for v in wire_net_map.values()}}
        target_nets = {n.id for n in nets}  # type: ignore[attr-defined]
        comp_ids: set[uuid.UUID] = set()
        wire_ids: set[uuid.UUID] = set()
        for net in nets:
            for pin_ref in net.pins:  # type: ignore[attr-defined]
                comp_ids.add(pin_ref.component_id)
        for wid, nid in wire_net_map.items():
            if nid in target_nets:
                wire_ids.add(wid)
        comp_ids.discard(comp_id)
        self._deselect()
        self._clear_multi_selection()
        self._selected_ids = comp_ids
        self._redraw_multi_highlights()
        self._selected_wire_ids = wire_ids
        self._redraw_multi_wire_highlights()
        self._notify_selection()

    def _select_net_wires(self, net_id: uuid.UUID) -> None:
        """Select all wires assigned to the given logical net."""
        wire_net_map = self._session.cached_wire_net_map()
        self._selected_wire_ids = {
            wid for wid, nid in wire_net_map.items() if nid == net_id
        }
        self._redraw_multi_wire_highlights()
        self._notify_selection()

    def _select_wires_physical(self, wire_id: uuid.UUID) -> None:
        """Select all wires continuously electrically connected to wire_id.

        BFS over wire endpoints: if two wires share a pad in the same
        ElectricSet they are considered continuously connected.
        """
        graph = self._session.connectivity_graph()
        wires = self._session.layout.wires

        # Build map: electric_set canonical_key → list[wire_id]
        from collections import defaultdict
        eset_to_wires: dict[object, list[uuid.UUID]] = defaultdict(list)
        wire_esets: dict[uuid.UUID, set[object]] = {}
        for w in wires:
            if len(w.path) < 2:
                continue
            keys: set[object] = set()
            for pt in [w.path[0], w.path[-1]]:
                k = graph.node_for_pad(pt, w.side)
                if k is not None:
                    keys.add(k)
                    eset_to_wires[k].append(w.id)
            wire_esets[w.id] = keys

        # BFS from wire_id
        visited: set[uuid.UUID] = set()
        frontier: list[uuid.UUID] = [wire_id]
        while frontier:
            wid = frontier.pop()
            if wid in visited:
                continue
            visited.add(wid)
            for k in wire_esets.get(wid, set()):
                for neighbour_wid in eset_to_wires.get(k, []):
                    if neighbour_wid not in visited:
                        frontier.append(neighbour_wid)

        self._selected_wire_ids = visited
        self._redraw_multi_wire_highlights()
        self._notify_selection()

    def _select_connected_comps(self, wire_id: uuid.UUID) -> None:
        """Select components whose pins are physically connected to wire_id."""
        graph = self._session.connectivity_graph()
        wire = self._session.wire_by_id(wire_id)
        if wire is None or len(wire.path) < 2:
            return
        # Collect electric set keys touched by this wire.
        wire_keys: set[object] = set()
        for pt in [wire.path[0], wire.path[-1]]:
            k = graph.node_for_pad(pt, wire.side)
            if k is not None:
                wire_keys.add(k)
        if not wire_keys:
            return
        # Find components whose pins share those electric sets.
        from protoboard.domain.schematic.net import PinRef
        target: set[uuid.UUID] = set()
        for cid, placement in self._session.layout.placements.items():
            for _pin_num, pos in self._session.iter_pin_positions(cid):
                k = graph.node_for_pad(pos, placement.side)
                if k in wire_keys:
                    target.add(cid)
                    break
        self._deselect()
        self._clear_multi_selection()
        self._selected_ids = target
        self._redraw_multi_highlights()
        self._notify_selection()

    def _select_net_comps_from_wire(
        self, wire_id: uuid.UUID, logical: bool = True
    ) -> None:
        """Select all components in the same net as wire_id.

        logical=True: uses schematic net (all comps in that net).
        logical=False: uses physical electric set (only directly connected).
        """
        from protoboard.gui.board_view import _build_wire_net_map
        wire_net_map = _build_wire_net_map(self._session)
        net_id = wire_net_map.get(wire_id)
        if net_id is None:
            return
        if logical:
            net = self._session.schematic.nets.get(net_id)
            if net is None:
                return
            target = {ref.component_id for ref in net.pins}
        else:
            self._select_connected_comps(wire_id)
            return
        self._deselect()
        self._clear_multi_selection()
        self._selected_ids = target
        self._redraw_multi_highlights()
        self._notify_selection()

    def _select_to_closest_pin_or_joint(self, wire_id: uuid.UUID, scene_pt: "QPointF") -> None:
        """Select the fragment of wire_id from click point to nearest pin/junction."""
        wire = self._session.wire_by_id(wire_id)
        if wire is None or len(wire.path) < 2:
            return
        graph = self._session.connectivity_graph()
        # Find index of path point closest to click.
        import math
        px, py = scene_pt.x(), scene_pt.y()
        closest_idx = min(
            range(len(wire.path)),
            key=lambda i: math.hypot(wire.path[i].x - px, wire.path[i].y - py),
        )
        # Build set of pin positions (any component pin).
        pin_positions: set[tuple[float, float]] = set()
        for cid in self._session.layout.placements:
            for _pin_num, pos in self._session.iter_pin_positions(cid):
                pin_positions.add((round(pos.x, 4), round(pos.y, 4)))
        # Build set of junction positions (pad shared by >1 wire endpoint).
        from collections import Counter
        endpoint_counts: Counter[tuple[float, float]] = Counter()
        for w in self._session.layout.wires:
            if len(w.path) >= 2:
                endpoint_counts[(round(w.path[0].x, 4), round(w.path[0].y, 4))] += 1
                endpoint_counts[(round(w.path[-1].x, 4), round(w.path[-1].y, 4))] += 1
        junctions = {pos for pos, cnt in endpoint_counts.items() if cnt > 1}
        stop_positions = pin_positions | junctions

        def _is_stop(pt: object) -> bool:
            from protoboard.geometry import Point as _Pt
            if not isinstance(pt, _Pt):
                return False
            return (round(pt.x, 4), round(pt.y, 4)) in stop_positions

        # Walk from closest_idx towards each end, stop at pin/junction.
        def _find_stop(start: int, direction: int) -> int:
            i = start
            while 0 <= i < len(wire.path):
                if i != start and _is_stop(wire.path[i]):
                    return i
                i += direction
            return max(0, min(len(wire.path) - 1, i - direction))

        left = _find_stop(closest_idx, -1)
        right = _find_stop(closest_idx, 1)
        # Select the shorter fragment.
        if closest_idx - left <= right - closest_idx:
            frag_start, frag_end = left, closest_idx
        else:
            frag_start, frag_end = closest_idx, right
        # Encode as wire selection with fragment info via _edit_start_idx.
        self._select_wire(wire_id)
        self._edit_start_idx = frag_start
        self._edit_end_idx = frag_end
        self._redraw_wire_edit_nodes()

    def _highlight_selection(self) -> None:
        """Push current selection onto the HighlightStack (Dimmed #2)."""
        from protoboard.gui.highlight_state import HighlightEntry, PROFILE_DIMMED_2
        comp_ids = frozenset(self._selected_ids)
        if self._selected_id is not None:
            comp_ids = comp_ids | {self._selected_id}
        wire_ids = frozenset(self._selected_wire_ids)
        if self._selected_wire_id is not None:
            wire_ids = wire_ids | {self._selected_wire_id}
        if not comp_ids and not wire_ids:
            return
        entry = HighlightEntry(
            comp_ids=comp_ids,
            wire_ids=wire_ids,
            profile=PROFILE_DIMMED_2,
            label="User",
        )
        self._session.highlight_stack.push(entry)

    def _clear_multi_selection(self) -> None:
        for item in self._multi_highlights:
            self._view.scene().removeItem(item)
        self._multi_highlights.clear()
        self._selected_ids.clear()
        self._selected_group_id = None

    def _delete_multi(self) -> None:
        existing_comps = set(self._session.layout.placements.keys())
        ids = [
            cid for cid in self._selected_ids
            if cid in existing_comps and not self._is_locked_in_group(cid)
        ]
        if not ids:
            return
        from typing import cast

        from protoboard.domain.commands.base import Command

        from protoboard.domain.commands.component_cmds import SchematicState

        commands = cast(
            list[Command[SchematicState]],
            [RemoveComponentCommand(component_id=cid) for cid in ids],
        )
        macro = cast(
            Command[ComponentState],
            MacroCommand(commands, description="Delete components"),
        )
        self._session.stack.execute(macro, self._session)
        self._clear_multi_selection()
        self._deselect()
        self._session.layout_changed.emit()

    # ------------------------------------------------------------------
    # Rubber-band selection
    # ------------------------------------------------------------------

    def _ensure_rubber_band_timer(self) -> None:
        if self._rubber_band_timer is not None:
            return
        timer = QTimer(self._view)
        timer.setSingleShot(True)
        # Interval lives on the view so PreferencesDialog can adjust it
        # without rebuilding the tool.  Default 33 ms ≈ 30 fps — faster
        # than the human eye can follow rubber-band geometry; over SSH
        # it keeps X11 traffic to a single round-trip per frame instead
        # of one per mouse-move event.
        interval = getattr(self._view, "_rubber_band_throttle_ms", 33)
        timer.setInterval(max(0, int(interval)))
        timer.timeout.connect(self._flush_rubber_band_update)
        self._rubber_band_timer = timer

    def _update_rubber_band(self, scene_pt: QPointF) -> None:
        """Schedule a rubber-band repaint at the next throttle tick.

        Replaces the legacy "redraw on every mouseMoveEvent" path.
        Stores the latest scene-pt and starts the throttle timer if
        idle; if the timer is already running, this becomes a free
        no-op (the most recent pending pt always wins).
        """
        if self._rubber_band_start is None:
            return
        self._pending_rubber_band_pt = scene_pt
        self._ensure_rubber_band_timer()
        if self._rubber_band_timer is not None and not self._rubber_band_timer.isActive():
            self._rubber_band_timer.start()

    def _flush_rubber_band_update(self) -> None:
        pt = self._pending_rubber_band_pt
        self._pending_rubber_band_pt = None
        if pt is None:
            return
        self._apply_rubber_band(pt)

    def _apply_rubber_band(self, scene_pt: QPointF) -> None:
        """Render the rubber-band rect for *scene_pt* — synchronous.

        Reuses the existing ``QGraphicsRectItem`` if present; only
        creates a new one on the first call after the rubber-band
        started.  ``setPen``/``setBrush`` only fire when the LR/RL
        direction actually changes, so steady-state moves cost a single
        ``setRect`` call.
        """
        if self._rubber_band_start is None:
            return
        start = self._rubber_band_start
        x0 = min(start.x(), scene_pt.x())
        y0 = min(start.y(), scene_pt.y())
        w = abs(scene_pt.x() - start.x())
        h = abs(scene_pt.y() - start.y())

        left_to_right = scene_pt.x() >= start.x()
        rect = QRectF(x0, y0, w, h)

        if self._rubber_band_item is None:
            pen = _RUBBER_BAND_PEN_LR if left_to_right else _RUBBER_BAND_PEN_RL
            fill = _RUBBER_BAND_FILL_LR if left_to_right else _RUBBER_BAND_FILL_RL
            item = QGraphicsRectItem(rect)
            item.setPen(pen)
            item.setBrush(fill)
            self._view.scene().addItem(item)
            self._rubber_band_item = item
            self._rubber_band_last_lr = left_to_right
            return

        # Reuse existing item — single setRect per frame.
        self._rubber_band_item.setRect(rect)
        if self._rubber_band_last_lr is not left_to_right:
            pen = _RUBBER_BAND_PEN_LR if left_to_right else _RUBBER_BAND_PEN_RL
            fill = _RUBBER_BAND_FILL_LR if left_to_right else _RUBBER_BAND_FILL_RL
            self._rubber_band_item.setPen(pen)
            self._rubber_band_item.setBrush(fill)
            self._rubber_band_last_lr = left_to_right

    def _finish_rubber_band(self, scene_pt: QPointF) -> None:
        self._clear_rubber_band()
        if self._rubber_band_start is None:
            return
        start = self._rubber_band_start
        self._rubber_band_start = None

        left_to_right = scene_pt.x() >= start.x()
        rx0 = min(start.x(), scene_pt.x())
        ry0 = min(start.y(), scene_pt.y())
        rx1 = max(start.x(), scene_pt.x())
        ry1 = max(start.y(), scene_pt.y())

        # Minimum drag size to avoid accidental selection.
        if (rx1 - rx0) < 0.1 and (ry1 - ry0) < 0.1:
            self._rubber_band_start = None
            return

        self._rubber_band_start = None
        session = self._session
        selected: set[uuid.UUID] = set()
        if self._filter_components():
            for comp_id in session.layout.placements:
                bbox = session.component_bbox(comp_id)
                if bbox is None:
                    continue
                bx0 = bbox.x
                by0 = bbox.y
                bx1 = bbox.x + bbox.width
                by1 = bbox.y + bbox.height
                if left_to_right:
                    # Fully inside.
                    if bx0 >= rx0 and by0 >= ry0 and bx1 <= rx1 and by1 <= ry1:
                        selected.add(comp_id)
                else:
                    # Intersects.
                    if bx0 < rx1 and bx1 > rx0 and by0 < ry1 and by1 > ry0:
                        selected.add(comp_id)

        # Wires.
        selected_wires: set[uuid.UUID] = set()
        _wires_to_check = session.layout.wires if self._filter_wires() else []
        for wire in _wires_to_check:
            pts = wire.path
            if left_to_right:
                # Fully inside: all points within rect.
                if all(rx0 <= p.x <= rx1 and ry0 <= p.y <= ry1 for p in pts):
                    selected_wires.add(wire.id)
            else:
                # Intersects: any point inside OR any segment crosses rect.
                if any(rx0 <= p.x <= rx1 and ry0 <= p.y <= ry1 for p in pts):
                    selected_wires.add(wire.id)
                elif any(
                    _segment_intersects_rect(
                        pts[i].x, pts[i].y, pts[i + 1].x, pts[i + 1].y,
                        rx0, ry0, rx1, ry1,
                    )
                    for i in range(len(pts) - 1)
                ):
                    selected_wires.add(wire.id)

        if selected or selected_wires:
            _log.debug("selection.rubber_band",
                       mode="ltr" if left_to_right else "rtl",
                       components=len(selected), wires=len(selected_wires))
            self._selected_ids = selected
            self._redraw_multi_highlights()
            self._selected_wire_ids = selected_wires
            self._redraw_multi_wire_highlights()
            if selected:
                self._select_primary(next(iter(selected)))
            else:
                self._notify_selection()

    def _clear_rubber_band(self) -> None:
        # Stop pending throttle ticks first — a tick firing after the
        # scene item has been removed would either no-op (because the
        # pending pt was cleared below) or, worse, create a fresh
        # rubber-band rectangle after the user already released the
        # button.  Cheap to call when the timer was never built.
        if self._rubber_band_timer is not None and self._rubber_band_timer.isActive():
            self._rubber_band_timer.stop()
        self._pending_rubber_band_pt = None
        self._rubber_band_last_lr = None
        if self._rubber_band_item is not None:
            self._view.scene().removeItem(self._rubber_band_item)
            self._rubber_band_item = None

    # ------------------------------------------------------------------
    # Wire selection
    # ------------------------------------------------------------------

    def _hit_test_wire(self, scene_pt: QPointF) -> uuid.UUID | None:
        px, py = scene_pt.x(), scene_pt.y()
        best_id: uuid.UUID | None = None
        best_d = _WIRE_HIT_DIST

        # Use spatial index: check ±1 cell around cursor to find wire candidates.
        cell_x, cell_y = round(px), round(py)
        candidates: set[uuid.UUID] = set()
        spatial = self._session.wire_spatial_index()
        for dcx in (-1, 0, 1):
            for dcy in (-1, 0, 1):
                for wid in spatial.get((cell_x + dcx, cell_y + dcy), []):
                    candidates.add(wid)

        # Look up each candidate via the session's O(1) wire-by-id cache
        # rather than rebuilding a dict from `layout.wires` (O(N)).  The
        # spatial index has already narrowed us down to a handful of
        # neighbours; the previous comprehension walked the whole list.
        for wid in candidates:
            wire = self._session.wire_by_id(wid)
            if wire is None:
                continue
            d = _min_dist_to_wire(px, py, wire)
            if d < best_d:
                best_d = d
                best_id = wid
        return best_id

    def _select_wire(self, wire_id: uuid.UUID) -> None:
        self._deselect_wire()
        self._selected_wire_id = wire_id
        _log.info("selection.wire", wire_id=str(wire_id))
        self._draw_wire_highlight(wire_id)

    def _select_wire_cycled(self, wire_id: uuid.UUID, scene_pt: "QPointF") -> None:
        """Cycle wire selection mode on repeated clicks on the same wire.

        Cycle:
          1st click → full electric net (all wires of the net) + component pads
          2nd click → full electric net, no component pads
          3rd click → only the clicked wire segment
          4th click → wraps back to 1st
        """
        _t0 = time.monotonic() if _CLICK_PROFILE else 0.0
        if self._wire_click_id == wire_id:
            self._wire_click_cycle = (self._wire_click_cycle % 3) + 1
        else:
            self._wire_click_id = wire_id
            self._wire_click_cycle = 1

        wire = self._session.wire_by_id(wire_id)
        if wire is None:
            self._select_wire(wire_id)
            self._notify_selection()
            return

        if self._wire_click_cycle == 3:
            # Segment only — original behaviour; clear net highlight.
            self._select_wire(wire_id)
            self._wire_click_id = wire_id
            self._wire_click_cycle = 3
            if self._view is not None:
                self._view.set_highlighted_net(None)
            self._notify_selection()
            return

        # Cycles 1 and 2: select all wires in the same electric net.
        # The wire→net map is cached on the session so this is one dict
        # lookup, not a full board scan with per-pad graph queries.
        wnm = self._session.cached_wire_net_map()
        clicked_net_id = wnm.get(wire_id)
        if clicked_net_id is None:
            # Wire not assigned to any logical net — fall back to segment.
            self._select_wire(wire_id)
            return
        net_wire_ids: set[uuid.UUID] = {
            wid for wid, nid in wnm.items() if nid == clicked_net_id
        }

        # Select the clicked wire as primary, rest as multi-selection.
        # _select_wire calls _deselect_wire which resets _wire_click_id/_wire_click_cycle,
        # so restore cycle state immediately after.
        cycle = self._wire_click_cycle
        self._select_wire(wire_id)
        self._wire_click_id = wire_id
        self._wire_click_cycle = cycle
        for wid in net_wire_ids:
            if wid != wire_id:
                self._selected_wire_ids.add(wid)
                self._draw_multi_wire_highlight(wid)

        # Activate net highlight + dim for cycles 1 and 2.
        if self._view is not None:
            self._highlight_net_id = clicked_net_id
            self._view.set_highlighted_net(clicked_net_id)

        if self._wire_click_cycle == 1:
            # Highlight component pins electrically connected to this net
            # as red circles (cycle 1 only — cleared on cycle 2/3/deselect).
            # The schematic carries the net→pins mapping directly, so we
            # don't need to query the graph per pin.
            net_obj = self._session.schematic.nets.get(clicked_net_id)
            pad_positions: list[Point] = []
            if net_obj is not None:
                for ref in net_obj.pins:
                    if ref.component_id in self._session.layout.placements:
                        for pin_num, pos in self._session.iter_pin_positions(ref.component_id):
                            if pin_num == ref.pin_number:
                                pad_positions.append(pos)
                                break
            self._draw_net_pad_highlights(pad_positions)

        if _CLICK_PROFILE:
            _log.info(
                "click_profile.select_wire_cycled",
                cycle=self._wire_click_cycle,
                n_net_wires=len(net_wire_ids),
                ms=round((time.monotonic() - _t0) * 1000, 1),
            )
        self._notify_selection()

    def _draw_net_pad_highlights(self, positions: "list[Point]") -> None:
        """Draw red circles on pin positions connected to the selected net (cycle 1)."""
        from protoboard.gui.layer_state import Z_SELECTION
        self._clear_net_pad_highlights()
        self._net_pad_highlight_positions = list(positions)
        r = 0.38
        pen = QPen(QColor(220, 40, 40), 0.12)
        brush = QBrush(QColor(220, 40, 40, 160))
        for pt in positions:
            item = QGraphicsEllipseItem(pt.x - r, pt.y - r, 2 * r, 2 * r)
            item.setPen(pen)
            item.setBrush(brush)
            item.setZValue(Z_SELECTION + 1.0)
            self._view.scene().addItem(item)
            self._net_pad_highlight_items.append(item)

    def _clear_net_pad_highlights(self) -> None:
        for item in self._net_pad_highlight_items:
            try:
                self._view.scene().removeItem(item)
            except RuntimeError:
                pass  # C++ object already deleted by scene.clear()
        self._net_pad_highlight_items.clear()
        self._net_pad_highlight_positions.clear()

    def _deselect_wire(self) -> None:
        self._cancel_wire_edit()
        if self._wire_highlight is not None:
            self._view.scene().removeItem(self._wire_highlight)
            self._wire_highlight = None
        self._selected_wire_id = None
        self._clear_multi_wire_selection()
        self._clear_net_pad_highlights()
        self._wire_click_id = None
        self._wire_click_cycle = 0

    def _draw_wire_highlight(self, wire_id: uuid.UUID) -> None:
        if self._wire_highlight is not None:
            self._view.scene().removeItem(self._wire_highlight)
            self._wire_highlight = None
        wire = self._session.wire_by_id(wire_id)
        if wire is None or len(wire.path) < 2:
            return
        from protoboard.gui.layer_state import Z_SELECTION
        group = QGraphicsItemGroup()
        pts = wire.path
        for i in range(len(pts) - 1):
            seg = QGraphicsLineItem(
                pts[i].x, pts[i].y, pts[i + 1].x, pts[i + 1].y
            )
            seg.setPen(_WIRE_SELECT_PEN)
            group.addToGroup(seg)
        for i, pt in enumerate(pts):
            r = _NODE_RADIUS
            is_start = (wire_id == self._edit_start_wire_id and i == self._edit_start_idx)
            is_end = (wire_id == self._edit_end_wire_id and i == self._edit_end_idx)
            is_sel = is_start or is_end
            dot = QGraphicsEllipseItem(pt.x - r, pt.y - r, 2 * r, 2 * r)
            dot.setPen(_NODE_SELECTED_PEN if is_sel else _NODE_PEN)
            dot.setBrush(_NODE_SELECTED_BRUSH if is_sel else _NODE_BRUSH)
            group.addToGroup(dot)
        group.setZValue(Z_SELECTION)
        self._view.scene().addItem(group)
        self._wire_highlight = group

    def _delete_wire(self, *, force: bool = False) -> None:
        wire_id = self._selected_wire_id
        if wire_id is None:
            return
        # Block deletion of wires structurally owned by a group we
        # haven't entered, unless *force* is set.  Selection (for
        # highlight) stays allowed; mutation requires entering the
        # group's edit context or passing force=True (typically
        # Shift+Delete from bulk shortcuts).
        if not force and self._wire_is_locked(wire_id):
            _log.info("delete_wire.skipped_locked", wire_id=str(wire_id)[:8])
            return
        self._cancel_wire_edit()
        cmd = RemoveWireCommand(wire_id=wire_id)
        self._session.stack.execute(cmd, self._session)
        self._deselect_wire()
        self._session.layout_changed.emit()

    def _draw_multi_wire_highlight(self, wire_id: uuid.UUID) -> None:
        wire = self._session.wire_by_id(wire_id)
        if wire is None or len(wire.path) < 2:
            return
        from protoboard.gui.layer_state import Z_SELECTION
        group = QGraphicsItemGroup()
        pts = wire.path
        for i in range(len(pts) - 1):
            seg = QGraphicsLineItem(
                pts[i].x, pts[i].y, pts[i + 1].x, pts[i + 1].y
            )
            seg.setPen(_WIRE_SELECT_PEN)
            group.addToGroup(seg)
        group.setZValue(Z_SELECTION)
        self._view.scene().addItem(group)
        self._wire_multi_highlights.append(group)

    def _redraw_multi_wire_highlights(self) -> None:
        for item in self._wire_multi_highlights:
            self._view.scene().removeItem(item)
        self._wire_multi_highlights.clear()
        for wid in self._selected_wire_ids:
            self._draw_multi_wire_highlight(wid)

    def _clear_multi_wire_selection(self) -> None:
        for item in self._wire_multi_highlights:
            self._view.scene().removeItem(item)
        self._wire_multi_highlights.clear()
        self._selected_wire_ids.clear()

    def _delete_multi_wires(self, *, force: bool = False) -> None:
        # Include primary wire (_selected_wire_id) alongside multi-selection.
        all_ids = set(self._selected_wire_ids)
        if self._selected_wire_id is not None:
            all_ids.add(self._selected_wire_id)
        # Filter out stale IDs (wire may have been removed by a previous operation).
        existing = {w.id for w in self._session.layout.wires}
        ids = [wid for wid in all_ids if wid in existing]
        # Drop wires structurally owned by a non-active group unless
        # *force* is set.  Force is passed by Shift+Delete bulk
        # shortcuts where the user explicitly intends to override the
        # group-lock guard.
        if not force:
            n_before = len(ids)
            ids = [wid for wid in ids if not self._wire_is_locked(wid)]
            n_skipped = n_before - len(ids)
            if n_skipped:
                _log.info("delete_wires.skipped_locked", n_skipped=n_skipped)
        if not ids:
            return
        from typing import cast

        from protoboard.domain.commands.base import Command

        commands = cast(
            list[Command[ComponentState]],
            [RemoveWireCommand(wire_id=wid) for wid in ids],
        )
        if len(commands) == 1:
            self._session.stack.execute(commands[0], self._session)
        else:
            macro = MacroCommand(commands, description="Delete wires")
            self._session.stack.execute(macro, self._session)
        self._deselect_wire()
        self._notify_selection()
        self._session.layout_changed.emit()

    # ------------------------------------------------------------------
    # Simplify selected wires (generic shortcut algorithm)
    # ------------------------------------------------------------------

    def _simplify_wires(
        self,
        wire_ids: set[uuid.UUID] | frozenset[uuid.UUID],
        *,
        mode_override: str | None = None,
    ) -> None:
        """Run generic_simplify_wire on every wire id in *wire_ids*.

        Uses the project's RoutingSettings.simplify_mode unless
        *mode_override* is one of "chebyshev" / "bare_parity".
        Builds one ReplaceWireFragmentsCommand per wire whose path actually
        changed and packs them into a MacroCommand for atomic undo.
        """
        from typing import cast
        from protoboard.domain.commands.base import Command, MacroCommand
        from protoboard.services.routing.postprocess import (
            SimplifyMode,
            generic_simplify_wire,
        )

        session = self._session
        if session is None:
            return

        # Resolve mode.
        if mode_override == "chebyshev":
            mode = SimplifyMode.CHEBYSHEV
        elif mode_override == "bare_parity":
            mode = SimplifyMode.BARE_PARITY
        else:
            rs = session.routing_settings
            mode = getattr(rs, "simplify_mode", SimplifyMode.BARE_PARITY)

        # Build pin_positions across all components for "don't drop a pin" check.
        pin_positions: set[Point] = set()
        for comp_id in session.layout.placements:
            for _pin_num, pos in session.iter_pin_positions(comp_id):
                pin_positions.add(pos)

        # Map every assigned pad → its net id (for alien-pad detection on bare).
        pad_to_net: dict[Point, uuid.UUID] = {}
        for net_obj in session.schematic.nets.values():
            for ref in net_obj.pins:
                pos = session.get_pin_position(ref.component_id, ref.pin_number)
                if pos is not None:
                    pad_to_net.setdefault(pos, net_obj.id)

        # All board pads on either side — bare wires keep pad-vertices on
        # shortcuts so connectivity / wire-edit see every contact.
        from protoboard.domain.layout.wire_utils import board_pads_for_side
        all_board_pads = (
            board_pads_for_side(session.board, "top")
            | board_pads_for_side(session.board, "bottom")
        )

        # Map each wire id → its net id by intersecting wire path with
        # pad_to_net.  When a wire touches any pinned pad with a known net,
        # that net is the wire's net (taking the first match is safe — wires
        # belong to a single net by construction).
        all_wires = session.layout.wires
        wire_to_net: dict[uuid.UUID, uuid.UUID | None] = {}
        for w in all_wires:
            wnet: uuid.UUID | None = None
            for pt in w.path:
                if pt in pad_to_net:
                    wnet = pad_to_net[pt]
                    break
            wire_to_net[w.id] = wnet

        # PadIndex is the authoritative pad-state source — fixes shorts
        # the heuristic checks missed (shared pads, plated-through cross-
        # layer, alien pins).
        pad_index = session.pad_index()
        cmds: list[Command[ComponentState]] = []
        for wid in wire_ids:
            wire = next((w for w in all_wires if w.id == wid), None)
            if wire is None:
                continue
            others = [w for w in all_wires if w.id != wid]
            new_wire = generic_simplify_wire(
                wire,
                other_wires=others,
                pin_positions=pin_positions,
                pad_to_net=pad_to_net,
                mode=mode,
                board_pads=all_board_pads,
                wire_net_id=wire_to_net.get(wid),
                other_wire_nets=wire_to_net,
                pin_net_map=pad_to_net,
                pad_index=pad_index,
            )
            if new_wire is wire or new_wire.path == wire.path:
                continue
            cmds.append(
                cast(
                    Command[ComponentState],
                    _replace_wire_path_cmd(
                        session.layout, wire, list(new_wire.path), list(wire.path),
                    ),
                )
            )

        if not cmds:
            self._status_msg("Simplify: nothing to shorten.")
            return
        cmd = cmds[0] if len(cmds) == 1 else MacroCommand(
            cmds, description=f"Simplify {len(cmds)} wire(s)"
        )
        session.stack.execute(cmd, session)
        session.layout_changed.emit()
        self._refresh_all_highlights()

    # ------------------------------------------------------------------
    # Optimize selected wires via plugin
    # ------------------------------------------------------------------

    def _optimize_selected_wires(
        self,
        wire_ids: set[uuid.UUID] | frozenset[uuid.UUID],
        opt_name: str,
    ) -> None:
        """Run a registered WireOptimizer on the selected wires.

        Builds an immutable RouteRequest snapshot of the current session
        and a synthetic AutorouteResult containing only the selected
        wires.  The optimizer is responsible for returning a result
        whose wires are the new versions; wires it dropped are treated
        as deletions, new wires (those the optimizer returned but that
        weren't in the input) are treated as additions, and matching
        ids whose path/side/insulated changed are replaced.

        Synchronous on purpose: the operation runs on a subset of the
        layout (PPM is normally a handful of wires), so blocking briefly
        is preferable to a background-thread complication for an
        interactive action.
        """
        from typing import cast
        from protoboard.domain.commands.base import Command, MacroCommand
        from protoboard.domain.commands.component_cmds import (
            AddWireCommand,
            RemoveWireCommand,
        )
        from protoboard.services.routing.autorouter import AutorouteResult
        from protoboard.services.routing.optimizer import (
            registry as _opt_registry,
        )
        from protoboard.services.routing.protocol import ProgressSink
        from protoboard.services.routing.snapshot import RouteRequest

        session = self._session
        if session is None:
            return
        if not wire_ids:
            self._status_msg("Optimize: nothing selected.")
            return

        try:
            optimizer = _opt_registry.get(opt_name)
        except KeyError:
            self._status_msg(f"Optimize: plugin {opt_name!r} is not registered.")
            return

        # Build the immutable request from live session state.
        request = RouteRequest.from_session(
            board=session.board,
            layout=session.layout,
            schematic=session.schematic,
            components=session.components,
            instances=session.instances,
            routing_settings=session.routing_settings,
        )

        # The optimizer sees only the selected wires as its "input result".
        selected_wires = [w for w in session.layout.wires if w.id in wire_ids]
        if not selected_wires:
            self._status_msg("Optimize: no matching wires in layout.")
            return
        result_in = AutorouteResult(
            wires=list(selected_wires),
            unrouted_nets=[],
            pass_stats=[],
        )

        try:
            result_out = optimizer.optimize(request, result_in, ProgressSink())
        except Exception as exc:  # noqa: BLE001
            self._status_msg(f"Optimize: plugin failed — {exc}")
            return

        # Translate diff into commands.  An optimizer may:
        # - keep a wire unchanged (skip),
        # - replace a wire (same id, different path/side/insulated),
        # - drop a wire (id no longer in output),
        # - add a new wire (id never seen before — rare but allowed).
        out_by_id = {w.id: w for w in result_out.wires}
        cmds: list[Command[ComponentState]] = []

        for wire_in in selected_wires:
            wire_out = out_by_id.pop(wire_in.id, None)
            if wire_out is None:
                # Dropped by the optimizer.
                cmds.append(cast(Command[ComponentState], RemoveWireCommand(wire_id=wire_in.id)))
                continue
            if (
                wire_out.path == wire_in.path
                and wire_out.side == wire_in.side
                and wire_out.insulated == wire_in.insulated
            ):
                continue  # No-op for this wire.
            # Side or insulation changed — full Remove+Add cycle.  Path-only
            # changes could use ReplaceWireFragmentsCommand, but mixing the two
            # paths complicates undo grouping, so always go Remove+Add.
            cmds.append(cast(Command[ComponentState], RemoveWireCommand(wire_id=wire_in.id)))
            cmds.append(cast(Command[ComponentState], AddWireCommand(wire=wire_out)))

        # Anything left in out_by_id is a freshly created wire — add it.
        for new_wire in out_by_id.values():
            cmds.append(cast(Command[ComponentState], AddWireCommand(wire=new_wire)))

        if not cmds:
            self._status_msg(
                f"Optimize ({getattr(optimizer, 'display_name', opt_name)}): "
                f"nothing changed."
            )
            return

        cmd = cmds[0] if len(cmds) == 1 else MacroCommand(
            cmds,
            description=(
                f"Optimize {len(selected_wires)} wire(s) via "
                f"{getattr(optimizer, 'display_name', opt_name)}"
            ),
        )
        session.stack.execute(cmd, session)
        session.layout_changed.emit()

    # ------------------------------------------------------------------
    # Unweave selected wires (topological fragment swap)
    # ------------------------------------------------------------------

    def _unweave_wires(
        self,
        wire_ids: set[uuid.UUID] | frozenset[uuid.UUID],
    ) -> None:
        """Run unweave_wires on every wire id in *wire_ids*.

        The unweave algorithm may split a single wire into several
        WireSegments (different ``side``/``insulated`` along the path) or
        merge wires sharing endpoint pads.  We therefore replace the whole
        selection: RemoveWireCommand for every selected wire, then
        AddWireCommand for every new Wire, all bundled in a single
        MacroCommand for atomic undo.
        """
        from typing import cast
        from protoboard.domain.commands.base import Command, MacroCommand
        from protoboard.domain.commands.component_cmds import (
            AddWireCommand, RemoveWireCommand,
        )
        from protoboard.services.routing.unweave import (
            count_mutual_crossings,
            unweave_wires,
        )

        session = self._session
        _log.info("unweave.start",
                  n_wire_ids=len(wire_ids),
                  wire_ids=[str(w) for w in wire_ids])
        if session is None:
            _log.info("unweave.abort", reason="no_session")
            return
        if len(wire_ids) < 2:
            _log.info("unweave.abort", reason="too_few_ids", n=len(wire_ids))
            self._status_msg("Unweave: select at least 2 wires.")
            return

        all_wires = session.layout.wires
        targets = [w for w in all_wires if w.id in wire_ids]
        if len(targets) < 2:
            _log.info("unweave.abort", reason="too_few_targets",
                      n_targets=len(targets), n_ids=len(wire_ids))
            self._status_msg("Unweave: at least 2 valid wires required.")
            return

        before = count_mutual_crossings(targets)
        _log.info("unweave.targets_resolved",
                  n_targets=len(targets),
                  crossings_before=before,
                  target_summary=[
                      {"id": str(w.id), "side": w.side, "insulated": w.insulated,
                       "n_vertices": len(w.path),
                       "start": (w.path[0].x, w.path[0].y) if w.path else None,
                       "end": (w.path[-1].x, w.path[-1].y) if w.path else None}
                      for w in targets
                  ])
        if before == 0:
            _log.info("unweave.abort", reason="no_crossings")
            self._status_msg("Unweave: no crossings to resolve.")
            return

        # Pass schematic + connectivity graph so the algorithm can split
        # wires on plated-through pads when that resolves more crossings.
        import time as _time
        _t0 = _time.monotonic()
        out = unweave_wires(
            targets,
            schematic=session.schematic,
            graph=session.connectivity_graph(),
            allow_layer_change=session.routing_settings.allow_layer_change,
        )
        dt_ms = (_time.monotonic() - _t0) * 1000.0
        after = count_mutual_crossings(out)
        _log.info("unweave.algorithm_done",
                  duration_ms=round(dt_ms, 1),
                  n_in=len(targets), n_out=len(out),
                  crossings_before=before, crossings_after=after)

        # If the result is byte-identical (same paths, same attributes) skip.
        same = (
            len(out) == len(targets)
            and all(
                a.path == b.path
                and a.side == b.side
                and a.insulated == b.insulated
                for a, b in zip(targets, out)
            )
        )
        if same:
            _log.info("unweave.abort", reason="byte_identical_result",
                      crossings=before)
            self._status_msg(
                f"Unweave: {before} crossings already at topological minimum."
            )
            return

        cmds: list[Command[ComponentState]] = []
        for orig in targets:
            cmds.append(cast(
                Command[ComponentState],
                RemoveWireCommand(wire_id=orig.id),
            ))
        for new_w in out:
            cmds.append(cast(
                Command[ComponentState],
                AddWireCommand(wire=new_w),
            ))

        cmd = MacroCommand(
            cmds, description=f"Unweave {len(targets)} → {len(out)} wire(s)"
        )
        session.stack.execute(cmd, session)
        session.invalidate_graph()
        session.layout_changed.emit()
        # Unweave swaps fragments between wires and may emit fresh ids
        # for split halves — the old selection ids no longer map to
        # real wires.  ``_refresh_all_highlights`` prunes stale ids and
        # redraws what survives.
        self._refresh_all_highlights()
        _log.info("unweave.committed",
                  description=cmd.description,
                  n_remove=len(targets), n_add=len(out),
                  crossings_before=before, crossings_after=after)
        self._status_msg(
            f"Unweave: {before} → {after} crossings"
            + (f" ({len(targets)}→{len(out)} segments)" if len(targets) != len(out) else "")
        )

    def _merge_wires_and_fragments(
        self,
        wire_ids: "frozenset[uuid.UUID] | None",
    ) -> None:
        """Two-phase wire merge — best-effort fuse + fragment coalesce.

        Phase 1 — :class:`FuseTouchingWiresCommand` collapses every
        pair of wires that share a 2-degree off-pin JointKnot and have
        matching ``insulated`` (the same merge ``save_project`` runs
        on disk).  Pin endpoints and pad junctions of 3+ wires are
        never fused.

        Phase 2 — for every wire still in the (possibly reduced)
        scope, run :func:`coalesce_same_side_fragments` to drop
        spurious same-side fragment boundaries that the wire-fuse
        phase may have introduced via cross-side collapse.

        ``wire_ids=None`` means "operate on the whole layout";
        otherwise the scope is restricted to the supplied ids (and
        phase 1 only fuses joints whose BOTH incident wires are in
        the set).  Both phases run as a single MacroCommand so undo
        rolls them back atomically.

        User-triggered only — never called from constructors or
        commit paths.  Reuses existing commands so wire identity,
        knot back-references, and undo/redo keep working.
        """
        from typing import cast
        from protoboard.domain.commands.base import Command, MacroCommand
        from protoboard.domain.commands.component_cmds import (
            FuseTouchingWiresCommand,
            ReplaceWireFragmentsCommand,
        )
        from protoboard.domain.layout.wire_utils import coalesce_same_side_fragments

        session = self._session
        if session is None:
            return

        # Phase 1 — fuse touching wires.  We execute the command in a
        # standalone fashion (not bundled into the final macro) so the
        # phase-2 pass can read the post-fuse layout state and snapshot
        # the right fragment lists for ReplaceWireFragmentsCommand.
        # If phase 1 fails partway, MacroCommand's atomic rollback in
        # the stack semantics still keeps us consistent — but only the
        # phase that's running.  To get atomic undo for both phases we
        # bundle them as a single MacroCommand after phase 1's effect
        # has been observed.
        fuse_cmd = FuseTouchingWiresCommand(wire_ids=wire_ids)
        # Dry-run by applying then immediately reverting wouldn't give
        # us the post-fuse snapshot we need; instead we apply, observe,
        # build the phase-2 sub-commands, then revert phase 1 so the
        # final MacroCommand starts from the original state.
        fuse_cmd.apply(session)
        n_fused = len(fuse_cmd._records)

        # After phase 1, derive the scope for phase 2.  Wires whose ids
        # disappeared (consumed by a fuse) drop out; wires that were
        # *produced* by a fuse get included so their fragments are
        # checked.  When no scope was supplied, just look at every wire
        # left in the layout.
        post_fuse_wire_ids: set[uuid.UUID]
        if wire_ids is None:
            post_fuse_wire_ids = {w.id for w in session.layout.wires}
        else:
            survived = {wid for wid in wire_ids if wid in session.layout.wires}
            merged_ids = {
                rec.merged_wire_id for rec in fuse_cmd._records
                if rec.merged_wire_id is not None
            }
            post_fuse_wire_ids = survived | merged_ids

        all_wires = session.layout.wires
        frag_cmds: list[Command[ComponentState]] = []
        n_fragment_wires = 0
        n_fragments_removed = 0
        for wid in post_fuse_wire_ids:
            wire = next((w for w in all_wires if w.id == wid), None)
            if wire is None:
                continue
            if len(wire.fragments) < 2:
                continue
            merged = coalesce_same_side_fragments(wire.fragments)
            if len(merged) == len(wire.fragments):
                continue
            n_fragment_wires += 1
            n_fragments_removed += len(wire.fragments) - len(merged)
            session.layout.ensure_wire_knots(wire)
            old_fragments = tuple(
                Fragment(path=list(f.path), side=f.side) for f in wire.fragments
            )
            frag_cmds.append(cast(
                Command[ComponentState],
                ReplaceWireFragmentsCommand(
                    wire_id=wire.id,
                    new_fragments=tuple(merged),
                    old_fragments=old_fragments,
                ),
            ))

        # Roll back phase 1 so the macro re-applies both phases atomically.
        fuse_cmd.revert(session)

        if n_fused == 0 and not frag_cmds:
            scope_label = "selection" if wire_ids is not None else "layout"
            self._status_msg(f"Merge wires: nothing to merge in {scope_label}.")
            return

        macro_parts: list[Command[ComponentState]] = []
        if n_fused > 0:
            macro_parts.append(cast(Command[ComponentState], fuse_cmd))
        macro_parts.extend(frag_cmds)
        if len(macro_parts) == 1:
            cmd: Command[ComponentState] = macro_parts[0]
        else:
            cmd = MacroCommand(  # type: ignore[assignment]
                macro_parts,
                description=(
                    f"Merge wires ({n_fused} fused, "
                    f"{n_fragment_wires} fragment-collapsed)"
                ),
            )
        session.stack.execute(cmd, session)
        session.layout_changed.emit()
        self._refresh_all_highlights()
        _log.info(
            "wire.merge_done",
            n_wires_fused=n_fused,
            n_fragment_wires=n_fragment_wires,
            n_fragments_removed=n_fragments_removed,
        )
        parts = []
        if n_fused:
            parts.append(f"fused {n_fused} wire pair(s)")
        if n_fragment_wires:
            parts.append(
                f"collapsed {n_fragments_removed} fragment boundaries in "
                f"{n_fragment_wires} wire(s)"
            )
        self._status_msg("Merge wires: " + "; ".join(parts) + ".")

    # ------------------------------------------------------------------
    # Wire segment editing
    # ------------------------------------------------------------------

    def _ensure_wire_ready_for_edit(self, wire_id: uuid.UUID) -> None:
        """Materialise every board-pad on a wire's path as a vertex.

        Wires laid as shortcuts (or post-simplified) may carry only their
        endpoints even though the physical conductor rests on several pads.
        Edit-mode hit-test only matches against existing vertices, so those
        intermediate pads would be unselectable.  This helper executes one
        ReplaceWireFragmentsCommand that re-inserts the missing pad vertices —
        idempotent: no command is emitted when the path is already enriched.

        Pads from BOTH sides of the board are considered — a wire passing
        geometrically over a pad that exists only on the opposite side still
        needs that node (plated-through pads in particular).  Applies to
        bare and insulated wires alike.
        """
        wire = next(
            (w for w in self._session.layout.wires if w.id == wire_id), None
        )
        if wire is None or len(wire.path) < 2:
            return
        from protoboard.domain.layout.wire_utils import (
            board_pads_for_side,
            insert_pad_nodes,
        )
        pads = (
            board_pads_for_side(self._session.board, "top")
            | board_pads_for_side(self._session.board, "bottom")
        )
        enriched = insert_pad_nodes(list(wire.path), pads)
        if enriched == list(wire.path):
            return
        cmd = _replace_wire_path_cmd(
            self._session.layout, wire, enriched, list(wire.path),
        )
        self._session.stack.execute(cmd, self._session)
        self._session.layout_changed.emit()

    def _hit_test_wire_node(self, scene_pt: QPointF) -> int | None:
        wire = self._get_selected_wire()
        if wire is None:
            return None
        self._ensure_wire_ready_for_edit(wire.id)
        wire = self._get_selected_wire()  # may have been replaced
        if wire is None:
            return None
        px, py = scene_pt.x(), scene_pt.y()
        best_i: int | None = None
        best_d = _NODE_HIT_RADIUS
        for i, pt in enumerate(wire.path):
            d = math.sqrt((px - pt.x) ** 2 + (py - pt.y) ** 2)
            if d < best_d:
                best_d = d
                best_i = i
        return best_i

    def _get_selected_wire(self) -> Wire | None:
        if self._selected_wire_id is None:
            return None
        return next(
            (w for w in self._session.layout.wires
             if w.id == self._selected_wire_id),
            None,
        )

    def _hit_test_wire_node_any(
        self, scene_pt: QPointF
    ) -> tuple[Wire | None, int | None]:
        """Hit-test wire nodes across all wires of the same net as the start node.

        Returns (wire, node_idx) of the closest node within _NODE_HIT_RADIUS,
        or (None, None) if nothing is close enough.  When a start node is already
        set, the search is limited to wires belonging to the same net.
        """
        from protoboard.gui.board_view import _build_wire_net_map

        px, py = scene_pt.x(), scene_pt.y()
        wire_net_map = _build_wire_net_map(self._session)

        # Determine which net to limit the search to (None = any wire).
        start_net_id: uuid.UUID | None = None
        if self._edit_start_wire_id is not None:
            start_net_id = wire_net_map.get(self._edit_start_wire_id)

        # Materialise board-pad vertices for every bare candidate wire so that
        # hit-test can match a pad that the wire physically passes through but
        # does not list as a vertex.  Helper is idempotent: no command emitted
        # when path is already enriched or wire is insulated.
        candidate_ids = [
            w.id for w in self._session.layout.wires
            if (start_net_id is None or wire_net_map.get(w.id) == start_net_id)
        ]
        for wid in candidate_ids:
            self._ensure_wire_ready_for_edit(wid)

        best_wire: Wire | None = None
        best_idx: int | None = None
        best_d = _NODE_HIT_RADIUS

        for wire in self._session.layout.wires:
            if start_net_id is not None and wire_net_map.get(wire.id) != start_net_id:
                continue
            for i, pt in enumerate(wire.path):
                d = math.sqrt((px - pt.x) ** 2 + (py - pt.y) ** 2)
                if d < best_d:
                    best_d = d
                    best_wire = wire
                    best_idx = i

        if best_wire is None:
            # Fallback: allow any wire when no net context yet.
            if start_net_id is None:
                for wire in self._session.layout.wires:
                    for i, pt in enumerate(wire.path):
                        d = math.sqrt((px - pt.x) ** 2 + (py - pt.y) ** 2)
                        if d < best_d:
                            best_d = d
                            best_wire = wire
                            best_idx = i

        return best_wire, best_idx

    def _net_wires_ordered(
        self,
        start_wire_id: uuid.UUID,
        end_wire_id: uuid.UUID,
        net_id: uuid.UUID,
    ) -> list[Wire] | None:
        """BFS through wires of *net_id* from start_wire to end_wire.

        Two wires are adjacent if any point of one wire lies within 1e-4 of any
        point of the other (shared pad / endpoint).

        Returns ordered list [start_wire, ..., end_wire] or None when not connected.
        """
        from protoboard.gui.board_view import _build_wire_net_map

        wire_net_map = _build_wire_net_map(self._session)
        net_wires = [
            w for w in self._session.layout.wires
            if wire_net_map.get(w.id) == net_id
        ]
        wire_by_id = {w.id: w for w in net_wires}

        if start_wire_id not in wire_by_id or end_wire_id not in wire_by_id:
            return None
        if start_wire_id == end_wire_id:
            return [wire_by_id[start_wire_id]]

        _EPS = 1e-4

        def _shares_point(a: Wire, b: Wire) -> bool:
            for pa in a.path:
                for pb in b.path:
                    if abs(pa.x - pb.x) < _EPS and abs(pa.y - pb.y) < _EPS:
                        return True
            return False

        # Build adjacency list.
        adj: dict[uuid.UUID, list[uuid.UUID]] = {w.id: [] for w in net_wires}
        for i, wa in enumerate(net_wires):
            for wb in net_wires[i + 1:]:
                if _shares_point(wa, wb):
                    adj[wa.id].append(wb.id)
                    adj[wb.id].append(wa.id)

        # BFS from start to end — record predecessors.
        from collections import deque
        pred: dict[uuid.UUID, uuid.UUID | None] = {start_wire_id: None}
        queue: deque[uuid.UUID] = deque([start_wire_id])
        while queue:
            cur = queue.popleft()
            if cur == end_wire_id:
                break
            for nxt in adj[cur]:
                if nxt not in pred:
                    pred[nxt] = cur
                    queue.append(nxt)

        if end_wire_id not in pred:
            return None

        # Reconstruct path.
        path: list[uuid.UUID] = []
        cur_id: uuid.UUID | None = end_wire_id
        while cur_id is not None:
            path.append(cur_id)
            cur_id = pred[cur_id]
        path.reverse()
        return [wire_by_id[wid] for wid in path]

    @staticmethod
    def _outer_endpoint_idx(
        wire: Wire, neighbour: Wire
    ) -> int:
        """Return 0 or len(path)-1: whichever endpoint of *wire* is NOT shared
        with *neighbour*.

        A wire in a chain connects to its neighbour at one of its two
        endpoints; the other endpoint is the "outer" side that must be
        preserved when excising an inner fragment.  Falls back to 0 when
        both endpoints touch the neighbour (degenerate chain) or neither
        does (caller error, shouldn't happen if ordered[] is a real chain).
        """
        _EPS = 1e-4
        first = wire.path[0]
        last = wire.path[-1]

        def _touches(p: Point) -> bool:
            for q in neighbour.path:
                if abs(p.x - q.x) < _EPS and abs(p.y - q.y) < _EPS:
                    return True
            return False

        first_shared = _touches(first)
        last_shared = _touches(last)
        # The outer end is the one NOT shared.  If only one is shared, the
        # other is unambiguously outer.  If both or neither are shared, fall
        # back to 0 — the caller can't disambiguate.
        if last_shared and not first_shared:
            return 0
        if first_shared and not last_shared:
            return len(wire.path) - 1
        return 0

    def _excise_cross_wire_range(
        self,
    ) -> tuple[object, Point, Point] | None:
        """Build a MacroCommand that excises the selected cross-wire fragment.

        Returns (macro_cmd, start_pt, end_pt) where start_pt and end_pt are the
        physical points at the edit boundaries (useful for re-routing afterwards).
        Returns None when the range is invalid or wires are not connected.

        Each wire in the chain has two endpoints: the *outer* one (which is
        not shared with the next wire in the chain) and the *inner* one
        (the junction point).  The fragment to keep on the start/end wires
        is always the sub-path from the outer endpoint to the edit index;
        all wires strictly between start and end are removed entirely.
        Picking ``path[:start_idx+1]`` unconditionally — as the previous
        implementation did — only happens to be right when ``path[0]`` is
        the outer endpoint, which is not guaranteed for the start wire.
        """
        from protoboard.gui.board_view import _build_wire_net_map

        start_wire_id = self._edit_start_wire_id
        end_wire_id = self._edit_end_wire_id
        start_idx = self._edit_start_idx
        end_idx = self._edit_end_idx

        if (start_wire_id is None or end_wire_id is None
                or start_idx is None or end_idx is None):
            return None

        wire_net_map = _build_wire_net_map(self._session)
        net_id = wire_net_map.get(start_wire_id)

        ordered = self._net_wires_ordered(start_wire_id, end_wire_id, net_id)  # type: ignore[arg-type]
        if ordered is None:
            self._status_msg("Cannot excise: nodes are not connected within the net.")
            return None

        start_wire = ordered[0]
        end_wire = ordered[-1]
        start_pt = start_wire.path[start_idx]
        end_pt = end_wire.path[end_idx]

        cmds: list[object] = []

        def _keep_fragment(
            wire: Wire, edit_idx: int, outer_idx: int,
        ) -> None:
            """Emit commands that keep the path slice from outer_idx to edit_idx.

            The slice moves the *inner* endpoint to ``path[edit_idx]``.  The
            wire's existing inner knot still points at the original endpoint,
            so a plain ``replace_wire_fragments`` would fail (PinKnot can't
            slide, shared JointKnot can't drag other wires).  Use
            Remove + AddWire instead so the new wire gets fresh knots
            resolved at its actual endpoints.
            """
            from protoboard.domain.commands.component_cmds import AddWireCommand
            from protoboard.domain.layout.wire import Wire as _W
            if edit_idx == outer_idx:
                # Edit lies on the outer endpoint — nothing left to keep.
                cmds.append(RemoveWireCommand(wire_id=wire.id))
                return
            if outer_idx == 0:
                new_path = list(wire.path[:edit_idx + 1])
            else:
                # outer_idx == last
                new_path = list(wire.path[edit_idx:])
            if len(new_path) < 2:
                cmds.append(RemoveWireCommand(wire_id=wire.id))
                return
            cmds.append(RemoveWireCommand(wire_id=wire.id))
            cmds.append(AddWireCommand(wire=_W(
                id=uuid.uuid4(),
                path=new_path,
                insulated=wire.insulated,
                side=wire.side,
                color=wire.color,
            )))

        # --- Start wire: keep fragment from its outer endpoint to start_idx ---
        if len(ordered) == 1:
            # Single-wire excise is handled by a separate code path (the
            # caller dispatches on `cross` before reaching here).  Use the
            # same Remove + AddWire approach as _keep_fragment so endpoint
            # knot resolution stays consistent.
            from protoboard.domain.commands.component_cmds import AddWireCommand
            from protoboard.domain.layout.wire import Wire as _W
            new_path = list(start_wire.path[:start_idx + 1])
            if len(new_path) < 2:
                cmds.append(RemoveWireCommand(wire_id=start_wire.id))
            else:
                cmds.append(RemoveWireCommand(wire_id=start_wire.id))
                cmds.append(AddWireCommand(wire=_W(
                    id=uuid.uuid4(),
                    path=new_path,
                    insulated=start_wire.insulated,
                    side=start_wire.side,
                    color=start_wire.color,
                )))
        else:
            start_outer = self._outer_endpoint_idx(start_wire, ordered[1])
            _keep_fragment(start_wire, start_idx, start_outer)

        # --- Intermediate wires: remove entirely ---
        for mid_wire in ordered[1:-1]:
            cmds.append(RemoveWireCommand(wire_id=mid_wire.id))

        # --- End wire: keep fragment from end_idx to its outer endpoint ---
        if len(ordered) > 1:
            end_outer = self._outer_endpoint_idx(end_wire, ordered[-2])
            _keep_fragment(end_wire, end_idx, end_outer)

        macro = MacroCommand(cmds, description="Excise wire fragment")  # type: ignore[arg-type]
        return macro, start_pt, end_pt

    def _draw_cross_wire_highlight(self) -> None:
        """Draw wire node highlights for a cross-wire edit selection.

        Shows nodes on start wire and end wire; marks start/end nodes red.
        If wires are connected, also dims intermediate wires.
        """
        # Remove existing primary highlight.
        if self._wire_highlight is not None:
            self._view.scene().removeItem(self._wire_highlight)
            self._wire_highlight = None

        start_wire_id = self._edit_start_wire_id
        end_wire_id = self._edit_end_wire_id
        if start_wire_id is None:
            return

        from protoboard.gui.layer_state import Z_SELECTION

        def _make_wire_group(wire: Wire, start_i: int | None, end_i: int | None) -> QGraphicsItemGroup:
            group = QGraphicsItemGroup()
            pts = wire.path
            for i in range(len(pts) - 1):
                seg = QGraphicsLineItem(pts[i].x, pts[i].y, pts[i + 1].x, pts[i + 1].y)
                seg.setPen(_WIRE_SELECT_PEN)
                group.addToGroup(seg)
            for i, pt in enumerate(pts):
                r = _NODE_RADIUS
                is_sel = (i == start_i or i == end_i)
                dot = QGraphicsEllipseItem(pt.x - r, pt.y - r, 2 * r, 2 * r)
                dot.setPen(_NODE_SELECTED_PEN if is_sel else _NODE_PEN)
                dot.setBrush(_NODE_SELECTED_BRUSH if is_sel else _NODE_BRUSH)
                group.addToGroup(dot)
            group.setZValue(Z_SELECTION)
            return group

        if end_wire_id is None or end_wire_id == start_wire_id:
            # Single-wire selection — use standard highlight.
            self._draw_wire_highlight(start_wire_id)
            return

        # Two different wires — draw both.
        start_wire = next((w for w in self._session.layout.wires if w.id == start_wire_id), None)
        end_wire = next((w for w in self._session.layout.wires if w.id == end_wire_id), None)

        group_start = _make_wire_group(start_wire, self._edit_start_idx, None) if start_wire else None
        group_end = _make_wire_group(end_wire, None, self._edit_end_idx) if end_wire else None

        if group_start is not None:
            self._view.scene().addItem(group_start)
            self._wire_highlight = group_start  # keep primary reference for cleanup

        if group_end is not None:
            self._view.scene().addItem(group_end)
            self._wire_multi_highlights.append(group_end)

    def _start_wire_edit(self, node_idx: int) -> None:
        self._cancel_wire_edit()
        self._edit_start_wire_id = self._selected_wire_id
        self._edit_start_idx = node_idx
        self._edit_end_idx = None
        self._edit_points = []
        if self._selected_wire_id is not None:
            self._draw_wire_highlight(self._selected_wire_id)

    def _set_edit_end(self, node_idx: int, wire: Wire) -> None:
        start = self._edit_start_idx
        if start is None:
            return
        same_wire = (wire.id == self._edit_start_wire_id)
        if same_wire and node_idx == start:
            return
        if same_wire and node_idx < start:
            start, node_idx = node_idx, start
            self._edit_start_idx = start
        self._edit_end_wire_id = wire.id
        self._edit_end_idx = node_idx
        self._edit_points = []
        self._draw_cross_wire_highlight()

    def _is_editing_wire(self) -> bool:
        return (
            self._edit_start_idx is not None
            and self._edit_end_idx is not None
        )

    def _edit_add_point(self, pt: Point) -> None:
        self._edit_points.append(pt)
        wire = self._get_selected_wire()
        if len(self._edit_points) >= 2:
            a = self._edit_points[-2]
            b = self._edit_points[-1]
        else:
            if wire is None or self._edit_start_idx is None:
                return
            a = wire.path[self._edit_start_idx]
            b = self._edit_points[0]
        line = QGraphicsLineItem(a.x, a.y, b.x, b.y)
        line.setPen(_EDIT_PREVIEW_PEN)
        self._view.scene().addItem(line)
        self._edit_preview_items.append(line)
        self._remove_edit_live()

    def _update_edit_live(self, scene_pt: QPointF) -> None:
        self._remove_edit_live()
        wire = self._get_selected_wire()
        if wire is None or self._edit_start_idx is None:
            return
        snapped = self._view.snap(scene_pt)
        anchor = (
            self._edit_points[-1] if self._edit_points
            else wire.path[self._edit_start_idx]
        )
        line = QGraphicsLineItem(
            anchor.x, anchor.y, snapped.x, snapped.y
        )
        line.setPen(_EDIT_PREVIEW_PEN)
        self._view.scene().addItem(line)
        self._edit_live_line = line

    def _remove_edit_live(self) -> None:
        if self._edit_live_line is not None:
            self._view.scene().removeItem(self._edit_live_line)
            self._edit_live_line = None

    def _clear_edit_preview(self) -> None:
        for item in self._edit_preview_items:
            self._view.scene().removeItem(item)
        self._edit_preview_items.clear()
        self._remove_edit_live()

    def _cancel_wire_edit(self) -> None:
        self._clear_edit_preview()
        self._edit_start_wire_id = None
        self._edit_end_wire_id = None
        self._edit_start_idx = None
        self._edit_end_idx = None
        self._edit_points = []

    def _commit_wire_edit(self) -> None:
        start = self._edit_start_idx
        end = self._edit_end_idx
        if start is None or end is None:
            return

        cross = (
            self._edit_start_wire_id is not None
            and self._edit_end_wire_id is not None
            and self._edit_start_wire_id != self._edit_end_wire_id
        )

        if cross:
            # Cross-wire: excise the range then stitch in the hand-drawn points.
            from protoboard.domain.commands.component_cmds import AddWireCommand
            from protoboard.domain.layout.wire import Wire as WS

            # Resolve side before cancel_wire_edit clears the IDs.
            _sw = next(
                (w for w in self._session.layout.wires
                 if w.id == self._edit_start_wire_id),
                None,
            ) if self._edit_start_wire_id else None
            _side = _sw.side if _sw else "top"

            excise_result = self._excise_cross_wire_range()
            if excise_result is None:
                return
            excise_macro, start_pt, end_pt = excise_result
            new_path = _dedupe_adjacent([start_pt] + list(self._edit_points) + [end_pt])
            self._clear_edit_preview()
            self._cancel_wire_edit()
            if len(new_path) < 2:
                self._session.stack.execute(excise_macro, self._session)
            else:
                new_wire = WS(id=uuid.uuid4(), path=new_path,
                              insulated=True, side=_side)  # type: ignore[arg-type]
                add_cmd = AddWireCommand(wire=new_wire)
                macro = MacroCommand([excise_macro, add_cmd], description="Reroute wire fragment")  # type: ignore[arg-type]
                self._session.stack.execute(macro, self._session)
            self._deselect_wire()
            self._session.layout_changed.emit()
            return

        # Same-wire path (original behaviour).
        wire = self._get_selected_wire()
        if wire is None:
            return
        prefix = list(wire.path[:start + 1])
        suffix = list(wire.path[end:])
        new_path = _dedupe_adjacent(
            prefix + list(self._edit_points) + suffix
        )
        if len(new_path) < 2:
            self._cancel_wire_edit()
            return
        self._clear_edit_preview()
        self._cancel_wire_edit()
        cmd = _replace_wire_path_cmd(
            self._session.layout, wire, new_path, list(wire.path),
        )
        self._session.stack.execute(cmd, self._session)
        self._session.layout_changed.emit()
        self._select_wire(wire.id)

    def _flip_wire_fragment(self, target_side: str | None = None) -> None:
        """Flip / move the selected wire fragment to a target layer.

        Args:
            target_side: "top" or "bottom" to force a layer; None toggles
                the opposite of the wire's current side.

        Prerequisites:
        - A single wire is selected (_selected_wire_id set).
        - A fragment is marked (_edit_start_idx and _edit_end_idx both set).
        - The fragment endpoints must lie on plated-through pads (vias) when
          the fragment does not span the whole wire — otherwise layer change is
          physically impossible.
        - No pads occupied by a different net on the target layer.
        - No existing wire on the target layer shares interior points with the
          fragment.

        On success executes FlipWireFragmentCommand (full undo/redo).
        On failure shows a status-bar message and does nothing.
        """
        from PySide6.QtWidgets import QMessageBox
        from protoboard.domain.commands.component_cmds import FlipWireFragmentCommand
        from protoboard.domain.layout.wire_utils import (
            board_pads_for_side,
            plated_through_pads,
        )

        wire = self._get_selected_wire()
        if wire is None or self._edit_start_idx is None or self._edit_end_idx is None:
            return

        if (
            self._edit_start_wire_id is not None
            and self._edit_end_wire_id is not None
            and self._edit_start_wire_id != self._edit_end_wire_id
        ):
            self._status_msg(
                "Cannot flip cross-wire fragment — select a fragment within a single wire."
            )
            return

        start = self._edit_start_idx
        end = self._edit_end_idx
        path = wire.path
        if target_side is None:
            resolved_side: str = "bottom" if wire.side == "top" else "top"
        else:
            if target_side == wire.side:
                self._status_msg(f"Wire fragment already on {target_side} layer.")
                return
            resolved_side = target_side
        fragment = path[start:end + 1]
        is_whole_wire = (start == 0 and end == len(path) - 1)

        session = self._session

        # --- Check 1: endpoints must be plated-through if not whole wire ---
        if not is_whole_wire:
            vias = plated_through_pads(session.board)
            for idx, label in ((start, "start"), (end, "end")):
                pt = path[idx]
                if pt not in vias:
                    self._status_msg(
                        f"Cannot flip: {label} point ({pt.x:.1f},{pt.y:.1f}) "
                        f"is not a plated-through pad (via required for layer change)."
                    )
                    return

        # --- Check 2: all fragment points must exist on resolved_side ---
        target_pads = board_pads_for_side(session.board, resolved_side)
        for pt in fragment:
            if pt not in target_pads:
                self._status_msg(
                    f"Cannot flip: pad ({pt.x:.1f},{pt.y:.1f}) "
                    f"is not available on {resolved_side} layer."
                )
                return

        # --- Check 3: no pin of another net on target_side ---
        # Build (Point, side) → net_id from placed component pins
        pin_net: dict[tuple[float, float], "uuid.UUID | None"] = {}
        for net in session.schematic.nets.values():
            for ref in net.pins:
                wp = session.get_pin_position(ref.component_id, ref.pin_number)
                if wp is None:
                    continue
                pin_net[(wp.x, wp.y)] = net.id

        # Determine which net this wire belongs to (if any)
        wire_net_id: "uuid.UUID | None" = None
        for net in session.schematic.nets.values():
            for ref in net.pins:
                wp = session.get_pin_position(ref.component_id, ref.pin_number)
                if wp is None:
                    continue
                if any(abs(wp.x - p.x) < 1e-4 and abs(wp.y - p.y) < 1e-4
                       for p in wire.path):
                    wire_net_id = net.id
                    break
            if wire_net_id is not None:
                break

        for pt in fragment:
            nid = pin_net.get((pt.x, pt.y))
            if nid is not None and nid != wire_net_id:
                self._status_msg(
                    f"Cannot flip: pad ({pt.x:.1f},{pt.y:.1f}) is occupied "
                    f"by a different net on {resolved_side} layer."
                )
                return

        # --- Check 4: no existing wire on resolved_side overlaps interior points ---
        interior = set(fragment[1:-1])
        if interior:
            for other in session.layout.wires:
                if other.id == wire.id or other.side != resolved_side:
                    continue
                other_pts = set(other.path)
                overlap = interior & other_pts
                if overlap:
                    pt = next(iter(overlap))
                    self._status_msg(
                        f"Cannot flip: interior point ({pt.x:.1f},{pt.y:.1f}) "
                        f"is already occupied by another wire on {resolved_side}."
                    )
                    return

        # --- All checks passed — execute ---
        cmd = FlipWireFragmentCommand(
            wire_id=wire.id,
            start_idx=start,
            end_idx=end,
            target_side=resolved_side,
        )
        self._cancel_wire_edit()
        self._session.stack.execute(cmd, self._session)
        self._session.layout_changed.emit()
        self._status_msg(f"Fragment flipped to {resolved_side} layer.")

    def _move_selected_wires_to_side(self, target_side: str | None) -> None:
        """Move all selected wires to a target layer (Ctrl+T/Ctrl+B/Ctrl+F).

        Args:
            target_side: "top" or "bottom" to force a layer; None toggles
                — requires all selected wires to share the same current side.
        """
        from typing import cast
        from protoboard.domain.commands.base import Command, MacroCommand
        from protoboard.domain.commands.component_cmds import (
            ComponentState,
            SetWireSideCommand,
        )

        wire_ids: set[uuid.UUID] = set(self._selected_wire_ids)
        if self._selected_wire_id is not None:
            wire_ids.add(self._selected_wire_id)
        if not wire_ids:
            return

        wires = [w for w in self._session.layout.wires if w.id in wire_ids]
        if not wires:
            return

        if target_side is None:
            sides = {w.side for w in wires}
            if len(sides) > 1:
                self._status_msg(
                    "Cannot flip: selected wires are on different layers."
                )
                return
            current_side = next(iter(sides))
            resolved_side = "bottom" if current_side == "top" else "top"
        else:
            resolved_side = target_side

        wires_to_move = [w for w in wires if w.side != resolved_side]
        if not wires_to_move:
            self._status_msg(f"Selected wires already on {resolved_side} layer.")
            return

        raw = [
            cast(Command[ComponentState], SetWireSideCommand(wire_id=w.id, side=resolved_side))
            for w in wires_to_move
        ]
        cmd: Command[ComponentState] = (
            raw[0] if len(raw) == 1
            else MacroCommand(raw, description=f"Move {len(raw)} wires to {resolved_side}")
        )
        self._session.stack.execute(cmd, self._session)
        self._session.layout_changed.emit()
        self._status_msg(f"Moved {len(wires_to_move)} wire(s) to {resolved_side} layer.")

    def _delete_wire_fragment(self) -> None:
        """Delete the selected fragment from the wire(s).

        Cross-wire: excises the range across multiple wire segments (MacroCommand).
        Same-wire: if whole wire → RemoveWireCommand; else → ReplaceWireFragmentsCommand.
        """
        if self._edit_start_idx is None or self._edit_end_idx is None:
            return

        cross = (
            self._edit_start_wire_id is not None
            and self._edit_end_wire_id is not None
            and self._edit_start_wire_id != self._edit_end_wire_id
        )

        if cross:
            excise_result = self._excise_cross_wire_range()
            if excise_result is None:
                return
            excise_macro, _sp, _ep = excise_result
            self._cancel_wire_edit()
            self._session.stack.execute(excise_macro, self._session)
            self._deselect_wire()
            self._session.layout_changed.emit()
            return

        # Same-wire path — cut the wire into two disjoint pieces.
        wire = self._get_selected_wire()
        if wire is None:
            return

        start = self._edit_start_idx
        end   = self._edit_end_idx
        path  = wire.path
        n = len(path)
        is_whole = (start == 0 and end == n - 1)

        self._cancel_wire_edit()

        if is_whole:
            cmd: object = RemoveWireCommand(wire_id=wire.id)
            self._session.stack.execute(cmd, self._session)  # type: ignore[arg-type]
            self._deselect_wire()
            self._session.layout_changed.emit()
            return

        # Two halves: path[0..start] (left piece, ending AT the start pad) and
        # path[end..n-1] (right piece, starting AT the end pad).  The selected
        # boundary pads (start, end) are preserved on their respective halves
        # as endpoints — only the interior path[start+1..end-1] is removed.
        # The wire is physically cut into two segments that meet (visually) at
        # the boundary pads; through-pad galvanic contact still depends on
        # whether those pads are plated-through.
        left_path = list(path[: start + 1])
        right_path = list(path[end:])

        from typing import cast
        from protoboard.domain.commands.base import Command, MacroCommand
        from protoboard.domain.commands.component_cmds import (
            AddWireCommand,
            ComponentState,
        )
        from protoboard.domain.layout.wire import Wire

        cmds: list[Command[ComponentState]] = [
            cast(Command[ComponentState], RemoveWireCommand(wire_id=wire.id))
        ]
        if len(left_path) >= 2:
            cmds.append(cast(Command[ComponentState], AddWireCommand(
                wire=Wire(
                    id=uuid.uuid4(),
                    path=left_path,
                    insulated=wire.insulated,
                    side=wire.side,
                    color=wire.color,
                )
            )))
        if len(right_path) >= 2:
            cmds.append(cast(Command[ComponentState], AddWireCommand(
                wire=Wire(
                    id=uuid.uuid4(),
                    path=right_path,
                    insulated=wire.insulated,
                    side=wire.side,
                    color=wire.color,
                )
            )))
        macro: Command[ComponentState] = (
            cmds[0] if len(cmds) == 1
            else MacroCommand(cmds, description="Cut wire fragment")
        )
        self._session.stack.execute(macro, self._session)
        self._deselect_wire()
        self._session.layout_changed.emit()

    def _status_msg(self, msg: str, timeout_ms: int = 4000) -> None:
        """Show a message in the main window status bar."""
        win = self._view.window()
        status = getattr(win, "_status", None)
        if status is not None:
            status.showMessage(msg, timeout_ms)

    def _autoroute_wire_fragment(self) -> None:
        start = self._edit_start_idx
        end = self._edit_end_idx
        if start is None or end is None:
            return

        from protoboard.domain.connectivity.graph import ConnectivityGraph
        from protoboard.services.routing.astar import find_paths
        from protoboard.services.routing.cost_model import RoutingCostModel

        cross = (
            self._edit_start_wire_id is not None
            and self._edit_end_wire_id is not None
            and self._edit_start_wire_id != self._edit_end_wire_id
        )

        if cross:
            from protoboard.domain.commands.component_cmds import AddWireCommand
            from protoboard.domain.layout.wire import Wire as WS

            excise_result = self._excise_cross_wire_range()
            if excise_result is None:
                return
            excise_macro, start_pt, end_pt = excise_result

            # Determine side from start wire.
            start_wire = next(
                (w for w in self._session.layout.wires
                 if w.id == self._edit_start_wire_id),
                None,
            ) if self._edit_start_wire_id else None
            side = start_wire.side if start_wire else "top"

            self._cancel_wire_edit()

            session = self._session
            graph = ConnectivityGraph(
                board=session.board,
                layout=session.layout,
                components=session.components,
                instances=session.instances,
            )
            results = find_paths(
                graph=graph,
                source=start_pt,
                targets=[end_pt],
                side=side,
                cost_model=RoutingCostModel(),
                k=1,
            )
            if results:
                routed = [pt for pt, _ in results[0].path]
                new_wire = WS(id=uuid.uuid4(), path=routed,
                              insulated=True, side=side)  # type: ignore[arg-type]
                add_cmd = AddWireCommand(wire=new_wire)
                macro = MacroCommand([excise_macro, add_cmd], description="Autoroute wire fragment")  # type: ignore[arg-type]
                self._session.stack.execute(macro, self._session)
            else:
                # No path found — just excise without reconnecting.
                self._session.stack.execute(excise_macro, self._session)
                self._status_msg("No path found — fragment removed without rerouting.")
            self._deselect_wire()
            self._session.layout_changed.emit()
            return

        # Same-wire path (original behaviour).
        wire = self._get_selected_wire()
        if wire is None:
            return
        session = self._session
        graph = ConnectivityGraph(
            board=session.board,
            layout=session.layout,
            components=session.components,
            instances=session.instances,
        )
        results = find_paths(
            graph=graph,
            source=wire.path[start],
            targets=[wire.path[end]],
            side=wire.side,
            cost_model=RoutingCostModel(),
            k=1,
        )
        if not results:
            return
        routed = [pt for pt, _ in results[0].path]
        middle = list(routed[1:-1])
        new_path = _dedupe_adjacent(
            list(wire.path[:start + 1]) + middle + list(wire.path[end:])
        )
        if len(new_path) < 2:
            self._cancel_wire_edit()
            return
        self._clear_edit_preview()
        self._cancel_wire_edit()
        cmd = _replace_wire_path_cmd(
            self._session.layout, wire, new_path, list(wire.path),
        )
        self._session.stack.execute(cmd, self._session)
        self._session.layout_changed.emit()
        self._select_wire(wire.id)

    # ------------------------------------------------------------------
    # Group navigation helpers
    # ------------------------------------------------------------------

    def _click_is_on_exported_pin(
        self, scene_pt: QPointF, tol: float = 0.6,
    ) -> bool:
        """Return True if *scene_pt* lies on an exported pin of any
        child group of the current active group.

        Mirrors :meth:`BoardView.is_locked_pin` but inverted: it asks
        "is this pin part of a child group's *external interface*?"
        rather than "is this pin locked away?".  Used by on_press to
        preserve normal pin semantics (route start, Shift+LPM net
        highlight) on exported pins even though they sit inside their
        parent group's outline rect.
        """
        from protoboard.domain.layout.group import all_component_ids
        groups = self._session.layout.groups
        active_group = groups.get(self._current_group_id)
        if active_group is None:
            return False
        exported: set[tuple[uuid.UUID, int]] = set()
        member_ids: set[uuid.UUID] = set()
        for child_gid in active_group.child_group_ids:
            child_group = groups.get(child_gid)
            if child_group is None:
                continue
            for cid in all_component_ids(groups, child_gid):
                member_ids.add(cid)
            for ep in child_group.exported_pins:
                exported.add((ep.component_id, ep.pin_number))
        if not exported:
            return False
        px, py = scene_pt.x(), scene_pt.y()
        tol_sq = tol * tol
        for comp_id in member_ids:
            for pin_num, p in self._session.iter_pin_positions(comp_id):
                if (comp_id, pin_num) not in exported:
                    continue
                dx = px - p.x
                dy = py - p.y
                if dx * dx + dy * dy < tol_sq:
                    return True
        return False

    def _hit_test_group_outline(self, scene_pt: QPointF) -> uuid.UUID | None:
        """Return group_id if the click landed on a group outline rect item.

        Multiple outlines can overlap at the click point (sibling groups
        whose bounding rects touch / nest).  Pick the outline with the
        *smallest* bounding rect — that is the most specific match and
        matches the user's intuition ("I clicked on the inner group").
        Without this disambiguation Qt's scene.items() Z-order would
        sometimes return the outer group instead, leaving the inner
        group "uwięziona" — outline-grabbed by its parent so drag
        never starts on it (user-observed 2026-06-01).
        """
        from PySide6.QtWidgets import QGraphicsRectItem
        groups = self._session.layout.groups
        active = groups.get(self._current_group_id)
        candidates: list[tuple[float, uuid.UUID]] = []
        # Strict hit-test: only items returned by scene.items(scene_pt).
        for item in self._view.scene().items(scene_pt):
            tag = item.data(1) if hasattr(item, "data") else None
            if tag != "group_outline":
                continue
            raw = item.data(0)
            if not raw:
                continue
            gid = uuid.UUID(str(raw))
            if not (active and gid in active.child_group_ids):
                continue
            try:
                br = item.sceneBoundingRect()
                area = float(br.width()) * float(br.height())
            except Exception:  # noqa: BLE001
                area = float("inf")
            candidates.append((area, gid))
        if candidates:
            candidates.sort(key=lambda t: t[0])
            chosen = candidates[0][1]
            _log.debug(
                "hit_test_group_outline.chosen",
                scene_pt=(round(scene_pt.x(), 2), round(scene_pt.y(), 2)),
                chosen=str(chosen)[:8],
                n_candidates=len(candidates),
            )
            return chosen
        # Strict scene.items() returned no outline candidate.  Fall back
        # to scanning *all* outline rects in the scene and matching by
        # geometric containment.  Qt's QGraphicsRectItem shape includes
        # the rect's interior, so scene.items(point) should normally
        # find it — but if a child item (e.g. a transparent group fill)
        # has captured the hit-test or the rect's Z-order pushed it
        # below something that swallows the event, fall through here so
        # the click still works.  Diagnostic log lists every outline
        # whose rect contains the point so a future "drag won't start"
        # report can pinpoint whether the click was in or out of the
        # group's bounding box.
        px, py = scene_pt.x(), scene_pt.y()
        fallback: list[tuple[float, uuid.UUID, tuple[float, float, float, float]]] = []
        for item in self._view.scene().items():
            if not isinstance(item, QGraphicsRectItem):
                continue
            tag = item.data(1) if hasattr(item, "data") else None
            if tag != "group_outline":
                continue
            raw = item.data(0)
            if not raw:
                continue
            try:
                gid = uuid.UUID(str(raw))
            except (ValueError, TypeError):
                continue
            if not (active and gid in active.child_group_ids):
                continue
            try:
                br = item.sceneBoundingRect()
            except Exception:  # noqa: BLE001
                continue
            if (br.x() <= px <= br.x() + br.width()
                    and br.y() <= py <= br.y() + br.height()):
                fallback.append(
                    (
                        float(br.width()) * float(br.height()),
                        gid,
                        (
                            round(br.x(), 2), round(br.y(), 2),
                            round(br.x() + br.width(), 2),
                            round(br.y() + br.height(), 2),
                        ),
                    )
                )
        if not fallback:
            _log.debug(
                "hit_test_group_outline.empty",
                scene_pt=(round(scene_pt.x(), 2), round(scene_pt.y(), 2)),
                active_group=str(self._current_group_id)[:8],
            )
            return None
        fallback.sort(key=lambda t: t[0])
        chosen = fallback[0][1]
        _log.info(
            "hit_test_group_outline.fallback_geometric",
            scene_pt=(round(scene_pt.x(), 2), round(scene_pt.y(), 2)),
            chosen=str(chosen)[:8],
            n_candidates=len(fallback),
            candidates=[
                (str(g)[:8], bbox) for _a, g, bbox in fallback
            ],
        )
        return chosen

    def _resolve_group_hit(self, comp_id: uuid.UUID) -> uuid.UUID:
        """If comp_id is inside a child group not yet entered, return the first
        component ID from that child group as a proxy for group-level selection.
        Returns comp_id unchanged if it's a direct member at the current depth."""
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        groups = self._session.layout.groups
        current = self._current_group_id
        g = groups.get(current)
        # Check if comp_id is directly in a child group — redirect to proxy.
        for child_id in (g.child_group_ids if g else frozenset()):
            if comp_id in all_component_ids(groups, child_id):
                child_g = groups.get(child_id)
                if child_g and child_g.component_ids:
                    return next(iter(child_g.component_ids))
        return comp_id

    def _current_group_component_ids(self) -> frozenset[uuid.UUID]:
        """Return the set of all component IDs accessible at the current group depth."""
        groups = self._session.layout.groups
        gid = self._current_group_id
        g = groups.get(gid)
        if g is None:
            return frozenset()
        return all_component_ids(groups, gid)

    def _is_accessible(self, comp_id: uuid.UUID) -> bool:
        """True if comp_id can be interacted with at the current group depth.

        Root group is special: its direct members are all components NOT inside
        any child group (components float freely at root level).
        Non-root groups: component must be in component_ids or a child group.
        """
        if not self._group_stack:
            return True
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        groups = self._session.layout.groups
        active_gid = self._current_group_id
        active = groups.get(active_gid)
        if active is None:
            return True  # no group info — allow all

        if active_gid == ROOT_GROUP_ID:
            # At root level: accessible if NOT inside any child group of root.
            for child_gid in active.child_group_ids:
                if comp_id in all_component_ids(groups, child_gid):
                    return True  # in a child group — selectable as proxy
            # Not in any child group = free component at root level.
            return True

        # Inside a non-root group: must be a direct member or in a child group.
        if comp_id in active.component_ids:
            return True
        for child_gid in active.child_group_ids:
            if comp_id in all_component_ids(groups, child_gid):
                return True
        _log.debug("selection.blocked", comp_id=str(comp_id), active_group=str(active_gid))
        return False

    def _group_for_selection(self) -> uuid.UUID | None:
        """Group the current selection: show dialog to create new or add to existing."""
        ids = frozenset(self._selected_ids)
        if self._selected_id and self._selected_id not in ids:
            ids = ids | {self._selected_id}
        if not ids:
            return None

        from protoboard.domain.commands.group_cmds import (
            AddToGroupCommand,
            CreateGroupCommand,
            RemoveFromGroupCommand,
            SetExportedPinsCommand,
        )
        from protoboard.domain.layout.group import (
            ComponentGroup,
            all_component_ids,
            external_pins_for_group,
            parent_group_of,
            promote_selection_to_top_level,
        )
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        from protoboard.gui.dialogs.group_target_dialog import GroupTargetDialog

        # Collect groups at the current level (child groups of active group),
        # excluding root.
        groups = self._session.layout.groups
        active_gid = self._current_group_id
        active = groups.get(active_gid)
        child_groups = [
            groups[gid]
            for gid in (active.child_group_ids if active else frozenset())
            if gid in groups and gid != ROOT_GROUP_ID
        ]

        default_name = f"Group{len(groups) + 1}"
        dlg = GroupTargetDialog(
            existing_groups=child_groups,
            default_name=default_name,
            parent=self._view.window(),  # type: ignore[arg-type]
        )
        if dlg.exec() != GroupTargetDialog.DialogCode.Accepted:
            return None

        if dlg.mode == "new":
            # Selection-promotion: any selected component nested inside
            # a child group of the active context is "promoted" to its
            # top-level structure under active.  The whole group then
            # joins the new group as a sub-group (not as flattened
            # directs), preserving hierarchy.  Free / direct-of-active
            # components stay as directs.  Rule confirmed 2026-06-04:
            # one selected member ⇒ whole group treated as a unit.
            promoted_free, promoted_groups = promote_selection_to_top_level(
                self._session.layout.groups, active_gid, ids
            )
            _log.info(
                "group_for_selection.promotion",
                active_gid=str(active_gid)[:8],
                n_selected=len(ids),
                n_free_after=len(promoted_free),
                n_promoted_groups=len(promoted_groups),
                promoted_group_ids=[str(g)[:8] for g in promoted_groups],
            )
            new_group_id = uuid.uuid4()
            exported: frozenset[ExportedPin] = frozenset()
            if dlg.auto_export_external:
                # The new group doesn't exist in session.layout.groups yet
                # — splice it into a transient dict so the encapsulated
                # helper can read direct members + child re-exports the
                # same way it does for any other group.
                transient = ComponentGroup(
                    id=new_group_id,
                    name=dlg.new_name or default_name,
                    component_ids=promoted_free,
                    child_group_ids=promoted_groups,
                )
                groups_with_new = dict(self._session.layout.groups)
                groups_with_new[new_group_id] = transient
                exported = external_pins_for_group(
                    groups_with_new, new_group_id, self._session.schematic
                )
            new_group = ComponentGroup(
                id=new_group_id,
                name=dlg.new_name or default_name,
                component_ids=promoted_free,
                child_group_ids=promoted_groups,
                exported_pins=exported,
            )
            cmd = CreateGroupCommand(group=new_group)
            self._session.stack.execute(cmd, self._session)
            # Detach every promoted sub-group from its current parent —
            # that parent is always `active_gid` for direct children of
            # active (the only kind promotion produces), but use
            # parent_group_of() defensively so future hierarchical
            # selections still work.
            for promoted_gid in promoted_groups:
                old_parent = parent_group_of(
                    self._session.layout.groups, promoted_gid
                )
                if old_parent is not None:
                    self._session.stack.execute(
                        RemoveFromGroupCommand(
                            group_id=old_parent,
                            child_group_ids=frozenset({promoted_gid}),
                        ),
                        self._session,
                    )
            # Register new group as a child of the active group.  Also
            # detach the free components from the parent's direct
            # membership so the "one component is in exactly one group"
            # invariant holds — otherwise drag/rotate hit-tests find
            # the components as directs of the parent before walking
            # into the child and never enter the group-drag-arming path
            # (user-observed 2026-06-04).
            cmd_attach = AddToGroupCommand(
                group_id=active_gid,
                child_group_ids=frozenset({new_group.id}),
            )
            self._session.stack.execute(cmd_attach, self._session)
            if promoted_free:
                cmd_detach = RemoveFromGroupCommand(
                    group_id=active_gid,
                    component_ids=promoted_free,
                )
                self._session.stack.execute(cmd_detach, self._session)
            self._session.layout_changed.emit()
            self._select_group(new_group.id)
            return new_group.id
        else:
            # Add to existing group.
            target_id = dlg.target_group_id
            if target_id is None:
                return None
            cmd_add = AddToGroupCommand(
                group_id=target_id,
                component_ids=ids,
            )
            self._session.stack.execute(cmd_add, self._session)
            # Same invariant fix as the "new group" branch: when adding
            # to an existing child group, detach the components from the
            # active group's direct membership.
            if target_id != active_gid:
                cmd_detach_existing = RemoveFromGroupCommand(
                    group_id=active_gid,
                    component_ids=ids,
                )
                self._session.stack.execute(cmd_detach_existing, self._session)
            if dlg.auto_export_external:
                # Recompute external pins for the *whole* group after merge —
                # adding components can both add and remove external pins
                # (a previously-external net may now be fully internal).
                new_exported = external_pins_for_group(
                    self._session.layout.groups, target_id,
                    self._session.schematic,
                )
                if new_exported != self._session.layout.groups[target_id].exported_pins:
                    self._session.stack.execute(
                        SetExportedPinsCommand(
                            group_id=target_id, new_exported=new_exported
                        ),
                        self._session,
                    )
            self._session.layout_changed.emit()
            self._select_group(target_id)
            return target_id

    def exit_group(self) -> None:
        """Pop the innermost group from the navigation stack (Esc)."""
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        if len(self._group_stack) > 1:  # never pop root
            exiting_gid = self._current_group_id
            _log.debug("group.exited", group_id=str(exiting_gid), remaining=len(self._group_stack) - 1)
            self._group_stack.pop()
            self._clear_multi_selection()
            self._deselect()
            self._reset_group_metadata(exiting_gid)
            self._notify_active_group()

    def _reset_group_metadata(self, group_id: uuid.UUID) -> None:
        """Recompute origin and zero rotation after editing a group. Physical positions unchanged."""
        from protoboard.domain.commands.group_cmds import _group_origin
        from protoboard.domain.layout.group import all_component_ids as _all_ids
        group = self._session.layout.groups.get(group_id)
        if group is None:
            return
        comp_ids = _all_ids(self._session.layout.groups, group_id)
        new_origin = _group_origin(
            comp_ids,
            self._session.layout.placements,
            self._session.instances,
            self._session.components,
        )
        from protoboard.domain.layout.group import ComponentGroup
        self._session.layout.groups[group_id] = ComponentGroup(
            id=group.id,
            name=group.name,
            component_ids=group.component_ids,
            child_group_ids=group.child_group_ids,
            exported_pins=group.exported_pins,
            rotation=0,
            origin=new_origin,
        )

    def _notify_active_group(self) -> None:
        """Inform BoardView of the current active group for rendering."""
        if hasattr(self._view, "set_active_group"):
            self._view.set_active_group(self._current_group_id)  # type: ignore[union-attr]

    # ------------------------------------------------------------------

    def _props_panel(self) -> PropertiesPanel | None:
        win = self._view.window()
        panel = getattr(win, "_props_panel", None)
        return panel if isinstance(panel, PropertiesPanel) else None

    # ------------------------------------------------------------------
    # Connectivity highlight
    # ------------------------------------------------------------------

    def _hit_test_pin(
        self, scene_pt: QPointF
    ) -> tuple[uuid.UUID, int] | None:
        """Return (comp_id, pin_number) if click lands within _HIT_RADIUS of a pin."""
        session = self._session
        best_d = _HIT_RADIUS
        best: tuple[uuid.UUID, int] | None = None
        camera = self._camera_side()
        layers = self._layer_state()
        for comp_id, placement in session.layout.placements.items():
            side = placement.side  # type: ignore[assignment]
            if layers.alpha_for_component(side) == 0.0:  # type: ignore[union-attr]
                continue
            for pin_num, pos in session.iter_pin_positions(comp_id):
                d = math.hypot(scene_pt.x() - pos.x, scene_pt.y() - pos.y)
                # Prefer front-side pins.
                effective_d = d if placement.side == camera else d + _HIT_RADIUS * 0.5
                if effective_d < best_d:
                    best_d = effective_d
                    best = (comp_id, pin_num)
        return best

    def _hit_test_pin_for_comp(
        self, scene_pt: QPointF, comp_id: uuid.UUID
    ) -> tuple[uuid.UUID, int] | None:
        """Like _hit_test_pin but scans only one component — O(P) not O(C×P)."""
        session = self._session
        best_d = _HIT_RADIUS
        best: tuple[uuid.UUID, int] | None = None
        for pin_num, pos in session.iter_pin_positions(comp_id):
            d = math.hypot(scene_pt.x() - pos.x, scene_pt.y() - pos.y)
            if d < best_d:
                best_d = d
                best = (comp_id, pin_num)
        return best

    def _activate_net_highlight(self, comp_id: uuid.UUID, pin_number: int) -> None:
        """Highlight all pads and wires belonging to the net of the given pin."""
        scene = self._view.scene() if self._view is not None else None
        if scene is None:
            return
        session = self._session
        from protoboard.domain.schematic.net import PinRef
        from protoboard.gui.layer_state import Z_OVERLAY
        self._clear_connectivity_highlight()

        net = session.schematic.net_for_pin(PinRef(comp_id, pin_number))
        if net is None:
            return
        self._highlight_net_id = net.id
        if self._view is not None:
            self._view.set_highlighted_net(net.id)

        # Highlight pads of all pins in the net.
        for ref in net.pins:
            pos = session.get_pin_position(ref.component_id, ref.pin_number)
            if pos is None:
                continue
            r = 0.24
            circle = QGraphicsEllipseItem(QRectF(pos.x - r, pos.y - r, 2 * r, 2 * r))
            circle.setBrush(QBrush(QColor(255, 255, 0, 150)))
            circle.setPen(QPen(QColor(255, 200, 0, 220), 0.05))
            circle.setZValue(Z_OVERLAY + 1.0)
            scene.addItem(circle)
            self._highlight_items.append(circle)

        # Wire dimming is handled by set_highlighted_net → _apply_highlight_opacity()
        # which uses the cached wire_net_map from the graph — no O(W×P) scan needed.
        self._notify_selection()

    def _pin_positions_for_ref(self, ref: object) -> list[object]:
        """Helper: get world positions for a PinRef."""
        from protoboard.domain.schematic.net import PinRef
        if not isinstance(ref, PinRef):
            return []
        pos = self._session.get_pin_position(ref.component_id, ref.pin_number)
        return [pos] if pos is not None else []

    def _activate_component_highlight(self, comp_id: uuid.UUID) -> None:
        """Highlight bboxes of all components directly connected to comp_id."""
        scene = self._view.scene() if self._view is not None else None
        if scene is None:
            return
        session = self._session
        from protoboard.gui.layer_state import Z_OVERLAY
        self._clear_connectivity_highlight()

        # Collect connected component IDs (any shared net).
        connected: set[uuid.UUID] = set()
        for net in session.schematic.nets.values():
            comp_ids_in_net = {pr.component_id for pr in net.pins}
            if comp_id in comp_ids_in_net:
                connected.update(comp_ids_in_net - {comp_id})

        self._highlight_comp_ids = connected

        for other_id in connected:
            bbox = session.component_bbox(other_id)
            if bbox is None:
                continue
            rect = QGraphicsRectItem(QRectF(bbox.x, bbox.y, bbox.width, bbox.height))
            rect.setPen(QPen(QColor(0, 255, 120, 210), 0.07))
            rect.setBrush(QBrush(QColor(0, 255, 120, 25)))
            rect.setZValue(Z_OVERLAY + 1.0)
            scene.addItem(rect)
            self._highlight_items.append(rect)

    def _clear_connectivity_highlight(self) -> None:
        """Remove all connectivity overlay items from the scene.

        Does NOT touch the HighlightStack — callers wanting to drop the
        auto-highlight as well must also call
        ``session.highlight_stack.clear_auto("select_tool")``.
        """
        scene = self._view.scene() if self._view is not None else None
        if scene is not None:
            for item in self._highlight_items:
                scene.removeItem(item)
        self._highlight_items.clear()
        self._highlight_net_id = None
        self._highlight_comp_ids = set()


# ---------------------------------------------------------------------------
# Wire distance helpers
# ---------------------------------------------------------------------------


def _min_dist_to_wire(px: float, py: float, wire: Wire) -> float:
    pts = wire.path
    best = math.inf
    for i in range(len(pts) - 1):
        d = _dist_point_to_segment(
            px, py, pts[i].x, pts[i].y, pts[i + 1].x, pts[i + 1].y
        )
        if d < best:
            best = d
    return best


def _dist_point_to_segment(
    px: float, py: float,
    ax: float, ay: float,
    bx: float, by: float,
) -> float:
    dx, dy = bx - ax, by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq < 1e-12:
        return math.sqrt((px - ax) ** 2 + (py - ay) ** 2)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len_sq))
    cx, cy = ax + t * dx, ay + t * dy
    return math.sqrt((px - cx) ** 2 + (py - cy) ** 2)


def _dedupe_adjacent(path: list[Point]) -> list[Point]:
    """Remove consecutive duplicate points."""
    if not path:
        return path
    result = [path[0]]
    for pt in path[1:]:
        if abs(pt.x - result[-1].x) > 1e-9 or abs(pt.y - result[-1].y) > 1e-9:
            result.append(pt)
    return result


def _segment_intersects_rect(
    ax: float, ay: float, bx: float, by: float,
    rx0: float, ry0: float, rx1: float, ry1: float,
) -> bool:
    """Cohen-Sutherland clip: True if segment AB intersects rect."""
    def _code(x: float, y: float) -> int:
        c = 0
        if x < rx0:
            c |= 1
        elif x > rx1:
            c |= 2
        if y < ry0:
            c |= 4
        elif y > ry1:
            c |= 8
        return c

    c0, c1 = _code(ax, ay), _code(bx, by)
    while True:
        if not (c0 | c1):
            return True   # both inside
        if c0 & c1:
            return False  # both outside same half-plane
        c = c0 if c0 else c1
        if c & 1:
            y = ay + (by - ay) * (rx0 - ax) / (bx - ax + 1e-15)
            x = rx0
        elif c & 2:
            y = ay + (by - ay) * (rx1 - ax) / (bx - ax + 1e-15)
            x = rx1
        elif c & 4:
            x = ax + (bx - ax) * (ry0 - ay) / (by - ay + 1e-15)
            y = ry0
        else:
            x = ax + (bx - ax) * (ry1 - ay) / (by - ay + 1e-15)
            y = ry1
        if c == c0:
            ax, ay, c0 = x, y, _code(x, y)
        else:
            bx, by, c1 = x, y, _code(x, y)
