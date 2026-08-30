# SPDX-License-Identifier: MIT
"""Tests for Knot subtypes: PinKnot, JointKnot, WirePinKnot."""

from __future__ import annotations

import uuid

from protoboard.domain.layout.knot import (
    JointKnot,
    Knot,
    PinKnot,
    WirePinKnot,
)
from protoboard.domain.schematic.net import PinRef
from protoboard.geometry import Point


class TestPinKnot:
    def test_construction(self) -> None:
        pid = uuid.uuid4()
        pin = PinRef(component_id=uuid.uuid4(), pin_number=1)
        k = PinKnot(id=pid, pin=pin)
        assert k.id == pid
        assert k.pin == pin
        assert k.wire_ids == frozenset()

    def test_satisfies_protocol(self) -> None:
        k = PinKnot(
            id=uuid.uuid4(),
            pin=PinRef(component_id=uuid.uuid4(), pin_number=1),
        )
        assert isinstance(k, Knot)

    def test_eq_by_id(self) -> None:
        pid = uuid.uuid4()
        pin1 = PinRef(component_id=uuid.uuid4(), pin_number=1)
        pin2 = PinRef(component_id=uuid.uuid4(), pin_number=2)
        a = PinKnot(id=pid, pin=pin1)
        b = PinKnot(id=pid, pin=pin2)
        assert a == b
        assert hash(a) == hash(b)


class TestJointKnot:
    def test_construction(self) -> None:
        jid = uuid.uuid4()
        pos = Point(2.5, 3.5)
        k = JointKnot(id=jid, position=pos)
        assert k.id == jid
        assert k.position == pos
        assert k.wire_ids == frozenset()

    def test_satisfies_protocol(self) -> None:
        k = JointKnot(id=uuid.uuid4(), position=Point(0, 0))
        assert isinstance(k, Knot)


class TestWirePinKnot:
    def test_construction(self) -> None:
        wpid = uuid.uuid4()
        pos = Point(4.5, 5.5)
        net = uuid.uuid4()
        wid = uuid.uuid4()
        k = WirePinKnot(
            id=wpid,
            position=pos,
            net_id=net,
            wire_ids=frozenset({wid}),
        )
        assert k.id == wpid
        assert k.position == pos
        assert k.net_id == net
        assert k.wire_ids == frozenset({wid})

    def test_default_wire_ids_empty(self) -> None:
        k = WirePinKnot(
            id=uuid.uuid4(),
            position=Point(0, 0),
            net_id=None,
        )
        assert k.wire_ids == frozenset()

    def test_net_id_optional(self) -> None:
        k = WirePinKnot(
            id=uuid.uuid4(),
            position=Point(1, 1),
            net_id=None,
        )
        assert k.net_id is None

    def test_satisfies_protocol(self) -> None:
        k = WirePinKnot(
            id=uuid.uuid4(),
            position=Point(0, 0),
            net_id=None,
        )
        assert isinstance(k, Knot)

    def test_eq_by_id(self) -> None:
        wpid = uuid.uuid4()
        a = WirePinKnot(id=wpid, position=Point(0, 0), net_id=None)
        b = WirePinKnot(
            id=wpid,
            position=Point(99, 99),
            net_id=uuid.uuid4(),
        )
        # Same id ⇒ equal, regardless of fields.  Idiom shared with the
        # other Knot subtypes.
        assert a == b
        assert hash(a) == hash(b)

    def test_distinct_from_jointknot(self) -> None:
        # WirePinKnot and JointKnot at the same id are NOT equal — different
        # type means a wire-pin should never accidentally compare-equal
        # to a transient joint that was replaced.
        same_id = uuid.uuid4()
        wp = WirePinKnot(
            id=same_id,
            position=Point(0, 0),
            net_id=None,
        )
        jk = JointKnot(id=same_id, position=Point(0, 0))
        assert wp != jk

    def test_subtype_dispatch(self) -> None:
        # The whole point of having a separate class is that
        # isinstance() narrows correctly — used pervasively in layout.py
        # and elsewhere.
        wp = WirePinKnot(
            id=uuid.uuid4(),
            position=Point(0, 0),
            net_id=None,
        )
        jk = JointKnot(id=uuid.uuid4(), position=Point(0, 0))
        pk = PinKnot(
            id=uuid.uuid4(),
            pin=PinRef(component_id=uuid.uuid4(), pin_number=1),
        )
        assert isinstance(wp, WirePinKnot)
        assert not isinstance(wp, JointKnot)
        assert not isinstance(wp, PinKnot)
        assert not isinstance(jk, WirePinKnot)
        assert not isinstance(pk, WirePinKnot)
