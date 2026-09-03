# SPDX-License-Identifier: MIT
"""Wire-fragment edit model + commit logic (pure, Qt-free).

A *wire-edit fragment* is the inclusive index range ``[start_idx : end_idx]`` on
one wire (same-wire) or across two wires of the same net (cross-wire).  The
SelectTool treats that fragment as a *selection* (analogous to a selected
component for ``R``); the operations a user can run on it — reroute, autoroute,
delete — are *actions on that selection*.

This module is the Qt-free home for:

* :class:`WireRange` — the fragment value object the tool stores, and
* the command builders the action methods call: :func:`build_reroute_command`,
  :func:`build_delete_command`, :func:`autoroute_fragment_path`.

The builders are verbatim ports of the legacy ``SelectTool`` methods
(``_commit_wire_edit`` / ``_delete_wire_fragment`` / ``_autoroute_wire_fragment``
/ ``_excise_cross_wire_range`` / ``_net_wires_ordered`` / ``_dedupe_adjacent`` /
``_replace_wire_path_cmd``) lifted off ``self`` so they take plain values
(``wires``, the ``wire_net_map``, the :class:`WireRange`) and return reusable
domain :class:`Command` objects.  No Qt, no session, no hit-test — headless
testable.  They reuse the existing domain commands (``ReplaceWireFragmentsCommand``,
``AddWireCommand``, ``RemoveWireCommand``, ``MacroCommand``) and the A* router
verbatim; nothing here re-implements routing or fragment geometry.
"""

from __future__ import annotations

import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from protoboard.domain.commands.base import MacroCommand, compose_command
from protoboard.domain.commands.component_cmds import (
    AddWireCommand,
    FlipWireFragmentCommand,
    RemoveWireCommand,
    ReplaceWireFragmentsCommand,
    SetWireInsulatedCommand,
)
from protoboard.domain.connectivity.incremental import ConnectivityGraph
from protoboard.domain.layout.wire import Fragment, Wire, fragments_for_slice
from protoboard.domain.layout.wire_utils import (
    point_on_pads,
    rebuild_fragments_from_path,
)
from protoboard.domain.routing.cost_model import RoutingCostModel
from protoboard.geometry import Point
from protoboard.types import Side

if TYPE_CHECKING:
    from collections.abc import Sequence

    from protoboard.controller.ports.routing import (
        FindPaths,
        SimplifyInputs,
        SimplifyWire,
    )
    from protoboard.domain.commands.base import Command, ComponentState
    from protoboard.domain.routing.simplify_mode import SimplifyMode


#: Builds the "add this freshly-minted wire" command for the range ops that cut
#: a wire and re-add a surviving/replacement piece (cross-wire excise, reroute,
#: same-wire cut). The default mints a plain, net-unaware ``AddWireCommand`` —
#: correct for same-wire reshapes whose endpoints (and thus derived net) are
#: unchanged, and it keeps this module Qt-free / session-free (its explicit
#: contract). The tool injects a net-aware factory (running ``resolve_wire_nets``
#: over the live session) when a replacement's endpoint may land on a pin and so
#: needs schematic reclassification. See ``docs/core-wire-semantics.md`` §5.
AddWireFactory = Callable[[Wire], "Command[ComponentState]"]


def _default_add_factory(wire: Wire) -> Command[ComponentState]:
    """The purity-preserving default: a plain, net-unaware ``AddWireCommand``."""
    return cast("Command[ComponentState]", AddWireCommand(wire=wire))


__all__ = [
    "AddWireFactory",
    "WireRange",
    "WireRangeEdit",
    "autoroute_fragment_path",
    "build_delete_command",
    "build_delete_range_edit",
    "build_reroute_command",
    "build_set_insulated_range_command",
    "build_simplify_range_command",
    "dedupe_adjacent",
    "excise_cross_wire_range",
    "net_wires_ordered",
    "outer_endpoint_idx",
    "replace_wire_path_cmd",
]


