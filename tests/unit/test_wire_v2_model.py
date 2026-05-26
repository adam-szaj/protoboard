# SPDX-License-Identifier: MIT
"""Tests for the v2 Wire / Fragment / Knot model and Layout mutators."""

from __future__ import annotations

import uuid

import pytest

from protoboard.domain.layout.knot import JointKnot, PinKnot
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.wire import Fragment, KnotRef, Wire
from protoboard.domain.schematic.net import PinRef
from protoboard.geometry import Point


def _new_pin(component_int: int = 1, pin_number: int = 1) -> PinRef:
    return PinRef(component_id=uuid.UUID(int=component_int), pin_number=pin_number)


def _make_layout(positions: dict[PinRef, Point] | None = None) -> Layout:
    layout = Layout()
    positions = positions or {}
    layout.set_pin_resolver(lambda p: positions[p])
    return layout


# --- Wire / Fragment basics ---------------------------------------------------


class TestFragment:
    def test_path_requires_two_points(self) -> None:
        with pytest.raises(ValueError, match="at least 2 points"):
            Fragment(path=(Point(0, 0),), side="top")

    def test_side_must_be_top_or_bottom(self) -> None:
        with pytest.raises(ValueError, match="top.*bottom"):
            Fragment(path=(Point(0, 0), Point(1, 0)), side="left")  # type: ignore[arg-type]


class TestWire:
    def test_empty_fragments_rejected(self) -> None:
        # Wire now accepts either path-or-fragments construction; empty
        # *both* must fail (path has fewer than 2 points either way).
        with pytest.raises(ValueError, match="at least 2 points"):
            Wire(
                id=uuid.uuid4(),
                a=KnotRef(id=uuid.uuid4()),
                b=KnotRef(id=uuid.uuid4()),
                fragments=[],
                insulated=False,
            )

    def test_fragment_boundary_must_chain(self) -> None:
        with pytest.raises(ValueError, match="boundary mismatch"):
            Wire(
                id=uuid.uuid4(),
                a=KnotRef(id=uuid.uuid4()),
                b=KnotRef(id=uuid.uuid4()),
                fragments=(
                    Fragment(path=(Point(0, 0), Point(1, 0)), side="top"),
                    Fragment(path=(Point(2, 0), Point(3, 0)), side="bottom"),
                ),
                insulated=False,
            )

    def test_path_flatten_drops_duplicate_via_vertex(self) -> None:
        w = Wire(
            id=uuid.uuid4(),
            a=KnotRef(id=uuid.uuid4()),
            b=KnotRef(id=uuid.uuid4()),
            fragments=(
                Fragment(path=(Point(0, 0), Point(2, 0)), side="top"),
                Fragment(path=(Point(2, 0), Point(3, 0)), side="bottom"),
                Fragment(path=(Point(3, 0), Point(5, 0)), side="top"),
            ),
            insulated=False,
        )
        assert w.path == [
            Point(0, 0), Point(2, 0), Point(3, 0), Point(5, 0),
        ]

    def test_endpoints_property(self) -> None:
        w = Wire(
            id=uuid.uuid4(),
            a=KnotRef(id=uuid.uuid4()),
            b=KnotRef(id=uuid.uuid4()),
            fragments=(Fragment(path=(Point(0, 0), Point(5, 5)), side="top"),),
            insulated=True,
        )
        assert w.endpoints == (Point(0, 0), Point(5, 5))

    def test_sides_property(self) -> None:
        w = Wire(
            id=uuid.uuid4(),
            a=KnotRef(id=uuid.uuid4()),
            b=KnotRef(id=uuid.uuid4()),
            fragments=(
                Fragment(path=(Point(0, 0), Point(2, 0)), side="top"),
                Fragment(path=(Point(2, 0), Point(3, 0)), side="bottom"),
            ),
            insulated=False,
        )
        assert w.sides == frozenset({"top", "bottom"})


# --- Knot management ----------------------------------------------------------


class TestPinKnotIndex:
    def test_get_or_create_pin_knot_idempotent(self) -> None:
        layout = _make_layout()
        pin = _new_pin()
        a = layout.get_or_create_pin_knot(pin)
        b = layout.get_or_create_pin_knot(pin)
        assert a.id == b.id
        assert layout.pin_knots[pin] == a.id

    def test_distinct_pins_get_distinct_knots(self) -> None:
        layout = _make_layout()
        a = layout.get_or_create_pin_knot(_new_pin(1, 1))
        b = layout.get_or_create_pin_knot(_new_pin(1, 2))
        assert a.id != b.id


