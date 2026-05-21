# SPDX-License-Identifier: MIT
"""Built-in wire optimizers, registered at import time.

Importing this module is sufficient to make the default optimizers
available to the worker pipeline.  External plugin discovery happens
separately and shares the same registry.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from protoboard.services.routing.optimizer import (
    OptimizerCapabilities,
    WireOptimizer,
    registry,
)

if TYPE_CHECKING:
    from protoboard.services.routing.autorouter import AutorouteResult
    from protoboard.services.routing.protocol import ProgressSink
    from protoboard.services.routing.snapshot import RouteRequest


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
        from protoboard.domain.board.pad_state import PadIndex
        from protoboard.domain.layout.layout import Layout
        from protoboard.domain.layout.wire_utils import board_pads_for_side
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

        board = request.board
        # Restore mutable Layout / Schematic from snapshot for the helpers.
        worker_layout = request.layout.to_layout()
        worker_schematic = request.schematic.to_schematic()
        components = dict(request.components)
        instances = dict(request.instances)

        # Rebuild pin-position → net mapping the way autoroute() did:
        # every placed pin gets registered on BOTH layers (plated-through
        # hole), and pads from same-net pins are tagged with that net id.
        from protoboard.domain.layout.placement import pin_world_position
        from protoboard.geometry import Point

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

        all_board_pads = (
            board_pads_for_side(board, "top")
            | board_pads_for_side(board, "bottom")
        )

        # Build a throw-away PadIndex over the routed state so the
        # per-vertex checks have authoritative info.
        _simplify_layout = Layout(
            placements=dict(worker_layout.placements),
            wires=list(result.wires),
        )
        _simplify_layout.ensure_root_group()
        pad_index = PadIndex(
            board, _simplify_layout, worker_schematic, components, instances,
        )

        simplified = simplify_wires_generic(
            result.wires,
            pin_positions=all_pins,
            pad_to_net=pad_to_net,
            mode=eff_mode,
            board_pads=all_board_pads,
            pad_index=pad_index,
        )

        # Return a new result preserving everything but the wires.
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


# Static protocol assertion — fails fast if anyone breaks the contract.
_check: WireOptimizer = SimplifyOptimizer()
del _check


def register_builtins() -> None:
    """Add all built-in optimizers to the global registry.  Idempotent."""
    registry.register(SimplifyOptimizer())


register_builtins()


__all__ = ["SimplifyOptimizer", "register_builtins"]
