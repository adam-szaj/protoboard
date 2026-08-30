# SPDX-License-Identifier: MIT
"""Group ownership classification — net-aware ``wire ∈ group?`` predicate.

Single source of truth for the question *"does this wire structurally
belong to this group?"*.  Pure functions, no Qt, no side-effects — safe
to call from domain code, GUI, and tests alike.

Classification rules (derived from the pad/pin/wire/junction semantics
discussion 2026-06-01):

A wire is **structurally owned by a group** iff:

1. Both endpoints are :class:`PinKnot`s anchored to pins of components
   inside the group, **or**
2. One endpoint is a PinKnot of a group component and the other is a
   :class:`JointKnot` (orphan tail), **and** the wire's resolved net is
   *internal* to the group (no pin of that net is exported).

A wire is **not owned** when:

* Any endpoint is a PinKnot of a component outside the group (this also
  covers the DRC-short case "external pin glued onto an internal net";
  ownership classification stays clean — DRC reports the short).
* The wire's resolved net is exported (one or more pins of the net are
  in the group's ``exported_pins``) — half-orphan tails of exported nets
  are visible interface, not group internals.
* Both endpoints are JointKnots without any group-pin anchor — orphan
  fragments are never anyone's structural property.

Net is **internal to the group** iff:

* It has at least one pin on a component of the group, **and**
* No pin of that net appears in the group's ``exported_pins``, **and**
* It is not a global net (``Net.global_net``) — global nets (VCC/GND)
  always reach the world.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from protoboard.services.call_counters import counted

if TYPE_CHECKING:
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.layout.group import ComponentGroup
    from protoboard.domain.layout.knot import Knot
    from protoboard.domain.layout.placement import (
        PlacementMap,
    )
    from protoboard.domain.layout.wire import Wire
    from protoboard.domain.schematic.schematic import Schematic


__all__ = [
    "endpoint_pin_component",
    "net_is_internal_to_group",
    "wire_is_owned_by_group",
    "wire_in_group_bbox",
    "group_bbox_with_owned_wires",
    "find_owned_wires",
    "OwnershipResolver",
]


# Tolerance for PinKnot path-vs-pin-position sanity check.  Pad centres
# are aligned to integer + 0.5 by convention; anything beyond half a
# raster unit is a stale-path indicator.
_PIN_PATH_MATCH_TOL = 0.4


@counted("find_owned_wires")
def find_owned_wires(
    wires: list[Wire],
    group: ComponentGroup,
    *,
    wire_net_map: dict[uuid.UUID, uuid.UUID],
    knots: dict[uuid.UUID, Knot],
    group_comp_ids: frozenset[uuid.UUID],
    schematic: Schematic | None,
    placements: PlacementMap | None = None,
    instances: dict[uuid.UUID, ComponentInstance] | None = None,
    definitions: dict[str, ComponentDefinition] | None = None,
) -> set[uuid.UUID]:
    """Return the IDs of all wires structurally owned by *group*.

    Thin batch wrapper over :func:`wire_is_owned_by_group`.  When
    *placements*/*instances*/*definitions* are passed, the classifier
    also runs a geometric sanity check (path endpoint must match
    PinKnot's resolved world position) — used to filter out wires whose
    path was left stale by a buggy drag/load path.
    """
    # One shared cache for pin world positions across all wires in this
    # batch.  Placements/instances/definitions are constant within the
    # call, so resolving each (component, pin) once eliminates the
    # per-wire footprint walk that used to dominate refresh time when
    # many wires share endpoint components.  Empty when sanity check
    # is disabled (no placements/instances/definitions).
    pin_world_cache: dict[tuple[uuid.UUID, int], object] | None = None
    if placements is not None and instances is not None and definitions is not None:
        pin_world_cache = {}
    result: set[uuid.UUID] = set()
    for w in wires:
        if wire_is_owned_by_group(
            w,
            group,
            wire_net_map=wire_net_map,
            knots=knots,
            group_comp_ids=group_comp_ids,
            schematic=schematic,
            placements=placements,
            instances=instances,
            definitions=definitions,
            _pin_world_cache=pin_world_cache,
        ):
            result.add(w.id)
    return result


def net_is_internal_to_group(
    net_id: uuid.UUID | None,
    group: ComponentGroup,
    schematic: Schematic | None,
    groups: dict[uuid.UUID, ComponentGroup] | None = None,
) -> bool:
    """True iff *net_id* identifies a net living entirely inside *group*.

    Net is internal iff every pin of the net belongs to a component in
    the group's *recursive* membership AND none of those pins is exported
    AND the net is not global.

    When *groups* is provided the recursive set is computed via
    ``all_component_ids(groups, group.id)``.  Without it the check falls
    back to ``group.component_ids`` (direct members only) — accepted for
    flat-tree callers and existing tests, but unsafe for nested layouts:
    a net spanning the group and one of its grandchildren would be
    misclassified as external.  New callers in nested contexts should
    pass *groups* explicitly.
    """
    if net_id is None or schematic is None:
        return False
    net = schematic.nets.get(net_id)
    if net is None:
        return False
    if net.global_net:
        return False
    if not net.pins:
        return False
    if groups is not None:
        from protoboard.domain.layout.group import all_component_ids

        group_comp_ids = all_component_ids(groups, group.id)
    else:
        group_comp_ids = group.component_ids
    exported = {(ep.component_id, ep.pin_number) for ep in group.exported_pins}
    has_group_pin = False
    for pin in net.pins:
        if pin.component_id not in group_comp_ids:
            # Net touches a component outside the group.  If this happens
            # via a legal exported pin, the net is the group's external
            # interface; otherwise the layout has a DRC short.  Either
            # way the net is not internal.
            return False
        has_group_pin = True
        if (pin.component_id, pin.pin_number) in exported:
            return False
    return has_group_pin


@counted("endpoint_pin_component")
def endpoint_pin_component(
    knot: Knot | None,
) -> uuid.UUID | None:
    """Return ``knot.pin.component_id`` if knot is a PinKnot, else None.

    Public helper — both this module and
    :mod:`protoboard.domain.layout.drag_classification` need to decide
    whether a wire endpoint is anchored to a component pin and, if so,
    to which component.
    """
    from protoboard.domain.layout.knot import PinKnot

    if isinstance(knot, PinKnot):
        pin = knot.pin  # narrow for type-checkers that don't follow runtime_checkable
        return pin.component_id
    return None


# Module-private alias kept so the existing internal callers below need
# no edits; new code should use the public ``endpoint_pin_component``.
_endpoint_pin_component = endpoint_pin_component


@counted("_pin_endpoint_matches_path")
def _pin_endpoint_matches_path(
    knot: Knot,
    wire_endpoint: object,
    placements: PlacementMap,
    instances: dict[uuid.UUID, ComponentInstance],
    definitions: dict[str, ComponentDefinition],
    pin_world_cache: dict[tuple[uuid.UUID, int], object] | None = None,
) -> bool:
    """True iff *knot* is a PinKnot whose resolved world position matches
    *wire_endpoint* within ``_PIN_PATH_MATCH_TOL``.

    Used to detect wires with stale paths (drag-pipeline left
    PinKnot ref intact but path was not translated).  Such wires
    should not count as anchored to the group — their visual
    position is desynchronised from the structural anchor.

    ``pin_world_cache`` (when supplied) memoises resolved pin world
    positions per ``(component_id, pin_number)`` so a batch ownership
    classification (``find_owned_wires`` over N wires × M groups)
    doesn't re-walk the footprint for every wire.  A sentinel
    ``False`` value caches "resolution failed" so we don't retry.
    The caller is responsible for cache lifetime; a fresh dict per
    ``find_owned_wires`` call is the canonical scope (placements are
    constant within that call).
    """
    from protoboard.domain.layout.knot import PinKnot

    if not isinstance(knot, PinKnot):
        return False
    pin = knot.pin
    cache_key = (pin.component_id, pin.pin_number)
    physical_positions: tuple[object, ...] | None = None
    if pin_world_cache is not None:
        cached = pin_world_cache.get(cache_key, _MISSING)
        if cached is not _MISSING:
            if cached is False:
                return False
            physical_positions = cast(tuple[object, ...], cached)
    if physical_positions is None:
        from protoboard.domain.components.package import resolved_footprint
        from protoboard.domain.components.package.pin_mapping import (
            package_pins_for_meta_pin,
        )
        from protoboard.domain.layout.placement import pin_world_position

        pl = placements.get(pin.component_id)
        if pl is None:
            if pin_world_cache is not None:
                pin_world_cache[cache_key] = False  # type: ignore[assignment]
            return False
        inst = instances.get(pin.component_id)
        if inst is None:
            if pin_world_cache is not None:
                pin_world_cache[cache_key] = False  # type: ignore[assignment]
            return False
        defn = definitions.get(inst.definition_ref)
        if defn is None:
            if pin_world_cache is not None:
                pin_world_cache[cache_key] = False  # type: ignore[assignment]
            return False
        try:
            fp = resolved_footprint(defn, inst, inst.families)
            physical_positions = tuple(
                pin_world_position(fp, pl, package_pin_number)
                for package_pin_number in package_pins_for_meta_pin(
                    defn, inst, pin.meta_pin_index
                )
            )
            if not physical_positions:
                return False
        except Exception:  # noqa: BLE001
            if pin_world_cache is not None:
                pin_world_cache[cache_key] = False  # type: ignore[assignment]
            return False
        if pin_world_cache is not None:
            pin_world_cache[cache_key] = physical_positions
    ep_x = getattr(wire_endpoint, "x", None)
    ep_y = getattr(wire_endpoint, "y", None)
    if ep_x is None or ep_y is None:
        return False
    for position in physical_positions:
        wp_x = getattr(position, "x", None)
        wp_y = getattr(position, "y", None)
        if wp_x is None or wp_y is None:
            continue
        dx = abs(float(wp_x) - float(ep_x))
        dy = abs(float(wp_y) - float(ep_y))
        if dx <= _PIN_PATH_MATCH_TOL and dy <= _PIN_PATH_MATCH_TOL:
            return True
    return False


_MISSING: object = object()


@counted("wire_is_owned_by_group")
def wire_is_owned_by_group(
    wire: Wire,
    group: ComponentGroup,
    *,
    wire_net_map: dict[uuid.UUID, uuid.UUID],
    knots: dict[uuid.UUID, Knot],
    group_comp_ids: frozenset[uuid.UUID],
    schematic: Schematic | None,
    placements: PlacementMap | None = None,
    instances: dict[uuid.UUID, ComponentInstance] | None = None,
    definitions: dict[str, ComponentDefinition] | None = None,
    _pin_world_cache: dict[tuple[uuid.UUID, int], object] | None = None,
) -> bool:
    """True iff *wire* structurally belongs to *group*.

    See module docstring for rule taxonomy.

    Args:
        wire: The wire to classify.
        group: The group under inspection.
        wire_net_map: Output of ``EditorSession.cached_wire_net_map`` or
            equivalent.  Used to resolve net for half-orphan tails (one
            PinKnot in group + one JointKnot — the JointKnot might still
            inherit the group net through a T-junction with another wire
            in the same net, and that net is what determines ownership).
        knots: Live ``layout.knots`` dict.  Required to resolve endpoint
            kind (PinKnot vs JointKnot) and PinKnot pin component.
        group_comp_ids: Recursive component-ID set of *group*
            (``all_component_ids(groups, group.id)``).  Pre-computed by
            the caller — usually one set per drag/render frame.
        schematic: Source of truth for net membership and global flag.
    """
    from protoboard.domain.layout.knot import JointKnot

    if wire.a is None or wire.b is None:
        return False
    ka = knots.get(wire.a.id)
    kb = knots.get(wire.b.id)
    if ka is None or kb is None:
        return False

    a_comp = _endpoint_pin_component(ka)
    b_comp = _endpoint_pin_component(kb)

    # Geometric sanity check (when full layout context is available):
    # a PinKnot endpoint only counts as "anchored to the group" if its
    # resolved pin world position actually coincides with the wire's
    # path endpoint.  Stale paths (left by a buggy drag/load that
    # translated some wires but missed this one) keep the PinKnot ref
    # intact while the path drifts off — visually the wire is
    # disconnected, structurally it still references a group pin.
    # Such wires must NOT be classified as owned, otherwise they
    # inflate the group's outline far from the visible group body.
    if placements is not None and instances is not None and definitions is not None:
        if (
            a_comp is not None
            and wire.path
            and not _pin_endpoint_matches_path(
                ka,
                wire.path[0],
                placements,
                instances,
                definitions,
                pin_world_cache=_pin_world_cache,
            )
        ):
            # PinKnot a is stale — treat as if the endpoint were a
            # loose joint (no group anchor on this side).
            a_comp = None
        if (
            b_comp is not None
            and wire.path
            and not _pin_endpoint_matches_path(
                kb,
                wire.path[-1],
                placements,
                instances,
                definitions,
                pin_world_cache=_pin_world_cache,
            )
        ):
            b_comp = None

    # If either endpoint is a PinKnot of a component outside the group,
    # the wire cannot be owned by the group.  Even if the other endpoint
    # is a group pin, this wire is either an interface wire (exported
    # net) or a DRC short — never internal.
    if a_comp is not None and a_comp not in group_comp_ids:
        return False
    if b_comp is not None and b_comp not in group_comp_ids:
        return False

    a_in = a_comp is not None and a_comp in group_comp_ids
    b_in = b_comp is not None and b_comp in group_comp_ids

    if a_in and b_in:
        # Both endpoints anchored to group pins.  Owned unconditionally —
        # net identity is irrelevant (wire physically lives between two
        # group pins, regardless of whether the net leaks via exported
        # pins elsewhere).
        return True

    # Full-orphan wire (both endpoints are JointKnots, neither anchors
    # to a group pin).  Per the 2026-06-01 design discussion: an orphan
    # belongs to no group — it can be deleted from outside or adopted
    # into any net.  This holds even if the orphan happens to sit on
    # pads of the group's internal net (e.g. legacy fragment left after
    # a partial RemoveWire) — without a structural pin anchor, the
    # wire has no group affiliation.
    if not (a_in or b_in):
        return False

    # Half-orphan: exactly one endpoint is a PinKnot of a group
    # component, the other is a free-position knot (JointKnot or
    # WirePinKnot — a wire-pin is a deliberate terminal but
    # ownership-wise behaves like a free joint: it doesn't anchor to
    # any other group).  Sanity check the free endpoint, then resolve
    # net membership.
    from protoboard.domain.layout.knot import WirePinKnot

    if not a_in and not isinstance(ka, (JointKnot, WirePinKnot)):
        return False
    if not b_in and not isinstance(kb, (JointKnot, WirePinKnot)):
        return False

    # Shared free-knot endpoint: if the JointKnot/WirePinKnot on the
    # free side has more than this single wire as an owner, the joint
    # acts as a structural anchor — translating it during a group drag
    # would silently move the foreign wires that share it.  The
    # follow-up replace_wire_fragments slide gate
    # (knot.wire_ids == {wire_id}) explicitly bails out in that case,
    # which then aborts the macro.  Mark the wire as not-owned so the
    # drag pipeline never picks it up; the half-orphan stays put while
    # the group's pin endpoint moves with the component, producing the
    # expected "wire stretched / dropped on commit" behaviour instead
    # of a hard exception.
    free_knot = kb if a_in else ka
    if isinstance(free_knot, (JointKnot, WirePinKnot)):
        if len(free_knot.wire_ids) > 1:
            return False

    net_id = wire_net_map.get(wire.id)
    return net_is_internal_to_group(net_id, group, schematic)


def wire_in_group_bbox(
    wire: Wire,
    bbox: tuple[float, float, float, float],
    tol: float = 0.5,
) -> bool:
    """True iff every vertex of *wire.path* lies inside *bbox + tol*.

    Diagnostic helper.  Not used by ownership classification — bbox is
    derived geometry, not a containment boundary.
    """
    if not wire.path:
        return False
    x0, y0, x1, y1 = bbox
    for p in wire.path:
        if p.x < x0 - tol or p.x > x1 + tol:
            return False
        if p.y < y0 - tol or p.y > y1 + tol:
            return False
    return True


def group_bbox_with_owned_wires(
    group: ComponentGroup,
    group_comp_ids: frozenset[uuid.UUID],
    placements: PlacementMap,
    instances: dict[uuid.UUID, ComponentInstance],
    definitions: dict[str, ComponentDefinition],
    wires: list[Wire],
    *,
    wire_net_map: dict[uuid.UUID, uuid.UUID],
    knots: dict[uuid.UUID, Knot],
    schematic: Schematic | None,
    pad: float = 0.5,
) -> tuple[float, float, float, float] | None:
    """Compute the outline bounding rect of *group*.

    Includes:
      * Pin world positions of every group component.
      * Vertices of every wire structurally owned by the group.

    Returns ``(x0, y0, x1, y1)`` with a ``pad`` margin, or ``None`` if
    the group has no resolvable pins (and therefore no meaningful bbox).
    """
    from protoboard.domain.components.package import resolved_footprint
    from protoboard.domain.layout.placement import pin_world_position

    xs: list[float] = []
    ys: list[float] = []

    for cid in group_comp_ids:
        pl = placements.get(cid)
        if pl is None:
            continue
        inst = instances.get(cid)
        if inst is None:
            continue
        defn = definitions.get(inst.definition_ref)
        if defn is None:
            continue
        fp = resolved_footprint(defn, inst, inst.families)
        for n in range(fp.pin_count()):
            p = pin_world_position(fp, pl, n + 1)
            xs.append(p.x)
            ys.append(p.y)

    for wire in wires:
        if not wire_is_owned_by_group(
            wire,
            group,
            wire_net_map=wire_net_map,
            knots=knots,
            group_comp_ids=group_comp_ids,
            schematic=schematic,
            placements=placements,
            instances=instances,
            definitions=definitions,
        ):
            continue
        for p in wire.path:
            xs.append(p.x)
            ys.append(p.y)

    if not xs or not ys:
        return None

    return (
        min(xs) - pad,
        min(ys) - pad,
        max(xs) + pad,
        max(ys) + pad,
    )


@dataclass
class OwnershipResolver:
    """Cohesive view for "which wires belong to a group?" over one layout context.

    Collapses the 6-foreign-type parameter sprawl of
    :func:`wire_is_owned_by_group` / :func:`find_owned_wires` /
    :func:`group_bbox_with_owned_wires` into fields set once, and owns the
    per-batch ``pin_world_position`` memo (``_pin_world_cache``) as instance
    state instead of a dict threaded through every call.

    The methods are thin organisers that delegate to the module's ``@counted``
    free functions — those stay the hot, profiled, perf-test-patched
    implementation; this object only gathers the arguments. Build it via
    :meth:`from_session` or directly with the context dicts.
    """

    knots: dict[uuid.UUID, Knot]
    wire_net_map: dict[uuid.UUID, uuid.UUID]
    schematic: Schematic | None
    placements: PlacementMap | None = None
    instances: dict[uuid.UUID, ComponentInstance] | None = None
    definitions: dict[str, ComponentDefinition] | None = None
    # Batch-scoped memo shared across this resolver's whole lifetime. Only
    # populated when the geometric sanity check is active (full layout context
    # present); a sentinel ``False`` value caches "resolution failed".
    _pin_world_cache: dict[tuple[uuid.UUID, int], object] = field(
        default_factory=dict, init=False, repr=False
    )

    @classmethod
    def from_session(cls, session: object) -> OwnershipResolver:
        """Build a resolver from an ``EditorSession``-like object.

        Reads ``layout.knots``, ``cached_wire_net_map()``, ``schematic``,
        ``layout.placements``, ``instances``, ``components``. Kept duck-typed
        (``object``) so the domain module stays Qt-free.
        """
        layout = session.layout  # type: ignore[attr-defined]
        return cls(
            knots=layout.knots,
            wire_net_map=session.cached_wire_net_map(),  # type: ignore[attr-defined]
            schematic=session.schematic,  # type: ignore[attr-defined]
            placements=layout.placements,
            instances=session.instances,  # type: ignore[attr-defined]
            definitions=session.components,  # type: ignore[attr-defined]
        )

    def is_owned(
        self,
        wire: Wire,
        group: ComponentGroup,
        group_comp_ids: frozenset[uuid.UUID],
    ) -> bool:
        """Whether *wire* structurally belongs to *group* (single wire)."""
        return wire_is_owned_by_group(
            wire,
            group,
            wire_net_map=self.wire_net_map,
            knots=self.knots,
            group_comp_ids=group_comp_ids,
            schematic=self.schematic,
            placements=self.placements,
            instances=self.instances,
            definitions=self.definitions,
            _pin_world_cache=self._pin_world_cache,
        )

    def owned_wire_ids(
        self,
        wires: list[Wire],
        group: ComponentGroup,
        group_comp_ids: frozenset[uuid.UUID],
    ) -> set[uuid.UUID]:
        """IDs of all wires structurally owned by *group*.

        Reuses this resolver's ``_pin_world_cache`` across the batch — one
        ``pin_world_position`` resolution per ``(component, pin)`` for the whole
        call, the same optimisation :func:`find_owned_wires` does internally.
        """
        result: set[uuid.UUID] = set()
        for w in wires:
            if self.is_owned(w, group, group_comp_ids):
                result.add(w.id)
        return result

    def net_is_internal(
        self,
        net_id: uuid.UUID | None,
        group: ComponentGroup,
        groups: dict[uuid.UUID, ComponentGroup] | None = None,
    ) -> bool:
        """Whether *net_id* is a net living entirely inside *group*."""
        return net_is_internal_to_group(net_id, group, self.schematic, groups)

    def group_bbox(
        self,
        group: ComponentGroup,
        group_comp_ids: frozenset[uuid.UUID],
        wires: list[Wire],
        *,
        pad: float = 0.5,
    ) -> tuple[float, float, float, float] | None:
        """Outline bbox of *group* including owned wires (None if no pins)."""
        if (
            self.placements is None
            or self.instances is None
            or self.definitions is None
        ):
            return None
        return group_bbox_with_owned_wires(
            group,
            group_comp_ids,
            self.placements,
            self.instances,
            self.definitions,
            wires,
            wire_net_map=self.wire_net_map,
            knots=self.knots,
            schematic=self.schematic,
            pad=pad,
        )
