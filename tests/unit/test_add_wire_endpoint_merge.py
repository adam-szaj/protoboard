# SPDX-License-Identifier: MIT
"""AddWireCommand endpoint-merge behaviour.

When a new wire's endpoint lands on an off-pin pad already occupied by
exactly one other wire and they share ``insulated``, ``AddWireCommand``
fuses them into a single Wire so the underlying model matches what the
user sees on screen (one continuous solderable artifact).
"""

from __future__ import annotations

import uuid
from typing import cast

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.commands.component_cmds import (
    AddWireCommand,
)
from protoboard.domain.components.definition import (
    ComponentDefinition,
    MetaPin,
    PackageOption,
)
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package import PackageFamily, PackageVariant
from protoboard.domain.components.package.loader import build_package
from protoboard.domain.layout.knot import JointKnot, PinKnot
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import PlacementCollection
from protoboard.domain.layout.wire import Wire, WireCollection
from protoboard.geometry import Point


def _perfboard(rows: int = 5, cols: int = 5) -> Board:
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


class _State:
    """Minimal ComponentState stub for AddWireCommand."""

    def __init__(self) -> None:
        self.board = _perfboard()
        self.layout = Layout(placements=PlacementCollection({}), wires=WireCollection())
        self.instances: dict[uuid.UUID, ComponentInstance] = {}
        self.components: dict[str, ComponentDefinition] = {}
        self.package_families: dict[str, PackageFamily] = {}


def _one_pin_definition(name: str) -> tuple[ComponentDefinition, PackageFamily]:
    family_id = f"{name}-package"
    package = build_package(
        {
            "shape": "prism",
            "dims": {"lx": 2.0, "ly": 2.0},
            "arrangement": "axial",
            "mount": "through_hole",
            "bend": 0,
            "leads": {
                "direction": [[0, 0, -1]],
                "body_offset": {"unit": 2.5, "value": []},
                "board_offset": "default",
            },
        },
        family_id,
    )
    family = PackageFamily(
        family_id=family_id,
        variants=(PackageVariant("std", "std", package),),
    )
    definition = ComponentDefinition(
        name=name,
        meta_pins=(MetaPin(("P1",)),),
        package_options=(
            PackageOption(
                name=name,
                ref=f"{family_id}/std",
                pin_map={1: (1,)},
            ),
        ),
    )
    return definition, family


def _add(
    state: _State,
    path: list[tuple[float, float]],
    *,
    insulated: bool = True,
    side: str = "top",
) -> AddWireCommand:
    wire = Wire(
        id=uuid.uuid4(),
        path=[Point(x, y) for x, y in path],
        insulated=insulated,
        side=side,  # type: ignore[arg-type]
    )
    cmd = AddWireCommand(wire=wire)
    cmd.apply(state)
    return cmd


# ---------------------------------------------------------------------------
# Same-side, same-insulated → fuse
# ---------------------------------------------------------------------------


