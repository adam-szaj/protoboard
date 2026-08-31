# SPDX-License-Identifier: MIT
"""Input preparation for the user-triggered wire operations (simplify).

The legacy ``SelectTool._simplify_wires`` hand-built four lookup structures
inline before launching the simplify worker: every pin position, a pad → net
map, the union of board pads, and a wire → net map.  Several of those predate
the session-level caches (``cached_wire_net_map``, :class:`PadIndex`) and
duplicated work those now do.

:func:`build_simplify_inputs` collects that prep in one place so the tool method
is a thin worker launch.  It is Qt-free (takes the session, not the view) and
leans on the existing session caches for the wire → net map.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from protoboard.domain.layout.wire_utils import board_pads_for_side

if TYPE_CHECKING:
    import uuid

    from protoboard.controller.editing import EditingContext
    from protoboard.domain.board.pad_state import PadIndex
    from protoboard.geometry import Point


@dataclass(frozen=True, slots=True)
class SimplifyInputs:
    """The read-only snapshots :func:`generic_simplify_wire` consumes.

    All fields are independent of the live layout (plain values / a cloned
    :class:`PadIndex`), so the simplify pass is safe to run in a worker thread
    concurrently with paint.
    """

    pin_positions: set[Point]
    pad_to_net: dict[Point, uuid.UUID]
    board_pads: set[Point]
    wire_to_net: dict[uuid.UUID, uuid.UUID | None]
    pad_index: PadIndex


def build_simplify_inputs(session: EditingContext) -> SimplifyInputs:
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

    board_pads = board_pads_for_side(session.board, "top") | board_pads_for_side(
        session.board, "bottom"
    )

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
