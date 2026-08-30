# SPDX-License-Identifier: MIT
"""Tests for WireCollection.get_or_raise / get_or_return."""

import uuid

import pytest

from protoboard.domain.layout.wire import Fragment, Wire, WireCollection
from protoboard.geometry import Point


def _wire() -> Wire:
    frag = Fragment(path=[Point(0, 0), Point(1, 0)], side="top")
    return Wire(id=uuid.uuid4(), fragments=[frag], insulated=True)


def test_get_or_raise_hit_returns_wire() -> None:
    w = _wire()
    coll = WireCollection([w])
    assert coll.get_or_raise(w.id) is w


def test_get_or_raise_miss_raises_keyerror_with_id() -> None:
    coll = WireCollection([])
    missing = uuid.uuid4()
    with pytest.raises(KeyError) as ei:
        coll.get_or_raise(missing)
    assert missing in ei.value.args


def test_get_or_raise_miss_uses_exc_factory() -> None:
    coll = WireCollection([])
    missing = uuid.uuid4()
    with pytest.raises(ValueError, match="boom"):
        coll.get_or_raise(missing, exc=lambda k: ValueError(f"boom {k}"))


def test_get_or_raise_miss_uses_exc_instance() -> None:
    coll = WireCollection([])
    with pytest.raises(RuntimeError, match="ready"):
        coll.get_or_raise(uuid.uuid4(), exc=RuntimeError("ready"))


def test_get_or_return_hit_returns_wire() -> None:
    w = _wire()
    coll = WireCollection([w])
    other = _wire()
    assert coll.get_or_return(w.id, other) is w


def test_get_or_return_miss_returns_eager_value() -> None:
    coll = WireCollection([])
    fallback = _wire()
    assert coll.get_or_return(uuid.uuid4(), fallback) is fallback


def test_get_or_return_miss_calls_factory_with_id() -> None:
    coll = WireCollection([])
    missing = uuid.uuid4()
    seen: list[uuid.UUID] = []

    def make(k: uuid.UUID) -> Wire:
        seen.append(k)
        return _wire()

    coll.get_or_return(missing, make)
    assert seen == [missing]
