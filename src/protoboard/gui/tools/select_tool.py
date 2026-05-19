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

import structlog
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QKeyEvent, QMouseEvent, QPen
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
    ReplaceWireSegmentCommand,
)
from protoboard.domain.layout.group import (
    all_component_ids,
    top_level_group_for,
)
from protoboard.domain.layout.placement import component_bbox, flip_component, pin_world_position, unrotate
from protoboard.domain.layout.wire import WireSegment
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
        # Rubber-band state.
        self._rubber_band_start: QPointF | None = None
        self._rubber_band_item: QGraphicsRectItem | None = None
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
        self._drag_timer: QTimer | None = None  # built lazily on first drag

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
        self._hl_source_wire = wire_id
        self._hl_source_comp = comp_id
        self._hl_source_pin = pin_number

        correlated_comps, correlated_wires = self._resolve_relation(
            self._relation,
            wire_id=wire_id,
            comp_id=comp_id,
            pin_number=pin_number,
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
        else:
            # LPM without Shift: select only the clicked object.  Component
            # selection is handled by on_press before us; wire selection is
            # delegated here.
            if wire_id is not None:
                self._select_wire(wire_id)

        self._refresh_auto_highlight(
            wire_id=wire_id,
            comp_id=comp_id,
            pin_number=pin_number,
            correlated_comps=correlated_comps,
            correlated_wires=correlated_wires,
        )
        self._notify_selection()

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
        from protoboard.gui.board_view import _build_wire_net_map

        session = self._session
        if session is None:
            return set()
        if comp_id is not None and pin_number is not None:
            single = self._resolve_logical_net(comp_id=comp_id, pin_number=pin_number)
            return {single} if single is not None else set()
        if wire_id is not None:
            wnm = _build_wire_net_map(session)
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
        from protoboard.gui.board_view import _build_wire_net_map

        session = self._session
        if session is None:
            return None
        if comp_id is not None and pin_number is not None:
            ref = PinRef(component_id=comp_id, pin_number=pin_number)
            net = session.schematic.net_for_pin(ref)
            if net is not None:
                return net.id
        if wire_id is not None:
            wnm = _build_wire_net_map(session)
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
        from protoboard.gui.board_view import _build_wire_net_map
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
            wnm0 = _build_wire_net_map(session)
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
            wnm = _build_wire_net_map(session)
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
                for ref in net_obj.pins:
                    ck = graph.node_for_pin(PinRef(ref.component_id, ref.pin_number))
                    if ck is not None:
                        same_key_count = sum(
                            1 for r2 in net_obj.pins
                            if graph.node_for_pin(PinRef(r2.component_id, r2.pin_number)) == ck
                        )
                        if same_key_count > 1:
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
            from protoboard.domain.layout.placement import component_bbox
            rgba_fill = profile.neighbor_rect_rgba
            rgba_pen = profile.neighbor_pen_rgba
            for other_id in correlated_comps:
                pl = session.layout.placements.get(other_id)
                if pl is None:
                    continue
                inst = session.instances.get(other_id)
                if inst is None:
                    continue
                defn = session.components.get(inst.definition_ref)
                if defn is None:
                    continue
                bbox = component_bbox(defn.footprint, pl, defn.component_class.value)
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
        """Commit floating state to CommandStack.  Refuses on HARD severity."""
        if not self._floating_ids:
            return
        from protoboard.gui.tools.drag_commit_decision import DragSeverity
        if self._floating_severity == DragSeverity.HARD.value:
            win = self._view.window() if self._view is not None else None
            sb = win.statusBar() if win is not None and hasattr(win, "statusBar") else None
            if sb is not None:
                sb.showMessage(
                    "Konflikt: nie można zatwierdzić w tym miejscu (Esc aby cofnąć)",
                    3000,
                )
            return

        from protoboard.domain.commands.base import Command

        cmds: list[Command[ComponentState]] = []
        # Wire commands (remove/add) must run BEFORE MoveComponentCommand so that
        # _update_wires_for_move (called inside MoveComponentCommand.apply) does not
        # encounter wires that are about to be removed and accidentally drop them
        # from layout.wires before RemoveWireCommand can find them by ID.
        for cmd in self._floating_extra_cmds:
            cmds.append(cmd)  # type: ignore[arg-type]
        n_removed = sum(
            1 for c in self._floating_extra_cmds
            if type(c).__name__ == "RemoveWireCommand"
        )
        for cid in self._floating_ids:
            new_pl = self._session.layout.placements.get(cid)
            old_pl = self._floating_orig_placements.get(cid)
            if new_pl is None or old_pl is None:
                continue
            cmds.append(
                MoveComponentCommand(
                    component_id=cid,
                    new_placement=new_pl,
                    old_placement=old_pl,
                    skip_conflict_check=True,
                    exclude_wire_ids=self._floating_dragged_wire_ids,
                )
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
        if cmds:
            if len(cmds) == 1:
                self._session.stack.execute(cmds[0], self._session)
            else:
                macro = MacroCommand(cmds, description="Move component")
                self._session.stack.execute(macro, self._session)
            self._session.layout_changed.emit()
            # Toast about removed wires (Ctrl+Z to undo).
            if n_removed:
                win = self._view.window() if self._view is not None else None
                sb = win.statusBar() if win is not None and hasattr(win, "statusBar") else None
                if sb is not None:
                    sb.showMessage(
                        f"Usunięto {n_removed} drut(ów) (Ctrl+Z by cofnąć)",
                        4000,
                    )

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
        if self._view is not None:
            self._drag_overlay.reset(self._view.scene())
            self._corner_text.hide(self._view.scene())
        self._floating_ids = set()
        self._floating_orig_placements = {}
        self._floating_extra_cmds = []
        self._floating_dragged_wire_ids = frozenset()
        self._floating_decision = None
        self._floating_severity = "clear"
        self._session.layout_changed.emit()

    def _clear_floating(self) -> None:
        self._clear_floating_highlights()
        self._floating_ids = set()
        self._floating_orig_placements = {}
        self._floating_extra_cmds = []
        self._floating_dragged_wire_ids = frozenset()

    def _draw_floating_highlights(self) -> None:
        if self._view is None:
            return
        self._clear_floating_highlights()
        from protoboard.domain.layout.placement import component_bbox
        from protoboard.gui.layer_state import Z_SELECTION
        scene = self._view.scene()
        for cid in self._floating_ids:
            pl = self._session.layout.placements.get(cid)
            inst = self._session.instances.get(cid)
            if pl is None or inst is None:
                continue
            defn = self._session.components.get(inst.definition_ref)
            if defn is None:
                continue
            bbox = component_bbox(defn.footprint, pl, defn.component_class)
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
        self._drag_orig_positions = {}
        self._drag_orig_wire_paths = {}
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
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)

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

        # --- Ctrl+click: wire node or add/remove from multi-selection ---
        if ctrl:
            # Check for wire node click on the single-selected wire (starts edit).
            if self._selected_wire_id is not None:
                idx = self._hit_test_wire_node(scene_pt)
                if idx is not None:
                    self._start_wire_edit(idx)
                    return True

            # Check component bbox.
            hit = self._hit_test_bbox(scene_pt) if self._filter_components() else None
            if hit is not None:
                if hit in self._selected_ids:
                    self._selected_ids.discard(hit)
                    self._redraw_multi_highlights()
                else:
                    self._selected_ids.add(hit)
                    self._draw_multi_highlight(hit)
                self._deselect_wire()
                self._select_primary(hit)
                return True

            # Check wire.
            wire_hit = (
                self._hit_test_wire(scene_pt) if self._filter_wires() else None
            )
            if wire_hit is not None:
                if wire_hit in self._selected_wire_ids:
                    self._selected_wire_ids.discard(wire_hit)
                    self._redraw_multi_wire_highlights()
                else:
                    self._selected_wire_ids.add(wire_hit)
                    self._draw_multi_wire_highlight(wire_hit)
                return True

            return False

        # --- Klik na obrysie grupy — zaznacz grupę jako całość ---
        group_outline_hit = (
            self._hit_test_group_outline(scene_pt) if self._filter_groups() else None
        )
        if group_outline_hit is not None:
            self._cancel_wire_edit()
            self._deselect_wire()
            groups = self._session.layout.groups
            grp_members = frozenset(all_component_ids(groups, group_outline_hit))
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
                self._dragging = True
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
                # Collect internal wires via graph reachability.
                from protoboard.domain.commands.group_cmds import _find_internal_wires, _pin_positions_for
                placements = self._session.layout.placements
                instances = self._session.instances
                components = self._session.components
                group_pins = _pin_positions_for(grp_members, placements, instances, components)
                all_ids = frozenset(placements.keys())
                external_pins = _pin_positions_for(all_ids - grp_members, placements, instances, components)
                internal_ids = _find_internal_wires(self._session.layout.wires, group_pins, external_pins)
                for wire in self._session.layout.wires:
                    if wire.id in internal_ids:
                        self._drag_orig_wire_paths[wire.id] = list(wire.path)
            return True

        # --- Normal left click: hit-test component or wire ---
        hit = self._hit_test(scene_pt) if self._filter_components() else None
        if hit is not None:
            # Block interaction with components outside the active group context.
            if not self._is_accessible(hit):
                return False
            # Determine which pin (if any) was under the cursor — needed for
            # both shift-driven relation∩filter selection and auto-highlight.
            pin_hit = self._hit_test_pin_for_comp(scene_pt, hit) if self._view is not None else None
            pin_comp_id, pin_number = (pin_hit if pin_hit is not None else (hit, None))

            if shift:
                # Shift+LPM on a component → selection = relation ∩ filter.
                # No drag; explicit consume.
                self._cancel_wire_edit()
                self._apply_interaction(
                    comp_id=pin_comp_id, pin_number=pin_number, shift_held=True,
                )
                return True

            # Plain LPM: trigger auto-highlight, then perform the usual
            # primary-select + drag flow.
            self._apply_interaction(
                comp_id=pin_comp_id, pin_number=pin_number, shift_held=False,
            )
            # If the hit component is in a group that we haven't entered yet,
            # redirect the selection to that group's representative ID.
            hit = self._resolve_group_hit(hit)
            self._cancel_wire_edit()
            if hit not in self._selected_ids:
                # Fresh single-component click — clear all existing selection.
                self._deselect_wire()
                self._clear_multi_selection()
                self._select_primary(hit)
            else:
                # Click on already-selected component — keep multi-selection
                # (including wires) and set as primary for properties panel.
                self._select_primary(hit)
            # Begin drag.
            self._drag_overlay.reset(self._view.scene())
            self._corner_text.hide(self._view.scene())
            self._drag_last_decision = None
            self._stop_drag_validation_timer()
            self._dragging = True
            self._drag_orig_positions = {}
            self._drag_orig_wire_paths = {}
            drag_ids = self._selected_ids if self._selected_ids else {hit}
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
            for wire in self._session.layout.wires:
                if wire.id in self._selected_wire_ids:
                    self._drag_orig_wire_paths[wire.id] = list(wire.path)
            return True

        wire_hit = self._hit_test_wire(scene_pt) if self._filter_wires() else None
        if wire_hit is not None:
            self._cancel_wire_edit()
            if shift:
                # Shift+LPM on a wire → selection = relation ∩ filter.
                self._apply_interaction(wire_id=wire_hit, shift_held=True)
                return True
            if wire_hit in self._selected_wire_ids:
                # Klik na już zaznaczony drut → drag całej selekcji.
                snapped = self._view.snap(scene_pt)
                self._dragging = True
                self._drag_orig_positions = {}
                self._drag_orig_wire_paths = {}
                # Anchor offset: użyj pierwszego punktu klikniętego drutu.
                wire_obj = next(
                    (w for w in self._session.layout.wires if w.id == wire_hit),
                    None,
                )
                if wire_obj and wire_obj.path:
                    self._drag_anchor_offset = (
                        wire_obj.path[0].x - snapped.x,
                        wire_obj.path[0].y - snapped.y,
                    )
                    # Use wire anchor as synthetic "selected_id" anchor.
                    self._selected_id = None
                for wid in self._selected_wire_ids:
                    wobj = next(
                        (w for w in self._session.layout.wires if w.id == wid),
                        None,
                    )
                    if wobj:
                        self._drag_orig_wire_paths[wid] = list(wobj.path)
                for cid in self._selected_ids:
                    pl = self._session.layout.placements.get(cid)
                    if pl is not None:
                        self._drag_orig_positions[cid] = pl.position
            else:
                self._clear_multi_selection()
                self._clear_multi_wire_selection()
                self._deselect()
                # Plain LPM on a wire: select it + auto-highlight relation.
                self._apply_interaction(wire_id=wire_hit, shift_held=False)
            return True

        # Check if click is inside bbox of an already-selected component.
        # This allows dragging a multi-selection by clicking anywhere inside
        # a selected component's body, not just near a pin.
        bbox_hit = self._hit_test_bbox(scene_pt) if self._filter_components() else None
        if bbox_hit is not None and (
            bbox_hit in self._selected_ids
            or bbox_hit == self._selected_id
        ):
            self._cancel_wire_edit()
            self._select_primary(bbox_hit)
            self._dragging = True
            self._drag_orig_positions = {}
            self._drag_orig_wire_paths = {}
            drag_ids = self._selected_ids if self._selected_ids else {bbox_hit}
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
            for wire in self._session.layout.wires:
                if wire.id in self._selected_wire_ids:
                    self._drag_orig_wire_paths[wire.id] = list(wire.path)
            return True

        # Nothing hit — clear connectivity highlight and start rubber-band.
        self._cancel_wire_edit()
        self._clear_multi_selection()
        self._deselect()
        self._deselect_wire()
        self._clear_connectivity_highlight()
        self._notify_selection()
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
        groups = self._session.layout.groups
        current_group_id = self._current_group_id
        parent_group = groups.get(current_group_id)
        _log.info(
            "group.double_click",
            hit=str(hit) if hit else None,
            current_group=str(current_group_id),
            child_group_ids=[str(g) for g in (parent_group.child_group_ids if parent_group else [])],
            group_stack_depth=len(self._group_stack),
        )
        if hit is None:
            return False
        # Find which child group of the current group contains this component.
        if parent_group is not None:
            for child_gid in parent_group.child_group_ids:
                members = all_component_ids(groups, child_gid)
                if hit in members:
                    self._group_stack.append(child_gid)
                    _log.info("group.entered", group_id=str(child_gid), stack_depth=len(self._group_stack))
                    self._clear_multi_selection()
                    self._deselect()
                    self._select_primary(hit)
                    self._notify_active_group()
                    return True
        _log.info("group.double_click_no_entry", reason="hit not in any child group")
        return False

    def on_move(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)

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
            anchor_id = self._selected_id
            drag_ids = (
                self._selected_ids if self._selected_ids
                else ({anchor_id} if anchor_id else set())
            )
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
        # Rubber-band release: apply selection.
        if self._rubber_band_start is not None:
            self._finish_rubber_band(scene_pt)
            return

        if not self._dragging:
            return
        self._dragging = False
        self._stop_drag_validation_timer()

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
            # --- Atomic group drag: check all conflicts first, revert all on block ---
            # Group drag stays on the atomic-block path: groups must remain
            # geometrically coherent, partial commits aren't meaningful.
            from protoboard.domain.layout.group import all_component_ids
            full_ids = set(all_component_ids(
                self._session.layout.groups, effective_group_id
            ))
            full_ids &= set(self._drag_orig_positions.keys())
            effective_drag = full_ids if full_ids else drag_ids
            _saved_group_id = self._selected_group_id
            self._selected_group_id = effective_group_id
            commands = self._build_group_drag_commands(effective_drag) or []
            self._selected_group_id = _saved_group_id
            self._drag_orig_wire_paths = {}
            if commands:
                if len(commands) == 1:
                    self._session.stack.execute(commands[0], self._session)
                else:
                    macro = MacroCommand(commands, description="Move selection")
                    self._session.stack.execute(macro, self._session)
                self._session.layout_changed.emit()
            self._drag_overlay.reset(self._view.scene())
            if self._selected_id is not None:
                self._select_primary(self._selected_id)
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
        decision = classify_drag_commit(
            session=self._session,
            moved_comp_ids=floating_moved or drag_ids,
            dragged_wire_ids=dragged_wire_ids,
        )

        # Wire-path-change commands for live-dragged wires that need a
        # ReplaceWireSegmentCommand entry in undo history.
        wire_move_cmds: list[object] = []
        for wire in self._session.layout.wires:
            orig_wire_path = self._drag_orig_wire_paths.get(wire.id)
            if orig_wire_path is None:
                continue
            if wire.path == orig_wire_path:
                continue
            wire_move_cmds.append(
                ReplaceWireSegmentCommand(
                    wire_id=wire.id,
                    new_path=list(wire.path),
                    old_path=orig_wire_path,
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
            if isinstance(cmd, ReplaceWireSegmentCommand):
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
            if self._selected_id is not None:
                self._select_primary(self._selected_id)
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
                if self._selected_id is not None:
                    self._select_primary(self._selected_id)
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
        if self._selected_id is not None and not self._is_floating():
            self._select_primary(self._selected_id)

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
            if self._selected_wire_ids or self._selected_wire_id is not None:
                self._delete_multi_wires()
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
                self._clear_multi_selection()
                self._deselect()
                self._deselect_wire()
                self._clear_connectivity_highlight()
                self._notify_selection()
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
        has_items = False

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
                    if active_grp is not None and comp_id in all_component_ids(
                        self._session.layout.groups, active_gid
                    ):
                        from protoboard.domain.commands.group_cmds import SetExportedPinsCommand
                        from protoboard.domain.layout.group import ExportedPin
                        menu.addSection(f"{inst.refdes} — export/unexport pins")
                        for pin_num in range(1, defn.footprint.pin_count() + 1):
                            ep = ExportedPin(component_id=comp_id, pin_number=pin_num)
                            pin_name = defn.pin_names.get(pin_num, str(pin_num))
                            already = ep in active_grp.exported_pins
                            marker = " ◀" if pin_num == clicked_pin else ""
                            label = (
                                f"✓ Unexport pin {pin_num} ({pin_name}){marker}"
                                if already
                                else f"  Export pin {pin_num} ({pin_name}){marker}"
                            )
                            act = menu.addAction(label)
                            act.setData(("export_pin", active_gid, ep, already))
                            act.hovered.connect(
                                lambda _cid=comp_id, _pn=pin_num: _hover_pin(_cid, _pn)
                            )

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
                    menu.addSection(f"{inst.refdes} — disconnect pin from net")
                    for pin_num, pin_name, net_name in netted_pins:
                        marker = " ◀" if pin_num == clicked_pin else ""
                        act = menu.addAction(f"Pin {pin_num} ({pin_name})  ←  {net_name}{marker}")
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

                # Unweave — only meaningful with ≥2 wires selected.
                if n_wires >= 2:
                    act_unweave = menu.addAction(
                        f"Unweave {n_wires} wires (topological)"
                    )
                    act_unweave.setData(("unweave_wires", target_wire_ids))

        if not has_items:
            return False

        chosen = menu.exec(self._view.mapToGlobal(self._view.mapFromScene(scene_pt)))
        if chosen is None:
            return True

        data = chosen.data()
        if not isinstance(data, tuple):
            return True
        action = data[0]

        if action == "lock":
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

        elif action == "unweave_wires":
            _, target_wire_ids = data
            self._unweave_wires(target_wire_ids)

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
        self, wire: "WireSegment", scene_pt: QPointF
    ) -> bool:
        from PySide6.QtWidgets import QMenu
        from protoboard.domain.commands.component_cmds import (
            SetWireInsulatedCommand,
            SetWireSideCommand,
        )
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

        chosen = menu.exec(
            self._view.mapToGlobal(self._view.mapFromScene(scene_pt))
        )
        if chosen is None:
            return True

        action, value = chosen.data()
        if action == "flip_fragment":
            self._flip_wire_fragment()
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
        ids = list(self._selected_ids)
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
        ids = list(self._selected_ids)
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
        """Recompute and redraw drag conflict overlay (throttled to snapped grid)."""
        if not self._drag_overlay.needs_update(snapped):
            return
        from protoboard.domain.layout.pad_conflict import (
            MoveConflictSeverity,
            MoveConflictResult,
            check_group_move_conflicts,
        )
        active_gid = self._selected_group_id
        if active_gid is None and not drag_ids:
            self._drag_overlay.reset(self._view.scene())
            return

        if active_gid is not None:
            # Build a fresh PadIndex over the live (post-drag) layout —
            # session.pad_index() is stale because the drag mutates positions
            # in place without emitting layout_changed.
            from protoboard.domain.board.pad_state import PadIndex as _PadIndex
            _drag_idx = _PadIndex(
                board=self._session.board,
                layout=self._session.layout,
                schematic=self._session.schematic,
                components=self._session.components,
                instances=self._session.instances,
            )
            result = check_group_move_conflicts(
                moved_ids=drag_ids,
                active_group_id=active_gid,
                layout_placements=self._session.layout.placements,
                layout_wires=self._session.layout.wires,
                layout_groups=self._session.layout.groups,
                instances=self._session.instances,
                components=self._session.components,
                schematic=self._session.schematic,
                pad_index=_drag_idx,
            )
        else:
            # Non-group drag: pad conflict check only (fast).
            # Wire-crossing check (find_wires_crossing_pads) is skipped during
            # live drag — it rebuilds the full pad map on every grid step and
            # dominates mouse-move latency.  It runs on mouse-release instead.
            from protoboard.domain.layout.pad_conflict import (
                find_conflicts, ConflictKind,
            )
            from protoboard.geometry import Point as _Point
            blocking: list[_Point] = []
            for cid in drag_ids:
                pl = self._session.layout.placements.get(cid)
                inst = self._session.instances.get(cid)
                if pl is None or inst is None:
                    continue
                defn = self._session.components.get(inst.definition_ref)
                if defn is None:
                    continue
                for c in find_conflicts(
                    pl, defn, cid,
                    self._session.layout.placements,
                    self._session.instances,
                    self._session.components,
                    self._session.schematic,
                ):
                    if c.kind == ConflictKind.DIFFERENT_NETS:
                        blocking.append(c.position)
            if blocking:
                result = MoveConflictResult(
                    severity=MoveConflictSeverity.BLOCKED,
                    blocking_pad_positions=blocking,
                    removable_wire_ids=[],
                )
            else:
                result = MoveConflictResult(
                    severity=MoveConflictSeverity.CLEAR,
                    blocking_pad_positions=[],
                    removable_wire_ids=[],
                )

        # Compute drag bbox for outline.
        bbox = self._drag_bbox(drag_ids)
        self._drag_overlay.update(self._view.scene(), result, bbox, snapped)

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

    def _update_corner_text_from_decision(self, decision: object) -> None:
        """Show or hide the corner banner based on *decision*."""
        from PySide6.QtGui import QColor
        from protoboard.gui.tools.drag_commit_decision import (
            DragCommitDecision,
            DragSeverity,
        )
        if not isinstance(decision, DragCommitDecision):
            return
        scene = self._view.scene()
        if scene is None:
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
        from protoboard.domain.board.pad_state import PadIndex as _PadIndex
        _drag_idx = _PadIndex(
            board=self._session.board,
            layout=self._session.layout,
            schematic=self._session.schematic,
            components=self._session.components,
            instances=self._session.instances,
        )
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

        _log.info("build_group_drag.conflict_result",
                  severity=result.severity.value,
                  blocking_pads=[(p.x, p.y) for p in result.blocking_pad_positions],
                  removable_wires=[str(i) for i in result.removable_wire_ids])

        if result.severity == MoveConflictSeverity.BLOCKED:
            _log.info("build_group_drag.reverting",
                      drag_ids=[str(i) for i in drag_ids],
                      orig_positions={str(k): (v.x, v.y)
                                      for k, v in self._drag_orig_positions.items()
                                      if k in drag_ids})
            # Revert ALL group members atomically.
            for cid in drag_ids:
                orig = self._drag_orig_positions.get(cid)
                if orig is not None:
                    pl = self._session.layout.placements.get(cid)
                    if pl is not None:
                        _log.debug("build_group_drag.revert_component",
                                   cid=str(cid),
                                   from_pos=(pl.position.x, pl.position.y),
                                   to_pos=(orig.x, orig.y))
                        pl.position = orig
            # Revert internal wire paths.
            reverted_wires = 0
            for wire in self._session.layout.wires:
                if wire.id in self._drag_orig_wire_paths:
                    wire.path = list(self._drag_orig_wire_paths[wire.id])
                    reverted_wires += 1
            _log.info("build_group_drag.reverted", wires=reverted_wires)
            sb = getattr(self._view.window(), "statusBar", lambda: None)()
            if sb is not None:
                sb.showMessage("Przesunięcie grupy zablokowane — konflikt sieci.", 4000)
            self._session.layout_changed.emit()
            return None

        # Build move + wire-replace commands.
        cmds: list[Command[ComponentState]] = []
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
                exclude_wire_ids=frozenset(self._drag_orig_wire_paths.keys()),
            ))

        for wire in self._session.layout.wires:
            if wire.id not in self._drag_orig_wire_paths:
                continue
            orig_path = self._drag_orig_wire_paths[wire.id]
            if wire.path == orig_path:
                continue
            cmds.append(ReplaceWireSegmentCommand(
                wire_id=wire.id,
                new_path=list(wire.path),
                old_path=orig_path,
            ))

        # Add removal commands for removable crossing wires.
        if result.severity == MoveConflictSeverity.REMOVABLE:
            id_set = set(result.removable_wire_ids)
            for wire in self._session.layout.wires:
                if wire.id in id_set:
                    cmds.append(RemoveWireCommand(wire_id=wire.id))
            sb = getattr(self._view.window(), "statusBar", lambda: None)()
            if sb is not None:
                sb.showMessage(
                    f"Usunięto {len(result.removable_wire_ids)} drut(ów) kolidujących po przesunięciu grupy.",
                    4000,
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
                f"Usunięto {len(cmds)} drut(ów) kolidujących po przesunięciu.", 4000
            )
        return cmds

    def _delete_selected(self) -> None:
        comp_id = self._selected_id
        if comp_id is None:
            return
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
        from protoboard.gui.board_view import _build_wire_net_map
        nets = (
            [n for n in self._nets_for_comp(comp_id) if n.id == net_id]  # type: ignore[attr-defined]
            if net_id is not None
            else self._nets_for_comp(comp_id)
        )
        wire_net_map = _build_wire_net_map(self._session)
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
        from protoboard.gui.board_view import _build_wire_net_map
        wire_net_map = _build_wire_net_map(self._session)
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
        wire = next(
            (w for w in self._session.layout.wires if w.id == wire_id), None
        )
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
        wire = next(
            (w for w in self._session.layout.wires if w.id == wire_id), None
        )
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
        ids = [cid for cid in self._selected_ids if cid in existing_comps]
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

    def _update_rubber_band(self, scene_pt: QPointF) -> None:
        if self._rubber_band_start is None:
            return
        if self._rubber_band_item is not None:
            self._view.scene().removeItem(self._rubber_band_item)
            self._rubber_band_item = None

        start = self._rubber_band_start
        x0 = min(start.x(), scene_pt.x())
        y0 = min(start.y(), scene_pt.y())
        w = abs(scene_pt.x() - start.x())
        h = abs(scene_pt.y() - start.y())

        left_to_right = scene_pt.x() >= start.x()
        pen = _RUBBER_BAND_PEN_LR if left_to_right else _RUBBER_BAND_PEN_RL
        fill = _RUBBER_BAND_FILL_LR if left_to_right else _RUBBER_BAND_FILL_RL

        item = QGraphicsRectItem(QRectF(x0, y0, w, h))
        item.setPen(pen)
        item.setBrush(fill)
        self._view.scene().addItem(item)
        self._rubber_band_item = item

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
            for comp_id, placement in session.layout.placements.items():
                inst = session.instances.get(comp_id)
                if inst is None:
                    continue
                defn = session.components.get(inst.definition_ref)
                if defn is None:
                    continue
                bbox = component_bbox(defn.footprint, placement, defn.component_class)
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

        wire_map = {w.id: w for w in self._session.layout.wires if w.id in candidates}
        for wire in wire_map.values():
            d = _min_dist_to_wire(px, py, wire)
            if d < best_d:
                best_d = d
                best_id = wire.id
        return best_id

    def _select_wire(self, wire_id: uuid.UUID) -> None:
        self._deselect_wire()
        self._selected_wire_id = wire_id
        _log.debug("selection.wire", wire_id=str(wire_id))
        self._draw_wire_highlight(wire_id)

    def _select_wire_cycled(self, wire_id: uuid.UUID, scene_pt: "QPointF") -> None:
        """Cycle wire selection mode on repeated clicks on the same wire.

        Cycle:
          1st click → full electric net (all wires of the net) + component pads
          2nd click → full electric net, no component pads
          3rd click → only the clicked wire segment
          4th click → wraps back to 1st
        """
        if self._wire_click_id == wire_id:
            self._wire_click_cycle = (self._wire_click_cycle % 3) + 1
        else:
            self._wire_click_id = wire_id
            self._wire_click_cycle = 1

        wire = next(
            (w for w in self._session.layout.wires if w.id == wire_id), None
        )
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
        graph = self._session.connectivity_graph()

        # Collect canonical keys for the electrically-active points of this wire.
        # Insulated wires only make contact at their two endpoints — intermediate
        # points float above the board and must not be used to match other wires.
        # Bare wires contact every pad they pass through.
        wire_canonical: set[object] = set()
        if wire.insulated:
            contact_pts = [wire.path[0], wire.path[-1]] if len(wire.path) >= 2 else wire.path
        else:
            contact_pts = wire.path
        for pt in contact_pts:
            ck = graph.node_for_pad(pt, wire.side)
            if ck is not None:
                wire_canonical.add(ck)

        if not wire_canonical:
            # Wire not in graph — fall back to segment.
            self._select_wire(wire_id)
            return

        # All wires whose electrically-active points share a canonical key.
        net_wire_ids: set[uuid.UUID] = set()
        for w in self._session.layout.wires:
            check_pts = (
                ([w.path[0], w.path[-1]] if len(w.path) >= 2 else w.path)
                if w.insulated
                else w.path
            )
            for pt in check_pts:
                ck = graph.node_for_pad(pt, w.side)
                if ck in wire_canonical:
                    net_wire_ids.add(w.id)
                    break

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
            from protoboard.gui.board_view import _build_wire_net_map
            _wnm = _build_wire_net_map(self._session)
            _net_id = _wnm.get(wire_id)
            self._highlight_net_id = _net_id  # keep in sync so _notify_selection sees it
            self._view.set_highlighted_net(_net_id)

        if self._wire_click_cycle == 1:
            # Highlight component pins electrically connected to this net
            # as red circles (cycle 1 only — cleared on cycle 2/3/deselect).
            from protoboard.domain.schematic.net import PinRef
            pad_positions: list[Point] = []
            for comp_id in list(self._session.layout.placements):
                for pin_num, pos in self._session.iter_pin_positions(comp_id):
                    ck = graph.node_for_pin(PinRef(component_id=comp_id, pin_number=pin_num))
                    if ck in wire_canonical:
                        pad_positions.append(pos)
            self._draw_net_pad_highlights(pad_positions)

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
        wire = next(
            (w for w in self._session.layout.wires if w.id == wire_id), None
        )
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

    def _delete_wire(self) -> None:
        wire_id = self._selected_wire_id
        if wire_id is None:
            return
        self._cancel_wire_edit()
        cmd = RemoveWireCommand(wire_id=wire_id)
        self._session.stack.execute(cmd, self._session)
        self._deselect_wire()
        self._session.layout_changed.emit()

    def _draw_multi_wire_highlight(self, wire_id: uuid.UUID) -> None:
        wire = next(
            (w for w in self._session.layout.wires if w.id == wire_id), None
        )
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

    def _delete_multi_wires(self) -> None:
        # Include primary wire (_selected_wire_id) alongside multi-selection.
        all_ids = set(self._selected_wire_ids)
        if self._selected_wire_id is not None:
            all_ids.add(self._selected_wire_id)
        # Filter out stale IDs (wire may have been removed by a previous operation).
        existing = {w.id for w in self._session.layout.wires}
        ids = [wid for wid in all_ids if wid in existing]
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
        Builds one ReplaceWireSegmentCommand per wire whose path actually
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
                    ReplaceWireSegmentCommand(
                        wire_id=wid,
                        new_path=list(new_wire.path),
                        old_path=list(wire.path),
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
        AddWireCommand for every new WireSegment, all bundled in a single
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
        if session is None:
            return
        if len(wire_ids) < 2:
            self._status_msg("Unweave: select at least 2 wires.")
            return

        all_wires = session.layout.wires
        targets = [w for w in all_wires if w.id in wire_ids]
        if len(targets) < 2:
            self._status_msg("Unweave: at least 2 valid wires required.")
            return

        before = count_mutual_crossings(targets)
        if before == 0:
            self._status_msg("Unweave: no crossings to resolve.")
            return

        # Pass schematic + connectivity graph so the algorithm can split
        # wires on plated-through pads when that resolves more crossings.
        out = unweave_wires(
            targets,
            schematic=session.schematic,
            graph=session.connectivity_graph(),
        )
        after = count_mutual_crossings(out)

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
        self._status_msg(
            f"Unweave: {before} → {after} crossings"
            + (f" ({len(targets)}→{len(out)} segments)" if len(targets) != len(out) else "")
        )

    # ------------------------------------------------------------------
    # Wire segment editing
    # ------------------------------------------------------------------

    def _ensure_wire_ready_for_edit(self, wire_id: uuid.UUID) -> None:
        """For a bare wire, materialise every board-pad on its path as a vertex.

        Bare wires laid as shortcuts (or post-simplified) may carry only their
        endpoints even though the physical conductor rests on several pads.
        Edit-mode hit-test only matches against existing vertices, so those
        intermediate pads would be unselectable.  This helper executes one
        ReplaceWireSegmentCommand that re-inserts the missing pad vertices —
        idempotent: no command is emitted when the path is already enriched
        or when the wire is insulated.
        """
        wire = next(
            (w for w in self._session.layout.wires if w.id == wire_id), None
        )
        if wire is None or wire.insulated or len(wire.path) < 2:
            return
        from protoboard.domain.layout.wire_utils import (
            board_pads_for_side,
            insert_pad_nodes,
        )
        pads = board_pads_for_side(self._session.board, wire.side)
        enriched = insert_pad_nodes(list(wire.path), pads)
        if enriched == list(wire.path):
            return
        from protoboard.domain.commands.component_cmds import (
            ReplaceWireSegmentCommand,
        )
        cmd = ReplaceWireSegmentCommand(wire_id=wire_id, new_path=enriched)
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

    def _get_selected_wire(self) -> WireSegment | None:
        if self._selected_wire_id is None:
            return None
        return next(
            (w for w in self._session.layout.wires
             if w.id == self._selected_wire_id),
            None,
        )

    def _hit_test_wire_node_any(
        self, scene_pt: QPointF
    ) -> tuple[WireSegment | None, int | None]:
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

        best_wire: WireSegment | None = None
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
    ) -> list[WireSegment] | None:
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

        def _shares_point(a: WireSegment, b: WireSegment) -> bool:
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

    def _excise_cross_wire_range(
        self,
    ) -> tuple[object, Point, Point] | None:
        """Build a MacroCommand that excises the selected cross-wire fragment.

        Returns (macro_cmd, start_pt, end_pt) where start_pt and end_pt are the
        physical points at the edit boundaries (useful for re-routing afterwards).
        Returns None when the range is invalid or wires are not connected.
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

        # --- Start wire: keep prefix [0 .. start_idx] ---
        if start_idx == 0:
            cmds.append(RemoveWireCommand(wire_id=start_wire.id))
        else:
            new_path = list(start_wire.path[:start_idx + 1])
            cmds.append(ReplaceWireSegmentCommand(
                wire_id=start_wire.id,
                new_path=new_path,
                old_path=list(start_wire.path),
            ))

        # --- Intermediate wires: remove entirely ---
        for mid_wire in ordered[1:-1]:
            cmds.append(RemoveWireCommand(wire_id=mid_wire.id))

        # --- End wire: keep suffix [end_idx .. end] ---
        if len(ordered) > 1:
            if end_idx == len(end_wire.path) - 1:
                cmds.append(RemoveWireCommand(wire_id=end_wire.id))
            else:
                new_path_end = list(end_wire.path[end_idx:])
                cmds.append(ReplaceWireSegmentCommand(
                    wire_id=end_wire.id,
                    new_path=new_path_end,
                    old_path=list(end_wire.path),
                ))

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

        def _make_wire_group(wire: WireSegment, start_i: int | None, end_i: int | None) -> QGraphicsItemGroup:
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

    def _set_edit_end(self, node_idx: int, wire: WireSegment) -> None:
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
            from protoboard.domain.layout.wire import WireSegment as WS

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
        cmd = ReplaceWireSegmentCommand(wire_id=wire.id, new_path=new_path)
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
        Same-wire: if whole wire → RemoveWireCommand; else → ReplaceWireSegmentCommand.
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
        from protoboard.domain.layout.wire import WireSegment

        cmds: list[Command[ComponentState]] = [
            cast(Command[ComponentState], RemoveWireCommand(wire_id=wire.id))
        ]
        if len(left_path) >= 2:
            cmds.append(cast(Command[ComponentState], AddWireCommand(
                wire=WireSegment(
                    id=uuid.uuid4(),
                    path=left_path,
                    insulated=wire.insulated,
                    side=wire.side,
                    color=wire.color,
                )
            )))
        if len(right_path) >= 2:
            cmds.append(cast(Command[ComponentState], AddWireCommand(
                wire=WireSegment(
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
            from protoboard.domain.layout.wire import WireSegment as WS

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
        cmd = ReplaceWireSegmentCommand(wire_id=wire.id, new_path=new_path)
        self._session.stack.execute(cmd, self._session)
        self._session.layout_changed.emit()
        self._select_wire(wire.id)

    # ------------------------------------------------------------------
    # Group navigation helpers
    # ------------------------------------------------------------------

    def _hit_test_group_outline(self, scene_pt: QPointF) -> uuid.UUID | None:
        """Return group_id if the click landed on a group outline rect item."""
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        for item in self._view.scene().items(scene_pt):
            tag = item.data(1) if hasattr(item, "data") else None
            if tag == "group_outline":
                raw = item.data(0)
                if raw:
                    gid = uuid.UUID(str(raw))
                    # Only outline-clicks for child groups of active group.
                    groups = self._session.layout.groups
                    active = groups.get(self._current_group_id)
                    if active and gid in active.child_group_ids:
                        return gid
        return None

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

        from protoboard.domain.commands.group_cmds import AddToGroupCommand, CreateGroupCommand
        from protoboard.domain.layout.group import ComponentGroup
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
            new_group = ComponentGroup(
                id=uuid.uuid4(),
                name=dlg.new_name or default_name,
                component_ids=ids,
            )
            cmd = CreateGroupCommand(group=new_group)
            self._session.stack.execute(cmd, self._session)
            # Register new group as a child of the active group.
            cmd_attach = AddToGroupCommand(
                group_id=active_gid,
                child_group_ids=frozenset({new_group.id}),
            )
            self._session.stack.execute(cmd_attach, self._session)
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
        from protoboard.domain.layout.placement import component_bbox
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
            pl = session.layout.placements.get(other_id)
            if pl is None:
                continue
            inst = session.instances.get(other_id)
            if inst is None:
                continue
            defn = session.components.get(inst.definition_ref)
            if defn is None:
                continue
            bbox = component_bbox(defn.footprint, pl, defn.component_class.value)
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


def _min_dist_to_wire(px: float, py: float, wire: WireSegment) -> float:
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
