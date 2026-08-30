# SPDX-License-Identifier: MIT
"""Targeted scenarios that exercise net-fusion bugs in the autorouter.

Every test runs `autoroute()` and then asserts that no electric set holds
pins of two different nets via `assert_no_net_fusion`.  Each scenario is
a small handcrafted layout that previously triggered a real fusion (or
that we want to keep regressing against).
"""

from __future__ import annotations

import uuid

from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import PlacementCollection
from protoboard.domain.layout.wire import WireCollection
from protoboard.domain.routing_settings import RoutingSettings
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.services.routing.autorouter import autoroute
from tests.instance_utils import bind
from tests.unit._autoroute_invariants import (
    assert_no_net_fusion,
    bare,
    insulated,
    make_perfboard,
    make_sil,
    place,
    sil_families,
)


def _inst(name: str, refdes: str, defn: ComponentDefinition) -> ComponentInstance:
    return bind(
        ComponentInstance(
            id=uuid.uuid4(), definition_ref=name, refdes=refdes, value=""
        ),
        defn,
        sil_families(defn),
    )


def _net(name: str, *refs: tuple[ComponentInstance, int]) -> Net:
    return Net(
        id=uuid.uuid4(),
        name=name,
        pins={PinRef(component_id=c.id, pin_number=p) for c, p in refs},
    )


def _run(
    board, layout, schematic, components, instances, wire_mode: str = "bare"
) -> None:
    rs = RoutingSettings(wire_mode=wire_mode)  # type: ignore[arg-type]
    result = autoroute(
        board,
        layout,
        schematic,
        components,
        instances,
        cost_model=rs.cost_model(),
        wire_mode=rs.wire_mode,
    )
    assert_no_net_fusion(
        board, layout, list(result.wires), schematic, components, instances
    )


# ---------------------------------------------------------------------------
# Category 1 — A* must not steal alien pads when routing a bare wire
# ---------------------------------------------------------------------------


class TestAlienPadStealing:
    def test_bare_route_skirts_existing_bare_chain(self) -> None:
        """A pre-placed bare chain claims pads (1.5..8.5, 0.5).  A second net
        with pins straddling row 2.5 must detour, not punch the chain."""
        board = make_perfboard(rows=3, cols=12)
        defn = make_sil("COMP")
        a1, a2 = _inst("COMP", "A1", defn), _inst("COMP", "A2", defn)
        b1, b2 = _inst("COMP", "B1", defn), _inst("COMP", "B2", defn)
        layout = Layout(
            placements=PlacementCollection(
                {
                    a1.id: place(a1.id, 0.5, 0.5),
                    a2.id: place(a2.id, 8.5, 0.5),
                    b1.id: place(b1.id, 0.5, 2.5),
                    b2.id: place(b2.id, 9.5, 2.5),
                }
            ),
            wires=WireCollection(
                [
                    bare((1.5, 0.5), (2.5, 0.5), side="bottom"),
                    bare((2.5, 0.5), (3.5, 0.5), side="bottom"),
                    bare((3.5, 0.5), (4.5, 0.5), side="bottom"),
                    bare((4.5, 0.5), (5.5, 0.5), side="bottom"),
                    bare((5.5, 0.5), (6.5, 0.5), side="bottom"),
                    bare((6.5, 0.5), (7.5, 0.5), side="bottom"),
                    bare((7.5, 0.5), (8.5, 0.5), side="bottom"),
                ]
            ),
        )
        net_a = _net("NET_A", (a1, 2), (a2, 1))
        net_b = _net("NET_B", (b1, 2), (b2, 1))
        sch = Schematic(nets={net_a.id: net_a, net_b.id: net_b})
        _run(
            board,
            layout,
            sch,
            {"COMP": defn},
            {a1.id: a1, a2.id: a2, b1.id: b1, b2.id: b2},
        )

    def test_bare_diagonal_route_skirts_existing_bare_diagonal(self) -> None:
        """Same idea but the alien net is built from diagonal steps."""
        board = make_perfboard(rows=6, cols=10)
        defn = make_sil("COMP")
        a1, a2 = _inst("COMP", "A1", defn), _inst("COMP", "A2", defn)
        b1, b2 = _inst("COMP", "B1", defn), _inst("COMP", "B2", defn)
        layout = Layout(
            placements=PlacementCollection(
                {
                    a1.id: place(a1.id, 0.5, 0.5),
                    a2.id: place(a2.id, 5.5, 5.5),
                    b1.id: place(b1.id, 5.5, 0.5),
                    b2.id: place(b2.id, 0.5, 5.5),
                }
            ),
            wires=WireCollection(
                [
                    # NET_A diagonal chain through (1.5,1.5), (2.5,2.5), (3.5,3.5), (4.5,4.5)  # noqa: E501
                    bare((1.5, 0.5), (1.5, 1.5), side="top"),
                    bare((1.5, 1.5), (2.5, 2.5), side="top"),
                    bare((2.5, 2.5), (3.5, 3.5), side="top"),
                    bare((3.5, 3.5), (4.5, 4.5), side="top"),
                    bare((4.5, 4.5), (5.5, 5.5), side="top"),
                ]
            ),
        )
        net_a = _net("NET_A", (a1, 2), (a2, 1))
        net_b = _net("NET_B", (b1, 1), (b2, 1))
        sch = Schematic(nets={net_a.id: net_a, net_b.id: net_b})
        _run(
            board,
            layout,
            sch,
            {"COMP": defn},
            {a1.id: a1, a2.id: a2, b1.id: b1, b2.id: b2},
            wire_mode="bare_diagonal",
        )


