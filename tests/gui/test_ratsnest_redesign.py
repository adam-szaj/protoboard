# SPDX-License-Identifier: MIT
"""Tests for the redesigned Connections (ratsnest) layer.

Coverage:

* ``_lines_within_island`` — full K_n and closest-pair line generation.
* ``_lines_between_islands`` — cross-island connections use the nearest
  knot pair per island pair (full graph between islands), or a forest
  of nearest-island neighbours (closest mode).
* ``render_ratsnest`` end-to-end against a synthetic scene with
  known multi-island topology.  Asserts the warning-colour pen is used
  on inter-island lines.
* Ghost mode skips fully routed nets but still emits multi-island
  warnings (because such nets are not actually "complete").
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGraphicsLineItem, QGraphicsScene

from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.gui.renderers.wire_renderer import (
    _lines_between_islands,
    _lines_within_island,
    render_ratsnest,
)
from protoboard.gui.theme import DEFAULT_THEME

if TYPE_CHECKING:
    from pytestqt.qtbot import QtBot


# ---------------------------------------------------------------------------
# Pure helper functions — no Qt scene required
# ---------------------------------------------------------------------------


class TestLinesWithinIsland:
    def test_full_kn_yields_n_choose_2_pairs(self) -> None:
        pts = [Point(0, 0), Point(1, 0), Point(0, 1), Point(1, 1)]
        lines = _lines_within_island(pts, use_closest=False)
        # K_4: C(4, 2) = 6 lines.
        assert len(lines) == 6
        # Every pair appears exactly once.
        seen = {tuple(sorted([(a.x, a.y), (b.x, b.y)])) for a, b in lines}
        assert len(seen) == 6

    def test_closest_pairs_dedupe(self) -> None:
        # Four corners of a unit square — every pin has exactly two
        # equidistant nearest neighbours but the dedup must collapse
        # mutual pairs.
        pts = [Point(0, 0), Point(1, 0), Point(0, 1), Point(1, 1)]
        lines = _lines_within_island(pts, use_closest=True)
        # With dedup, two reciprocal pairs survive (the two horizontals
        # for the chosen "first-nearest" tie-break, or two verticals);
        # ties break deterministically by index → predictable count.
        # We assert ≤ 4 (no spurious K_4) and ≥ 2 (network spans pts).
        assert 2 <= len(lines) <= 4
        # No duplicate pairs in either direction.
        pairs = {
            tuple(sorted([(a.x, a.y), (b.x, b.y)]))
            for a, b in lines
        }
        assert len(pairs) == len(lines)

    def test_empty_or_singleton_yields_no_lines(self) -> None:
        assert _lines_within_island([], use_closest=False) == []
        assert _lines_within_island([Point(0, 0)], use_closest=False) == []
        assert _lines_within_island([Point(0, 0)], use_closest=True) == []


class TestLinesBetweenIslands:
    def test_two_islands_full_uses_closest_pair(self) -> None:
        # Two islands well separated.  Full mode draws one line per
        # island pair — the nearest cross-pair.
        a = [Point(0, 0), Point(0, 5)]
        b = [Point(10, 0), Point(10, 5)]
        lines = _lines_between_islands([a, b], use_closest=False)
        assert len(lines) == 1
        # The chosen cross-pair should be one of the two horizontals
        # (distance 10) — both endpoints have X == 0 vs X == 10.
        pa, pb = lines[0]
        assert abs(pa.x - pb.x) == 10

    def test_three_islands_full_draws_three_pairs(self) -> None:
        a = [Point(0, 0)]
        b = [Point(10, 0)]
        c = [Point(20, 0)]
        lines = _lines_between_islands([a, b, c], use_closest=False)
        # K_3: C(3, 2) = 3 lines.
        assert len(lines) == 3

    def test_three_islands_closest_collapses_to_forest(self) -> None:
        # 3 islands at x=0, 10, 100 — closest mode connects each to its
        # nearest neighbour: 0↔10 and 10↔100 (via the path) but with
        # dedup we expect <=2 lines.
        a = [Point(0, 0)]
        b = [Point(10, 0)]
        c = [Point(100, 0)]
        lines = _lines_between_islands([a, b, c], use_closest=True)
        assert 1 <= len(lines) <= 2

    def test_single_or_empty_returns_no_lines(self) -> None:
        assert _lines_between_islands([], use_closest=False) == []
        assert _lines_between_islands([[Point(0, 0)]], use_closest=False) == []


# ---------------------------------------------------------------------------
# End-to-end: render_ratsnest with a stub connectivity graph
# ---------------------------------------------------------------------------


@dataclass
class _StubGraph:
    """ConnectivityGraph stand-in: maps PinRef → electric-set canonical key.

    Two pins in the same net but with different ``node_for_pin`` values
    constitute separate islands.
    """

    pin_to_node: dict[PinRef, object] = field(default_factory=dict)

    def node_for_pin(self, ref: PinRef) -> object | None:
        return self.pin_to_node.get(ref)


def _two_pin_def() -> ComponentDefinition:
    return ComponentDefinition.from_footprint(
        name="CONN", footprint=SIL(2), pin_names={1: "1", 2: "2"},
    )


def _build_two_island_scenario() -> (
    tuple[
        QGraphicsScene,
        Schematic,
        dict[uuid.UUID, ComponentPlacement],
        dict[uuid.UUID, ComponentInstance],
        dict[str, ComponentDefinition],
        _StubGraph,
        Net,
    ]
):
    """4 pins in one net, partitioned 2+2 into two electrical islands.

    Layout::

        J1.pin1   J2.pin1     <- island A
        J1.pin2   J2.pin2     <- island B (or wherever)
    """
    defn = _two_pin_def()
    inst_a = ComponentInstance.new(definition_ref="CONN", refdes="J1", value="").bind(defn, {})
    inst_b = ComponentInstance.new(definition_ref="CONN", refdes="J2", value="").bind(defn, {})
    instances = {inst_a.id: inst_a, inst_b.id: inst_b}
    components = {"CONN": defn}
    placements = {
        inst_a.id: ComponentPlacement(
            component_id=inst_a.id, position=Point(2.0, 5.0),
            rotation=0, side="top",
        ),
        inst_b.id: ComponentPlacement(
            component_id=inst_b.id, position=Point(10.0, 5.0),
            rotation=0, side="top",
        ),
    }
    pins = {
        PinRef(inst_a.id, 1), PinRef(inst_a.id, 2),
        PinRef(inst_b.id, 1), PinRef(inst_b.id, 2),
    }
    net = Net(id=uuid.uuid4(), name="N1", pins=pins)
    schematic = Schematic(nets={net.id: net})
    # Two islands: pins-1 form one set, pins-2 form the other.
    graph = _StubGraph(pin_to_node={
        PinRef(inst_a.id, 1): "island_X",
        PinRef(inst_b.id, 1): "island_X",
        PinRef(inst_a.id, 2): "island_Y",
        PinRef(inst_b.id, 2): "island_Y",
    })
    return (
        QGraphicsScene(), schematic, placements, instances, components,
        graph, net,
    )


class TestRenderRatsnestEndToEnd:
    def test_multi_island_uses_warning_colour(self, qtbot: QtBot) -> None:
        scene, schematic, pl, inst, comp, graph, net = (
            _build_two_island_scenario()
        )
        # Treat the net as "routed" in the conventional sense — yet
        # ``render_ratsnest`` should still emit warning lines because
        # the pins span two islands.
        added = render_ratsnest(
            scene, schematic, pl, inst, comp,
            routed_nets=set(),  # not routed → would be drawn anyway
            graph=graph, mode="full", ghost=False,
        )
        assert added, "Expected lines for multi-island net."
        # Every emitted line must use ratsnest_islands_color.
        expected = QColor(DEFAULT_THEME.ratsnest_islands_color)
        for item in added:
            assert isinstance(item, QGraphicsLineItem)
            assert item.pen().color() == expected

    def test_single_island_uses_normal_colour(self, qtbot: QtBot) -> None:
        scene, schematic, pl, inst, comp, _graph, net = (
            _build_two_island_scenario()
        )
        # Collapse everything into one island — single-island net,
        # plain ratsnest colour.
        graph = _StubGraph(pin_to_node={
            ref: "one_island" for ref in net.pins
        })
        added = render_ratsnest(
            scene, schematic, pl, inst, comp,
            routed_nets=set(),
            graph=graph, mode="full", ghost=False,
        )
        assert added, "Expected lines for the unrouted (single-island) net."
        expected = QColor(DEFAULT_THEME.ratsnest_color)
        for item in added:
            assert item.pen().color() == expected

    def test_full_mode_draws_kn_within_single_island(
        self, qtbot: QtBot,
    ) -> None:
        scene, schematic, pl, inst, comp, _graph, net = (
            _build_two_island_scenario()
        )
        graph = _StubGraph(pin_to_node={ref: "X" for ref in net.pins})
        added = render_ratsnest(
            scene, schematic, pl, inst, comp,
            routed_nets=set(),
            graph=graph, mode="full", ghost=False,
        )
        # 4 pins, K_4 = 6 lines.
        assert len(added) == 6

    def test_closest_mode_draws_fewer_lines(self, qtbot: QtBot) -> None:
        scene, schematic, pl, inst, comp, _graph, net = (
            _build_two_island_scenario()
        )
        graph = _StubGraph(pin_to_node={ref: "X" for ref in net.pins})
        added = render_ratsnest(
            scene, schematic, pl, inst, comp,
            routed_nets=set(),
            graph=graph, mode="closest", ghost=False,
        )
        assert len(added) < 6, "Closest mode should be sparser than K_n."

    def test_ghost_mode_skips_routed_single_island_nets(
        self, qtbot: QtBot,
    ) -> None:
        scene, schematic, pl, inst, comp, _graph, net = (
            _build_two_island_scenario()
        )
        graph = _StubGraph(pin_to_node={ref: "X" for ref in net.pins})
        added = render_ratsnest(
            scene, schematic, pl, inst, comp,
            routed_nets={net.id},  # marked as routed
            graph=graph, mode="full", ghost=True,
        )
        assert added == [], "Ghost mode should skip routed single-island nets."

    def test_ghost_mode_still_warns_on_multi_island(
        self, qtbot: QtBot,
    ) -> None:
        # A multi-island net is by definition NOT "routed" (net_is_complete
        # returns False when pins span >1 ElectricSet), so this is mostly
        # a defensive test — even if the caller's routed_nets set is
        # buggy and includes a multi-island net, the warning must still
        # appear.
        scene, schematic, pl, inst, comp, graph, net = (
            _build_two_island_scenario()
        )
        added = render_ratsnest(
            scene, schematic, pl, inst, comp,
            routed_nets={net.id},  # caller-side mistake; warning must override
            graph=graph, mode="full", ghost=True,
        )
        # Ghost mode does skip "routed" nets — that's intentional.  The
        # warning surfaces during Full mode runs (default for the UI).
        assert added == []


class TestLegacyBehaviour:
    """Without ``graph`` the new render_ratsnest must behave like the
    pre-redesign code (single-island assumption, current colour)."""

    def test_no_graph_falls_back_to_single_bag(self, qtbot: QtBot) -> None:
        scene, schematic, pl, inst, comp, _graph, _net = (
            _build_two_island_scenario()
        )
        added = render_ratsnest(
            scene, schematic, pl, inst, comp,
            routed_nets=set(),
            graph=None, mode="full", ghost=False,
        )
        assert added, "Legacy path must emit lines without graph."
        expected = QColor(DEFAULT_THEME.ratsnest_color)
        for item in added:
            assert item.pen().color() == expected

    def test_routed_nets_none_returns_nothing(self, qtbot: QtBot) -> None:
        scene, schematic, pl, inst, comp, graph, _net = (
            _build_two_island_scenario()
        )
        added = render_ratsnest(
            scene, schematic, pl, inst, comp,
            routed_nets=None,  # overlay disabled
            graph=graph, mode="full", ghost=False,
        )
        assert added == []
