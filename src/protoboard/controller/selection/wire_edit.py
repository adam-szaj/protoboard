# SPDX-License-Identifier: MIT
"""Wire-fragment edit model + commit logic (pure, Qt-free).

A *wire-edit fragment* is the inclusive index range ``[start_idx : end_idx]`` on
one wire (same-wire) or across two wires of the same net (cross-wire).  The
SelectTool treats that fragment as a *selection* (analogous to a selected
component for ``R``); the operations a user can run on it — reroute, autoroute,
delete — are *actions on that selection*.

This module is the Qt-free home for:

* :class:`WireEditState` — the fragment value object the tool stores, and
* the command builders the action methods call: :func:`build_reroute_command`,
  :func:`build_delete_command`, :func:`autoroute_fragment_path`.

The builders are verbatim ports of the legacy ``SelectTool`` methods
(``_commit_wire_edit`` / ``_delete_wire_fragment`` / ``_autoroute_wire_fragment``
/ ``_excise_cross_wire_range`` / ``_net_wires_ordered`` / ``_dedupe_adjacent`` /
``_replace_wire_path_cmd``) lifted off ``self`` so they take plain values
(``wires``, the ``wire_net_map``, the :class:`WireEditState`) and return reusable
domain :class:`Command` objects.  No Qt, no session, no hit-test — headless
testable.  They reuse the existing domain commands (``ReplaceWireFragmentsCommand``,
``AddWireCommand``, ``RemoveWireCommand``, ``MacroCommand``) and the A* router
verbatim; nothing here re-implements routing or fragment geometry.
"""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from protoboard.domain.commands.base import MacroCommand
from protoboard.domain.commands.component_cmds import (
    AddWireCommand,
    FlipWireFragmentCommand,
    RemoveWireCommand,
    ReplaceWireFragmentsCommand,
)
from protoboard.domain.layout.wire import Fragment, Wire
from protoboard.domain.layout.wire_utils import rebuild_fragments_from_path
from protoboard.geometry import Point

if TYPE_CHECKING:
    from collections.abc import Sequence

    from protoboard.domain.commands.base import Command, ComponentState

__all__ = [
    "WireEditState",
    "autoroute_fragment_path",
    "build_delete_command",
    "build_reroute_command",
    "dedupe_adjacent",
    "excise_cross_wire_range",
    "net_wires_ordered",
    "outer_endpoint_idx",
    "replace_wire_path_cmd",
]


@dataclass(frozen=True, slots=True)
class WireEditState:
    """The selected wire fragment — an inclusive index range on one or two wires.

    Same-wire when ``start_wire_id == end_wire_id`` (then ``start_idx <= end_idx``
    by construction); cross-wire when the two ids differ (sibling wires of the
    same net).  ``waypoints`` holds the hand-drawn reroute points of the *active*
    reroute operation (empty otherwise).  Constructed only when both endpoints
    are chosen — the "armed but incomplete" state is ``None`` in the tool.
    """

    start_wire_id: uuid.UUID
    start_idx: int
    end_wire_id: uuid.UUID
    end_idx: int
    waypoints: tuple[Point, ...] = field(default=())

    @property
    def is_cross(self) -> bool:
        """True when the fragment spans two distinct wires (cross-wire edit)."""
        return self.start_wire_id != self.end_wire_id

    def with_waypoints(self, waypoints: Sequence[Point]) -> WireEditState:
        """Return a copy carrying *waypoints* (the active reroute points)."""
        return WireEditState(
            start_wire_id=self.start_wire_id,
            start_idx=self.start_idx,
            end_wire_id=self.end_wire_id,
            end_idx=self.end_idx,
            waypoints=tuple(waypoints),
        )


# -- small pure geometry helpers (ports of the legacy free/static funcs) -----


def dedupe_adjacent(path: list[Point]) -> list[Point]:
    """Remove consecutive duplicate points (port of ``_dedupe_adjacent``)."""
    if not path:
        return path
    result = [path[0]]
    for pt in path[1:]:
        if abs(pt.x - result[-1].x) > 1e-9 or abs(pt.y - result[-1].y) > 1e-9:
            result.append(pt)
    return result


