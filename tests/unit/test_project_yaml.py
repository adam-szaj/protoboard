# SPDX-License-Identifier: MIT
"""Tests for project save/load — instances, wires, board roundtrip."""

from __future__ import annotations

import uuid
from pathlib import Path

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import WireSegment
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


class TestInstancesRoundtrip:
    def test_instances_saved_and_loaded(self, tmp_path: Path) -> None:
        board = _perfboard()
        inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        instances = {inst.id: inst}
        lay = Layout(placements={}, wires=[])
        sch = Schematic(nets={})

        save_project(tmp_path, board, lay, sch, instances=instances)
        _, _, _, loaded_instances, _ = load_project(tmp_path)

        assert inst.id in loaded_instances
        loaded = loaded_instances[inst.id]
        assert loaded.refdes == "R1"
        assert loaded.value == "10k"
        assert loaded.definition_ref == "R"

    def test_empty_instances_roundtrip(self, tmp_path: Path) -> None:
        board = _perfboard()
        lay = Layout(placements={}, wires=[])
        sch = Schematic(nets={})
        save_project(tmp_path, board, lay, sch, instances={})
        _, _, _, instances, _ = load_project(tmp_path)
        assert instances == {}

    def test_multiple_instances_preserved(self, tmp_path: Path) -> None:
        board = _perfboard()
        i1 = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        i2 = ComponentInstance.new(definition_ref="R", refdes="R2", value="4k7")
        instances = {i1.id: i1, i2.id: i2}
        save_project(tmp_path, board, Layout(), Schematic(nets={}), instances=instances)
        _, _, _, loaded, _ = load_project(tmp_path)
        assert set(loaded.keys()) == {i1.id, i2.id}

    def test_project_without_instances_file_loads_empty(self, tmp_path: Path) -> None:
        """Backwards compat: projects saved before instances.yaml exist."""
        board = _perfboard()
        save_project(tmp_path, board, Layout(), Schematic(nets={}))
        # Manually remove instances.yaml to simulate old project.
        instances_file = tmp_path / "instances.yaml"
        if instances_file.exists():
            instances_file.unlink()
        _, _, _, instances, _ = load_project(tmp_path)
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
        loaded_board, _, _, _, _ = load_project(tmp_path)
        atoms = list(loaded_board.atoms())
        strip_lengths = [
            a.length for a, _ in atoms if isinstance(a, StripAtom)
        ]
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
        loaded_board, _, _, _, _ = load_project(tmp_path)
        atoms = list(loaded_board.atoms())
        iso_atoms = [a for a, _ in atoms if isinstance(a, StripAtom) and a.isolated]
        assert len(iso_atoms) == 1


class TestWiresRoundtrip:
    def test_wire_path_preserved(self, tmp_path: Path) -> None:
        board = _perfboard()
        wire = WireSegment(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(1.5, 0.5), Point(2.5, 0.5)],
            insulated=True,
            side="top",
        )
        lay = Layout(placements={}, wires=[wire])
        save_project(tmp_path, board, lay, Schematic(nets={}))
        _, loaded_lay, _, _, _ = load_project(tmp_path)
        assert len(loaded_lay.wires) == 1
        loaded_wire = loaded_lay.wires[0]
        assert loaded_wire.path == wire.path
        assert loaded_wire.insulated is True

    def test_wire_color_preserved(self, tmp_path: Path) -> None:
        board = _perfboard()
        wire = WireSegment(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(1.5, 0.5)],
            insulated=False,
            side="top",
            color="#FF0000",
        )
        lay = Layout(placements={}, wires=[wire])
        save_project(tmp_path, board, lay, Schematic(nets={}))
        _, loaded_lay, _, _, _ = load_project(tmp_path)
        assert loaded_lay.wires[0].color == "#FF0000"


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
        lay = Layout(placements={comp_id: pl}, wires=[])
        save_project(tmp_path, board, lay, Schematic(nets={}))
        _, loaded_lay, _, _, _ = load_project(tmp_path)
        loaded_pl = loaded_lay.placements[comp_id]
        assert loaded_pl.position == Point(2.5, 1.5)
        assert loaded_pl.rotation == 90


class TestPinVariantRoundtrip:
    def test_pin_variant_preserved(self, tmp_path: Path) -> None:
        board = _perfboard()
        inst = ComponentInstance.new(
            definition_ref="PinHeader_1x04",
            refdes="J1",
            value="",
            pin_variant="i2c_vcc_gnd_sda_scl",
        )
        instances = {inst.id: inst}
        save_project(tmp_path, board, Layout(placements={}, wires=[]), Schematic(nets={}), instances)
        _, _, _, loaded_instances, _ = load_project(tmp_path)
        loaded = loaded_instances[inst.id]
        assert loaded.pin_variant == "i2c_vcc_gnd_sda_scl"

    def test_pin_variant_none_preserved(self, tmp_path: Path) -> None:
        board = _perfboard()
        inst = ComponentInstance.new(
            definition_ref="PinHeader_1x04",
            refdes="J1",
            value="",
            pin_variant=None,
        )
        instances = {inst.id: inst}
        save_project(tmp_path, board, Layout(placements={}, wires=[]), Schematic(nets={}), instances)
        _, _, _, loaded_instances, _ = load_project(tmp_path)
        loaded = loaded_instances[inst.id]
        assert loaded.pin_variant is None
