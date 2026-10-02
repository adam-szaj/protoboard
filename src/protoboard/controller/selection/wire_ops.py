# SPDX-License-Identifier: MIT
"""Prepare wire-operation computations and undo commands for selection tools."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from protoboard.controller.ports.routing import SimplifyInputs
from protoboard.controller.selection.wire_edit import replace_wire_path_cmd
from protoboard.domain.commands.component_cmds import (
    AddWireCommand,
    FuseTouchingWiresCommand,
    RemoveWireCommand,
    ReplaceWireFragmentsCommand,
)
from protoboard.domain.layout.wire import Fragment
from protoboard.domain.layout.wire_utils import (
    board_pads_union,
    coalesce_same_side_fragments,
)
from protoboard.geometry import Point

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from protoboard.controller.editing.contexts import EditingReadContext
    from protoboard.controller.ports.routing import SimplifyWire
    from protoboard.domain.commands.base import Command, ComponentState
    from protoboard.domain.layout.layout import Layout
    from protoboard.domain.layout.wire import Wire
    from protoboard.domain.routing.simplify_mode import SimplifyMode


SimplifyChange = tuple[uuid.UUID, list[Point], list[Point]]


def build_simplify_inputs(session: EditingReadContext) -> SimplifyInputs:
    """Collect the simplify-pass inputs from session-level caches.

    * ``pin_positions`` — every component pin position ("don't drop a pin").
    * ``pad_to_net`` — assigned pad position → net id (alien-pad detection).
    * ``board_pads`` — union of top + bottom board pads (bare wires keep
      pad-vertices on shortcuts so connectivity stays visible).
    * ``wire_to_net`` — wire id → net id, from the session's cached map.
    * ``pad_index`` — the authoritative pad-state source.
    """
    pin_positions: set[Point] = set()
    for comp_id in session.layout.placements:
        for _pin_num, pin_pos in session.iter_pin_positions(comp_id):
            pin_positions.add(pin_pos)

    pad_to_net: dict[Point, uuid.UUID] = {}
    for net_obj in session.schematic.nets.values():
        for ref in net_obj.pins:
            pad_pos = session.get_pin_position(ref.component_id, ref.pin_number)
            if pad_pos is not None:
                pad_to_net.setdefault(pad_pos, net_obj.id)

    board_pads = board_pads_union(session.board)

    # wire id → net id via the session cache (replaces the legacy manual
    # path-intersection loop).  cached_wire_net_map only holds wires with a
    # resolvable net; absent ⇒ None, matching the old behaviour.
    cached = session.cached_wire_net_map()
    wire_to_net: dict[uuid.UUID, uuid.UUID | None] = {
        w.id: cached.get(w.id) for w in session.layout.wires
    }

    return SimplifyInputs(
        pin_positions=pin_positions,
        pad_to_net=pad_to_net,
        board_pads=board_pads,
        wire_to_net=wire_to_net,
        pad_index=session.pad_index(),
    )


def prepare_simplify_worker(
    session: EditingReadContext,
    wire_ids: Iterable[uuid.UUID],
    mode: SimplifyMode,
    simplify_wire: SimplifyWire,
) -> Callable[[Callable[[int, int], None]], list[SimplifyChange]]:
    """Capture inputs before launch and return the read-only simplify computation."""
    inputs = build_simplify_inputs(session)
    all_wires = list(session.layout.wires)
    wires_by_id = {wire.id: wire for wire in all_wires}
    wid_list = list(wire_ids)

    def _simplify_in_worker(
        progress_cb: Callable[[int, int], None],
    ) -> list[SimplifyChange]:
        results: list[SimplifyChange] = []
        n = len(wid_list)
        for i, wid in enumerate(wid_list):
            wire = wires_by_id.get(wid)
            if wire is None:
                progress_cb(i + 1, n)
                continue
            new_wire = simplify_wire(
                inputs.request(
                    wire,
                    [w for w in all_wires if w.id != wid],
                    mode,
                )
            )
            if new_wire is not wire and new_wire.path != wire.path:
                results.append((wid, list(new_wire.path), list(wire.path)))
            progress_cb(i + 1, n)
        return results

    return _simplify_in_worker


def build_simplify_commands(
    layout: Layout,
    changes: Sequence[SimplifyChange],
) -> list[Command[ComponentState]]:
    """Prepare path replacements, ignoring wires removed while the worker ran."""
    cmds: list[Command[ComponentState]] = []
    for wid, new_path, orig_path in changes:
        wire = layout.wires.get(wid)
        if wire is None:
            continue  # vanished mid-flight (concurrent edit)
        cmds.append(replace_wire_path_cmd(layout, wire, new_path, orig_path))
    return cmds


def build_unweave_commands(
    targets: Sequence[Wire],
    output_wires: Sequence[Wire],
) -> list[Command[ComponentState]]:
    """Replace the selected topology only when geometry or wire attributes changed."""
    same = len(output_wires) == len(targets) and all(
        a.path == b.path and a.side == b.side and a.insulated == b.insulated
        for a, b in zip(targets, output_wires)
    )
    if same:
        return []
    cmds: list[Command[ComponentState]] = [
        RemoveWireCommand(wire_id=w.id) for w in targets
    ]
    cmds += [AddWireCommand(wire=w) for w in output_wires]
    return cmds


def _build_fragment_collapse_commands(
    layout: Layout,
    post_fuse_ids: set[uuid.UUID],
) -> tuple[list[Command[ComponentState]], int, int]:
    """Coalesce fragments against the temporarily applied post-fuse layout."""
    all_wires = layout.wires
    frag_cmds: list[Command[ComponentState]] = []
    n_fragment_wires = 0
    n_fragments_removed = 0
    for wid in post_fuse_ids:
        wire = all_wires.get(wid)
        if wire is None or len(wire.fragments) < 2:
            continue
        coalesced = coalesce_same_side_fragments(wire.fragments)
        if len(coalesced) == len(wire.fragments):
            continue
        n_fragment_wires += 1
        n_fragments_removed += len(wire.fragments) - len(coalesced)
        layout.ensure_wire_knots(wire)
        old_fragments = tuple(
            Fragment(path=list(f.path), side=f.side) for f in wire.fragments
        )
        frag_cmds.append(
            ReplaceWireFragmentsCommand(
                wire_id=wire.id,
                new_fragments=tuple(coalesced),
                old_fragments=old_fragments,
            )
        )
    return frag_cmds, n_fragment_wires, n_fragments_removed


def build_merge_commands(
    state: ComponentState,
    wire_ids: frozenset[uuid.UUID] | None,
) -> tuple[list[Command[ComponentState]], int, int, int]:
    """Prepare an atomic fuse/coalesce sequence, reverting the fuse before return."""
    # Phase 1: apply the fuse to observe the post-fuse layout, build the
    # phase-2 fragment commands against it, then revert so the final macro
    # re-applies both phases atomically.
    fuse_cmd = FuseTouchingWiresCommand(wire_ids=wire_ids)
    fuse_cmd.apply(state)
    n_fused = len(fuse_cmd._records)  # noqa: SLF001 — legacy command contract

    if wire_ids is None:
        post_fuse_ids: set[uuid.UUID] = {w.id for w in state.layout.wires}
    else:
        survived = {wid for wid in wire_ids if wid in state.layout.wires}
        merged = {
            rec.merged_wire_id
            for rec in fuse_cmd._records  # noqa: SLF001
            if rec.merged_wire_id is not None
        }
        post_fuse_ids = survived | merged

    frag_cmds, n_fragment_wires, n_fragments_removed = (
        _build_fragment_collapse_commands(state.layout, post_fuse_ids)
    )

    fuse_cmd.revert(state)
    cmds: list[Command[ComponentState]] = []
    if n_fused > 0:
        cmds.append(fuse_cmd)
    cmds += frag_cmds
    return cmds, n_fused, n_fragment_wires, n_fragments_removed


def build_optimizer_commands(
    selected_wires: Sequence[Wire],
    output_wires: Sequence[Wire],
) -> list[Command[ComponentState]]:
    """Diff optimizer output by wire identity, retaining unchanged input wires."""
    out_by_id = {w.id: w for w in output_wires}
    cmds: list[Command[ComponentState]] = []
    for wire_in in selected_wires:
        wire_out = out_by_id.pop(wire_in.id, None)
        if wire_out is None:
            cmds.append(RemoveWireCommand(wire_id=wire_in.id))
            continue
        if (
            wire_out.path == wire_in.path
            and wire_out.side == wire_in.side
            and wire_out.insulated == wire_in.insulated
        ):
            continue
        # Side/insulation change → full Remove+Add (path-only could reuse
        # ReplaceWireFragments, but mixing complicates undo grouping).
        cmds.append(RemoveWireCommand(wire_id=wire_in.id))
        cmds.append(AddWireCommand(wire=wire_out))
    for new_wire in out_by_id.values():
        cmds.append(AddWireCommand(wire=new_wire))
    return cmds
