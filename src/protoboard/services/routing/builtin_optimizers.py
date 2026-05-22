# SPDX-License-Identifier: MIT
"""Built-in wire optimizers, registered at import time.

Importing this module is sufficient to make the default optimizers
available to the worker pipeline.  External plugin discovery happens
separately and shares the same registry.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import TYPE_CHECKING

from protoboard.services.routing.optimizer import (
    OptimizerCapabilities,
    WireOptimizer,
    registry,
)

if TYPE_CHECKING:
    from protoboard.domain.board.pad_state import PadIndex
    from protoboard.geometry import Point
    from protoboard.services.routing.autorouter import AutorouteResult
    from protoboard.services.routing.protocol import ProgressSink
    from protoboard.services.routing.snapshot import RouteRequest


@dataclasses.dataclass(frozen=True)
class _PadContext:
    """Shared per-result pad state — used by every optimizer that needs
    PadIndex authority + pin-net maps + board pads.

    Built once per ``optimize()`` call from the immutable RouteRequest and
    the wires from the current AutorouteResult.
    """

    pad_index: "PadIndex"
    all_pins: set["Point"]
    pad_to_net: dict["Point", uuid.UUID]
    pin_net_map: dict["Point", uuid.UUID]
    point_side_to_net: dict[tuple["Point", str], uuid.UUID]
    all_board_pads: set["Point"]
    worker_layout: object  # Layout — kept loosely typed to avoid the import at module level
    worker_schematic: object  # Schematic
    components: dict
    instances: dict


def _build_pad_context(
    request: "RouteRequest",
    wires: list,
) -> _PadContext:
    """Construct the PadIndex + pin-net helpers shared by all optimizers.

    Mirrors the boilerplate ``SimplifyOptimizer`` used to inline.
    """
    from protoboard.domain.board.pad_state import PadIndex
    from protoboard.domain.layout.layout import Layout
    from protoboard.domain.layout.placement import pin_world_position
    from protoboard.domain.layout.wire_utils import board_pads_for_side
    from protoboard.geometry import Point

    board = request.board
    worker_layout = request.layout.to_layout()
    worker_schematic = request.schematic.to_schematic()
    components = dict(request.components)
    instances = dict(request.instances)

    all_pin_positions: dict[tuple[Point, str], uuid.UUID] = {}
    point_side_to_net: dict[tuple[Point, str], uuid.UUID] = {}
    _FLOATING = uuid.UUID("00000000-0000-0000-0000-000000000002")
    for net in worker_schematic.nets.values():
        for ref in net.pins:
            placement = worker_layout.placements.get(ref.component_id)
            if placement is None:
                continue
            inst = instances.get(ref.component_id)
            if inst is None:
                continue
            defn = components.get(inst.definition_ref)
            if defn is None:
                continue
            if ref.pin_number < 1 or ref.pin_number > defn.footprint.pin_count():
                continue
            pt = pin_world_position(defn.footprint, placement, ref.pin_number)
            point_side_to_net[(pt, "top")] = net.id
            point_side_to_net[(pt, "bottom")] = net.id
    for comp_id, placement in worker_layout.placements.items():
        inst = instances.get(comp_id)
        if inst is None:
            continue
        defn = components.get(inst.definition_ref)
        if defn is None:
            continue
        for pin_num in range(1, defn.footprint.pin_count() + 1):
            pt = pin_world_position(defn.footprint, placement, pin_num)
            ps_top = (pt, "top")
            ps_bot = (pt, "bottom")
            net_id = point_side_to_net.get(ps_top, _FLOATING)
            all_pin_positions[ps_top] = net_id
            all_pin_positions[ps_bot] = net_id

    all_pins: set[Point] = {pos for pos, _side in all_pin_positions}
    pad_to_net: dict[Point, uuid.UUID] = {}
    for (pos, _side), nid in all_pin_positions.items():
        pad_to_net.setdefault(pos, nid)

    # pin_net_map: only pads from named (non-floating) nets — used by
    # ``_candidate_pins_alien``.
    pin_net_map: dict[Point, uuid.UUID] = {
        pos: nid for pos, nid in pad_to_net.items() if nid != _FLOATING
    }

    all_board_pads = (
        board_pads_for_side(board, "top")
        | board_pads_for_side(board, "bottom")
    )

    layout_for_pads = Layout(
        placements=dict(worker_layout.placements),
        wires=list(wires),
    )
    layout_for_pads.ensure_root_group()
    pad_index = PadIndex(
        board, layout_for_pads, worker_schematic, components, instances,
    )

    return _PadContext(
        pad_index=pad_index,
        all_pins=all_pins,
        pad_to_net=pad_to_net,
        pin_net_map=pin_net_map,
        point_side_to_net=point_side_to_net,
        all_board_pads=all_board_pads,
        worker_layout=worker_layout,
        worker_schematic=worker_schematic,
        components=components,
        instances=instances,
    )


class SimplifyOptimizer:
    """Shorten wires by running the generic shortcut algorithm.

    Wraps :func:`simplify_wires_generic` (the legacy "simplify pass").
    Only useful for routes that contain bare diagonals — the algorithm
    is a no-op on routes that have no candidate shortcuts.  Safe to run
    on any layout: returns the input unchanged when no improvement is
    found.

    The optimizer constructs a throw-away ``Layout`` over the result's
    wires so the per-vertex ``PadIndex`` checks have authoritative
    state, mirroring what ``autoroute()`` used to do inline.
    """

    name = "simplify"
    display_name = "Simplify (shorten paths)"

    def __init__(
        self,
        mode: "object | None" = None,
    ) -> None:
        # `mode` is the SimplifyMode enum, kept loosely typed here to
        # avoid the postprocess import at module load.  Resolved to a
        # concrete mode on first optimize() call.
        self._mode = mode

    def optimize(
        self,
        request: "RouteRequest",
        result: "AutorouteResult",
        sink: "ProgressSink",
    ) -> "AutorouteResult":
        from protoboard.services.routing.autorouter import AutorouteResult
        from protoboard.services.routing.postprocess import (
            SimplifyMode,
            simplify_wires_generic,
        )

        if not result.wires:
            return result
        if sink.cancelled():
            return result

        eff_mode = self._mode if self._mode is not None else SimplifyMode.BARE_PARITY
        if not isinstance(eff_mode, SimplifyMode):
            eff_mode = SimplifyMode.BARE_PARITY

        ctx = _build_pad_context(request, result.wires)

        simplified = simplify_wires_generic(
            result.wires,
            pin_positions=ctx.all_pins,
            pad_to_net=ctx.pad_to_net,
            mode=eff_mode,
            board_pads=ctx.all_board_pads,
            pad_index=ctx.pad_index,
        )

        return AutorouteResult(
            wires=simplified,
            unrouted_nets=list(result.unrouted_nets),
            pass_stats=list(result.pass_stats),
        )

    def capabilities(self) -> OptimizerCapabilities:
        return OptimizerCapabilities(
            shortens_paths=True,
            notes=(
                "Runs the generic shortcut algorithm to shorten routes by "
                "finding cheaper subpaths.  Most effective with the "
                "bare-diagonal wire mode; no-op when no shortcuts exist."
            ),
        )


class InsulatedToBareOptimizer:
    """Convert insulated wires to bare where electrically safe.

    Pure type-flip: ``wire.insulated`` is changed from True to False
    without touching ``wire.path``.  Path-level shortening is the job of
    :class:`SimplifyOptimizer` and should be chained AFTER this optimizer:
    ``["insulated_to_bare", "simplify"]``.

    The conversion is driven by post-routing safety predicates from
    :mod:`protoboard.services.routing.postprocess`, all of which are
    reused as-is:

    * Per-vertex authorisation via ``PadIndex.can_route_through(...,
      wire_type="bare")``.
    * Mid-segment pad scan via ``_candidate_safe_via_pad_index`` with
      ``wire_is_insulated=False`` — covers the case where the path
      skips a pad that the bare conductor would physically rest on.
    * Bare×bare crossings against every other wire in the result via
      ``_candidate_safe_against_others``.
    * (When the project routes in bare-diagonal mode) the parity rule
      via ``_parity_ok_for_bare``.

    Strategy: single greedy pass.  Wires are sorted by ascending path
    length; short wires usually flip safely first, which can unlock
    longer ones because the other-wires list passed to the geometric
    check loses a layer of "insulated air" as flips happen.
    """

    name = "insulated_to_bare"
    display_name = "Convert insulated → bare where safe"

    def optimize(
        self,
        request: "RouteRequest",
        result: "AutorouteResult",
        sink: "ProgressSink",
    ) -> "AutorouteResult":
        from protoboard.services.routing.autorouter import AutorouteResult
        from protoboard.services.routing.postprocess import (
            _candidate_pins_alien,
            _candidate_safe_against_others,
            _candidate_safe_via_pad_index,
            _parity_ok_for_bare,
        )

        if not result.wires:
            return result
        if sink.cancelled():
            return result

        # Nothing to flip if the project is in pure-bare routing — every
        # wire is already bare.  But the optimizer must still be safe to
        # run (it just no-ops).
        flippable = [w for w in result.wires if w.insulated]
        if not flippable:
            return result

        ctx = _build_pad_context(request, result.wires)

        # Build the wire-id → net-id map needed by
        # ``_candidate_safe_against_others``.  A wire's net is the net of
        # the pin sitting on either of its endpoints; lookup via
        # ``pin_net_map`` (keyed by Point).
        wire_to_net: dict[uuid.UUID, "uuid.UUID | None"] = {}
        for w in result.wires:
            net_id: "uuid.UUID | None" = None
            for endpoint in (w.path[0], w.path[-1]):
                cand = ctx.pin_net_map.get(endpoint)
                if cand is not None:
                    net_id = cand
                    break
            wire_to_net[w.id] = net_id

        # Detect bare_diagonal mode — only then is the parity rule active.
        rs = request.routing_settings
        bare_diagonal_mode = getattr(rs, "wire_mode", None) == "bare_diagonal"

        # Operate on a mutable copy of the wires so each flip is visible
        # to subsequent safety checks (they see *current* wire types).
        working: list = list(result.wires)
        # Index by id for O(1) replacement.
        by_id = {w.id: idx for idx, w in enumerate(working)}

        # Sort flip-candidates by path length ascending — short wires
        # almost always succeed and reduce the "insulated-other"
        # constraint on longer wires.
        order = sorted(flippable, key=lambda w: len(w.path))

        if sink.cancelled():
            return result

        flipped_count = 0
        for cand in order:
            if sink.cancelled():
                break
            # The wire as currently stored (it may have been replaced
            # by an earlier flip — read fresh).
            cur = working[by_id[cand.id]]
            if not cur.insulated:
                continue  # already flipped (defensive)
            net_id = wire_to_net.get(cur.id)
            candidate_segs = list(zip(cur.path, cur.path[1:]))
            candidate_vertices = list(cur.path)
            original_endpoints = (cur.path[0], cur.path[-1])

            # 1. Bare-diagonal parity rule.
            if bare_diagonal_mode:
                src_parity = (
                    int(cur.path[0].x // 1) + int(cur.path[0].y // 1)
                ) % 2
                if not _parity_ok_for_bare(candidate_segs, src_parity):
                    continue

            # 2. Alien-pin pass-through check (bare wires can't run over
            #    a pin of another net even if the path doesn't terminate
            #    there).
            if _candidate_pins_alien(
                candidate_segs,
                candidate_vertices,
                ctx.pin_net_map,
                net_id,
                list(cur.path),
            ):
                continue

            # 3. PadIndex-authorised vertex + mid-segment pad scan.
            if not _candidate_safe_via_pad_index(
                candidate_vertices,
                original_endpoints,
                ctx.pad_index,
                cur.side,
                net_id,
                wire_is_insulated=False,
            ):
                continue

            # 4. Bare×bare against all other wires (including those just
            #    flipped in this pass).
            other_wires = [w for w in working if w.id != cur.id]
            other_nets = {w.id: wire_to_net.get(w.id) for w in other_wires}
            if not _candidate_safe_against_others(
                candidate_segs,
                other_wires,
                cur.id,
                wire_side=cur.side,
                wire_net_id=net_id,
                other_wire_nets=other_nets,
                original_endpoints=original_endpoints,
            ):
                continue

            # All checks pass — flip.
            from protoboard.domain.layout.wire import WireSegment
            flipped_wire = WireSegment(
                id=cur.id,
                path=list(cur.path),
                insulated=False,
                side=cur.side,
                color=cur.color,
            )
            working[by_id[cur.id]] = flipped_wire
            flipped_count += 1

        if flipped_count == 0:
            return result

        return AutorouteResult(
            wires=working,
            unrouted_nets=list(result.unrouted_nets),
            pass_stats=list(result.pass_stats),
        )

    def capabilities(self) -> OptimizerCapabilities:
        return OptimizerCapabilities(
            insulated_to_bare=True,
            notes=(
                "Flips insulated wires to bare where the surrounding "
                "state proves it is safe (PadIndex authorisation + "
                "bare×bare crossing checks + alien-pin pass-through).  "
                "Run BEFORE 'simplify' so the simplifier can use bare-"
                "specific shortcuts (corner cuts, bare-diagonals)."
            ),
        )


# Static protocol assertion — fails fast if anyone breaks the contract.
_check_simplify: WireOptimizer = SimplifyOptimizer()
_check_i2b: WireOptimizer = InsulatedToBareOptimizer()
del _check_simplify
del _check_i2b


def register_builtins() -> None:
    """Add all built-in optimizers to the global registry.  Idempotent."""
    registry.register(SimplifyOptimizer())
    registry.register(InsulatedToBareOptimizer())


register_builtins()


__all__ = [
    "SimplifyOptimizer",
    "InsulatedToBareOptimizer",
    "register_builtins",
]
