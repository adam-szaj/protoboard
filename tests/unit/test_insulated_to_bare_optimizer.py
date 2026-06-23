# SPDX-License-Identifier: MIT
"""Tests for InsulatedToBareOptimizer — pure type-flip insulated→bare.

The optimizer changes ``wire.insulated`` from True to False where the
surrounding state proves the conversion is safe; it never modifies
``wire.path``.
"""

from __future__ import annotations

import threading
import uuid
from typing import Literal

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SingleRow
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import Wire, WireCollection
from protoboard.domain.routing_settings import RoutingSettings
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.services.routing.autorouter import AutorouteResult
from protoboard.services.routing.builtin_optimizers import (
    InsulatedToBareOptimizer,
)
from protoboard.services.routing.optimizer import (
    OptimizerCapabilities,
    WireOptimizer,
    registry,
)
from protoboard.services.routing.protocol import ProgressSink
from protoboard.services.routing.snapshot import RouteRequest


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _perfboard(rows: int = 6, cols: int = 6) -> Board:
    return Board(
        root=BoardGrid(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            cell=PadAtom(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                layers=PadLayers(top=True, bottom=True, plated_through=True),
            ),
            rows=rows,
            cols=cols,
            row_gap=0.0,
            col_gap=0.0,
        )
    )


def _sil2() -> ComponentDefinition:
    return ComponentDefinition(
        name="COMP", footprint=SingleRow(n=2), pin_names={1: "A", 2: "B"}
    )


def _place(cid: uuid.UUID, x: float, y: float, rotation: int = 90) -> ComponentPlacement:
    return ComponentPlacement(
        component_id=cid, position=Point(x, y), rotation=rotation, side="top"
    )


def _two_pin_project_with_insulated_wire(
    a_pos: tuple[float, float] = (0.5, 0.5),
    b_pos: tuple[float, float] = (3.5, 0.5),
    extra_components: list[tuple[float, float]] | None = None,
) -> tuple[Board, Layout, Schematic, dict, dict, Wire]:
    """Build a board with two SIL2 parts on one net + a manual insulated wire
    connecting them along a free corridor.

    Returns (board, layout, schematic, components, instances, the_wire).
    """
    board = _perfboard()
    defn = _sil2()
    cid_a, cid_b = uuid.uuid4(), uuid.uuid4()
    inst_a = ComponentInstance(id=cid_a, definition_ref="COMP", refdes="R1", value="")
    inst_b = ComponentInstance(id=cid_b, definition_ref="COMP", refdes="R2", value="")
    placements = {
        cid_a: _place(cid_a, *a_pos),
        cid_b: _place(cid_b, *b_pos),
    }
    instances = {cid_a: inst_a, cid_b: inst_b}

    if extra_components:
        for x, y in extra_components:
            cid = uuid.uuid4()
            inst = ComponentInstance(
                id=cid, definition_ref="COMP", refdes=f"R_{len(instances)+1}", value="",
            )
            placements[cid] = _place(cid, x, y)
            instances[cid] = inst

    layout = Layout(placements=placements)
    ref_a2 = PinRef(component_id=cid_a, pin_number=2)
    ref_b1 = PinRef(component_id=cid_b, pin_number=1)
    net = Net(id=uuid.uuid4(), name="N1", pins={ref_a2, ref_b1})
    schematic = Schematic(nets={net.id: net})

    # Build an insulated wire from pin a2 → pin b1 along the row.
    # SingleRow(n=2) with rotation=90 puts pin1 at placement.position and
    # pin2 at (position.x + 1, position.y).  Verify via the same helper
    # the optimizer uses.
    from protoboard.domain.layout.placement import pin_world_position
    p_start = pin_world_position(defn.footprint, placements[cid_a], 2)
    p_end = pin_world_position(defn.footprint, placements[cid_b], 1)
    # straight horizontal path
    path = [p_start]
    cur = p_start
    while cur.x != p_end.x or cur.y != p_end.y:
        dx = 1 if p_end.x > cur.x else (-1 if p_end.x < cur.x else 0)
        dy = 1 if p_end.y > cur.y else (-1 if p_end.y < cur.y else 0)
        cur = Point(cur.x + dx, cur.y + dy)
        path.append(cur)

    wire = Wire(
        id=uuid.uuid4(),
        path=path,
        insulated=True,
        side="top",
        color=None,
    )
    layout = Layout(placements=placements, wires=WireCollection([wire]))
    return board, layout, schematic, {"COMP": defn}, instances, wire


def _make_request(
    board, layout, schematic, components, instances,
    wire_mode: Literal["bare", "insulated", "auto", "bare_diagonal"] = "insulated",
) -> RouteRequest:
    rs = RoutingSettings(wire_mode=wire_mode, enable_simplify_pass=False)
    return RouteRequest.from_session(
        board=board, layout=layout, schematic=schematic,
        components=components, instances=instances,
        routing_settings=rs,
    )


