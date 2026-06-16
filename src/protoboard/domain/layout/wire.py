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


def flatten_fragments(fragments: list[Fragment]) -> list[Point]:
    """Concatenate fragments into a single path; shared via vertex dropped once.

    The first point of fragment N+1 equals the last point of fragment N (the
    via point on a plated-through pad), so each fragment after the first
    contributes its path minus that shared leading vertex. The single home for
    this operation — ``wire_utils`` and ``layout`` import it as
    ``_flatten_fragments``.
    """
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

    The optional knot references ``a`` / ``b`` track endpoint hubs.  A
    Wire without explicit knots (a/b default to None) still works for any
    consumer that only needs ``path``/``side``/``insulated``.

    Two construction styles are supported:

    * **Path-style**: ``Wire(id=..., path=..., side=..., insulated=...)``
      — fragments default to a single same-side fragment.
    * **Fragment-style** (knot-aware): ``Wire(id=..., a=..., b=...,
      fragments=[...], insulated=...)`` — ``path`` and ``side`` are
      derived from the fragments.
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
        # Accept tuple/iterable for path; normalise to a mutable list.
        if self.path and not isinstance(self.path, list):
            self.path = list(self.path)
        if not isinstance(self.fragments, list):
            self.fragments = list(self.fragments)
        # v2-style construction: derive path/side from fragments if path
        # wasn't supplied.
        if self.fragments and not self.path:
            self.path = flatten_fragments(self.fragments)
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


class WireCollection:
    """Dict-keyed wire storage with list-like semantics for migration.

    Internally a ``dict[UUID, Wire]`` keyed by wire id (insertion-ordered
    in Python 3.7+).  Exposes both:

    * **List-style API** for consumers that haven't migrated yet:
      iteration, ``append``, ``remove``, ``len``, ``in``, indexing,
      slice assignment (``wires[:] = new_list``).
    * **Dict-style API** for v2-aware consumers: ``get(id)``, ``items()``,
      ``values()``, ``keys()``, ``[id]`` lookup.

    Identity is by wire id; assigning the same wire twice updates in
    place (no duplicate keys).  Removing a wire whose id isn't in the
    collection is a no-op (matches ``list.remove`` only when the wire was
    constructed and never inserted — most call sites use this pattern
    via ``layout.wires.remove(wire)``).
    """

    __slots__ = ("_by_id",)

    def __init__(self, initial: list[Wire] | None = None) -> None:
        self._by_id: dict[uuid.UUID, Wire] = {}
        if initial:
            for w in initial:
                self._by_id[w.id] = w

    # ----- list-style API -----

    def __iter__(self) -> "_WireCollectionIter":
        return _WireCollectionIter(iter(self._by_id.values()))

    def __len__(self) -> int:
        return len(self._by_id)

    def __contains__(self, item: object) -> bool:
        if isinstance(item, Wire):
            return item.id in self._by_id
        if isinstance(item, uuid.UUID):
            return item in self._by_id
        return False

    def __bool__(self) -> bool:
        return bool(self._by_id)

    def __getitem__(self, key: object) -> "Wire":
        # Support both index-style (rare) and id-style lookup.
        if isinstance(key, int):
            try:
                return list(self._by_id.values())[key]
            except IndexError as exc:
                raise IndexError(f"WireCollection index {key} out of range") from exc
        if isinstance(key, uuid.UUID):
            return self._by_id[key]
        raise TypeError(f"WireCollection indices must be int or UUID, got {type(key).__name__}")

    def __setitem__(self, key: object, value: object) -> None:
        # `wires[:] = new_list` — slice assignment replaces the whole collection.
        if isinstance(key, slice) and key == slice(None, None, None):
            if not isinstance(value, list):
                raise TypeError("Only `wires[:] = list[Wire]` is supported")
            self._by_id = {w.id: w for w in value}
            return
        # `wires[i] = wire` for legacy code that indexes by position.
        if isinstance(key, int):
            keys = list(self._by_id.keys())
            if not isinstance(value, Wire):
                raise TypeError("Value must be a Wire")
            if key < 0 or key >= len(keys):
                raise IndexError("WireCollection index out of range")
            # Replace at position: remove the old key, insert the new wire
            # at the same ordinal slot.  Build a new ordered dict.
            new_dict: dict[uuid.UUID, Wire] = {}
            for i, k in enumerate(keys):
                if i == key:
                    new_dict[value.id] = value
                else:
                    new_dict[k] = self._by_id[k]
            self._by_id = new_dict
            return
        if isinstance(key, uuid.UUID):
            if not isinstance(value, Wire):
                raise TypeError("Value must be a Wire")
            self._by_id[key] = value
            return
        raise TypeError(f"WireCollection keys must be slice/int/UUID, got {type(key).__name__}")

    def append(self, wire: Wire) -> None:
        self._by_id[wire.id] = wire

    def remove(self, wire: Wire) -> None:
        self._by_id.pop(wire.id, None)

    def extend(self, wires: object) -> None:
        try:
            iterator = iter(wires)  # type: ignore[call-overload]
        except TypeError as exc:
            raise TypeError("WireCollection.extend requires an iterable") from exc
        for w in iterator:
            if not isinstance(w, Wire):
                raise TypeError("WireCollection.extend got a non-Wire item")
            self._by_id[w.id] = w

    def clear(self) -> None:
        self._by_id.clear()

    # ----- dict-style API -----

    def get(self, wire_id: uuid.UUID, default: "Wire | None" = None) -> "Wire | None":
        return self._by_id.get(wire_id, default)

    def values(self) -> "list[Wire]":
        return list(self._by_id.values())

    def keys(self) -> "list[uuid.UUID]":
        return list(self._by_id.keys())

    def items(self) -> "list[tuple[uuid.UUID, Wire]]":
        return list(self._by_id.items())

    def pop(self, wire_id: uuid.UUID, default: "Wire | None" = None) -> "Wire | None":
        return self._by_id.pop(wire_id, default)

    # ----- comparisons -----

    def __eq__(self, other: object) -> bool:
        # Equal to another collection of the same wires (by id, insertion
        # order).  Also compares element-wise against a list, so legacy
        # assertions like ``assert layout.wires == []`` keep working.
        if isinstance(other, WireCollection):
            return list(self._by_id.values()) == list(other._by_id.values())
        if isinstance(other, list):
            return list(self._by_id.values()) == other
        return NotImplemented

    # ----- diagnostics -----

    def __repr__(self) -> str:
        return f"WireCollection(n={len(self._by_id)})"


from collections.abc import Iterator as _Iterator


class _WireCollectionIter:
    """Iterator returning Wire instances in insertion order."""

    __slots__ = ("_it",)

    def __init__(self, it: _Iterator[Wire]) -> None:
        self._it: _Iterator[Wire] = it

    def __iter__(self) -> "_WireCollectionIter":
        return self

    def __next__(self) -> "Wire":
        return next(self._it)