class TestJointKnot:
    def test_create_joint_knot_registers(self) -> None:
        layout = _make_layout()
        k = layout.create_joint_knot(Point(3, 0))
        assert k.id in layout.knots
        assert layout.knots[k.id] is k


# --- Layout mutators ----------------------------------------------------------


def _single_frag_wire(a: KnotRef, b: KnotRef, start: Point, end: Point) -> Wire:
    return Wire(
        id=uuid.uuid4(),
        a=a,
        b=b,
        fragments=(Fragment(path=(start, end), side="top"),),
        insulated=False,
    )


class TestAddWireV2:
    def test_back_refs_updated(self) -> None:
        layout = _make_layout({_new_pin(1, 1): Point(0, 0), _new_pin(2, 1): Point(5, 0)})
        ka = layout.get_or_create_pin_knot(_new_pin(1, 1))
        kb = layout.get_or_create_pin_knot(_new_pin(2, 1))
        w = _single_frag_wire(KnotRef(id=ka.id), KnotRef(id=kb.id), Point(0, 0), Point(5, 0))
        layout.add_wire_v2(w)
        assert w.id in layout.knots[ka.id].wire_ids
        assert w.id in layout.knots[kb.id].wire_ids

    def test_unknown_knot_raises(self) -> None:
        layout = _make_layout()
        bogus = KnotRef(id=uuid.uuid4())
        layout.create_joint_knot(Point(0, 0))  # only one knot — bogus is unknown
        w = Wire(
            id=uuid.uuid4(),
            a=bogus,
            b=bogus,
            fragments=(Fragment(path=(Point(0, 0), Point(1, 0)), side="top"),),
            insulated=False,
        )
        with pytest.raises(ValueError, match="not in layout.knots"):
            layout.add_wire_v2(w)


class TestRemoveWireV2:
    def test_pin_knot_survives(self) -> None:
        layout = _make_layout({_new_pin(1, 1): Point(0, 0), _new_pin(2, 1): Point(5, 0)})
        ka = layout.get_or_create_pin_knot(_new_pin(1, 1))
        kb = layout.get_or_create_pin_knot(_new_pin(2, 1))
        w = _single_frag_wire(KnotRef(id=ka.id), KnotRef(id=kb.id), Point(0, 0), Point(5, 0))
        layout.add_wire_v2(w)
        removed = layout.remove_wire_v2(w.id)
        assert removed == []
        assert ka.id in layout.knots  # PinKnot stays — pin still exists

    def test_orphan_joint_knot_removed(self) -> None:
        layout = _make_layout({_new_pin(1, 1): Point(0, 0)})
        ka = layout.get_or_create_pin_knot(_new_pin(1, 1))
        joint = layout.create_joint_knot(Point(5, 0))
        w = _single_frag_wire(KnotRef(id=ka.id), KnotRef(id=joint.id), Point(0, 0), Point(5, 0))
        layout.add_wire_v2(w)
        removed = layout.remove_wire_v2(w.id)
        assert joint.id in removed
        assert joint.id not in layout.knots


class TestReplaceWireFragments:
    def test_id_preserved(self) -> None:
        layout = _make_layout({_new_pin(1, 1): Point(0, 0), _new_pin(2, 1): Point(5, 0)})
        ka = layout.get_or_create_pin_knot(_new_pin(1, 1))
        kb = layout.get_or_create_pin_knot(_new_pin(2, 1))
        w = _single_frag_wire(KnotRef(id=ka.id), KnotRef(id=kb.id), Point(0, 0), Point(5, 0))
        layout.add_wire_v2(w)
        new_frags = [Fragment(path=[Point(0, 0), Point(2, 0), Point(5, 0)], side="top")]
        updated = layout.replace_wire_fragments(w.id, new_frags)
        assert updated.id == w.id  # the headline invariant
        assert list(layout.wires_v2[w.id].fragments) == new_frags

    def test_endpoint_mismatch_raises(self) -> None:
        layout = _make_layout({_new_pin(1, 1): Point(0, 0), _new_pin(2, 1): Point(5, 0)})
        ka = layout.get_or_create_pin_knot(_new_pin(1, 1))
        kb = layout.get_or_create_pin_knot(_new_pin(2, 1))
        w = _single_frag_wire(KnotRef(id=ka.id), KnotRef(id=kb.id), Point(0, 0), Point(5, 0))
        layout.add_wire_v2(w)
        # New path[0] = (1, 0) — disagrees with PinKnot ka at (0, 0).
        bad_frags = (Fragment(path=(Point(1, 0), Point(5, 0)), side="top"),)
        with pytest.raises(ValueError, match="disagrees with"):
            layout.replace_wire_fragments(w.id, bad_frags)