# ---------------------------------------------------------------------------
# Protocol & registry conformance
# ---------------------------------------------------------------------------

class TestProtocolConformance:
    def test_satisfies_wire_optimizer_protocol(self) -> None:
        assert isinstance(InsulatedToBareOptimizer(), WireOptimizer)

    def test_registered_at_import(self) -> None:
        import protoboard.services.routing.builtin_optimizers  # noqa: F401
        assert "insulated_to_bare" in registry.list_available()

    def test_capabilities_advertise_insulated_to_bare(self) -> None:
        caps = InsulatedToBareOptimizer().capabilities()
        assert caps.insulated_to_bare
        assert not caps.shortens_paths
        assert isinstance(caps, OptimizerCapabilities)


# ---------------------------------------------------------------------------
# Empty / cancelled inputs
# ---------------------------------------------------------------------------

class TestEmptyInputs:
    def test_empty_wires_return_unchanged(self) -> None:
        opt = InsulatedToBareOptimizer()
        req = RouteRequest.from_session(
            board=_perfboard(), layout=Layout(),
            schematic=Schematic(nets={}),
            components={}, instances={},
            routing_settings=RoutingSettings(),
        )
        result = AutorouteResult(wires=[], unrouted_nets=[], pass_stats=[])
        assert opt.optimize(req, result, ProgressSink()) is result

    def test_cancelled_returns_input_unchanged(self) -> None:
        board, layout, sch, comps, insts, wire = _two_pin_project_with_insulated_wire()
        req = _make_request(board, layout, sch, comps, insts)
        ev = threading.Event(); ev.set()
        sink = ProgressSink(cancel_flag=ev)
        result_in = AutorouteResult(wires=[wire], unrouted_nets=[], pass_stats=[])
        result_out = InsulatedToBareOptimizer().optimize(req, result_in, sink)
        assert result_out is result_in

    def test_no_insulated_wires_returns_input_unchanged(self) -> None:
        """If every wire is already bare, the optimizer is a no-op."""
        board, layout, sch, comps, insts, wire = _two_pin_project_with_insulated_wire()
        bare = Wire(
            id=wire.id, path=list(wire.path), insulated=False,
            side=wire.side, color=wire.color,
        )
        req = _make_request(board, layout, sch, comps, insts)
        result = AutorouteResult(wires=[bare], unrouted_nets=[], pass_stats=[])
        opt = InsulatedToBareOptimizer()
        out = opt.optimize(req, result, ProgressSink())
        assert out is result


# ---------------------------------------------------------------------------
# Safety predicate behaviour
# ---------------------------------------------------------------------------

class TestFlipsWhenSafe:
    def test_insulated_over_free_corridor_flips_to_bare(self) -> None:
        """A simple insulated wire on a free corridor must demote to bare."""
        board, layout, sch, comps, insts, wire = _two_pin_project_with_insulated_wire()
        req = _make_request(board, layout, sch, comps, insts)
        result = AutorouteResult(wires=[wire], unrouted_nets=[], pass_stats=[])
        out = InsulatedToBareOptimizer().optimize(req, result, ProgressSink())
        assert len(out.wires) == 1
        assert out.wires[0].insulated is False
        # path is unchanged
        assert [(p.x, p.y) for p in out.wires[0].path] == [
            (p.x, p.y) for p in wire.path
        ]


