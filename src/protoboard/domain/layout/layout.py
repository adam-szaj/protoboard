# SPDX-License-Identifier: MIT
"""Layout — physical realisation: placements, wires, knots, groups."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace

import structlog
from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.knot import JointKnot, Knot, PinKnot
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import (
    Fragment, Wire, WireCollection, WireSegment,
)
from protoboard.domain.schematic.net import PinRef
from protoboard.geometry import Point

PinResolver = Callable[[PinRef], Point]

ROOT_GROUP_ID: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")

_log = structlog.get_logger(__name__)


@dataclass
class Layout:
    placements: dict[uuid.UUID, ComponentPlacement] = field(default_factory=dict)
    # Dict-keyed wire storage with list-like API (see ``WireCollection`` for
    # the migration shim).  Legacy ``for w in layout.wires``,
    # ``layout.wires.append(w)`` and ``layout.wires.remove(w)`` keep working;
    # v2 consumers can use ``layout.wires.get(id)`` / ``.values()`` /
    # ``.items()`` directly.  Constructor still accepts a ``list[Wire]``
    # (or list[WireSegment], same class) via ``__post_init__`` normalisation.
    wires: WireCollection = field(default_factory=WireCollection)

    def __post_init__(self) -> None:
        # Normalise list-style construction:
        # ``Layout(wires=[w1, w2])`` from existing tests stays valid.
        # mypy thinks the isinstance branches are unreachable (field is typed
        # as WireCollection), but Python's dataclass __init__ accepts any
        # value matching no type-check at runtime.
        if not isinstance(self.wires, WireCollection):
            self.wires = WireCollection(list(self.wires))
        if not isinstance(self.wires_v2, WireCollection):
            self.wires_v2 = WireCollection(list(self.wires_v2))
    groups: dict[uuid.UUID, ComponentGroup] = field(default_factory=dict)

    # Knot storage — endpoint hubs for wires.  Populated by v2 commands and
    # by the migration in ``services/persistence/wire_migration.py``.
    knots: dict[uuid.UUID, Knot] = field(default_factory=dict)
    # Index for fast pin -> knot lookup (one PinKnot per PinRef).
    pin_knots: dict[PinRef, uuid.UUID] = field(default_factory=dict)
    # ``wires_v2`` is a backward-compat view on top of ``wires`` so any
    # remaining v2-aware code that still references it sees the same data.
    # Removal scheduled for Cutover Step 3.
    wires_v2: WireCollection = field(default_factory=WireCollection)

    # Resolver for PinKnot world positions.  ``None`` until the caller
    # registers one via ``set_pin_resolver``; required by mutators that
    # validate fragment endpoints against knot positions.
    _pin_resolver: PinResolver | None = field(default=None, repr=False)

    def set_pin_resolver(self, resolver: PinResolver) -> None:
        self._pin_resolver = resolver

    def ensure_root_group(self) -> None:
        """Ensure root group exists and all non-root groups are reachable from it.

        Called after loading from disk. Adopts any orphan groups (groups not
        referenced as a child by any other group) into root's child_group_ids so
        that the SelectTool can find them via _hit_test_group_outline.
        """
        if ROOT_GROUP_ID not in self.groups:
            self.groups[ROOT_GROUP_ID] = ComponentGroup(
                id=ROOT_GROUP_ID,
                name="",
                component_ids=frozenset(),
                child_group_ids=frozenset(),
            )

        # Collect all group IDs that are referenced as children by some group.
        referenced: set[uuid.UUID] = set()
        for g in self.groups.values():
            referenced |= set(g.child_group_ids)

        # Any non-root group not referenced by anyone is an orphan → attach to root.
        orphans = frozenset(
            gid for gid in self.groups
            if gid != ROOT_GROUP_ID and gid not in referenced
        )
        if orphans:
            _log.warning(
                "layout.orphan_groups_adopted",
                count=len(orphans),
                ids=[str(g) for g in orphans],
            )
            root = self.groups[ROOT_GROUP_ID]
            self.groups[ROOT_GROUP_ID] = ComponentGroup(
                id=root.id,
                name=root.name,
                component_ids=root.component_ids,
                child_group_ids=root.child_group_ids | orphans,
                exported_pins=root.exported_pins,
            )

    # ------------------------------------------------------------------
    # Knot / Wire mutators (new model, used by v2 commands and migration)
    # ------------------------------------------------------------------

    def get_or_create_pin_knot(self, pin: PinRef) -> PinKnot:
        """Return the PinKnot for ``pin``, creating one on first lookup.

        Each ``PinRef`` maps to exactly one ``PinKnot``; the index
        ``pin_knots`` is the source of truth.
        """
        existing_id = self.pin_knots.get(pin)
        if existing_id is not None:
            knot = self.knots[existing_id]
            assert isinstance(knot, PinKnot)
            return knot
        knot = PinKnot(id=uuid.uuid4(), pin=pin)
        self.knots[knot.id] = knot
        self.pin_knots[pin] = knot.id
        return knot

    def create_joint_knot(self, position: Point) -> JointKnot:
        """Create a fresh JointKnot at ``position`` and register it.

        Callers must ensure ``position`` lies on a board pad (validated by
        DRC, not here — the domain model trusts its constructors).
        """
        knot = JointKnot(id=uuid.uuid4(), position=position)
        self.knots[knot.id] = knot
        return knot

    def add_wire_v2(self, wire: Wire) -> None:
        """Insert ``wire`` into ``wires_v2`` and update knot back-refs.

        Both endpoint knots must already exist in ``self.knots``.
        """
        if wire.id in self.wires_v2:
            raise ValueError(f"Wire {wire.id} already in layout")
        if wire.a is None or wire.b is None:
            raise ValueError(
                f"Wire {wire.id} needs both endpoint knots (a/b) for v2 add"
            )
        for ref in (wire.a, wire.b):
            knot = self.knots.get(ref.id)
            if knot is None:
                raise ValueError(f"Wire endpoint knot {ref.id} not in layout.knots")
            self.knots[ref.id] = _knot_with_wire(knot, wire.id, add=True)
        self.wires_v2[wire.id] = wire

    def remove_wire_v2(self, wire_id: uuid.UUID) -> list[uuid.UUID]:
        """Remove a wire and clean up orphan JointKnots.

        Returns the list of knot ids that were removed because they became
        orphan (only ``JointKnot``s — ``PinKnot``s survive even with zero
        wires, since the pin itself still exists).
        """
        wire = self.wires_v2.pop(wire_id, None)
        if wire is None:
            raise KeyError(f"Wire {wire_id} not in layout")
        removed: list[uuid.UUID] = []
        refs = [r for r in (wire.a, wire.b) if r is not None]
        for ref in refs:
            knot = self.knots.get(ref.id)
            if knot is None:
                continue
            updated = _knot_with_wire(knot, wire_id, add=False)
            if isinstance(updated, JointKnot) and not updated.wire_ids:
                del self.knots[ref.id]
                removed.append(ref.id)
            else:
                self.knots[ref.id] = updated
        return removed

    def replace_wire_fragments(
        self,
        wire_id: uuid.UUID,
        fragments: list[Fragment] | tuple[Fragment, ...],
    ) -> Wire:
        """Atomically replace a wire's fragments, preserving its id.

        This is the central fix for the segment-proliferation bug: edits
        that change the path of a wire never mint a new id.  The wire's
        ``path`` attribute is rebuilt from the new fragments so legacy
        consumers (which read ``wire.path``) see the updated geometry.
        """
        existing = self.wires_v2.get(wire_id)
        if existing is None:
            raise KeyError(f"Wire {wire_id} not in layout")
        frag_list = list(fragments)
        if not frag_list:
            raise ValueError("fragments must not be empty")
        # Validate endpoints against knots before mutating.
        a_pos = frag_list[0].path[0]
        b_pos = frag_list[-1].path[-1]
        if existing.a is None or existing.b is None:
            raise ValueError("replace_wire_fragments requires a Wire with both knots")
        a_knot = self.knots[existing.a.id]
        b_knot = self.knots[existing.b.id]
        if _knot_position(a_knot, self) != a_pos:
            raise ValueError(
                f"replace_wire_fragments: new path[0]={a_pos} disagrees with "
                f"knot a position {_knot_position(a_knot, self)}"
            )
        if _knot_position(b_knot, self) != b_pos:
            raise ValueError(
                f"replace_wire_fragments: new path[-1]={b_pos} disagrees with "
                f"knot b position {_knot_position(b_knot, self)}"
            )
        # Mutate in place — Wire is mutable now, identity is by id.
        existing.fragments = frag_list
        existing.path = _flatten_fragments(frag_list)
        existing.side = frag_list[0].side
        return existing

    def try_collapse_joint(
        self,
        knot_id: uuid.UUID,
        *,
        plated_through_pads: frozenset[Point] | None = None,
    ) -> Wire | None:
        """If a JointKnot now has exactly 2 incident wires, try to merge them.

        Auto-merge succeeds iff:
          * both wires have the same ``insulated`` flag, AND
          * either the side of the fragment ending at the joint in wire1
            equals the side of the fragment starting at the joint in
            wire2 (no implicit via at the joint position), OR the joint's
            position is a plated-through pad (a real via that bridges
            top↔bottom electrically).

        ``plated_through_pads`` is consulted only for the side-mismatch
        case; callers can omit it to enforce strict same-side merging.

        On success the joint is removed and a single merged Wire replaces
        the two; on failure (or if the knot is a PinKnot, or has !=2
        wires), the layout is unchanged and ``None`` is returned.
        """
        joint = self.knots.get(knot_id)
        if not isinstance(joint, JointKnot):
            return None
        if len(joint.wire_ids) != 2:
            return None
        w1_id, w2_id = tuple(joint.wire_ids)
        w1 = self.wires_v2[w1_id]
        w2 = self.wires_v2[w2_id]
        if w1.insulated != w2.insulated:
            return None
        # Orient w1 so its `b` is the joint; orient w2 so its `a` is the joint.
        w1o = _orient_wire_toward(w1, knot_id, end_at_b=True)
        w2o = _orient_wire_toward(w2, knot_id, end_at_b=False)
        if w1o is None or w2o is None:
            return None
        last = w1o.fragments[-1]
        first = w2o.fragments[0]
        sides_match = last.side == first.side
        is_plated = (
            plated_through_pads is not None
            and joint.position in plated_through_pads
        )
        if not sides_match and not is_plated:
            return None
        merged_fragments: list[Fragment] = list(w1o.fragments[:-1])
        if sides_match:
            # Coalesce into one fragment — the boundary vertex is interior.
            coalesced = Fragment(
                path=list(last.path) + list(first.path[1:]),
                side=last.side,
            )
            merged_fragments.append(coalesced)
            merged_fragments.extend(w2o.fragments[1:])
        else:
            # Cross via: keep two adjacent fragments, joint vertex shared.
            merged_fragments.append(last)
            merged_fragments.append(first)
            merged_fragments.extend(w2o.fragments[1:])
        # Build the new wire keeping the lowest UUID (deterministic).
        winning_id = min(w1.id, w2.id)
        assert w1o.a is not None and w2o.b is not None
        flat = _flatten_fragments(merged_fragments)
        merged = Wire(
            id=winning_id,
            path=flat,
            insulated=w1.insulated,
            side=merged_fragments[0].side,
            color=w1.color if w1.color is not None else w2.color,
            fragments=merged_fragments,
            a=w1o.a,
            b=w2o.b,
        )
        # Apply: remove both wires (this will also try to clean up the
        # joint, which we know is now empty), then add the merged wire.
        # We don't use remove_wire_v2 because we want exact control.
        self.wires_v2.pop(w1.id, None)
        self.wires_v2.pop(w2.id, None)
        # Detach back-refs from the joint and from the remaining endpoints.
        for w in (w1, w2):
            for ref in (w.a, w.b):
                if ref is None:
                    continue
                k = self.knots.get(ref.id)
                if k is None:
                    continue
                self.knots[ref.id] = _knot_with_wire(k, w.id, add=False)
        # Joint should be empty now — drop it.
        joint_after = self.knots.get(knot_id)
        if isinstance(joint_after, JointKnot) and not joint_after.wire_ids:
            del self.knots[knot_id]
        # Add merged.
        self.add_wire_v2(merged)
        return merged


# ----------------------------------------------------------------------
# Internal helpers (module-private)
# ----------------------------------------------------------------------


def _knot_with_wire(knot: Knot, wire_id: uuid.UUID, *, add: bool) -> Knot:
    """Return a copy of ``knot`` with ``wire_id`` added/removed from wire_ids."""
    if add:
        new_ids = knot.wire_ids | {wire_id}
    else:
        new_ids = knot.wire_ids - {wire_id}
    if isinstance(knot, PinKnot):
        return PinKnot(id=knot.id, pin=knot.pin, wire_ids=new_ids)
    if isinstance(knot, JointKnot):
        return JointKnot(id=knot.id, position=knot.position, wire_ids=new_ids)
    raise TypeError(f"unknown Knot subtype: {type(knot).__name__}")


def _knot_position(knot: Knot, layout: Layout) -> Point:
    """Resolve a knot's world position (PinKnot is resolved lazily).

    For ``PinKnot``s, the caller must register a resolver via
    ``Layout.set_pin_resolver``.  A missing resolver is a programmer error
    and raises ``RuntimeError``.
    """
    if isinstance(knot, JointKnot):
        return knot.position
    if isinstance(knot, PinKnot):
        resolver = layout._pin_resolver
        if resolver is None:
            raise RuntimeError(
                "PinKnot position requested but no pin resolver registered on "
                "Layout (call layout.set_pin_resolver(...) after construction)"
            )
        return resolver(knot.pin)
    raise TypeError(f"unknown Knot subtype: {type(knot).__name__}")


def _orient_wire_toward(
    wire: Wire,
    knot_id: uuid.UUID,
    *,
    end_at_b: bool,
) -> Wire | None:
    """Return a possibly-reversed wire so the joint is at the chosen end.

    If ``end_at_b`` is True, the returned wire has ``b.id == knot_id``;
    otherwise ``a.id == knot_id``.  Returns ``None`` if the joint is not
    one of the wire's endpoints.
    """
    if wire.a is None or wire.b is None:
        return None
    if wire.a.id == knot_id and not end_at_b:
        return wire
    if wire.b.id == knot_id and end_at_b:
        return wire
    if wire.a.id == knot_id and end_at_b:
        return _reverse_wire(wire)
    if wire.b.id == knot_id and not end_at_b:
        return _reverse_wire(wire)
    return None


def _flatten_fragments(fragments: list[Fragment]) -> list[Point]:
    """Concatenate fragments into a single path, dropping shared vias."""
    if not fragments:
        return []
    out: list[Point] = list(fragments[0].path)
    for frag in fragments[1:]:
        out.extend(frag.path[1:])
    return out


def _reverse_wire(wire: Wire) -> Wire:
    """Return ``wire`` with endpoints + fragments + per-fragment path reversed."""
    reversed_fragments = [
        Fragment(path=list(reversed(f.path)), side=f.side)
        for f in reversed(wire.fragments)
    ]
    return replace(wire, a=wire.b, b=wire.a, fragments=reversed_fragments)
