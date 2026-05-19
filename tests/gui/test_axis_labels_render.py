# SPDX-License-Identifier: MIT
"""Tests for axis-label rendering on the BoardView."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QGraphicsTextItem
from pytestqt.qtbot import QtBot

from protoboard.domain.axis_labels_settings import (
    AxisLabelFormat,
    AxisLabelSettings,
)
from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.gui.board_view import BoardView
from protoboard.gui.layer_state import LayerVisibility
from protoboard.gui.session import EditorSession
from protoboard.services.library.loader import load_library


_LIBRARY_DIR = Path(__file__).parent.parent / "fixtures" / "library"


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


@pytest.fixture()
def view(qtbot: QtBot) -> tuple[BoardView, EditorSession]:
    library = load_library([_LIBRARY_DIR])
    sess = EditorSession(board=_perfboard(), library=library)
    bv = BoardView(sess)
    qtbot.addWidget(bv)
    return bv, sess


def _text_items(bv: BoardView) -> list[str]:
    return [
        it.toPlainText()
        for it in bv.scene().items()
        if isinstance(it, QGraphicsTextItem)
    ]


class TestAxisLabelsRender:
    def test_no_labels_when_disabled(self, view: tuple[BoardView, EditorSession]) -> None:
        bv, sess = view
        sess.axis_label_settings = AxisLabelSettings()
        bv.refresh()
        # An empty session has no component refdes labels either, so any text
        # at all would have to come from axis labels.
        assert _text_items(bv) == []

    def test_columns_numeric_zero(self, view: tuple[BoardView, EditorSession]) -> None:
        bv, sess = view
        sess.axis_label_settings = AxisLabelSettings(columns=AxisLabelFormat.NUMERIC_0)
        bv.refresh()
        texts = _text_items(bv)
        # 4 columns × 2 sides (top + bottom) → 8 labels.
        assert sorted(texts) == sorted(["0", "1", "2", "3"] * 2)

    def test_rows_alphabetic(self, view: tuple[BoardView, EditorSession]) -> None:
        bv, sess = view
        sess.axis_label_settings = AxisLabelSettings(rows=AxisLabelFormat.ALPHABETIC)
        bv.refresh()
        texts = _text_items(bv)
        # 4 rows × 2 sides (left + right) → 8 labels.
        assert sorted(texts) == sorted(["a", "b", "c", "d"] * 2)

    def test_both_axes(self, view: tuple[BoardView, EditorSession]) -> None:
        bv, sess = view
        sess.axis_label_settings = AxisLabelSettings(
            columns=AxisLabelFormat.NUMERIC_1,
            rows=AxisLabelFormat.ALPHABETIC,
        )
        bv.refresh()
        texts = _text_items(bv)
        # 4 cols × 2 + 4 rows × 2 = 16 labels.
        expected = ["1", "2", "3", "4"] * 2 + ["a", "b", "c", "d"] * 2
        assert sorted(texts) == sorted(expected)

    def test_hidden_layer_suppresses_labels(
        self, view: tuple[BoardView, EditorSession]
    ) -> None:
        bv, sess = view
        sess.axis_label_settings = AxisLabelSettings(columns=AxisLabelFormat.NUMERIC_0)
        sess.layer_overrides.axis_labels = LayerVisibility.HIDDEN
        bv._layer_overrides = sess.layer_overrides
        bv.refresh()
        assert _text_items(bv) == []

    def test_alphabetic_wraps_to_two_letters(
        self, qtbot: QtBot
    ) -> None:
        library = load_library([_LIBRARY_DIR])
        sess = EditorSession(board=_perfboard(rows=2, cols=30), library=library)
        sess.axis_label_settings = AxisLabelSettings(columns=AxisLabelFormat.ALPHABETIC)
        bv = BoardView(sess)
        qtbot.addWidget(bv)
        bv.refresh()
        texts = _text_items(bv)
        # 30 columns × 2 sides; entries 0..25 are a..z, 26..29 are aa..ad.
        expected_one_side = [
            "a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m",
            "n", "o", "p", "q", "r", "s", "t", "u", "v", "w", "x", "y", "z",
            "aa", "ab", "ac", "ad",
        ]
        assert sorted(texts) == sorted(expected_one_side * 2)
