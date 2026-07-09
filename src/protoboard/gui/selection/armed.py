# SPDX-License-Identifier: MIT
"""Armed transform sub-tool — one shape behind a shared protocol.

SelectTool offers an *armed transform mode* entered by ``[R]`` (rotate) or
``[G]`` (move).  It is armed on a selection, stays live while the user steps it
(keys / wheel / a pivot click) — and lets the user switch the active sub-mode
between rotate and move *without committing* — then ends with either an
**accept** (commit) or a **cancel** (revert).  Rather than branching on *which*
mode is live, SelectTool holds one :class:`ArmedSubTool` and dispatches through
it.

The contract that makes this more than a mechanical merge:

**Disarm is conditional on the state being committable.**  ``accept()`` commits
*and* finishes only when :meth:`ArmedSubTool.can_commit` is true; if the action
left a conflict the user cannot accept, the sub-tool **stays armed** and waits
(the floating-drag feel).  ``cancel()`` always reverts and finishes.

This module is Qt-free: input arrives as the seam DTOs
(:class:`~protoboard.gui.ports.KeyInput` / ``WheelInput``); accept / cancel
arrive as calls (the owner translates Enter / Esc through its Intent map).  The single concrete implementation is
:class:`~protoboard.gui.selection.float_transform.FloatTransform`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from protoboard.geometry import Point
    from protoboard.gui.ports import KeyInput, WheelInput


@runtime_checkable
class ArmedSubTool(Protocol):
    """A SelectTool mode that arms, accepts input, then commits or reverts.

    SelectTool drives it uniformly: feed it keys / wheel / clicks while it is
    the active armed mode, then read :attr:`finished` to know when to drop it.
    Implementations own their domain commands; this protocol unifies *control
    flow*, not the operations.
    """

    def on_key(self, inp: KeyInput) -> bool:
        """Handle a key while armed.  Return True when the key was consumed.

        Step / mode keys live here; accept and cancel arrive as
        :meth:`accept` / :meth:`cancel` calls (the owner translates
        Enter / Esc into intents).  A consumed key may set :attr:`finished`;
        the caller checks it after every consumed key.
        """
        ...

    def wants_wheel(self, inp: WheelInput) -> bool:
        """True when this sub-tool consumes the wheel (vs. letting it zoom).

        Lets SelectTool answer ``BoardView``'s pre-zoom probe accurately:
        float-rotate steps on the wheel (True), keyboard-move leaves it to zoom
        (False).
        """
        ...

    def on_wheel(self, inp: WheelInput) -> bool:
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