# ---------------------------------------------------------------------------
# Category 2 — plated-through hole: bare on one side claims both sides
# ---------------------------------------------------------------------------


class TestPlatedThroughBlocking:
    def test_bare_bottom_blocks_top_insulated(self) -> None:
        """An existing bare bottom wire must prevent an insulated top route
        from landing on the same pad — the plated-through hole would short."""
        board = make_perfboard(rows=4, cols=8)
        defn = make_sil("COMP")
        a1, a2 = _inst("COMP", "A1", defn), _inst("COMP", "A2", defn)
        b1, b2 = _inst("COMP", "B1", defn), _inst("COMP", "B2", defn)
        layout = Layout(
            placements=PlacementCollection(
                {
                    # NET_A on row 0 with bare bottom wires across (1.5..3.5, 0.5)
                    a1.id: place(a1.id, 0.5, 0.5),
                    a2.id: place(a2.id, 3.5, 0.5),
                    # NET_B on row 2 that must traverse columns 1.5..3.5 somewhere
                    b1.id: place(b1.id, 0.5, 2.5),
                    b2.id: place(b2.id, 5.5, 2.5),
                }
            ),
            wires=WireCollection(
                [
                    bare((1.5, 0.5), (2.5, 0.5), side="bottom"),
                    bare((2.5, 0.5), (3.5, 0.5), side="bottom"),
                ]
            ),
        )
        net_a = _net("NET_A", (a1, 2), (a2, 1))
        net_b = _net("NET_B", (b1, 2), (b2, 1))
        sch = Schematic(nets={net_a.id: net_a, net_b.id: net_b})
        # "auto" tries bare first, then insulated jumper — the latter is the
        # one that used to short via the plated-through hole.
        _run(
            board,
            layout,
            sch,
            {"COMP": defn},
            {a1.id: a1, a2.id: a2, b1.id: b1, b2.id: b2},
            wire_mode="auto",
        )


# ---------------------------------------------------------------------------
# Category 3 — alien electric set with no component pin at the conflict pad
# ---------------------------------------------------------------------------