def replace_wire_path_cmd(
    layout: object,
    wire: Wire,
    new_path: list[Point],
    old_path: list[Point] | None = None,
) -> ReplaceWireFragmentsCommand:
    """Build a ReplaceWireFragmentsCommand for an in-place path edit.

    Port of the legacy module-level ``_replace_wire_path_cmd``.  Ensures the
    wire has v2 endpoint knots, rebuilds fragments from ``new_path`` along the
    existing via boundaries, and snapshots ``old_path`` (defaulting to a
    single-fragment view of ``wire.side``) into ``old_fragments`` for undo.
    Callers that mutated ``wire.path`` in place before issuing the command must
    pass the pre-mutation path as ``old_path``.
    """
    layout.ensure_wire_knots(wire)  # type: ignore[attr-defined]
    if old_path is None:
        old_fragments = tuple(
            Fragment(path=list(f.path), side=f.side) for f in wire.fragments
        )
    elif len(wire.fragments) <= 1:
        old_fragments = (Fragment(path=list(old_path), side=wire.side),)
    else:
        # Multi-fragment wire whose path was changed in place — derive the
        # pre-edit fragments from the old path (same boundary positions).
        old_fragments = tuple(rebuild_fragments_from_path(wire, list(old_path)))
    new_fragments = tuple(rebuild_fragments_from_path(wire, list(new_path)))
    return ReplaceWireFragmentsCommand(
        wire_id=wire.id,
        new_fragments=new_fragments,
        old_fragments=old_fragments,
    )


def net_wires_ordered(
    wires: Sequence[Wire],
    wire_net_map: dict[uuid.UUID, uuid.UUID],
    start_wire_id: uuid.UUID,
    end_wire_id: uuid.UUID,
    net_id: uuid.UUID,
) -> list[Wire] | None:
    """BFS through wires of *net_id* from start to end (port of ``_net_wires_ordered``).

    Two wires are adjacent if any point of one lies within 1e-4 of any point of
    the other (shared pad / endpoint).  Returns the ordered chain
    ``[start_wire, ..., end_wire]`` or ``None`` when not connected.
    """
    net_wires = [w for w in wires if wire_net_map.get(w.id) == net_id]
    wire_by_id = {w.id: w for w in net_wires}

    if start_wire_id not in wire_by_id or end_wire_id not in wire_by_id:
        return None
    if start_wire_id == end_wire_id:
        return [wire_by_id[start_wire_id]]

    _eps = 1e-4

    def _shares_point(a: Wire, b: Wire) -> bool:
        for pa in a.path:
            for pb in b.path:
                if abs(pa.x - pb.x) < _eps and abs(pa.y - pb.y) < _eps:
                    return True
        return False

    adj: dict[uuid.UUID, list[uuid.UUID]] = {w.id: [] for w in net_wires}
    for i, wa in enumerate(net_wires):
        for wb in net_wires[i + 1:]:
            if _shares_point(wa, wb):
                adj[wa.id].append(wb.id)
                adj[wb.id].append(wa.id)

    pred: dict[uuid.UUID, uuid.UUID | None] = {start_wire_id: None}
    queue: deque[uuid.UUID] = deque([start_wire_id])
    while queue:
        cur = queue.popleft()
        if cur == end_wire_id:
            break
        for nxt in adj[cur]:
            if nxt not in pred:
                pred[nxt] = cur
                queue.append(nxt)

    if end_wire_id not in pred:
        return None

    path: list[uuid.UUID] = []
    cur_id: uuid.UUID | None = end_wire_id
    while cur_id is not None:
        path.append(cur_id)
        cur_id = pred[cur_id]
    path.reverse()
    return [wire_by_id[wid] for wid in path]


def outer_endpoint_idx(wire: Wire, neighbour: Wire) -> int:
    """Endpoint index of *wire* NOT shared with *neighbour*.

    Port of the legacy ``_outer_endpoint_idx``.
    A wire in a chain connects to its neighbour at one of its two endpoints; the
    other is the "outer" side preserved when excising an inner fragment.  Falls
    back to 0 when both or neither endpoint touches the neighbour.
    """
    _eps = 1e-4
    first = wire.path[0]
    last = wire.path[-1]

    def _touches(p: Point) -> bool:
        for q in neighbour.path:
            if abs(p.x - q.x) < _eps and abs(p.y - q.y) < _eps:
                return True
        return False

    first_shared = _touches(first)
    last_shared = _touches(last)
    if last_shared and not first_shared:
        return 0
    if first_shared and not last_shared:
        return len(wire.path) - 1
    return 0


