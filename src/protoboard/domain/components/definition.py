# SPDX-License-Identifier: MIT
"""Component definitions — name + footprint + pin name mapping."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from protoboard.domain.components.footprints import FootprintTemplate
from protoboard.domain.components.package.shapes import Package
from protoboard.geometry import Point


class ComponentClass(StrEnum):
    """Electrical/functional class of a component.

    Determines the automatic refdes prefix (e.g. RESISTOR → "R") and
    can be used for DRC categorisation.
    """

    RESISTOR = "resistor"
    CAPACITOR_POLAR = "capacitor_polar"
    CAPACITOR_NONPOLAR = "capacitor_nonpolar"
    INDUCTOR = "inductor"
    TRANSFORMER = "transformer"
    DIODE = "diode"
    DIODE_ZENER = "diode_zener"
    DIODE_LED = "diode_led"
    TRANSISTOR_NPN = "transistor_npn"
    TRANSISTOR_PNP = "transistor_pnp"
    TRANSISTOR_NMOS = "transistor_nmos"
    TRANSISTOR_PMOS = "transistor_pmos"
    TRANSISTOR_NJFET = "transistor_njfet"
    TRANSISTOR_PJFET = "transistor_pjfet"
    IC = "ic"
    CONNECTOR = "connector"
    SWITCH = "switch"
    FUSE = "fuse"
    CRYSTAL = "crystal"
    MECHANICAL = "mechanical"
    OTHER = "other"


# Standard IPC/IEEE refdes prefixes per class.
REFDES_PREFIX: dict[ComponentClass, str] = {
    ComponentClass.RESISTOR: "R",
    ComponentClass.CAPACITOR_POLAR: "C",
    ComponentClass.CAPACITOR_NONPOLAR: "C",
    ComponentClass.INDUCTOR: "L",
    ComponentClass.TRANSFORMER: "T",
    ComponentClass.DIODE: "D",
    ComponentClass.DIODE_ZENER: "D",
    ComponentClass.DIODE_LED: "D",
    ComponentClass.TRANSISTOR_NPN: "Q",
    ComponentClass.TRANSISTOR_PNP: "Q",
    ComponentClass.TRANSISTOR_NMOS: "Q",
    ComponentClass.TRANSISTOR_PMOS: "Q",
    ComponentClass.TRANSISTOR_NJFET: "Q",
    ComponentClass.TRANSISTOR_PJFET: "Q",
    ComponentClass.IC: "U",
    ComponentClass.CONNECTOR: "J",
    ComponentClass.SWITCH: "SW",
    ComponentClass.FUSE: "F",
    ComponentClass.CRYSTAL: "Y",
    ComponentClass.MECHANICAL: "M",
    ComponentClass.OTHER: "X",
}


@dataclass(frozen=True, slots=True)
class PinBendSpec:
    """Specyfikacja gięcia jednego pinu.

    Dwa tryby (wzajemnie wykluczające się):
    - dyskretny: ``discrete`` niepusty — lista dozwolonych offsetów (np. TO-92).
    - swobodny 2D: ``max_reach > 0`` — pin może trafić w dowolny pad siatki
      w promieniu euklidesowym [min_reach, max_reach] od punktu (0,0) komponentu
      (placement.position — punkt obrotu, niezależny od offsetów pinów).

    Offset zawsze w lokalnym układzie komponentu (przed rotacją).
    """

    discrete: tuple[Point, ...] = ()
    min_reach: float = 0.0   # min odległość euklidesowa od punktu (0,0) komponentu
    max_reach: float = 0.0   # max odległość; 0 = brak trybu swobodnego
    default_offset: Point = field(default_factory=lambda: Point(0.0, 0.0))

    def is_free(self) -> bool:
        """True gdy tryb swobodny 2D (dowolny pad w dozwolonym promieniu)."""
        return self.max_reach > 0.0

    def is_reachable_from_origin(self, origin_world: Point, target_world: Point) -> bool:
        """Czy docelowy pad mieści się w dozwolonym promieniu od punktu (0,0) komponentu."""
        d = math.hypot(target_world.x - origin_world.x, target_world.y - origin_world.y)
        return self.min_reach <= d <= self.max_reach

    def is_reachable_from_pin1(self, pin1_world: Point, target_world: Point) -> bool:
        """Backward-compat alias — delegates to is_reachable_from_origin."""
        return self.is_reachable_from_origin(pin1_world, target_world)


@dataclass(frozen=True, slots=True)
class PinVariant:
    """An alternative pin-name mapping for a component.

    A variant overrides a subset (or all) of the base ``pin_names``.
    The footprint geometry is unchanged — only signal labels differ.

    Example: a 4-pin SIL header shared between I2C and UART applications
    can have one variant ``{1: VCC, 2: GND, 3: SDA, 4: SCL}`` and another
    ``{1: VCC, 2: GND, 3: TX, 4: RX}``.
    """

    id: str
    label: str
    pin_names: dict[int, str]


@dataclass(frozen=True, slots=True)
class ComponentDefinition:
    """Static definition of a component (a "type")."""

    name: str
    footprint: FootprintTemplate
    pin_names: dict[int, str]
    description: str = ""
    component_class: ComponentClass = ComponentClass.OTHER
    metadata: dict[str, Any] = field(default_factory=dict)
    # Per-pin bend specifications (local frame, before rotation).
    # Key = pin number (1-based). Value = PinBendSpec (discrete or continuous).
    bendable_pins: dict[int, PinBendSpec] = field(default_factory=dict)
    # Alternative pin-name mappings (same footprint, different signal labelling).
    variants: tuple[PinVariant, ...] = ()
    # Default body variant for two-pin components, as ``"{family}/{variant_id}"``.
    # ``ComponentInstance.body_variant`` overrides this per instance.  Library
    # YAML may specify this to pin a sensible package size (e.g.
    # ``resistor_axial/1_4w``); when ``None`` the renderer falls back to the
    # family default registered in ``_CLASS_TO_FAMILY``.
    default_body_variant: str | None = None
    # Default ``ComponentInstance.value`` for parts placed from this
    # definition.  Two conventions:
    #
    # * Concrete parts (BC547, 1N4148, NE555, …) leave this empty so the
    #   loader falls back to ``defn.name`` — the part number itself is the
    #   value, and the overlay label renders as e.g. ``"Q1 2N2222"``.
    # * Generic templates (``R``, ``C``, ``L``, ``F``, ``Pot`` …) set
    #   ``default_value: "?"`` in their YAML so a freshly placed instance
    #   visibly asks the user to fill in the actual value (``"R23 ?"``).
    #
    # Either way the user can edit ``instance.value`` after placement.
    default_value: str = ""
    # Whether the component body physically blocks wires from routing
    # under it.  Default False — most through-hole parts sit on standoffs
    # / leads and leave a gap to the PCB, so wires can route under them
    # (sometimes deliberately, with the component acting as a jumper).
    # Set True for SMD packages and any tight-fit body that physically
    # touches the board.  Autoroute additionally soft-penalises via
    # the ``under_component_top`` cost knob regardless of this flag.
    body_blocks_routing: bool = False
    # Optional parametric package model (docs/footprints/spec.md). When set, it
    # is the source of truth for pin geometry and bend/reach; ``footprint`` is
    # derived from it (via PackageFootprint) and ``bendable_pins`` is superseded
    # by the package's bend/solver. Consumers migrate to query ``package``
    # incrementally; when ``None`` the legacy footprint/bendable_pins apply.
    package: Package | None = None
    # Default package-family variant as ``"{family}/{variant_id}"`` (e.g.
    # ``"DO-35/typ"``) referencing the package-family catalogue. This is the
    # package-model successor to ``default_body_variant``; it resolves to a real
    # Package via the catalogue. ``ComponentInstance.package_variant`` overrides
    # it per instance. Precedence at resolution: instance override → this ref →
    # the inline ``package`` above → None (see ``component_package``).
    package_family_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ComponentDefinition.name must not be empty")
        for pin_number in self.pin_names:
            if pin_number < 1 or pin_number > self.footprint.pin_count():
                raise ValueError(
                    f"pin_names references pin {pin_number} which is outside "
                    f"the footprint's pin range (1..{self.footprint.pin_count()})"
                )
        if self.default_body_variant is not None:
            parts = self.default_body_variant.split("/", 1)
            if len(parts) != 2 or not parts[0] or not parts[1]:
                raise ValueError(
                    f"default_body_variant must be 'family/variant_id'; got "
                    f"{self.default_body_variant!r}"
                )
        if self.package_family_ref is not None:
            parts = self.package_family_ref.split("/", 1)
            if len(parts) != 2 or not parts[0] or not parts[1]:
                raise ValueError(
                    f"package_family_ref must be 'family/variant_id'; got "
                    f"{self.package_family_ref!r}"
                )

    @property
    def pin_count(self) -> int:
        return self.footprint.pin_count()

    @property
    def refdes_prefix(self) -> str:
        """Standard refdes prefix for this component class (e.g. 'U', 'R', 'Q')."""
        return REFDES_PREFIX[self.component_class]

    def pin_name(self, pin_number: int) -> str:
        """Return the user-facing name of the given pin (1-indexed)."""
        if pin_number < 1 or pin_number > self.pin_count:
            raise ValueError(f"pin {pin_number} out of range (1..{self.pin_count})")
        return self.pin_names.get(pin_number, f"P{pin_number}")

    def effective_pin_names(self, pin_variant: str | None = None) -> dict[int, str]:
        """Return pin_names with variant overrides applied."""
        if pin_variant is None or not self.variants:
            return self.pin_names
        for v in self.variants:
            if v.id == pin_variant:
                return {**self.pin_names, **v.pin_names}
        return self.pin_names
