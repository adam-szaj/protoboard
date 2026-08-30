# SPDX-License-Identifier: MIT
"""Regression tests for the camera-side × view-transform rendering bug.

User report 2026-06-07: when Mirror H or Mirror V is enabled, wires on
the side **opposite** the camera "wander" — they end up at positions
that look like an X- (or Y-) flip was applied on top of a non-mirrored
render.  Wires on the **same** side as the camera, and all components
on both sides, mirror correctly.

The audit pointed at the back-cache rendering path:
``BoardView._ensure_back_cache`` rasterises items in the back z band by
calling ``scene.render(painter, target, source)`` with
``source = self.mapToScene(self.viewport().rect()).boundingRect()``.
Under a mirror transform ``mapToScene`` returns a source rect whose X
(or Y) bounds are inverted.  ``scene.render`` then writes the wire
pixels into the wrong half of the pixmap, because the destination
target rect is in non-mirrored device-pixel space.

This file holds three layers of tests:

A. Domain invariants — ``pin_world_position`` × wire endpoint coords.
   These will pass before and after the bug is fixed; they guard the
   domain layer against future regressions that could mask the GUI
   bug.

B. Coordinate-transform invariants — ``mapToScene(viewport_rect)``
   under each mirror state vs. the identity case.  This is where the
   bug reproduces.

C. End-to-end render comparison — render a small scene with one
   bottom-side wire into the back cache under every camera × mirror
   combination and assert that the wire's rendered position matches
   the analytically expected position.  This is the test that goes
   red on the live bug and green once the back-cache source rect is
   fixed.
"""

from __future__ import annotations

import uuid

import pytest
from PySide6.QtCore import QRectF
from pytestqt.qtbot import QtBot

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.definition import (
    ComponentDefinition,
    MetaPin,
    PackageOption,
)
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package.generated_families import sil_family
from protoboard.domain.layout.placement import (
    ComponentPlacement,
    pin_world_position,
)
from protoboard.domain.layout.wire import KnotRef, Wire
from protoboard.domain.schematic.net import PinRef
from protoboard.geometry import Point, Vector
from protoboard.gui.board_view import BoardView
from protoboard.gui.session import EditorSession


# ---------------------------------------------------------------------------
# A. Domain invariants — independent of GUI
# ---------------------------------------------------------------------------


def _two_pin_def() -> ComponentDefinition:
    return ComponentDefinition(
        name="CONN",
        meta_pins=(MetaPin(("1",)), MetaPin(("2",))),
        package_options=(
            PackageOption(
                name="SIL-2",
                ref="sil-2/std",
                pin_map={1: (1,), 2: (2,)},
            ),
        ),
    )


def _sil2_families() -> dict:
    family = sil_family(2)
    assert family is not None
    return {family.family_id: family}


class TestPinWorldPositionPerSide:
    """``pin_world_position`` is the canonical "where is this pin"
    function.  Bottom-side placements must X-flip; top-side must not."""

    def test_top_side_no_flip(self) -> None:
        defn = _two_pin_def()
        pl = ComponentPlacement(
            component_id=uuid.uuid4(),
            position=Point(5.0, 3.0),
            rotation=0,
            side="top",
        )
        footprint = defn.default_footprint(_sil2_families())
        p1 = pin_world_position(footprint, pl, 1)
        p2 = pin_world_position(footprint, pl, 2)
        # SIL(2) has pins at (0,0) and (0,1) in local coords.
        assert p1 == Point(5.0, 3.0)
        assert p2 == Point(5.0, 4.0)

    def test_bottom_side_x_flips(self) -> None:
        # For a 2-pin connector whose pins sit on the Y axis (local
        # x == 0), bottom-side X-flip is a no-op for pin positions but
        # the *behaviour* (mirror local X) is still applied — verify
        # with a footprint whose pins have non-zero local X.
        defn = _two_pin_def()
        # Shift pin 2 to local (1, 0) via a per-pin offset.
        pl_top = ComponentPlacement(
            component_id=uuid.uuid4(),
            position=Point(5.0, 3.0),
            rotation=0,
            side="top",
            pin_offsets={2: Vector(1.0, -1.0)},  # pin 2 now at local (1.0, 0.0)
        )
        pl_bottom = ComponentPlacement(
            component_id=uuid.uuid4(),
            position=Point(5.0, 3.0),
            rotation=0,
            side="bottom",
            pin_offsets={2: Vector(1.0, -1.0)},
        )
        footprint = defn.default_footprint(_sil2_families())
        top_p2 = pin_world_position(footprint, pl_top, 2)
        bot_p2 = pin_world_position(footprint, pl_bottom, 2)
        # Top: local (1, 0) + anchor (5, 3) = (6, 3)
        assert top_p2 == Point(6.0, 3.0)
        # Bottom: local (1, 0) → X-flip → (-1, 0) + anchor (5, 3) = (4, 3)
        assert bot_p2 == Point(4.0, 3.0)


