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
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.components.package.family import PackageFamily


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
    ring = package.board_offset_range(orientation=orientation)
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
