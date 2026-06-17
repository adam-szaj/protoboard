# SPDX-License-Identifier: MIT
"""Tests for GroupTree — the behaviour home for group-hierarchy queries.

Covers the methods directly AND guards that the legacy free-function shims in
``group.py`` delegate identically (so the ~12 caller modules keep working).
"""

from __future__ import annotations

import uuid

from typing import TYPE_CHECKING

from protoboard.domain.layout import group as group_mod
from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.group_tree import GroupTree
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic

if TYPE_CHECKING:
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.layout.placement import ComponentPlacement


def _ids(n: int) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(n)]


def _tree() -> tuple[GroupTree, dict, dict]:
    """Tree A[c1, B[c2, C[c3]]], D[c4] (D is a sibling of A)."""
    c1, c2, c3, c4 = _ids(4)
    A, B, C, D = _ids(4)
    groups = {
        A: ComponentGroup(id=A, name="A", component_ids=frozenset({c1}),
                          child_group_ids=frozenset({B})),
        B: ComponentGroup(id=B, name="B", component_ids=frozenset({c2}),
                          child_group_ids=frozenset({C})),
        C: ComponentGroup(id=C, name="C", component_ids=frozenset({c3})),
        D: ComponentGroup(id=D, name="D", component_ids=frozenset({c4})),
    }
    ids = {"c1": c1, "c2": c2, "c3": c3, "c4": c4, "A": A, "B": B, "C": C, "D": D}
    return GroupTree(groups), groups, ids


# ---------------------------------------------------------------------------
# Hierarchy queries
# ---------------------------------------------------------------------------


class TestHierarchy:
    def test_all_component_ids_recursive(self) -> None:
        t, _, i = _tree()
        assert t.all_component_ids(i["A"]) == frozenset({i["c1"], i["c2"], i["c3"]})
        assert t.all_component_ids(i["C"]) == frozenset({i["c3"]})

    def test_all_component_ids_unknown_group(self) -> None:
        t, _, _ = _tree()
        assert t.all_component_ids(uuid.uuid4()) == frozenset()

    def test_top_level_group_for(self) -> None:
        t, _, i = _tree()
        assert t.top_level_group_for(i["c3"]) == i["A"]
        assert t.top_level_group_for(i["c4"]) == i["D"]
        assert t.top_level_group_for(uuid.uuid4()) is None

    def test_top_level_group_under(self) -> None:
        t, _, i = _tree()
        assert t.top_level_group_under(i["A"], i["c3"]) == i["B"]
        # c1 is a direct member of active A → not under any child group
        assert t.top_level_group_under(i["A"], i["c1"]) is None
        # c4 lives outside A's subtree
        assert t.top_level_group_under(i["A"], i["c4"]) is None

    def test_parent_group_of(self) -> None:
        t, _, i = _tree()
        assert t.parent_group_of(i["C"]) == i["B"]
        assert t.parent_group_of(i["A"]) is None  # top-level, no parent here

    def test_would_create_cycle(self) -> None:
        t, _, i = _tree()
        assert t.would_create_cycle(i["A"], i["A"]) is True            # self
        assert t.would_create_cycle(i["C"], i["A"]) is True            # A ⊃ C
        assert t.would_create_cycle(i["A"], i["D"]) is False           # disjoint

    def test_promote_selection(self) -> None:
        t, _, i = _tree()
        free, promoted = t.promote_selection_to_top_level(i["A"], frozenset({i["c3"]}))
        assert free == frozenset()
        assert promoted == frozenset({i["B"]})


# ---------------------------------------------------------------------------
# Export surface / schematic queries
# ---------------------------------------------------------------------------


class TestExportAndNets:
    def test_validate_exported_pins_flags_broken_chain(self) -> None:
        t, _, i = _tree()
        # B exports a pin of c3 (a grandchild member, not re-exported by C) →
        # invalid from B's perspective.
        ep = PinRef(component_id=i["c3"], pin_number=1)
        groups = dict(t._groups)
        groups[i["B"]] = ComponentGroup(
            id=i["B"], name="B", component_ids=frozenset({i["c2"]}),
            child_group_ids=frozenset({i["C"]}),
            exported_pins=frozenset({ep}),
        )
        t2 = GroupTree(groups)
        assert ep in t2.validate_exported_pins(i["B"])

    def test_internal_net_ids(self) -> None:
        t, _, i = _tree()
        nid = uuid.uuid4()
        sch = Schematic(nets={nid: Net(
            id=nid, name="N",
            pins={PinRef(i["c2"], 1), PinRef(i["c3"], 1)},
        )})
        # both pins inside A's recursive members → internal to {c2,c3}
        assert t.internal_net_ids_for_components(
            frozenset({i["c2"], i["c3"]}), sch) == frozenset({nid})
        # not internal to a smaller set
        assert t.internal_net_ids_for_components(frozenset({i["c2"]}), sch) == frozenset()

    def test_schematic_projection_drops_thin_nets(self) -> None:
        t, _, i = _tree()
        keep, drop = uuid.uuid4(), uuid.uuid4()
        sch = Schematic(nets={
            keep: Net(id=keep, name="K", pins={PinRef(i["c2"], 1), PinRef(i["c3"], 1)}),
            drop: Net(id=drop, name="D", pins={PinRef(i["c2"], 2), PinRef(i["c4"], 1)}),
        })
        proj = t.schematic_projection_for_group(frozenset({i["c2"], i["c3"]}), sch)
        assert set(proj.nets) == {keep}  # drop has only 1 surviving pin