@dataclass(frozen=True, slots=True)
class WireRange:
    """An inclusive index range on one wire (or across two same-net wires).

    The *single* granularity every wire operation targets: an inclusive
    ``[start_idx : end_idx]`` on a wire's flattened path. A **whole wire** is the
    special case ``full_span(wire) = [0, len(path)-1]`` — so a select-mode
    whole-wire op and a wire-edit fragment op are the same operation on different
    ranges (``docs/core-wire-semantics.md``: "reduce op-on-wire to op-on-range").

    Same-wire when ``start_wire_id == end_wire_id`` (then ``start_idx <= end_idx``
    by construction); cross-wire when the two ids differ (sibling wires of the
    same net).  ``waypoints`` holds the hand-drawn reroute points of the *active*
    reroute operation (empty otherwise).  Constructed only when both endpoints
    are chosen — the "armed but incomplete" state is ``None`` in the tool.

    ``WireRange`` is shared by whole-wire and fragment operations.
    """

    start_wire_id: uuid.UUID
    start_idx: int
    end_wire_id: uuid.UUID
    end_idx: int
    waypoints: tuple[Point, ...] = field(default=())

    @classmethod
    def full_span(cls, wire: Wire) -> WireRange:
        """The whole-wire range ``[0 : len(path)-1]`` — the op-on-wire special case."""
        return cls(
            start_wire_id=wire.id,
            start_idx=0,
            end_wire_id=wire.id,
            end_idx=len(wire.path) - 1,
        )

    @property
    def is_cross(self) -> bool:
        """True when the fragment spans two distinct wires (cross-wire edit)."""
        return self.start_wire_id != self.end_wire_id

    def is_full_span(self, wire: Wire) -> bool:
        """True when this range covers *wire* end-to-end (a whole-wire op)."""
        return (
            not self.is_cross
            and self.start_wire_id == wire.id
            and self.start_idx == 0
            and self.end_idx == len(wire.path) - 1
        )

    def with_waypoints(self, waypoints: Sequence[Point]) -> WireRange:
        """Return a copy carrying *waypoints* (the active reroute points)."""
        return WireRange(
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
        return any(point_on_pads(pa, b.path, tol=_eps) for pa in a.path)

    adj: dict[uuid.UUID, list[uuid.UUID]] = {w.id: [] for w in net_wires}
    for i, wa in enumerate(net_wires):
        for wb in net_wires[i + 1 :]:
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
        return point_on_pads(p, neighbour.path, tol=_eps)

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
    state: WireRange,
    add_factory: AddWireFactory = _default_add_factory,
) -> tuple[Command[ComponentState], Point, Point] | None:
    """Build a MacroCommand excising the cross-wire fragment.

    Port of the legacy ``_excise_cross_wire_range``.
    Returns ``(macro, start_pt, end_pt)`` where the points are the physical edit
    boundaries (used to re-route afterwards), or ``None`` when the wires are not
    connected within the net.  Each wire keeps its outer-to-edit sub-path;
    strictly-intermediate wires are removed entirely.

    *add_factory* builds the "add a surviving sub-path" command; the default
    mints a net-unaware ``AddWireCommand`` (kept path/net stable), while the tool
    injects a net-aware factory so a surviving piece whose new endpoint lands on
    a pin re-runs schematic classification (``docs/core-wire-semantics.md`` §5).
    """
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

    def _add(wire: Wire, lo: int, hi: int) -> None:
        # Fragment-style slice — keeps the live per-segment sides of a
        # mixed wire (path-style + scalar wire.side would flatten them).
        cmds.append(
            add_factory(
                Wire(
                    id=uuid.uuid4(),
                    fragments=fragments_for_slice(wire, lo, hi),
                    insulated=wire.insulated,
                    color=wire.color,
                )
            )
        )

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
        _remove(wire)
        lo, hi = (0, edit_idx) if outer_idx == 0 else (edit_idx, len(wire.path) - 1)
        if hi > lo:
            _add(wire, lo, hi)

    # Start wire: keep fragment from its outer endpoint to start_idx.
    if len(ordered) == 1:
        _remove(start_wire)
        if state.start_idx >= 1:
            _add(start_wire, 0, state.start_idx)
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
    state: WireRange,
    add_factory: AddWireFactory = _default_add_factory,
) -> Command[ComponentState] | None:
    """Build the command committing the reroute of *state* with its waypoints.

    Same-wire: ``ReplaceWireFragmentsCommand`` over
    ``prefix[:start+1] + waypoints + suffix[end:]`` (port of ``_commit_wire_edit``
    same-wire branch) — id-preserving, so the derived net is unchanged and no
    ``add_factory`` applies.  Cross-wire: ``MacroCommand[excise, add(new_path)]``
    where ``new_path = [start_pt] + waypoints + [end_pt]`` (cross branch); both
    the excise's surviving pieces and the replacement wire go through
    *add_factory* so a net-aware caller reclassifies them.
    Returns ``None`` when the resulting path is degenerate or the cross-wire
    range cannot be excised.
    """
    waypoints = list(state.waypoints)

    if state.is_cross:
        start_wire = _find_wire(wires, state.start_wire_id)
        # Live side of the segment at the pick start — wire.side is a
        # construction-time snapshot and goes stale after a fragment flip.
        side = (
            _segment_side(start_wire, state.start_idx)
            if start_wire is not None
            else "bottom"
        )
        # Inherit insulation from the rerouted wire — a bare reroute must
        # stay bare (forcing insulated=True silently changed bare wires).
        insulated = start_wire.insulated if start_wire is not None else False
        excise = excise_cross_wire_range(wires, wire_net_map, state, add_factory)
        if excise is None:
            return None
        excise_macro, start_pt, end_pt = excise
        new_path = dedupe_adjacent([start_pt, *waypoints, end_pt])
        if len(new_path) < 2:
            return excise_macro
        new_wire = Wire(id=uuid.uuid4(), path=new_path, insulated=insulated, side=side)
        add_cmd = add_factory(new_wire)
        macro = MacroCommand(
            [excise_macro, add_cmd],
            description="Reroute wire fragment",
        )
        return cast("Command[ComponentState]", macro)

    # Same-wire.
    wire = _find_wire(wires, state.start_wire_id)
    if wire is None:
        return None
    prefix = list(wire.path[: state.start_idx + 1])
    suffix = list(wire.path[state.end_idx :])
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
    state: WireRange,
    add_factory: AddWireFactory = _default_add_factory,
) -> Command[ComponentState] | None:
    """Build the command deleting the fragment (port of ``_delete_wire_fragment``).

    Cross-wire: the excise macro.  Same-wire: whole wire → ``RemoveWireCommand``;
    otherwise cut into the two surviving halves (``path[:start+1]`` and
    ``path[end:]``) via ``Remove + add(half)``.  Returns ``None`` on an invalid
    cross-wire range.  Every fresh surviving piece goes through *add_factory* so
    a net-aware caller reclassifies a half whose new endpoint lands on a pin.
    """
    if state.is_cross:
        excise = excise_cross_wire_range(wires, wire_net_map, state, add_factory)
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

    cmds: list[Command[ComponentState]] = [
        cast("Command[ComponentState]", RemoveWireCommand(wire_id=wire.id))
    ]
    # Fragment-style halves keep the live per-segment sides of a mixed wire.
    for lo, hi in ((0, start), (end, n - 1)):
        if hi > lo:
            cmds.append(
                add_factory(
                    Wire(
                        id=uuid.uuid4(),
                        fragments=fragments_for_slice(wire, lo, hi),
                        insulated=wire.insulated,
                        color=wire.color,
                    )
                )
            )
    command = compose_command(cmds, description="Cut wire fragment")
    assert command is not None
    return command


