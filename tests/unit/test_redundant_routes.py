# SPDX-License-Identifier: MIT
"""Regression: a ≥3-pin net's autoroute must not produce redundant wires.

The greedy MST in `_route_net` routes the closest pin pair first, then
each remaining pin to whatever is already connected.  A* receives the
whole electric set as target_pads (autorouter.py:1329, via
`graph.pads_in_same_electric_set`), so the second segment may end on a
pad reached through a different corridor than the first wire — producing
two parallel paths between the same pin pair, a cycle inside one net.

This test loads a hand-crafted project at
`tests/fixtures/redundant_route_steiner/` and asserts that every wire
produced by autoroute is essential (its removal disconnects its
endpoints in the connectivity graph).
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from protoboard.paths import BUILTIN_LIBRARY_DIR
from protoboard.services.library.loader import load_library
from protoboard.services.persistence.project_yaml import load_project
from protoboard.services.routing.autorouter import autoroute

from tests.unit._autoroute_invariants import (
    assert_no_net_fusion,
    assert_no_redundant_routes,
)


FIXTURE = Path(__file__).parent.parent / "fixtures" / "redundant_route_steiner"


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture not present")
class TestRedundantRoutes:
    def test_steiner_three_pin_net_has_no_double_path(self) -> None:
        lib = load_library([BUILTIN_LIBRARY_DIR])
        board, layout, schematic, instances, routing, _, _ = load_project(FIXTURE)
        components = {name: lib.get(name) for name in lib.names()}

        # Re-route from a clean slate — drop the fixture's own wires so the
        # test exercises greedy autoroute deterministically.
        clean = replace(layout, wires=[])
        result = autoroute(
            board, clean, schematic, components, instances,
            cost_model=routing.cost_model(),
            wire_mode=routing.wire_mode,
            max_rip_up_passes=routing.max_rip_up_passes,
            max_optimize_iterations=0,  # no ProcessPool, deterministic
        )

        # Two invariants, severity order.
        assert_no_net_fusion(
            board, clean, list(result.wires),
            schematic, components, instances,
        )
        assert_no_redundant_routes(
            board, clean, list(result.wires),
            components, instances,
        )
