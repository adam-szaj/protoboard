# SPDX-License-Identifier: MIT
"""``ArmedTransformTool`` — the armed rotate/move/flip mode as its own tool.

Entered from :class:`~protoboard.gui.tools.gui_tools.SelectTool` on ``[R]`` /
``[G]`` (which resolves the selection to a target snapshot and swaps to this
tool), it is a thin **input adapter** over the controller's stateful armed API
(:meth:`~protoboard.controller.editing.controller.EditController.begin_transform`
and the ``*_armed`` verbs, shipped in E3). Being its own registered tool, it
carries its own KeyMap layer (rotate/move/flip/step/wheel) — pushed by
``ToolHost.set_tool``, so it shadows the global shortcuts for as long as a
transform is live, and pops the moment control returns to SelectTool.

The tool owns no transform *logic*: keys become controller verbs
(``rotate_armed`` / ``move_armed`` / ``flip_armed`` / ``switch_transform_mode``
/ ``set_transform_pivot`` / ``accept_transform`` / ``cancel_transform``); the
controller holds the state and commits on accept. On a terminal
(accept / cancel / Esc) it publishes ``select.idle`` and calls the injected
*return_to* to hand control back to the SelectTool that armed it. A conflicting
accept parks the transform (``armed_transform.finished`` stays False) and this
tool stays active — the floating-conflict feel.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Callable

from protoboard.controller.tools.base import ControllerTool
from protoboard.controller.ports.inputs_details import WheelNotchAccumulator

if TYPE_CHECKING:
    from protoboard.geometry import Point
    from protoboard.gui.board_view_tool import AbstractBoardView
    from protoboard.controller.ports import PointerInput, WheelInput
    from protoboard.controller.editing import EditState
    from protoboard.gui.tools.select_preview import SelectPreview
    from protoboard.gui.tools.transform_overlays import TransformOverlays
    from protoboard.controller.editing.armed_transform import ArmedTransform
    from protoboard.controller.editing.controller import EditController


# Own action namespace — the armed keys move off ``select-tool.action.*`` onto
# this tool's own layer.
_ACT_ROTATE = "armed-transform.action.rotate"
_ACT_MOVE = "armed-transform.action.move"
_ACT_FLIP = "armed-transform.action.flip"
_ACT_STEP_LEFT = "armed-transform.action.step_left"
_ACT_STEP_RIGHT = "armed-transform.action.step_right"
_ACT_STEP_UP = "armed-transform.action.step_up"
_ACT_STEP_DOWN = "armed-transform.action.step_down"
_ACT_WHEEL_ROTATE = "armed-transform.action.wheel_rotate"
# Terminal ops resolve through the generic globals (KeyMap `final` actions
# reserve ``Return``/``Esc`` — no tool-specific action id may rebind them; see
# GLOBAL_ACTIONS in gui/keymap.py).  The old raw-key dispatch handled numpad
# Enter identically to Return and never handled Space; ``global.action.confirm``
# additionally accepts Space (a harmless widening — no user-visible key
# previously did anything else on Space while armed).
_ACT_CONFIRM = "global.action.confirm"
_ACT_CANCEL = "global.action.cancel"

_STEP_VECTORS: dict[str, tuple[float, float]] = {
    _ACT_STEP_LEFT: (-1.0, 0.0),
    _ACT_STEP_RIGHT: (1.0, 0.0),
    _ACT_STEP_UP: (0.0, -1.0),
    _ACT_STEP_DOWN: (0.0, 1.0),
}

# Every action this tool owns — all claimed unconditionally (it is only ever
# active while armed), so a global QAction on the same key never shadows it.
_ARMED_ACTION_IDS: tuple[str, ...] = (
    _ACT_ROTATE, _ACT_MOVE, _ACT_FLIP,
    _ACT_STEP_LEFT, _ACT_STEP_RIGHT, _ACT_STEP_UP, _ACT_STEP_DOWN,
    _ACT_CONFIRM, _ACT_CANCEL,
)


class ArmedTransformTool(ControllerTool):
    """The armed rotate/move/flip transform, driven as a registered tool."""

    def __init__(
        self,
        comp_ids: frozenset[uuid.UUID],
        group_id: uuid.UUID | None,
        *,
        active_group_id: uuid.UUID,
        mode: str,
        pivot: Point | None,
        return_to: Callable[[], None],
    ) -> None:
        self._comp_ids = comp_ids
        self._group_id = group_id
        self._active_group_id = active_group_id
        self._mode = mode
        self._pivot = pivot
        self._return_to = return_to
        self._armed: ArmedTransform | None = None
        self._preview: SelectPreview | None = None
        self._overlays: TransformOverlays | None = None
        self._wheel = WheelNotchAccumulator()

    # -- lifecycle ----------------------------------------------------------

    def activate(self, session: EditState, view: AbstractBoardView) -> None:
        self._session = session
        self._view = view
        self._armed = session.edit_controller.begin_transform(
            self._comp_ids,
            self._group_id,
            active_group_id=self._active_group_id,
            mode=self._mode,
            pivot=self._pivot,
            preview=self._on_preview,
            status=lambda msg: self._status(msg),
        )
        self._draw_pivot_markers()
        self._status(self._armed.status_hint())
        view.notify_controller_state(self.state)

    def deactivate(self) -> None:
        # Cancel a still-live transform so leaving the tool reverts a half-done
        # rotate/move; a finished one (post accept/cancel) is a no-op.
        controller = self._controller
        armed = self._armed
        if controller is not None and armed is not None and not armed.finished:
            controller.cancel_transform()
        self._clear_overlays()
        self._armed = None
        self._session = None  # type: ignore[assignment]
        self._view = None  # type: ignore[assignment]

    # -- controller access --------------------------------------------------

    @property
    def _controller(self) -> EditController | None:
        session = self._session
        return None if session is None else session.edit_controller

    # -- state --------------------------------------------------------------

    @property
    def state(self) -> str:
        armed = self._armed
        if armed is not None and not armed.finished:
            return f"move.float.{armed.mode}"
        return "select.idle"

    # -- commands -------------------------------------------------------------

    def command_table(self) -> dict[str, Callable[[], object]]:
        """``action_id -> command`` — the data ``EditController.dispatch_tool_
        command`` runs (the GUI-composed subclass's ``on_key`` resolves a key
        to one of these ids via the KeyMap and calls it)."""
        table: dict[str, Callable[[], object]] = {
            _ACT_CONFIRM: self._cmd_confirm,
            _ACT_CANCEL: self._cmd_cancel,
            _ACT_FLIP: self._cmd_flip,
            _ACT_ROTATE: self._cmd_rotate,
            _ACT_MOVE: self._cmd_move,
        }
        for action_id, vec in _STEP_VECTORS.items():
            table[action_id] = self._make_step_cmd(vec)
        return table

    def _cmd_confirm(self) -> bool:
        controller = self._controller
        if controller is None or self._armed is None:
            return False
        controller.accept_transform()
        return self._after_verb()

    def _cmd_cancel(self) -> bool:
        controller = self._controller
        if controller is None or self._armed is None:
            return False
        controller.cancel_transform()
        return self._after_verb()

    def _cmd_flip(self) -> bool:
        controller = self._controller
        if controller is None or self._armed is None:
            return False
        controller.flip_armed()
        return self._after_verb()

    def _cmd_rotate(self) -> bool:
        """[R] re-aims into rotate (keeping the float pose) or, when already
        in rotate, steps +90°."""
        controller = self._controller
        if controller is None or self._armed is None:
            return False
        if self._armed.mode != "rotate":
            controller.switch_transform_mode("rotate")
        else:
            controller.rotate_armed(90)
        return self._after_verb()

    def _cmd_move(self) -> bool:
        controller = self._controller
        if controller is None or self._armed is None:
            return False
        controller.switch_transform_mode("move")
        return self._after_verb()

    def _make_step_cmd(self, vec: tuple[float, float]) -> Callable[[], bool]:
        def _cmd() -> bool:
            controller = self._controller
            if controller is None or self._armed is None:
                return False
            dx, dy = vec
            if self._armed.mode == "rotate":
                controller.rotate_armed(90 if dy < 0 else -90)
            else:
                controller.move_armed(dx, dy)
            return self._after_verb()
        return _cmd

    def accept(self) -> None:
        """Commit the armed transform (programmatic; the atomic menu path).

        A committable accept returns to SelectTool; a conflict parks this tool
        active — same conditional-disarm as the interactive Enter.
        """
        controller = self._controller
        if controller is not None and self._armed is not None:
            controller.accept_transform()
            self._after_verb()

    def cancel(self) -> None:
        """Abort the armed transform, restoring the pre-arm pose (programmatic)."""
        controller = self._controller
        if controller is not None and self._armed is not None:
            controller.cancel_transform()
            self._after_verb()

    def _after_verb(self) -> bool:
        """Common tail after a consumed verb: publish state, return on terminal.

        A terminal verb (accept/cancel) clears the controller's armed transform;
        an un-committable accept leaves it live (parked in the conflict), so this
        stays armed. Redraws pivots (the preview refresh drops them) and only
        hands back to SelectTool once the controller reports done.
        """
        controller = self._controller
        if controller is not None and controller.armed_transform is None:
            # Terminal — committed or cancelled. Match the old disarm sequence:
            # publish select.idle, then return control to SelectTool.
            view = self._view
            if view is not None:
                view.notify_controller_state("select.idle")
            self._return_to()
        return True

    def key_claims(self) -> tuple[tuple[str, Callable[[], bool]], ...]:
        armed = self._is_armed
        return tuple((a, armed) for a in _ARMED_ACTION_IDS)

    def _is_armed(self) -> bool:
        return self._armed is not None and not self._armed.finished

    def displayable_action_ids(self) -> list[str]:
        """Ordered action ids for the ``[?]`` overlay / RMB menu.

        The tool declares no shortcut literals (Step 2b — the ``defaults:``
        resource owns them); this lists the same ops the old ``key_actions()``
        enumerated.
        """
        return [_ACT_ROTATE, _ACT_MOVE, _ACT_FLIP,
                _ACT_STEP_LEFT, _ACT_STEP_RIGHT, _ACT_STEP_UP, _ACT_STEP_DOWN]

    # -- mouse --------------------------------------------------------------

    def on_press(self, inp: PointerInput) -> bool:
        controller = self._controller
        if controller is None or self._armed is None:
            return False
        controller.set_transform_pivot(inp.scene_pos)
        self._draw_pivot_markers()
        return True  # suppress the normal selection a press would trigger

    def wants_wheel(self, inp: WheelInput) -> bool:
        return self._armed is not None and not self._armed.finished

    def on_wheel(self, inp: WheelInput) -> bool:
        controller = self._controller
        if controller is None or self._armed is None:
            return False
        notches = self._wheel.add(inp.delta_y)
        for _ in range(abs(notches)):
            controller.rotate_armed(90 if notches > 0 else -90)
        self._after_verb()
        return True

    def mouse_actions(self) -> dict[str, str | None]:
        return {_ACT_WHEEL_ROTATE: "WheelUp|WheelDown"}

    def mouse_dispatch(self) -> tuple[
        tuple[str, Callable[[PointerInput | WheelInput], bool]],
        ...,
    ]:
        return ((_ACT_WHEEL_ROTATE, self._mouse_wheel_rotate),)

    def _mouse_wheel_rotate(self, inp: PointerInput | WheelInput) -> bool:
        from protoboard.controller.ports import WheelInput

        if not isinstance(inp, WheelInput) or not self.wants_wheel(inp):
            return False  # not armed → the chord falls through to zoom
        return self.on_wheel(inp)

    # -- preview ------------------------------------------------------------

    def _on_preview(self, decision: object) -> None:
        """Live armed feedback: re-render the transformed bodies + conflict.

        A rotate/flip changes each body's orientation, which a cheap glide can't
        show — so the step needs a real ``refresh()`` (which clears the scene, so
        the pivot markers are re-drawn after).
        """
        from protoboard.controller.tools.drag_commit_decision import DragCommitDecision

        view = self._view
        if view is not None:
            view.refresh()
        self._draw_pivot_markers()
        if self._overlays is not None:
            dec = decision if isinstance(decision, DragCommitDecision) else None
            self._overlays.show_conflict(dec)

    def _draw_pivot_markers(self) -> None:
        """Draw the rotate pivot marker(s): what the selection spins around.

        Shared pad pivot → one marker on that pad; per-object → one on each
        rotating component's centre. No-op unless armed in rotate sub-mode.
        """
        preview = self._preview
        armed = self._armed
        session = self._session
        if preview is None:
            return
        preview.clear_pivots()
        if armed is None or session is None or armed.mode != "rotate":
            return
        if armed.pivot is not None:
            preview.show_pivot(armed.pivot, shared=True)
        else:
            for cid in armed.comp_ids:
                pl = session.layout.placements.get(cid)
                if pl is not None:
                    preview.show_pivot(pl.position, shared=False)

    def _clear_overlays(self) -> None:
        if self._preview is not None:
            self._preview.clear_pivots()
        if self._overlays is not None:
            self._overlays.clear()

    def on_scene_cleared(self) -> None:
        # A refresh (including the one ToolHost.set_tool fires right after we
        # arm) wipes the scene — drop the stale pivot items, then re-draw them
        # so the markers survive the rebuild.
        if self._preview is not None:
            self._preview.on_scene_cleared()
        self._draw_pivot_markers()

    def _status(self, msg: str) -> None:
        from protoboard.controller.ports import StatusEvent

        view = self._view
        if view is not None:
            view.notify_status(StatusEvent("armed-transform.status", {"text": msg}))
