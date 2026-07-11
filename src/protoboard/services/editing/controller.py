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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from protoboard.domain.commands.base import (
    Command,
    CommandRejectedError,
    CommandStack,
    ComponentState,
    SchematicState,
)

if TYPE_CHECKING:
    from pathlib import Path

    from protoboard.domain.board.pad_state import PadIndex
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.components.package import PackageFamily
    from protoboard.domain.connectivity.incremental import ConnectivityGraph
    from protoboard.domain.layout.group import ComponentGroup
    from protoboard.domain.layout.group_tree import GroupTree
    from protoboard.domain.layout.pad_conflict import ConflictScanner
    from protoboard.domain.layout.placement import ComponentPlacement
    from protoboard.domain.layout.wire import Wire
    from protoboard.domain.routing_settings import RoutingSettings
    from protoboard.geometry import Point, Vector
    from protoboard.services.drc.engine import DRCIssue
    from protoboard.services.editing.item_handle import (
        ComponentHandle,
        GroupHandle,
        ItemId,
        SelectionHandle,
        WireHandle,
    )
    from protoboard.services.editing.placement_conflict import PlacementPrompts
    from protoboard.services.editing.wire_nets import NetMergePrompt
    from protoboard.services.routing.autorouter import AutorouteResult
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
    def run_drc(self) -> list[DRCIssue]: ...
    def pad_index(self) -> PadIndex: ...
    def connectivity_graph(self) -> ConnectivityGraph: ...
    def get_pin_position(
        self, comp_id: uuid.UUID, pin_number: int
    ) -> Point | None: ...

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

    def get_item(
        self,
        item_id: ItemId,
        *,
        active_group_id: uuid.UUID | None = None,
    ) -> ComponentHandle | GroupHandle | WireHandle:
        """Address a single edit target by :class:`ItemId`, returning its handle.

        The handle offers only the operations the target's tag supports — a
        wire handle has no rotate / flip. *active_group_id* is the edit context
        a component handle uses for the group-lock guard (defaults to ROOT).
        """
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        from protoboard.services.editing.item_handle import (
            ComponentHandle,
            GroupHandle,
            WireHandle,
        )

        active = active_group_id if active_group_id is not None else ROOT_GROUP_ID
        if item_id.tag == "group":
            return GroupHandle(self, item_id.uuid)
        if item_id.tag == "wire":
            return WireHandle(self, item_id.uuid)
        return ComponentHandle(self, item_id.uuid, active_group_id=active)

    def get_items(
        self,
        item_ids: ItemId | Iterable[ItemId],
        *,
        active_group_id: uuid.UUID | None = None,
    ) -> SelectionHandle:
        """Address a set of targets, returning one :class:`SelectionHandle`.

        A single :class:`ItemId` is accepted and treated as a one-element set.
        Rotate / flip on the returned handle fold every addressed group and the
        loose components into ONE atomic macro — the seam that lets several
        groups rotate in a single undoable step.
        """
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        from protoboard.services.editing.item_handle import ItemId, SelectionHandle

        ids = [item_ids] if isinstance(item_ids, ItemId) else list(item_ids)
        active = active_group_id if active_group_id is not None else ROOT_GROUP_ID
        return SelectionHandle(
            self,
            group_ids=frozenset(i.uuid for i in ids if i.tag == "group"),
            comp_ids=frozenset(i.uuid for i in ids if i.tag == "component"),
            active_group_id=active,
        )

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

        Thin wrapper over :meth:`_rotate_targets`, the shared builder the
        ``ItemId`` handles also drive; here a single ``group_id`` is the
        one-group case of the general multi-group path.
        """
        return self._rotate_targets(
            group_ids=frozenset({group_id}) if group_id is not None else frozenset(),
            comp_ids=frozenset(comp_ids),
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
            cmds[0] if len(cmds) == 1
            else MacroCommand(cmds, description="Rotate selection")
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

        Thin wrapper over :meth:`_flip_targets`, the shared builder the
        ``ItemId`` handles also drive.
        """
        return self._flip_targets(
            group_ids=frozenset({group_id}) if group_id is not None else frozenset(),
            comp_ids=frozenset(comp_ids),
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
            cid for gid in group_ids for cid in all_component_ids(state.layout.groups, gid)
        )
        flip_cmd: object = (
            cmds[0] if len(cmds) == 1
            else MacroCommand(cmds, description="Flip selection")
        )
        if group_members:
            return self._apply_transform(
                flip_cmd, group_members, remove_crossing_wires=remove_crossing_wires,
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
        from protoboard.domain.commands.component_cmds import AddWireWithNetsCommand
        from protoboard.domain.layout.wire import Wire
        from protoboard.domain.layout.wire_utils import (
            board_pads_for_side,
            insert_pad_nodes,
        )
        from protoboard.services.editing.wire_nets import resolve_wire_nets

        if len(path) < 2:
            return None

        state = self._state
        # Pads from BOTH sides: a single-side board returns an empty set for the
        # other side (no effect).
        pads = (
            board_pads_for_side(state.board, "top")
            | board_pads_for_side(state.board, "bottom")
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

    def resolve_placement(
        self,
        ref: str,
        *,
        position: Point,
        rotation: int = 0,
        side: str = "top",
        pin_offsets: dict[int, Vector] | None = None,
        package_variant: str | None = None,
        pin_variant: str | None = None,
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
        from protoboard.domain.components.instance import ComponentInstance
        from protoboard.domain.layout.placement import ComponentPlacement
        from protoboard.services.editing.placement_conflict import check_and_resolve

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
                pin_variant=pin_variant,
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
            instance=inst, placement=placement, wire_cmds=wire_cmds,
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

    def apply_autoroute(self, result: AutorouteResult) -> int:
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
            commands=kept, description=f"Autoroute ({len(kept)} wires)",
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
    ) -> AutorouteResult:
        """Route *net_ids* (or every net when None) synchronously.

        The blocking counterpart to the GUI's background ``AutorouteWorker`` —
        used by the CLI. Builds an immutable ``RouteRequest`` from the current
        state + *settings* and runs the default router on the calling thread.
        Does **not** mutate the layout; pass the returned result to
        :meth:`apply_autoroute` to commit it.
        """
        # Ensure the built-in router/optimizers are registered.
        import protoboard.services.routing.builtin  # noqa: F401
        from protoboard.services.routing.protocol import ProgressSink, registry
        from protoboard.services.routing.snapshot import RouteRequest

        state = self._state
        request = RouteRequest.from_session(
            board=state.board,
            layout=state.layout,
            schematic=state.schematic,
            components=state.components,
            instances=state.instances,
            routing_settings=settings,
            net_ids=net_ids,
            report_path=report_path,
        )
        return registry.default().route(request, ProgressSink())

    def run_drc(self) -> list[DRCIssue]:
        """Run DRC synchronously and return the issues (no side effects).

        Thin pass-through to :meth:`HeadlessSession.run_drc` — the Qt-free
        DRCEngine path. The GUI keeps its background ``DRCWorker``; this is the
        blocking check for the CLI / headless validation.
        """
        return self._state.run_drc()

    def save(self, directory: Path) -> None:
        """Save the current project to *directory* (Qt-free YAML)."""
        from protoboard.services.persistence.project_yaml import save_project

        state = self._state
        save_project(
            directory,
            board=state.board,
            layout=state.layout,
            schematic=state.schematic,
            instances=state.instances,
        )

    def export(self, kind: str, path: Path) -> None:
        """Export the board to *path*. *kind* is ``pdf`` / ``svg`` / ``png``.

        Thin pass-through to the export service. PDF/PNG go through Qt's paint
        pipeline (the caller must have a ``QGuiApplication`` — the CLI sets one
        up with the offscreen platform); the import is local so the edit verbs
        stay Qt-free.
        """
        state = self._state
        common = {
            "board": state.board,
            "layout": state.layout,
            "components": state.components,
            "instances": state.instances,
            "schematic": state.schematic,
        }
        if kind == "pdf":
            from protoboard.services.export.pdf import export_pdf
            export_pdf(path, **common)  # type: ignore[arg-type]
        elif kind == "png":
            from protoboard.services.export.png import export_png
            export_png(path, **common)  # type: ignore[arg-type]
        elif kind == "svg":
            from protoboard.services.export.svg import export_svg
            path.write_text(export_svg(**common))  # type: ignore[arg-type]
        else:
            raise ValueError(f"unknown export kind: {kind!r}")

    def create_group(
        self,
        comp_ids: Iterable[uuid.UUID],
        *,
        active_group_id: uuid.UUID,
        name: str | None = None,
        auto_export: bool = False,
    ) -> uuid.UUID | None:
        """Promote *comp_ids* into a new group under *active_group_id*.

        Splits the selection into free components + top-level groups under the
        active context, mints the new group (optionally auto-exporting its
        external pins), then runs the undo-sensitive create + reparent sequence
        as one batch (a single change hook at the end). Returns the new group id,
        or None when the selection is empty. The target-picker dialog and the
        select-the-new-group side effect stay with the caller.
        """
        ids = frozenset(comp_ids)
        if not ids:
            return None
        new_group, promoted_free, promoted_groups = self._prepare_new_group(
            active_group_id, ids, name=name, auto_export=auto_export,
        )
        self._execute_group_promotion(
            active_group_id, new_group, promoted_free, promoted_groups,
        )
        return new_group.id

    def add_to_group(
        self,
        group_id: uuid.UUID,
        comp_ids: Iterable[uuid.UUID],
        *,
        active_group_id: uuid.UUID,
        auto_export: bool = False,
    ) -> None:
        """Add *comp_ids* to the existing *group_id*.

        Attaches them to the target and detaches them from the active group when
        that differs, then (when *auto_export*) refreshes the target's exported
        pins. One batch, one change hook at the end. Selecting the target stays
        with the caller.
        """
        from protoboard.domain.commands.group_cmds import (
            AddToGroupCommand,
            RemoveFromGroupCommand,
            SetExportedPinsCommand,
        )

        ids = frozenset(comp_ids)
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
                    SetExportedPinsCommand(
                        group_id=group_id, new_exported=new_exported
                    )
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
                RemoveFromGroupCommand(
                    group_id=active_gid, component_ids=promoted_free
                )
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


__all__ = ["EditController", "EditState", "ResolvedPlacement"]