# --- Joint collapse -----------------------------------------------------------


class TestTryCollapseJoint:
    def _three_wires_meeting_at_joint(
        self,
        insulated_left: bool = True,
        insulated_right: bool = True,
        side_left: str = "top",
        side_right: str = "top",
    ) -> tuple[Layout, JointKnot, Wire, Wire]:
        layout = _make_layout({_new_pin(1, 1): Point(0, 0), _new_pin(2, 1): Point(5, 0)})
        ka = layout.get_or_create_pin_knot(_new_pin(1, 1))
        kb = layout.get_or_create_pin_knot(_new_pin(2, 1))
        joint = layout.create_joint_knot(Point(3, 0))
        w_left = Wire(
            id=uuid.uuid4(), a=KnotRef(id=ka.id), b=KnotRef(id=joint.id),
            fragments=(Fragment(path=(Point(0, 0), Point(3, 0)), side=side_left),),  # type: ignore[arg-type]
            insulated=insulated_left,
        )
        w_right = Wire(
            id=uuid.uuid4(), a=KnotRef(id=joint.id), b=KnotRef(id=kb.id),
            fragments=(Fragment(path=(Point(3, 0), Point(5, 0)), side=side_right),),  # type: ignore[arg-type]
            insulated=insulated_right,
        )
        layout.add_wire_v2(w_left)
        layout.add_wire_v2(w_right)
        return layout, joint, w_left, w_right

    def test_same_side_same_insulated_merges(self) -> None:
        layout, joint, _, _ = self._three_wires_meeting_at_joint()
        merged = layout.try_collapse_joint(joint.id)
        assert merged is not None
        assert joint.id not in layout.knots
        assert len(layout.wires_v2) == 1
        # Path order depends on which wire's endpoint became `a` after merge;
        # either orientation is correct, so accept both.
        path = layout.wires_v2[merged.id].path
        forward = [Point(0, 0), Point(3, 0), Point(5, 0)]
        assert path == forward or path == list(reversed(forward))

    def test_insulated_mismatch_blocks(self) -> None:
        layout, joint, _, _ = self._three_wires_meeting_at_joint(
            insulated_left=True, insulated_right=False,
        )
        assert layout.try_collapse_joint(joint.id) is None
        assert joint.id in layout.knots
        assert len(layout.wires_v2) == 2

    def test_side_mismatch_without_via_blocks(self) -> None:
        layout, joint, _, _ = self._three_wires_meeting_at_joint(
            side_left="top", side_right="bottom",
        )
        assert layout.try_collapse_joint(joint.id) is None
        assert joint.id in layout.knots

    def test_side_mismatch_with_plated_through_merges(self) -> None:
        layout, joint, _, _ = self._three_wires_meeting_at_joint(
            side_left="top", side_right="bottom",
        )
        plated = frozenset({Point(3, 0)})
        merged = layout.try_collapse_joint(joint.id, plated_through_pads=plated)
        assert merged is not None
        assert joint.id not in layout.knots
        # Two fragments kept — joint vertex is the via boundary.  Orientation
        # depends on which wire's id is min(); both orderings are correct.
        assert len(merged.fragments) == 2
        assert set(f.side for f in merged.fragments) == {"top", "bottom"}

    def test_pin_knot_not_collapsible(self) -> None:
        layout = _make_layout({_new_pin(1, 1): Point(0, 0)})
        ka = layout.get_or_create_pin_knot(_new_pin(1, 1))
        # PinKnots are not subject to try_collapse_joint even if degree==2.
        assert layout.try_collapse_joint(ka.id) is None
