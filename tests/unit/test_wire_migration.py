# SPDX-License-Identifier: MIT
"""Contract tests for migrating legacy wires to the knot-aware model."""

from __future__ import annotations

import uuid

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.layout.knot import JointKnot, PinKnot
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.wire import Wire
from protoboard.domain.schematic.net import PinRef
from protoboard.geometry import Point
from protoboard.services.persistence.wire_migration import migrate_layout_to_v2


def _board() -> Board:
    return Board(
        root=BoardGrid(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            cell=PadAtom(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                layers=PadLayers(top=True, bottom=True, plated_through=True),
            ),
            rows=5,
            cols=5,
            row_gap=0.0,
            col_gap=0.0,
        )
    )


def test_migration_merges_legacy_segments_and_preserves_pin_endpoint() -> None:
    component_id = uuid.uuid4()
    pin = PinRef(component_id, 1)
    layout = Layout(
        wires=[
            Wire(
                id=uuid.uuid4(),
                path=[Point(0.5, 0.5), Point(2.5, 0.5)],
                side="top",
            ),
            Wire(
                id=uuid.uuid4(),
                path=[Point(2.5, 0.5), Point(4.5, 0.5)],
                side="top",
            ),
        ]
    )

    report = migrate_layout_to_v2(
        layout,
        _board(),
        {Point(0.5, 0.5): pin},
        lambda component, number: (
            Point(0.5, 0.5) if (component, number) == (component_id, 1) else None
        ),
    )

    assert report.legacy_wire_count == 2
    assert report.new_wire_count == 1
    assert report.merges_performed == 1
    assert report.off_pad_endpoints == []
    assert any(
        isinstance(knot, PinKnot) and knot.pin == pin for knot in layout.knots.values()
    )
    assert not any(
        isinstance(knot, JointKnot) and knot.position == Point(2.5, 0.5)
        for knot in layout.knots.values()
    )
    assert next(iter(layout.wires)).path in (
        [Point(0.5, 0.5), Point(2.5, 0.5), Point(4.5, 0.5)],
        [Point(4.5, 0.5), Point(2.5, 0.5), Point(0.5, 0.5)],
    )


def test_migration_reports_off_pad_endpoint_without_aborting() -> None:
    layout = Layout(
        wires=[
            Wire(
                id=uuid.uuid4(),
                path=[Point(0.5, 0.5), Point(9.0, 9.0)],
                side="top",
            )
        ]
    )

    report = migrate_layout_to_v2(layout, _board(), {}, lambda *_: None)

    assert report.legacy_wire_count == 1
    assert report.new_wire_count == 1
    assert report.off_pad_endpoints == [Point(9.0, 9.0)]
    assert len(layout.wires) == 1


def test_migration_is_idempotent_after_first_conversion() -> None:
    layout = Layout(
        wires=[
            Wire(
                id=uuid.uuid4(),
                path=[Point(0.5, 0.5), Point(4.5, 0.5)],
                side="bottom",
                insulated=True,
            )
        ]
    )
    first = migrate_layout_to_v2(layout, _board(), {}, lambda *_: None)
    before = [(wire.id, wire.path, wire.side, wire.insulated) for wire in layout.wires]

    second = migrate_layout_to_v2(layout, _board(), {}, lambda *_: None)

    assert first.new_wire_count == 1
    assert second.legacy_wire_count == 0
    assert second.new_wire_count == 1
    assert [
        (wire.id, wire.path, wire.side, wire.insulated) for wire in layout.wires
    ] == before
