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


@pytest.fixture
def _force_sequential_astar(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force A* side-evaluation pool to run synchronously.

    Hypothesis can only shrink counterexamples that fail deterministically.
    The real `_ASTAR_POOL` is a ThreadPoolExecutor whose `as_completed`
    order depends on thread scheduling — under tied universe costs the
    winner flips between runs, which Hypothesis flags as flaky.
    """
    from protoboard.services.routing import autorouter
    monkeypatch.setattr(autorouter, "_ASTAR_POOL", autorouter._SequentialExecutor())


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


def _det_uuid(prefix: int, index: int) -> uuid.UUID:
    """Deterministic UUID — index-based, no system randomness.

    Test runs share input parameters, so giving instances/nets stable IDs
    keeps set/dict iteration order identical across replays.  `uuid.uuid4()`
    inside a fuzz body breaks Hypothesis shrinking — each run draws fresh
    random UUIDs and the resulting hash-set iteration order can flip the
    autoroute outcome enough to mark the example flaky.
    """
    return uuid.UUID(int=(prefix << 96) | index)


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
            id=_det_uuid(0x01, i), definition_ref="COMP",
            refdes=f"R{i+1}", value="",
        )
        insts.append(inst)
        placements[inst.id] = place(inst.id, col + 0.5, row + 0.5, rot)

    layout = Layout(placements=placements, wires=[])
    return layout, insts, {i.id: i for i in insts}


def _partition_into_nets(insts: list, n_nets: int) -> list[Net]:
    """Split components into *n_nets* non-singleton nets.

    Pin 1 and pin 2 of the SAME component are always in the SAME net.
    A real 2-terminal component electrically connects its pins (the component
    body is in both pins' electric set), so splitting them across nets would
    create a tautological short — autoroute would be tested on a circuit that
    cannot exist in hardware.

    The trade-off: with both pins of every component on the same net, the
    component itself provides a "wire" between its pads and autoroute often
    has nothing to do (or trivially short routes between same-component pads).
    To force non-trivial routing we still need ≥2 components per net so a
    net has pads to bridge — `n_nets` is clamped to at most len(insts)//2.
    """
    n_nets = max(1, min(n_nets, len(insts) // 2))
    buckets: list[list[PinRef]] = [[] for _ in range(n_nets)]
    for i, inst in enumerate(insts):
        b = i % n_nets
        buckets[b].append(PinRef(component_id=inst.id, pin_number=1))
        buckets[b].append(PinRef(component_id=inst.id, pin_number=2))

    nets = []
    for i, bucket in enumerate(buckets):
        if len(bucket) < 2:
            continue
        nets.append(Net(
            id=_det_uuid(0x02, i), name=f"N{i+1}",
            pins=set(bucket),
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

        try:
            assert_no_net_fusion(
                board, layout, list(result.wires),
                schematic, components, instances,
            )
        except AssertionError as e:
            raise AssertionError(
                str(e) + _dump_scenario(insts, instances, nets, layout)
            ) from e


def _dump_scenario(insts: list, instances: dict, nets: list, layout: Layout) -> str:
    """Pretty-print a fuzz scenario for triage when assertion fires."""
    lines = ["", "=== fuzz scenario dump ==="]
    lines.append("Instances (refdes → pin1, pin2):")
    for inst in insts:
        pl = layout.placements[inst.id]
        if pl.rotation == 90:
            p1, p2 = (pl.position.x, pl.position.y), (pl.position.x + 1, pl.position.y)
        else:
            p1, p2 = (pl.position.x, pl.position.y), (pl.position.x, pl.position.y + 1)
        lines.append(f"  {inst.refdes} rot={pl.rotation}: pin1={p1} pin2={p2}")
    lines.append("Nets (name → component refdes/pin):")
    for net in nets:
        names = []
        for ref in sorted(net.pins, key=lambda r: (str(r.component_id), r.pin_number)):
            inst_obj = instances[ref.component_id]
            names.append(f"{inst_obj.refdes}.p{ref.pin_number}")
        lines.append(f"  {net.name}: {names}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Wider fuzz: bigger boards + optimize-pass enabled (covers ProcessPool path)
# ---------------------------------------------------------------------------

BIG_BOARD_ROWS = st.integers(min_value=10, max_value=30)
BIG_BOARD_COLS = st.integers(min_value=10, max_value=30)
BIG_N_COMPONENTS = st.integers(min_value=6, max_value=40)
BIG_SLOTS = 80


@pytest.mark.property
class TestAutorouteFuzzWide:
    """Larger boards + optimize pass.  Aims at the gold_performance_test
    fusion bug that the small-board fuzz never reproduces."""

    @settings(
        max_examples=1000,
        deadline=60000,  # optimize pass adds ~10× work per example
        suppress_health_check=[
            HealthCheck.too_slow, HealthCheck.data_too_large,
            HealthCheck.function_scoped_fixture,
        ],
    )
    @given(
        rows=BIG_BOARD_ROWS,
        cols=BIG_BOARD_COLS,
        n_comps=BIG_N_COMPONENTS,
        rotations=st.lists(ROTATIONS, min_size=BIG_SLOTS, max_size=BIG_SLOTS),
        positions=st.lists(
            st.tuples(
                st.integers(min_value=0, max_value=28),
                st.integers(min_value=0, max_value=28),
            ),
            min_size=BIG_SLOTS, max_size=BIG_SLOTS,
        ),
        n_nets=st.integers(min_value=1, max_value=8),
        wire_mode=WIRE_MODES,
    )
    def test_autoroute_never_fuses_nets_wide(
        self,
        _force_sequential_astar: None,
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

        try:
            assert_no_net_fusion(
                board, layout, list(result.wires),
                schematic, components, instances,
            )
        except AssertionError as e:
            raise AssertionError(
                str(e) + _dump_scenario(insts, instances, nets, layout)
            ) from e
