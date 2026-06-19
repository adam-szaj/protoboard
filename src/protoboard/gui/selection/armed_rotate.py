# SPDX-License-Identifier: MIT
"""``ArmedRotate`` — float-rotate as an :class:`ArmedSubTool`.

Armed on a selection, the rotation is *previewed* live and ``accept()`` commits
it as one undo entry — but **only if the previewed state is committable**.  A
rotate that lands in any conflict leaves the sub-tool armed; the user steps
somewhere clear or cancels (the floating feel, restored for rotate).

Preview is done by applying the *very* rotate command an accept would commit
(``cmd.apply(state)``) directly to the layout — off the undo stack — and
reverting it with ``cmd.revert(state)``.  The command is the single source of
truth for what a rotation does (including owned-wire handling), so the preview
can never diverge from what accept commits.  Each step rebuilds one command for
the net accumulated angle, so the preview is always "one rotation by accum".

Default pivot is each object's own centre; a clicked pad becomes the shared
pivot for the whole selection (re-click resets).  Steps are ±90°.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Callable, cast

from PySide6.QtCore import Qt

from protoboard.geometry import Point

if TYPE_CHECKING:
    from PySide6.QtGui import QKeyEvent, QWheelEvent

    from protoboard.domain.commands.base import Command, ComponentState
    from protoboard.gui.selection.drag_controller import DragContext
    from protoboard.gui.tools.drag_commit_decision import DragCommitDecision

    SnapFn = Callable[[Point], "Point | None"]
    CommitFn = Callable[["object"], bool]  # execute a command; False = rejected
    PreviewFn = Callable[["DragCommitDecision | None"], None]
    StatusFn = Callable[[str], None]
    Matcher = Callable[["QKeyEvent", str], bool]  # (event, action_id) via KeyMap


# One mouse-wheel detent in Qt ``angleDelta`` units (15° × 8 = 1/8°-units).
# A standard notch reports 120; we step one 90° rotation per accumulated notch.
_WHEEL_NOTCH = 120


class ArmedRotate:
    """Float-rotate sub-tool — preview-then-commit with conditional disarm."""

    def __init__(
        self,
        ctx: DragContext,
        *,
        comp_ids: frozenset[uuid.UUID],
        group_id: uuid.UUID | None,
        snap: SnapFn,
        commit: CommitFn,
        matches: "Matcher",
        cw_ids: "tuple[str, ...]",
        ccw_ids: "tuple[str, ...]",
        pivot: Point | None = None,
        preview: PreviewFn | None = None,
        status: StatusFn | None = None,
    ) -> None:
        self._ctx = ctx
        self._comp_ids = comp_ids
        self._group_id = group_id
        self._snap = snap
        self._commit = commit
        self._matches = matches
        self._cw_ids = cw_ids    # action ids that step +90 (CW)
        self._ccw_ids = ccw_ids  # action ids that step -90 (CCW)
        self._preview = preview
        self._status = status
        self._accum = 0            # net previewed degrees (multiple of 90)
        self._pivot: Point | None = pivot  # seeded shared pivot (or None)
        self._finished = False
        # Wheel debounce: a mouse can emit several sub-detent angleDelta events
        # per physical notch, which would over-rotate (one notch → 180°/270°).
        # We accumulate raw 1/8°-units and step once per full notch (_WHEEL_NOTCH),
        # carrying the remainder — so one detent = exactly one 90° step, however
        # many events it arrives in, while a hard spin still steps proportionally.
        self._wheel_accum = 0
        # The command currently applied to the layout as the live preview (for
        # the net angle), or None when the preview is at zero.
        self._preview_cmd: object | None = None

    # -- introspection ------------------------------------------------------

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def accum(self) -> int:
        return self._accum

    @property
    def pivot(self) -> Point | None:
        return self._pivot

    @property
    def comp_ids(self) -> frozenset[uuid.UUID]:
        """The components being rotated — for the pivot-marker overlay."""
        return self._comp_ids

    def status_hint(self) -> str:
        piv = "shared pad pivot" if self._pivot is not None else "per-object"
        return (
            f"Rotate armed ({piv})  ·  wheel / [↑][↓] / [R] step 90°  ·  "
            "click a pad = shared pivot  ·  [Enter] accept  ·  [Esc] cancel"
        )

    # -- input --------------------------------------------------------------

    def on_key(self, event: QKeyEvent) -> bool:
        key = event.key()
        # Enter / Esc are raw confirm / cancel (not global shortcuts); the
        # rotation steps dispatch by action id through the KeyMap.
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.accept()
            return True
        if key == Qt.Key.Key_Escape:
            self.cancel()
            return True
        if any(self._matches(event, a) for a in self._cw_ids):
            self._step(90)
            return True
        if any(self._matches(event, a) for a in self._ccw_ids):
            self._step(-90)
            return True
        return False

    def wants_wheel(self, event: QWheelEvent) -> bool:
        return True  # float-rotate steps on the wheel

    def on_wheel(self, event: QWheelEvent) -> bool:
        """Accumulate wheel delta; step ±90° once per full notch.

        Debounces mice that emit several sub-detent events per physical notch
        (which would jump 180°/270° from one flick) while keeping a hard spin
        proportional.  Reversing direction discards the opposite-sign carry so a
        back-flick takes effect immediately.
        """
        dy = event.angleDelta().y()
        if dy == 0:
            return True
        if (dy > 0) != (self._wheel_accum >= 0):
            self._wheel_accum = 0  # direction reversed — drop stale carry
        self._wheel_accum += dy
        while self._wheel_accum >= _WHEEL_NOTCH:
            self._wheel_accum -= _WHEEL_NOTCH
            self._step(90)
        while self._wheel_accum <= -_WHEEL_NOTCH:
            self._wheel_accum += _WHEEL_NOTCH
            self._step(-90)
        return True

    def on_click(self, scene_pt: Point) -> bool:
        """Set / reset the shared pad pivot; re-rotate the preview about it."""
        pad = self._snap(scene_pt)
        if pad is None:
            return False
        same = (
            self._pivot is not None
            and self._pivot.x == pad.x
            and self._pivot.y == pad.y
        )
        self._pivot = None if same else pad
        self._rebuild_preview()  # re-base the same accum about the new pivot
        if self._status is not None:
            self._status(self.status_hint())
        return True

    # -- commit-readiness + terminal ---------------------------------------

    def can_commit(self) -> bool:
        decision = self._classify()
        return decision is None or decision.can_commit

    def accept(self) -> None:
        if not self.can_commit():
            # Conditional disarm: a conflicting rotate parks armed.
            if self._status is not None:
                self._status(
                    "Rotate: conflict here — step to a clear spot or [Esc]"
                )
            self._emit_preview()
            return
        accum = self._accum
        self._clear_preview()  # revert the off-stack preview …
        self._finished = True
        if accum % 360 != 0:
            cmd = self._build_command(accum % 360)
            if cmd is not None:
                self._commit(cmd)  # … then commit the net rotation for real
        if self._status is not None:
            self._status("Rotation applied" if accum % 360 else "Rotation: no change")

    def cancel(self) -> None:
        self._clear_preview()
        self._finished = True
        if self._status is not None:
            self._status("Rotation cancelled")
        self._emit_preview()

    # -- preview mechanics (command apply/revert, off the command stack) ----

    def _step(self, delta: int) -> None:
        self._accum = (self._accum + delta) % 360
        self._rebuild_preview()

    def _rebuild_preview(self) -> None:
        """Re-apply one preview command for the current (accum, pivot)."""
        self._clear_preview(reset_accum=False)
        if self._accum % 360 != 0:
            cmd = self._build_command(self._accum % 360)
            if cmd is not None:
                cast("Command[ComponentState]", cmd).apply(
                    cast("ComponentState", self._ctx)
                )
                self._preview_cmd = cmd
        self._emit_preview()

    def _clear_preview(self, *, reset_accum: bool = True) -> None:
        """Revert the live preview command (if any) without touching the stack."""
        if self._preview_cmd is not None:
            cast("Command[ComponentState]", self._preview_cmd).revert(
                cast("ComponentState", self._ctx)
            )
            self._preview_cmd = None
        if reset_accum:
            self._accum = 0

    def _classify(self) -> DragCommitDecision | None:
        from protoboard.gui.placement_conflict import classify_drag_commit

        if self._accum % 360 == 0:
            return None
        try:
            return classify_drag_commit(
                session=self._ctx,  # type: ignore[arg-type]
                moved_comp_ids=set(self._comp_ids),
                dragged_wire_ids=frozenset(),
                drag_set_members=self._comp_ids,
            )
        except Exception:  # noqa: BLE001 — classifier best-effort
            return None

    def _build_command(self, delta: int) -> object | None:
        from protoboard.domain.commands.group_cmds import (
            RotateAroundPointCommand,
            RotateComponentsCommand,
            RotateGroupCommand,
        )

        if self._pivot is not None:
            return RotateAroundPointCommand(
                comp_ids=self._comp_ids, origin=self._pivot, delta=delta
            )
        if self._group_id is not None:
            return RotateGroupCommand(group_id=self._group_id, delta=delta)
        return RotateComponentsCommand(comp_ids=self._comp_ids, delta=delta)

    def _emit_preview(self) -> None:
        if self._preview is not None:
            self._preview(self._classify())
