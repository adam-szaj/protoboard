# SPDX-License-Identifier: MIT
"""Tests for SVG and BOM CSV exporters."""

from __future__ import annotations

import csv
import io
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import WireSegment
from protoboard.geometry import Point
from protoboard.services.export.bom import export_bom_csv
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
        rows=rows,
        cols=cols,
        row_gap=0.0,
        col_gap=0.0,
    )
    return Board(root=grid)


def _r_defn() -> ComponentDefinition:
    return ComponentDefinition(name="R", footprint=SIL(2), pin_names={1: "A", 2: "B"})


# ---------------------------------------------------------------------------
# SVG exporter
# ---------------------------------------------------------------------------


class TestSvgExport:
    def test_returns_string(self) -> None:
        board = _perfboard(3, 3)
        lay = Layout(placements={}, wires=[], cuts=[])
        result = export_svg(board, lay, components={}, instances={})
        assert isinstance(result, str)

    def test_is_valid_xml(self) -> None:
        board = _perfboard(3, 3)
        lay = Layout(placements={}, wires=[], cuts=[])
        result = export_svg(board, lay, components={}, instances={})
        ET.fromstring(result)  # must not raise

    def test_svg_root_element(self) -> None:
        board = _perfboard(3, 3)
        lay = Layout(placements={}, wires=[], cuts=[])
        result = export_svg(board, lay, components={}, instances={})
        root = ET.fromstring(result)
        assert root.tag.endswith("svg") or root.tag == "svg"

    def test_contains_pads(self) -> None:
        board = _perfboard(2, 2)
        lay = Layout(placements={}, wires=[], cuts=[])
        result = export_svg(board, lay, components={}, instances={})
        # 4 pads → at least 4 circle elements
        root = ET.fromstring(result)
        ns = {"svg": "http://www.w3.org/2000/svg"}
        circles = root.findall(".//svg:circle", ns) or root.findall(".//circle")
        assert len(circles) >= 4

    def test_contains_wire_lines(self) -> None:
        board = _perfboard(1, 3)
        wire = WireSegment(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(1.5, 0.5), Point(2.5, 0.5)],
            insulated=True,
            side="top",
        )
        lay = Layout(placements={}, wires=[wire], cuts=[])
        result = export_svg(board, lay, components={}, instances={})
        assert "<line" in result or "<polyline" in result or "<path" in result

    def test_contains_component_label(self) -> None:
        board = _perfboard(2, 4)
        defn = _r_defn()
        inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        pl = ComponentPlacement(
            component_id=inst.id, position=Point(0.5, 0.5), rotation=90, side="top"
        )
        lay = Layout(placements={inst.id: pl}, wires=[], cuts=[])
        result = export_svg(
            board, lay, components={defn.name: defn}, instances={inst.id: inst}
        )
        assert "R1" in result

    def test_write_to_file(self, tmp_path: Path) -> None:
        board = _perfboard(2, 2)
        lay = Layout(placements={}, wires=[], cuts=[])
        out = tmp_path / "board.svg"
        svg_str = export_svg(board, lay, components={}, instances={})
        out.write_text(svg_str, encoding="utf-8")
        assert out.exists()
        assert out.stat().st_size > 0

    def test_viewbox_covers_board(self) -> None:
        board = _perfboard(5, 10)
        lay = Layout(placements={}, wires=[], cuts=[])
        result = export_svg(board, lay, components={}, instances={})
        # Board is 10 wide × 5 tall in raster units — viewBox must cover that.
        assert "viewBox" in result
        root = ET.fromstring(result)
        vb = root.get("viewBox", "")
        parts = vb.split()
        assert len(parts) == 4
        width = float(parts[2])
        height = float(parts[3])
        assert width >= 10.0
        assert height >= 5.0


# ---------------------------------------------------------------------------
# BOM CSV exporter
# ---------------------------------------------------------------------------


class TestBomExport:
    def _layout_with_two_resistors(
        self,
    ) -> tuple[
        Layout, dict[str, ComponentDefinition], dict[uuid.UUID, ComponentInstance]
    ]:
        defn = _r_defn()
        r1 = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        r2 = ComponentInstance.new(definition_ref="R", refdes="R2", value="4k7")
        pl_r1 = ComponentPlacement(
            component_id=r1.id, position=Point(0.5, 0.5), rotation=90, side="top"
        )
        pl_r2 = ComponentPlacement(
            component_id=r2.id, position=Point(2.5, 0.5), rotation=90, side="top"
        )
        lay = Layout(placements={r1.id: pl_r1, r2.id: pl_r2}, wires=[], cuts=[])
        return lay, {defn.name: defn}, {r1.id: r1, r2.id: r2}

    def test_returns_string(self) -> None:
        lay, comps, insts = self._layout_with_two_resistors()
        result = export_bom_csv(lay, components=comps, instances=insts)
        assert isinstance(result, str)

    def test_csv_has_header(self) -> None:
        lay, comps, insts = self._layout_with_two_resistors()
        result = export_bom_csv(lay, components=comps, instances=insts)
        reader = csv.DictReader(io.StringIO(result))
        assert reader.fieldnames is not None
        fieldnames = [f.lower() for f in reader.fieldnames]
        assert "refdes" in fieldnames
        assert "value" in fieldnames

    def test_csv_has_two_rows(self) -> None:
        lay, comps, insts = self._layout_with_two_resistors()
        result = export_bom_csv(lay, components=comps, instances=insts)
        reader = csv.DictReader(io.StringIO(result))
        rows = list(reader)
        assert len(rows) == 2

    def test_csv_refdes_values(self) -> None:
        lay, comps, insts = self._layout_with_two_resistors()
        result = export_bom_csv(lay, components=comps, instances=insts)
        reader = csv.DictReader(io.StringIO(result))
        refdeses = {r["RefDes"] for r in reader}
        assert refdeses == {"R1", "R2"}

    def test_csv_component_values(self) -> None:
        lay, comps, insts = self._layout_with_two_resistors()
        result = export_bom_csv(lay, components=comps, instances=insts)
        reader = csv.DictReader(io.StringIO(result))
        values = {r["Value"] for r in reader}
        assert "10k" in values
        assert "4k7" in values

    def test_csv_sorted_by_refdes(self) -> None:
        lay, comps, insts = self._layout_with_two_resistors()
        result = export_bom_csv(lay, components=comps, instances=insts)
        reader = csv.DictReader(io.StringIO(result))
        refdeses = [r["RefDes"] for r in reader]
        assert refdeses == sorted(refdeses)

    def test_write_to_file(self, tmp_path: Path) -> None:
        lay, comps, insts = self._layout_with_two_resistors()
        out = tmp_path / "bom.csv"
        result = export_bom_csv(lay, components=comps, instances=insts)
        out.write_text(result, encoding="utf-8")
        assert out.exists()

    def test_empty_layout_gives_header_only(self) -> None:
        lay = Layout(placements={}, wires=[], cuts=[])
        result = export_bom_csv(lay, components={}, instances={})
        reader = csv.DictReader(io.StringIO(result))
        rows = list(reader)
        assert rows == []