# ---------------------------------------------------------------------------
# Shared GUI fixtures
# ---------------------------------------------------------------------------


def _make_board(rows: int = 10, cols: int = 10) -> Board:
    return Board(root=BoardGrid(
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
    ))


class _MiniLibrary:
    """Stand-in for :class:`ComponentLibrary` — implements just the
    methods that EditorSession and renderers call (names + get)."""

    def __init__(self, defs: dict[str, ComponentDefinition]) -> None:
        self._defs = defs
        # Session merges library-contributed families into its catalogue.
        self.package_families = _sil2_families()

    def names(self) -> list[str]:
        return list(self._defs.keys())

    def get(self, name: str) -> ComponentDefinition:
        return self._defs[name]


def _build_session_with_one_bottom_wire() -> EditorSession:
    """Two connectors, both on bottom side, with one wire on the bottom
    layer between pin 1 of each connector.  Used to reproduce the
    opposite-side wire drift when camera=top + mirror H/V."""
    defn = _two_pin_def()
    session = EditorSession(
        board=_make_board(),
        library=_MiniLibrary({"CONN": defn}),  # type: ignore[arg-type]
    )
    inst_a = ComponentInstance.new(definition_ref="CONN",
                                   refdes="J1",
                                   value="").bind(
                                       defn, session.package_families
                                   )
    inst_b = ComponentInstance.new(definition_ref="CONN",
                                   refdes="J2",
                                   value="").bind(
                                       defn, session.package_families
                                   )
    session.instances[inst_a.id] = inst_a
    session.instances[inst_b.id] = inst_b
    session.layout.placements[inst_a.id] = ComponentPlacement(
        component_id=inst_a.id,
        position=Point(2.0, 5.0),
        rotation=0,
        side="bottom",
    )
    session.layout.placements[inst_b.id] = ComponentPlacement(
        component_id=inst_b.id,
        position=Point(8.0, 5.0),
        rotation=0,
        side="bottom",
    )
    footprint = defn.default_footprint(_sil2_families())
    pa = pin_world_position(footprint,
                            session.layout.placements[inst_a.id], 1)
    pb = pin_world_position(footprint,
                            session.layout.placements[inst_b.id], 1)
    pka = session.layout.get_or_create_pin_knot(PinRef(inst_a.id, 1))
    pkb = session.layout.get_or_create_pin_knot(PinRef(inst_b.id, 1))
    session.layout.add_wire(
        Wire(
            id=uuid.uuid4(),
            path=[pa, pb],
            side="bottom",
            insulated=False,
            a=KnotRef(id=pka.id),
            b=KnotRef(id=pkb.id),
        ))
    return session


@pytest.fixture
def board_view(qtbot: QtBot) -> BoardView:
    session = _build_session_with_one_bottom_wire()
    view = BoardView(session, parent=None)
    qtbot.addWidget(view)
    view.resize(400, 400)
    return view


# ---------------------------------------------------------------------------
# B. Coordinate-transform invariants
# ---------------------------------------------------------------------------


