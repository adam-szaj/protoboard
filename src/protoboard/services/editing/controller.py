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

Verbs are added incrementally, each repointing the GUI tool that used to build
the command inline: :meth:`~EditController.delete` is the first. The
command-execution substrate (:meth:`EditController._execute`, a Qt-free copy of
the tools' old ``_execute_command``) underpins them all.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from protoboard.domain.commands.base import (
    Command,
    CommandRejectedError,
    CommandStack,
    ComponentState,
    SchematicState,
)

if TYPE_CHECKING:
    from protoboard.domain.layout.group_tree import GroupTree
    from protoboard.domain.layout.wire import Wire


@runtime_checkable
class EditState(SchematicState, Protocol):
    """State substrate an :class:`EditController` verb needs.

    A structural protocol — ``HeadlessSession`` and ``EditorSession`` satisfy
    it without inheriting. Extends :class:`SchematicState` (board / layout /
    instances / components / schematic — the members every command reads) with
    the :class:`CommandStack` the controller runs commands through and the read
    helpers the group-lock guards need (:meth:`wire_by_id`, :meth:`group_tree`,
    :meth:`cached_wire_net_map`).

    ``notify_changed`` is an *optional* side-effect hook — the GUI session
    emits ``layout_changed`` from it so panels/DRC refresh; a headless driver
    leaves it a no-op (or omits it entirely, hence the ``getattr`` probe in
    :meth:`EditController._notify`).
    """

    stack: CommandStack[ComponentState]

    def wire_by_id(self, wire_id: uuid.UUID) -> Wire | None: ...
    def group_tree(self) -> GroupTree: ...
    def cached_wire_net_map(self) -> dict[uuid.UUID, uuid.UUID]: ...

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
    # Edit verbs
    # ------------------------------------------------------------------

    def delete(
        self,
        comp_ids: Iterable[uuid.UUID],
        wire_ids: Iterable[uuid.UUID],
        *,
        active_group_id: uuid.UUID,
        force: bool = False,
    ) -> bool:
        """Delete the given wires and/or components. Returns True if it ran.

        Stale ids (already gone from the layout) are dropped. Components and
        wires structurally owned by a non-active group are skipped by the
        group-lock guard, evaluated against the explicit *active_group_id* (the
        tool's interaction context; a bare CLI passes ROOT). *force* (the
        Shift+Delete escape hatch) lifts the lock for **wires only** — a grouped
        component still survives, matching the legacy tool contract. Both kinds
        go into one atomic macro so a mixed selection undoes in one step.
        Selection bookkeeping (clearing + broadcasting) stays with the caller.
        """
        from protoboard.domain.commands.base import MacroCommand
        from protoboard.domain.commands.component_cmds import (
            RemoveComponentCommand,
            RemoveWireCommand,
        )
        from protoboard.services.editing.group_lock import (
            component_locked_in_group,
            wire_locked_in_group,
        )

        state = self._state
        existing_wires = {w.id for w in state.layout.wires}
        keep_wires = [
            wid for wid in wire_ids
            if wid in existing_wires
            and (force or not wire_locked_in_group(
                state, wid, active_group_id=active_group_id))
        ]
        existing_comps = set(state.layout.placements)
        keep_comps = [
            cid for cid in comp_ids
            if cid in existing_comps
            and not component_locked_in_group(
                state, cid, active_group_id=active_group_id)
        ]
        if not keep_wires and not keep_comps:
            return False

        cmds: list[Command[ComponentState]] = [
            cast("Command[ComponentState]", RemoveWireCommand(wire_id=wid))
            for wid in keep_wires
        ]
        cmds += [
            cast("Command[ComponentState]", RemoveComponentCommand(component_id=cid))
            for cid in keep_comps
        ]
        cmd: Command[ComponentState] = (
            cmds[0] if len(cmds) == 1
            else cast(
                "Command[ComponentState]",
                MacroCommand(cmds, description="Delete selection"),
            )
        )
        return self._execute(cmd)

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
        self._state.notify_changed()
        return True


__all__ = ["EditController", "EditState"]
