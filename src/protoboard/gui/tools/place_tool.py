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

import uuid
from enum import StrEnum
from typing import cast

import structlog
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QGraphicsEllipseItem, QGraphicsItem, QGraphicsLineItem

from protoboard.domain.commands.component_cmds import PlaceComponentCommand
from protoboard.domain.components.definition import ComponentDefinition, PinBendSpec
from protoboard.domain.components.footprints import Footprint
from protoboard.domain.components.package import resolve_component_package
from protoboard.domain.components.package.adapter import PackageFootprint
from protoboard.domain.components.package.reach import effective_bend_specs
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.placement import ComponentPlacement, flip_component, pin_world_position, unrotate
from protoboard.geometry import Point, Vector
from protoboard.gui.board_view_tool import _Tool
from protoboard.gui.placement_conflict import check_and_resolve
from protoboard.gui.renderers.component_renderer import make_ghost_item

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGraphicsItemGroup, QMainWindow

from protoboard.services.debug_trace import traced

_log = structlog.get_logger(__name__)
_REACH_PAD_RADIUS = 0.18

# Namespaced key actions PlaceTool owns.  All inherit the generic global
# defaults (rotate ``R`` / ``Shift+R``, flip ``Alt+R``, re-drag ``Ctrl+R``):
# while placing, those globals route through the (read-only) SelectTool and
# no-op, so PlaceTool claims them and drives its own ghost transform.
# Pin 1 anchors the placement: it lands on the first click and is never the
# dragged pin (see on_press). Pins are numbered from 1.
_ANCHOR_PIN = 1

_ACT_ROTATE_CW = "place-tool.action.rotate_cw"
_ACT_ROTATE_CCW = "place-tool.action.rotate_ccw"
_ACT_FLIP = "place-tool.action.flip"
_ACT_REDRAG = "place-tool.action.redrag"
_ACT_CANCEL = "place-tool.action.cancel"
_ACT_CONFIRM = "place-tool.action.confirm"


