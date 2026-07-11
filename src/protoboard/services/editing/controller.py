# SPDX-License-Identifier: MIT
"""EditController — the presentation-free edit-verb layer (Controller tier).

Both the GUI tools and a headless CLI drive the *same* verbs on this object:
each verb assembles a domain :class:`Command`, runs it through the session's
:class:`CommandStack`, and pings the state's change hook. The controller is
**stateless** — interaction state (armed / floating / pick) stays in the GUI
tools; the driver (GUI ``SelectTool``, or the CLI) owns selection and the
active group.

State is taken through the :class:`EditState` Protocol, satisfied structurally
by both :class:`~protoboard.services.editing.headless_session.HeadlessSession`
and the GUI ``EditorSession``. Qt-free by construction — the same mechanical
rule that keeps ``domain/`` display-independent (CLAUDE.md §3).

This module is the step-0 scaffold: it carries the command-execution substrate
(:meth:`EditController._execute`, a Qt-free copy of the tools' old
``_execute_command``). The concrete edit verbs (delete / rotate / place / …)
land in later steps.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from protoboard.domain.commands.base import (
    Command,
    CommandRejectedError,
    CommandStack,
    ComponentState,
)

if TYPE_CHECKING:
    from protoboard.domain.schematic.schematic import Schematic


@runtime_checkable
class EditState(Protocol):
    """State substrate an :class:`EditController` verb needs.

    A structural protocol — ``HeadlessSession`` and ``EditorSession`` satisfy
    it without inheriting. Extends :class:`ComponentState` (board / layout /
    instances / components, the members every command reads) with the
    :class:`CommandStack` the controller runs commands through and the
    schematic that net-aware verbs touch.

    ``notify_changed`` is an *optional* side-effect hook — the GUI session
    emits ``layout_changed`` from it so panels/DRC refresh; a headless driver
    leaves it a no-op (or omits it entirely, hence the ``getattr`` probe in
    :meth:`EditController._notify`).
    """

    stack: CommandStack[ComponentState]
    schematic: Schematic

    def notify_changed(self) -> None:
        """Signal that a command mutated the layout (GUI: emit; headless: no-op)."""
        ...


class EditController:
    """Stateless edit-verb surface shared by GUI tools and a headless CLI.

    Construct one per session (``session.edit_controller``); every verb resolves
    its targets to ids up front, builds the domain command, and commits it via
    :meth:`_execute`.
    """

    def __init__(self, state: EditState) -> None:
        self._state = state

    @property
    def state(self) -> EditState:
        return self._state

    # ------------------------------------------------------------------
    # Command execution substrate
    # ------------------------------------------------------------------

    def _execute(self, cmd: object) -> bool:
        """Run *cmd* through the session's CommandStack.

        Qt-free copy of the tools' old ``_execute_command``: casts *cmd* to the
        command protocol, executes it against the state, and — on success —
        fires the change hook. Returns ``False`` (without raising) when the
        command rejects itself, matching the tools' contract.
        """
        try:
            self._state.stack.execute(
                cast("Command[ComponentState]", cmd),
                cast("ComponentState", self._state),
            )
        except CommandRejectedError:
            return False
        self._notify()
        return True

    def _notify(self) -> None:
        """Invoke the state's optional change hook.

        The GUI session emits ``layout_changed``; a headless state that omits
        the hook is a silent no-op.
        """
        hook = getattr(self._state, "notify_changed", None)
        if callable(hook):
            hook()


__all__ = ["EditController", "EditState"]
