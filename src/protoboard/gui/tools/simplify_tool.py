# SPDX-License-Identifier: MIT
"""Simplify tool — Select-like behaviour with Shift+LPM = draft / apply.

Workflow:
* Plain LPM / RPM / drag / keyboard: inherited from :class:`SelectTool`
  (normal selection, rubber-band, move, etc.).
* Shift+LPM on a wire:
  - if no draft exists OR the draft is for a different wire → compute the
    simplified path and draw it as a dashed overlay (no command pushed).
  - if a draft already exists for THIS wire → commit it via
    ``ReplaceWireSegmentCommand`` (atomic, undoable).  Draft cleared.
* ESC, switching tool, clicking off a wire, or any non-Shift click clears
  the draft.

This SHADOWS the parent's Shift+LPM relation-select semantics ONLY when
the hit target is a wire — clicks on components, groups or empty space
fall through to the parent.
"""

from __future__ import annotations

import uuid

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPen
from PySide6.QtWidgets import QGraphicsLineItem

from protoboard.domain.commands.component_cmds import ReplaceWireFragmentsCommand
from protoboard.domain.layout.wire import Wire
from protoboard.domain.layout.wire_utils import rebuild_fragments_from_path
from protoboard.geometry import Point
from protoboard.gui.tools.select_tool import SelectTool
from protoboard.services.routing.postprocess import (
    SimplifyMode,
    generic_simplify_wire,
)


_PREVIEW_WIDTH = 0.12


def compute_simplified_wire(
    session: object,
    wire_id: uuid.UUID,
    *,
    mode: SimplifyMode | None = None,
) -> Wire | None:
    """Compute the simplified path for *wire_id* without applying it.

    Returns the new ``Wire`` (with a fresh id) when a shortening is
    available, or ``None`` when ``generic_simplify_wire`` could not shorten
    the path further.  Callers wanting the raw path use ``result.path``.
    """
    layout = session.layout  # type: ignore[attr-defined]
    wire = next((w for w in layout.wires if w.id == wire_id), None)
    if wire is None:
        return None

    if mode is None:
        rs = session.routing_settings  # type: ignore[attr-defined]
        mode = getattr(rs, "simplify_mode", SimplifyMode.BARE_PARITY)
    assert mode is not None

    # Pin positions across all components (Steiner-preservation set).
    pin_positions: set[Point] = set()
    for comp_id in layout.placements:
        for _pn, pos in session.iter_pin_positions(comp_id):  # type: ignore[attr-defined]
            pin_positions.add(pos)

    # pad → net id for every pad whose pin is assigned to a schematic net.
    pad_to_net: dict[Point, uuid.UUID] = {}
    for net_obj in session.schematic.nets.values():  # type: ignore[attr-defined]
        for ref in net_obj.pins:
            pos = session.get_pin_position(ref.component_id, ref.pin_number)  # type: ignore[attr-defined]
            if pos is not None:
                pad_to_net.setdefault(pos, net_obj.id)

    from protoboard.domain.layout.wire_utils import board_pads_for_side
    all_board_pads = (
        board_pads_for_side(session.board, "top")  # type: ignore[attr-defined]
        | board_pads_for_side(session.board, "bottom")  # type: ignore[attr-defined]
    )

    # Map each wire id → its net id (first pad-pin hit on the wire's path).
    wire_to_net: dict[uuid.UUID, uuid.UUID | None] = {}
    for w in layout.wires:
        wnet: uuid.UUID | None = None
        for pt in w.path:
            if pt in pad_to_net:
                wnet = pad_to_net[pt]
                break
        wire_to_net[w.id] = wnet

    pad_index = session.pad_index()  # type: ignore[attr-defined]
    others = [w for w in layout.wires if w.id != wire_id]
    new_wire = generic_simplify_wire(
        wire,
        other_wires=others,
        pin_positions=pin_positions,
        pad_to_net=pad_to_net,
        mode=mode,
        board_pads=all_board_pads,
        wire_net_id=wire_to_net.get(wire_id),
        other_wire_nets=wire_to_net,
        pin_net_map=pad_to_net,
        pad_index=pad_index,
    )
    if new_wire is wire or new_wire.path == wire.path:
        return None
    return new_wire