def excise_cross_wire_range(
    wires: Sequence[Wire],
    wire_net_map: dict[uuid.UUID, uuid.UUID],
    state: WireEditState,
) -> tuple[Command[ComponentState], Point, Point] | None:
    """Build a MacroCommand excising the cross-wire fragment.

    Port of the legacy ``_excise_cross_wire_range``.
    Returns ``(macro, start_pt, end_pt)`` where the points are the physical edit
    boundaries (used to re-route afterwards), or ``None`` when the wires are not
    connected within the net.  Each wire keeps its outer-to-edit sub-path;
    strictly-intermediate wires are removed entirely.
    """
    from typing import cast

    net_id = wire_net_map.get(state.start_wire_id)
    if net_id is None:
        return None

    ordered = net_wires_ordered(
        wires, wire_net_map, state.start_wire_id, state.end_wire_id, net_id
    )
    if ordered is None:
        return None

    start_wire = ordered[0]
    end_wire = ordered[-1]
    start_pt = start_wire.path[state.start_idx]
    end_pt = end_wire.path[state.end_idx]

    cmds: list[Command[ComponentState]] = []

    def _remove(wire: Wire) -> None:
        cmds.append(cast("Command[ComponentState]", RemoveWireCommand(wire_id=wire.id)))

    def _add(wire: Wire, new_path: list[Point]) -> None:
        cmds.append(cast("Command[ComponentState]", AddWireCommand(wire=Wire(
            id=uuid.uuid4(),
            path=new_path,
            insulated=wire.insulated,
            side=wire.side,
            color=wire.color,
        ))))

    def _keep_fragment(wire: Wire, edit_idx: int, outer_idx: int) -> None:
        """Keep the path slice from outer_idx to edit_idx via Remove + AddWire.

        The slice moves the *inner* endpoint to ``path[edit_idx]``; the wire's
        existing inner knot still points at the original endpoint, so a plain
        replace would fail.  Remove + AddWire gives the new wire fresh knots
        resolved at its actual endpoints.
        """
        if edit_idx == outer_idx:
            _remove(wire)
            return
        new_path = (
            list(wire.path[:edit_idx + 1]) if outer_idx == 0
            else list(wire.path[edit_idx:])
        )
        _remove(wire)
        if len(new_path) >= 2:
            _add(wire, new_path)

    # Start wire: keep fragment from its outer endpoint to start_idx.
    if len(ordered) == 1:
        new_path = list(start_wire.path[:state.start_idx + 1])
        _remove(start_wire)
        if len(new_path) >= 2:
            _add(start_wire, new_path)
    else:
        start_outer = outer_endpoint_idx(start_wire, ordered[1])
        _keep_fragment(start_wire, state.start_idx, start_outer)

    # Intermediate wires: remove entirely.
    for mid_wire in ordered[1:-1]:
        _remove(mid_wire)

    # End wire: keep fragment from end_idx to its outer endpoint.
    if len(ordered) > 1:
        end_outer = outer_endpoint_idx(end_wire, ordered[-2])
        _keep_fragment(end_wire, state.end_idx, end_outer)

    macro = MacroCommand(cmds, description="Excise wire fragment")
    return cast("Command[ComponentState]", macro), start_pt, end_pt


# -- the operation command builders the tool's action methods call -----------


def build_reroute_command(
    layout: object,
    wires: Sequence[Wire],
    wire_net_map: dict[uuid.UUID, uuid.UUID],
    state: WireEditState,
) -> Command[ComponentState] | None:
    """Build the command committing the reroute of *state* with its waypoints.

    Same-wire: ``ReplaceWireFragmentsCommand`` over
    ``prefix[:start+1] + waypoints + suffix[end:]`` (port of ``_commit_wire_edit``
    same-wire branch).  Cross-wire: ``MacroCommand[excise, AddWire(new_path)]``
    where ``new_path = [start_pt] + waypoints + [end_pt]`` (cross branch).
    Returns ``None`` when the resulting path is degenerate or the cross-wire
    range cannot be excised.
    """
    from typing import cast

    waypoints = list(state.waypoints)

    if state.is_cross:
        start_wire = _find_wire(wires, state.start_wire_id)
        side = start_wire.side if start_wire is not None else "bottom"
        # Inherit insulation from the rerouted wire — a bare reroute must
        # stay bare (forcing insulated=True silently changed bare wires).
        insulated = start_wire.insulated if start_wire is not None else False
        excise = excise_cross_wire_range(wires, wire_net_map, state)
        if excise is None:
            return None
        excise_macro, start_pt, end_pt = excise
        new_path = dedupe_adjacent([start_pt, *waypoints, end_pt])
        if len(new_path) < 2:
            return excise_macro
        new_wire = Wire(
            id=uuid.uuid4(), path=new_path, insulated=insulated, side=side
        )
        add_cmd = AddWireCommand(wire=new_wire)
        macro = MacroCommand(
            [excise_macro, cast("Command[ComponentState]", add_cmd)],
            description="Reroute wire fragment",
        )
        return cast("Command[ComponentState]", macro)

    # Same-wire.
    wire = _find_wire(wires, state.start_wire_id)
    if wire is None:
        return None
    prefix = list(wire.path[:state.start_idx + 1])
    suffix = list(wire.path[state.end_idx:])
    new_path = dedupe_adjacent(prefix + waypoints + suffix)
    if len(new_path) < 2:
        return None
    return cast(
        "Command[ComponentState]",
        replace_wire_path_cmd(layout, wire, new_path, list(wire.path)),
    )