@dataclass(frozen=True)
class WireRangeEdit:
    """Outcome of a range edit that may have cut a wire.

    ``was_cut`` is True when the op split the wire (increased the wire count),
    in which case ``survivor_wire_ids`` are the freshly-minted surviving pieces
    the tool should re-arm against. For an id-preserving full-span op both are
    empty / False.
    """

    command: Command[ComponentState]
    survivor_wire_ids: tuple[uuid.UUID, ...]
    was_cut: bool


def build_set_insulated_range_command(
    wires: Sequence[Wire],
    wire_net_map: dict[uuid.UUID, uuid.UUID],
    state: WireRange,
    insulated: bool,
    add_factory: AddWireFactory = _default_add_factory,
) -> WireRangeEdit | None:
    """Set the selected sub-range's insulation, splitting the wire if needed.

    Same-wire full span → an id-preserving ``SetWireInsulatedCommand``.
    Same-wire sub-range → split into up to three pieces (left survivor /
    insulated target / right survivor), each a fresh ``Wire`` minted through
    *add_factory*; the target carries the new *insulated*, the survivors keep
    the original.  Cross-wire is refused (``None``).  A no-op (slice already at
    *insulated*) also returns ``None``.
    """
    if state.is_cross:
        return None  # Cross-wire set-insulated deferred (see docs/todo.md).

    wire = _find_wire(wires, state.start_wire_id)
    if wire is None:
        return None
    if wire.insulated == insulated:
        return None  # no-op

    start, end = state.start_idx, state.end_idx
    n = len(wire.path)
    if start == 0 and end == n - 1:
        return WireRangeEdit(
            command=cast(
                "Command[ComponentState]",
                SetWireInsulatedCommand(wire_id=wire.id, insulated=insulated),
            ),
            survivor_wire_ids=(),
            was_cut=False,
        )

    cmds: list[Command[ComponentState]] = [
        cast("Command[ComponentState]", RemoveWireCommand(wire_id=wire.id))
    ]
    survivors: list[uuid.UUID] = []
    # Fragment-style slices preserve each segment's live per-fragment side —
    # a path-style rebuild with the scalar ``wire.side`` would flatten a
    # previously flipped section back onto one layer.
    for lo, hi, keep_insulated, is_survivor in (
        (0, start, wire.insulated, True),
        (start, end, insulated, False),
        (end, n - 1, wire.insulated, True),
    ):
        if hi > lo:
            new_id = uuid.uuid4()
            cmds.append(
                add_factory(
                    Wire(
                        id=new_id,
                        fragments=fragments_for_slice(wire, lo, hi),
                        insulated=keep_insulated,
                        color=wire.color,
                    )
                )
            )
            if is_survivor:
                survivors.append(new_id)
    macro_desc = "Insulate wire fragment" if insulated else "Bare wire fragment"
    return WireRangeEdit(
        command=cast(
            "Command[ComponentState]", MacroCommand(cmds, description=macro_desc)
        ),
        survivor_wire_ids=tuple(survivors),
        was_cut=True,
    )


