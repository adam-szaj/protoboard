# SPDX-License-Identifier: MIT
"""Wire / Fragment / KnotRef — domain model.

A Wire is a single solder-able artifact connecting exactly two Knots
(endpoint hubs — either a component pin or a t-joint).  It is composed of
one or more Fragments.  Each Fragment is a contiguous sub-path on a single
board side (top/bottom); fragment boundaries appear only where the wire
transitions sides through a plated-through pad (a "via").

`insulated` is per-wire: an insulated jumper that crosses to the other
side via a plated-through pad is one solder-able artifact and stays
insulated end-to-end.

The legacy ``WireSegment`` dataclass is preserved as a compat shim used by
the v1 persistence loader; consumers that operate on the new model use
``Wire``.  ``WireSegment`` will be removed in Commit B once all consumers
have migrated.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from protoboard.geometry import Point

Side = Literal["top", "bottom"]


@dataclass(frozen=True, slots=True)
class Fragment:
    """A maximal same-side sub-path of a Wire.

    The first point of fragment N+1 must equal the last point of fragment N
    (the via point on a plated-through pad).  Side is per-fragment; the
    wire's ``insulated`` flag applies to the whole wire.
    """

    path: tuple[Point, ...]
    side: Side

    def __post_init__(self) -> None:
        if self.side not in ("top", "bottom"):
            raise ValueError(f"side must be 'top' or 'bottom'; got {self.side!r}")
        if len(self.path) < 2:
            raise ValueError("Fragment.path must have at least 2 points")


@dataclass(frozen=True, slots=True)
class KnotRef:
    """Stable reference to a Knot living in Layout.knots."""

    id: uuid.UUID


@dataclass(frozen=True, slots=True)
class Wire:
    """A single electrical conductor between two Knots.

    Identity is by ``id`` — stable across path edits and reconstruction.
    Mutating operations construct a new Wire with the same id via
    ``dataclasses.replace``.

    Invariants (enforced by Layout mutators, not __post_init__):
      * ``fragments[0].path[0]`` is the world position of knot ``a``
      * ``fragments[-1].path[-1]`` is the world position of knot ``b``
      * For every consecutive fragment pair, the last point of N equals the
        first point of N+1 (shared via point)
    """

    id: uuid.UUID
    a: KnotRef
    b: KnotRef
    fragments: tuple[Fragment, ...]
    insulated: bool
    color: str | None = None

    def __post_init__(self) -> None:
        if not self.fragments:
            raise ValueError("Wire must have at least one fragment")
        # Chain check: each fragment boundary must coincide.
        for i in range(len(self.fragments) - 1):
            end = self.fragments[i].path[-1]
            start = self.fragments[i + 1].path[0]
            if end != start:
                raise ValueError(
                    f"Fragment boundary mismatch at index {i}: "
                    f"frag[{i}] ends at {end}, frag[{i + 1}] starts at {start}"
                )

    @property
    def path(self) -> list[Point]:
        """Flattened path across all fragments.

        Shared via points (end of N == start of N+1) appear once.  For a
        single-fragment wire this returns a copy of that fragment's path.
        """
        if len(self.fragments) == 1:
            return list(self.fragments[0].path)
        out: list[Point] = list(self.fragments[0].path)
        for frag in self.fragments[1:]:
            # Skip the duplicate boundary point.
            out.extend(frag.path[1:])
        return out

    @property
    def endpoints(self) -> tuple[Point, Point]:
        return self.fragments[0].path[0], self.fragments[-1].path[-1]

    @property
    def sides(self) -> frozenset[Side]:
        """Distinct sides traversed by this wire."""
        return frozenset(f.side for f in self.fragments)


# ---------------------------------------------------------------------------
# Legacy compat shim — used by v1 persistence loader and any consumer not yet
# migrated to the new model.  Will be removed in Commit B.
# ---------------------------------------------------------------------------


@dataclass
class WireSegment:
    """Legacy single-side wire record (pre-knot model).

    DEPRECATED: prefer ``Wire`` + ``Fragment``.  Kept only so the v1
    persistence loader can decode old project files before migration runs.
    """

    id: uuid.UUID
    path: list[Point]
    insulated: bool
    side: Side
    color: str | None = None

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, WireSegment):
            return NotImplemented
        return self.id == other.id

    def __post_init__(self) -> None:
        if self.side not in ("top", "bottom"):
            raise ValueError(f"side must be 'top' or 'bottom'; got {self.side!r}")
        if len(self.path) < 2:
            raise ValueError("WireSegment path must have at least 2 points")
