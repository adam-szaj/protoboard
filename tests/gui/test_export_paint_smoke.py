# SPDX-License-Identifier: MIT
"""Smoke tests for build_export_scene / paint_snapshot pipeline."""

from __future__ import annotations

import uuid

import pytest
from pytestqt.qtbot import QtBot

from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QPainter

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pad_state import PadIndex
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.schematic.net import Net
from protoboard.domain.schematic.schematic import PinRef, Schematic
from protoboard.geometry import Point
from protoboard.services.export.paint import build_export_scene, paint_snapshot
from protoboard.services.export.snapshot import BoardSnapshot


def _empty_snapshot() -> BoardSnapshot:
    board = Board(root=BoardGrid(
        width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
        cell=PadAtom(width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
                     layers=PadLayers(top=True, bottom=True, plated_through=True)),
        rows=3, cols=3, row_gap=0, col_gap=0,
    ))
    lay = Layout()
    lay.ensure_root_group()
    sch = Schematic(nets={})
    idx = PadIndex(board, lay, sch, {}, {})
    return BoardSnapshot(
        board=board, layout=lay, components={}, instances={},
        schematic=sch, pad_index=idx, two_pin_body_families={},
    )


def _snapshot_with_resistor() -> BoardSnapshot:
    board = Board(root=BoardGrid(
        width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
        cell=PadAtom(width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
                     layers=PadLayers(top=True, bottom=True, plated_through=True)),
        rows=2, cols=4, row_gap=0, col_gap=0,
    ))
    defn = ComponentDefinition(name="R", footprint=SIL(2), pin_names={1: "A", 2: "B"})
    inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="")
    pl = ComponentPlacement(
        component_id=inst.id, position=Point(0.5, 0.5),
        rotation=90, side="top",
    )
    lay = Layout()
    lay.ensure_root_group()
    lay.placements[inst.id] = pl
    n1 = uuid.uuid4()
    sch = Schematic(nets={
        n1: Net(id=n1, name="N1", pins={PinRef(component_id=inst.id, pin_number=1)}),
    })
    idx = PadIndex(board, lay, sch, {"R": defn}, {inst.id: inst})
    return BoardSnapshot(
        board=board, layout=lay,
        components={"R": defn},
        instances={inst.id: inst},
        schematic=sch, pad_index=idx, two_pin_body_families={},
    )


class TestBuildExportScene:
    def test_empty_board_yields_scene(self, qtbot: QtBot) -> None:
        scene, rect = build_export_scene(_empty_snapshot())
        # Should have at least the board pads (9) + frame.
        items = scene.items()
        assert len(items) >= 9
        # Scene rect covers board + 1-raster margin → 5×5 for a 3×3 board.
        assert rect.width() == pytest.approx(5.0)
        assert rect.height() == pytest.approx(5.0)

    def test_resistor_adds_component_and_pin_pads(self, qtbot: QtBot) -> None:
        scene, _ = build_export_scene(_snapshot_with_resistor())
        # Component group + pin-pads (pin1 has SQUARE → disc + carve;
        # pin2 has CIRCLE → disc only; plated-through → both sides).
        # Plus board pads + ratsnest (single-pin net so no ratsnest line).
        # Just assert items > board-only.
        empty_scene, _ = build_export_scene(_empty_snapshot())
        assert len(scene.items()) > len(empty_scene.items())


class TestPaintSnapshot:
    def test_paints_to_qimage(self, qtbot: QtBot) -> None:
        img = QImage(100, 100, QImage.Format.Format_ARGB32)
        img.fill(0)
        painter = QPainter(img)
        try:
            paint_snapshot(painter, _snapshot_with_resistor(), QRectF(0, 0, 100, 100))
        finally:
            painter.end()
        # Verify at least some non-zero pixels exist (something was rendered).
        all_zero = True
        for y in range(100):
            for x in range(100):
                if img.pixel(x, y) != 0:
                    all_zero = False
                    break
            if not all_zero:
                break
        assert not all_zero, "Painter produced empty image"

    def test_same_snapshot_paints_deterministically(self, qtbot: QtBot) -> None:
        # Painting the *same* snapshot twice must produce identical output.
        snap = _snapshot_with_resistor()
        img1 = QImage(100, 100, QImage.Format.Format_ARGB32)
        img2 = QImage(100, 100, QImage.Format.Format_ARGB32)
        img1.fill(0)
        img2.fill(0)
        for img in (img1, img2):
            painter = QPainter(img)
            try:
                paint_snapshot(painter, snap, QRectF(0, 0, 100, 100))
            finally:
                painter.end()
        assert img1 == img2, "Same snapshot should paint identical images"


class TestSnapshotWithView:
    def test_with_view_overrides_camera(self, qtbot: QtBot) -> None:
        snap = _snapshot_with_resistor()
        bottom = snap.with_view(camera_side="bottom")
        assert bottom.camera_side == "bottom"
        # Original unchanged (snapshot is frozen).
        assert snap.camera_side == "top"

    def test_with_view_overrides_transform(self, qtbot: QtBot) -> None:
        snap = _snapshot_with_resistor()
        mirrored = snap.with_view(view_transform="mirror_h")
        assert mirrored.view_transform == "mirror_h"
        assert snap.view_transform == "normal"
