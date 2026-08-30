# SPDX-License-Identifier: MIT
"""Tests for project save/load — instances, wires, board roundtrip."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import cast

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package.family import PackageFamily
from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement, PlacementCollection
from protoboard.domain.layout.wire import Wire, WireCollection
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.services.persistence.project_yaml import load_project, save_project


def _perfboard() -> Board:
    grid = BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=3,
        cols=5,
        row_gap=0.0,
        col_gap=0.0,
    )
    return Board(root=grid)


def test_schematic_persists_metapin_ordinal(tmp_path: Path) -> None:
    component_id = uuid.UUID(int=1)
    net_id = uuid.UUID(int=2)
    schematic = Schematic(
        nets={
            net_id: Net(
                id=net_id,
                name="SIG",
                pins={PinRef(component_id, 2)},
            )
        }
    )

    save_project(
        tmp_path,
        _perfboard(),
        Layout(),
        schematic,
        instances={},
    )

    schematic_yaml = (tmp_path / "schematic.yaml").read_text()
    assert "meta_pin: 2" in schematic_yaml
    assert "pin_number:" not in schematic_yaml
    _, _, loaded, *_ = load_project(tmp_path)
    assert loaded.nets[net_id].pins == {PinRef(component_id, 2)}


class TestInstancesRoundtrip:
    def test_instances_saved_and_loaded(self, tmp_path: Path) -> None:
        board = _perfboard()
        inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        instances = {inst.id: inst}
        lay = Layout(placements=PlacementCollection({}), wires=cast(WireCollection, []))
        sch = Schematic(nets={})

        save_project(tmp_path, board, lay, sch, instances=instances)
        _, _, _, loaded_instances, _, _, _ = load_project(tmp_path)

        assert inst.id in loaded_instances
        loaded = loaded_instances[inst.id]
        assert loaded.refdes == "R1"
        assert loaded.value == "10k"
        assert loaded.definition_ref == "R"

    def test_appearance_roundtrip(self, tmp_path: Path) -> None:
        from protoboard.domain.components.appearance import Appearance

        board = _perfboard()
        styled = ComponentInstance.new(
            definition_ref="R",
            refdes="R1",
            value="10k",
            appearance=Appearance(color="#1a1a1a", corner_radius_mm=(0.4,)),
        )
        plain = ComponentInstance.new(definition_ref="C", refdes="C1", value="100n")
        instances = {styled.id: styled, plain.id: plain}
        save_project(tmp_path, board, Layout(), Schematic(nets={}), instances=instances)
        # The styled instance emits a ``style:`` string; an unstyled one must not
        # gain the key (no file bloat).
        text = (tmp_path / "instances.yaml").read_text()
        assert "style:" in text
        assert "color: #1a1a1a" in text
        assert "corner-radius: 0.4" in text
        _, _, _, loaded, _, _, _ = load_project(tmp_path)
        assert loaded[styled.id].appearance == Appearance(
            color="#1a1a1a", corner_radius_mm=(0.4,)
        )
        assert loaded[plain.id].appearance is None

    def test_empty_instances_roundtrip(self, tmp_path: Path) -> None:
        board = _perfboard()
        lay = Layout(placements=PlacementCollection({}), wires=cast(WireCollection, []))
        sch = Schematic(nets={})
        save_project(tmp_path, board, lay, sch, instances={})
        _, _, _, instances, _, _, _ = load_project(tmp_path)
        assert instances == {}

    def test_multiple_instances_preserved(self, tmp_path: Path) -> None:
        board = _perfboard()
        i1 = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        i2 = ComponentInstance.new(definition_ref="R", refdes="R2", value="4k7")
        instances = {i1.id: i1, i2.id: i2}
        save_project(tmp_path, board, Layout(), Schematic(nets={}), instances=instances)
        _, _, _, loaded, _, _, _ = load_project(tmp_path)
        assert set(loaded.keys()) == {i1.id, i2.id}

    def test_project_without_instances_file_loads_empty(self, tmp_path: Path) -> None:
        """Backwards compat: projects saved before instances.yaml exist."""
        board = _perfboard()
        save_project(tmp_path, board, Layout(), Schematic(nets={}))
        # Manually remove instances.yaml to simulate old project.
        instances_file = tmp_path / "instances.yaml"
        if instances_file.exists():
            instances_file.unlink()
        _, _, _, instances, _, _, _ = load_project(tmp_path)
        assert instances == {}


class TestBoardRoundtrip:
    def test_split_strip_preserved(self, tmp_path: Path) -> None:
        """A board with split strip segments survives save/load via board.yaml."""
        from protoboard.domain.board.boxes import HBox, StripAtom, VBox
        from protoboard.domain.board.pads import PadLayers

        layers = PadLayers(top=False, bottom=True, plated_through=False)
        seg_a = StripAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            length=3,
            orientation="horizontal",
            layers=layers,
        )
        seg_b = StripAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            length=4,
            orientation="horizontal",
            layers=layers,
        )
        row = HBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[seg_a, seg_b],
            gap=0.0,
        )
        board = Board(
            root=VBox(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                children=[row],
                gap=1.0,
            )
        )
        save_project(tmp_path, board, Layout(), Schematic(nets={}))
        loaded_board, _, _, _, _, _, _ = load_project(tmp_path)
        atoms = list(loaded_board.atoms())
        strip_lengths = [a.length for a, _ in atoms if isinstance(a, StripAtom)]
        assert sorted(strip_lengths) == [3, 4]

    def test_isolated_strip_roundtrip(self, tmp_path: Path) -> None:
        from protoboard.domain.board.boxes import HBox, StripAtom, VBox
        from protoboard.domain.board.pads import PadLayers

        layers = PadLayers(top=False, bottom=True, plated_through=False)
        iso = StripAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            length=1,
            orientation="horizontal",
            layers=layers,
            isolated=True,
        )
        row = HBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[iso],
            gap=0.0,
        )
        board = Board(
            root=VBox(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                children=[row],
                gap=1.0,
            )
        )
        save_project(tmp_path, board, Layout(), Schematic(nets={}))
        loaded_board, _, _, _, _, _, _ = load_project(tmp_path)
        atoms = list(loaded_board.atoms())
        iso_atoms = [a for a, _ in atoms if isinstance(a, StripAtom) and a.isolated]
        assert len(iso_atoms) == 1


class TestWiresRoundtrip:
    def test_wire_path_preserved(self, tmp_path: Path) -> None:
        board = _perfboard()
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(1.5, 0.5), Point(2.5, 0.5)],
            insulated=True,
            side="top",
        )
        lay = Layout(
            placements=PlacementCollection({}), wires=cast(WireCollection, [wire])
        )
        save_project(tmp_path, board, lay, Schematic(nets={}))
        _, loaded_lay, _, _, _, _, _ = load_project(tmp_path)
        assert len(loaded_lay.wires) == 1
        loaded_wire = loaded_lay.wires[0]
        assert loaded_wire.path == wire.path
        assert loaded_wire.insulated is True

    def test_wire_color_preserved(self, tmp_path: Path) -> None:
        board = _perfboard()
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(1.5, 0.5)],
            insulated=False,
            side="top",
            color="#FF0000",
        )
        lay = Layout(
            placements=PlacementCollection({}), wires=cast(WireCollection, [wire])
        )
        save_project(tmp_path, board, lay, Schematic(nets={}))
        _, loaded_lay, _, _, _, _, _ = load_project(tmp_path)
        assert loaded_lay.wires[0].color == "#FF0000"

    def test_legacy_insulated_wire_gets_pad_nodes_on_load(
        self,
        tmp_path: Path,
    ) -> None:
        # Legacy project: insulated wire saved with only endpoints, no pad
        # nodes in between.  After load, every pad the path crosses must
        # appear as an intermediate vertex so simplify / unweave / edit
        # tools have "handles" there.
        board = _perfboard()
        wid = uuid.uuid4()
        wire = Wire(
            id=wid,
            path=[Point(0.5, 0.5), Point(4.5, 0.5)],  # straight horizontal
            insulated=True,
            side="top",
        )
        lay = Layout(
            placements=PlacementCollection({}), wires=cast(WireCollection, [wire])
        )
        save_project(tmp_path, board, lay, Schematic(nets={}))
        _, loaded_lay, _, _, _, _, _ = load_project(tmp_path)
        loaded = loaded_lay.wires[0]
        # Pad-node enrichment kept the same id and grew the path.
        assert loaded.id == wid
        coords = [(p.x, p.y) for p in loaded.path]
        # Every pad on the line y=0.5 from x=0.5 to x=4.5 is now a vertex.
        assert coords[0] == (0.5, 0.5)
        assert coords[-1] == (4.5, 0.5)
        assert (1.5, 0.5) in coords
        assert (2.5, 0.5) in coords
        assert (3.5, 0.5) in coords

    def test_load_is_idempotent_for_already_enriched_insulated(
        self,
        tmp_path: Path,
    ) -> None:
        # An already-enriched insulated wire round-trips without growing.
        board = _perfboard()
        wire = Wire(
            id=uuid.uuid4(),
            path=[
                Point(0.5, 0.5),
                Point(1.5, 0.5),
                Point(2.5, 0.5),
                Point(3.5, 0.5),
            ],
            insulated=True,
            side="top",
        )
        lay = Layout(
            placements=PlacementCollection({}), wires=cast(WireCollection, [wire])
        )
        save_project(tmp_path, board, lay, Schematic(nets={}))
        _, loaded_lay, _, _, _, _, _ = load_project(tmp_path)
        assert loaded_lay.wires[0].path == wire.path

    def test_load_does_not_enrich_bare_wires(
        self,
        tmp_path: Path,
    ) -> None:
        # The migration applies only to insulated wires.  Bare wires are
        # produced with pad-nodes by their creators (RouteTool, A*) and the
        # parity model relies on caller-controlled vertices — load doesn't
        # touch them.
        board = _perfboard()
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(2.5, 0.5)],
            insulated=False,
            side="top",
        )
        lay = Layout(
            placements=PlacementCollection({}), wires=cast(WireCollection, [wire])
        )
        save_project(tmp_path, board, lay, Schematic(nets={}))
        _, loaded_lay, _, _, _, _, _ = load_project(tmp_path)
        # Bare wire path comes back unchanged.
        assert loaded_lay.wires[0].path == wire.path


class TestPlacementsRoundtrip:
    def test_placement_position_preserved(self, tmp_path: Path) -> None:
        board = _perfboard()
        comp_id = uuid.uuid4()
        pl = ComponentPlacement(
            component_id=comp_id,
            position=Point(2.5, 1.5),
            rotation=90,
            side="top",
        )
        lay = Layout(
            placements=PlacementCollection({comp_id: pl}),
            wires=cast(WireCollection, []),
        )
        save_project(tmp_path, board, lay, Schematic(nets={}))
        _, loaded_lay, _, _, _, _, _ = load_project(tmp_path)
        loaded_pl = loaded_lay.placements[comp_id]
        assert loaded_pl.position == Point(2.5, 1.5)
        assert loaded_pl.rotation == 90


class TestPackageVariantRoundtrip:
    def test_package_variant_preserved(self, tmp_path: Path) -> None:
        board = _perfboard()
        inst = ComponentInstance.new(
            definition_ref="D1",
            refdes="D1",
            value="",
            package_variant="DO-41/L",
        )
        instances = {inst.id: inst}
        save_project(
            tmp_path,
            board,
            Layout(placements=PlacementCollection({}), wires=cast(WireCollection, [])),
            Schematic(nets={}),
            instances,
        )
        _, _, _, loaded_instances, _, _, _ = load_project(tmp_path)
        assert loaded_instances[inst.id].package_variant == "DO-41/L"

    def test_package_variant_none_preserved(self, tmp_path: Path) -> None:
        board = _perfboard()
        inst = ComponentInstance.new(
            definition_ref="D1",
            refdes="D1",
            value="",
            package_variant=None,
        )
        instances = {inst.id: inst}
        save_project(
            tmp_path,
            board,
            Layout(placements=PlacementCollection({}), wires=cast(WireCollection, [])),
            Schematic(nets={}),
            instances,
        )
        _, _, _, loaded_instances, _, _, _ = load_project(tmp_path)
        assert loaded_instances[inst.id].package_variant is None

    def test_tolerance_preserved(self, tmp_path: Path) -> None:
        board = _perfboard()
        inst = ComponentInstance.new(
            definition_ref="R",
            refdes="R1",
            value="10k",
            tolerance="1%",
        )
        bare = ComponentInstance.new(definition_ref="R", refdes="R2", value="")
        instances = {inst.id: inst, bare.id: bare}
        save_project(
            tmp_path,
            board,
            Layout(placements=PlacementCollection({}), wires=cast(WireCollection, [])),
            Schematic(nets={}),
            instances,
        )
        _, _, _, loaded_instances, _, _, _ = load_project(tmp_path)
        assert loaded_instances[inst.id].tolerance == "1%"
        assert loaded_instances[bare.id].tolerance is None


class TestEncapsulatedExportMigration:
    """Loader strips group exports that violate the re-export chain
    (docs/archive/group-export-encapsulation.md §3.3).  Legacy projects with
    flat grandchild exports load silently with the invalid entries
    removed."""

    def _save_layout_with_groups(
        self, tmp_path: Path, board: Board, groups: dict, comp_ids: set
    ) -> None:
        from protoboard.domain.layout.group import ComponentGroup
        from protoboard.domain.layout.layout import ROOT_GROUP_ID, Layout

        # The migration runs irrespective of placements/wires; pass an
        # empty layout with just the groups we care about.
        root = ComponentGroup(
            id=ROOT_GROUP_ID,
            name="root",
            component_ids=frozenset(),
            child_group_ids=frozenset(
                g.id for g in groups.values() if g.id != ROOT_GROUP_ID
            ),
        )
        all_groups = {ROOT_GROUP_ID: root, **groups}
        lay = Layout(
            placements=PlacementCollection({}),
            wires=cast(WireCollection, []),
            groups=all_groups,
        )
        save_project(tmp_path, board, lay, Schematic(nets={}))

    def test_grandchild_flat_export_stripped(self, tmp_path: Path) -> None:
        from protoboard.domain.layout.group import ComponentGroup
        from protoboard.domain.schematic.net import PinRef

        board = _perfboard()
        c_comp = uuid.uuid4()
        ep = PinRef(component_id=c_comp, pin_number=1)
        c = ComponentGroup(
            id=uuid.uuid4(),
            name="C",
            component_ids=frozenset({c_comp}),
            exported_pins=frozenset({ep}),
        )
        b = ComponentGroup(
            id=uuid.uuid4(),
            name="B",
            child_group_ids=frozenset({c.id}),
        )
        a = ComponentGroup(
            id=uuid.uuid4(),
            name="A",
            child_group_ids=frozenset({b.id}),
            exported_pins=frozenset({ep}),
        )
        self._save_layout_with_groups(
            tmp_path, board, {a.id: a, b.id: b, c.id: c}, {c_comp}
        )
        _, lay_loaded, _, _, _, _, _ = load_project(tmp_path)
        # A's invalid flat export is stripped; C's direct-member export survives.
        assert lay_loaded.groups[a.id].exported_pins == frozenset()
        assert lay_loaded.groups[c.id].exported_pins == frozenset({ep})

    def test_chain_legal_exports_preserved(self, tmp_path: Path) -> None:
        from protoboard.domain.layout.group import ComponentGroup
        from protoboard.domain.schematic.net import PinRef

        board = _perfboard()
        c_comp = uuid.uuid4()
        ep = PinRef(component_id=c_comp, pin_number=1)
        c = ComponentGroup(
            id=uuid.uuid4(),
            name="C",
            component_ids=frozenset({c_comp}),
            exported_pins=frozenset({ep}),
        )
        b = ComponentGroup(
            id=uuid.uuid4(),
            name="B",
            child_group_ids=frozenset({c.id}),
            exported_pins=frozenset({ep}),  # B re-exports → chain ok
        )
        a = ComponentGroup(
            id=uuid.uuid4(),
            name="A",
            child_group_ids=frozenset({b.id}),
            exported_pins=frozenset({ep}),
        )
        self._save_layout_with_groups(
            tmp_path, board, {a.id: a, b.id: b, c.id: c}, {c_comp}
        )
        _, lay_loaded, _, _, _, _, _ = load_project(tmp_path)
        # All three keep the export — chain holds at every level.
        assert lay_loaded.groups[a.id].exported_pins == frozenset({ep})
        assert lay_loaded.groups[b.id].exported_pins == frozenset({ep})
        assert lay_loaded.groups[c.id].exported_pins == frozenset({ep})

    def test_mixed_legal_and_invalid(self, tmp_path: Path) -> None:
        from protoboard.domain.layout.group import ComponentGroup
        from protoboard.domain.schematic.net import PinRef

        board = _perfboard()
        a_direct = uuid.uuid4()
        c_grand = uuid.uuid4()
        ep_legal = PinRef(component_id=a_direct, pin_number=2)
        ep_invalid = PinRef(component_id=c_grand, pin_number=1)

        c = ComponentGroup(
            id=uuid.uuid4(),
            name="C",
            component_ids=frozenset({c_grand}),
            exported_pins=frozenset({ep_invalid}),
        )
        b = ComponentGroup(
            id=uuid.uuid4(),
            name="B",
            child_group_ids=frozenset({c.id}),
            # No re-export, so ep_invalid breaks the chain at B and beyond.
        )
        a = ComponentGroup(
            id=uuid.uuid4(),
            name="A",
            component_ids=frozenset({a_direct}),
            child_group_ids=frozenset({b.id}),
            exported_pins=frozenset({ep_legal, ep_invalid}),
        )
        self._save_layout_with_groups(
            tmp_path, board, {a.id: a, b.id: b, c.id: c}, {a_direct, c_grand}
        )
        _, lay_loaded, _, _, _, _, _ = load_project(tmp_path)
        # A keeps the direct-member export only.
        assert lay_loaded.groups[a.id].exported_pins == frozenset({ep_legal})

    def test_round_trip_legal_unchanged(self, tmp_path: Path) -> None:
        # Save → load → save → load: legal exports survive every cycle.
        from protoboard.domain.layout.group import ComponentGroup
        from protoboard.domain.schematic.net import PinRef

        board = _perfboard()
        comp = uuid.uuid4()
        ep = PinRef(component_id=comp, pin_number=1)
        g = ComponentGroup(
            id=uuid.uuid4(),
            name="G",
            component_ids=frozenset({comp}),
            exported_pins=frozenset({ep}),
        )
        self._save_layout_with_groups(tmp_path, board, {g.id: g}, {comp})
        _, lay_loaded, sch, insts, *_ = load_project(tmp_path)
        assert lay_loaded.groups[g.id].exported_pins == frozenset({ep})
        # Round-trip again — invalid migration must be idempotent.
        save_project(tmp_path, board, lay_loaded, sch, instances=insts)
        _, lay_again, *_ = load_project(tmp_path)
        assert lay_again.groups[g.id].exported_pins == frozenset({ep})


class TestGroupTransformRoundtrip:
    """A group's scene-graph ``transform`` survives save/load (Phase 1).

    The default (identity) transform is not written and reloads as identity; a
    non-identity transform round-trips via its ``coeffs``.
    """

    def _round_trip(self, tmp_path: Path, group: ComponentGroup) -> ComponentGroup:
        from protoboard.domain.layout.group import ComponentGroup
        from protoboard.domain.layout.layout import ROOT_GROUP_ID, Layout

        root = ComponentGroup(
            id=ROOT_GROUP_ID,
            name="root",
            child_group_ids=frozenset({group.id}),
        )
        lay = Layout(
            placements=PlacementCollection({}),
            wires=cast(WireCollection, []),
            groups={ROOT_GROUP_ID: root, group.id: group},
        )
        save_project(tmp_path, _perfboard(), lay, Schematic(nets={}))
        _, lay_loaded, *_ = load_project(tmp_path)
        return lay_loaded.groups[group.id]

    def test_identity_transform_round_trips(self, tmp_path: Path) -> None:
        from protoboard.domain.layout.group import ComponentGroup
        from protoboard.geometry import Transform2D

        g = ComponentGroup(id=uuid.uuid4(), name="G")
        loaded = self._round_trip(tmp_path, g)
        assert loaded.transform == Transform2D.identity()

    def test_non_identity_transform_round_trips(self, tmp_path: Path) -> None:
        from protoboard.domain.layout.group import ComponentGroup
        from protoboard.geometry import Transform2D, Vector

        # translate(10,20) ∘ rotate(90) ∘ reflect — a det=−1 transform with
        # translation, exercising all six coefficients.
        t = (
            Transform2D.translation(Vector(10.0, 20.0))
            .compose(Transform2D.rotation(1))
            .compose(Transform2D.reflection())
        )
        g = ComponentGroup(id=uuid.uuid4(), name="G", transform=t)
        loaded = self._round_trip(tmp_path, g)
        assert loaded.transform == t
        assert loaded.transform.determinant() == -1.0

    def test_identity_transform_not_written(self, tmp_path: Path) -> None:
        # A default-transform group must not add a `transform` key to disk.
        from protoboard.domain.layout.group import ComponentGroup
        from protoboard.domain.layout.layout import ROOT_GROUP_ID, Layout

        g = ComponentGroup(id=uuid.uuid4(), name="G")
        root = ComponentGroup(
            id=ROOT_GROUP_ID,
            name="root",
            child_group_ids=frozenset({g.id}),
        )
        lay = Layout(
            placements=PlacementCollection({}),
            wires=cast(WireCollection, []),
            groups={ROOT_GROUP_ID: root, g.id: g},
        )
        save_project(tmp_path, _perfboard(), lay, Schematic(nets={}))
        layout_yaml = (tmp_path / "layout.yaml").read_text()
        assert "transform:" not in layout_yaml


class _StubLibrary:
    """Migration-library stub: the ``get``/``names`` surface ``load_project``
    needs to resolve a definition during the v2→v3 group migration."""

    def __init__(
        self,
        by_name: dict[str, ComponentDefinition],
        package_families: dict[str, PackageFamily] | None = None,
    ) -> None:
        self._by_name = by_name
        self.package_families = package_families or {}

    def names(self):
        return self._by_name.keys()

    def get(self, name: str) -> ComponentDefinition | None:
        return self._by_name.get(name)


class TestGroupV2ToV3Migration:
    """A rotated group survives save/load with bit-identical world pins, and a
    hand-authored v2 file (baked placements + group rotation, no transform)
    migrates to identical world positions (Phase 3).

    Under the scene-graph model the on-disk placements are group-LOCAL and the
    group carries the rotation as a ``transform``; the read seam recomposes the
    world pose.  These tests are the persistence half of the Phase-3 parity
    guarantee (the in-memory half is ``test_pin_world_parity``).
    """

    def _resistor_defn(self):  # type: ignore[no-untyped-def]
        from tests.unit._autoroute_invariants import make_sil

        return make_sil("R", 3)

    def _big_board(self) -> Board:
        return Board(
            root=BoardGrid(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                cell=PadAtom(
                    width_policy=SizePolicy.HUG,
                    height_policy=SizePolicy.HUG,
                    layers=PadLayers(top=True, bottom=True, plated_through=True),
                ),
                rows=40,
                cols=40,
                row_gap=0.0,
                col_gap=0.0,
            )
        )

    def _world_pins(self, defn, layout, cids):  # type: ignore[no-untyped-def]
        from protoboard.domain.layout.placement import pin_world_position
        from tests.unit._autoroute_invariants import sil_families

        fp = defn.default_footprint(sil_families(defn))
        return {
            cid: [
                (
                    round(pin_world_position(fp, layout.placements[cid], n).x, 4),
                    round(pin_world_position(fp, layout.placements[cid], n).y, 4),
                )
                for n in range(1, fp.pin_count() + 1)
            ]
            for cid in cids
        }

    def test_rotated_group_round_trips_world_positions(self, tmp_path: Path) -> None:
        from dataclasses import dataclass, field

        from protoboard.domain.commands.group_cmds import RotateGroupCommand
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        from protoboard.services.persistence.project_yaml import (
            set_migration_families,
            set_migration_library,
        )

        defn = self._resistor_defn()
        from tests.unit._autoroute_invariants import sil_families

        families = sil_families(defn)
        lib = _StubLibrary({"R": defn}, families)

        @dataclass
        class _State:
            layout: Layout
            instances: dict = field(default_factory=dict)
            components: dict = field(default_factory=dict)
            schematic: Schematic = field(default_factory=lambda: Schematic(nets={}))
            board: Board = field(default_factory=self._big_board)

        layout = Layout(
            placements=PlacementCollection({}), wires=cast(WireCollection, [])
        )
        layout.ensure_root_group()
        state = _State(layout=layout, components={"R": defn})
        cids = []
        for i, (x, y) in enumerate([(10, 10), (13, 10), (10, 14)]):
            inst = ComponentInstance.new(
                definition_ref="R", refdes=f"R{i}", value="1k"
            ).bind(defn, families)
            state.instances[inst.id] = inst
            layout.placements[inst.id] = ComponentPlacement(
                component_id=inst.id,
                position=Point(x, y),
                rotation=0,
                side="top",
            )
            cids.append(inst.id)
        g = ComponentGroup(
            id=uuid.uuid4(),
            name="G",
            component_ids=frozenset(cids),
        )
        layout.groups[g.id] = g
        root = layout.groups[ROOT_GROUP_ID]
        layout.groups[ROOT_GROUP_ID] = root.replace(
            child_group_ids=root.child_group_ids | {g.id}
        )

        RotateGroupCommand(group_id=g.id, delta=90).apply(state)
        before = self._world_pins(defn, layout, cids)
        # The rotation is carried by the group transform, not baked positions.
        assert layout.groups[g.id].transform != g.transform

        set_migration_library(lib)
        set_migration_families(families)
        try:
            save_project(
                tmp_path, state.board, layout, state.schematic, state.instances
            )
            # The group transform must be on disk (scene-graph encoding).
            assert "transform:" in (tmp_path / "layout.yaml").read_text()
            _b, loaded, _s, _i, *_ = load_project(tmp_path)
        finally:
            set_migration_library(None)
            set_migration_families(None)

        after = self._world_pins(defn, loaded, cids)
        assert after == before

    def test_v2_file_migrates_to_identical_world_positions(
        self, tmp_path: Path
    ) -> None:
        import io

        from ruamel.yaml import YAML

        from protoboard.domain.commands.group_cmds import _rotation_about
        from protoboard.domain.layout.placement import (
            compose_placement,
            pin_world_position,
        )
        from protoboard.services.persistence.board_yaml import board_to_yaml
        from protoboard.services.persistence.project_yaml import (
            set_migration_families,
            set_migration_library,
        )

        defn = self._resistor_defn()
        from tests.unit._autoroute_invariants import sil_families

        families = sil_families(defn)
        fp = defn.default_footprint(families)
        lib = _StubLibrary({"R": defn}, families)
        c1, c2 = uuid.uuid4(), uuid.uuid4()
        gid = uuid.uuid4()
        origin = Point(10.0, 10.0)

        # v2 BAKED placements = the rotation flattened into each pose.
        t_g = _rotation_about(origin, 90)
        baked1 = compose_placement(
            t_g, ComponentPlacement(c1, Point(10.0, 10.0), 0, "top")
        )
        baked2 = compose_placement(
            t_g, ComponentPlacement(c2, Point(13.0, 10.0), 0, "top")
        )

        def _world(pl):  # type: ignore[no-untyped-def]
            return [
                (
                    round(pin_world_position(fp, pl, n).x, 4),
                    round(pin_world_position(fp, pl, n).y, 4),
                )
                for n in range(1, fp.pin_count() + 1)
            ]

        expected = {c1: _world(baked1), c2: _world(baked2)}

        # Hand-author a v2 layout.yaml: baked placements, group rotation=90,
        # and NO ``transform`` key — exactly what a pre-scene-graph save wrote.
        def _pl(pl):  # type: ignore[no-untyped-def]
            return {
                "component_id": str(pl.component_id),
                "position": {"x": pl.position.x, "y": pl.position.y},
                "rotation": pl.rotation,
                "side": pl.side,
            }

        data = {
            "format_version": 2,
            "placements": [_pl(baked1), _pl(baked2)],
            "knots": [],
            "wires": [],
            "groups": [
                {"id": "00000000-0000-0000-0000-000000000001", "name": ""},
                {
                    "id": str(gid),
                    "name": "G",
                    "component_ids": sorted([str(c1), str(c2)]),
                    "rotation": 90,
                    "origin": {"x": 10.0, "y": 10.0},
                },
            ],
        }
        yaml = YAML(typ="safe")
        yaml.default_flow_style = False
        buf = io.StringIO()
        yaml.dump(data, buf)
        (tmp_path / "layout.yaml").write_text(buf.getvalue())
        (tmp_path / "board.yaml").write_text(board_to_yaml(self._big_board()))
        (tmp_path / "schematic.yaml").write_text("nets: []\n")
        ibuf = io.StringIO()
        yaml.dump(
            {
                "instances": [
                    {
                        "id": str(c1),
                        "definition_ref": "R",
                        "refdes": "R0",
                        "value": "1k",
                    },
                    {
                        "id": str(c2),
                        "definition_ref": "R",
                        "refdes": "R1",
                        "value": "1k",
                    },
                ]
            },
            ibuf,
        )
        (tmp_path / "instances.yaml").write_text(ibuf.getvalue())

        set_migration_library(lib)
        set_migration_families(families)
        try:
            _b, loaded, _s, _i, *_ = load_project(tmp_path)
        finally:
            set_migration_library(None)
            set_migration_families(None)

        # World positions reconstructed identically, and the group gained a
        # non-identity transform (the v2→v3 conversion).
        from protoboard.geometry import Transform2D

        assert loaded.groups[gid].transform != Transform2D.identity()
        got = {c1: _world(loaded.placements[c1]), c2: _world(loaded.placements[c2])}
        assert got == expected


class TestWirePinPersistence:
    """Wire-pin knots survive save/load with their position + net snapshot
    (docs/archive/wire-pin-implementation.md §3.9).
    """

    def _save_wire_with_wire_pins(
        self,
        tmp_path: Path,
        board: Board,
    ) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID | None]:
        """Construct a wire with both endpoints promoted to wire-pins, save it,
        return (wire_id, knot_a_id, knot_b_id, net_id_a).
        """
        from protoboard.domain.commands.component_cmds import (
            ConvertEndpointToWirePinCommand,
        )
        from protoboard.domain.layout.knot import JointKnot
        from protoboard.domain.layout.wire import KnotRef

        lay = Layout(placements=PlacementCollection({}), wires=cast(WireCollection, []))
        ka = JointKnot(id=uuid.uuid4(), position=Point(0.5, 0.5))
        kb = JointKnot(id=uuid.uuid4(), position=Point(1.5, 0.5))
        lay.knots[ka.id] = ka
        lay.knots[kb.id] = kb
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(1.5, 0.5)],
            insulated=True,
            side="top",
            a=KnotRef(id=ka.id),
            b=KnotRef(id=kb.id),
        )
        lay.add_wire(wire)

        class _S:
            def __init__(self, lay: Layout) -> None:
                self.board = board
                self.layout = lay
                self.instances: dict[uuid.UUID, ComponentInstance] = {}
                self.components: dict[str, ComponentDefinition] = {}

        state = _S(lay)
        net_a = uuid.uuid4()
        ConvertEndpointToWirePinCommand(
            wire_id=wire.id,
            endpoint="a",
            net_id=net_a,
        ).apply(state)
        ConvertEndpointToWirePinCommand(
            wire_id=wire.id,
            endpoint="b",
            net_id=None,
        ).apply(state)

        save_project(tmp_path, board, lay, Schematic(nets={}))
        return wire.id, ka.id, kb.id, net_a

    def test_round_trip_preserves_wire_pin_kind(self, tmp_path: Path) -> None:
        from protoboard.domain.layout.knot import WirePinKnot

        board = _perfboard()
        _, ka_id, kb_id, _ = self._save_wire_with_wire_pins(tmp_path, board)
        _, lay_loaded, _, _, _, _, _ = load_project(tmp_path)
        a_knot = lay_loaded.knots[ka_id]
        b_knot = lay_loaded.knots[kb_id]
        assert isinstance(a_knot, WirePinKnot)
        assert isinstance(b_knot, WirePinKnot)

    def test_round_trip_preserves_position(self, tmp_path: Path) -> None:
        board = _perfboard()
        _, ka_id, kb_id, _ = self._save_wire_with_wire_pins(tmp_path, board)
        _, lay_loaded, _, _, _, _, _ = load_project(tmp_path)
        from protoboard.domain.layout.knot import WirePinKnot

        a_knot = lay_loaded.knots[ka_id]
        b_knot = lay_loaded.knots[kb_id]
        assert isinstance(a_knot, WirePinKnot)
        assert isinstance(b_knot, WirePinKnot)
        assert a_knot.position == Point(0.5, 0.5)
        assert b_knot.position == Point(1.5, 0.5)

    def test_round_trip_preserves_net_snapshot(self, tmp_path: Path) -> None:
        # net_id="..." preserved as uuid; "null" preserved as None.
        board = _perfboard()
        _, ka_id, kb_id, net_a = self._save_wire_with_wire_pins(tmp_path, board)
        _, lay_loaded, _, _, _, _, _ = load_project(tmp_path)
        from protoboard.domain.layout.knot import WirePinKnot

        a_knot = lay_loaded.knots[ka_id]
        b_knot = lay_loaded.knots[kb_id]
        assert isinstance(a_knot, WirePinKnot)
        assert isinstance(b_knot, WirePinKnot)
        assert a_knot.net_id == net_a
        assert b_knot.net_id is None

    def test_unknown_kind_raises(self, tmp_path: Path) -> None:
        # Forward-compat: unknown kind in YAML is an error (legacy
        # versions seeing future kinds fail fast).  Need a non-empty
        # wires block whose first element looks knot-aware
        # ("a_knot_id" present) so _layout_from_yaml routes to the
        # knot-aware decoder.
        import yaml as _yaml

        from protoboard.services.persistence.project_yaml import (
            SerializationError,
            _layout_from_yaml,
        )

        kid = str(uuid.uuid4())
        bad_yaml = _yaml.safe_dump(
            {
                "format_version": 2,
                "placements": [],
                "knots": [
                    {"id": kid, "kind": "future_pin_kind"},
                ],
                "wires": [
                    {
                        "id": str(uuid.uuid4()),
                        "a_knot_id": kid,
                        "b_knot_id": kid,
                        "path": [{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 0.0}],
                        "side": "top",
                        "insulated": False,
                    },
                ],
            }
        )
        import pytest

        with pytest.raises(SerializationError, match="unknown knot kind"):
            _layout_from_yaml(bad_yaml)


class TestDropDanglingInstances:
    """Components whose definition is missing (e.g. bad package skipped by the
    library) are dropped from the board on load; the project still loads."""

    class _FakeLibrary:
        def __init__(self, definitions: dict[str, ComponentDefinition]) -> None:
            self._definitions = definitions

        def get(self, name: str) -> ComponentDefinition | None:
            return self._definitions.get(name)

    def test_dangling_instance_dropped(self, tmp_path: Path) -> None:
        from protoboard.services.persistence.project_yaml import (
            set_migration_library,
        )

        board = _perfboard()
        good = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        bad = ComponentInstance.new(definition_ref="GHOST", refdes="X1", value="?")
        instances = {good.id: good, bad.id: bad}
        pl_good = ComponentPlacement(
            component_id=good.id,
            position=Point(0.0, 0.0),
            rotation=0,
            side="top",
        )
        pl_bad = ComponentPlacement(
            component_id=bad.id,
            position=Point(1.0, 0.0),
            rotation=0,
            side="top",
        )
        lay = Layout(
            placements=PlacementCollection({good.id: pl_good, bad.id: pl_bad}),
            wires=cast(WireCollection, []),
        )
        save_project(tmp_path, board, lay, Schematic(nets={}), instances=instances)

        from tests.unit._autoroute_invariants import make_sil

        set_migration_library(self._FakeLibrary({"R": make_sil("R")}))
        try:
            _, loaded_layout, _, loaded_instances, _, _, _ = load_project(tmp_path)
        finally:
            set_migration_library(None)

        # bad dropped from instances AND placements; good survives.
        assert good.id in loaded_instances
        assert bad.id not in loaded_instances
        assert good.id in loaded_layout.placements
        assert bad.id not in loaded_layout.placements

    def test_no_library_keeps_everything(self, tmp_path: Path) -> None:
        from protoboard.services.persistence.project_yaml import (
            set_migration_library,
        )

        board = _perfboard()
        bad = ComponentInstance.new(definition_ref="GHOST", refdes="X1", value="?")
        instances = {bad.id: bad}
        save_project(tmp_path, board, Layout(), Schematic(nets={}), instances=instances)

        set_migration_library(None)  # no library → no dropping
        _, _, _, loaded_instances, _, _, _ = load_project(tmp_path)
        assert bad.id in loaded_instances
