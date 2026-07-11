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
    from protoboard.domain.board.pad_state import PadIndex
    from protoboard.domain.components.package import PackageFamily
    from protoboard.domain.layout.group_tree import GroupTree
    from protoboard.domain.layout.pad_conflict import ConflictScanner
    from protoboard.domain.layout.wire import Wire
    from protoboard.geometry import Point


@runtime_checkable
class EditState(SchematicState, Protocol):
    """State substrate an :class:`EditController` verb needs.

    A structural protocol — ``HeadlessSession`` and ``EditorSession`` satisfy
    it without inheriting. Extends :class:`SchematicState` (board / layout /
    instances / components / schematic — the members every command reads) with
    the :class:`CommandStack` the controller runs commands through, the package-
    family catalogue variant-aware verbs need, and the read helpers the group-
    lock guards / crossing-wire scan use (:meth:`wire_by_id`, :meth:`group_tree`,
    :meth:`cached_wire_net_map`, :meth:`conflict_scanner`).

    ``notify_changed`` is the side-effect hook — the GUI session emits
    ``layout_changed`` from it so panels/DRC refresh; a headless driver leaves
    it a no-op (:meth:`HeadlessSession.notify_changed`).
    """

    stack: CommandStack[ComponentState]
    package_families: dict[str, PackageFamily]

    def wire_by_id(self, wire_id: uuid.UUID) -> Wire | None: ...
    def group_tree(self) -> GroupTree: ...
    def cached_wire_net_map(self) -> dict[uuid.UUID, uuid.UUID]: ...
    def conflict_scanner(
        self, *, pad_index: PadIndex | None = ...
    ) -> ConflictScanner: ...

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

    def rotate(
        self,
        comp_ids: Iterable[uuid.UUID],
        *,
        group_id: uuid.UUID | None = None,
        delta_deg: int = 90,
        pivot: Point | None = None,
        active_group_id: uuid.UUID | None = None,
        remove_crossing_wires: bool = True,
    ) -> int:
        """Rotate a group / loose selection by *delta_deg*. Returns #wires dropped.

        The unified dispatch (from the tool's ``_rotate_multi``): an explicit
        *pivot* rotates every affected component around that point; otherwise a
        *group_id* rotates the group rigidly about its origin, and a loose
        selection rotates each component in place. When *group_id* is given the
        affected set is the group's full recursive membership; a loose selection
        is filtered by the group-lock guard against *active_group_id* (defaults
        to ROOT when omitted) and to components that actually have a placement.

        After the rotate, wires the move pushed across an alien pad are dropped
        (unless *remove_crossing_wires* is False — the single-primary path,
        which historically skipped this cleanup). The count of dropped wires is
        returned so the caller can surface a status message; presentation
        (highlight refresh, panel re-broadcast) stays with the caller.
        """
        from protoboard.domain.commands.group_cmds import (
            RotateAroundPointCommand,
            RotateComponentsCommand,
            RotateGroupCommand,
        )

        affected = self._affected_for_transform(
            comp_ids, group_id=group_id, active_group_id=active_group_id,
        )
        if not affected:
            return 0

        rotate_cmd: object
        if pivot is not None:
            rotate_cmd = RotateAroundPointCommand(
                comp_ids=frozenset(affected),
                origin=pivot,
                delta=delta_deg % 360,
            )
        elif group_id is not None:
            rotate_cmd = RotateGroupCommand(group_id=group_id, delta=delta_deg % 360)
        else:
            rotate_cmd = RotateComponentsCommand(
                comp_ids=frozenset(affected), delta=delta_deg % 360
            )
        return self._apply_transform(
            rotate_cmd, affected, remove_crossing_wires=remove_crossing_wires,
        )

    def flip(
        self,
        comp_ids: Iterable[uuid.UUID],
        *,
        group_id: uuid.UUID | None = None,
        active_group_id: uuid.UUID | None = None,
        remove_crossing_wires: bool = True,
    ) -> int:
        """Flip a group (rigid body) or the 2-pin components in a loose selection.

        A *group_id* flips the whole group about its pin centroid via
        ``FlipGroupCommand`` (axis ``'v'``, the designed default). A loose
        selection flips each 2-pin component in place (``flip_component`` pivots
        around the pin span); components with other pin counts are skipped, as
        are group-locked ones. Returns #wires dropped for crossing an alien pad,
        mirroring :meth:`rotate`.
        """
        from protoboard.domain.commands.base import MacroCommand
        from protoboard.domain.commands.component_cmds import MoveComponentCommand
        from protoboard.domain.components.package import resolved_footprint
        from protoboard.domain.layout.placement import flip_component

        state = self._state
        if group_id is not None:
            from protoboard.domain.commands.group_cmds import FlipGroupCommand

            affected = self._affected_for_transform(
                comp_ids, group_id=group_id, active_group_id=active_group_id,
            )
            if not affected:
                return 0
            return self._apply_transform(
                FlipGroupCommand(group_id=group_id, axis="v"),
                affected,
                remove_crossing_wires=remove_crossing_wires,
            )

        ids = self._loose_unlocked(comp_ids, active_group_id=active_group_id)
        families = state.package_families
        cmds: list[Command[ComponentState]] = []
        for cid in ids:
            pl = state.layout.placements.get(cid)
            inst = state.instances.get(cid)
            if pl is None or inst is None:
                continue
            defn = state.components.get(inst.definition_ref)
            if defn is None:
                continue
            fp = resolved_footprint(defn, inst, families)
            if fp.pin_count() != 2:
                continue
            cmds.append(
                cast(
                    "Command[ComponentState]",
                    MoveComponentCommand(
                        component_id=cid,
                        new_placement=flip_component(fp, pl),
                        old_placement=pl.copy(),
                    ),
                )
            )
        if not cmds:
            return 0
        self._execute(
            MacroCommand(cmds, description=f"Flip {len(cmds)} components")
        )
        return 0

    # ------------------------------------------------------------------
    # Transform helpers (shared by rotate + flip)
    # ------------------------------------------------------------------

    def _affected_for_transform(
        self,
        comp_ids: Iterable[uuid.UUID],
        *,
        group_id: uuid.UUID | None,
        active_group_id: uuid.UUID | None,
    ) -> frozenset[uuid.UUID]:
        """Resolve the components a rotate/flip actually touches.

        A *group_id* expands to the group's full recursive membership; a loose
        selection is filtered by :meth:`_loose_unlocked`.
        """
        if group_id is not None:
            from protoboard.domain.layout.group import all_component_ids

            return all_component_ids(self._state.layout.groups, group_id)
        return self._loose_unlocked(comp_ids, active_group_id=active_group_id)

    def _loose_unlocked(
        self,
        comp_ids: Iterable[uuid.UUID],
        *,
        active_group_id: uuid.UUID | None,
    ) -> frozenset[uuid.UUID]:
        """Loose components that are placed and not sealed inside another group."""
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        from protoboard.services.editing.group_lock import (
            component_locked_in_group,
        )

        active = active_group_id if active_group_id is not None else ROOT_GROUP_ID
        state = self._state
        return frozenset(
            cid
            for cid in comp_ids
            if state.layout.placements.get(cid) is not None
            and not component_locked_in_group(state, cid, active_group_id=active)
        )

    def _apply_transform(
        self,
        transform_cmd: object,
        affected: frozenset[uuid.UUID],
        *,
        remove_crossing_wires: bool,
    ) -> int:
        """Execute a rotate/flip, then optionally drop wires it pushed across
        alien pads (as a separate undo step). Returns the #wires dropped."""
        from protoboard.domain.commands.base import MacroCommand

        self._execute(transform_cmd)
        if not remove_crossing_wires:
            return 0
        remove_cmds = self._crossing_wire_remove_cmds(set(affected))
        if remove_cmds:
            self._execute(
                MacroCommand(remove_cmds, description="Remove crossing wires")
            )
        return len(remove_cmds)

    def _crossing_wire_remove_cmds(
        self, moved_comp_ids: set[uuid.UUID]
    ) -> list[Command[ComponentState]]:
        """RemoveWireCommands for wires crossing an alien pad after a move.

        A pure query — does not execute; the caller folds the result into a
        macro. Returns an empty list when nothing crosses.
        """
        from protoboard.domain.commands.component_cmds import RemoveWireCommand

        state = self._state
        bad_ids = state.conflict_scanner().find_wires_crossing_pads(
            wires=list(state.layout.wires),
            moved_comp_ids=moved_comp_ids,
        )
        if not bad_ids:
            return []
        id_set = set(bad_ids)
        return [
            cast("Command[ComponentState]", RemoveWireCommand(wire_id=w.id))
            for w in state.layout.wires
            if w.id in id_set
        ]

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