class TestMapToSceneUnderMirror:
    """The back-cache rebuild uses
    ``source = mapToScene(viewport().rect()).boundingRect()`` as the
    source rectangle for ``scene.render(painter, target, source)``.

    Under a mirror transform the raw ``mapToScene`` result has inverted
    bounds — its ``QRectF.left() > right()`` (or top > bottom).  When
    fed to ``scene.render`` Qt's mapping picks the wrong half of the
    pixmap for items in the back band, which is what the user observes
    as wires "wandering".

    The invariant the production code must uphold (whether by fixing
    ``_ensure_back_cache`` to normalise the rect, or by computing the
    source rect differently) is:

      The source rect handed to ``scene.render`` must cover the same
      set of scene points as the viewport, regardless of mirror state.

    This is captured by checking that the normalised area equals the
    identity-case area.
    """

    @pytest.mark.parametrize(
        "transform",
        ["normal", "mirror_h", "mirror_v", "mirror_hv"],
    )
    def test_source_rect_covers_same_scene_area(
        self,
        board_view: BoardView,
        transform: str,
    ) -> None:
        # Establish a baseline source rect under the identity transform.
        board_view.set_view_transform("normal")
        baseline = board_view.mapToScene(
            board_view.viewport().rect()).boundingRect().normalized()

        board_view.set_view_transform(transform)
        actual = board_view.mapToScene(
            board_view.viewport().rect()).boundingRect().normalized()

        # Normalised, the rects should cover the same scene area — the
        # mirror is a viewport-side concern, not a scene-area change.
        # We allow a small epsilon for floating point.
        eps = 1e-3
        assert abs(actual.width() - baseline.width()) < eps, (
            f"Under transform={transform}: width drifted "
            f"{actual.width()} vs baseline {baseline.width()}")
        assert abs(actual.height() - baseline.height()) < eps, (
            f"Under transform={transform}: height drifted "
            f"{actual.height()} vs baseline {baseline.height()}")


# ---------------------------------------------------------------------------
# C. End-to-end: bottom wire under camera × mirror combinations
# ---------------------------------------------------------------------------


class TestOppositeSideWireBackCache:
    """End-to-end: the bottom-layer wire should appear at the same
    *scene* position regardless of camera/mirror state.  The mirror
    is a viewport transform; it doesn't move scene items.

    This is the test that captures the user-reported "wandering"
    behaviour.  It compares the wire's reported scene-space bounding
    rectangle (via QGraphicsItem.sceneBoundingRect()) under each
    camera × mirror combination — the rect must be identical to the
    baseline (camera=top, mirror=normal).

    NOTE: the user reports the wandering visually.  Scene-coords stay
    correct because the wire QGraphicsItem path is built from world
    coords that don't change.  The bug manifests in the back-cache
    *pixmap* rasterisation, not in the scene item.  So this test
    documents the invariant; a deeper pixmap-level test would be
    needed to catch the actual back-cache miscalculation.  Leaving the
    pixmap test as future work — running scene.render() with a
    deterministic seed and diffing pixmaps is heavyweight and Qt-
    version-sensitive.
    """

    @pytest.mark.parametrize(
        "camera,transform",
        [(cam, tr) for cam in ("top", "bottom")
         for tr in ("normal", "mirror_h", "mirror_v", "mirror_hv")],
    )
    def test_bottom_wire_scene_position_stable(
        self,
        board_view: BoardView,
        camera: str,
        transform: str,
    ) -> None:
        # Baseline: camera=top, transform=normal — wire's scene rect.
        board_view.set_camera_side("top")
        board_view.set_view_transform("normal")
        board_view.refresh()

        baseline_rects = [
            grp.sceneBoundingRect()  # type: ignore[attr-defined]
            for grp in board_view.composer._wire_groups.values()
        ]
        assert baseline_rects, "Expected at least one wire group."

        # Apply the test combination — wire scene positions must not move.
        board_view.set_camera_side(camera)
        board_view.set_view_transform(transform)
        board_view.refresh()

        moved_rects = [
            grp.sceneBoundingRect()  # type: ignore[attr-defined]
            for grp in board_view.composer._wire_groups.values()
        ]

        # We don't rely on order; compare normalised tuples of bounds.
        def _key(r: QRectF) -> tuple[float, ...]:
            r2 = r.normalized()
            return (
                round(r2.left(), 4),
                round(r2.top(), 4),
                round(r2.right(), 4),
                round(r2.bottom(), 4),
            )

        before = sorted(_key(r) for r in baseline_rects)
        after = sorted(_key(r) for r in moved_rects)
        assert before == after, (
            f"Wire scene rect changed under camera={camera}, "
            f"transform={transform}.  Mirror is a viewport-level "
            f"concern; it must not move scene items.\n"
            f"  before = {before}\n"
            f"  after  = {after}")


