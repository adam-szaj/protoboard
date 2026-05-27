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
    Fragment, Wire, WireCollection,
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
    # (or list[Wire], same class) via ``__post_init__`` normalisation.
    wires: WireCollection = field(default_factory=WireCollection)
    groups: dict[uuid.UUID, ComponentGroup] = field(default_factory=dict)

    # Knot storage — endpoint hubs for wires.  Populated by v2 commands and
    # by the migration in ``services/persistence/wire_migration.py``.
    knots: dict[uuid.UUID, Knot] = field(default_factory=dict)
    # Index for fast pin -> knot lookup (one PinKnot per PinRef).
    pin_knots: dict[PinRef, uuid.UUID] = field(default_factory=dict)

    # Resolver for PinKnot world positions.  ``None`` until the caller
    # registers one via ``set_pin_resolver``; required by mutators that
    # validate fragment endpoints against knot positions.
    _pin_resolver: PinResolver | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # Normalise list-style construction:
        # ``Layout(wires=[w1, w2])`` from existing tests stays valid.
        if not isinstance(self.wires, WireCollection):
            self.wires = WireCollection(list(self.wires))

    @property
    def wires_v2(self) -> WireCollection:
        """Backward-compat alias — same storage as ``wires``.

        Kept so consumers still using ``layout.wires_v2`` (commands, tests
        from the cutover transition period) don't break.  Removal scheduled
        once all callers migrate to ``wires``.
        """
        return self.wires

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
        if wire.id in self.wires:
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
        self.wires[wire.id] = wire

    def remove_wire_v2(self, wire_id: uuid.UUID) -> list[uuid.UUID]:
        """Remove a wire and clean up orphan JointKnots.

        Returns the list of knot ids that were removed because they became
        orphan (only ``JointKnot``s — ``PinKnot``s survive even with zero
        wires, since the pin itself still exists).
        """
        wire = self.wires.pop(wire_id, None)
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

    def ensure_wire_knots(self, wire: Wire) -> None:
        """Backfill ``wire.a``/``wire.b`` from current endpoints if missing.

        Tools that mutate a wire's fragments via
        ``replace_wire_fragments`` require both endpoint knots to be set.
        Most runtime wires get knots from ``AddWireCommand`` or the load
        migration; this method handles wires inserted into ``layout.wires``
        without going through those paths (test fixtures, legacy code).

        Each missing endpoint is matched against existing ``PinKnot``s by
        world position (within ``1e-6``); if no pin matches, a fresh
        ``JointKnot`` is created at the endpoint.  Knot back-refs
        (``wire_ids``) are updated atomically.
        """
        if wire.a is not None and wire.b is not None:
            return
        endpoints = (
            ("a", wire.path[0]),
            ("b", wire.path[-1]),
        )
        for attr, pos in endpoints:
            if getattr(wire, attr) is not None:
                continue
            matched_id: uuid.UUID | None = None
            if self._pin_resolver is not None:
                for knot_id in self.pin_knots.values():
                    pk = self.knots.get(knot_id)
                    if not isinstance(pk, PinKnot):
                        continue
                    try:
                        pin_pos = self._pin_resolver(pk.pin)
                    except RuntimeError:
                        continue
                    if (
                        abs(pin_pos.x - pos.x) < 1e-6
                        and abs(pin_pos.y - pos.y) < 1e-6
                    ):
                        matched_id = knot_id
                        break
            if matched_id is None:
                jk = self.create_joint_knot(pos)
                matched_id = jk.id
            from protoboard.domain.layout.wire import KnotRef as _KnotRef
            setattr(wire, attr, _KnotRef(id=matched_id))
        for ref in (wire.a, wire.b):
            if ref is None:
                continue
            k = self.knots.get(ref.id)
            if k is None or wire.id in k.wire_ids:
                continue
            self.knots[ref.id] = _knot_with_wire(k, wire.id, add=True)

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
        existing = self.wires.get(wire_id)
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
        # Endpoint validation.  A ``JointKnot`` whose only incident wire is
        # the one being edited is free to follow the wire — wire-only drag
        # moves a free joint along with its sole wire.  Shared joints (>=2
        # incident wires) must not move silently because that would shift
        # other wires too; ditto PinKnots whose position comes from the
        # placement, not the joint.
        def _resolve_endpoint(
            knot: Knot, wire_id_: uuid.UUID, new_pos: Point, label: str,
        ) -> None:
            cur = _knot_position(knot, self)
            if cur == new_pos:
                return
            if isinstance(knot, JointKnot) and knot.wire_ids == frozenset({wire_id_}):
                # Free joint — slide along with the wire.
                knot.position = new_pos
                return
            raise ValueError(
                f"replace_wire_fragments: new {label}={new_pos} disagrees with "
                f"knot {label[-1]} position {cur}"
            )
        _resolve_endpoint(a_knot, wire_id, a_pos, "path[0]")
        _resolve_endpoint(b_knot, wire_id, b_pos, "path[-1]")
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
        w1 = self.wires[w1_id]
        w2 = self.wires[w2_id]
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
        self.wires.pop(w1.id, None)
        self.wires.pop(w2.id, None)
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