class TestAlienElectricSetWithoutPin:
    def test_bare_route_avoids_pad_in_alien_set_via_wire_only(self) -> None:
        """The alien net owns pad (5.5,1.5) purely via a bare wire — no
        component pin sits there.  occupied_pads (built from pins) would
        miss it; the fix must catch it via the electric-set expansion."""
        board = make_perfboard(rows=4, cols=10)
        defn = make_sil("COMP")
        a1, a2 = _inst("COMP", "A1", defn), _inst("COMP", "A2", defn)
        b1, b2 = _inst("COMP", "B1", defn), _inst("COMP", "B2", defn)
        layout = Layout(
            placements=PlacementCollection(
                {
                    # NET_A connects (1.5, 0.5) to (8.5, 0.5) — pins on row 0.
                    a1.id: place(a1.id, 0.5, 0.5),
                    a2.id: place(a2.id, 8.5, 0.5),
                    # NET_B pins at (1.5, 2.5) and (8.5, 2.5) — row 2.
                    b1.id: place(b1.id, 0.5, 2.5),
                    b2.id: place(b2.id, 8.5, 2.5),
                }
            ),
            wires=WireCollection(
                [
                    # NET_A pre-wired through row 1.5 (intermediate pads with no pin).
                    bare((1.5, 0.5), (1.5, 1.5), side="bottom"),
                    bare((1.5, 1.5), (2.5, 1.5), side="bottom"),
                    bare((2.5, 1.5), (3.5, 1.5), side="bottom"),
                    bare((3.5, 1.5), (4.5, 1.5), side="bottom"),
                    bare((4.5, 1.5), (5.5, 1.5), side="bottom"),
                    bare((5.5, 1.5), (6.5, 1.5), side="bottom"),
                    bare((6.5, 1.5), (7.5, 1.5), side="bottom"),
                    bare((7.5, 1.5), (8.5, 1.5), side="bottom"),
                    bare((8.5, 1.5), (8.5, 0.5), side="bottom"),
                ]
            ),
        )
        net_a = _net("NET_A", (a1, 2), (a2, 1))
        net_b = _net("NET_B", (b1, 2), (b2, 1))
        sch = Schematic(nets={net_a.id: net_a, net_b.id: net_b})
        _run(
            board,
            layout,
            sch,
            {"COMP": defn},
            {a1.id: a1, a2.id: a2, b1.id: b1, b2.id: b2},
        )


# ---------------------------------------------------------------------------
# Category 4 — insulated jumper endpoints must be NET_OWNER pins, not stray pads
# ---------------------------------------------------------------------------


class TestInsulatedJumperEndpoints:
    def test_insulated_jumper_pair_does_not_link_alien_nets(self) -> None:
        """Insulated wire endpoints contact the board pad they sit on.  The
        router must never plant such an endpoint on a pad that already
        belongs to a different net."""
        board = make_perfboard(rows=5, cols=8)
        defn = make_sil("COMP")
        a1, a2 = _inst("COMP", "A1", defn), _inst("COMP", "A2", defn)
        b1, b2 = _inst("COMP", "B1", defn), _inst("COMP", "B2", defn)
        layout = Layout(
            placements=PlacementCollection(
                {
                    a1.id: place(a1.id, 0.5, 0.5),
                    a2.id: place(a2.id, 6.5, 0.5),
                    b1.id: place(b1.id, 0.5, 3.5),
                    b2.id: place(b2.id, 6.5, 3.5),
                }
            ),
            wires=WireCollection(
                [
                    # NET_A claims pads (1.5..6.5, 0.5) via bare bottom row.
                    bare((1.5, 0.5), (2.5, 0.5), side="bottom"),
                    bare((2.5, 0.5), (3.5, 0.5), side="bottom"),
                    bare((3.5, 0.5), (4.5, 0.5), side="bottom"),
                    bare((4.5, 0.5), (5.5, 0.5), side="bottom"),
                    bare((5.5, 0.5), (6.5, 0.5), side="bottom"),
                    # NET_A also stakes a top-layer jumper toward row 2 — the rest of
                    # the board is now visually attractive for any insulated route.
                    insulated((6.5, 0.5), (6.5, 2.5), side="top"),
                ]
            ),
        )
        net_a = _net("NET_A", (a1, 2), (a2, 1))
        net_b = _net("NET_B", (b1, 2), (b2, 1))
        sch = Schematic(nets={net_a.id: net_a, net_b.id: net_b})
        _run(
            board,
            layout,
            sch,
            {"COMP": defn},
            {a1.id: a1, a2.id: a2, b1.id: b1, b2.id: b2},
            wire_mode="auto",
        )


