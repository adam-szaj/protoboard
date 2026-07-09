# SPDX-License-Identifier: MIT
"""Tests for PlaceTool — placement, undo, rotation, refdes generation."""

from __future__ import annotations

from typing import cast

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent

from protoboard.domain.commands.base import ComponentState
from protoboard.geometry import Point
from protoboard.gui.board_view import BoardView
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.place_tool import PlaceTool


def _left_click(tool: PlaceTool, x: float, y: float) -> None:
    """Click once (anchor). If the tool enters drag_pin phase, click again to confirm."""
    ev = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(x, y),
        QPointF(x, y),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    tool.on_press(ev, QPointF(x, y))
    # If the component has bendable pins, confirm each drag pin with a click at
    # the same position (accepts the default offset).
    while tool._phase == "drag_pin":
        tool.on_press(ev, QPointF(x, y))


def _key_r(tool: PlaceTool) -> None:
    tool.on_key(
        QKeyEvent(
            QKeyEvent.Type.KeyPress,
            Qt.Key.Key_R,
            Qt.KeyboardModifier.NoModifier,
        )
    )


class TestPlaceToolPlacement:
    def test_place_adds_to_layout(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        _left_click(tool, 0.5, 0.5)
        assert len(session.layout.placements) == 1

    def test_place_adds_instance(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        _left_click(tool, 0.5, 0.5)
        assert len(session.instances) == 1

    def test_place_snaps_to_pad(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        _left_click(tool, 0.4, 0.4)
        placement = list(session.layout.placements.values())[0]
        from protoboard.geometry import Point

        assert placement.position == Point(0.5, 0.5)

    def test_place_default_rotation_zero(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        _left_click(tool, 0.5, 0.5)
        placement = list(session.layout.placements.values())[0]
        assert placement.rotation == 0


class TestPlaceToolSmdMount:
    """An SMD chip's pin 2 snaps to an orthogonal or diagonal neighbour pad."""

    def _add_smd_def(self, session: EditorSession) -> str:
        from protoboard.domain.components.definition import (
            ComponentClass,
            ComponentDefinition,
        )
        from protoboard.domain.components.package.adapter import PackageFootprint
        from protoboard.domain.components.package.loader import build_package
        pkg = build_package(
            {
                "shape": "prism",
                "dims": {"length": 2.0, "width": 1.25},
                "arrangement": "axial", "mount": "surface", "bend": 0,
                "leads": {
                    "direction": [[0, 0, -1], [0, 0, -1]],
                    "body_offset": {"unit": 2.5, "value": [[1, 0]]},
                    "board_offset": "default",
                },
            },
            "SMD_TEST",
        )
        defn = ComponentDefinition(
            name="R_SMD", component_class=ComponentClass.RESISTOR,
            pin_names={}, package=pkg,
        )
        session.components["R_SMD"] = defn
        return "R_SMD"

    @staticmethod
    def _builtin_view(session: EditorSession) -> "BoardView":
        # A session over the real shipped library (the fixture library has only a
        # minimal ``R`` with no ``footprints:`` list), reusing the fixture board.
        from protoboard.paths import BUILTIN_LIBRARY_DIR
        from protoboard.services.library.loader import load_library

        lib = load_library([BUILTIN_LIBRARY_DIR])
        sess = EditorSession(board=session.board, library=lib)
        return BoardView(sess)

    def test_place_library_resistor_as_smd_variant_activates(
        self, session: EditorSession
    ) -> None:
        # Regression: placing the real library ``R`` in its ``SMD-0805``
        # footprint variant must NOT crash. The variant arrives as a
        # ``package_variant`` address (no placed instance yet), and the bend-spec
        # resolution must read it without synthesizing a (refdes-requiring)
        # ComponentInstance. Reproduces the activate → _rebuild_ghost →
        # _bend_specs path that raised ``refdes must not be empty``.
        view = self._builtin_view(session)
        # The chosen SMD body → pin 2 gets a free neighbour ring (the eight pads
        # around pin 1), so the tool enters the two-phase drag.
        view.set_tool(PlaceTool("R", package_variant="SMD-0805/typ"))
        tool = view._active_tool
        assert isinstance(tool, PlaceTool)
        specs = tool._bend_specs(view._session.components["R"])
        assert list(specs.keys()) == [2]
        assert specs[2].is_free()

    def test_place_library_resistor_as_axial_variant_stays_free(
        self, session: EditorSession
    ) -> None:
        # The same library ``R`` in a THT axial footprint keeps its free
        # bendable lead — the SMD fix must not turn axial parts rigid.
        view = self._builtin_view(session)
        view.set_tool(PlaceTool("R", package_variant="DIN-0207/typ"))
        tool = view._active_tool
        assert isinstance(tool, PlaceTool)
        specs = tool._bend_specs(view._session.components["R"])
        assert any(s.is_free() for s in specs.values())

    def test_ghost_body_matches_chosen_variant(
        self, session: EditorSession
    ) -> None:
        # Regression: the ghost resolves its body against the catalogue, so
        # _rebuild_ghost must draw with a theme carrying the session's
        # package_families — otherwise the chosen SMD-0805 falls through to the
        # default candy disc. The two variants must give different ghost bodies.
        view = self._builtin_view(session)
        view.set_tool(PlaceTool("R", package_variant="SMD-0805/typ"))
        smd_tool = view._active_tool
        assert isinstance(smd_tool, PlaceTool)
        smd_tool._rebuild_ghost()
        assert smd_tool._ghost is not None
        smd_rect = smd_tool._ghost.boundingRect()

        view.set_tool(PlaceTool("R", package_variant="DIN-0207/typ"))
        axial_tool = view._active_tool
        assert isinstance(axial_tool, PlaceTool)
        axial_tool._rebuild_ghost()
        assert axial_tool._ghost is not None
        # Different bodies → different ghost geometry (the SMD chip vs the axial
        # body). If the theme lacked the catalogue both would be the default.
        assert smd_rect != axial_tool._ghost.boundingRect()

    def test_drag_baseline_uses_chosen_variant_geometry(
        self, session: EditorSession
    ) -> None:
        # Regression: the drag-pin baseline (and flip / pin-count) must use the
        # chosen variant's footprint, the same geometry the ghost renders. When
        # it used defn.default_footprint() instead, the offset was measured against one
        # body and drawn against another, so the ghost drifted in drag-pin —
        # worse with rotation. The pin-2 baseline must differ between the SMD
        # chip and the axial body (different pin spacing).
        from protoboard.geometry import Point

        view = self._builtin_view(session)
        view.set_tool(PlaceTool("R", package_variant="SMD-0805/typ"))
        smd = view._active_tool
        assert isinstance(smd, PlaceTool)
        smd._anchor_pos = Point(5.0, 5.0)
        smd_base = smd._pin_baseline_world(2)

        view.set_tool(PlaceTool("R", package_variant="DIN-0207/typ"))
        axial = view._active_tool
        assert isinstance(axial, PlaceTool)
        axial._anchor_pos = Point(5.0, 5.0)
        axial_base = axial._pin_baseline_world(2)

        assert smd_base is not None and axial_base is not None
        # Different bodies → pin 2 sits at different world spots from the anchor.
        assert (smd_base.x, smd_base.y) != (axial_base.x, axial_base.y)

    def test_smd_enters_drag_pin_phase(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        # The SMD second terminal is movable → placing it is a two-phase drag.
        name = self._add_smd_def(session)
        board_view.set_tool(PlaceTool(name))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        from PySide6.QtGui import QMouseEvent
        ev = QMouseEvent(
            QMouseEvent.Type.MouseButtonPress, QPointF(1, 1), QPointF(1, 1),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        tool.on_press(ev, QPointF(1, 1))
        assert tool._phase == "drag_pin"
        assert tool._current_drag_pin == 2

    def test_pin2_snaps_to_diagonal_neighbour(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        from protoboard.geometry import Point
        from protoboard.domain.layout.placement import pin_world_position
        name = self._add_smd_def(session)
        board_view.set_tool(PlaceTool(name))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        from PySide6.QtGui import QMouseEvent

        def press(x: float, y: float) -> None:
            ev = QMouseEvent(
                QMouseEvent.Type.MouseButtonPress, QPointF(x, y), QPointF(x, y),
                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            tool.on_press(ev, QPointF(x, y))

        press(1, 1)                       # anchor pin 1 at (1,1)
        assert tool._phase == "drag_pin"
        # Drag pin 2 toward the diagonal SE pad (2,2) and confirm.
        off = tool._compute_drag_offset(Point(2.0, 2.0))
        assert off is not None
        # Committing that offset must land pin 2 on the SE diagonal neighbour.
        tool._pin_offsets[2] = off
        defn = session.components[name]
        from protoboard.domain.layout.placement import ComponentPlacement
        import uuid
        pl = ComponentPlacement(
            component_id=uuid.uuid4(), position=Point(1.0, 1.0),
            rotation=0, side="top", pin_offsets={2: off},
        )
        p1 = pin_world_position(defn.default_footprint(), pl, 1)
        p2 = pin_world_position(defn.default_footprint(), pl, 2)
        assert (round(p1.x), round(p1.y)) == (1, 1)
        assert (round(p2.x), round(p2.y)) == (2, 2)  # SE diagonal neighbour

    def test_axial_pin1_anchors_at_click(self, session: EditorSession) -> None:
        # Regression: an axial part (R / diode) has BOTH leads free, so its
        # bendable specs are {1, 2}. Pin 1 is the anchor and must land exactly on
        # the (snapped) first click — it must NOT become the dragged pin.
        # Previously pin 1 was dragged (current_drag_pin == 1) and, bound by its
        # r_min reach disc, could not stick to the clicked pad (it snapped out to
        # the ring).
        view = self._builtin_view(session)
        view.set_tool(PlaceTool("R"))  # default axial footprint
        tool = view._active_tool
        assert isinstance(tool, PlaceTool)
        # Sanity: both axial leads are free → bendable {1, 2}.
        defn = view._session.components["R"]
        assert set(tool._bend_specs(defn).keys()) == {1, 2}

        from PySide6.QtGui import QMouseEvent
        ev = QMouseEvent(
            QMouseEvent.Type.MouseButtonPress, QPointF(5, 5), QPointF(5, 5),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        snapped = view.snap(Point(5, 5))
        tool.on_press(ev, QPointF(5, 5))

        # Pin 1 anchors — it is NOT the dragged pin; pin 2 is.
        assert tool._phase == "drag_pin"
        assert tool._current_drag_pin == 2
        assert tool._anchor_pos == snapped
        # Pin 1's baseline world position equals the anchor (its default offset),
        # i.e. it sits on the clicked pad — not pushed out to the r_min ring.
        base1 = tool._pin_baseline_world(1)
        assert base1 is not None
        assert (base1.x, base1.y) == (snapped.x, snapped.y)


class TestPlaceToolUndo:
    def test_undo_removes_placement(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        _left_click(tool, 0.5, 0.5)
        assert len(session.layout.placements) == 1
        session.stack.undo(cast(ComponentState, session))
        assert len(session.layout.placements) == 0

    def test_undo_removes_instance(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """BUG 6 regression: undo must also remove instance from session.instances."""
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        _left_click(tool, 0.5, 0.5)
        assert len(session.instances) == 1
        session.stack.undo(cast(ComponentState, session))
        assert len(session.instances) == 0

    def test_redo_restores_placement(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        _left_click(tool, 0.5, 0.5)
        session.stack.undo(cast(ComponentState, session))
        session.stack.redo(cast(ComponentState, session))
        assert len(session.layout.placements) == 1


class TestPlaceToolRotation:
    def test_rotate_increments_by_90(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        _key_r(tool)
        assert tool._rotation == 90

    def test_rotate_wraps_at_360(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        for _ in range(4):
            _key_r(tool)
        assert tool._rotation == 0

    def test_placed_component_has_rotated_placement(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        _key_r(tool)
        _left_click(tool, 0.5, 0.5)
        placement = list(session.layout.placements.values())[0]
        assert placement.rotation == 90


def _key_event(
    key: Qt.Key, mods: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier
) -> QKeyEvent:
    return QKeyEvent(QKeyEvent.Type.KeyPress, key, mods)


class TestPlaceToolKeymap:
    """PlaceTool claims the transform keys (so the no-op-while-placing global
    rotate/flip actions don't shadow them), dynamically via the KeyMap."""

    def test_wants_rotate_and_flip_keys(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        assert tool.wants_key(_key_event(Qt.Key.Key_R)) is True  # rotate CW
        assert tool.wants_key(
            _key_event(Qt.Key.Key_R, Qt.KeyboardModifier.AltModifier)
        ) is True  # flip
        # An unrelated key is not claimed.
        assert tool.wants_key(_key_event(Qt.Key.Key_K)) is False

    def test_rotate_follows_rebound_global(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        # Rebind the global rotate from "R" to "K"; PlaceTool inherits it.
        board_view.keymap.set_override("global.action.rotate_cw", "K")
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        assert tool.wants_key(_key_event(Qt.Key.Key_R)) is False
        assert tool.wants_key(_key_event(Qt.Key.Key_K)) is True
        # [K] now rotates the ghost.
        tool.on_key(_key_event(Qt.Key.Key_K))
        assert tool._rotation == 90


class TestPlaceToolRefdes:
    def test_first_component_gets_r1(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        _left_click(tool, 0.5, 0.5)
        inst = list(session.instances.values())[0]
        assert inst.refdes == "R1"

    def test_second_component_gets_r2(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        _left_click(tool, 0.5, 0.5)
        _left_click(tool, 2.5, 0.5)
        refdeses = {i.refdes for i in session.instances.values()}
        assert refdeses == {"R1", "R2"}


class TestPlaceToolInitialValue:
    """``ComponentDefinition.default_value`` controls what gets written
    into ``ComponentInstance.value`` on a fresh placement."""

    def test_concrete_part_value_equals_name(
        self, board_view: BoardView, session: EditorSession,
    ) -> None:
        # The default fixture's R has no default_value field → falls
        # back to defn.name="R".
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        _left_click(tool, 0.5, 0.5)
        inst = list(session.instances.values())[0]
        assert inst.value == "R"

    def test_generic_template_value_is_question_mark(
        self, board_view: BoardView, session: EditorSession,
    ) -> None:
        # Override the in-memory definition with default_value="?" to
        # simulate a "generic" template entry.
        import dataclasses as _dc
        defn = session.components["R"]
        session.components["R"] = _dc.replace(defn, default_value="?")
        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        _left_click(tool, 0.5, 0.5)
        inst = list(session.instances.values())[0]
        assert inst.value == "?"
