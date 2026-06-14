# SPDX-License-Identifier: MIT
"""Offset list normalisation — run-length and sugar forms → flat vectors.

The YAML for ``body_offset`` / ``board_offset`` accepts compact forms; this
module expands them to a flat ``list[Point]`` of N−1 consecutive step vectors
(see ``docs/footprints/spec.md``). Pure functions, no Qt.

Accepted element forms in a raw offset list:
  * ``[count, [x, y]]`` — run-length: repeat vector ``[x, y]`` ``count`` times
    (second element is a list).
  * ``[x, y]``          — a single vector (second element is a number).

Distinguished by the *type* of the second element: list → run-length,
number → bare vector.
"""

from __future__ import annotations

from collections.abc import Sequence

from protoboard.geometry import Point

# A raw offset element is either a bare 2-vector [x, y] or a run-length
# pair [count, [x, y]]. We accept ints/floats and nested sequences.
RawOffsetElement = Sequence[object]
RawOffsetList = Sequence[RawOffsetElement]


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _as_vector(value: object) -> Point:
    """Coerce a 2-element sequence of numbers into a Point."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"expected [x, y] vector, got {value!r}")
    parts = list(value)
    if len(parts) != 2 or not all(_is_number(p) for p in parts):
        raise ValueError(f"expected [x, y] with two numbers, got {value!r}")
    return Point(float(parts[0]), float(parts[1]))


def expand_offsets(raw: RawOffsetList) -> list[Point]:
    """Expand a raw (possibly run-length) offset list to flat step vectors.

    Each output element is the step from pin *i* to pin *i+1*. Returns N−1
    vectors for N pins.

    >>> expand_offsets([[3, [0, 1]], [3, 0], [3, [0, -1]]])  # DIP-8
    [Point(0.0, 1.0), Point(0.0, 1.0), Point(0.0, 1.0), Point(3.0, 0.0), ...]
    """
    out: list[Point] = []
    for element in raw:
        if not isinstance(element, Sequence) or isinstance(element, (str, bytes)):
            raise ValueError(f"offset element must be a list, got {element!r}")
        parts = list(element)
        if len(parts) != 2:
            raise ValueError(
                f"offset element must have 2 items ([x,y] or [count,[x,y]]), "
                f"got {element!r}"
            )
        second = parts[1]
        if _is_number(second):
            # Bare vector [x, y].
            out.append(_as_vector(element))
        else:
            # Run-length [count, [x, y]].
            count = parts[0]
            if not isinstance(count, int) or isinstance(count, bool) or count < 1:
                raise ValueError(f"run-length count must be int >= 1, got {count!r}")
            vec = _as_vector(second)
            out.extend([vec] * count)
    return out


def scale_offsets(vectors: Sequence[Point], unit: float) -> list[Point]:
    """Multiply each offset vector by ``unit`` (raster step → mm)."""
    return [Point(v.x * unit, v.y * unit) for v in vectors]


def cumulative_positions(steps: Sequence[Point]) -> list[Point]:
    """Turn N−1 step vectors into N absolute positions, pin 1 at (0, 0)."""
    positions = [Point(0.0, 0.0)]
    for step in steps:
        positions.append(positions[-1] + step)
    return positions
