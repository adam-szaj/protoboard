# SPDX-License-Identifier: MIT
"""EditController — the presentation-free edit-verb layer (Controller tier).

Both the GUI tools and a headless CLI drive the *same* verbs on this object:
each verb assembles a domain :class:`Command`, runs it through the session's
:class:`CommandStack`, and pings the state's change hook. The verbs are
**stateless**; the sole interaction state the controller now owns is the
**armed transform** (rotate/move/flip — see :meth:`EditController.begin_transform`),
stateful the way an active place-tool is. Selection is model state (read from
``state``); the driver (GUI ``SelectTool``, or the CLI) still owns the active
group and passes it in.

State is taken through the :class:`EditingContext` protocol, satisfied structurally
by both :class:`~protoboard.headless_session.HeadlessSession`
and the GUI ``EditorSession``. Qt-free by construction — the same mechanical
rule that keeps ``domain/`` display-independent (CLAUDE.md §3).

Verbs are added incrementally, each repointing the GUI tool that used to build
the command inline: :meth:`~EditController.delete` is the first. The
command-execution substrate (:meth:`EditController._execute`, a Qt-free copy of
the tools' old ``_execute_command``) underpins them all.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from protoboard.controller.editing.contexts import EditingContext
from protoboard.controller.editing.item_handle import ItemTag
from protoboard.domain.commands.base import (
    Command,
    CommandRejectedError,
    ComponentState,
)

if TYPE_CHECKING:
    from pathlib import Path

    from protoboard.controller.editing.armed_transform import ArmedTransform
    from protoboard.controller.editing.drag_preferences import DragPreferences
    from protoboard.controller.editing.item_handle import (
        ComponentHandle,
        GroupHandle,
        HandleKind,
        ItemId,
        ReadOnlyComponentHandle,
        WireHandle,
    )
    from protoboard.controller.editing.placement_conflict import PlacementPrompts
    from protoboard.controller.editing.wire_nets import NetMergePrompt
    from protoboard.controller.ports.drc import DRCIssue
    from protoboard.controller.ports.edit_services import (
        EditServicePorts,
        RouteResult,
    )
    from protoboard.controller.selection.drop_resolution import ResolveFn
    from protoboard.controller.tools.base import ControllerTool
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.layout.group import ComponentGroup
    from protoboard.domain.layout.placement import ComponentPlacement
    from protoboard.domain.routing_settings import RoutingSettings
    from protoboard.geometry import Point, Vector
    from protoboard.types import Side


@dataclass(frozen=True)
class ResolvedPlacement:
    """A conflict-resolved placement ready to commit via ``place_component``.

    Carries the (bound) incoming instance, its resolved ``ComponentPlacement``
    (with any ``shared_pins`` the conflict check applied), and the follow-up wire
    commands the resolution produced (wire re-route / removal on conflict).
    """

    instance: ComponentInstance
    placement: ComponentPlacement
    wire_cmds: tuple[object, ...] = field(default=())


def _wire_signature(wire: object) -> tuple[object, ...]:
    """Hashable identity of *wire* for autoroute duplicate detection.

    Two wires are the same *connection* when they share the unordered endpoint
    pair (captured as ``a.id.int XOR b.id.int`` so A→B and B→A collapse), the
    side, and the insulated flag. Wires without resolved knot refs fall back to
    a canonicalised path signature. Verbatim port of ``MainWindow``'s helper.
    """
    a = getattr(wire, "a", None)
    b = getattr(wire, "b", None)
    side = getattr(wire, "side", None)
    insulated = getattr(wire, "insulated", None)
    if a is not None and b is not None:
        endpoint_xor = a.id.int ^ b.id.int
        return ("knots", endpoint_xor, side, insulated)
    path = getattr(wire, "path", None) or []
    if not path:
        return ("path", side, insulated, ())
    rounded = tuple((round(p.x, 6), round(p.y, 6)) for p in path)
    reversed_rounded = tuple(reversed(rounded))
    canonical = rounded if rounded <= reversed_rounded else reversed_rounded
    return ("path", side, insulated, canonical)


@dataclass(frozen=True)
class _DeletePolicy:
    """How one edit-item kind is deleted — the per-tag rule table entry.

    ``keep`` answers "should this id be deleted?" — it folds the existence check
    and the kind-specific guard (a wire honours *force* + the wire-lock; a
    component honours the component-lock; a group has no guard). ``command``
    builds the domain command that does it (remove wire / remove component /
    disband group). Together they replace the three hand-rolled per-kind
    pipelines the delete verb used to carry.
    """

    keep: Callable[..., bool]
    command: Callable[[uuid.UUID], object]


def _delete_policies() -> dict[ItemTag, _DeletePolicy]:
    """The ``ItemTag`` → :class:`_DeletePolicy` table :meth:`_delete_targets` loops.

    Built lazily (commands / guards are imported here, not at module load, to
    keep the domain-command import out of the hot import path). ``keep`` takes
    ``(state, id, *, active_group_id, force)``; only the wire policy reads
    *force*, only wire / component read the lock — a group is always kept if it
    exists.
    """
    from protoboard.controller.editing.group_lock import (
        component_locked_in_group,
        wire_locked_in_group,
    )
    from protoboard.domain.commands.component_cmds import (
        RemoveComponentCommand,
        RemoveWireCommand,
    )
    from protoboard.domain.commands.group_cmds import DisbandGroupCommand

    def keep_wire(
        state: EditingContext,
        wid: uuid.UUID,
        *,
        active_group_id: uuid.UUID,
        force: bool,
    ) -> bool:
        return state.wire_by_id(wid) is not None and (
            force
            or not wire_locked_in_group(state, wid, active_group_id=active_group_id)
        )

    def keep_component(
        state: EditingContext,
        cid: uuid.UUID,
        *,
        active_group_id: uuid.UUID,
        force: bool,
    ) -> bool:
        return cid in state.layout.placements and not component_locked_in_group(
            state, cid, active_group_id=active_group_id
        )

    def keep_group(
        state: EditingContext,
        gid: uuid.UUID,
        *,
        active_group_id: uuid.UUID,
        force: bool,
    ) -> bool:
        return gid in state.layout.groups

    return {
        ItemTag.wire: _DeletePolicy(
            keep=keep_wire,
            command=lambda wid: RemoveWireCommand(wire_id=wid),
        ),
        ItemTag.component: _DeletePolicy(
            keep=keep_component,
            command=lambda cid: RemoveComponentCommand(component_id=cid),
        ),
        ItemTag.group: _DeletePolicy(
            keep=keep_group,
            command=lambda gid: DisbandGroupCommand(group_id=gid),
        ),
    }


def _never_matches(inp: object, action_id: str) -> bool:
    """A ``matches`` that never fires — the controller drives the armed
    transform through its semantic verbs (rotate/move/flip), so the raw
    key-chord path (``ArmedTransform.on_key``) is unused. A GUI adapter that
    still wants ``on_key`` supplies its own matcher instead."""
    return False


class EditController:
    """Edit-verb surface shared by GUI tools and a headless CLI.

    Construct one per session (``session.edit_controller``); every verb resolves
    its targets to ids up front, builds the domain command, and commits it via
    :meth:`_execute`.

    The verbs are stateless. The one exception is the **armed transform**
    (:meth:`begin_transform` … :meth:`accept_transform`): while a rotate/move/
    flip is armed the controller holds it as :attr:`armed_transform`, the way a
    place-tool session is stateful. Selection is model state (read through
    :attr:`state`), not controller state.
    """

    def __init__(self, state: EditingContext, ports: EditServicePorts) -> None:
        self._state = state
        self._ports = ports
        # Interaction state for the active armed transform (rotate/move/flip).
        # None unless a transform is currently armed — the controller is
        # stateful only within the scope of that one active sub-tool.
        self._armed: ArmedTransform | None = None
        # The active tool (Select/Place/Route/Cut/Simplify/ArmedTransform) —
        # the Controller's other piece of interaction state, alongside the
        # armed transform above.  None when no tool is active.  Owned here
        # (not by the View/ToolHost) so a headless driver can activate a tool
        # and drive its commands with no Qt at all (see tool_commands() /
        # dispatch_tool_command()).
        self._active_tool: ControllerTool | None = None

    @property
    def state(self) -> EditingContext:
        return self._state

    @property
    def armed_transform(self) -> ArmedTransform | None:
        """The live armed transform, or None when nothing is armed."""
        return self._armed

    # ------------------------------------------------------------------
    # Active-tool facade
    # ------------------------------------------------------------------

    @property
    def active_tool(self) -> ControllerTool | None:
        """The tool currently owning interaction (None ⇒ no tool active)."""
        return self._active_tool

    def set_active_tool(self, tool: ControllerTool | None) -> None:
        """Record *tool* as active and notify the state (KeyMap-layer swap).

        Ownership only — activation/deactivation (session/view binding) stays
        with the caller (``ToolHost.set_tool``), which already sequences
        deactivate-old / activate-new around this call.
        """
        self._active_tool = tool
        self._state.notify_active_tool_changed()

    def tool_commands(self) -> dict[str, Callable[[], object]]:
        """The active tool's ``action_id -> command`` table (``{}`` if none)."""
        tool = self._active_tool
        if tool is None:
            return {}
        return dict(tool.command_table())

    def dispatch_tool_command(self, action_id: str) -> bool:
        """Run the active tool's command for *action_id*. True if handled."""
        handler = self.tool_commands().get(action_id)
        if handler is None:
            return False
        handler()
        return True

    # ------------------------------------------------------------------
    # Edit verbs
    # ------------------------------------------------------------------

    def delete(
        self,
        item_ids: Iterable[ItemId] | None = None,
        *,
        active_group_id: uuid.UUID,
        force: bool = False,
    ) -> bool:
        """Delete the current selection (or *item_ids*). Returns True if it ran.

        With no *item_ids* the current model-side selection is deleted; passing
        *item_ids* first makes them the selection, then deletes it. Each tag is
        handled by its kind: a component / wire is removed (subject to the
        group-lock guard against *active_group_id*), a group is **disbanded**
        (dissolved — its members stay placed; the domain's only "remove a group"
        op). Stale ids are dropped.

        *force* (the Shift+Delete escape hatch) lifts the lock for **wires
        only** — a grouped component still survives. Everything folds into one
        atomic macro so a mixed selection undoes in one step. Selection
        bookkeeping (clearing + broadcasting) stays with the caller.

        Folds the whole addressed selection into ONE atomic macro (see
        :meth:`rotate`), so it splits the targets directly.
        """
        self._select_if_given(item_ids)
        group_ids, comp_ids, wire_ids = self._split_targets(item_ids)
        return self._delete_targets(
            comp_ids=comp_ids,
            wire_ids=wire_ids,
            group_ids=group_ids,
            active_group_id=active_group_id,
            force=force,
        )

    def _delete_targets(
        self,
        *,
        comp_ids: frozenset[uuid.UUID],
        wire_ids: frozenset[uuid.UUID],
        group_ids: frozenset[uuid.UUID],
        active_group_id: uuid.UUID,
        force: bool = False,
    ) -> bool:
        """Remove components / wires + disband groups as one atomic macro.

        The kind-split builder the ``delete`` verb and the handles share. Each
        tag carries its own deletion rule — a :class:`_DeletePolicy` in the
        per-tag table :func:`_delete_policies`: a wire / component removal
        honours the group-lock guard (a wire also honours *force*); a group is
        **disbanded** (dissolve, keep members) with no lock, addressed as one
        external object. One loop over the table applies each rule; the survivors
        fold into one atomic macro (order wire → component → group preserved for
        undo). Returns True iff a command ran.
        """
        from protoboard.domain.commands.base import MacroCommand

        state = self._state
        policies = _delete_policies()
        cmds: list[Command[ComponentState]] = []
        # Order is undo-sensitive; keep wire → component → group.
        for tag, ids in (
            (ItemTag.wire, wire_ids),
            (ItemTag.component, comp_ids),
            (ItemTag.group, group_ids),
        ):
            policy = policies[tag]
            cmds += [
                cast("Command[ComponentState]", policy.command(item_id))
                for item_id in ids
                if policy.keep(
                    state, item_id, active_group_id=active_group_id, force=force
                )
            ]
        if not cmds:
            return False

        cmd: Command[ComponentState] = (
            cmds[0]
            if len(cmds) == 1
            else cast(
                "Command[ComponentState]",
                MacroCommand(cmds, description="Delete selection"),
            )
        )
        return self._execute(cmd)

    def get_item(
        self,
        item_id: ItemId,
        *,
        active_group_id: uuid.UUID | None = None,
    ) -> ComponentHandle | ReadOnlyComponentHandle | GroupHandle | WireHandle:
        """Address a single edit target by :class:`ItemId`, returning its handle.

        The handle offers only the operations the target supports in the current
        edit context — a wire handle has no rotate / flip; a component sealed
        inside a group viewed from *outside* (its direct group is not
        *active_group_id*) yields a :class:`ReadOnlyComponentHandle` with no
        mutating op at all. *active_group_id* is that edit context (defaults to
        ROOT).
        """
        from protoboard.controller.editing.group_lock import component_locked_in_group
        from protoboard.controller.editing.item_handle import (
            ComponentHandle,
            GroupHandle,
            ReadOnlyComponentHandle,
            WireHandle,
        )
        from protoboard.domain.layout.layout import ROOT_GROUP_ID

        active = active_group_id if active_group_id is not None else ROOT_GROUP_ID
        if item_id.tag is ItemTag.group:
            return GroupHandle(self, item_id.uuid)
        if item_id.tag is ItemTag.wire:
            return WireHandle(self, item_id.uuid)
        if component_locked_in_group(self._state, item_id.uuid, active_group_id=active):
            return ReadOnlyComponentHandle(self, item_id.uuid, active_group_id=active)
        return ComponentHandle(self, item_id.uuid, active_group_id=active)

    def get_group(self, group_id: uuid.UUID) -> GroupHandle:
        """Address a group by id, returning a narrow :class:`GroupHandle`.

        For a caller that already knows the target is a group. Raises
        ``KeyError`` if *group_id* is not a group in the current layout — the id
        is mistyped, not a silently-wrong handle.
        """
        from protoboard.controller.editing.item_handle import GroupHandle

        if group_id not in self._state.layout.groups:
            raise KeyError(f"no group with id {group_id}")
        return GroupHandle(self, group_id)

    def get_wire(self, wire_id: uuid.UUID) -> WireHandle:
        """Address a wire by id, returning a narrow :class:`WireHandle`.

        Raises ``KeyError`` if *wire_id* is not a wire in the current layout.
        """
        from protoboard.controller.editing.item_handle import WireHandle

        if self._state.wire_by_id(wire_id) is None:
            raise KeyError(f"no wire with id {wire_id}")
        return WireHandle(self, wire_id)

    def get_component(
        self, comp_id: uuid.UUID, *, active_group_id: uuid.UUID | None = None
    ) -> ComponentHandle | ReadOnlyComponentHandle:
        """Address a component by id, returning a component handle.

        Read-only when the component is sealed inside a group viewed from
        outside *active_group_id* (defaults to ROOT), editable otherwise. Raises
        ``KeyError`` if *comp_id* has no placement.
        """
        from protoboard.controller.editing.group_lock import component_locked_in_group
        from protoboard.controller.editing.item_handle import (
            ComponentHandle,
            ReadOnlyComponentHandle,
        )
        from protoboard.domain.layout.layout import ROOT_GROUP_ID

        if comp_id not in self._state.layout.placements:
            raise KeyError(f"no component with id {comp_id}")
        active = active_group_id if active_group_id is not None else ROOT_GROUP_ID
        if component_locked_in_group(self._state, comp_id, active_group_id=active):
            return ReadOnlyComponentHandle(self, comp_id, active_group_id=active)
        return ComponentHandle(self, comp_id, active_group_id=active)

    def get_items(
        self,
        item_ids: ItemId | Iterable[ItemId] | None = None,
        *,
        active_group_id: uuid.UUID | None = None,
    ) -> list[HandleKind]:
        """Address a set of targets, returning one handle per id (a plain list).

        A single :class:`ItemId` is accepted and treated as a one-element set;
        ``None`` (no argument) addresses the whole current selection. Each id is
        resolved through :meth:`get_item`, so the list carries the *same* typed
        handles a single lookup would — including a :class:`ReadOnlyComponent
        Handle` for a component sealed inside a group viewed from outside. The
        caller filters / maps with ordinary comprehensions (``[h for h in items
        if h.supports_rotate()]``); the :class:`HandleKind` predicates keep it
        from having to ``isinstance``.

        This is an **inspection / per-item** surface. The atomic multi-target
        operation (rotate/flip/delete a whole selection as one undoable macro)
        lives on the verbs :meth:`rotate` / :meth:`flip` / :meth:`delete` — a
        naive ``for h in get_items(): h.rotate()`` loop would be N undo steps,
        not one.
        """
        from protoboard.controller.editing.item_handle import ItemId

        if item_ids is None:
            ids: list[ItemId] = list(self._state.selection())
        elif isinstance(item_ids, ItemId):
            ids = [item_ids]
        else:
            ids = list(item_ids)
        return [self.get_item(iid, active_group_id=active_group_id) for iid in ids]

    def _select_if_given(self, item_ids: Iterable[ItemId] | None) -> None:
        """Apply the ``select these`` side-effect of the selection-relative verbs.

        When *item_ids* is given, it becomes the new model-side selection (the
        ``select these → operate`` shortcut). ``None`` leaves the selection
        untouched (the verb operates on whatever is already selected). Kept
        separate from :meth:`_split_targets` so the side-effect is applied once,
        up front, and the split then reads a settled selection.
        """
        if item_ids is not None:
            self._state.select(frozenset(item_ids))

    def _split_targets(
        self, item_ids: Iterable[ItemId] | None
    ) -> tuple[frozenset[uuid.UUID], frozenset[uuid.UUID], frozenset[uuid.UUID]]:
        """Split the addressed selection into ``(group, comp, wire)`` id-buckets.

        With *item_ids* ``None`` the current model-side selection is used;
        otherwise *item_ids* itself is split (the verb has already applied it as
        the selection via :meth:`_select_if_given`, so the two agree). The atomic
        multi-target verbs need the raw buckets to build one macro; the per-item
        :meth:`get_items` surface does not go through here.
        """
        selection = self._state.selection() if item_ids is None else frozenset(item_ids)
        return (
            frozenset(i.uuid for i in selection if i.tag is ItemTag.group),
            frozenset(i.uuid for i in selection if i.tag is ItemTag.component),
            frozenset(i.uuid for i in selection if i.tag is ItemTag.wire),
        )

    def rotate(
        self,
        item_ids: Iterable[ItemId] | None = None,
        *,
        delta_deg: int = 90,
        pivot: Point | None = None,
        active_group_id: uuid.UUID | None = None,
        remove_crossing_wires: bool = True,
    ) -> int:
        """Rotate the current selection (or *item_ids*) by *delta_deg*.

        With no *item_ids* the current model-side selection is rotated; passing
        *item_ids* first makes them the selection, then rotates it (the ``select
        these → rotate`` shortcut). Each selected group turns rigidly about its
        origin, each loose component in place — unless an explicit *pivot* is
        given, which rotates every affected component about that one point.
        Loose components are filtered by the group-lock guard against
        *active_group_id* (defaults to ROOT). Wires in the selection are ignored.

        After the rotate, wires pushed across an alien pad are dropped (unless
        *remove_crossing_wires* is False). Returns the count dropped; presentation
        (highlight refresh, panel re-broadcast) stays with the caller.

        The whole addressed selection turns as ONE atomic macro (several groups
        in a single undoable step) — the reason this verb builds the targets
        directly rather than looping the per-item handles from :meth:`get_items`.
        """
        self._select_if_given(item_ids)
        group_ids, comp_ids, _wire_ids = self._split_targets(item_ids)
        return self._rotate_targets(
            group_ids=group_ids,
            comp_ids=comp_ids,
            delta_deg=delta_deg,
            pivot=pivot,
            active_group_id=active_group_id,
            remove_crossing_wires=remove_crossing_wires,
        )

    def _rotate_targets(
        self,
        *,
        group_ids: frozenset[uuid.UUID],
        comp_ids: frozenset[uuid.UUID],
        delta_deg: int = 90,
        pivot: Point | None = None,
        active_group_id: uuid.UUID | None = None,
        remove_crossing_wires: bool = True,
    ) -> int:
        """Rotate any mix of whole groups + loose components as one macro.

        Each group turns rigidly (``RotateGroupCommand``, or
        ``RotateAroundPointCommand`` when a shared *pivot* is given); the loose
        components turn about *pivot* if given, else each in place. Every
        command folds into one :class:`MacroCommand` so a multi-group selection
        is a single undoable step — the fix for the legacy single-``group_id``
        limitation. Returns the count of wires dropped for crossing an alien pad.
        """
        from protoboard.domain.commands.base import MacroCommand
        from protoboard.domain.commands.group_cmds import (
            RotateAroundPointCommand,
            RotateComponentsCommand,
            RotateGroupCommand,
        )
        from protoboard.domain.layout.group import all_component_ids

        delta = delta_deg % 360
        loose = self._loose_unlocked(comp_ids, active_group_id=active_group_id)
        groups = self._state.layout.groups
        group_members = frozenset(
            cid for gid in group_ids for cid in all_component_ids(groups, gid)
        )
        affected = loose | group_members
        if not affected:
            return 0

        cmds: list[Command[ComponentState]] = []
        if pivot is not None:
            # A shared pivot rotates every affected component around one point;
            # groups have no rigid-body special case here.
            cmds.append(
                cast(
                    "Command[ComponentState]",
                    RotateAroundPointCommand(
                        comp_ids=affected, origin=pivot, delta=delta
                    ),
                )
            )
        else:
            for gid in group_ids:
                cmds.append(
                    cast(
                        "Command[ComponentState]",
                        RotateGroupCommand(group_id=gid, delta=delta),
                    )
                )
            if loose:
                cmds.append(
                    cast(
                        "Command[ComponentState]",
                        RotateComponentsCommand(comp_ids=loose, delta=delta),
                    )
                )
        if not cmds:
            return 0
        rotate_cmd: object = (
            cmds[0]
            if len(cmds) == 1
            else MacroCommand(cmds, description="Rotate selection")
        )
        return self._apply_transform(
            rotate_cmd,
            affected,
            remove_crossing_wires=remove_crossing_wires,
        )

    def flip(
        self,
        item_ids: Iterable[ItemId] | None = None,
        *,
        active_group_id: uuid.UUID | None = None,
        remove_crossing_wires: bool = True,
    ) -> int:
        """Flip the current selection (or *item_ids*), mirroring :meth:`rotate`.

        With no *item_ids* the current model-side selection is flipped; passing
        *item_ids* first makes them the selection. Each selected group flips as a
        rigid body about its pin centroid (``FlipGroupCommand``, axis ``'v'``);
        each loose 2-pin component flips in place (other pin counts and
        group-locked components are skipped). Wires in the selection are ignored.
        Returns #wires dropped for crossing an alien pad.

        Folds the whole addressed selection into ONE atomic macro (see
        :meth:`rotate`), so it splits the targets directly.
        """
        self._select_if_given(item_ids)
        group_ids, comp_ids, _wire_ids = self._split_targets(item_ids)
        return self._flip_targets(
            group_ids=group_ids,
            comp_ids=comp_ids,
            active_group_id=active_group_id,
            remove_crossing_wires=remove_crossing_wires,
        )

    def _flip_targets(
        self,
        *,
        group_ids: frozenset[uuid.UUID],
        comp_ids: frozenset[uuid.UUID],
        active_group_id: uuid.UUID | None = None,
        remove_crossing_wires: bool = True,
    ) -> int:
        """Flip any mix of whole groups + loose 2-pin components as one macro.

        Each group flips about its pin centroid (``FlipGroupCommand``, axis
        ``'v'``); each loose 2-pin component flips in place. Every command folds
        into one macro so a multi-group selection undoes in one step. Returns
        the count of wires dropped for crossing an alien pad; the loose-only
        path drops none (matching the legacy per-component flip).
        """
        from protoboard.domain.commands.base import MacroCommand
        from protoboard.domain.commands.component_cmds import MoveComponentCommand
        from protoboard.domain.commands.group_cmds import FlipGroupCommand
        from protoboard.domain.components.package import resolved_footprint
        from protoboard.domain.layout.group import all_component_ids
        from protoboard.domain.layout.placement import flip_component

        state = self._state
        families = state.package_families

        cmds: list[Command[ComponentState]] = [
            cast("Command[ComponentState]", FlipGroupCommand(group_id=gid, axis="v"))
            for gid in group_ids
        ]
        loose = self._loose_unlocked(comp_ids, active_group_id=active_group_id)
        for cid in loose:
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

        # Groups' rigid-body flip needs the crossing-wire cleanup; the
        # loose-only 2-pin flip historically skipped it. The affected set for
        # cleanup is the groups' recursive membership.
        group_members = frozenset(
            cid
            for gid in group_ids
            for cid in all_component_ids(state.layout.groups, gid)
        )
        flip_cmd: object = (
            cmds[0]
            if len(cmds) == 1
            else MacroCommand(cmds, description="Flip selection")
        )
        if group_members:
            return self._apply_transform(
                flip_cmd,
                group_members,
                remove_crossing_wires=remove_crossing_wires,
            )
        self._execute(flip_cmd)
        return 0

    def add_wire(
        self,
        path: Sequence[Point],
        *,
        insulated: bool,
        side: str,
        merge_prompt: NetMergePrompt | None = None,
    ) -> uuid.UUID | None:
        """Add a wire along *path*, resolving the nets its endpoints touch.

        The *path* is already snapped (the tool owns snapping); this enriches it
        with pad nodes on both board sides — so a wire passing over a pad that
        exists only on the far side still carries that node for simplify / edit —
        builds the ``AddWireWithNetsCommand``, and runs the net resolution. Net
        creation / extension is silent; a two-distinct-net merge asks the
        injected *merge_prompt* and aborts (returns None) if it is rejected.

        Returns the new wire's id on commit, or None when the path is too short
        (< 2 points) or the merge was cancelled. Interaction state (routing-net
        highlight, preview overlays) stays with the caller.
        """
        from protoboard.controller.editing.wire_nets import resolve_wire_nets
        from protoboard.domain.commands.component_cmds import AddWireWithNetsCommand
        from protoboard.domain.layout.wire import Wire
        from protoboard.domain.layout.wire_utils import (
            board_pads_for_side,
            insert_pad_nodes,
        )

        if len(path) < 2:
            return None

        state = self._state
        # Pads from BOTH sides: a single-side board returns an empty set for the
        # other side (no effect).
        pads = board_pads_for_side(state.board, "top") | board_pads_for_side(
            state.board, "bottom"
        )
        enriched_path = insert_pad_nodes(list(path), pads)

        wire = Wire(
            id=uuid.uuid4(),
            path=enriched_path,
            insulated=insulated,
            side=cast("Side", side),
        )
        cmd = AddWireWithNetsCommand(wire=wire)
        if not resolve_wire_nets(state, wire, cmd, merge_prompt):
            return None
        self._execute(cast("Command[ComponentState]", cmd))
        return wire.id

    def set_wires_insulated(
        self, wire_ids: Iterable[uuid.UUID], *, insulated: bool
    ) -> None:
        """Set every wire in *wire_ids* insulated/bare as one macro.

        The single unified builder for the "Wire: Set Insulated / Bare" op —
        both the menu-bar slot and the context-menu handler call this.
        """
        from protoboard.domain.commands.base import MacroCommand
        from protoboard.domain.commands.component_cmds import SetWireInsulatedCommand

        ids = list(wire_ids)
        if not ids:
            return
        cmds: list[Command[ComponentState]] = [
            cast(
                "Command[ComponentState]",
                SetWireInsulatedCommand(wire_id=w, insulated=insulated),
            )
            for w in ids
        ]
        verb = "insulated" if insulated else "bare"
        cmd = (
            cmds[0]
            if len(cmds) == 1
            else MacroCommand(
                cmds,
                description=f"Set {len(cmds)} wires {verb}",
            )
        )
        self._execute(cmd)

    def set_wires_side(self, wire_ids: Iterable[uuid.UUID], *, side: Side) -> None:
        """Flatten every wire in *wire_ids* onto *side* (explicit "Move to").

        The single unified builder for "Wire: Move to Top/Bottom Layer".
        Distinct verb from :meth:`flip_wires` (toggle).
        """
        from protoboard.domain.commands.base import MacroCommand
        from protoboard.domain.commands.component_cmds import SetWireSideCommand

        ids = list(wire_ids)
        if not ids:
            return
        cmds: list[Command[ComponentState]] = [
            cast("Command[ComponentState]", SetWireSideCommand(wire_id=w, side=side))
            for w in ids
        ]
        cmd = (
            cmds[0]
            if len(cmds) == 1
            else MacroCommand(
                cmds,
                description=f"Move {len(cmds)} wires to {side}",
            )
        )
        self._execute(cmd)

    def flip_wires(self, wire_ids: Iterable[uuid.UUID]) -> None:
        """Flip every wire in *wire_ids* by toggling each fragment's side.

        A mixed wire stays mixed (inverted); no ``wire.side`` is read, so a
        mixed wire is never flattened (the old ``side=None`` bug). The single
        unified builder for the "Wire: Flip to Opposite Layer" op.
        """
        from protoboard.domain.commands.base import MacroCommand
        from protoboard.domain.commands.component_cmds import FlipWireSideCommand

        ids = list(wire_ids)
        if not ids:
            return
        cmds: list[Command[ComponentState]] = [
            cast("Command[ComponentState]", FlipWireSideCommand(wire_id=w)) for w in ids
        ]
        cmd = (
            cmds[0]
            if len(cmds) == 1
            else MacroCommand(
                cmds,
                description=f"Flip {len(cmds)} wires",
            )
        )
        self._execute(cmd)

    def set_locked(self, component_ids: Iterable[uuid.UUID], *, locked: bool) -> None:
        """Set the lock flag on every component in *component_ids* as one macro.

        The single unified builder for "Lock / Unlock". The toggle decision
        (all-locked -> unlock) stays with the caller, which passes an explicit
        *locked*.
        """
        from protoboard.domain.commands.base import MacroCommand
        from protoboard.domain.commands.component_cmds import LockComponentCommand

        placements = self._state.layout.placements
        cmds: list[Command[ComponentState]] = [
            cast(
                "Command[ComponentState]",
                LockComponentCommand(component_id=c, locked=locked),
            )
            for c in component_ids
            if c in placements and placements[c].locked != locked
        ]
        if not cmds:
            return
        verb = "Lock" if locked else "Unlock"
        cmd = (
            cmds[0]
            if len(cmds) == 1
            else MacroCommand(
                cmds,
                description=f"{verb} {len(cmds)} component(s)",
            )
        )
        self._execute(cmd)

    def resolve_placement(
        self,
        ref: str,
        *,
        position: Point,
        rotation: int = 0,
        side: str = "top",
        pin_offsets: dict[int, Vector] | None = None,
        package_variant: str | None = None,
        existing_instance: ComponentInstance | None = None,
        value: str | None = None,
        prompts: PlacementPrompts,
    ) -> ResolvedPlacement | None:
        """Build the incoming instance + placement and resolve its conflicts.

        The first half of the old ``PlaceTool._commit``: mint (or reuse) the
        instance, bind it so its chosen-variant footprint resolves, build the
        ``ComponentPlacement``, and run :func:`check_and_resolve` against the
        injected *prompts*. Returns a :class:`ResolvedPlacement` (instance +
        resolved placement + follow-up wire commands) ready for
        :meth:`place_component`, or None when the placement is blocked.

        This does **not** touch ``session.instances`` / ``session.layout`` or run
        any command — so the caller (GUI floating preview, or the CLI) decides
        when to commit. When *existing_instance* is given (a netlist-imported
        part) its identity is reused; otherwise a fresh refdes/value is minted.
        """
        from protoboard.controller.editing.placement_conflict import check_and_resolve
        from protoboard.domain.components.instance import ComponentInstance
        from protoboard.domain.layout.placement import ComponentPlacement

        state = self._state
        defn = state.components.get(ref)

        if existing_instance is not None:
            inst = existing_instance
        else:
            if value is not None:
                init_value = value
            elif defn is not None and defn.default_value:
                init_value = defn.default_value
            else:
                init_value = ref
            inst = ComponentInstance.new(
                definition_ref=ref,
                refdes=self._next_refdes(ref),
                value=init_value,
                package_variant=package_variant,
            )

        # Bind so the chosen-variant footprint resolves during the conflict
        # check (the instance is not yet in session.instances).
        if defn is not None:
            inst = inst.bind(defn, state.package_families)

        placement = ComponentPlacement(
            component_id=inst.id,
            position=position,
            rotation=rotation,
            side=cast("Side", side),
            pin_offsets=dict(pin_offsets) if pin_offsets else {},
        )
        wire_cmds: tuple[object, ...] = ()
        if defn is not None:
            resolved = check_and_resolve(
                session=state,
                placement=placement,
                defn=defn,
                instance_id=inst.id,
                prompts=prompts,
                instance=inst,
            )
            if resolved is None:
                return None
            placement = resolved.placement
            wire_cmds = resolved.wire_cmds
        return ResolvedPlacement(
            instance=inst,
            placement=placement,
            wire_cmds=wire_cmds,
        )

    def place_component(self, resolved: ResolvedPlacement) -> uuid.UUID:
        """Commit a :class:`ResolvedPlacement` — the old ``PlaceTool._do_place``.

        Executes the ``PlaceComponentCommand`` (folded into one macro with any
        follow-up wire commands the conflict resolution produced) and returns the
        placed component's id. Floating / auto-accept UX stays with the caller.
        """
        from protoboard.domain.commands.base import MacroCommand
        from protoboard.domain.commands.component_cmds import PlaceComponentCommand

        inst = resolved.instance
        place_cmd = PlaceComponentCommand(
            component_id=inst.id,
            placement=resolved.placement,
            instance=inst,
        )
        if resolved.wire_cmds:
            all_cmds = cast(
                "list[Command[ComponentState]]",
                [place_cmd, *resolved.wire_cmds],
            )
            self._execute(
                cast(
                    "Command[ComponentState]",
                    MacroCommand(all_cmds, description="Place component"),
                )
            )
        else:
            self._execute(cast("Command[ComponentState]", place_cmd))
        return inst.id

    def _next_refdes(self, ref: str) -> str:
        """Mint the next free refdes for definition *ref* (e.g. ``R3``)."""
        state = self._state
        defn = state.components.get(ref)
        prefix = defn.refdes_prefix if defn is not None else ref[0].upper()
        existing = {
            inst.refdes
            for inst in state.instances.values()
            if inst.refdes.startswith(prefix)
        }
        n = 1
        while f"{prefix}{n}" in existing:
            n += 1
        return f"{prefix}{n}"

    def apply_autoroute(self, result: RouteResult) -> int:
        """Commit an autoroute *result*'s wires, dropping duplicates. Returns #applied.

        Absorbs the apply half of ``MainWindow._on_autoroute_done``: filters out
        wires whose signature duplicates an already-existing wire (a stale-graph
        second-run trap where the router re-emits an existing connection with a
        fresh UUID), then commits the survivors as one ``Autoroute`` macro.

        Returns the number of wires actually added — 0 when *result* had no wires
        or every wire duplicated an existing one. Presentation (the summary
        dialog, dirty/undo/refresh) stays with the caller, which inspects both
        this count and ``result`` to pick its message.
        """
        from protoboard.domain.commands.base import MacroCommand
        from protoboard.domain.commands.component_cmds import AddWireCommand

        if not result.wires:
            return 0

        state = self._state
        existing_signatures = {_wire_signature(w) for w in state.layout.wires}
        kept: list[Command[ComponentState]] = []
        for w in result.wires:
            sig = _wire_signature(w)
            if sig in existing_signatures:
                continue
            existing_signatures.add(sig)
            kept.append(cast("Command[ComponentState]", AddWireCommand(wire=w)))
        if not kept:
            return 0
        macro: MacroCommand[ComponentState] = MacroCommand(
            commands=kept,
            description=f"Autoroute ({len(kept)} wires)",
        )
        # Silent: the GUI caller owns the refresh + single layout_changed emit
        # (in its legacy order, after mark_dirty); a headless caller's notify is
        # a no-op anyway. The command is committed to the stack either way.
        self._execute_silent(cast("Command[ComponentState]", macro))
        return len(kept)

    def autoroute(
        self,
        net_ids: set[uuid.UUID] | None,
        *,
        settings: RoutingSettings,
        report_path: Path | None = None,
    ) -> RouteResult:
        """Route *net_ids* (or every net when None) synchronously.

        The blocking counterpart to the GUI's background ``AutorouteWorker`` —
        used by the CLI. Builds an immutable ``RouteRequest`` from the current
        state + *settings* and runs the default router on the calling thread.
        Does **not** mutate the layout; pass the returned result to
        :meth:`apply_autoroute` to commit it.
        """
        state = self._state
        return self._ports.autoroute.route(
            board=state.board,
            layout=state.layout,
            schematic=state.schematic,
            components=state.components,
            instances=state.instances,
            settings=settings,
            net_ids=net_ids,
            report_path=report_path,
        )

    def run_drc(self) -> list[DRCIssue]:
        """Run DRC synchronously and return issues without GUI side effects."""
        state = self._state
        return self._ports.drc.check(
            board=state.board,
            layout=state.layout,
            schematic=state.schematic,
            components=state.components,
            instances=state.instances,
            graph=state.connectivity_graph(),
            axis_labels=state.axis_label_settings,
            pad_index=state.pad_index(),
        )

    def save(self, directory: Path) -> None:
        """Save the current project to *directory* (Qt-free YAML)."""
        state = self._state
        self._ports.projects.save(
            directory,
            board=state.board,
            layout=state.layout,
            schematic=state.schematic,
            instances=state.instances,
        )

    def export(self, kind: str, path: Path) -> None:
        """Export the board through the configured technical adapter."""
        state = self._state
        self._ports.export.export(
            kind,
            path,
            board=state.board,
            layout=state.layout,
            components=state.components,
            instances=state.instances,
            schematic=state.schematic,
            package_families=state.package_families,
        )

    def create_group(
        self,
        item_ids: Iterable[ItemId],
        *,
        active_group_id: uuid.UUID,
        name: str | None = None,
        auto_export: bool = False,
    ) -> uuid.UUID | None:
        """Promote the component *item_ids* into a new group under *active_group_id*.

        Splits the selection into free components + top-level groups under the
        active context, mints the new group (optionally auto-exporting its
        external pins), then runs the undo-sensitive create + reparent sequence
        as one batch (a single change hook at the end). Returns the new group id,
        or None when the selection is empty. Nesting is by component containment
        — an existing sub-group is promoted because one of its members is in
        *item_ids*, so only ``component`` tags are used (group / wire tags are
        ignored). The target-picker dialog and select-the-new-group side effect
        stay with the caller.
        """
        ids = frozenset(i.uuid for i in item_ids if i.tag is ItemTag.component)
        if not ids:
            return None
        new_group, promoted_free, promoted_groups = self._prepare_new_group(
            active_group_id,
            ids,
            name=name,
            auto_export=auto_export,
        )
        self._execute_group_promotion(
            active_group_id,
            new_group,
            promoted_free,
            promoted_groups,
        )
        return new_group.id

    def add_to_group(
        self,
        group_id: uuid.UUID,
        item_ids: Iterable[ItemId],
        *,
        active_group_id: uuid.UUID,
        auto_export: bool = False,
    ) -> None:
        """Add the component *item_ids* to the existing *group_id*.

        Attaches them to the target and detaches them from the active group when
        that differs, then (when *auto_export*) refreshes the target's exported
        pins. One batch, one change hook at the end. Only ``component`` tags are
        used. Selecting the target stays with the caller.
        """
        from protoboard.domain.commands.group_cmds import (
            AddToGroupCommand,
            RemoveFromGroupCommand,
            SetExportedPinsCommand,
        )

        ids = frozenset(i.uuid for i in item_ids if i.tag is ItemTag.component)
        if not ids:
            return
        state = self._state
        self._execute_silent(AddToGroupCommand(group_id=group_id, component_ids=ids))
        if group_id != active_group_id:
            self._execute_silent(
                RemoveFromGroupCommand(group_id=active_group_id, component_ids=ids)
            )
        if auto_export:
            new_exported = state.group_tree().external_pins_for_group(
                group_id, state.schematic
            )
            if new_exported != state.layout.groups[group_id].exported_pins:
                self._execute_silent(
                    SetExportedPinsCommand(group_id=group_id, new_exported=new_exported)
                )
        state.notify_changed()

    # ------------------------------------------------------------------
    # Group-promotion helpers
    # ------------------------------------------------------------------

    def _prepare_new_group(
        self,
        active_gid: uuid.UUID,
        ids: frozenset[uuid.UUID],
        *,
        name: str | None,
        auto_export: bool,
    ) -> tuple[ComponentGroup, frozenset[uuid.UUID], frozenset[uuid.UUID]]:
        """Promote the selection and build the (still uncommitted) new group.

        Splits *ids* into free components + top-level groups under *active_gid*,
        mints the new group id, and computes its auto-exported pins against a
        transient tree that already contains the prospective group (when
        *auto_export*). Returns ``(new_group, promoted_free, promoted_groups)``.
        """
        from protoboard.domain.layout.group import ComponentGroup
        from protoboard.domain.layout.group_tree import GroupTree
        from protoboard.domain.schematic.net import PinRef

        state = self._state
        default_name = f"Group{len(state.layout.groups) + 1}"
        promoted_free, promoted_groups = (
            state.group_tree().promote_selection_to_top_level(active_gid, ids)
        )
        new_group_id = uuid.uuid4()
        exported: frozenset[PinRef] = frozenset()
        if auto_export:
            transient = ComponentGroup(
                id=new_group_id,
                name=name or default_name,
                component_ids=promoted_free,
                child_group_ids=promoted_groups,
            )
            groups_with_new = dict(state.layout.groups)
            groups_with_new[new_group_id] = transient
            exported = GroupTree(groups_with_new).external_pins_for_group(
                new_group_id, state.schematic
            )
        new_group = ComponentGroup(
            id=new_group_id,
            name=name or default_name,
            component_ids=promoted_free,
            child_group_ids=promoted_groups,
            exported_pins=exported,
        )
        return new_group, promoted_free, promoted_groups

    def _execute_group_promotion(
        self,
        active_gid: uuid.UUID,
        new_group: ComponentGroup,
        promoted_free: frozenset[uuid.UUID],
        promoted_groups: frozenset[uuid.UUID],
    ) -> None:
        """Run the create + reparent command sequence for a new promoted group.

        Order is undo-sensitive: create the group, detach each promoted child
        from its old parent, attach the new group under *active_gid*, then detach
        the promoted free components from *active_gid*. Notifies once at the end.
        """
        from protoboard.domain.commands.group_cmds import (
            AddToGroupCommand,
            CreateGroupCommand,
            RemoveFromGroupCommand,
        )

        state = self._state
        self._execute_silent(CreateGroupCommand(group=new_group))
        for promoted_gid in promoted_groups:
            old_parent = state.group_tree().parent_group_of(promoted_gid)
            if old_parent is not None:
                self._execute_silent(
                    RemoveFromGroupCommand(
                        group_id=old_parent,
                        child_group_ids=frozenset({promoted_gid}),
                    )
                )
        self._execute_silent(
            AddToGroupCommand(
                group_id=active_gid, child_group_ids=frozenset({new_group.id})
            )
        )
        if promoted_free:
            self._execute_silent(
                RemoveFromGroupCommand(group_id=active_gid, component_ids=promoted_free)
            )
        state.notify_changed()

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
        from protoboard.controller.editing.group_lock import (
            component_locked_in_group,
        )
        from protoboard.domain.layout.layout import ROOT_GROUP_ID

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
    # Armed transform (rotate / move / flip) — a stateful sub-tool
    # ------------------------------------------------------------------
    #
    # The controller is stateful only while a transform is armed. A GUI tool
    # (or a headless driver) arms one on the selection, drives it with the
    # semantic verbs below (rotate / move / flip / set_pivot / update_pointer),
    # and finishes with accept / cancel. The Qt seams stay injected — the GUI
    # supplies the preview / status callbacks; snapping is a controller method
    # (:meth:`snap`), so no view is needed to drive it.

    def begin_transform(
        self,
        comp_ids: frozenset[uuid.UUID],
        group_id: uuid.UUID | None,
        *,
        active_group_id: uuid.UUID,
        mode: str = "rotate",
        pivot: Point | None = None,
        preview: Callable[[object], None] | None = None,
        status: Callable[[str], None] | None = None,
        preferences: DragPreferences | None = None,
        resolve_free: ResolveFn | None = None,
        resolve_sgroup: ResolveFn | None = None,
        resolve_disconnect: ResolveFn | None = None,
    ) -> ArmedTransform:
        """Arm a rotate/move/flip transform on *comp_ids* (and whole *group_id*).

        Stores it as the controller's live sub-tool and returns it. *pivot* seeds
        a shared rotate pivot; *preview* / *status* are the injected GUI seams
        (a headless driver leaves them None); the *resolve_* / *preferences*
        control soft-conflict wire drops (None ⇒ headless auto-remove). Snapping
        a clicked pivot goes through :meth:`snap`, closed over *active_group_id*.
        """
        from protoboard.controller.editing.armed_transform import ArmedTransform

        def _snap(raw: Point) -> Point | None:
            return self.snap(raw, active_group_id=active_group_id)

        self._armed = ArmedTransform(
            self._state,
            comp_ids=comp_ids,
            group_id=group_id,
            snap=_snap,
            execute=self._execute,
            matches=_never_matches,
            mode=mode,
            pivot=pivot,
            preview=preview,
            status=status,
            preferences=preferences,
            resolve_free=resolve_free,
            resolve_sgroup=resolve_sgroup,
            resolve_disconnect=resolve_disconnect,
        )
        return self._armed

    def rotate_armed(self, delta: int) -> None:
        """Step the armed transform ±90° (no-op when nothing is armed)."""
        if self._armed is not None:
            self._armed._rotate(delta)

    def move_armed(self, dx: float, dy: float) -> None:
        """Nudge the armed transform by (dx, dy) cells (no-op when unarmed)."""
        if self._armed is not None:
            self._armed._move(dx, dy)

    def flip_armed(self) -> None:
        """Flip 2-pin members of the armed transform (no-op when unarmed)."""
        if self._armed is not None:
            self._armed._flip()

    def switch_transform_mode(self, mode: str) -> None:
        """Re-aim the armed transform's sub-mode (rotate/move) without re-arming."""
        if self._armed is not None:
            self._armed.switch_mode(mode)

    def set_transform_pivot(self, raw_pt: Point) -> None:
        """Snap *raw_pt* and toggle it as the armed rotate pivot (click a pad)."""
        if self._armed is not None:
            self._armed.on_click(raw_pt)

    def accept_transform(self) -> None:
        """Commit the armed transform (parks armed on a HARD conflict).

        Clears the controller's armed state once the sub-tool reports finished —
        an un-committable accept leaves it armed so the caller can step clear.
        """
        armed = self._armed
        if armed is None:
            return
        armed.accept()
        if armed.finished:
            self._armed = None

    def cancel_transform(self) -> None:
        """Abort the armed transform, restoring the pre-arm pose."""
        if self._armed is not None:
            self._armed.cancel()
            self._armed = None

    def snap(
        self,
        scene_pt: Point,
        *,
        active_group_id: uuid.UUID,
        exclude_ids: frozenset[uuid.UUID] | None = None,
    ) -> Point:
        """Snap *scene_pt* to the nearest accessible pad or component pin.

        The Qt-free twin of the view's snap: pads and pins within a 3×3 grid
        window compete by squared distance (pads first, so a pad wins a tie);
        pins locked inside a child group of *active_group_id* are reachable only
        if exported; *exclude_ids* skips a component's pins (e.g. the one being
        dragged). Off-board — nothing within the window — falls back to the
        nearest half-integer pad-grid centre.
        """
        import math

        from protoboard.geometry import Point

        # While a transform is armed, snap against its PREVIEW pose (the live
        # layout stays pristine) so a pivot-click lands on the moved pins.
        state: EditingContext = self._state
        if self._armed is not None:
            from protoboard.controller.editing.preview_state import PreviewState

            state = PreviewState(self._state, self._armed.preview_layout())
        groups = state.layout.groups
        tree = state.group_tree()
        active_group = groups.get(active_group_id)
        locked_comp_ids: set[uuid.UUID] = set()
        exported_pins: set[tuple[uuid.UUID, int]] = set()
        if active_group is not None:
            for child_gid in active_group.child_group_ids:
                child_group = groups.get(child_gid)
                if child_group is None:
                    continue
                for cid in tree.all_component_ids(child_gid):
                    locked_comp_ids.add(cid)
                for ep in child_group.exported_pins:
                    exported_pins.add((ep.component_id, ep.pin_number))

        sx, sy = scene_pt.x, scene_pt.y
        best: Point | None = None
        best_d = math.inf

        def _check(px: float, py: float) -> None:
            nonlocal best, best_d
            d = (sx - px) * (sx - px) + (sy - py) * (sy - py)
            if d < best_d:
                best_d = d
                best = Point(px, py)

        # Board pads — bucket centres into the same round-keyed 3×3 grid the
        # view uses, sourced from the shared Board.pad_positions() so the two
        # can never diverge.
        from collections import defaultdict

        pad_spatial: dict[tuple[int, int], list[Point]] = defaultdict(list)
        for _p in state.board.pad_positions():
            pad_spatial[(int(round(_p.x)), int(round(_p.y)))].append(_p)
        cx, cy = int(round(sx)), int(round(sy))
        for dcx in (-1, 0, 1):
            for dcy in (-1, 0, 1):
                for p in pad_spatial.get((cx + dcx, cy + dcy), []):
                    _check(p.x, p.y)

        # Component pin positions — use comp spatial index; skip locked pins.
        candidates: set[uuid.UUID] = set()
        comp_idx = state.comp_spatial_index()
        for dcx in (-1, 0, 1):
            for dcy in (-1, 0, 1):
                for cid in comp_idx.get((cx + dcx, cy + dcy), []):
                    candidates.add(cid)

        for comp_id in candidates:
            if exclude_ids and comp_id in exclude_ids:
                continue
            if comp_id in locked_comp_ids:
                for pin_num, p in state.iter_pin_positions(comp_id):
                    if (comp_id, pin_num) not in exported_pins:
                        continue
                    _check(p.x, p.y)
            else:
                for _pin_num, p in state.iter_pin_positions(comp_id):
                    _check(p.x, p.y)

        if best is not None:
            return best
        # No real pad or pin within the 3×3 search window — return the
        # nearest hypothetical pad-grid centre so off-board cursors still
        # snap to a half-integer grid.
        return Point(math.floor(sx) + 0.5, math.floor(sy) + 0.5)

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
        ran = self._execute_silent(cmd)
        if ran:
            self._state.notify_changed()
        return ran

    def _execute_silent(self, cmd: object) -> bool:
        """Execute *cmd* without firing the change hook.

        For multi-command verbs (the group promotion sequence) that issue
        several commands and notify once at the end — mirroring the tool's
        ``_exec_command`` + single ``layout_changed`` emit.
        """
        try:
            self._state.stack.execute(
                cast("Command[ComponentState]", cmd),
                cast("ComponentState", self._state),
            )
        except CommandRejectedError:
            return False
        return True


__all__ = ["EditController", "EditingContext", "ResolvedPlacement"]