# ---------------------------------------------------------------------------
# Category 5 — diagonal crossings between two routed nets
# ---------------------------------------------------------------------------


class TestDiagonalCrossings:
    def test_two_perpendicular_diagonals_at_mesh_centre_safe(self) -> None:
        """In bare_diagonal mode two pads of opposite parity should be
        routable as crossing diagonals without fusing nets."""
        board = make_perfboard(rows=5, cols=5)
        defn = make_sil("COMP")
        a1, a2 = _inst("COMP", "A1", defn), _inst("COMP", "A2", defn)
        b1, b2 = _inst("COMP", "B1", defn), _inst("COMP", "B2", defn)
        layout = Layout(
            placements=PlacementCollection(
                {
                    # NET_A: (1.5, 1.5) → (3.5, 3.5) — even parity "/" family
                    a1.id: place(a1.id, 0.5, 1.5),  # pin1=(0.5,1.5), pin2=(1.5,1.5)
                    a2.id: place(a2.id, 3.5, 3.5),
                    # NET_B: (1.5, 3.5) → (3.5, 1.5) — odd parity "\" family
                    b1.id: place(b1.id, 0.5, 3.5),
                    b2.id: place(b2.id, 3.5, 1.5),
                }
            )
        )
        net_a = _net("NET_A", (a1, 2), (a2, 1))
        net_b = _net("NET_B", (b1, 2), (b2, 1))
        sch = Schematic(nets={net_a.id: net_a, net_b.id: net_b})
        _run(
            board,
            layout,
            sch,
            {"COMP": defn},
            {a1.id: a1, a2.id: a2, b1.id: b1, b2.id: b2},
            wire_mode="bare_diagonal",
        )


# ---------------------------------------------------------------------------
# Category 6 — many nets cross-routed, smoke test
# ---------------------------------------------------------------------------


class TestMultiNetSmoke:
    def test_four_nets_no_fusion(self) -> None:
        """Four independent nets on the same board, routed simultaneously.
        Catches global ordering / push-and-shove regressions."""
        board = make_perfboard(rows=6, cols=8)
        defn = make_sil("COMP")
        comps = {f"R{i}": _inst("COMP", f"R{i}", defn) for i in range(1, 9)}
        layout = Layout(
            placements=PlacementCollection(
                {
                    comps["R1"].id: place(comps["R1"].id, 0.5, 0.5),
                    comps["R2"].id: place(comps["R2"].id, 6.5, 0.5),
                    comps["R3"].id: place(comps["R3"].id, 0.5, 2.5),
                    comps["R4"].id: place(comps["R4"].id, 6.5, 2.5),
                    comps["R5"].id: place(comps["R5"].id, 0.5, 4.5),
                    comps["R6"].id: place(comps["R6"].id, 6.5, 4.5),
                    comps["R7"].id: place(comps["R7"].id, 3.5, 0.5),
                    comps["R8"].id: place(comps["R8"].id, 3.5, 5.5),
                }
            )
        )
        nets = [
            _net("N1", (comps["R1"], 2), (comps["R2"], 1)),
            _net("N2", (comps["R3"], 2), (comps["R4"], 1)),
            _net("N3", (comps["R5"], 2), (comps["R6"], 1)),
            _net("N4", (comps["R7"], 1), (comps["R8"], 2)),
        ]
        sch = Schematic(nets={n.id: n for n in nets})
        _run(board, layout, sch, {"COMP": defn}, {c.id: c for c in comps.values()})