class TestEndpointMergeSameAttrs:
    def test_two_same_side_wires_meeting_fuse(self) -> None:
        state = _State()
        _add(state, [(0.5, 0.5), (2.5, 0.5)])
        _add(state, [(2.5, 0.5), (4.5, 0.5)])
        # Both wires share endpoint at (2.5, 0.5) which is *not* a pin —
        # they fuse into one Wire.  Insulated wires also get pad-vertex
        # enrichment, so the fused path includes every intermediate pad.
        assert len(state.layout.wires) == 1
        w = list(state.layout.wires)[0]
        path_pts = [(p.x, p.y) for p in w.path]
        # Endpoints survive (in either order, since the collapse may flip
        # orientation); pad vertices appear in between.
        assert {path_pts[0], path_pts[-1]} == {(0.5, 0.5), (4.5, 0.5)}
        assert (2.5, 0.5) in path_pts
        assert len(w.fragments) == 1  # same-side → single fragment

    def test_joint_at_fusion_pad_is_consumed(self) -> None:
        state = _State()
        _add(state, [(0.5, 0.5), (2.5, 0.5)])
        _add(state, [(2.5, 0.5), (4.5, 0.5)])
        # The joint at (2.5, 0.5) should no longer exist — the two wires
        # collapsed into one and the central joint was consumed.
        joint_positions = [
            k.position for k in state.layout.knots.values() if isinstance(k, JointKnot)
        ]
        assert all(
            not (abs(p.x - 2.5) < 1e-9 and abs(p.y - 0.5) < 1e-9)
            for p in joint_positions
        )

    def test_three_wires_in_a_row_fuse(self) -> None:
        state = _State()
        _add(state, [(0.5, 0.5), (1.5, 0.5)])
        _add(state, [(1.5, 0.5), (2.5, 0.5)])
        _add(state, [(2.5, 0.5), (3.5, 0.5)])
        # Three wires meeting end-to-end at (1.5,…) and (2.5,…) — both
        # joints are degree-2 in sequence; both should collapse.
        assert len(state.layout.wires) == 1
        w = list(state.layout.wires)[0]
        path_pts = [(p.x, p.y) for p in w.path]
        assert {path_pts[0], path_pts[-1]} == {(0.5, 0.5), (3.5, 0.5)}
        assert (1.5, 0.5) in path_pts
        assert (2.5, 0.5) in path_pts


# ---------------------------------------------------------------------------
# Cross-side via plated-through → fuse into multi-fragment Wire
# ---------------------------------------------------------------------------


class TestEndpointMergeCrossSide:
    def test_top_bottom_via_pad_fuses_into_two_fragments(self) -> None:
        state = _State()  # perfboard pads are plated-through
        _add(state, [(0.5, 0.5), (2.5, 0.5)], side="top")
        _add(state, [(2.5, 0.5), (4.5, 0.5)], side="bottom")
        # Plated-through pad at (2.5, 0.5) lets the two sides bridge
        # electrically — the wires fuse, but the resulting Wire has TWO
        # fragments (one per side) so the via boundary survives.
        assert len(state.layout.wires) == 1
        w = list(state.layout.wires)[0]
        assert len(w.fragments) == 2
        assert {f.side for f in w.fragments} == {"top", "bottom"}


# ---------------------------------------------------------------------------
# Mismatched attrs → no fuse
# ---------------------------------------------------------------------------


class TestEndpointMergeMismatchedAttrs:
    def test_mismatched_insulated_does_not_fuse(self) -> None:
        state = _State()
        _add(state, [(0.5, 0.5), (2.5, 0.5)], insulated=True)
        _add(state, [(2.5, 0.5), (4.5, 0.5)], insulated=False)
        # bare ≠ insulated — each wire is a physically distinct artifact
        # (bare bus + insulated jumper) so they stay separate, joined by
        # a shared JointKnot.
        assert len(state.layout.wires) == 2

    def test_shared_joint_persists_when_no_fuse(self) -> None:
        state = _State()
        _add(state, [(0.5, 0.5), (2.5, 0.5)], insulated=True)
        _add(state, [(2.5, 0.5), (4.5, 0.5)], insulated=False)
        joint_at_pad = [
            k
            for k in state.layout.knots.values()
            if isinstance(k, JointKnot)
            and abs(k.position.x - 2.5) < 1e-9
            and abs(k.position.y - 0.5) < 1e-9
        ]
        assert len(joint_at_pad) == 1
        # The single joint references both wires.
        assert len(joint_at_pad[0].wire_ids) == 2


# ---------------------------------------------------------------------------
# Undo / redo
# ---------------------------------------------------------------------------


