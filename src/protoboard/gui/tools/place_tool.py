# SPDX-License-Identifier: MIT
"""Place component tool — click to place the selected component.

Flow for components without bendable_pins:
  - Move mouse → ghost follows cursor (pin 1 = snap point).
  - Left-click → commit placement.

Flow for components with bendable_pins (two-phase):
  Phase ANCHOR:
    - Ghost follows cursor (pin 1 = snap point, pins use default offsets).
    - Left-click → fix pin 1 position, enter PIN_DRAG phase.
    - Ctrl+R → also enters PIN_DRAG (re-drag after previous placement).
  Phase PIN_DRAG (one step per bendable pin, in pin-number order):
    - Pin 1 is frozen.
    - For is_free specs: cursor snaps to any board pad in reach; ring overlay
      highlights reachable pads.
    - For discrete specs: cursor snaps to nearest allowed offset.
    - Ghost updates live.
    - Left-click → confirm this pin's offset, advance to next pin (or commit).
    - Ctrl+R → reset current pin to default_offset and restart drag for it.
    - Esc → return to ANCHOR phase (discard offsets, resume normal ghost).
    - R / Shift+R → standard rotation ±90° (pin_offsets unchanged).
"""

from __future__ import annotations

import math
import uuid
from typing import Literal

import structlog
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QGraphicsEllipseItem, QGraphicsItem, QGraphicsLineItem

from protoboard.domain.commands.component_cmds import PlaceComponentCommand
from protoboard.domain.components.definition import ComponentDefinition, PinBendSpec
from protoboard.domain.components.package.reach import package_to_bend_specs
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.placement import ComponentPlacement, flip_component, pin_world_position, unrotate
from protoboard.geometry import Point
from protoboard.gui.board_view import _Tool
from protoboard.gui.placement_conflict import check_and_resolve
from protoboard.gui.renderers.component_renderer import make_ghost_item

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGraphicsItemGroup

_log = structlog.get_logger(__name__)
_REACH_PAD_RADIUS = 0.18


