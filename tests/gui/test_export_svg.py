# SPDX-License-Identifier: MIT
"""Tests for the SVG board exporter (paint_snapshot pipeline)."""

from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import Wire, WireCollection
from protoboard.geometry import Point
from protoboard.services.export.svg import export_svg


def _perfboard(rows: int, cols: int) -> Board:
    grid = BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=rows, cols=cols,
        row_gap=0.0, col_gap=0.0,
    )
    return Board(root=grid)


def _r_defn() -> ComponentDefinition:
    return ComponentDefinition(name="R", footprint=SIL(2), pin_names={1: "A", 2: "B"})


class TestSvgExport:
    def test_returns_string(self, qtbot: QtBot) -> None:
        board = _perfboard(3, 3)
        lay = Layout(placements={}, wires=WireCollection([]))
        result = export_svg(board, lay, components={}, instances={})
        assert isinstance(result, str)
        assert result, "SVG export must produce non-empty output"

    def test_is_valid_xml(self, qtbot: QtBot) -> None:
        board = _perfboard(3, 3)
        lay = Layout(placements={}, wires=WireCollection([]))
        result = export_svg(board, lay, components={}, instances={})
        ET.fromstring(result)  # must not raise

    def test_svg_root_element(self, qtbot: QtBot) -> None:
        board = _perfboard(3, 3)
        lay = Layout(placements={}, wires=WireCollection([]))
        result = export_svg(board, lay, components={}, instances={})
        root = ET.fromstring(result)
        assert root.tag.endswith("svg") or root.tag == "svg"

    def test_viewbox_covers_board(self, qtbot: QtBot) -> None:
        board = _perfboard(5, 10)
        lay = Layout(placements={}, wires=WireCollection([]))
        result = export_svg(board, lay, components={}, instances={})
        root = ET.fromstring(result)
        vb = root.get("viewBox", "")
        parts = vb.split()
        assert len(parts) == 4
        width = float(parts[2])
        height = float(parts[3])
        # Board (10 × 5) + 1-raster margin → at least 12 × 7 user units.
        # Default scale 10 → at least 120 × 70.
        assert width >= 120.0
        assert height >= 70.0

    def test_write_to_file(self, qtbot: QtBot, tmp_path: Path) -> None:
        board = _perfboard(2, 2)
        lay = Layout(placements={}, wires=WireCollection([]))
        out = tmp_path / "board.svg"
        svg_str = export_svg(board, lay, components={}, instances={})
        out.write_text(svg_str, encoding="utf-8")
        assert out.exists()
        assert out.stat().st_size > 0

    def test_with_component_renders(self, qtbot: QtBot) -> None:
        # Smoke test: SVG generation completes without error when there's
        # a placed component.  Exact content depends on QSvgGenerator output
        # which we don't pin in tests; we just confirm the painter ran.
        board = _perfboard(2, 4)
        defn = _r_defn()
        inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        pl = ComponentPlacement(
            component_id=inst.id, position=Point(0.5, 0.5), rotation=90, side="top"
        )
        lay = Layout(placements={inst.id: pl}, wires=WireCollection([]))
        result = export_svg(
            board, lay, components={defn.name: defn}, instances={inst.id: inst}
        )
        ET.fromstring(result)
        assert "<svg" in result

    def test_with_wire_renders(self, qtbot: QtBot) -> None:
        board = _perfboard(1, 3)
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(1.5, 0.5), Point(2.5, 0.5)],
            insulated=True, side="top",
        )
        lay = Layout(placements={}, wires=WireCollection([wire]))
        result = export_svg(board, lay, components={}, instances={})
        ET.fromstring(result)
        assert "<svg" in result
