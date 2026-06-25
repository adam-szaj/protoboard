# SPDX-License-Identifier: MIT
"""Reach info derived from a Package — the budulec that replaces bendable_pins.

Consumers (place_tool ring overlay, DRC overlap-skip) historically read
``PinBendSpec``; this computes the same information from a ``Package`` instead.
The reach ring is measured from the fixed component ORIGIN ``[0, 0]`` (the
default position of pin 1, the rotation point) — matching the legacy
``is_reachable_from_origin`` semantics. The origin never moves; every pin
(including pin 1) may bend its lead within the ring.

``package_to_bend_specs`` adapts a Package into the legacy ``dict[int,
PinBendSpec]`` so existing consumers work unchanged; our model is always the
free-2D mode (no discrete), so ``discrete`` is always empty.

Pure domain. ``bend`` is per package; the resulting reach is shared by all pins.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from protoboard.domain.components.package.body import MountStyle
from protoboard.domain.components.package.component_package import (
    resolve_component_package,
)
from protoboard.domain.components.package.family import resolve_variant
from protoboard.domain.components.package.shapes import (
    Package,
    surface_mount_offsets,
)
from protoboard.domain.components.package.solver import (
    RASTER_MM,
    MountOrientation,
    OffsetRange,
)
from protoboard.geometry import Point
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
    """Where a pin's lead may land: a euclidean ring measured from the ORIGIN.

    The component ORIGIN is the fixed reference point ``[0, 0]`` (= the default
    position of pin 1 = the rotation point). The origin does NOT move; what
    moves is the pin's bent lead relative to its default. ``default`` is the
    pin's home hole. ``ring`` is the allowed spacing from the origin (mm) — for
    bend ``[body_offset, body_offset+bend]``, for axial the solver's
    ``[r_min, r_max]``. ``is_movable`` is False when the ring is a single point
    (rigid — DIP, bend=0).
    """

    origin: Point
    default: Point
    ring: OffsetRange

    @property
    def is_movable(self) -> bool:
        return self.ring.r_max_mm > self.ring.r_min_mm

    def is_reachable(self, target: Point, *, raster_mm: float) -> bool:
        """Whether ``target`` (in holes) is a legal landing for this pin's lead.

        The ring is spacing from the fixed component origin, so distance is
        measured from the ORIGIN, in mm (holes × raster) to match ring units.
        """
        dx = (target.x - self.origin.x) * raster_mm
        dy = (target.y - self.origin.y) * raster_mm
        dist = (dx * dx + dy * dy) ** 0.5
        return self.ring.contains_mm(dist)


def pin_reaches(
    package: Package,
    *,
    orientation: MountOrientation = MountOrientation.HORIZONTAL,
) -> dict[int, PinReach]:
    """Reach info per pin number (1-based) for a placed package.

    The component ORIGIN is the fixed point ``[0, 0]`` (default position of pin
    1, the rotation point). The origin never moves; every pin — including pin 1
    — may have its lead bent within ``ring`` of the origin. ``bend``/solver are
    per package, so the ring is shared; each pin carries its own default hole.
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
    via its discrete mount points even though its body is rigid.
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
    sit on any of a *discrete* set of neighbour pads (``surface_mount_offsets``)
    — orthogonal or diagonal.  That pin gets a discrete ``PinBendSpec`` whose
    options are those offsets; place_tool's discrete-snap and the panel's combo
    handle the rest.

    Every other package keeps the free-ring behaviour: only movable pins (ring
    wider than a point) are included; ``min_reach``/``max_reach`` are the ring in
    HOLE units (ring mm ÷ RASTER_MM); ``discrete`` is empty (free-2D).
    """
    from protoboard.domain.components.definition import PinBendSpec

    is_surface = getattr(package, "mount", None) is MountStyle.SURFACE
    if is_surface and package.pin_count() == 2:
        return {
            2: PinBendSpec(
                discrete=surface_mount_offsets(package),
                default_offset=Point(0.0, 0.0),
            )
        }

    out: dict[int, PinBendSpec] = {}
    for pin, reach in pin_reaches(package, orientation=orientation).items():
        if not reach.is_movable:
            continue
        out[pin] = PinBendSpec(
            discrete=(),
            min_reach=reach.ring.r_min_mm / RASTER_MM,
            max_reach=reach.ring.r_max_mm / RASTER_MM,
            default_offset=Point(0.0, 0.0),
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
    if families is not None:
        # A picker-chosen address (no placed instance yet) takes precedence — it
        # is the variant the user is actively placing.
        if package_variant:
            variant = resolve_variant(families, package_variant)
            if variant is not None:
                return package_to_bend_specs(variant.package)
        pkg = resolve_component_package(defn, instance, families)
        if pkg is not None:
            return package_to_bend_specs(pkg)
    if defn.package is not None:
        return package_to_bend_specs(defn.package)
    return defn.bendable_pins