class PlaceTool(_Tool):
    """Places a component at the snapped cursor position on left-click.

    Two construction modes:
    - ``PlaceTool(definition_ref)``      — create a brand-new instance from library.
    - ``PlaceTool(definition_ref, existing_instance=inst)`` — place a specific
      instance imported from netlist (keeps its UUID, refdes, value, nets).
    """

    def __init__(
        self,
        definition_ref: str,
        existing_instance: object | None = None,
        pin_variant: str | None = None,
        package_variant: str | None = None,
    ) -> None:
        super().__init__()
        self._definition_ref = definition_ref
        # When placing a netlist-imported instance, keep its identity.
        self._existing_instance = existing_instance  # ComponentInstance | None
        self._pin_variant = pin_variant
        self._package_variant = package_variant
        self._rotation = 0
        self._ghost: QGraphicsItemGroup | None = None
        # Last snapped cursor position in the anchor phase — kept so the
        # ghost stays under the cursor across R / Shift+R rotations.
        self._last_cursor_pos: Point | None = None
        # Two-phase state
        self._phase: Literal["anchor", "drag_pin", "floating"] = "anchor"
        self._anchor_pos: Point | None = None
        self._pin_offsets: dict[int, Point] = {}
        self._drag_pin_queue: list[int] = []
        self._current_drag_pin: int | None = None
        # Reach overlay items
        self._reach_items: list[QGraphicsItem] = []
        # Floating state (placed but not committed)
        self._pending_inst: object = None
        self._pending_placement: object = None
        self._pending_wire_cmds: list[object] = []

    def activate(self, session: object, view: object) -> None:
        super().activate(session, view)  # type: ignore[arg-type]
        self._reset_phase()
        self._rebuild_ghost()

    def deactivate(self) -> None:
        if self._phase == "floating":
            self._cancel_floating_place()
        self._clear_reach_overlay()
        self._remove_ghost()
        self._reset_phase()

    # ------------------------------------------------------------------
    # Bend-spec resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _bend_specs(defn: ComponentDefinition) -> dict[int, PinBendSpec]:
        """Effective per-pin bend specs: from the parametric package when set,
        otherwise the legacy ``bendable_pins``. Single point of migration —
        the rest of the tool keeps its PinBendSpec logic unchanged."""
        if defn.package is not None:
            return package_to_bend_specs(defn.package)
        return defn.bendable_pins

    # ------------------------------------------------------------------
    # Mouse events
    # ------------------------------------------------------------------

    def on_press(self, event: QMouseEvent, scene_pt: QPointF) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        snapped = self._view.snap(scene_pt)

        if self._phase == "floating":
            self._commit_floating_place()
            return True

        defn = self._session.components.get(self._definition_ref)
        specs = self._bend_specs(defn) if defn else {}
        bendable = sorted(specs.keys())

        if self._phase == "anchor":
            if not bendable:
                self._commit(snapped, pin_offsets={})
            else:
                self._anchor_pos = snapped
                self._pin_offsets = {
                    p: specs[p].default_offset for p in bendable
                }
                self._drag_pin_queue = bendable[1:]
                self._current_drag_pin = bendable[0]
                self._phase = "drag_pin"
                self._rebuild_ghost_at_anchor()
                self._update_reach_overlay()
            return True

        if self._phase == "drag_pin":
            assert self._anchor_pos is not None and self._current_drag_pin is not None
            new_off = self._compute_drag_offset(snapped)
            if new_off is not None:
                self._pin_offsets[self._current_drag_pin] = new_off
            if self._drag_pin_queue:
                self._current_drag_pin = self._drag_pin_queue.pop(0)
                self._rebuild_ghost_at_anchor()
                self._update_reach_overlay()
            else:
                self._clear_reach_overlay()
                self._commit(self._anchor_pos, pin_offsets=dict(self._pin_offsets))
                self._reset_phase()
            return True

        return False

    def on_move(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        snapped = self._view.snap(scene_pt)
        if self._phase == "anchor":
            self._last_cursor_pos = snapped
            self._move_ghost(snapped.x, snapped.y)
        elif self._phase == "drag_pin":
            assert self._anchor_pos is not None and self._current_drag_pin is not None
            new_off = self._compute_drag_offset(snapped)
            if new_off is not None:
                self._pin_offsets[self._current_drag_pin] = new_off
                self._rebuild_ghost_at_anchor()
            self._update_reach_line(snapped)

    def on_key(self, event: object) -> None:
        if not isinstance(event, QKeyEvent):
            return
        key = event.key()
        mods = event.modifiers()
        ctrl  = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        alt   = bool(mods & Qt.KeyboardModifier.AltModifier)

        # Floating phase: Enter commits, Esc cancels.
        if self._phase == "floating":
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._commit_floating_place()
                event.accept()
            elif key == Qt.Key.Key_Escape:
                self._cancel_floating_place()
                self._rebuild_ghost()
                event.accept()
            return

        if key == Qt.Key.Key_R and alt and not ctrl and self._phase == "drag_pin":
            # Alt+R: flip pin1/pin2 — anchor moves to old pin2 pad.
            defn = self._session.components.get(self._definition_ref)
            if defn and defn.footprint.pin_count() == 2 and self._anchor_pos is not None:
                import uuid as _uuid
                tmp_pl = ComponentPlacement(
                    component_id=_uuid.UUID(int=0),
                    position=self._anchor_pos,
                    rotation=self._rotation,
                    side="top",
                    pin_offsets=dict(self._pin_offsets),
                )
                flipped = flip_component(defn.footprint, tmp_pl)
                self._anchor_pos  = flipped.position
                self._rotation    = flipped.rotation
                self._pin_offsets = flipped.pin_offsets
                self._rebuild_ghost_at_anchor()
                self._update_reach_overlay()
            event.accept()

        elif key == Qt.Key.Key_R and not ctrl and not alt:
            delta = -90 if shift else 90
            self._rotation = (self._rotation + delta) % 360
            if self._phase == "drag_pin":
                self._rebuild_ghost_at_anchor()
                self._update_reach_overlay()
            else:
                # Rebuild rebakes the body shape for the new rotation, then
                # re-park the ghost under the cursor's last known position.
                self._rebuild_ghost()
                if self._last_cursor_pos is not None:
                    self._move_ghost(
                        self._last_cursor_pos.x, self._last_cursor_pos.y,
                    )
            event.accept()

        elif key == Qt.Key.Key_R and ctrl:
            # Ctrl+R — restart drag for the current bendable pin from default.
            defn = self._session.components.get(self._definition_ref)
            specs = self._bend_specs(defn) if defn else {}
            if not specs:
                return
            bendable = sorted(specs.keys())
            if self._phase == "anchor" and self._anchor_pos is None:
                return  # nothng to re-drag yet
            if self._phase == "anchor":
                # anchor already fixed (e.g. after phase reset), start drag
                self._pin_offsets = {
                    p: specs[p].default_offset for p in bendable
                }
                self._drag_pin_queue = bendable[1:]
                self._current_drag_pin = bendable[0]
                self._phase = "drag_pin"
            else:
                # Reset current drag pin to default
                assert self._current_drag_pin is not None
                spec = specs.get(self._current_drag_pin)
                if spec:
                    self._pin_offsets[self._current_drag_pin] = spec.default_offset
            self._rebuild_ghost_at_anchor()
            self._update_reach_overlay()
            event.accept()

        elif key == Qt.Key.Key_Escape and self._phase == "drag_pin":
            self._clear_reach_overlay()
            self._reset_phase()
            self._rebuild_ghost()
            event.accept()

    def on_scene_cleared(self) -> None:
        self._ghost = None
        self._reach_items = []
        self._rebuild_ghost()

    # ------------------------------------------------------------------
    # Ghost helpers
    # ------------------------------------------------------------------

    def _rebuild_ghost(self) -> None:
        self._remove_ghost()
        defn = self._session.components.get(self._definition_ref)
        if defn is None:
            return
        offsets: dict[int, Point] = {}
        if self._phase == "drag_pin":
            offsets = dict(self._pin_offsets)
        else:
            specs = self._bend_specs(defn)
            if specs:
                offsets = {p: spec.default_offset for p, spec in specs.items()}
        ghost = make_ghost_item(defn, rotation=self._rotation, pin_offsets=offsets)
        # Z=20 puts the ghost above all back-side scene items (Z<Z_BOARD=3.0)
        # so BoardView._ensure_back_cache doesn't bake it into the back-side
        # pixmap and hide it.  Without this the ghost stays "stuck" at (0,0)
        # because setVisible(False) is applied to the back-cached item and
        # setPos() updates pose on an invisible object.
        ghost.setZValue(20.0)
        self._view.scene().addItem(ghost)
        self._ghost = ghost

    def _rebuild_ghost_at_anchor(self) -> None:
        self._rebuild_ghost()
        if self._anchor_pos is not None and self._ghost is not None:
            self._ghost.setPos(self._anchor_pos.x, self._anchor_pos.y)

    def _remove_ghost(self) -> None:
        if self._ghost is not None:
            self._view.scene().removeItem(self._ghost)
            self._ghost = None

    def _move_ghost(self, x: float, y: float) -> None:
        if self._ghost is not None:
            self._ghost.setPos(x, y)
            # setPos already schedules a repaint via Qt's
            # FullViewportUpdate mode; no explicit update() needed once
            # the back-cache filter no longer hides the ghost's children.

    # ------------------------------------------------------------------
    # Reach overlay
    # ------------------------------------------------------------------

    def _update_reach_overlay(self) -> None:
        """Draw green dots on all board pads reachable by the active pin."""
        self._clear_reach_overlay()
        if self._phase != "drag_pin" or self._anchor_pos is None:
            return
        defn = self._session.components.get(self._definition_ref)
        if defn is None or self._current_drag_pin is None:
            return
        spec = self._bend_specs(defn).get(self._current_drag_pin)
        if spec is None or not spec.is_free():
            return

        origin = self._anchor_pos  # placement.position = component (0,0) = reach centre

        r = _REACH_PAD_RADIUS
        for pt in self._view.board_pad_positions():
            if spec.is_reachable_from_origin(origin, pt):
                item = QGraphicsEllipseItem(pt.x - r, pt.y - r, 2 * r, 2 * r)
                from PySide6.QtGui import QBrush, QPen
                item.setBrush(QBrush(QColor(self._theme().placement_reach_color)))
                item.setPen(QPen(Qt.GlobalColor.transparent, 0))
                item.setZValue(20.0)
                self._view.scene().addItem(item)
                self._reach_items.append(item)

    def _update_reach_line(self, cursor: Point) -> None:
        """Update the feedback line from pin baseline to cursor."""
        # Remove old line (last item if it's a line)
        if self._reach_items and isinstance(self._reach_items[-1], QGraphicsLineItem):
            self._view.scene().removeItem(self._reach_items.pop())

        if self._phase != "drag_pin" or self._anchor_pos is None:
            return
        defn = self._session.components.get(self._definition_ref)
        if defn is None or self._current_drag_pin is None:
            return
        spec = self._bend_specs(defn).get(self._current_drag_pin)
        if spec is None or not spec.is_free():
            return

        origin = self._anchor_pos  # component (0,0) = reach centre

        line = QGraphicsLineItem(origin.x, origin.y, cursor.x, cursor.y)
        from PySide6.QtGui import QPen
        pen = QPen(QColor(self._theme().placement_reach_line_color), 0.05)
        pen.setStyle(Qt.PenStyle.DashLine)
        line.setPen(pen)
        line.setZValue(20.0)
        self._view.scene().addItem(line)
        self._reach_items.append(line)

    def _clear_reach_overlay(self) -> None:
        for item in self._reach_items:
            self._view.scene().removeItem(item)
        self._reach_items = []

    # ------------------------------------------------------------------
    # Offset computation
    # ------------------------------------------------------------------

    def _pin_baseline_world(self, pin_number: int) -> Point | None:
        """World position of the pin ignoring any current offset."""
        if self._anchor_pos is None:
            return None
        defn = self._session.components.get(self._definition_ref)
        if defn is None:
            return None
        temp = ComponentPlacement(
            component_id=uuid.UUID(int=0),
            position=self._anchor_pos,
            rotation=self._rotation,
            side="top",
            pin_offsets={},
        )
        return pin_world_position(defn.footprint, temp, pin_number)

    def _compute_drag_offset(self, cursor: Point) -> Point | None:
        """Return the clamped/snapped local offset for the active drag pin."""
        assert self._anchor_pos is not None and self._current_drag_pin is not None
        defn = self._session.components.get(self._definition_ref)
        if defn is None:
            return None
        spec: PinBendSpec | None = self._bend_specs(defn).get(self._current_drag_pin)
        if spec is None:
            return None

        baseline = self._pin_baseline_world(self._current_drag_pin)
        if baseline is None:
            return None

        if spec.is_free():
            # Snap to nearest board pad; reject if outside reach from origin.
            snapped = self._view.snap(QPointF(cursor.x, cursor.y))
            if not spec.is_reachable_from_origin(self._anchor_pos, snapped):
                return None
            # Convert world position of snapped pad to local offset for pin.
            world_delta = Point(snapped.x - baseline.x, snapped.y - baseline.y)
            local_off = unrotate(world_delta, self._rotation)
            return local_off

        if spec.discrete:
            # Snap to nearest discrete option (in world space).
            best: Point = spec.discrete[0]
            best_dist = math.inf
            temp_no_off = ComponentPlacement(
                component_id=uuid.UUID(int=0),
                position=self._anchor_pos,
                rotation=self._rotation,
                side="top",
                pin_offsets={},
            )
            for pt in spec.discrete:
                candidate_pl = ComponentPlacement(
                    component_id=uuid.UUID(int=0),
                    position=self._anchor_pos,
                    rotation=self._rotation,
                    side="top",
                    pin_offsets={self._current_drag_pin: pt},
                )
                world_pos = pin_world_position(
                    defn.footprint, candidate_pl, self._current_drag_pin
                )
                d = math.hypot(cursor.x - world_pos.x, cursor.y - world_pos.y)
                if d < best_dist:
                    best, best_dist = pt, d
            return best

        return None

    # ------------------------------------------------------------------
    # Commit
    # ------------------------------------------------------------------

    def _commit(self, position: Point, pin_offsets: dict[int, Point]) -> None:
        _log.debug("place.attempt", definition=self._definition_ref, pos=(position.x, position.y), rotation=self._rotation)
        defn = self._session.components.get(self._definition_ref)

        # Netlist-imported instance: reuse its identity (UUID, refdes, value, nets).
        if self._existing_instance is not None:
            from protoboard.domain.components.instance import ComponentInstance as _CI
            inst: _CI = self._existing_instance  # type: ignore[assignment]
        else:
            # Pick the initial instance value per the library convention:
            # concrete parts (BC547, NE555, …) carry the part number as
            # their name, so ``defn.default_value`` is empty and we fall
            # back to ``defn.name`` — the rendered label naturally shows
            # the part number ("Q1 BC547").  Generic templates (R, C, L
            # …) set ``default_value: "?"`` to prompt the user
            # ("R23 ?").
            if defn is not None and defn.default_value:
                init_value = defn.default_value
            else:
                init_value = self._definition_ref
            inst = ComponentInstance.new(
                definition_ref=self._definition_ref,
                refdes=self._next_refdes(),
                value=init_value,
                pin_variant=self._pin_variant,
                package_variant=self._package_variant,
            )

        placement = ComponentPlacement(
            component_id=inst.id,
            position=position,
            rotation=self._rotation,
            side="top",
            pin_offsets=pin_offsets,
        )
        has_wire_conflict = False
        wire_cmds: list[object] = []
        if defn is not None:
            resolved = check_and_resolve(
                session=self._session,
                placement=placement,
                defn=defn,
                instance_id=inst.id,
                parent_widget=self._view.window(),
            )
            if resolved is None:
                _log.info("place.blocked", definition=self._definition_ref, pos=(position.x, position.y))
                return
            wire_cmds = getattr(resolved, "_wire_cmds", [])
            has_wire_conflict = bool(wire_cmds)
            placement = resolved

        from protoboard.gui.app_settings import load_app_settings
        auto_accept = load_app_settings().auto_accept_placement

        if auto_accept and not has_wire_conflict:
            self._do_place(inst, placement, wire_cmds)
        else:
            # Enter floating — show component on board, wait for Enter/Esc.
            self._pending_inst = inst
            self._pending_placement = placement
            self._pending_wire_cmds = wire_cmds
            self._session.layout.placements[inst.id] = placement
            self._session.instances[inst.id] = inst
            self._phase = "floating"
            self._session.layout_changed.emit()
            win = self._view.window() if self._view is not None else None
            sb = win.statusBar() if win is not None else None
            if sb is not None:
                sb.showMessage("[Enter] Accept placement  ·  [Esc] Cancel", 0)

    def _do_place(
        self, inst: object, placement: object, wire_cmds: list[object]
    ) -> None:
        """Execute PlaceComponentCommand (and any wire commands) immediately."""
        from protoboard.domain.commands.component_cmds import PlaceComponentCommand
        from protoboard.domain.commands.base import MacroCommand

        cmd = PlaceComponentCommand(
            component_id=inst.id,  # type: ignore[union-attr]
            placement=placement,  # type: ignore[arg-type]
            instance=inst,  # type: ignore[arg-type]
        )
        if wire_cmds:
            all_cmds = [cmd] + wire_cmds  # type: ignore[operator]
            macro = MacroCommand(all_cmds, description="Place component")  # type: ignore[arg-type]
            self._session.stack.execute(macro, self._session)
        else:
            self._session.stack.execute(cmd, self._session)
        _log.info("place.committed", definition=self._definition_ref,
                  refdes=inst.refdes, pos=(placement.position.x, placement.position.y))  # type: ignore[union-attr]
        self._session.layout_changed.emit()

    def _commit_floating_place(self) -> None:
        """Commit the pending floating placement."""
        if self._pending_inst is None or self._pending_placement is None:
            return
        self._do_place(self._pending_inst, self._pending_placement, self._pending_wire_cmds)
        self._pending_inst = None
        self._pending_placement = None
        self._pending_wire_cmds = []
        self._phase = "anchor"
        win = self._view.window() if self._view is not None else None
        sb = win.statusBar() if win is not None else None
        if sb is not None:
            sb.clearMessage()

    def _cancel_floating_place(self) -> None:
        """Remove the pending floating placement from layout without a command."""
        if self._pending_inst is not None:
            inst_id = self._pending_inst.id  # type: ignore[union-attr]
            self._session.layout.placements.pop(inst_id, None)
            self._session.instances.pop(inst_id, None)
            self._session.layout_changed.emit()
        self._pending_inst = None
        self._pending_placement = None
        self._pending_wire_cmds = []
        self._phase = "anchor"
        win = self._view.window() if self._view is not None else None
        sb = win.statusBar() if win is not None else None
        if sb is not None:
            sb.clearMessage()

    def _reset_phase(self) -> None:
        self._phase = "anchor"
        self._anchor_pos = None
        self._pin_offsets = {}
        self._drag_pin_queue = []
        self._current_drag_pin = None

    # ------------------------------------------------------------------

    def _next_refdes(self) -> str:
        defn = self._session.components.get(self._definition_ref)
        prefix = (
            defn.refdes_prefix if defn is not None else self._definition_ref[0].upper()
        )
        existing = {
            inst.refdes
            for inst in self._session.instances.values()
            if inst.refdes.startswith(prefix)
        }
        n = 1
        while f"{prefix}{n}" in existing:
            n += 1
        return f"{prefix}{n}"