class TestEndpointMergeUndo:
    def test_undo_after_fusion_restores_pre_cmd_state(self) -> None:
        state = _State()
        cmd1 = _add(state, [(0.5, 0.5), (2.5, 0.5)])
        cmd2 = _add(state, [(2.5, 0.5), (4.5, 0.5)])
        assert len(state.layout.wires) == 1  # fused
        cmd2.revert(state)
        # Pre-cmd2 state: only the first wire (no fuse, no second wire).
        assert len(state.layout.wires) == 1
        assert cmd1.wire.id in state.layout.wires

    def test_redo_re_fuses_after_undo(self) -> None:
        state = _State()
        _add(state, [(0.5, 0.5), (2.5, 0.5)])
        cmd2 = _add(state, [(2.5, 0.5), (4.5, 0.5)])
        cmd2.revert(state)
        cmd2.apply(state)
        assert len(state.layout.wires) == 1

    def test_no_fusion_no_collapse_state(self) -> None:
        state = _State()
        _add(state, [(0.5, 0.5), (2.5, 0.5)], insulated=True)
        cmd2 = _add(
            state,
            [(2.5, 0.5), (4.5, 0.5)],
            insulated=False,
        )
        # No fusion happened → _collapses is empty → revert simply
        # removes the second wire.
        assert cmd2._collapses == []
        cmd2.revert(state)
        assert len(state.layout.wires) == 1


# ---------------------------------------------------------------------------
# Pin endpoints stay separate
# ---------------------------------------------------------------------------


def _state_with_pin_at(pos: Point) -> _State:
    """Helper that places a 1-pin SIL component at ``pos`` so the pad
    becomes a real PinKnot site."""
    from protoboard.domain.layout.placement import ComponentPlacement

    state = _State()
    defn, family = _one_pin_definition("STUB")
    state.package_families[family.family_id] = family
    state.components["STUB"] = defn
    comp_id = uuid.uuid4()
    state.instances[comp_id] = ComponentInstance(
        id=comp_id,
        definition_ref="STUB",
        refdes="X1",
        value="",
    ).bind(defn, state.package_families)
    state.layout.placements[comp_id] = ComponentPlacement(
        component_id=comp_id,
        position=pos,
        rotation=0,
        side="top",
    )
    return state


class TestEndpointMergeOnPin:
    def test_two_wires_at_pin_stay_separate(self) -> None:
        state = _state_with_pin_at(Point(2.5, 0.5))
        _add(state, [(0.5, 0.5), (2.5, 0.5)])
        _add(state, [(2.5, 0.5), (4.5, 0.5)])
        # Both wires end at the same pin — pin is a physical hub, no fuse.
        assert len(state.layout.wires) == 2
        # PinKnot exists at the pin's position.
        pin_knots = [k for k in state.layout.knots.values() if isinstance(k, PinKnot)]
        assert len(pin_knots) == 1


class TestInteriorSplitOnAdd:
    """AddWireCommand should T-junction-split an existing wire when the
    new wire's endpoint lands on its interior vertex."""

    def test_new_wire_endpoint_on_interior_splits(self) -> None:
        state = _State()
        # First wire passes through (2.5, 0.5) (a pad-vertex on the way).
        _add(state, [(0.5, 0.5), (4.5, 0.5)])
        wires_before = len(state.layout.wires)
        # Second wire ends at the middle of the first.
        _add(state, [(2.5, 2.5), (2.5, 0.5)])
        # Expect three wires total: split halves + the new one.
        assert len(state.layout.wires) == wires_before + 2
        # And a shared hub (JointKnot) at (2.5, 0.5) with degree-3.
        from protoboard.domain.layout.knot import JointKnot

        hub_candidates = [
            k
            for k in state.layout.knots.values()
            if isinstance(k, JointKnot)
            and abs(k.position.x - 2.5) < 1e-9
            and abs(k.position.y - 0.5) < 1e-9
        ]
        assert len(hub_candidates) == 1
        assert len(hub_candidates[0].wire_ids) == 3

    def test_undo_split_restores_first_wire(self) -> None:
        state = _State()
        _add(state, [(0.5, 0.5), (4.5, 0.5)])
        wires_before = len(state.layout.wires)
        cmd2 = _add(state, [(2.5, 2.5), (2.5, 0.5)])
        assert len(state.layout.wires) == wires_before + 2
        cmd2.revert(state)
        # Original wire is back as one piece; new wire is gone.
        assert len(state.layout.wires) == wires_before
        # And no JointKnot at the split position (was a non-pin pad —
        # the hub created by make_t_junction was orphan after revert).
        from protoboard.domain.layout.knot import JointKnot

        hub_candidates = [
            k
            for k in state.layout.knots.values()
            if isinstance(k, JointKnot)
            and abs(k.position.x - 2.5) < 1e-9
            and abs(k.position.y - 0.5) < 1e-9
        ]
        assert len(hub_candidates) == 0

    def test_no_split_when_no_interior_collision(self) -> None:
        state = _State()
        _add(state, [(0.5, 0.5), (2.5, 0.5)])
        cmd2 = _add(state, [(2.5, 2.5), (3.5, 2.5)])
        assert cmd2._splits == []


