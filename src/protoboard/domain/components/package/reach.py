# SPDX-License-Identifier: MIT
"""Reach info derived from a Package — the budulec that replaces bendable_pins.

Consumers (place_tool ring overlay, DRC overlap-skip) historically read
``PinBendSpec``; this computes the same information from a ``Package`` instead.
Each pin's reach is a **disc centred on that pin's OWN default hole** — a
euclidean ``[r_min, r_max]`` measured from ``default`` (per-pin model, 2026-06-26;
previously one ring shared by all pins, measured from the fixed component
origin). The ``origin`` field is retained as the rotation point ``[0, 0]`` but is
no longer the measurement centre.

``package_to_bend_specs`` adapts a Package into the legacy ``dict[int,
PinBendSpec]`` so existing consumers work unchanged; our model is always the
free-2D mode (no discrete), so ``discrete`` is always empty.

Pure domain. The ``ring`` magnitude is per package (one ``OffsetRange`` today),
but each pin carries its own ``default`` so its disc is centred independently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from protoboard.domain.components.package.body import MountStyle
from protoboard.domain.components.package.component_package import (
    resolve_component_package,
)
from protoboard.domain.components.package.family import resolve_variant
from protoboard.domain.components.package.shapes import Package
from protoboard.domain.components.package.solver import (
    RASTER_MM,
    MountOrientation,
    OffsetRange,
)
from protoboard.geometry import Point, Vector
from protoboard.services.debug_trace import traced

if TYPE_CHECKING:
    from protoboard.domain.components.definition import (
        ComponentDefinition,
        PinBendSpec,
    )
    from protoboard.domain.components.footprints import Footprint
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.components.package.family import PackageFamily
    from protoboard.domain.layout.placement import ComponentPlacement


@dataclass(frozen=True, slots=True)
class PinReach:
    """Where a pin's lead may land: a euclidean disc centred on the pin default.

    ``default`` is the pin's home hole and the **centre of the reach disc**;
    distance is measured from it (see :meth:`is_reachable`). ``ring`` is the
    allowed spacing (mm) — for bend ``[body_offset, body_offset+bend]``, for
    axial the solver's ``[r_min, r_max]``. ``origin`` is the fixed component
    reference ``[0, 0]`` (the rotation point); it is retained for callers that
    need it but is no longer the reach centre. ``is_movable`` is False when the
    ring is a single point (rigid — DIP, bend=0).
    """

    origin: Point
    default: Point
    ring: OffsetRange

    @property
    def is_movable(self) -> bool:
        return self.ring.r_max_mm > self.ring.r_min_mm

    def is_reachable(self, target: Point, *, raster_mm: float) -> bool:
        """Whether ``target`` (in holes) is a legal landing for this pin's lead.

        The reach is a disc centred on the pin's OWN ``default`` hole, so
        distance is measured from ``default``, in mm (holes × raster) to match
        ring units. (Pre-2026-06-26 this measured from the shared component
        origin; the per-pin model re-centres each pin's disc on its own home.)
        """
        dx = (target.x - self.default.x) * raster_mm
        dy = (target.y - self.default.y) * raster_mm
        dist = (dx * dx + dy * dy) ** 0.5
        return self.ring.contains_mm(dist)


def pin_reaches(
    package: Package,
    *,
    orientation: MountOrientation = MountOrientation.HORIZONTAL,
) -> dict[int, PinReach]:
    """Reach info per pin number (1-based) for a placed package.

    Each pin's reach is a disc centred on that pin's own ``default`` hole. The
    ``ring`` magnitude is per package (``bend``/solver yield one ``OffsetRange``
    today), but every pin carries its own ``default`` so the discs are centred
    independently — pin 3 of a TO-92 bends around pin 3's home, not the origin.
    ``origin`` stays the fixed rotation point ``[0, 0]``.
    """
    positions = package.pin_positions(orientation=orientation)
    # The reach ring is where a movable terminal MAY land — the union over mount
    # orientations (axial parts can lie flat or stand upright). pin_positions
    # still uses the per-orientation layout for the default (ghost) spacing.
    ring = package.reach_offset_range()
    origin = Point(0.0, 0.0)
    out: dict[int, PinReach] = {}
    for idx, pos in enumerate(positions, start=1):
        out[idx] = PinReach(origin=origin, default=pos, ring=ring)
    return out


@traced("is_pin_bendable")
def is_pin_bendable(
    package: Package,
    pin_number: int,
    *,
    orientation: MountOrientation = MountOrientation.HORIZONTAL,
) -> bool:
    """Whether a pin can move — replaces ``pin in bendable_pins``.

    A through-hole pin is movable iff its reach ring is wider than a point
    (covers both ``bend`` and the axial solver: the solver yields r_max > r_min
    when lead_length > 0).  A surface-mount package's second terminal is movable
    via its free reach ring over the neighbour pads even though its body is rigid.
    """
    if getattr(package, "mount", None) is MountStyle.SURFACE:
        return pin_number == 2 and package.pin_count() == 2
    reach = pin_reaches(package, orientation=orientation).get(pin_number)
    return reach is not None and reach.is_movable


@traced("package_to_bend_specs")
def package_to_bend_specs(
    package: Package,
    *,
    orientation: MountOrientation = MountOrientation.HORIZONTAL,
) -> dict[int, "PinBendSpec"]:
    """Adapt a Package into the legacy ``dict[int, PinBendSpec]``.

    Lets existing consumers (place_tool) keep their ``PinBendSpec`` logic while
    geometry is driven by the package.

    A **surface-mount** package (an SMD chip) is the special case: the body is
    rigid (no lead bend), but the part rests on pads, so its second terminal may
    sit on any neighbour pad of the first terminal. That is expressed as a
    **free reach ring** ``[1, √2 + ε]`` holes from the origin — the band that
    spans pin 1's orthogonal (1.0) and diagonal (√2) neighbours — so the
    place-tool / re-drag snap any of the eight surrounding pads via the same
    free-2D path every other movable pin uses (no discrete-snap special case).

    Every other package keeps the free-ring behaviour: only movable pins (ring
    wider than a point) are included; ``min_reach``/``max_reach`` are the ring in
    HOLE units (ring mm ÷ RASTER_MM); ``discrete`` is empty (free-2D).
    """
    from protoboard.domain.components.definition import PinBendSpec

    is_surface = getattr(package, "mount", None) is MountStyle.SURFACE
    if is_surface and package.pin_count() == 2:
        from math import sqrt
        return {
            2: PinBendSpec(
                min_reach=1.0,
                max_reach=(sqrt(2) + 0.1),
                default_offset=Vector(0.0, 0.0),
            )
        }

    out: dict[int, PinBendSpec] = {}
    for pin, reach in pin_reaches(package, orientation=orientation).items():
        if not reach.is_movable:
            continue
        out[pin] = PinBendSpec(
            min_reach=reach.ring.r_min_mm / RASTER_MM,
            max_reach=reach.ring.r_max_mm / RASTER_MM,
            default_offset=Vector(0.0, 0.0),
        )
    return out


def effective_bend_specs(
    defn: "ComponentDefinition",
    instance: "ComponentInstance | None" = None,
    families: "dict[str, PackageFamily] | None" = None,
    package_variant: str | None = None,
) -> dict[int, "PinBendSpec"]:
    """The per-pin bend specs in force for a definition / placed instance.

    Single point of truth shared by the place tool, the properties panel and the
    select-tool re-drag: a parametric package drives the specs (including an SMD
    chip's discrete mount points); otherwise the legacy ``bendable_pins`` apply.
    Without this, a consumer reading ``defn.bendable_pins`` directly would miss
    package-driven specs — the loader clears ``bendable_pins`` for rigid (incl.
    SMD) packages baked onto the definition.

    When *families* is given, the **chosen footprint variant** drives the specs.
    The variant comes from *instance* (a placed part — properties panel /
    select-tool re-drag) or, during placement before any instance exists, from
    the *package_variant* address string (the place tool's picker choice). Both
    resolve through the catalogue by the same precedence
    (``variant`` → ``defn.package_family_ref`` → ``defn.package``). This is what
    makes a part whose bodies live in a ``footprints:`` list (e.g. ``R`` placed
    as ``SMD-0805``) resolve its rigid / SMD specs even though ``defn.package``
    is ``None``. Omitting *families* keeps the legacy ``defn.package`` →
    ``bendable_pins`` behaviour for callers with no catalogue to resolve against.
    """
    # Local import avoids a module-load cycle (kind → package.loader).
    from protoboard.domain.components.package.kind import is_generic_package
    if families is not None:
        # A picker-chosen address (no placed instance yet) takes precedence — it
        # is the variant the user is actively placing.
        if package_variant:
            variant = resolve_variant(families, package_variant)
            if variant is not None:
                return package_to_bend_specs(variant.package)
        pkg = resolve_component_package(defn, instance, families)
        if pkg is not None and not is_generic_package(pkg):
            return package_to_bend_specs(pkg)
    # A declared package drives the specs; the synthetic generic fallback
    # (``kind="_generic"``) does not — it defers to the authored ``bendable_pins``
    # so a THT part keeps its real bend ring after the package invariant made
    # ``defn.package`` always non-None.
    if defn.package is not None and not is_generic_package(defn.package):
        return package_to_bend_specs(defn.package)
    return defn.bendable_pins


# ---------------------------------------------------------------------------
# Re-drag feasibility — the single predicate shared by overlay and commit.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RedragResult:
    """The outcome of a candidate re-drag.

    ``ok`` is whether the move is legal (every pin's final hole lies in its own
    reach disc and no rigid pin is forced off a free pad).  ``new_placement`` is
    the placement that *would* result (``position`` + recomputed ``pin_offsets``)
    when ``ok``; ``None`` otherwise.  Both the overlay (per candidate pad) and
    the commit (on the clicked pad) call :func:`redrag_feasibility`, so the lit
    pads and the committable pads are guaranteed identical.
    """

    ok: bool
    new_placement: "ComponentPlacement | None" = None


def _pin_baseline_world(
    footprint: "Footprint", placement: "ComponentPlacement", pin: int
) -> Point:
    """World position of *pin* with its own offset stripped (its default hole)."""
    from protoboard.domain.layout.placement import pin_world_position

    temp = placement.copy()
    temp.pin_offsets = {k: v for k, v in placement.pin_offsets.items() if k != pin}
    return pin_world_position(footprint, temp, pin)


def _world_delta_to_local_offset(
    world_delta: Vector,
    placement: "ComponentPlacement",
    footprint: Footprint,
) -> Vector:
    """Invert the placement pipeline: world delta → local pin offset.

    Mirrors the forward pipeline (local → flip(bottom) → rotate → +position)
    used by ``pin_world_position``: unrotate, then un-flip on the bottom side.
    The flip reflection is its own inverse, so applying the footprint's
    ``flip_transform`` (as a direction — translation ignored) undoes it.
    """
    from protoboard.domain.layout.placement import unrotate

    local = unrotate(world_delta, placement.rotation)
    if placement.side == "bottom":
        local = footprint.flip_transform().apply(local)
    return local


def redrag_feasibility(
    *,
    footprint: "Footprint",
    placement: "ComponentPlacement",
    specs: dict[int, "PinBendSpec"],
    grabbed_pin: int,
    target: Point,
    free_pads: frozenset[tuple[float, float]],
) -> RedragResult:
    """Whether moving *grabbed_pin* to *target* is legal, and the resulting placement.

    The induced transform depends only on whether the grabbed pin is bendable:

    * **Bendable grab** — a pure lead bend: only ``pin_offsets[grabbed_pin]``
      changes, the body stays put.  Legal iff *target* is within that pin's bend
      disc ``[0, max_reach]`` of its own default — so the pin can ALWAYS return
      to its default (offset 0); ``min_reach`` (the 2-pin spacing floor) does not
      apply to a pin's bend about its own home.  Other pins are untouched.
    * **Rigid grab** — the body translates so the rigid pin reaches *target*;
      every *other* bendable lead absorbs the shift (its world position stays on
      its current pad via a compensating offset) and must remain within its own
      disc, and every *other* rigid pin must land on a free pad.  Legal iff all
      those hold.

    *free_pads* is the set of board holes a moved rigid pin may land on (rounded
    ``(x, y)`` tuples); pass the board's free-pad set.  Pure domain — no Qt.
    """
    from protoboard.domain.layout.placement import pin_world_position

    spec = specs.get(grabbed_pin)
    grabbed_bendable = spec is not None and spec.is_free()

    if grabbed_bendable:
        assert spec is not None
        baseline = _pin_baseline_world(footprint, placement, grabbed_pin)
        if not spec.is_within_bend_disc(baseline, target):
            return RedragResult(ok=False)
        local_off = _world_delta_to_local_offset(
            target - baseline, placement, footprint
        )
        new_pl = placement.copy()
        new_offsets = dict(placement.pin_offsets)
        if local_off.x == 0.0 and local_off.y == 0.0:
            new_offsets.pop(grabbed_pin, None)
        else:
            new_offsets[grabbed_pin] = local_off
        new_pl.pin_offsets = new_offsets
        return RedragResult(ok=True, new_placement=new_pl)

    # Rigid grab: translate the body so the grabbed pin reaches the target.
    grabbed_world = pin_world_position(footprint, placement, grabbed_pin)
    body_shift = target - grabbed_world  # Vector
    if body_shift.dx == 0.0 and body_shift.dy == 0.0:
        return RedragResult(ok=True, new_placement=placement.copy())

    new_pl = placement.copy()
    new_pl.position = placement.position + body_shift
    new_offsets = dict(placement.pin_offsets)

    for other in range(1, footprint.pin_count() + 1):
        if other == grabbed_pin:
            continue
        old_world = pin_world_position(footprint, placement, other)
        other_spec = specs.get(other)
        if other_spec is not None and other_spec.is_free():
            # Bendable: keep this lead on its current pad — the offset absorbs
            # the body shift.  It must stay within this pin's own bend disc.
            baseline_new = _pin_baseline_world(footprint, new_pl, other)
            if not other_spec.is_within_bend_disc(baseline_new, old_world):
                return RedragResult(ok=False)
            local_off = _world_delta_to_local_offset(
                old_world - baseline_new, new_pl, footprint
            )
            if local_off.x == 0.0 and local_off.y == 0.0:
                new_offsets.pop(other, None)
            else:
                new_offsets[other] = local_off
        else:
            # Rigid: this pin moves with the body and must land on a free pad.
            landing = old_world + body_shift
            key = (round(landing.x, 4), round(landing.y, 4))
            if key not in free_pads:
                return RedragResult(ok=False)
    new_pl.pin_offsets = new_offsets
    return RedragResult(ok=True, new_placement=new_pl)
