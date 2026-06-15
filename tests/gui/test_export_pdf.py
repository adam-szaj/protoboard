# SPDX-License-Identifier: MIT
"""Tests for PDF + multi-panel template PDF exporters."""

from __future__ import annotations

from pathlib import Path

from pytestqt.qtbot import QtBot

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pad_state import PadIndex
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.layout.layout import Layout
from protoboard.domain.schematic.schematic import Schematic
from protoboard.services.export.pdf import export_pdf
from protoboard.services.export.pdf_template import export_template_pdf
from protoboard.services.export.snapshot import BoardSnapshot


def _small_board() -> Board:
    return Board(root=BoardGrid(
        width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
        cell=PadAtom(width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
                     layers=PadLayers(top=True, bottom=True, plated_through=True)),
        rows=3, cols=3, row_gap=0, col_gap=0,
    ))


def _snapshot() -> BoardSnapshot:
    board = _small_board()
    lay = Layout()
    lay.ensure_root_group()
    sch = Schematic(nets={})
    idx = PadIndex(board, lay, sch, {}, {})
    return BoardSnapshot(
        board=board, layout=lay,
        components={}, instances={},
        schematic=sch, pad_index=idx, package_families={},
    )


class TestExportPdf:
    def test_writes_pdf_file(self, qtbot: QtBot, tmp_path: Path) -> None:
        board = _small_board()
        lay = Layout()
        lay.ensure_root_group()
        out = tmp_path / "board.pdf"
        export_pdf(out, board, lay, components={}, instances={})
        assert out.exists()
        assert out.stat().st_size > 100  # PDF has at least header + xref


class TestExportTemplatePdf:
    def test_writes_template_pdf(self, qtbot: QtBot, tmp_path: Path) -> None:
        out = tmp_path / "template.pdf"
        export_template_pdf(out, _snapshot())
        assert out.exists()
        # 4 panels = larger than single-page PDF, sanity-check size.
        assert out.stat().st_size > 500