# ---------------------------------------------------------------------------
# Cross-net joint reuse → MUST refuse to share a single hub
# ---------------------------------------------------------------------------


class _SchematicState(_State):
    """State with a real Schematic — required for AddWireCommand to gate
    cross-net joint reuse.  Each wire is anchored at the start endpoint
    to a 1-pin SIL component pinned to a different net so the wire's
    intended net resolves unambiguously."""

    def __init__(self) -> None:
        super().__init__()
        from protoboard.domain.schematic.schematic import Schematic

        self.schematic = Schematic(nets={})


def _place_pin_at(
    state: _SchematicState,
    refdes: str,
    pos: Point,
    side: str = "top",
) -> ComponentInstance:
    """Drop a 1-pin SIL component at *pos* on *side*.  Returns the new instance."""
    from protoboard.domain.layout.placement import ComponentPlacement

    defn_name = f"_R_{refdes}"
    defn, family = _one_pin_definition(defn_name)
    state.package_families[family.family_id] = family
    state.components[defn_name] = defn
    inst = ComponentInstance.new(
        definition_ref=defn_name,
        refdes=refdes,
        value="",
    ).bind(defn, state.package_families)
    state.instances[inst.id] = inst
    state.layout.placements[inst.id] = ComponentPlacement(
        component_id=inst.id,
        position=pos,
        rotation=0,
        side=side,  # type: ignore[arg-type]
    )
    return inst


def _net_with(
    state: _SchematicState,
    name: str,
    *pins: tuple[ComponentInstance, int],
) -> uuid.UUID:
    from protoboard.domain.schematic.net import Net, PinRef

    nid = uuid.uuid4()
    state.schematic.nets[nid] = Net(
        id=nid,
        name=name,
        pins={PinRef(component_id=i.id, pin_number=p) for i, p in pins},
    )
    return nid


