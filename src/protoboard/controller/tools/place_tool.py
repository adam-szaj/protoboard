# SPDX-License-Identifier: MIT
"""Place component tool — click to place the selected component.

Flow for packages without bendable pins:
  - Move mouse → ghost follows cursor (pin 1 = snap point).
  - Left-click → commit placement.

Flow for packages with bendable pins (two-phase):
  Phase ANCHOR:
    - Ghost follows cursor (pin 1 = snap point, pins use default offsets).
    - Left-click → fix pin 1 position, enter PIN_DRAG phase.
    - Ctrl+R → also enters PIN_DRAG (re-drag after previous placement).
  Phase PIN_DRAG (one step per bendable pin, in pin-number order):
    - Pin 1 is frozen.
    - For is_free specs: cursor snaps to any board pad in reach; ring overlay
      highlights reachable pads.
    - Ghost updates live.
    - Left-click → confirm this pin's offset, advance to next pin (or commit).
    - Ctrl+R → reset current pin to default_offset and restart drag for it.
    - Esc → return to ANCHOR phase (discard offsets, resume normal ghost).
    - R / Shift+R → standard rotation ±90° (pin_offsets unchanged).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

from protoboard.controller.app_settings import load_app_settings
from protoboard.controller.ports import LEFT_BUTTON, StatusEvent
from protoboard.controller.tools.base import ControllerTool
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import Footprint
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package import resolve_package_variant_footprint
from protoboard.domain.components.package.bend import PinBendSpec
from protoboard.domain.components.package.reach import effective_bend_specs
from protoboard.domain.layout.placement import (
    ComponentPlacement,
    flip_component,
    pin_world_position,
    unrotate,
)
from protoboard.geometry import Point, Vector
from protoboard.instrumentation.debug_trace import traced

if TYPE_CHECKING:
    from protoboard.controller.editing import EditingContext
    from protoboard.controller.editing.controller import ResolvedPlacement
    from protoboard.controller.editing.placement_conflict import PlacementPrompts
    from protoboard.controller.ports import PointerInput
    from protoboard.controller.tools.base import _ViewProto
    from protoboard.controller.tools.place_preview import PlacePreview
    from protoboard.controller.view_options.theme import Theme

_log = structlog.get_logger(__name__)

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
# Terminal ops resolve through the generic globals (KeyMap `final` actions
# reserve ``Return``/``Esc`` — no tool-specific action id may rebind them; see
# GLOBAL_ACTIONS in gui/keymap.py).  The old raw-key dispatch handled numpad
# Enter identically to Return and never handled Space; ``global.action.confirm``
# additionally accepts Space (a harmless widening — no key previously did
# anything else on Space in the floating phase).
_ACT_CANCEL = "global.action.cancel"
_ACT_CONFIRM = "global.action.confirm"


class PhaseEnum(StrEnum):
    ANCHOR = "ancor"
    DRAG_PIN = "drag_pin"
    FLOATING = "floating"


class PlaceTool(ControllerTool):
    """Places a component at the snapped cursor position on left-click.

    Two construction modes:
    - ``PlaceTool(definition_ref)``      — create a brand-new instance from library.
    - ``PlaceTool(definition_ref, existing_instance=inst)`` — place a specific
      instance imported from netlist (keeps its UUID, refdes, value, nets).
    """

    def __init__(
        self,
        definition_ref: str,
        existing_instance: ComponentInstance | None = None,
        package_variant: str | None = None,
        preview: PlacePreview | None = None,
        prompts: PlacementPrompts | None = None,
    ) -> None:
        super().__init__()
        self._definition_ref = definition_ref
        # When placing a netlist-imported instance, keep its identity.
        self._existing_instance = existing_instance  # ComponentInstance | None
        self._package_variant = package_variant
        self._rotation = 0
        # Ghost + reach overlay behind the semantic preview seam.  An
        # injected preview (tests) survives re-activation; the default Qt
        # one binds the current view's scene on activate().
        self._preview: PlacePreview | None = preview
        self._preview_injected = preview is not None
        # The pad/wire conflict decisions (qt-ports D4) — injected callables;
        # the default Qt prompts (modal dialogs) bind the view's window on
        # activate().
        self._prompts: PlacementPrompts | None = prompts
        self._prompts_injected = prompts is not None
        # Last snapped cursor position in the anchor phase — kept so the
        # ghost stays under the cursor across R / Shift+R rotations.
        self._last_cursor_pos: Point | None = None

        self._phase: PhaseEnum = PhaseEnum.ANCHOR
        self._anchor_pos: Point | None = None
        self._pin_offsets: dict[int, Vector] = {}
        self._drag_pin_queue: list[int] = []
        self._current_drag_pin: int | None = None
        # Floating state (placed but not committed): the controller's resolved
        # placement, held until the user accepts (Enter) or cancels (Esc).
        self._pending: ResolvedPlacement | None = None

    @property
    def preview(self) -> PlacePreview:
        assert self._preview is not None  # set by activate() / injection
        return self._preview

    @property
    def prompts(self) -> PlacementPrompts:
        assert self._prompts is not None  # set by activate() / injection
        return self._prompts

    @property
    def _view(  # pyright: ignore[reportIncompatibleVariableOverride]
        self,
    ) -> _ViewProto:
        """The bound view, narrowed away from None for the post-activate
        event handlers (same idiom as :attr:`preview`)."""
        assert self.__view is not None  # set by activate()
        return self.__view

    @_view.setter
    def _view(  # pyright: ignore[reportIncompatibleVariableOverride]
        self, value: _ViewProto | None
    ) -> None:
        self.__view = value

    def _theme_typed(self) -> Theme:
        """Return the active GUI theme or the headless default."""
        return self._theme()

    def _definition(self) -> ComponentDefinition | None:
        """Resolve the definition currently being placed."""
        return self._session.components.get(self._definition_ref)

    @traced()
    def _is_phase(self, phase: PhaseEnum) -> bool:
        return self._phase == phase

    @traced()
    def _set_phase(self, phase: PhaseEnum) -> PhaseEnum:
        old_phase = self._phase
        self._phase = phase
        return old_phase

    @traced()
    def activate(self, session: EditingContext, view: object) -> None:
        super().activate(session, view)  # type: ignore[arg-type]
        self.finish_activate()

    @traced()
    def finish_activate(self) -> None:
        """Reset the phase machine and bake the initial ghost.

        Split out of :meth:`activate` so a GUI subclass can call
        ``ControllerTool.activate`` (session/view bind only — via
        ``super().activate``), inject its default Qt preview / prompts, and
        only then call this — the ghost the phase-reset triggers needs
        ``self.preview`` to already exist.
        """
        self._reset_phase()
        self._rebuild_ghost()

    @traced()
    def deactivate(self) -> None:
        if self._is_phase(PhaseEnum.FLOATING):
            self._cancel_floating_place()
        self.preview.clear_reach()
        self.preview.clear_ghost()
        self._reset_phase()

    def can_exit(self) -> bool:
        """Cancel an unfinished placement before leaving the tool."""
        return self._is_phase(PhaseEnum.ANCHOR)

    # ------------------------------------------------------------------
    # Bend-spec resolution
    # ------------------------------------------------------------------

    @traced()
    def _bend_specs(self, defn: ComponentDefinition) -> dict[int, PinBendSpec]:
        """Effective per-pin bend specs for the part being placed.

        The **chosen footprint variant** drives the specs: a part whose bodies
        live in ``package_options:`` (e.g. ``R`` placed as ``SMD-0805``)
        resolves its rigid / SMD king-move spec, not another package's axial
        reach. During placement no real instance exists yet, so we
        pass the picker's ``package_variant`` address (a netlist-imported
        ``existing_instance`` supplies it via ``instance`` instead) plus the
        session catalogue. With no chosen variant the resolver falls through to
        the definition's own package, so THT parts are unaffected.
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
    def on_press(self, inp: PointerInput) -> bool:
        if inp.button != LEFT_BUTTON:
            return False
        snapped = self._view.snap(inp.scene_pos)

        if self._is_phase(PhaseEnum.FLOATING):
            self._commit_floating_place()
            return True

        defn = self._definition()
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
                self._pin_offsets = {p: specs[p].default_offset for p in bendable}
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
                self.preview.clear_reach()
                self._commit(self._anchor_pos, pin_offsets=dict(self._pin_offsets))
                self._reset_phase()
            return True

        return False

    @traced()
    def on_move(self, inp: PointerInput) -> None:
        snapped = self._view.snap(inp.scene_pos)
        if self._is_phase(PhaseEnum.ANCHOR):
            self._last_cursor_pos = snapped
            self.preview.move_ghost(snapped)
        elif self._is_phase(PhaseEnum.DRAG_PIN):
            assert self._anchor_pos is not None and self._current_drag_pin is not None
            new_off = self._compute_drag_offset(snapped)
            if new_off is not None:
                self._pin_offsets[self._current_drag_pin] = new_off
                self._rebuild_ghost_at_anchor()
            self._update_reach_line(snapped)

    @traced()
    def key_actions(self) -> dict[str, str | None]:
        """Declares these action ids exist — every value is ``None`` (inherit
        the generic global default), so no shortcut LITERAL is declared here
        (Step 2b).  Still required: an unregistered action id's rebind
        lookup restarts at the KeyMap's *parent* (see KeyMap.resolve), which
        can miss a user override recorded on this map when it isn't sharing
        the parent's ``_overrides`` dict by reference; a ``None``-registered
        action instead restarts the inherit lookup at *self*, honouring a
        rebind recorded here.  See ``displayable_action_ids`` for the
        equivalent [?]-overlay listing.
        """
        return {
            _ACT_ROTATE_CW: None,
            _ACT_ROTATE_CCW: None,
            _ACT_FLIP: None,
            _ACT_REDRAG: None,
        }

    @traced()
    def key_claims(self) -> tuple[tuple[str, Callable[[], bool]], ...]:
        """Claim the transform keys so the (no-op while placing) globals don't
        shadow them.  Decided dynamically through the KeyMap — no key literals."""
        always = self._always
        return (
            (_ACT_ROTATE_CW, always),
            (_ACT_ROTATE_CCW, always),
            (_ACT_FLIP, always),
            (_ACT_REDRAG, always),
            (_ACT_CONFIRM, always),
            (_ACT_CANCEL, always),
        )

    @staticmethod
    def _always() -> bool:
        return True

    @traced()
    def command_table(self) -> dict[str, Callable[[], object]]:
        """``action_id -> command`` — the data ``EditController.dispatch_tool_
        command`` runs (the GUI-composed subclass's ``on_key`` resolves a key
        to one of these ids via the KeyMap and calls it).

        ``confirm`` / ``cancel`` are phase-sensitive (floating commit/cancel
        vs. drag-pin cancel) — the wrappers here reproduce the same
        precedence the old raw ``on_key`` dispatch had.
        """
        return {
            _ACT_FLIP: self._key_flip_pin_pair,
            _ACT_ROTATE_CW: lambda: self._key_rotate(90),
            _ACT_ROTATE_CCW: lambda: self._key_rotate(-90),
            _ACT_REDRAG: self._key_redrag,
            _ACT_CONFIRM: self._confirm_cmd,
            _ACT_CANCEL: self._cancel_cmd,
        }

    @traced()
    def _confirm_cmd(self) -> bool:
        """``[Enter]``: commit the floating placement (only phase it applies)."""
        if self._is_phase(PhaseEnum.FLOATING):
            self._commit_floating_place()
            return True
        return False

    @traced()
    def _cancel_cmd(self) -> bool:
        """``[Esc]``: cancel the pending placement or leave the tool."""
        if self._is_phase(PhaseEnum.FLOATING):
            self._cancel_floating_place()
            self._rebuild_ghost()
            return True
        if self._is_phase(PhaseEnum.DRAG_PIN):
            self.preview.clear_reach()
            self._reset_phase()
            self._rebuild_ghost()
            return True
        # In ANCHOR there is no pending placement to revert, but the cancel
        # action is still handled so action dispatchers can leave PlaceTool
        # before the first component is placed.
        return True

    @traced()
    def _key_flip_pin_pair(self) -> bool:
        """Flip pin1/pin2 while dragging a pin — anchor moves to old pin2 pad."""
        if not self._is_phase(PhaseEnum.DRAG_PIN):
            return False
        defn = self._definition()
        fp = self._resolved_footprint(defn) if defn else None
        if (
            defn
            and fp is not None
            and fp.pin_count() == 2
            and self._anchor_pos is not None
        ):
            import uuid as _uuid

            tmp_pl = ComponentPlacement(
                component_id=_uuid.UUID(int=0),
                position=self._anchor_pos,
                rotation=self._rotation,
                side="top",
                pin_offsets=dict(self._pin_offsets),
            )
            flipped = flip_component(fp, tmp_pl)
            self._anchor_pos = flipped.position
            self._rotation = flipped.rotation
            self._pin_offsets = flipped.pin_offsets
            self._rebuild_ghost_at_anchor()
            self._update_reach_overlay()
        return True

    @traced()
    def _key_rotate(self, delta: int) -> bool:
        """Rotate the ghost by *delta* degrees (±90)."""
        self._rotation = (self._rotation + delta) % 360
        if self._is_phase(PhaseEnum.DRAG_PIN):
            self._rebuild_ghost_at_anchor()
            self._update_reach_overlay()
        else:
            # Rebuild rebakes the body shape for the new rotation, then
            # re-park the ghost under the cursor's last known position.
            self._rebuild_ghost()
            if self._last_cursor_pos is not None:
                self.preview.move_ghost(self._last_cursor_pos)
        return True

    @traced()
    def _key_redrag(self) -> bool:
        """Re-drag — restart drag for the current bendable pin from default."""
        defn = self._definition()
        specs = self._bend_specs(defn) if defn else {}
        if not specs:
            return False
        bendable = sorted(specs.keys())
        if self._is_phase(PhaseEnum.ANCHOR) and self._anchor_pos is None:
            return False  # nothing to re-drag yet
        if self._is_phase(PhaseEnum.ANCHOR):
            # anchor already fixed (e.g. after phase reset), start drag
            self._pin_offsets = {p: specs[p].default_offset for p in bendable}
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
        return True

    @traced()
    def on_scene_cleared(self) -> None:
        self.preview.on_scene_cleared()
        self._rebuild_ghost()

    # ------------------------------------------------------------------
    # Ghost helpers
    # ------------------------------------------------------------------

    @traced()
    def _rebuild_ghost(self) -> None:
        defn = self._definition()
        if defn is None:
            self.preview.clear_ghost()
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
            self._theme_typed(), package_families=families or {}
        )
        self.preview.show_ghost(
            defn,
            rotation=self._rotation,
            pin_offsets=offsets,
            package_variant=self._package_variant,
            theme=theme,
        )

    @traced()
    def _rebuild_ghost_at_anchor(self) -> None:
        self._rebuild_ghost()
        if self._anchor_pos is not None:
            self.preview.move_ghost(self._anchor_pos)

    # ------------------------------------------------------------------
    # Reach overlay
    # ------------------------------------------------------------------

    @traced()
    def _update_reach_overlay(self) -> None:
        """Mark the pads the active pin may land on with green dots.

        Free pins highlight every board pad within reach; a discrete pin (an SMD
        chip's second terminal) highlights its mount-candidate pads instead, so
        the orthogonal + diagonal mounting options are visible during the drag.
        """
        self.preview.clear_reach()
        if self._phase != "drag_pin" or self._anchor_pos is None:
            return
        defn = self._definition()
        if defn is None or self._current_drag_pin is None:
            return
        spec = self._bend_specs(defn).get(self._current_drag_pin)
        if spec is None:
            return

        if spec.is_free():
            origin = self._anchor_pos  # placement.position = component (0,0)
            self.preview.show_reach_dots(
                [
                    pt
                    for pt in self._view.board_pad_positions()
                    if spec.is_reachable_from_pin_default(origin, pt)
                ]
            )
            return

    @traced()
    def _update_reach_line(self, cursor: Point) -> None:
        """Update the feedback line from the reach centre to the cursor."""
        self.preview.clear_reach_line()
        if self._phase != "drag_pin" or self._anchor_pos is None:
            return
        defn = self._definition()
        if defn is None or self._current_drag_pin is None:
            return
        spec = self._bend_specs(defn).get(self._current_drag_pin)
        if spec is None or not spec.is_free():
            return

        origin = self._anchor_pos  # component (0,0) = reach centre
        self.preview.show_reach_line(origin, cursor)

    # ------------------------------------------------------------------
    # Offset computation
    # ------------------------------------------------------------------

    @traced()
    def _resolved_footprint(self, defn: ComponentDefinition) -> Footprint:
        """The footprint of the *chosen* package variant, not the definition's.

        The ghost renders the picked ``package_variant``'s body and pin spacing
        (see ``make_ghost_item``); the drag-pin baseline, flip and pin-count
        logic must use that SAME geometry, or the offset is measured against one
        footprint and drawn against another — the body drifts, worse with
        rotation. Falls back to the definition default when no variant resolves."""
        families = getattr(self._session, "package_families", None) or {}
        return resolve_package_variant_footprint(
            defn,
            self._package_variant,
            families,
        )

    @traced()
    def _pin_baseline_world(self, pin_number: int) -> Point | None:
        """World position of the pin ignoring any current offset."""
        if self._anchor_pos is None:
            return None
        defn = self._definition()
        if defn is None:
            return None
        temp = ComponentPlacement(
            component_id=uuid.UUID(int=0),
            position=self._anchor_pos,
            rotation=self._rotation,
            side="top",
            pin_offsets={},
        )
        return pin_world_position(self._resolved_footprint(defn), temp, pin_number)

    @traced()
    def _compute_drag_offset(self, cursor: Point) -> Vector | None:
        """Return the clamped/snapped local offset for the active drag pin."""
        assert self._anchor_pos is not None and self._current_drag_pin is not None
        defn = self._definition()
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
            snapped = self._view.snap(cursor)
            if not spec.is_reachable_from_pin_default(self._anchor_pos, snapped):
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
        _log.debug(
            "place.attempt",
            definition=self._definition_ref,
            pos=(position.x, position.y),
            rotation=self._rotation,
        )

        # Resolve instance + placement + conflicts via the controller; the tool
        # keeps the floating / auto-accept UX and the phase machine.
        resolved = self._session.edit_controller.resolve_placement(
            self._definition_ref,
            position=position,
            rotation=self._rotation,
            side="top",
            pin_offsets=pin_offsets,
            package_variant=self._package_variant,
            existing_instance=self._existing_instance,
            prompts=self.prompts,
        )
        if resolved is None:
            _log.info(
                "place.blocked",
                definition=self._definition_ref,
                pos=(position.x, position.y),
            )
            return

        has_wire_conflict = bool(resolved.wire_cmds)

        auto_accept = load_app_settings().auto_accept_placement

        if auto_accept and not has_wire_conflict:
            self._do_place(resolved)
        else:
            # Enter floating — show component on board, wait for Enter/Esc.
            inst, placement = resolved.instance, resolved.placement
            self._reset_pending(resolved)
            self._session.layout.placements[inst.id] = placement
            self._session.instances[inst.id] = inst
            self._set_phase(PhaseEnum.FLOATING)
            self._session.notify_changed()
            if self._view is not None:
                self._view.notify_status(
                    StatusEvent(
                        "place.floating",
                        {
                            "text": "[Enter] Accept placement  ·  [Esc] Cancel",
                            "timeout_ms": 0,
                        },
                    )
                )

    @traced()
    def _do_place(self, resolved: ResolvedPlacement) -> None:
        """Commit a resolved placement immediately via the controller."""
        self._session.edit_controller.place_component(resolved)
        _log.info(
            "place.committed",
            definition=self._definition_ref,
            refdes=resolved.instance.refdes,
            pos=(resolved.placement.position.x, resolved.placement.position.y),
        )

    @traced()
    def _reset_pending(self, resolved: ResolvedPlacement | None = None) -> None:
        self._pending = resolved

    @traced()
    def _commit_floating_place(self) -> None:
        """Commit the pending floating placement."""
        if self._pending is None:
            return

        self._do_place(self._pending)
        self._reset_pending()
        self._set_phase(PhaseEnum.ANCHOR)
        if self._view is not None:
            self._view.notify_status(StatusEvent("place.floating.cleared"))

    @traced()
    def _cancel_floating_place(self) -> None:
        """Remove the pending floating placement from layout without a command."""
        if self._pending is not None:
            inst_id = self._pending.instance.id
            self._session.layout.placements.pop(inst_id, None)
            self._session.instances.pop(inst_id, None)
            self._session.notify_changed()
        self._reset_pending()
        self._set_phase(PhaseEnum.ANCHOR)
        if self._view is not None:
            self._view.notify_status(StatusEvent("place.floating.cleared"))

    @traced()
    def _reset_phase(self) -> None:
        self._set_phase(PhaseEnum.ANCHOR)
        self._anchor_pos = None
        self._pin_offsets = {}
        self._drag_pin_queue = []
        self._current_drag_pin = None