def build_delete_range_edit(
    wires: Sequence[Wire],
    wire_net_map: dict[uuid.UUID, uuid.UUID],
    state: WireRange,
    add_factory: AddWireFactory = _default_add_factory,
) -> WireRangeEdit | None:
    """``build_delete_command`` wrapped as a ``WireRangeEdit``.

    Captures the fresh survivor ids by wrapping *add_factory*, and reports
    ``was_cut=True`` whenever a delete actually removed wire (always, for a
    valid range).  Full-span delete removes the whole wire → no survivors →
    the tool exits; a sub-range delete leaves the two halves as survivors.
    """
    survivors: list[uuid.UUID] = []

    def _capture(wire: Wire) -> Command[ComponentState]:
        survivors.append(wire.id)
        return add_factory(wire)

    cmd = build_delete_command(wires, wire_net_map, state, _capture)
    if cmd is None:
        return None
    return WireRangeEdit(command=cmd, survivor_wire_ids=tuple(survivors), was_cut=True)


def _segment_side(wire: Wire, seg_idx: int) -> Side:
    """Current side of the wire segment ``path[seg_idx] -> path[seg_idx+1]``.

    A fragment spans ``len(fragment.path) - 1`` consecutive segments; walking
    the fragment list in order maps each segment to its fragment's side. This
    is the live per-fragment side (unlike ``wire.side``, a construction-time
    snapshot). Falls back to ``wire.side`` for a wire with no fragment list.
    """
    if not wire.fragments:
        return wire.side
    cursor = 0
    for frag in wire.fragments:
        span = len(frag.path) - 1
        if cursor <= seg_idx < cursor + span:
            return frag.side
        cursor += span
    return wire.fragments[-1].side