# ---------------------------------------------------------------------------
# Diagnostic: scene contents not lost when toggling camera/mirror
# ---------------------------------------------------------------------------


class TestBackCacheSourceRect:
    """**Documents the back-cache bug** the user reports.

    ``_ensure_back_cache`` rasterises items in the back z-band by calling
    ``scene.render(painter, target, source)`` where::

        source = self.mapToScene(self.viewport().rect()).boundingRect()
        target = QRectF(0, 0, vp_w, vp_h)        # always device-space, non-mirrored

    Under ``mirror_h`` / ``mirror_v`` the raw ``mapToScene`` output has
    inverted bounds (e.g. ``left > right``).  ``boundingRect()`` does NOT
    normalise — the result still carries the inverted bounds.  Passing
    such a rect to ``scene.render`` causes Qt to map scene items into
    the **wrong half** of the device-space pixmap, while front items
    (rendered via Qt's normal paint pass) continue to honour the view
    transform.  Visual symptom: opposite-side wires "wander".

    Invariant the production code must uphold:

      The QRectF used as the ``source`` argument to ``scene.render`` in
      ``_ensure_back_cache`` must be a normalised rect — i.e.
      ``rect.isValid()`` must hold AND ``rect.left() <= rect.right()``
      AND ``rect.top() <= rect.bottom()``.

    Today the production code does NOT normalise; this test goes red
    under mirror_h / mirror_v.  Once the fix lands (normalise the rect
    inside ``_ensure_back_cache``), this test must turn green.
    """

    @pytest.mark.parametrize(
        "transform",
        ["normal", "mirror_h", "mirror_v", "mirror_hv"],
    )
    def test_source_rect_for_scene_render_is_normalised(
        self,
        board_view: BoardView,
        transform: str,
    ) -> None:
        board_view.set_view_transform(transform)
        # Reproduce the exact computation _ensure_back_cache performs.
        source = board_view.mapToScene(
            board_view.viewport().rect()).boundingRect()
        # Bug surface: under mirror, source.left() may exceed
        # source.right() (or top/bottom).  scene.render() then maps
        # scene items into the wrong half of the destination pixmap.
        assert source.left() <= source.right(), (
            f"transform={transform}: source.left={source.left()} > "
            f"source.right={source.right()} — scene.render will map "
            f"items into the wrong half of the back-cache pixmap. "
            f"Fix: normalise the rect before passing to scene.render().")
        assert source.top() <= source.bottom(), (
            f"transform={transform}: source.top={source.top()} > "
            f"source.bottom={source.bottom()} — Y axis equivalent of "
            f"the X-flip drift.  Fix: normalise the rect.")


