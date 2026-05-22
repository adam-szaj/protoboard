# SPDX-License-Identifier: MIT
"""RoutingCostModel — per-step cost parameters for the A* router."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RoutingCostModel:
    """Configurable cost weights for A* routing.

    All values are additive costs per step.  `inf` means the step is
    forbidden.

    Diagonal moves (8-connectivity) use `free_pad * diagonal_multiplier`
    as their base cost.  The multiplier defaults to √2 so that diagonal
    and straight steps preserve Euclidean distance ratios.

    Wire-crossing semantics:
    - A bare wire crossing another bare wire is a short → `crossing_bare_bare = inf`.
    - An insulated wire crossing a bare wire is a jumper → `crossing_insulated_bare`.
    - An insulated wire crossing another insulated wire is fine → cost 0 extra.
    """

    free_pad: float = 1.0
    same_net_pad: float = 0.1          # discount for pads on the target net
    occupied_by_other_net: float = float("inf")   # pin of another net — blocked
    direction_change: float = 1.0      # penalty per turn (discourages detours)
    diagonal_multiplier: float = math.sqrt(2)     # cost factor for diagonal step
    # Crossing costs — applied when a wire passes over an existing wire segment.
    crossing_bare_bare: float = float("inf")      # bare × bare = short (default)
    # Exception to crossing_bare_bare: two bare diagonals that cross at a
    # mesh-cell centre (int, int) and are mutually perpendicular are
    # electrically safe in physical perfboard practice — the wires can be bent
    # around each other.  Default = inf preserves the strict ban; the
    # bare_diagonal routing mode lowers this to a finite value.
    crossing_bare_bare_diagonal: float = float("inf")
    crossing_insulated_bare: float = 5.0          # insulated × bare = jumper OK
    # Layer-change cost — paid once per top↔bottom via on a plated-through pad.
    layer_change: float = 2.0
    # Extra cost per step on the top layer beneath a component body.
    # Routing under a component on top is legal but undesirable — it makes
    # physical inspection and rework harder.  A bottom-layer detour is
    # preferred when it is not significantly longer.
    under_component_top: float = 3.0
    # When True, restrict diagonal moves to the parity family of the source
    # pad (parity = (floor(x)+floor(y)) % 2).  Guarantees that any two bare
    # diagonals crossing at a mesh-cell centre belong to different families
    # and are therefore mutually perpendicular — the electrical-safety
    # invariant that makes crossing_bare_bare_diagonal usable.
    bare_diagonal_mode: bool = False
    # ------------------------------------------------------------------
    # Enhanced-router penalties (opt-in; see EnhancedAStarRouter).
    # All default to 0 so the default `astar` router is byte-identical
    # to the pre-PR-3 behaviour.  Tuning starting points are noted next
    # to each field.  These knobs are read by the Python A* hot loop;
    # the Rust kernel currently ignores them (tracking issue: port once
    # the semantics stabilise).
    # ------------------------------------------------------------------
    # Extra cost when a step lands on a pad that already hosts another
    # insulated wire's vertex on the same side.  Tuning starting point: 1.0.
    at_pad_insulated_insulated_same_side: float = 0.0
    # Extra cost when a step lands on a pad that already hosts a bare
    # wire's vertex on the same side (insulated wire stepping onto bare
    # endpoint, or bare wire stepping onto an existing insulated endpoint).
    # Bare-on-bare same-side is enforced as INF upstream via `occupied`
    # and electrical-net checks; this knob covers the mixed case only.
    # Tuning starting point: 3.0.
    at_pad_insulated_bare_same_side: float = 0.0
    # Extra cost when the step is collinear with — and overlaps the
    # interior of — an existing wire segment on the *opposite* side
    # (top vs bottom).  Applied when at least one of the two wires is
    # insulated; bare×bare on opposite sides is electrically fine and
    # incurs no cost.  Tuning starting point: 0.5.
    parallel_opposite_side: float = 0.0
    # When > 0, enables mid-cell-preferred push-and-shove.  Before the
    # classic per-axis nudge fires, the enhanced router tries to *reroute*
    # the blocking wire under ``bare_diagonal_mode=True`` with the
    # opposite parity family — so any unavoidable crossing falls between
    # pads (mesh-cell centre) rather than on a pad.  ``max_reroute_factor``
    # is the ceiling: the reroute is accepted only if its cost is at
    # most ``original_cost * max_reroute_factor``.  0 = disabled.
    # Tuning starting point: 1.5.
    max_reroute_factor: float = 0.0
    # Cost added when a step touches an existing wire of the *same*
    # electrical net (crossing, collinear overlap, or shared pad vertex).
    # 0.0 means same-net reuse is electrically free — the router happily
    # produces a shared trunk instead of redundant parallel runs.  Applies
    # to bare and insulated wires alike, against any existing wire of the
    # net (manual or autoroute-placed).  Overrides ``crossing_*``,
    # ``at_pad_*``, and ``parallel_opposite_side`` penalties when the
    # crossed/touched wire belongs to the active net.
    same_net_wire_reuse_cost: float = 0.0
