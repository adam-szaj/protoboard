# SPDX-License-Identifier: MIT
"""BodyCanvas — the painter-style drawing API for body renderers/plugins."""

from __future__ import annotations

import pytest
from PySide6.QtGui import QColor, QPen, QTransform

from protoboard.controller.view_options.theme import DEFAULT_THEME
from protoboard.gui.renderers.packages import BodyCanvas


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
        assert not mixed.path().contains(_pt(0.02, 0.02))  # tl rounded
        assert mixed.path().contains(_pt(1.98, 1.98))  # br sharp


class TestCanvasUnits:
    def test_mm_units_scale_rect_inputs_to_holes(self, qtbot) -> None:
        pitch = DEFAULT_THEME.raster_pitch_mm
        c = _canvas(units="mm")
        item = c.addRect((0.0, 0.0), (2 * pitch, pitch))
        br = item.path().boundingRect()
        assert br.width() == pytest.approx(2.0)
        assert br.height() == pytest.approx(1.0)

    def test_mm_units_scale_radii_and_arcs(self, qtbot) -> None:
        pitch = DEFAULT_THEME.raster_pitch_mm
        c = _canvas(units="mm")
        circle = c.addCircle((0.0, 0.0), pitch)
        assert circle.path().boundingRect().width() == pytest.approx(2.0)
        arc = c.addArc((0.0, 0.0), pitch, 0.0, 360.0)
        assert arc.path().boundingRect().width() == pytest.approx(2.0)

    def test_mm_units_scale_corner_round(self, qtbot) -> None:
        pitch = DEFAULT_THEME.raster_pitch_mm
        c = _canvas(units="mm")
        rounded = c.addRect((0.0, 0.0), (2 * pitch, 2 * pitch), round=pitch)
        # A 1-hole corner radius on a 2×2-hole rect: the corner point is cut.
        assert not rounded.path().contains(_pt(0.05, 0.05))

    def test_mm_units_scale_paths(self, qtbot) -> None:
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QPainterPath

        pitch = DEFAULT_THEME.raster_pitch_mm
        path = QPainterPath()
        path.addRect(QRectF(0.0, 0.0, pitch, pitch))
        c = _canvas(units="mm")
        item = c.addPath(path)
        assert item.path().boundingRect().width() == pytest.approx(1.0)

    def test_pen_width_is_not_scaled(self, qtbot) -> None:
        holes = _canvas().addRect((0, 0), (1, 1), pen_width=0.2)
        mm = _canvas(units="mm").addRect((0, 0), (2.54, 2.54), pen_width=0.2)
        assert mm.pen().widthF() == holes.pen().widthF()

    def test_unknown_units_raise(self, qtbot) -> None:
        with pytest.raises(ValueError, match="units"):
            _canvas(units="inches")


def _pt(x: float, y: float):
    from PySide6.QtCore import QPointF

    return QPointF(x, y)