class TestEndpointMergeCrossNet:
    """Two wires of different schematic nets meeting at an off-pin pad
    must NOT share a JointKnot — that would short the two nets via a
    phantom solder joint.  Regression: the reference_big_project bug
    where 4 wires of mixed nets converged on (25.5, 6.5) and produced
    a 22-net electric cluster."""

    def test_cross_net_endpoints_get_separate_joints(self) -> None:
        state = _SchematicState()
        # Pins anchor each wire to a distinct net.
        r1 = _place_pin_at(state, "R1", Point(0.5, 0.5))
        r2 = _place_pin_at(state, "R2", Point(4.5, 0.5))
        # NET_A spans R1 only; NET_B spans R2 only.  Both wires "meet" at
        # off-pin pad (2.5, 0.5).
        _net_with(state, "NET_A", (r1, 1))
        _net_with(state, "NET_B", (r2, 1))
        _add(state, [(0.5, 0.5), (2.5, 0.5)])
        _add(state, [(4.5, 0.5), (2.5, 0.5)])
        # Two joints at (2.5, 0.5), one per net — not one shared hub.
        joints_here = [
            k
            for k in state.layout.knots.values()
            if isinstance(k, JointKnot)
            and abs(k.position.x - 2.5) < 1e-9
            and abs(k.position.y - 0.5) < 1e-9
        ]
        assert len(joints_here) == 2
        # Each joint has exactly its own wire.
        assert all(len(k.wire_ids) == 1 for k in joints_here)

    def test_same_net_endpoints_share_one_joint(self) -> None:
        """Sanity: same-net wires meeting at an off-pin pad must still
        share one hub (so they can fuse via merge_endpoint_wires later)."""
        state = _SchematicState()
        r1 = _place_pin_at(state, "R1", Point(0.5, 0.5))
        r2 = _place_pin_at(state, "R2", Point(4.5, 0.5))
        _net_with(state, "NET_AB", (r1, 1), (r2, 1))
        _add(state, [(0.5, 0.5), (2.5, 0.5)])
        _add(state, [(4.5, 0.5), (2.5, 0.5)])
        joints_here = [
            k
            for k in state.layout.knots.values()
            if isinstance(k, JointKnot)
            and abs(k.position.x - 2.5) < 1e-9
            and abs(k.position.y - 0.5) < 1e-9
        ]
        # Either one shared joint (degree 2) or zero (if collapsed into
        # one wire).  The key invariant is: there is no phantom split.
        assert len(joints_here) <= 1

    def test_cross_net_with_pin_on_top_wire_on_bottom(self) -> None:
        """The autoroute scenario from reference_big_project: each wire is
        on the BOTTOM layer but its component pin sits on the TOP layer
        of the same plated-through hole.  The cross-net joint gate must
        still recognise the bottom wire as belonging to the top pin's
        net (via through-hole) and refuse to share a hub between two
        different nets meeting at an off-pad point."""
        state = _SchematicState()
        # Override the 5×5 default with a board big enough to host an
        # off-pad meeting point at (3.5, 2.5).  Plated-through, so the
        # bottom wires reach the top pins electrically.
        state.board = _perfboard(rows=4, cols=8)
        # R1 (NET_A) at (0.5, 0.5) TOP, R2 (NET_B) at (6.5, 0.5) TOP.
        r1 = _place_pin_at(state, "R1", Point(0.5, 0.5), side="top")
        r2 = _place_pin_at(state, "R2", Point(6.5, 0.5), side="top")
        _net_with(state, "NET_A", (r1, 1))
        _net_with(state, "NET_B", (r2, 1))
        # Both wires routed on BOTTOM, meeting at off-pad (3.5, 2.5).
        _add(state, [(0.5, 0.5), (3.5, 2.5)], side="bottom")
        _add(state, [(6.5, 0.5), (3.5, 2.5)], side="bottom")
        joints_here = [
            k
            for k in state.layout.knots.values()
            if isinstance(k, JointKnot)
            and abs(k.position.x - 3.5) < 1e-9
            and abs(k.position.y - 2.5) < 1e-9
        ]
        # Two distinct joints — one per net — must remain.
        assert len(joints_here) == 2
        assert all(len(k.wire_ids) == 1 for k in joints_here)


# ---------------------------------------------------------------------------
# Strategy-driven net gates (rule 8 — _find_pin_at via can_place_pin)
# ---------------------------------------------------------------------------


