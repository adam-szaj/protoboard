# SPDX-License-Identifier: MIT
"""BodyCanvas — the painter-style drawing API for body renderers/plugins."""

from __future__ import annotations

from PySide6.QtGui import QColor, QPen, QTransform

from protoboard.gui.renderers.packages import BodyCanvas
from protoboard.gui.theme import DEFAULT_THEME


def _canvas(**kw) -> BodyCanvas:
    kw.setdefault("fill", "#aabbcc")
    kw.setdefault("border", "#112233")
    return BodyCanvas(DEFAULT_THEME, **kw)


class TestCanvasState:
    def test_defaults_apply_to_items(self, qtbot) -> None:
        import pytest
        c = _canvas(alpha=0.5, z=3.0)
        item = c.addRect((0, 0), (2, 1))
        assert item.zValue() == 3.0
        # QColor quantizes alpha to 8 bits.
        assert item.brush().color().alphaF() == pytest.approx(0.5, abs=1 / 255)
        assert c.items == [item]

    def test_setters_change_subsequent_items_only(self, qtbot) -> None:
        c = _canvas()
        first = c.addRect((0, 0), (1, 1))
        c.setFillColor("#ff0000")
        c.setBorderColor("#00ff00")
        second = c.addRect((0, 0), (1, 1))
        assert first.brush().color().name() == "#aabbcc"
        assert second.brush().color().name() == "#ff0000"
        assert second.pen().color().name() == "#00ff00"

    def test_inline_kwargs_override_one_item(self, qtbot) -> None:
        c = _canvas()
        item = c.addRect((0, 0), (1, 1), fill="#ff0000", border="#00ff00")
        after = c.addRect((0, 0), (1, 1))
        assert item.brush().color().name() == "#ff0000"
        assert item.pen().color().name() == "#00ff00"
        assert after.brush().color().name() == "#aabbcc"  # state untouched

    def test_set_pen_and_reset(self, qtbot) -> None:
        c = _canvas()
        pen = QPen(QColor("#123456"))
        c.setPen(pen)
        assert c.addRect((0, 0), (1, 1)).pen().color().name() == "#123456"
        c.setPen(None)
        assert c.addRect((0, 0), (1, 1)).pen().color().name() == "#112233"

    def test_mm_converts_by_raster_pitch(self, qtbot) -> None:
        c = _canvas()
        assert c.mm(DEFAULT_THEME.raster_pitch_mm) == 1.0


class TestCanvasZ:
    def test_z_above_and_below_step_around_drawn_items(self, qtbot) -> None:
        c = _canvas(z=2.0)
        assert c.zAbove() == 2.0  # nothing drawn yet — the base z
        c.addRect((0, 0), (1, 1))
        assert c.zAbove() == 2.0 + BodyCanvas.Z_STEP
        assert c.zBelow() == 2.0 - BodyCanvas.Z_STEP

    def test_track_includes_external_items(self, qtbot) -> None:
        base = _canvas(z=1.0)
        base.addRect((0, 0), (1, 1), z=1.5)
        c = _canvas(z=1.0)
        c.track(base.items)
        assert c.zAbove() == 1.5 + BodyCanvas.Z_STEP

    def test_absolute_z_kwarg(self, qtbot) -> None:
        c = _canvas(z=1.0)
        item = c.addRect((0, 0), (1, 1), z=7.25)
        assert item.zValue() == 7.25


class TestCanvasFrames:
    def test_default_mode_sets_item_transform(self, qtbot) -> None:
        t = QTransform()
        t.translate(3.0, 4.0)
        c = _canvas(transform=t)
        item = c.addRect((0, 0), (2, 2))
        assert item.transform().dx() == 3.0
        assert item.sceneBoundingRect().center().x() == 4.0  # 1 + 3

    def test_bake_mode_maps_path_identity_item_transform(self, qtbot) -> None:
        t = QTransform()
        t.translate(3.0, 4.0)
        c = _canvas(transform=t, bake=True)
        item = c.addRect((0, 0), (2, 2))
        assert item.transform().isIdentity()
        assert item.path().boundingRect().center().x() == 4.0


class TestCanvasShapes:
    def test_circle_and_ellipse_extents(self, qtbot) -> None:
        c = _canvas()
        circle = c.addCircle((1.0, 1.0), 0.5)
        ellipse = c.addEllipse((0.0, 0.0), 2.0, 1.0)
        assert circle.path().boundingRect().width() == 1.0
        assert ellipse.path().boundingRect().width() == 4.0
        assert ellipse.path().boundingRect().height() == 2.0

    def test_arc_is_stroke_only(self, qtbot) -> None:
        c = _canvas()
        arc = c.addArc((0.0, 0.0), 1.0, 0.0, 90.0)
        assert arc.brush().style().name == "NoBrush"

    def test_rect_round_uniform_and_per_corner(self, qtbot) -> None:
        c = _canvas()
        sharp = c.addRect((0, 0), (2, 2))
        uniform = c.addRect((0, 0), (2, 2), round=0.5)
        mixed = c.addRect((0, 0), (2, 2), round=(0.5, 0.0, 0.0, 0.0))
        # A rounded corner pulls the path off the sharp corner point.
        assert sharp.path().contains(_pt(0.02, 0.02))
        assert not uniform.path().contains(_pt(0.02, 0.02))
        assert not mixed.path().contains(_pt(0.02, 0.02))    # tl rounded
        assert mixed.path().contains(_pt(1.98, 1.98))        # br sharp


def _pt(x: float, y: float):
    from PySide6.QtCore import QPointF
    return QPointF(x, y)