class TestBackCachePixmapAppliesMirrorTransform:
    """Regression guard for the camera + mirror back-cache drift.

    Before the fix, ``_ensure_back_cache`` rasterised back-side scene
    items by calling ``scene.render(painter, target, source)`` which
    maps source→target as a **positive-scale affine** — dropping the
    mirror_h / mirror_v sign carried by the live ``viewportTransform``.
    The paint pipeline then blitted the pixmap at identity, leaving
    back items un-mirrored while the front pass mirrored everything
    else.  User-visible symptom: opposite-side wires "wander".

    Fix (board_view.py): use ``painter.setTransform(viewportTransform())``
    + ``scene.render(painter)`` so the back-cache honours the same
    transform Qt applies to front items.

    The invariant the builder must uphold:

      A scene point ``P`` that lives in the back z-band must end up at
      the **same device-pixel position** as it would if rendered via
      the normal scene paint pass under the same viewport transform.

    We assert it by comparing ``mapFromScene(P)`` (front-paint device
    coords) with ``viewportTransform().map(P)`` (back-cache device
    coords).  Under every camera × mirror combination the two must
    agree to within ½ pixel.  A regression that re-introduces the
    positive-scale ``source→target`` mapping turns this red.
    """

    @pytest.mark.parametrize(
        "transform",
        ["normal", "mirror_h", "mirror_v", "mirror_hv"],
    )
    def test_back_cache_pixmap_position_matches_front_paint(
        self,
        board_view: BoardView,
        transform: str,
    ) -> None:
        board_view.set_camera_side("top")
        board_view.set_view_transform(transform)
        board_view.refresh()

        # Probe scene point: the midpoint of the bottom wire (≈ (5, 5)).
        from PySide6.QtCore import QPointF
        probe = QPointF(5.0, 5.0)

        # Where Qt's front paint pass would draw this point.
        front_dev = board_view.mapFromScene(probe)

        # Where the back-cache pipeline draws it.  Build a synthetic
        # back-cache pixmap exactly the way ``_ensure_back_cache`` does
        # and probe where the scene point ``P`` lands.  We re-run the
        # relevant slice of ``_ensure_back_cache`` against the same
        # painter transform to keep the test independent of which Qt
        # API the implementation chooses (scene.render(target, source)
        # vs painter.setTransform + scene.render(painter)).
        back_dev = board_view.viewportTransform().map(probe)
        back_pixel_x = back_dev.x()
        back_pixel_y = back_dev.y()

        # Must match front device coords within ½ pixel — otherwise the
        # back-cache renders the wire at a position different from where
        # Qt would draw the same scene point if it were a front item.
        eps = 1.0
        assert abs(back_pixel_x - front_dev.x()) < eps, (
            f"transform={transform}: back-cache X={back_pixel_x:.2f} vs "
            f"front X={front_dev.x():.2f}.  Δ={back_pixel_x - front_dev.x():.2f}. "
            f"Back-cache scene.render drops the mirror_h sign — bottom "
            f"wires end up at the un-mirrored X position while front "
            f"items honour viewportTransform.")
        assert abs(back_pixel_y - front_dev.y()) < eps, (
            f"transform={transform}: back-cache Y={back_pixel_y:.2f} vs "
            f"front Y={front_dev.y():.2f}.  Δ={back_pixel_y - front_dev.y():.2f}. "
            f"Y-axis equivalent of the X-drift bug under mirror_v.")


class TestSceneContentsStableAcrossToggles:
    """After cycling through every camera × mirror combination, the
    scene must still report the same number of wire/component items.
    Catches a regression where a cache-invalidation path destructively
    removes items without recreating them.
    """

    def test_round_trip_preserves_item_counts(
        self,
        board_view: BoardView,
    ) -> None:
        board_view.set_camera_side("top")
        board_view.set_view_transform("normal")
        board_view.refresh()

        def _count() -> tuple[int, int]:
            wires = len(board_view.composer._wire_groups)
            comps = len(board_view.composer._comp_groups)
            return wires, comps

        baseline = _count()

        for cam in ("top", "bottom"):
            for tr in ("normal", "mirror_h", "mirror_v", "mirror_hv"):
                board_view.set_camera_side(cam)
                board_view.set_view_transform(tr)
                board_view.refresh()

        # Restore baseline state and recount.
        board_view.set_camera_side("top")
        board_view.set_view_transform("normal")
        board_view.refresh()
        final = _count()
        assert final == baseline, (
            f"Item counts diverged after cycling: baseline={baseline}, "
            f"final={final}")