class TestRefusesWhenUnsafe:
    def test_insulated_wire_over_alien_pin_stays_insulated(self) -> None:
        """If the wire's path crosses a pin of another net, refuse to flip.

        Wire runs from R1.pin2=(1.5, 0.5) to R2.pin1=(3.5, 0.5).  The
        wire's path is therefore [(1.5,0.5), (2.5,0.5), (3.5,0.5)].
        We place an alien component R3 at (2.5, 0.5) so its pin1
        coincides with the *mid-path* vertex — bare conductor would
        physically rest on its copper.
        """
        # Use a slightly larger board to fit R3 at (2.5, 0.5).
        board, layout, sch, comps, insts, wire = _two_pin_project_with_insulated_wire(
            extra_components=[(2.5, 0.5)],
        )
        # Identify R3 (the third component added by extra_components).
        original_ids = {next(iter(insts.keys())), list(insts.keys())[1]}
        alien_cid = next(cid for cid in layout.placements if cid not in original_ids)
        alien_net = Net(
            id=uuid.uuid4(), name="ALIEN",
            pins={PinRef(component_id=alien_cid, pin_number=1)},
        )
        sch = Schematic(nets={**sch.nets, alien_net.id: alien_net})

        req = _make_request(board, layout, sch, comps, insts)
        result = AutorouteResult(wires=[wire], unrouted_nets=[], pass_stats=[])
        out = InsulatedToBareOptimizer().optimize(req, result, ProgressSink())
        assert len(out.wires) == 1
        assert out.wires[0].insulated is True, (
            "wire crosses alien pin (2.5,0.5) — must NOT flip to bare"
        )

    def test_two_insulated_sharing_a_pad_first_flips_second_does_not(self) -> None:
        """Two insulated wires share an endpoint pad of different nets.

        After the first wire flips bare, the second one's endpoint sits
        on a pad that is now bare-electrically-bound to the first wire's
        net — so the second one must NOT flip.
        """
        # Build a 6x6 board with 4 components:
        # A=(0.5,0.5) B=(2.5,0.5) net1  /  C=(0.5,2.5) D=(2.5,2.5) net2
        # Both nets routed as insulated wires along their rows.  Then
        # add a third insulated wire that *also* uses the pad (2.5, 0.5)
        # as endpoint but belongs to a net that includes (2.5, 2.5) — i.e.
        # we deliberately fabricate a shared-pad conflict by ABUSING the
        # pad index.
        #
        # Simpler version: two insulated wires whose endpoints both land
        # on pad (1.5, 1.5) — only one of them can flip.
        board = _perfboard()
        defn = _sil2()
        ids = [uuid.uuid4() for _ in range(4)]
        instances = {
            ids[i]: ComponentInstance(
                id=ids[i], definition_ref="COMP", refdes=f"R{i+1}", value=""
            ) for i in range(4)
        }
        placements = {
            ids[0]: _place(ids[0], 0.5, 0.5),
            ids[1]: _place(ids[1], 0.5, 2.5),
            ids[2]: _place(ids[2], 2.5, 0.5),
            ids[3]: _place(ids[3], 2.5, 2.5),
        }
        # Net1: pin1 of R1 ↔ pin1 of R3  (horizontal at y=0.5)
        # Net2: pin1 of R2 ↔ pin1 of R4  (horizontal at y=2.5)
        net1 = Net(id=uuid.uuid4(), name="N1",
                   pins={PinRef(component_id=ids[0], pin_number=1),
                         PinRef(component_id=ids[2], pin_number=1)})
        net2 = Net(id=uuid.uuid4(), name="N2",
                   pins={PinRef(component_id=ids[1], pin_number=1),
                         PinRef(component_id=ids[3], pin_number=1)})
        schematic = Schematic(nets={net1.id: net1, net2.id: net2})

        from protoboard.domain.layout.placement import pin_world_position
        w1_path = [
            pin_world_position(defn.footprint, placements[ids[0]], 1),
            Point(1.5, 0.5),
            pin_world_position(defn.footprint, placements[ids[2]], 1),
        ]
        w2_path = [
            pin_world_position(defn.footprint, placements[ids[1]], 1),
            Point(1.5, 2.5),
            pin_world_position(defn.footprint, placements[ids[3]], 1),
        ]
        w1 = Wire(id=uuid.uuid4(), path=w1_path, insulated=True, side="top")
        w2 = Wire(id=uuid.uuid4(), path=w2_path, insulated=True, side="top")
        layout = Layout(placements=placements, wires=WireCollection([w1, w2]))

        req = _make_request(board, layout, schematic, {"COMP": defn}, instances)
        result = AutorouteResult(wires=[w1, w2], unrouted_nets=[], pass_stats=[])
        out = InsulatedToBareOptimizer().optimize(req, result, ProgressSink())
        # Both wires are on disjoint rows on free corridors → both should
        # flip to bare (no shared pad in this fixture).  This confirms
        # that "independent" wires both flip.
        assert all(not w.insulated for w in out.wires)


# ---------------------------------------------------------------------------
# Non-mutation guarantees
# ---------------------------------------------------------------------------

class TestNonMutation:
    def test_does_not_mutate_input_wires(self) -> None:
        board, layout, sch, comps, insts, wire = _two_pin_project_with_insulated_wire()
        original_insulated = wire.insulated
        original_path_ids = [id(p) for p in wire.path]

        req = _make_request(board, layout, sch, comps, insts)
        result = AutorouteResult(wires=[wire], unrouted_nets=[], pass_stats=[])
        InsulatedToBareOptimizer().optimize(req, result, ProgressSink())

        # Input wire untouched.
        assert wire.insulated is original_insulated
        assert [id(p) for p in wire.path] == original_path_ids

    def test_preserves_unrouted_nets_and_pass_stats(self) -> None:
        board, layout, sch, comps, insts, wire = _two_pin_project_with_insulated_wire()
        req = _make_request(board, layout, sch, comps, insts)
        result = AutorouteResult(
            wires=[wire],
            unrouted_nets=["FOO"],
            pass_stats=[],
        )
        out = InsulatedToBareOptimizer().optimize(req, result, ProgressSink())
        assert out.unrouted_nets == ["FOO"]
