# SPDX-License-Identifier: MIT
"""Armed transform sub-tools — one shape behind a shared protocol.

SelectTool offers a few *armed modes*: float-rotate (``[R]``) and keyboard
float-move (``[G]``).  Each one is armed on a selection, stays live while the
user steps it (keys / wheel / a pivot click), and ends with either an
**accept** (commit) or a **cancel** (revert).  They share that whole shape, so
rather than branching on *which* mode is live, SelectTool holds one
:class:`ArmedSubTool` and dispatches through it.

The contract that makes the modes more than a mechanical merge:

**Disarm is conditional on the state being committable.**  ``accept()`` commits
*and* finishes only when :meth:`ArmedSubTool.can_commit` is true; if the action
left a conflict the user cannot accept, the sub-tool **stays armed** and waits
(the floating-drag feel, restored for rotate).  ``cancel()`` always reverts and
finishes.

This module is Qt-light: it takes :class:`QKeyEvent` / wheel events at the
boundary but holds no scene.  The concrete sub-tools live next to it
(:mod:`armed_rotate`, and the keyboard-move adapter over
:class:`~protoboard.gui.selection.drag_controller.DragController`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from PySide6.QtGui import QKeyEvent, QWheelEvent

    from protoboard.geometry import Point


@runtime_checkable
class ArmedSubTool(Protocol):
    """A SelectTool mode that arms, accepts input, then commits or reverts.

    SelectTool drives it uniformly: feed it keys / wheel / clicks while it is
    the active armed mode, then read :attr:`finished` to know when to drop it.
    Implementations own their domain commands; this protocol unifies *control
    flow*, not the operations.
    """

    def on_key(self, event: QKeyEvent) -> bool:
        """Handle a key while armed.  Return True when the key was consumed.

        Step / accept / cancel keys live here.  A consumed accept or cancel sets
        :attr:`finished`; the caller checks it after every consumed key.
        """
        ...

    def wants_wheel(self, event: QWheelEvent) -> bool:
        """True when this sub-tool consumes the wheel (vs. letting it zoom).

        Lets SelectTool answer ``BoardView``'s pre-zoom probe accurately:
        float-rotate steps on the wheel (True), keyboard-move leaves it to zoom
        (False).
        """
        ...

    def on_wheel(self, event: QWheelEvent) -> bool:
        """Handle a wheel tick while armed (e.g. rotate ±90°).  Consumed?"""
        ...

    def on_click(self, scene_pt: Point) -> bool:
        """Handle a board click while armed (e.g. set a pivot).  Consumed?

        Returning True suppresses the normal selection that the press would
        otherwise trigger.
        """
        ...

    def can_commit(self) -> bool:
        """True when the current state may be committed (no HARD/blocking conflict).

        When False, :meth:`accept` is a no-op that leaves the sub-tool armed —
        the user must step somewhere legal or cancel.
        """
        ...

    def accept(self) -> None:
        """Commit if :meth:`can_commit`, then finish; otherwise stay armed.

        The conditional-disarm contract: an un-committable accept does NOT
        finish — it parks in the conflict for the user to resolve.
        """
        ...

    def cancel(self) -> None:
        """Revert any live preview and finish.  Always disarms."""
        ...

    @property
    def finished(self) -> bool:
        """True once the sub-tool has committed or cancelled — drop it."""
        ...

    def status_hint(self) -> str:
        """One-line status-bar prompt for the current armed state."""
        ...