# ---------------------------------------------------------------------------
# Shim equivalence — legacy free functions must delegate identically
# ---------------------------------------------------------------------------


class TestShimEquivalence:
    def test_free_functions_match_methods(self) -> None:
        t, groups, i = _tree()
        assert group_mod.all_component_ids(groups, i["A"]) == t.all_component_ids(i["A"])
        assert group_mod.top_level_group_for(groups, i["c3"]) == t.top_level_group_for(i["c3"])
        assert group_mod.top_level_group_under(groups, i["A"], i["c3"]) == \
            t.top_level_group_under(i["A"], i["c3"])
        assert group_mod.parent_group_of(groups, i["C"]) == t.parent_group_of(i["C"])
        assert group_mod.would_create_cycle(groups, i["C"], i["A"]) == \
            t.would_create_cycle(i["C"], i["A"])
        assert group_mod.promote_selection_to_top_level(groups, i["A"], frozenset({i["c3"]})) == \
            t.promote_selection_to_top_level(i["A"], frozenset({i["c3"]}))


# ---------------------------------------------------------------------------
# Pin-world geometry — pin_world_positions (plural sibling of group_origin)
# ---------------------------------------------------------------------------


def _two_resistors() -> tuple[
    frozenset[uuid.UUID],
    dict[uuid.UUID, ComponentPlacement],
    dict[uuid.UUID, ComponentInstance],
    dict[str, ComponentDefinition],
]:
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.footprints import SIL
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.layout.placement import ComponentPlacement
    from protoboard.geometry import Point

    defn = ComponentDefinition(name="R", footprint=SIL(2), pin_names={1: "A", 2: "B"})
    inst_a = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
    inst_b = ComponentInstance.new(definition_ref="R", refdes="R2", value="10k")
    placements = {
        inst_a.id: ComponentPlacement(
            component_id=inst_a.id, position=Point(0, 0), rotation=0, side="top"
        ),
        inst_b.id: ComponentPlacement(
            component_id=inst_b.id, position=Point(5, 2), rotation=0, side="top"
        ),
    }
    instances = {inst_a.id: inst_a, inst_b.id: inst_b}
    components = {"R": defn}
    return frozenset({inst_a.id, inst_b.id}), placements, instances, components


class TestPinWorldPositions:
    def test_matches_per_pin_resolution(self) -> None:
        from protoboard.domain.layout.placement import pin_world_position

        comp_ids, placements, instances, components = _two_resistors()
        got = GroupTree.pin_world_positions(
            comp_ids, placements, instances, components
        )
        expected: set[tuple[float, float]] = set()
        for cid in comp_ids:
            pl = placements[cid]
            defn = components[instances[cid].definition_ref]
            for n in range(defn.footprint.pin_count()):
                p = pin_world_position(defn.footprint, pl, n + 1)
                expected.add((p.x, p.y))
        assert got == expected
        assert len(got) == 4  # two 2-pin resistors, no coincident pins

    def test_origin_is_top_left_of_positions(self) -> None:
        # group_origin must be the (min x+y) member of pin_world_positions —
        # the two geometry helpers agree on the same point set.
        comp_ids, placements, instances, components = _two_resistors()
        positions = GroupTree.pin_world_positions(
            comp_ids, placements, instances, components
        )
        origin = GroupTree.group_origin(
            comp_ids, placements, instances, components
        )
        assert origin is not None
        assert (origin.x, origin.y) in positions
        assert (origin.x + origin.y) == min(px + py for px, py in positions)

    def test_unresolvable_component_skipped(self) -> None:
        # A comp_id with no placement contributes nothing (no crash).
        comp_ids, placements, instances, components = _two_resistors()
        ghost = uuid.uuid4()
        got = GroupTree.pin_world_positions(
            comp_ids | {ghost}, placements, instances, components
        )
        assert len(got) == 4
