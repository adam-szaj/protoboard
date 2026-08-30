# SPDX-License-Identifier: MIT
"""Offset list normalisation — run-length and sugar forms → flat vectors.

The YAML for ``body_offset`` / ``board_offset`` accepts compact forms; this
module expands them to a flat ``list[Point]`` of N−1 consecutive step vectors
(see ``docs/footprints/package-model.md``). Pure functions, no Qt.

Accepted element forms in a raw offset list:
  * ``[count, [x, y]]`` — run-length: repeat vector ``[x, y]`` ``count`` times
    (second element is a list).
  * ``[x, y]``          — a single vector (second element is a number).

Distinguished by the *type* of the second element: list → run-length,
number → bare vector.

Sugar (spec §2.3): a 2-pin component may give its single pair as a bare
vector — ``body_offset: [1, 0]`` instead of ``[[1, 0]]``. A top-level list
whose *both* elements are numbers is unambiguously that form (a valid offset
list has sequence elements), so it expands to one step vector.
"""

from __future__ import annotations

from collections.abc import Sequence

from protoboard.geometry import Point, Vector, Vector3D

# A raw offset element is either a bare 2-vector [x, y] or a run-length
# pair [count, [x, y]]. We accept ints/floats and nested sequences.
RawOffsetElement = Sequence[object]
RawOffsetList = Sequence[RawOffsetElement]


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _as_vector(value: object) -> Vector:
    """Coerce a 2-element sequence of numbers into a Vector."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"expected [x, y] vector, got {value!r}")
    parts = list(value)
    if len(parts) != 2 or not all(_is_number(p) for p in parts):
        raise ValueError(f"expected [x, y] with two numbers, got {value!r}")
    return Vector(float(parts[0]), float(parts[1]))


def expand_offsets(raw: RawOffsetList | Sequence[float]) -> list[Vector]:
    """Expand a raw (possibly run-length) offset list to flat step vectors.

    Each output element is the step from pin *i* to pin *i+1*. Returns N−1
    vectors for N pins.

    >>> expand_offsets([[3, [0, 1]], [3, 0], [3, [0, -1]]])  # DIP-8
    [Vector(0.0, 1.0), Vector(0.0, 1.0), Vector(0.0, 1.0), Vector(3.0, 0.0), ...]
    """
    # Single-vector sugar: ``[x, y]`` (both elements numbers) is one pair —
    # the 2-pin shorthand the spec allows alongside the scalar form.
    if (
        isinstance(raw, Sequence)
        and not isinstance(raw, (str, bytes))
        and len(raw) == 2
        and all(_is_number(p) for p in raw)
    ):
        return [_as_vector(raw)]
    out: list[Vector] = []
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


def _as_vector3d(value: object) -> Vector3D:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"expected [x, y, z] direction, got {value!r}")
    parts = list(value)
    if len(parts) != 3 or not all(_is_number(p) for p in parts):
        raise ValueError(f"direction must be [x, y, z] of numbers, got {value!r}")
    return Vector3D(float(parts[0]), float(parts[1]), float(parts[2]))


def expand_direction(raw: object, pin_count: int) -> list[Vector3D]:
    """Expand ``direction`` (3 YAML forms) to one Vector3D per pin.

    1. single vector ``[x,y,z]``          → applied to all pins;
    2. list of N vectors                  → per pin;
    3. grouped ``[[indices], [x,y,z]]``…  → indices are 1-based pin numbers.
    """
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError(f"direction must be a list, got {raw!r}")
    items = list(raw)

    # Form 1: single [x,y,z] (three numbers).
    if len(items) == 3 and all(_is_number(i) for i in items):
        return [_as_vector3d(items)] * pin_count

    # Form 3: grouped — every element is [indices_list, vector_list].
    def _is_grouped_element(el: object) -> bool:
        return (
            isinstance(el, Sequence)
            and not isinstance(el, (str, bytes))
            and len(list(el)) == 2
            and isinstance(list(el)[0], Sequence)
            and isinstance(list(el)[1], Sequence)
        )

    if items and all(_is_grouped_element(el) for el in items):
        result: list[Vector3D | None] = [None] * pin_count
        for el in items:
            indices, vec = list(el)
            v = _as_vector3d(vec)
            for idx in indices:
                if not isinstance(idx, int) or isinstance(idx, bool):
                    raise ValueError(f"pin index must be int, got {idx!r}")
                if not 1 <= idx <= pin_count:
                    raise ValueError(f"pin index {idx} out of range 1..{pin_count}")
                result[idx - 1] = v
        missing = [i + 1 for i, r in enumerate(result) if r is None]
        if missing:
            raise ValueError(f"direction missing for pins {missing}")
        return [r for r in result if r is not None]

    # Form 2: list of N per-pin vectors.
    vectors = [_as_vector3d(el) for el in items]
    if len(vectors) != pin_count:
        raise ValueError(f"direction has {len(vectors)} vectors but {pin_count} pins")
    return vectors


def scale_offsets(vectors: Sequence[Vector], unit: float) -> list[Vector]:
    """Multiply each offset vector by ``unit`` (raster step → mm)."""
    return [Vector(v.dx * unit, v.dy * unit) for v in vectors]


def cumulative_positions(steps: Sequence[Vector]) -> list[Point]:
    """Turn N−1 step vectors into N absolute positions, pin 1 at (0, 0)."""
    positions = [Point(0.0, 0.0)]
    for step in steps:
        positions.append(positions[-1] + step)
    return positions