def build_delete_command(
    wires: Sequence[Wire],
    wire_net_map: dict[uuid.UUID, uuid.UUID],
    state: WireEditState,
) -> Command[ComponentState] | None:
    """Build the command deleting the fragment (port of ``_delete_wire_fragment``).

    Cross-wire: the excise macro.  Same-wire: whole wire → ``RemoveWireCommand``;
    otherwise cut into the two surviving halves (``path[:start+1]`` and
    ``path[end:]``) via ``Remove + AddWire``.  Returns ``None`` on an invalid
    cross-wire range.
    """
    from typing import cast

    if state.is_cross:
        excise = excise_cross_wire_range(wires, wire_net_map, state)
        if excise is None:
            return None
        return excise[0]

    wire = _find_wire(wires, state.start_wire_id)
    if wire is None:
        return None
    start, end = state.start_idx, state.end_idx
    n = len(wire.path)
    if start == 0 and end == n - 1:
        return cast("Command[ComponentState]", RemoveWireCommand(wire_id=wire.id))

    left_path = list(wire.path[:start + 1])
    right_path = list(wire.path[end:])
    cmds: list[Command[ComponentState]] = [
        cast("Command[ComponentState]", RemoveWireCommand(wire_id=wire.id))
    ]
    for half in (left_path, right_path):
        if len(half) >= 2:
            cmds.append(cast("Command[ComponentState]", AddWireCommand(wire=Wire(
                id=uuid.uuid4(),
                path=half,
                insulated=wire.insulated,
                side=wire.side,
                color=wire.color,
            ))))
    if len(cmds) == 1:
        return cmds[0]
    return cast(
        "Command[ComponentState]",
        MacroCommand(cmds, description="Cut wire fragment"),
    )


def build_flip_fragment_command(
    wires: Sequence[Wire],
    state: WireEditState,
    target_side: str | None = None,
) -> Command[ComponentState] | None:
    """Build the command flipping the selected fragment to the opposite layer.

    Same-wire only: a fragment-flip re-sides one contiguous index range of a
    single wire (the conductor stays one :class:`Wire`; the boundary vertices
    become vias).  A cross-wire selection has no single wire to flip, so this
    returns ``None``.  ``target_side`` defaults to the opposite of the wire's
    current side.  Returns ``None`` when the wire is missing or the range is
    degenerate (a single vertex cannot be a fragment).
    """
    if state.is_cross:
        return None
    wire = _find_wire(wires, state.start_wire_id)
    if wire is None:
        return None
    start, end = state.start_idx, state.end_idx
    if start >= end:
        return None
    side = target_side or ("bottom" if wire.side == "top" else "top")
    from typing import cast

    return cast(
        "Command[ComponentState]",
        FlipWireFragmentCommand(
            wire_id=wire.id, start_idx=start, end_idx=end, target_side=side
        ),
    )


def autoroute_fragment_path(
    graph: object,
    source: Point,
    target: Point,
    side: str,
) -> list[Point] | None:
    """Run A* between *source* and *target*; return the routed point list or None.

    Wraps ``find_paths`` (k=1) exactly as ``_autoroute_wire_fragment`` does.  The
    caller supplies the :class:`ConnectivityGraph` (built from the session) and
    the boundary points/side; this stays Qt-free and returns just the geometry.
    """
    from typing import cast

    from protoboard.domain.connectivity.incremental import ConnectivityGraph
    from protoboard.services.routing.astar import find_paths
    from protoboard.services.routing.cost_model import RoutingCostModel

    results = find_paths(
        graph=cast(ConnectivityGraph, graph),
        source=source,
        targets=[target],
        side=side,
        cost_model=RoutingCostModel(),
        k=1,
    )
    if not results:
        return None
    return [pt for pt, _ in results[0].path]


def _find_wire(wires: Sequence[Wire], wire_id: uuid.UUID) -> Wire | None:
    return next((w for w in wires if w.id == wire_id), None)
