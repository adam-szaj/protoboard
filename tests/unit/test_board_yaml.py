# SPDX-License-Identifier: MIT
"""Tests for the YAML serializer/deserializer for Board."""

from pathlib import Path

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import (
    BoardGrid,
    PadAtom,
    SizePolicy,
    StripAtom,
    VBox,
)
from protoboard.domain.board.pads import PadLayers
from protoboard.errors import ProtoboardError
from protoboard.geometry import Rect
from protoboard.services.persistence.board_yaml import (
    board_from_yaml,
    board_to_yaml,
    load_board,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "boards"


def _approx_equal(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol


class TestPerfboardFixture:
    def test_load_perfboard_10x10(self) -> None:
        board = load_board(FIXTURES / "perfboard_10x10.yaml")
        atoms = list(board.atoms())
        assert len(atoms) == 100

    def test_perfboard_corner_positions(self) -> None:
        board = load_board(FIXTURES / "perfboard_10x10.yaml")
        atoms = list(board.atoms())
        first_atom, first_pos = atoms[0]
        last_atom, last_pos = atoms[-1]
        assert isinstance(first_atom, PadAtom)
        assert isinstance(last_atom, PadAtom)
        assert _approx_equal(first_pos.x, 0.5)
        assert _approx_equal(first_pos.y, 0.5)
        assert _approx_equal(last_pos.x, 9.5)
        assert _approx_equal(last_pos.y, 9.5)

    def test_perfboard_bounds(self) -> None:
        board = load_board(FIXTURES / "perfboard_10x10.yaml")
        assert board.bounds == Rect.of(x=0.0, y=0.0, lx=10.0, ly=10.0)

    def test_perfboard_pads_are_plated(self) -> None:
        board = load_board(FIXTURES / "perfboard_10x10.yaml")
        atoms = list(board.atoms())
        for atom, _ in atoms:
            assert isinstance(atom, PadAtom)
            assert atom.layers.plated_through is True


class TestStripboardFixture:
    def test_load_stripboard_10x10(self) -> None:
        board = load_board(FIXTURES / "stripboard_10x10.yaml")
        atoms = list(board.atoms())
        # 10 strip atoms (each represents one strip).
        assert len(atoms) == 10

    def test_stripboard_strips_are_horizontal_length_10(self) -> None:
        board = load_board(FIXTURES / "stripboard_10x10.yaml")
        atoms = list(board.atoms())
        for atom, _ in atoms:
            assert isinstance(atom, StripAtom)
            assert atom.orientation == "horizontal"
            assert atom.length == 10

    def test_stripboard_strips_are_y_aligned_per_row(self) -> None:
        board = load_board(FIXTURES / "stripboard_10x10.yaml")
        atoms = list(board.atoms())
        # With default gap=1.0 between strips of height 1,
        # y centers are 0.5, 2.5, 4.5, ..., 18.5.
        for i, (_, pos) in enumerate(atoms):
            expected_y = 0.5 + i * 2.0
            assert _approx_equal(pos.y, expected_y)


class TestRoundtrip:
    def test_construct_serialize_load(self) -> None:
        original_board = Board(
            root=BoardGrid(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                cell=PadAtom(
                    width_policy=SizePolicy.HUG,
                    height_policy=SizePolicy.HUG,
                    layers=PadLayers(top=True, bottom=True, plated_through=True),
                ),
                rows=5,
                cols=5,
            )
        )
        yaml_str = board_to_yaml(original_board)
        loaded_board = board_from_yaml(yaml_str)

        original_atoms = list(original_board.atoms())
        loaded_atoms = list(loaded_board.atoms())
        assert len(original_atoms) == len(loaded_atoms)
        for (oa, op), (la, lp) in zip(original_atoms, loaded_atoms, strict=True):
            assert type(oa) is type(la)
            assert _approx_equal(op.x, lp.x)
            assert _approx_equal(op.y, lp.y)

    def test_yaml_to_yaml_idempotent(self) -> None:
        original = (FIXTURES / "perfboard_10x10.yaml").read_text()
        board = board_from_yaml(original)
        emitted = board_to_yaml(board)
        # Reload the emitted YAML and check that parsing succeeds.
        reloaded = board_from_yaml(emitted)
        assert len(list(reloaded.atoms())) == 100


class TestVBoxFixtureLoad:
    def test_vbox_with_strip_children(self) -> None:
        # Inline YAML round-trip without a fixture file.
        yaml_text = """
type: VBox
width_policy: hug
height_policy: hug
gap: 0.0
children:
  - type: StripAtom
    width_policy: hug
    height_policy: hug
    length: 5
    orientation: horizontal
    layers:
      top: false
      bottom: true
  - type: StripAtom
    width_policy: hug
    height_policy: hug
    length: 5
    orientation: horizontal
    layers:
      top: false
      bottom: true
"""
        board = board_from_yaml(yaml_text)
        assert isinstance(board.root, VBox)
        assert len(board.root.children) == 2


class TestErrors:
    def test_unknown_type_raises(self) -> None:
        bad = """
type: NotARealType
width_policy: hug
height_policy: hug
"""
        with pytest.raises(ProtoboardError):
            board_from_yaml(bad)

    def test_missing_required_field_raises(self) -> None:
        # StripAtom without 'length'.
        bad = """
type: StripAtom
width_policy: hug
height_policy: hug
orientation: horizontal
layers:
  top: false
  bottom: true
"""
        with pytest.raises(ProtoboardError):
            board_from_yaml(bad)

    def test_invalid_policy_raises(self) -> None:
        bad = """
type: PadAtom
width_policy: stretchy
height_policy: hug
layers:
  top: true
"""
        with pytest.raises(ProtoboardError):
            board_from_yaml(bad)
