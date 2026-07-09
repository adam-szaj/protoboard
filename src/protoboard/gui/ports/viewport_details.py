# SPDX-License-Identifier: MIT
"""``CameraMath`` — the viewport mapping as one pure concept (qt-ports §2.6).

The transform + scroll + viewport-size triple is one concept, historically
smeared across ``mapToScene`` / scrollbar / ``viewport().rect()`` call
sites.  This class *is* the math: the headless viewport fake delegates its
whole mapping here, and the Qt-vs-fake property test pins the Qt adapter to
it, so the two implementations cannot drift.

Convention (matches ``QGraphicsView``): a scene point maps to viewport
pixels as ``view = T·scene − scroll``; the inverse is
``scene = T⁻¹·(view + scroll)``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from protoboard.geometry import Point, Rect, Size, Transform2D, Vector

if TYPE_CHECKING:
    pass


class CameraMath:
    """Pure affine camera model: transform, scroll offset, viewport size."""

    def __init__(
        self,
        transform: Transform2D | None = None,
        scroll: Vector = Vector(0.0, 0.0),
        viewport_size: Size = Size(640.0, 480.0),
    ) -> None:
        self.transform = transform if transform is not None else Transform2D()
        self.scroll = scroll
        self.viewport_size = viewport_size

    # -- mapping -------------------------------------------------------------

    def map_view_to_scene(self, view_pt: Point) -> Point:
        shifted = Point(view_pt.x + self.scroll.dx, view_pt.y + self.scroll.dy)
        return self.transform.inverse().apply(shifted)

    def map_scene_to_view(self, scene_pt: Point) -> Point:
        mapped = self.transform.apply(scene_pt)
        return Point(mapped.x - self.scroll.dx, mapped.y - self.scroll.dy)

    def view_scale(self) -> float:
        return abs(self.transform.a)

    # -- framing ---------------------------------------------------------------

    def visible_scene_rect(self) -> Rect:
        """Bounding rect of the viewport corners mapped to scene space.

        Mirror transforms flip corner order, so normalise with min/max.
        """
        w, h = self.viewport_size.width, self.viewport_size.height
        corners = [
            self.map_view_to_scene(Point(0.0, 0.0)),
            self.map_view_to_scene(Point(w, 0.0)),
            self.map_view_to_scene(Point(0.0, h)),
            self.map_view_to_scene(Point(w, h)),
        ]
        xs = [c.x for c in corners]
        ys = [c.y for c in corners]
        return Rect(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))

    def pan_by(self, view_delta: Vector) -> None:
        self.scroll = Vector(
            self.scroll.dx + view_delta.dx, self.scroll.dy + view_delta.dy
        )

    def center_on(self, scene_pt: Point) -> None:
        """Set scroll so *scene_pt* lands in the viewport centre."""
        mapped = self.transform.apply(scene_pt)
        self.scroll = Vector(
            mapped.x - self.viewport_size.width / 2.0,
            mapped.y - self.viewport_size.height / 2.0,
        )

    def fit_in_view(self, scene_rect: Rect) -> None:
        """Frame *scene_rect* keeping aspect ratio and mirror signs."""
        w = max(scene_rect.width, 1e-9)
        h = max(scene_rect.height, 1e-9)
        mag = min(self.viewport_size.width / w, self.viewport_size.height / h)
        sx = -1.0 if self.transform.a < 0 else 1.0
        sy = -1.0 if self.transform.d < 0 else 1.0
        self.transform = Transform2D(sx * mag, 0.0, 0.0, sy * mag, 0.0, 0.0)
        self.center_on(scene_rect.center)
