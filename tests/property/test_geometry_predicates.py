"""Property checks for pure segment predicates."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from protoboard.geometry import Point, segments_cross_strict

_COORDINATES = st.floats(
    min_value=-100.0,
    max_value=100.0,
    allow_nan=False,
    allow_infinity=False,
)
_POINTS = st.builds(Point, x=_COORDINATES, y=_COORDINATES)


@pytest.mark.property
@given(a1=_POINTS, a2=_POINTS, b1=_POINTS, b2=_POINTS)
def test_strict_crossing_is_symmetric_under_segment_order(
    a1: Point, a2: Point, b1: Point, b2: Point
) -> None:
    result = segments_cross_strict(a1, a2, b1, b2)
    assert result == segments_cross_strict(b1, b2, a1, a2)