class SimplifyTool(SelectTool):
    """SelectTool extension with Shift+LPM draft/apply on wires."""

    def __init__(self) -> None:
        super().__init__()
        self._draft_wire_id: uuid.UUID | None = None
        self._draft_path: list[Point] | None = None
        self._draft_items: list[QGraphicsLineItem] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def deactivate(self) -> None:
        self._clear_draft()
        super().deactivate()

    def on_scene_cleared(self) -> None:
        # Scene items were destroyed under us — drop dangling refs.
        self._draft_items.clear()
        self._draft_wire_id = None
        self._draft_path = None
        super().on_scene_cleared()

    # ------------------------------------------------------------------
    # Input handling
    # ------------------------------------------------------------------

    def on_press(self, event: QMouseEvent, scene_pt: QPointF) -> bool:
        # Shift+LPM on a wire → draft or apply (depending on whether the
        # draft already exists for this wire).
        if (
            event.button() == Qt.MouseButton.LeftButton
            and bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        ):
            wire_id = self._hit_test_wire(scene_pt)
            if wire_id is not None:
                if self._draft_wire_id == wire_id and self._draft_path is not None:
                    return self._apply_simplify(wire_id)
                return self._preview_simplify(wire_id)
        # Any non-(Shift+LPM-on-wire) interaction discards the draft and
        # falls through to the parent SelectTool.
        self._clear_draft()
        return super().on_press(event, scene_pt)

    def on_key(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self._clear_draft()
        super().on_key(event)

    # ------------------------------------------------------------------
    # Preview + apply
    # ------------------------------------------------------------------

    def _preview_simplify(self, wire_id: uuid.UUID) -> bool:
        new_wire = compute_simplified_wire(self._session, wire_id)
        self._clear_draft()
        if new_wire is None:
            self._status_msg("Simplify: nothing to shorten on this wire.")
            return True
        self._draft_wire_id = wire_id
        self._draft_path = list(new_wire.path)
        self._draw_draft_overlay(self._draft_path)
        self._status_msg(
            "Simplify: draft ready — Shift+LPM again on the wire to apply,"
            " ESC or LPM elsewhere to cancel."
        )
        return True

    def _apply_simplify(self, wire_id: uuid.UUID) -> bool:
        if self._draft_path is None:
            return True
        new_path = list(self._draft_path)
        old_wire = next(
            (w for w in self._session.layout.wires if w.id == wire_id),
            None,
        )
        if old_wire is None:
            self._clear_draft()
            return True
        # Ensure the wire has knots before issuing a v2 fragment edit.  Most
        # runtime wires already have a/b (set on AddWireCommand or by the
        # load_project migration); test-built wires inserted directly into
        # layout.wires don't, so we backfill lazily here.
        self._session.layout.ensure_wire_knots(old_wire)
        # Simplify preserves per-fragment side topology — same-side wires
        # collapse into one fragment; multi-fragment wires keep their split
        # at the same via positions, which simplify never crosses.
        new_fragments = rebuild_fragments_from_path(old_wire, new_path)
        cmd = ReplaceWireFragmentsCommand(
            wire_id=wire_id,
            new_fragments=tuple(new_fragments),
        )
        self._session.stack.execute(cmd, self._session)
        self._session.layout_changed.emit()
        self._clear_draft()
        self._status_msg("Simplify: applied.")
        return True

    # ------------------------------------------------------------------
    # Overlay drawing
    # ------------------------------------------------------------------

    def _draw_draft_overlay(self, path: list[Point]) -> None:
        scene = self._view.scene()
        if scene is None:
            return
        color = QColor(self._theme().routing_preview_color)
        pen = QPen(color, _PREVIEW_WIDTH)
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setCosmetic(False)
        for a, b in zip(path, path[1:]):
            line = QGraphicsLineItem(a.x, a.y, b.x, b.y)
            line.setPen(pen)
            line.setZValue(1000.0)
            scene.addItem(line)
            self._draft_items.append(line)

    def _clear_draft(self) -> None:
        scene = self._view.scene() if self._view is not None else None
        for item in self._draft_items:
            if scene is not None:
                try:
                    scene.removeItem(item)
                except RuntimeError:
                    pass  # C++ object already gone
        self._draft_items.clear()
        self._draft_wire_id = None
        self._draft_path = None