def build_flip_fragment_command(
    wires: Sequence[Wire],
    state: WireRange,
    target_side: str | None = None,
) -> Command[ComponentState] | None:
    """Build the command flipping the selected fragment to the opposite layer.

    Same-wire only: a fragment-flip re-sides one contiguous index range of a
    single wire (the conductor stays one :class:`Wire`; the boundary vertices
    become vias).  A cross-wire selection has no single wire to flip, so this
    returns ``None``.  ``target_side`` defaults to the opposite of the
    *selected range's* current side.  Returns ``None`` when the wire is missing
    or the range is degenerate (a single vertex cannot be a fragment).
    """
    if state.is_cross:
        return None
    wire = _find_wire(wires, state.start_wire_id)
    if wire is None:
        return None
    start, end = state.start_idx, state.end_idx
    if start >= end:
        return None
    # Toggle relative to the side of the fragment covering the range's first
    # segment — NOT ``wire.side``, which is a construction-time snapshot that
    # FlipWireFragmentCommand never updates (so it goes stale after one flip,
    # and repeated flips would target the same side forever).
    current_side = _segment_side(wire, start)
    side = target_side or ("bottom" if current_side == "top" else "top")
    return cast(
        "Command[ComponentState]",
        FlipWireFragmentCommand(
            wire_id=wire.id, start_idx=start, end_idx=end, target_side=side
        ),
    )


def build_simplify_range_command(
    layout: object,
    wires: Sequence[Wire],
    state: WireRange,
    inputs: SimplifyInputs,
    mode: SimplifyMode,
    simplify_wire: SimplifyWire,
) -> Command[ComponentState] | None:
    """Simplify one *range* of a wire — whole wire is the full-span special case.

    Same-wire only. Extracts the slice ``wire.path[start_idx:end_idx+1]`` as a
    temporary :class:`Wire` **carrying the original wire's id** (so
    ``generic_simplify_wire``'s own-wire self-exclusion works and the untouched
    prefix/suffix are not treated as obstacles), simplifies it, then re-stitches
    ``prefix + simplified-interior + suffix`` and issues an id-preserving
    :func:`replace_wire_path_cmd` (``ReplaceWireFragmentsCommand``). Because the
    wire id is preserved, the derived net is unchanged — no ``add_factory`` needed.

    *inputs* is a ``SimplifyInputs`` snapshot (from ``wire_ops.build_simplify_inputs``)
    and *mode* a ``SimplifyMode`` — taken as ``object`` to keep this module free of
    the ``services.routing`` import at module load; resolved lazily here.

    Returns ``None`` for a cross-wire range, a missing/degenerate wire, or when
    simplification changes nothing.

    Note (BARE_PARITY edge): ``generic_simplify_wire`` anchors bare-diagonal
    parity on ``wire.path[0]``. For a *mid-wire bare slice* the temp slice's
    ``path[0]`` is the slice start, whose grid parity may differ from the real
    wire origin — so a bare + BARE_PARITY sub-range simplify may pick a
    wrong-parity diagonal. Full-span ranges and insulated / CHEBYSHEV modes are
    unaffected; callers that need bare sub-range parity must thread the true
    origin (tracked in the plan, docs §5 / Stage 5 caveat).
    """
    if state.is_cross:
        return None
    wire = _find_wire(wires, state.start_wire_id)
    if wire is None or len(wire.path) < 2:
        return None
    start, end = state.start_idx, state.end_idx
    if start >= end:
        return None

    other_wires = [w for w in wires if w.id != wire.id]

    if state.is_full_span(wire):
        slice_wire = wire
    else:
        # Slice as a standalone wire, KEEPING the id (self-exclusion + parity).
        slice_wire = Wire(
            id=wire.id,
            path=list(wire.path[start : end + 1]),
            insulated=wire.insulated,
            side=wire.side,
            color=wire.color,
        )

    simplified = simplify_wire(inputs.request(slice_wire, other_wires, mode))
    if simplified is slice_wire or simplified.path == slice_wire.path:
        return None  # nothing shortened

    if state.is_full_span(wire):
        new_path = list(simplified.path)
    else:
        prefix = list(wire.path[:start])
        suffix = list(wire.path[end + 1 :])
        new_path = dedupe_adjacent(prefix + list(simplified.path) + suffix)
    if new_path == list(wire.path) or len(new_path) < 2:
        return None
    return cast(
        "Command[ComponentState]",
        replace_wire_path_cmd(layout, wire, new_path, list(wire.path)),
    )


def autoroute_fragment_path(
    find_paths: FindPaths,
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
