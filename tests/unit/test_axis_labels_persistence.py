# SPDX-License-Identifier: MIT
"""Round-trip and default-skip tests for axis_labels.yaml persistence."""

from __future__ import annotations

from pathlib import Path

from protoboard.domain.axis_labels_settings import (
    AxisLabelFormat,
    AxisLabelSettings,
)
from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.layout.layout import Layout
from protoboard.domain.schematic.schematic import Schematic
from protoboard.services.persistence.project_yaml import (
    load_project,
    save_project,
)


def _perfboard(rows: int = 4, cols: int = 4) -> Board:
    grid = BoardGrid(
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
    return Board(root=grid)


def _save(tmp_path: Path, settings: AxisLabelSettings | None) -> None:
    save_project(
        directory=tmp_path,
        board=_perfboard(),
        layout=Layout(placements={}, wires=[]),
        schematic=Schematic(nets={}),
        instances={},
        axis_label_settings=settings,
    )


class TestAxisLabelsPersistence:
    def test_default_settings_skip_file(self, tmp_path: Path) -> None:
        _save(tmp_path, AxisLabelSettings())  # both axes DISABLED
        assert not (tmp_path / "axis_labels.yaml").exists()

    def test_omitted_settings_skip_file(self, tmp_path: Path) -> None:
        _save(tmp_path, None)
        assert not (tmp_path / "axis_labels.yaml").exists()

    def test_columns_enabled_writes_file(self, tmp_path: Path) -> None:
        _save(tmp_path, AxisLabelSettings(columns=AxisLabelFormat.NUMERIC_0))
        assert (tmp_path / "axis_labels.yaml").exists()

    def test_roundtrip_both_axes(self, tmp_path: Path) -> None:
        original = AxisLabelSettings(
            columns=AxisLabelFormat.NUMERIC_1,
            rows=AxisLabelFormat.ALPHABETIC,
        )
        _save(tmp_path, original)
        (*_, loaded) = load_project(tmp_path)
        assert loaded == original

    def test_load_without_file_returns_default(self, tmp_path: Path) -> None:
        _save(tmp_path, AxisLabelSettings())  # writes everything else, no axis_labels.yaml
        (*_, loaded) = load_project(tmp_path)
        assert loaded == AxisLabelSettings()

    def test_default_after_enabled_deletes_file(self, tmp_path: Path) -> None:
        # First save with labels enabled — file appears.
        _save(tmp_path, AxisLabelSettings(rows=AxisLabelFormat.ALPHABETIC))
        assert (tmp_path / "axis_labels.yaml").exists()
        # Second save with defaults — file deleted.
        _save(tmp_path, AxisLabelSettings())
        assert not (tmp_path / "axis_labels.yaml").exists()

    def test_corrupt_file_falls_back_to_defaults(self, tmp_path: Path) -> None:
        _save(tmp_path, AxisLabelSettings(columns=AxisLabelFormat.NUMERIC_0))
        # Tamper with the file — invalid format string for the columns key.
        (tmp_path / "axis_labels.yaml").write_text("columns: bogus\nrows: alphabetic\n")
        (*_, loaded) = load_project(tmp_path)
        # Invalid value falls back to DISABLED for that axis; valid one survives.
        assert loaded.columns == AxisLabelFormat.DISABLED
        assert loaded.rows == AxisLabelFormat.ALPHABETIC
