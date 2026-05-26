# SPDX-License-Identifier: MIT
"""Wire / Fragment / KnotRef — domain model (mutable).

A Wire is a single solder-able artifact connecting exactly two Knots
(endpoint hubs — either a component pin or a t-joint).  It is composed of
one or more Fragments.  Each Fragment is a contiguous sub-path on a single
board side (top/bottom); fragment boundaries appear only where the wire
transitions sides through a plated-through pad (a "via").

`insulated` is per-wire: an insulated jumper that crosses to the other
side via a plated-through pad is one solder-able artifact and stays
insulated end-to-end.

`Wire` is **mutable** — `fragments` is a list and `Fragment` itself is a
mutable dataclass.  Identity is by ``id`` only; edits should preserve
``id`` to keep highlight, recording and undo invariants intact.  Use
``Layout.replace_wire_fragments`` or direct attribute assignment for path
edits.

Legacy ``WireSegment`` aliases ``Wire`` — both names refer to the same
mutable class; the alias is kept so the v1 persistence loader and any
not-yet-migrated consumer keep working unchanged.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal

from protoboard.geometry import Point

Side = Literal["top", "bottom"]


@dataclass
class Fragment:
    """A maximal same-side sub-path of a Wire.

    The first point of fragment N+1 must equal the last point of fragment N
    (the via point on a plated-through pad).  Mutable so per-fragment edits
    (translating path, flipping side) are cheap.
    """

    path: list[Point]
    side: Side

    def __post_init__(self) -> None:
        if self.side not in ("top", "bottom"):
            raise ValueError(f"side must be 'top' or 'bottom'; got {self.side!r}")
        # Accept any sequence on the way in, normalise to a mutable list.
        if not isinstance(self.path, list):
            self.path = list(self.path)
        if len(self.path) < 2:
            raise ValueError("Fragment.path must have at least 2 points")


@dataclass(frozen=True, slots=True)
class KnotRef:
    """Stable reference to a Knot living in Layout.knots."""

    id: uuid.UUID


def _flatten_fragments_list(fragments: list[Fragment]) -> list[Point]:
    """Concatenate fragments into a single path; duplicate via vertex dropped."""
    if not fragments:
        return []
    out: list[Point] = list(fragments[0].path)
    for frag in fragments[1:]:
        out.extend(frag.path[1:])
    return out


@dataclass
class Wire:
    """A single electrical conductor between two Knots.

    Identity is by ``id`` — stable across path edits and reconstruction.
    Mutable storage: ``fragments`` is a list; ``path`` is the flattened
    convenience view (and also the canonical storage for single-side
    wires, which is the common case during migration).

    The optional knot references ``a`` / ``b`` track endpoint hubs for
    consumers using the v2 wire model.  Legacy consumers can construct a
    Wire without explicit knots (a/b default to None) and operate on
    ``path``/``side``/``insulated`` just as they did with ``WireSegment``.

    Two construction styles are supported:

    * **Legacy** (WireSegment-compatible): ``Wire(id=..., path=..., side=...,
      insulated=...)`` — fragments default to a single same-side fragment.
    * **v2** (knot-aware): ``Wire(id=..., a=..., b=..., fragments=[...],
      insulated=...)`` — ``path`` and ``side`` are derived from fragments.
    """

    id: uuid.UUID
    path: list[Point] = field(default_factory=list)
    insulated: bool = False
    side: Side = "top"
    color: str | None = None
    fragments: list[Fragment] = field(default_factory=list)
    a: KnotRef | None = None
    b: KnotRef | None = None

    def __post_init__(self) -> None:
        if self.side not in ("top", "bottom"):
            raise ValueError(f"side must be 'top' or 'bottom'; got {self.side!r}")
        # Accept tuple/iterable for path too (legacy WireSegment took a list,
        # but v2 callers pass tuples).
        if self.path and not isinstance(self.path, list):
            self.path = list(self.path)
        if not isinstance(self.fragments, list):
            self.fragments = list(self.fragments)
        # v2-style construction: derive path/side from fragments if path
        # wasn't supplied.
        if self.fragments and not self.path:
            self.path = _flatten_fragments_list(self.fragments)
            self.side = self.fragments[0].side
        # Legacy-style construction: build a single fragment from path.
        if self.path and not self.fragments:
            self.fragments = [Fragment(path=list(self.path), side=self.side)]
        if len(self.path) < 2:
            raise ValueError("Wire.path must have at least 2 points")
        # Chain check: consecutive fragments must share their boundary vertex.
        for i in range(len(self.fragments) - 1):
            end = self.fragments[i].path[-1]
            start = self.fragments[i + 1].path[0]
            if end != start:
                raise ValueError(
                    f"Fragment boundary mismatch at index {i}: "
                    f"frag[{i}] ends at {end}, frag[{i + 1}] starts at {start}"
                )

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Wire):
            return NotImplemented
        return self.id == other.id

    @property
    def endpoints(self) -> tuple[Point, Point]:
        return self.path[0], self.path[-1]

    @property
    def sides(self) -> frozenset[Side]:
        """Distinct sides traversed by this wire (per-fragment)."""
        if self.fragments:
            return frozenset(f.side for f in self.fragments)
        return frozenset({self.side})


# Legacy alias — ``WireSegment`` and ``Wire`` are the same class now.  Code
# that imported ``WireSegment`` keeps working unchanged; the alias will be
# removed once all consumers update their imports (Cutover Step 3).
WireSegment = Wire
