# SPDX-License-Identifier: MIT
"""Component definitions — name + package + pin name mapping."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from protoboard.domain.components.appearance import Appearance
from protoboard.domain.components.footprints import Footprint
from protoboard.domain.components.package.shapes import Package
from protoboard.geometry import Point, Vector


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
    DEVBOARD = "devboard"
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
    ComponentClass.DEVBOARD: "U",
    ComponentClass.CONNECTOR: "J",
    ComponentClass.SWITCH: "SW",
    ComponentClass.FUSE: "F",
    ComponentClass.CRYSTAL: "Y",
    ComponentClass.MECHANICAL: "M",
    ComponentClass.OTHER: "X",
}


@dataclass(frozen=True, slots=True)
class PinBendSpec:
    """Specyfikacja gięcia jednego pinu — swobodny tryb 2D.

    ``max_reach > 0`` → pin może trafić w dowolny pad siatki w promieniu
    euklidesowym [min_reach, max_reach] od punktu (0,0) komponentu
    (placement.position — punkt obrotu, niezależny od offsetów pinów).
    ``max_reach == 0`` → pin sztywny (brak trybu swobodnego).

    Offset zawsze w lokalnym układzie komponentu (przed rotacją).
    """

    min_reach: float = 0.0   # min odległość euklidesowa od punktu (0,0) komponentu
    max_reach: float = 0.0   # max odległość; 0 = brak trybu swobodnego
    default_offset: Vector = field(default_factory=lambda: Vector(0.0, 0.0))

    def is_free(self) -> bool:
        """True gdy tryb swobodny 2D (dowolny pad w dozwolonym promieniu)."""
        return self.max_reach > 0.0

    def is_within_bend_disc(
        self, pin_default_world: Point, target_world: Point
    ) -> bool:
        """Czy ``target`` mieści się w dysku odgięcia ``[0, max_reach]`` wokół
        domyślnej pozycji pinu.

        The per-pin BEND check used by re-drag: the lead may bend anywhere from
        its own default (offset 0 — always legal, the lead simply straightens)
        out to ``max_reach``.  ``min_reach`` is intentionally NOT applied here —
        it is the 2-pin *spacing* floor (distance from the origin / the other
        terminal, enforced by the place tool via
        :meth:`is_reachable_from_origin`), a different axis.  Conflating the two
        wrongly blocked a pin from returning to its own default after several
        re-drags (2026-06-26).
        """
        d = math.hypot(
            target_world.x - pin_default_world.x,
            target_world.y - pin_default_world.y,
        )
        return d <= self.max_reach

    def is_reachable_from_pin_default(
        self, pin_default_world: Point, target_world: Point
    ) -> bool:
        """Czy ``target`` mieści się w pierścieniu ``[min_reach, max_reach]``
        wyśrodkowanym na ``pin_default_world``.

        Annulus form — applies ``min_reach`` as well.  Kept for callers that want
        the spacing floor centred on a point; re-drag's per-pin bend uses
        :meth:`is_within_bend_disc` instead (no ``min_reach``).
        """
        d = math.hypot(
            target_world.x - pin_default_world.x,
            target_world.y - pin_default_world.y,
        )
        return self.min_reach <= d <= self.max_reach

    def is_reachable_from_origin(self, origin_world: Point, target_world: Point) -> bool:
        """Back-compat alias — measures the radius from whatever centre is passed.

        Identical math to :meth:`is_reachable_from_pin_default`; the name reflects
        the pre-per-pin model where the centre was the component origin. New
        callers should use ``is_reachable_from_pin_default`` and pass the pin's
        default world position.
        """
        return self.is_reachable_from_pin_default(origin_world, target_world)

    def is_reachable_from_pin1(self, pin1_world: Point, target_world: Point) -> bool:
        """Backward-compat alias — delegates to is_reachable_from_pin_default."""
        return self.is_reachable_from_pin_default(pin1_world, target_world)


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
class FootprintOption:
    """One available footprint a component offers (a picker entry).

    A component's logical identity (name/pins/class) is separate from the
    *footprints* it can be placed in. Each option is either a **reference** into
    the package-family catalogue (``ref = "family/variant"``) or an **inline**
    parametric package built from the component's YAML (``package``) — exactly
    one of the two is set. ``name`` is the picker leaf (joined with the
    component ``symbol`` as ``symbol_name``); ``style`` overrides the list's
    ``footprint_style`` for this entry only.
    """

    name: str
    ref: str | None = None
    package: Package | None = None
    style: Appearance | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("FootprintOption.name must not be empty")
        if (self.ref is None) == (self.package is None):
            raise ValueError(
                "FootprintOption requires exactly one of 'ref' / 'package'"
            )


@dataclass(frozen=True, slots=True)
class ComponentDefinition:
    """Static definition of a component (a "type").

    ``package`` is the single stored geometry source — the definition's
    DEFAULT body (the loader synthesizes a non-rigid generic bbox package when
    the YAML declares none, so it is always set). There is deliberately no
    ``footprint`` field: a placed component's geometry is its **instance's**
    resolved variant (``footprint_of`` / ``resolved_footprint``), and reading
    a definition-level footprint in a placement context was a recurring
    silent-wrong-body bug. Contexts that genuinely need the default (library
    preview, pre-placement ghost) call :meth:`default_footprint`, which says
    so in its name.
    """

    name: str
    package: Package
    pin_names: dict[int, str]
    description: str = ""
    component_class: ComponentClass = ComponentClass.OTHER
    metadata: dict[str, Any] = field(default_factory=dict)
    # Per-pin bend specifications (local frame, before rotation).
    # Key = pin number (1-based). Value = PinBendSpec (discrete or continuous).
    bendable_pins: dict[int, PinBendSpec] = field(default_factory=dict)
    # Alternative pin-name mappings (same footprint, different signal labelling).
    variants: tuple[PinVariant, ...] = ()
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
    # Default package-family variant as ``"{family}/{variant_id}"`` (e.g.
    # ``"DO-35/typ"``) referencing the package-family catalogue. This is the
    # package-model successor to ``default_body_variant``; it resolves to a real
    # Package via the catalogue. ``ComponentInstance.package_variant`` overrides
    # it per instance. Precedence at resolution: instance override → this ref →
    # the inline ``package`` above → None (see ``component_package``).
    package_family_ref: str | None = None
    # Visual-only override (fill colour, corner radius) for every instance of
    # this type, layered on top of the package/theme defaults. A placed
    # ``ComponentInstance.appearance`` overrides this per part. ``None`` = inherit
    # the package/theme appearance. Geometry/mask/DRC/routing are unaffected.
    appearance: Appearance | None = None
    # Schematic symbol root (e.g. "R", "U", "LED"). Drives the refdes prefix and
    # the footprint-picker label root (``symbol_name``). ``None`` falls back to
    # ``REFDES_PREFIX[component_class]``. Inheritable via library ``extends``.
    symbol: str | None = None
    # Available footprints this component can be placed in (the picker offers
    # these; the user must choose one at place time). The default/first entry
    # derives the ``package`` above so instance-less consumers see one default
    # geometry. Empty = no choice (the part carries its ``package`` directly).
    # See docs/footprints/authoring.md.
    footprints: tuple[FootprintOption, ...] = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ComponentDefinition.name must not be empty")
        if not isinstance(self.package, Package):
            # Loud guard for positional callers that used to pass a Footprint
            # here (the removed ``footprint`` field's old slot).
            raise TypeError(
                f"ComponentDefinition.package must be a Package, got "
                f"{type(self.package).__name__}; for a bare footprint use "
                f"ComponentDefinition.from_footprint(...)"
            )
        for pin_number in self.pin_names:
            if pin_number < 1 or pin_number > self.package.pin_count():
                raise ValueError(
                    f"pin_names references pin {pin_number} which is outside "
                    f"the package's pin range (1..{self.package.pin_count()})"
                )
        if self.package_family_ref is not None:
            parts = self.package_family_ref.split("/", 1)
            if len(parts) != 2 or not parts[0] or not parts[1]:
                raise ValueError(
                    f"package_family_ref must be 'family/variant_id'; got "
                    f"{self.package_family_ref!r}"
                )

    @classmethod
    def from_footprint(
        cls,
        name: str,
        footprint: Footprint,
        pin_names: dict[int, str],
        **kwargs: Any,
    ) -> ComponentDefinition:
        """A definition whose package is synthesized over *footprint*.

        The loader's no-package invariant as a factory: the pin cloud comes
        from *footprint*, the body is the non-rigid generic bbox package
        (``kind="_generic"``). For YAML parts declaring only ``footprint:``,
        and for test fixtures that used to pass ``footprint=`` directly.
        """
        from protoboard.domain.components.package.kind import (
            synth_generic_package,
        )

        pkg = synth_generic_package(footprint)
        if pkg is None:
            raise ValueError(
                f"{name}: cannot synthesize a package over a footprint with "
                f"no pins")
        return cls(name=name, package=pkg, pin_names=pin_names, **kwargs)

    def default_footprint(self) -> Footprint:
        """The DEFAULT-variant footprint — ``PackageFootprint`` over ``package``.

        For anything *placed*, use the instance-resolved geometry
        (``footprint_of`` / ``ComponentInstance.resolved_footprint``); reading
        the definition default for a placed component is the silent-wrong-body
        bug this method's name exists to make visible. Legitimate callers are
        instance-less contexts only: library preview, pre-placement ghost,
        BOM fallback.
        """
        from protoboard.domain.components.package.adapter import (
            PackageFootprint,
        )

        return PackageFootprint(self.package)

    @property
    def pin_count(self) -> int:
        return self.package.pin_count()

    @property
    def refdes_prefix(self) -> str:
        """Refdes prefix: the explicit ``symbol`` if set, else the class default.

        ``symbol`` (e.g. 'R', 'U', 'LED') is also the footprint-picker label root.
        """
        return self.symbol or REFDES_PREFIX[self.component_class]

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

    def pins_grouped_by_name(
        self, pin_variant: str | None = None
    ) -> dict[str, frozenset[int]]:
        """Group pins sharing an *explicit* name into one electrical node.

        Returns ``{name: frozenset(pin_numbers)}`` for every name carried by
        two or more pins — these are the internally-connected pins of the
        package (e.g. several ``GND``). Only explicitly named pins count:
        unnamed pins (which fall back to a unique ``P{n}`` label) never group.

        No name is special-cased — ``NC`` and friends group like any other.
        That is intentional: unconnected pins shorted "in a vacuum" carry no
        net, so DRC never sees them (see spec, variant B).
        """
        by_name: dict[str, list[int]] = {}
        for pin_num, name in self.effective_pin_names(pin_variant).items():
            by_name.setdefault(name, []).append(pin_num)
        return {
            name: frozenset(pins)
            for name, pins in by_name.items()
            if len(pins) > 1
        }