class PhaseEnum(StrEnum):
    ANCHOR = "ancor"
    DRAG_PIN = "drag_pin"
    FLOATING = "floating"

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

        self._phase: PhaseEnum = PhaseEnum.ANCHOR
        self._anchor_pos: Point | None = None
        self._pin_offsets: dict[int, Vector] = {}
        self._drag_pin_queue: list[int] = []
        self._current_drag_pin: int | None = None
        # Reach overlay items
        self._reach_items: list[QGraphicsItem] = []
        # Floating state (placed but not committed)
        self._pending_inst: object = None
        self._pending_placement: object = None
        self._pending_wire_cmds: list[object] = []

    @traced()
    def _is_phase(self, phase: PhaseEnum) -> bool:
        return self._phase == phase

    @traced()
    def _set_phase(self, phase: PhaseEnum) -> PhaseEnum:
        old_phase = self._phase
        self._phase = phase
        return old_phase

    @traced()
    def activate(self, session: object, view: object) -> None:
        super().activate(session, view)  # type: ignore[arg-type]
        self._reset_phase()
        self._rebuild_ghost()

    @traced()
    def deactivate(self) -> None:
        if self._is_phase(PhaseEnum.FLOATING):
            self._cancel_floating_place()
        self._clear_reach_overlay()
        self._remove_ghost()
        self._reset_phase()

    # ------------------------------------------------------------------
    # Bend-spec resolution
    # ------------------------------------------------------------------

    @traced()
    def _bend_specs(self, defn: ComponentDefinition) -> dict[int, PinBendSpec]:
        """Effective per-pin bend specs for the part being placed.

        The **chosen footprint variant** drives the specs: a part whose bodies
        live in a ``footprints:`` list (e.g. ``R`` placed as ``SMD-0805``)
        resolves its rigid / SMD king-move spec, not the legacy axial
        ``bendable_pins``. During placement no real instance exists yet, so we
        pass the picker's ``package_variant`` address (a netlist-imported
        ``existing_instance`` supplies it via ``instance`` instead) plus the
        session catalogue. With no chosen variant the resolver falls through to
        ``defn``'s own package / ``bendable_pins``, so THT parts are unaffected.
        Shared with the properties panel and the select-tool re-drag via
        :func:`effective_bend_specs`."""
        inst = (
            self._existing_instance
            if isinstance(self._existing_instance, ComponentInstance)
            else None
        )
        families = getattr(self._session, "package_families", None)
        return effective_bend_specs(
            defn, inst, families, package_variant=self._package_variant
        )

    # ------------------------------------------------------------------
    # Mouse events
    # ------------------------------------------------------------------

    @traced()
    def on_press(self, event: QMouseEvent, scene_pt: QPointF) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        snapped = self._view.snap(scene_pt)

        if self._is_phase(PhaseEnum.FLOATING):
            self._commit_floating_place()
            return True

        defn = self._session.components.get(self._definition_ref)
        specs = self._bend_specs(defn) if defn else {}
        bendable = sorted(specs.keys())

        if self._is_phase(PhaseEnum.ANCHOR):
            if not bendable:
                self._commit(snapped, pin_offsets={})
            else:
                # Pin 1 is the ANCHOR: it lands exactly on the click and is
                # never dragged. Only the OTHER pins enter the drag pipeline.
                # The drag queue is therefore the bendable pins minus pin 1.
                #   * SMD: bendable is [2] (pin 1 is a rigid anchor, not free),
                #     so the queue stays [2] — pin 2 is dragged to a neighbour.
                #   * Axial (R, diode): both leads are free so bendable is
                #     [1, 2]; dropping pin 1 leaves [2]. Without this, pin 1 was
                #     itself dragged and — bound by its r_min reach disc — could
                #     not stick to the clicked pad (it snapped out to the ring).
                self._anchor_pos = snapped
                self._pin_offsets = {
                    p: specs[p].default_offset for p in bendable
                }
                self._drag_pin_queue = [p for p in bendable if p != _ANCHOR_PIN]
                if self._drag_pin_queue:
                    self._current_drag_pin = self._drag_pin_queue.pop(0)
                    self._set_phase(PhaseEnum.DRAG_PIN)
                    self._rebuild_ghost_at_anchor()
                    self._update_reach_overlay()
                else:
                    # Only the anchor pin is bendable — nothing to drag; commit
                    # at its default offset (it sits on the clicked pad).
                    self._commit(snapped, pin_offsets=dict(self._pin_offsets))
            return True

        if self._is_phase(PhaseEnum.DRAG_PIN):
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

    @traced()
    def on_move(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        snapped = self._view.snap(scene_pt)
        if self._is_phase(PhaseEnum.ANCHOR):
            self._last_cursor_pos = snapped
            self._move_ghost(snapped.x, snapped.y)
        elif self._is_phase(PhaseEnum.DRAG_PIN):
            assert self._anchor_pos is not None and self._current_drag_pin is not None
            new_off = self._compute_drag_offset(snapped)
            if new_off is not None:
                self._pin_offsets[self._current_drag_pin] = new_off
                self._rebuild_ghost_at_anchor()
            self._update_reach_line(snapped)

    @traced()
    def key_actions(self) -> dict[str, str | None]:
        """Transform keys PlaceTool drives during placement (all inherit global).

        While the tool is active the global rotate / flip / re-drag actions run
        through the read-only SelectTool and no-op, so PlaceTool claims them to
        transform its ghost.  ``None`` inherits the generic global default, so
        rebinding any of them is honoured by both the veto and the handler.
        """
        return {
            _ACT_ROTATE_CW: None,
            _ACT_ROTATE_CCW: None,
            _ACT_FLIP: None,
            _ACT_REDRAG: None,
        }

    @traced()
    def wants_key(self, event: QKeyEvent) -> bool:
        """Claim the transform keys so the (no-op while placing) globals don't
        shadow them.  Decided dynamically through the KeyMap — no key literals."""
        return (self._key_matches(event, _ACT_ROTATE_CW)
                or self._key_matches(event, _ACT_ROTATE_CCW)
                or self._key_matches(event, _ACT_FLIP)
                or self._key_matches(event, _ACT_REDRAG)
                or self._key_matches(event, _ACT_CONFIRM)
                or self._key_matches(event, _ACT_CANCEL))

    @traced()
    def on_key(self, event: object) -> None:
        if not isinstance(event, QKeyEvent):
            return
        key = event.key()

        # Floating phase: Enter commits, Esc cancels.
        if self._is_phase(PhaseEnum.FLOATING):
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._commit_floating_place()
                event.accept()
            elif key == Qt.Key.Key_Escape:
                self._cancel_floating_place()
                self._rebuild_ghost()
                event.accept()
            return

        if self._key_matches(event, _ACT_FLIP) and self._is_phase(PhaseEnum.DRAG_PIN):
            # Flip pin1/pin2 — anchor moves to old pin2 pad.
            defn = self._session.components.get(self._definition_ref)
            fp = self._resolved_footprint(defn) if defn else None
            if (defn and fp is not None and fp.pin_count() == 2
                    and self._anchor_pos is not None):
                import uuid as _uuid
                tmp_pl = ComponentPlacement(
                    component_id=_uuid.UUID(int=0),
                    position=self._anchor_pos,
                    rotation=self._rotation,
                    side="top",
                    pin_offsets=dict(self._pin_offsets),
                )
                flipped = flip_component(fp, tmp_pl)
                self._anchor_pos  = flipped.position
                self._rotation    = flipped.rotation
                self._pin_offsets = flipped.pin_offsets
                self._rebuild_ghost_at_anchor()
                self._update_reach_overlay()
            event.accept()

        elif self._key_matches(event, _ACT_ROTATE_CW) or self._key_matches(
            event, _ACT_ROTATE_CCW
        ):
            delta = -90 if self._key_matches(event, _ACT_ROTATE_CCW) else 90
            self._rotation = (self._rotation + delta) % 360
            if self._is_phase(PhaseEnum.DRAG_PIN):
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

        elif self._key_matches(event, _ACT_REDRAG):
            # Re-drag — restart drag for the current bendable pin from default.
            defn = self._session.components.get(self._definition_ref)
            specs = self._bend_specs(defn) if defn else {}
            if not specs:
                return
            bendable = sorted(specs.keys())
            if self._is_phase(PhaseEnum.ANCHOR) and self._anchor_pos is None:
                return  # nothng to re-drag yet
            if self._is_phase(PhaseEnum.ANCHOR):
                # anchor already fixed (e.g. after phase reset), start drag
                self._pin_offsets = {
                    p: specs[p].default_offset for p in bendable
                }
                self._drag_pin_queue = bendable[1:]
                self._current_drag_pin = bendable[0]
                self._set_phase(PhaseEnum.DRAG_PIN)
            else:
                # Reset current drag pin to default
                assert self._current_drag_pin is not None
                spec = specs.get(self._current_drag_pin)
                if spec:
                    self._pin_offsets[self._current_drag_pin] = spec.default_offset
            self._rebuild_ghost_at_anchor()
            self._update_reach_overlay()
            event.accept()

        elif key == Qt.Key.Key_Escape and self._is_phase(PhaseEnum.DRAG_PIN):
            self._clear_reach_overlay()
            self._reset_phase()
            self._rebuild_ghost()
            event.accept()

    @traced()
    def on_scene_cleared(self) -> None:
        self._ghost = None
        self._reach_items = []
        self._rebuild_ghost()

    # ------------------------------------------------------------------
    # Ghost helpers
    # ------------------------------------------------------------------

    @traced()
    def _rebuild_ghost(self) -> None:
        self._remove_ghost()
        defn = self._session.components.get(self._definition_ref)
        if defn is None:
            return
        offsets: dict[int, Vector] = {}
        if self._is_phase(PhaseEnum.DRAG_PIN):
            offsets = dict(self._pin_offsets)
        else:
            specs = self._bend_specs(defn)
            if specs:
                offsets = {p: spec.default_offset for p, spec in specs.items()}
        # The ghost resolves its body from the chosen variant against the
        # catalogue, so the theme it draws with must carry the session's
        # package_families (the view's theme override may not). Without this the
        # resolve falls through to the default body — the candy-disc bug.
        import dataclasses
        families = getattr(self._session, "package_families", None)
        theme = dataclasses.replace(
            self._theme(), package_families=families or {})
        ghost = make_ghost_item(
            defn, rotation=self._rotation, theme=theme, pin_offsets=offsets,
            package_variant=self._package_variant,
        )
        # Z=20 puts the ghost above all back-side scene items (Z<Z_BOARD=3.0)
        # so BoardView._ensure_back_cache doesn't bake it into the back-side
        # pixmap and hide it.  Without this the ghost stays "stuck" at (0,0)
        # because setVisible(False) is applied to the back-cached item and
        # setPos() updates pose on an invisible object.
        ghost.setZValue(20.0)
        self._view.scene().addItem(ghost)
        self._ghost = ghost

    @traced()
    def _rebuild_ghost_at_anchor(self) -> None:
        self._rebuild_ghost()
        if self._anchor_pos is not None and self._ghost is not None:
            self._ghost.setPos(self._anchor_pos.x, self._anchor_pos.y)

    @traced()
    def _remove_ghost(self) -> None:
        if self._ghost is not None:
            self._view.scene().removeItem(self._ghost)
            self._ghost = None

    @traced()
    def _move_ghost(self, x: float, y: float) -> None:
        if self._ghost is not None:
            self._ghost.setPos(x, y)
            # setPos already schedules a repaint via Qt's
            # FullViewportUpdate mode; no explicit update() needed once
            # the back-cache filter no longer hides the ghost's children.

    # ------------------------------------------------------------------
    # Reach overlay
    # ------------------------------------------------------------------

    @traced()
    def _update_reach_overlay(self) -> None:
        """Draw green dots on the pads the active pin may land on.

        Free pins highlight every board pad within reach; a discrete pin (an SMD
        chip's second terminal) highlights its mount-candidate pads instead, so
        the orthogonal + diagonal mounting options are visible during the drag.
        """
        self._clear_reach_overlay()
        if self._phase != "drag_pin" or self._anchor_pos is None:
            return
        defn = self._session.components.get(self._definition_ref)
        if defn is None or self._current_drag_pin is None:
            return
        spec = self._bend_specs(defn).get(self._current_drag_pin)
        if spec is None:
            return

        from PySide6.QtGui import QBrush, QPen
        r = _REACH_PAD_RADIUS
        color = QColor(self._theme().placement_reach_color)

        def _dot(pt: Point) -> None:
            item = QGraphicsEllipseItem(pt.x - r, pt.y - r, 2 * r, 2 * r)
            item.setBrush(QBrush(color))
            item.setPen(QPen(Qt.GlobalColor.transparent, 0))
            item.setZValue(20.0)
            self._view.scene().addItem(item)
            self._reach_items.append(item)

        if spec.is_free():
            origin = self._anchor_pos  # placement.position = component (0,0)
            for pt in self._view.board_pad_positions():
                if spec.is_reachable_from_origin(origin, pt):
                    _dot(pt)
            return

    @traced()
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

    @traced()
    def _clear_reach_overlay(self) -> None:
        for item in self._reach_items:
            self._view.scene().removeItem(item)
        self._reach_items = []

    # ------------------------------------------------------------------
    # Offset computation
    # ------------------------------------------------------------------

    @traced()
    def _resolved_footprint(self, defn: ComponentDefinition) -> "Footprint":
        """The footprint of the *chosen* package variant, not the definition's.

        The ghost renders the picked ``package_variant``'s body and pin spacing
        (see ``make_ghost_item``); the drag-pin baseline, flip and pin-count
        logic must use that SAME geometry, or the offset is measured against one
        footprint and drawn against another — the body drifts, worse with
        rotation. Falls back to the definition default when no variant resolves."""
        if self._package_variant is not None:
            inst = ComponentInstance.new(
                definition_ref=defn.name, refdes="?", value="",
                package_variant=self._package_variant,
            )
            families = getattr(self._session, "package_families", None) or {}
            pkg = resolve_component_package(defn, inst, families)
            if pkg is not None:
                return PackageFootprint(pkg)
        return defn.default_footprint()

    @traced()
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
        return pin_world_position(self._resolved_footprint(defn), temp,
                                  pin_number)

    @traced()
    def _compute_drag_offset(self, cursor: Point) -> Vector | None:
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
            world_delta = snapped - baseline
            local_off = unrotate(world_delta, self._rotation)
            return local_off

        return None

    # ------------------------------------------------------------------
    # Commit
    # ------------------------------------------------------------------

    @traced()
    def _commit(self, position: Point, pin_offsets: dict[int, Vector]) -> None:
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

        # Bind the instance to its definition + catalogue so its chosen variant
        # footprint resolves now — the conflict check runs *before* the instance
        # enters session.instances, so without binding it would fall back to the
        # definition default footprint (wrong pin spacing → bogus body overlap /
        # off-pad pins, blocking valid placements).
        if defn is not None:
            families = getattr(self._session, "package_families", None)
            inst = inst.bind(defn, families)

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
                instance=inst,
            )
            if resolved is None:
                _log.info("place.blocked", definition=self._definition_ref, pos=(position.x, position.y))
                return
            wire_cmds = list(resolved.wire_cmds)
            has_wire_conflict = bool(wire_cmds)
            placement = resolved.placement

        from protoboard.gui.app_settings import load_app_settings
        auto_accept = load_app_settings().auto_accept_placement

        if auto_accept and not has_wire_conflict:
            self._do_place(inst, placement, wire_cmds)
        else:
            # Enter floating — show component on board, wait for Enter/Esc.
            self._reset_pending(inst, placement, wire_cmds)
            self._session.layout.placements[inst.id] = placement
            self._session.instances[inst.id] = inst
            self._set_phase(PhaseEnum.FLOATING)
            self._session.layout_changed.emit()
            win = self._view.window() if self._view is not None else None
            sb = cast(QMainWindow, win).statusBar() if win is not None else None
            if sb is not None:
                sb.showMessage("[Enter] Accept placement  ·  [Esc] Cancel", 0)

    @traced()
    def _do_place(
        self, inst: object, placement: object, wire_cmds: list[object]
    ) -> None:
        """Execute PlaceComponentCommand (and any wire commands) immediately."""
        from protoboard.domain.commands.component_cmds import PlaceComponentCommand
        from protoboard.domain.commands.base import ComponentState, MacroCommand

        state = cast(ComponentState, self._session)
        cmd = PlaceComponentCommand(
            component_id=inst.id,  # type: ignore[union-attr]
            placement=placement,  # type: ignore[arg-type]
            instance=inst,  # type: ignore[arg-type]
        )
        if wire_cmds:
            all_cmds = [cmd] + wire_cmds  # type: ignore[operator]
            macro = MacroCommand(all_cmds, description="Place component")  # type: ignore[arg-type]
            self._session.stack.execute(macro, state)
        else:
            self._session.stack.execute(cmd, state)
        _log.info("place.committed",
                  definition=self._definition_ref,
                  refdes=inst.refdes,
                  pos=(placement.position.x,
                       placement.position.y))  # type: ignore[union-attr]
        self._session.layout_changed.emit()

    @traced()
    def _reset_pending(self,
                       inst: object = None,
                       placement: object = None,
                       wire_cmds: list[object] | None = None):
        self._pending_inst = inst
        self._pending_placement = placement
        self._pending_wire_cmds = wire_cmds or []

    @traced()
    def _commit_floating_place(self) -> None:
        """Commit the pending floating placement."""
        if self._pending_inst is None or self._pending_placement is None:
            return

        self._do_place(self._pending_inst, self._pending_placement,
                       self._pending_wire_cmds)
        self._reset_pending()
        self._set_phase(PhaseEnum.ANCHOR)
        win = self._view.window() if self._view is not None else None
        sb = cast(QMainWindow, win).statusBar() if win is not None else None
        if sb is not None:
            sb.clearMessage()

    @traced()
    def _cancel_floating_place(self) -> None:
        """Remove the pending floating placement from layout without a command."""
        if self._pending_inst is not None:
            inst_id = self._pending_inst.id  # type: ignore[union-attr]
            self._session.layout.placements.pop(inst_id, None)
            self._session.instances.pop(inst_id, None)
            self._session.layout_changed.emit()
        self._reset_pending()
        self._set_phase(PhaseEnum.ANCHOR)
        win = self._view.window() if self._view is not None else None
        sb = cast(QMainWindow, win).statusBar() if win is not None else None
        if sb is not None:
            sb.clearMessage()

    @traced()
    def _reset_phase(self) -> None:
        self._set_phase(PhaseEnum.ANCHOR)
        self._anchor_pos = None
        self._pin_offsets = {}
        self._drag_pin_queue = []
        self._current_drag_pin = None

    # ------------------------------------------------------------------

    @traced()
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
