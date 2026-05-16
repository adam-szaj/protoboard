# SPDX-License-Identifier: MIT
"""Hypothesis fuzz: autoroute must never fuse distinct nets.

Run with:

    uv run pytest -m property tests/property/test_autoroute_fuzz.py -v

The strategy generates a small random plated-through perfboard, drops a
handful of SIL components on disjoint pads, partitions the pins into a
few nets, calls `autoroute()`, and asserts the no-fusion invariant.
Hypothesis automatically shrinks any failing case to a minimal layout —
when a counterexample lands, copy it into tests/unit/test_autoroute_invariants.py
as a permanent regression scenario.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from protoboard.domain.layout.layout import Layout
from protoboard.domain.routing_settings import RoutingSettings
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.services.routing.autorouter import autoroute

from tests.unit._autoroute_invariants import (
    assert_no_net_fusion, make_perfboard, make_sil, place,
)


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

BOARD_ROWS = st.integers(min_value=6, max_value=20)
BOARD_COLS = st.integers(min_value=6, max_value=20)
ROTATIONS = st.sampled_from([0, 90])
N_COMPONENTS = st.integers(min_value=4, max_value=20)
WIRE_MODES = st.sampled_from(["bare", "bare_diagonal", "auto", "insulated"])
# Candidate (rotation, position) slots — _generate_layout greedily picks
# the first n_comps that don't collide.  Oversample 2× to keep success
# rate high on dense boards.
SLOTS = 40


def _generate_layout(
    rows: int, cols: int, n_comps: int, rotations: list[int],
    positions: list[tuple[int, int]],
) -> tuple[Layout, list, dict]:
    """Place *n_comps* 2-pin SIL components on the board, no pin collisions."""
    from protoboard.domain.components.instance import ComponentInstance

    used_pads: set[tuple[float, float]] = set()
    insts: list[ComponentInstance] = []
    placements: dict[uuid.UUID, Any] = {}

    for i, (rot, (col, row)) in enumerate(zip(rotations, positions)):
        if i >= n_comps:
            break
        if rot == 90:  # horizontal
            p1, p2 = (col + 0.5, row + 0.5), (col + 1.5, row + 0.5)
        else:  # vertical
            p1, p2 = (col + 0.5, row + 0.5), (col + 0.5, row + 1.5)
        if p1 in used_pads or p2 in used_pads:
            continue
        if p1[0] >= cols or p1[1] >= rows or p2[0] >= cols or p2[1] >= rows:
            continue
        used_pads.add(p1)
        used_pads.add(p2)
        inst = ComponentInstance(
            id=uuid.uuid4(), definition_ref="COMP",
            refdes=f"R{i+1}", value="",
        )
        insts.append(inst)
        placements[inst.id] = place(inst.id, col + 0.5, row + 0.5, rot)

    layout = Layout(placements=placements, wires=[])
    return layout, insts, {i.id: i for i in insts}


def _partition_into_nets(insts: list, n_nets: int) -> list[Net]:
    """Split the 2*len(insts) pins into *n_nets* non-singleton nets."""
    pins = []
    for inst in insts:
        pins.append(PinRef(component_id=inst.id, pin_number=1))
        pins.append(PinRef(component_id=inst.id, pin_number=2))
    n_nets = min(n_nets, len(pins) // 2)
    if n_nets < 1:
        n_nets = 1
    chunk = len(pins) // n_nets
    nets = []
    for i in range(n_nets):
        start = i * chunk
        end = (i + 1) * chunk if i < n_nets - 1 else len(pins)
        chunk_pins = pins[start:end]
        if len(chunk_pins) < 2:
            continue
        nets.append(Net(
            id=uuid.uuid4(), name=f"N{i+1}",
            pins=set(chunk_pins),
        ))
    return nets


@pytest.mark.property
class TestAutorouteFuzz:
    @settings(
        max_examples=2000,
        deadline=30000,  # 30 s per example; dense boards take a while
        suppress_health_check=[
            HealthCheck.too_slow, HealthCheck.data_too_large,
            HealthCheck.function_scoped_fixture,
        ],
    )
    @given(
        rows=BOARD_ROWS,
        cols=BOARD_COLS,
        n_comps=N_COMPONENTS,
        rotations=st.lists(ROTATIONS, min_size=SLOTS, max_size=SLOTS),
        positions=st.lists(
            st.tuples(
                st.integers(min_value=0, max_value=18),
                st.integers(min_value=0, max_value=18),
            ),
            min_size=SLOTS, max_size=SLOTS,
        ),
        n_nets=st.integers(min_value=1, max_value=6),
        wire_mode=WIRE_MODES,
    )
    def test_autoroute_never_fuses_nets(
        self,
        rows: int, cols: int, n_comps: int,
        rotations: list[int], positions: list[tuple[int, int]],
        n_nets: int, wire_mode: str,
    ) -> None:
        defn = make_sil("COMP")
        layout, insts, instances = _generate_layout(
            rows, cols, n_comps, rotations, positions
        )
        if len(insts) < 2:
            return

        nets = _partition_into_nets(insts, n_nets)
        if not nets:
            return
        schematic = Schematic(nets={n.id: n for n in nets})
        components = {"COMP": defn}
        board = make_perfboard(rows, cols)

        rs = RoutingSettings(
            wire_mode=wire_mode,  # type: ignore[arg-type]
            max_optimize_iterations=0,
        )
        result = autoroute(
            board, layout, schematic, components, instances,
            cost_model=rs.cost_model(),
            wire_mode=rs.wire_mode,
            max_optimize_iterations=0,
        )

        assert_no_net_fusion(
            board, layout, list(result.wires),
            schematic, components, instances,
        )
