# SPDX-License-Identifier: MIT
"""Tests for the BOM CSV exporter.

SVG / PDF / PNG export tests live in ``tests/gui/test_export_*`` because
the new pipeline routes through ``paint_snapshot`` → ``QGraphicsScene``
which needs a real ``QApplication`` (qtbot fixture).
"""

from __future__ import annotations

import csv
import io
import uuid
from pathlib import Path

from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import WireCollection
from protoboard.geometry import Point
from protoboard.services.export.bom import export_bom_csv


def _r_defn() -> ComponentDefinition:
    return ComponentDefinition(name="R", footprint=SIL(2), pin_names={1: "A", 2: "B"})


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
        lay = Layout(placements={r1.id: pl_r1, r2.id: pl_r2}, wires=WireCollection())
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
        lay = Layout(placements={}, wires=WireCollection())
        result = export_bom_csv(lay, components={}, instances={})
        reader = csv.DictReader(io.StringIO(result))
        rows = list(reader)
        assert rows == []
