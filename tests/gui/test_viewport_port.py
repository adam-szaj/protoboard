# SPDX-License-Identifier: MIT
"""ViewportPort: CameraMath model + the Qt-vs-fake mapping property test.

Two mapping implementations exist (QtViewport over a real QGraphicsView and
the pure CameraMath the headless fake uses) — the property test pins them to
each other on a set of transforms including mirrors, so they cannot drift
(qt-ports §2.1/§2.6).
"""

from __future__ import annotations

from protoboard.geometry import Point, Rect, Size, Transform2D, Vector
from protoboard.gui.board_view import BoardView
from protoboard.controller.ports.viewport_details import CameraMath
from protoboard.gui.qt_viewport import QtViewport


class TestCameraMathMapping:
    def test_round_trip(self) -> None:
        cam = CameraMath(Transform2D(60.0, 0.0, 0.0, 60.0), Vector(120.0, -30.0))
        p = Point(3.25, -1.5)
        v = cam.map_scene_to_view(p)
        back = cam.map_view_to_scene(v)
        assert abs(back.x - p.x) < 1e-12 and abs(back.y - p.y) < 1e-12

    def test_pan_shifts_visible_rect(self) -> None:
        cam = CameraMath(Transform2D(10.0, 0.0, 0.0, 10.0), Vector(0.0, 0.0),
                         Size(100.0, 100.0))
        before = cam.visible_scene_rect()
        cam.pan_by(Vector(50.0, 0.0))
        after = cam.visible_scene_rect()
        assert abs(after.x - (before.x + 5.0)) < 1e-12  # 50 px / scale 10
        assert after.lx == before.lx

    def test_center_on_puts_point_at_centre(self) -> None:
        cam = CameraMath(Transform2D(20.0, 0.0, 0.0, -20.0), Vector(0.0, 0.0),
                         Size(200.0, 100.0))
        cam.center_on(Point(7.0, 3.0))
        centre_scene = cam.map_view_to_scene(Point(100.0, 50.0))
        assert abs(centre_scene.x - 7.0) < 1e-12
        assert abs(centre_scene.y - 3.0) < 1e-12

    def test_fit_in_view_frames_rect(self) -> None:
        cam = CameraMath(Transform2D(-5.0, 0.0, 0.0, 5.0),  # mirrored-h
                         Vector(0.0, 0.0), Size(400.0, 200.0))
        cam.fit_in_view(Rect.of(0.0, 0.0, 10.0, 10.0))
        # Constrained axis: height (200 px / 10 units = 20 px/unit).
        assert abs(cam.view_scale() - 20.0) < 1e-12
        assert cam.transform.a < 0  # mirror sign preserved
        visible = cam.visible_scene_rect()
        assert visible.contains(Point(5.0, 5.0))

    def test_zoom_at_cursor_keeps_point_under_cursor(self) -> None:
        # The full deterministic camera model: zoom-at-pivot leaves the scene
        # point under the pivot unmoved (ViewCamera's correction math).
        cam = CameraMath(Transform2D(10.0, 0.0, 0.0, 10.0), Vector(30.0, 40.0),
                         Size(300.0, 300.0))
        pivot = Point(120.0, 80.0)
        before = cam.map_view_to_scene(pivot)
        factor = 1.331
        t = cam.transform
        cam.transform = Transform2D(
            t.a * factor, t.b * factor, t.c * factor, t.d * factor, t.tx, t.ty
        )
        after = cam.map_view_to_scene(pivot)
        scale = cam.transform.a
        cam.pan_by(Vector(-(after.x - before.x) * scale,
                          -(after.y - before.y) * scale))
        final = cam.map_view_to_scene(pivot)
        assert abs(final.x - before.x) < 1e-9
        assert abs(final.y - before.y) < 1e-9


class TestQtVsFakeProperty:
    """Pin QtViewport's mapping to the CameraMath model on many transforms."""

    _TRANSFORMS = [
        Transform2D(60.0, 0.0, 0.0, 60.0),
        Transform2D(-45.0, 0.0, 0.0, 45.0),    # mirror_h
        Transform2D(30.0, 0.0, 0.0, -30.0),    # mirror_v
        Transform2D(-12.5, 0.0, 0.0, -12.5),   # mirror_hv
        Transform2D(7.0, 0.0, 0.0, 7.0),
    ]
    # Deterministic pseudo-random view points (Date-free, seed-free).
    _POINTS = [
        Point(float((i * 73) % 311), float((i * 137) % 229)) for i in range(12)
    ]

    def test_mapping_matches_camera_math(self, board_view: BoardView) -> None:
        port = QtViewport(board_view)
        for t in self._TRANSFORMS:
            port.set_view_transform(t)
            # Sync the fake to Qt's actual state: same transform; scroll is
            # derived from where Qt maps view-origin (this absorbs Qt's
            # scene-rect-based scrollbar offset convention).
            fake = CameraMath(
                transform=port.view_transform(),
                viewport_size=Size(
                    port.viewport_rect().lx, port.viewport_rect().ly
                ),
            )
            scene0 = port.map_view_to_scene(Point(0.0, 0.0))
            mapped0 = fake.transform.apply(scene0)
            fake.scroll = Vector(mapped0.x, mapped0.y)
            for p in self._POINTS:
                qt_pt = port.map_view_to_scene(p)
                fake_pt = fake.map_view_to_scene(p)
                assert abs(qt_pt.x - fake_pt.x) < 1e-6, (t, p)
                assert abs(qt_pt.y - fake_pt.y) < 1e-6, (t, p)

    def test_pan_by_matches_camera_math(self, board_view: BoardView) -> None:
        port = QtViewport(board_view)
        # Build the scene first — Qt derives the scrollbar range from the
        # scene rect, and an empty scene pins the range (and pan_by) to 0.
        board_view.refresh()
        # Zoom far enough in that the content exceeds the viewport.
        scale = 600.0
        port.set_view_transform(Transform2D(scale, 0.0, 0.0, scale))
        hbar = board_view.horizontalScrollBar()
        hbar.setValue((hbar.minimum() + hbar.maximum()) // 2)
        vbar = board_view.verticalScrollBar()
        vbar.setValue((vbar.minimum() + vbar.maximum()) // 2)
        probe = Point(50.0, 50.0)
        before = port.map_view_to_scene(probe)
        port.pan_by(Vector(120.0, 60.0))
        after = port.map_view_to_scene(probe)
        # Panning content by +delta shifts what's under a fixed view point
        # by +delta/scale in scene space (CameraMath's model).
        assert abs((after.x - before.x) - 120.0 / scale) < 1e-6
        assert abs((after.y - before.y) - 60.0 / scale) < 1e-6
