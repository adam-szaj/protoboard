# SPDX-License-Identifier: MIT
"""Tests for EditorSession — signal wiring and state initialisation."""

from __future__ import annotations

import uuid

import pytest

from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.geometry import Point
from protoboard.gui.session import EditorSession


def _place_r(session: EditorSession, x: float, y: float) -> ComponentInstance:
    """Helper: place a 2-pin resistor at (x, y) via session internals."""
    inst = ComponentInstance(
        id=uuid.uuid4(),
        definition_ref="R",
        refdes="R1",
        value="10k",
    )
    session.instances[inst.id] = inst
    placement = ComponentPlacement(
        component_id=inst.id,
        position=Point(x, y),
        rotation=0,
        side="top",
    )
    session.layout.placements[inst.id] = placement
    return inst


class TestSessionInit:
    def test_components_populated_from_library(self, session: EditorSession) -> None:
        # library has at least R, LM358, BC547
        assert len(session.components) > 0

    def test_layout_empty_on_init(self, session: EditorSession) -> None:
        assert session.layout.placements == {}
        assert session.layout.wires == []

    def test_instances_empty_on_init(self, session: EditorSession) -> None:
        assert session.instances == {}


class TestSessionSignals:
    def test_layout_changed_signal_exists(self, session: EditorSession) -> None:
        received: list[None] = []
        session.layout_changed.connect(lambda: received.append(None))
        session.layout_changed.emit()
        assert len(received) == 1

    def test_drc_changed_emitted_on_run_drc(self, session: EditorSession) -> None:
        received: list[list[object]] = []
        session.drc_changed.connect(lambda issues: received.append(issues))
        session.run_drc()
        assert len(received) == 1

    def test_run_drc_returns_list(self, session: EditorSession) -> None:
        issues = session.run_drc()
        assert isinstance(issues, list)


class TestGetPinPosition:
    """Tests for EditorSession.get_pin_position()."""

    def test_returns_none_for_unknown_comp_id(self, session: EditorSession) -> None:
        assert session.get_pin_position(uuid.uuid4(), 1) is None

    def test_returns_none_when_not_placed(self, session: EditorSession) -> None:
        inst = ComponentInstance(
            id=uuid.uuid4(), definition_ref="R", refdes="R99", value="1k"
        )
        session.instances[inst.id] = inst
        # Instance exists but no placement.
        assert session.get_pin_position(inst.id, 1) is None

    def test_returns_pin1_world_pos(self, session: EditorSession) -> None:
        inst = _place_r(session, x=3.0, y=4.0)
        pos = session.get_pin_position(inst.id, 1)
        assert pos is not None
        # R footprint is SIL(2): pin1 at local (0,0), so world pos == placement.position.
        assert pytest.approx(pos.x) == 3.0
        assert pytest.approx(pos.y) == 4.0

    def test_returns_pin2_world_pos(self, session: EditorSession) -> None:
        inst = _place_r(session, x=0.0, y=0.0)
        pos = session.get_pin_position(inst.id, 2)
        assert pos is not None
        # SIL(2): pin2 at local (0,1) → world (0,1) when rotation=0.
        assert pos.x == pytest.approx(0.0)
        assert pos.y == pytest.approx(1.0)

    def test_returns_none_for_out_of_range_pin(self, session: EditorSession) -> None:
        inst = _place_r(session, x=0.0, y=0.0)
        assert session.get_pin_position(inst.id, 99) is None

    def test_returns_none_for_unknown_definition(self, session: EditorSession) -> None:
        inst = ComponentInstance(
            id=uuid.uuid4(), definition_ref="NO_SUCH_PART", refdes="X1", value=""
        )
        session.instances[inst.id] = inst
        pl = ComponentPlacement(
            component_id=inst.id, position=Point(0, 0), rotation=0, side="top"
        )
        session.layout.placements[inst.id] = pl
        assert session.get_pin_position(inst.id, 1) is None

    def test_respects_rotation(self, session: EditorSession) -> None:
        inst = _place_r(session, x=0.0, y=0.0)
        placement = session.layout.placements[inst.id]
        placement.rotation = 90
        pos = session.get_pin_position(inst.id, 2)
        assert pos is not None
        # SIL(2) pin2 at local (0,1); rotated 90° CW → (1, 0) world.
        assert pos.x == pytest.approx(1.0, abs=1e-6)
        assert pos.y == pytest.approx(0.0, abs=1e-6)


class TestIterPinPositions:
    """Tests for EditorSession.iter_pin_positions()."""

    def test_returns_empty_for_unknown_comp_id(self, session: EditorSession) -> None:
        assert session.iter_pin_positions(uuid.uuid4()) == []

    def test_returns_empty_when_not_placed(self, session: EditorSession) -> None:
        inst = ComponentInstance(
            id=uuid.uuid4(), definition_ref="R", refdes="R99", value="1k"
        )
        session.instances[inst.id] = inst
        assert session.iter_pin_positions(inst.id) == []

    def test_two_pin_component(self, session: EditorSession) -> None:
        inst = _place_r(session, x=5.0, y=2.0)
        result = session.iter_pin_positions(inst.id)
        assert len(result) == 2
        pin_nums = [pn for pn, _ in result]
        assert pin_nums == [1, 2]

    def test_positions_match_get_pin_position(self, session: EditorSession) -> None:
        inst = _place_r(session, x=1.0, y=1.0)
        for pin_num, pos in session.iter_pin_positions(inst.id):
            expected = session.get_pin_position(inst.id, pin_num)
            assert expected is not None
            assert pytest.approx(pos.x) == expected.x
            assert pytest.approx(pos.y) == expected.y

    def test_dip_component_returns_all_pins(self, session: EditorSession) -> None:
        # LM358 is DIP-8 in test library.
        inst = ComponentInstance(
            id=uuid.uuid4(), definition_ref="LM358", refdes="U1", value="LM358"
        )
        session.instances[inst.id] = inst
        pl = ComponentPlacement(
            component_id=inst.id, position=Point(0, 0), rotation=0, side="top"
        )
        session.layout.placements[inst.id] = pl
        result = session.iter_pin_positions(inst.id)
        assert len(result) == 8
        assert [pn for pn, _ in result] == list(range(1, 9))