class _StrategyState(_SchematicState):
    """SchematicState that also exposes ``pad_index`` and ``strategy_for``
    — the two attributes ``_resolve_endpoint_knot`` reads through the
    duck-typed ``_strategy_from_state``/``_pad_index_from_state``
    helpers.  Used by tests that need the Strategy-driven net gate
    (rule 8 ``can_place_pin``) to actually fire."""

    def __init__(self) -> None:
        super().__init__()
        self._pad_index_cache = None

    def pad_index(self):
        if self._pad_index_cache is None:
            from protoboard.domain.board.pad_state import PadIndex

            self._pad_index_cache = PadIndex(
                board=self.board,
                layout=self.layout,
                schematic=self.schematic,
                components=self.components,
                instances=self.instances,
            )
        return self._pad_index_cache

    def invalidate_pad_index(self) -> None:
        self._pad_index_cache = None

    def strategy_for(self, subsystem: str):
        from protoboard.domain.layout.pad_strategy import (
            StrategyConfig,
            SubsystemName,
            make_strategy,
        )

        name = cast(SubsystemName, subsystem)
        return make_strategy(name, StrategyConfig.from_session(self, name))


class TestPinNetGate:
    """Rule 8 — :func:`_find_pin_at` consults the DRC strategy and
    refuses to return a pin whose net would conflict with the incoming
    wire's net.  When the gate rejects, the resolver falls back to
    creating a fresh JointKnot at the same position (the geometry
    still exists — the model just doesn't pretend the wires are
    bonded through the pin)."""

    def test_foreign_net_wire_does_not_attach_to_pin(self) -> None:
        state = _StrategyState()
        r1 = _place_pin_at(state, "R1", Point(0.5, 0.5))
        r2 = _place_pin_at(state, "R2", Point(4.5, 0.5))
        _net_with(state, "NET_A", (r1, 1))
        _net_with(state, "NET_B", (r2, 1))
        state.invalidate_pad_index()
        # Wire of NET_B starts at R2's pin (resolves wire net to NET_B
        # via _wire_intended_net) and ends ON R1's pin (NET_A).
        _add(state, [(4.5, 0.5), (0.5, 0.5)])
        # The wire was added, but its endpoint at (0.5, 0.5) is NOT a
        # PinKnot referencing R1 — the strategy gate rejected, so we
        # fell through to JointKnot.
        wire = list(state.layout.wires)[0]
        assert wire.b is not None
        end_knot = state.layout.knots[wire.b.id]
        assert isinstance(end_knot, JointKnot), (
            "wire of NET_B must not solder to R1's NET_A pin; expected "
            "JointKnot fallback"
        )

    def test_same_net_wire_attaches_to_pin(self) -> None:
        """Sanity inverse: same-net wire ends at the pin via PinKnot."""
        state = _StrategyState()
        r1 = _place_pin_at(state, "R1", Point(0.5, 0.5))
        r2 = _place_pin_at(state, "R2", Point(4.5, 0.5))
        _net_with(state, "NET_AB", (r1, 1), (r2, 1))
        state.invalidate_pad_index()
        _add(state, [(4.5, 0.5), (0.5, 0.5)])
        wire = list(state.layout.wires)[0]
        assert wire.b is not None
        end_knot = state.layout.knots[wire.b.id]
        assert isinstance(end_knot, PinKnot)

    def test_orphan_wire_attaches_to_pin(self) -> None:
        """A wire with no resolved net (no pin on path → orphan)
        should still attach to the pin — orphans are tolerated; the
        new wire inherits the pin's net via the existing T-junction
        machinery on the next pass."""
        state = _StrategyState()
        r1 = _place_pin_at(state, "R1", Point(0.5, 0.5))
        _net_with(state, "NET_A", (r1, 1))
        state.invalidate_pad_index()
        # Wire from off-pad (2.5, 2.5) to R1's pin (0.5, 0.5) — no
        # other pin on the path so the wire is net-less.
        _add(state, [(2.5, 2.5), (0.5, 0.5)])
        wire = list(state.layout.wires)[0]
        assert wire.b is not None
        # Endpoint resolves to R1's PinKnot — orphan + named is the
        # ASSIGN_NET case, which still ALLOWs the pin attachment.
        end_knot = state.layout.knots[wire.b.id]
        assert isinstance(end_knot, PinKnot)
