# SPDX-License-Identifier: MIT
"""Component definitions — name + package + pin name mapping."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from protoboard.domain.components.appearance import Appearance
from protoboard.domain.components.footprints import Footprint
from protoboard.domain.components.package.family import PackageFamily


class ComponentClass(StrEnum):
    """The KNOWN electrical/functional classes of a component.

    The vocabulary the code attaches behaviour to: the automatic refdes
    prefix (e.g. RESISTOR → "R"), the polarity marker, the devboard look.
    ``ComponentDefinition.component_class`` is deliberately an OPEN ``str`` —
    a library may tag parts with classes this build does not know (the loader
    warns, everything degrades to neutral defaults), so newer libraries keep
    loading on older code. Compare with ``==`` (a StrEnum member equals its
    string), never ``is``.
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


# Standard IPC/IEEE refdes prefixes per KNOWN class (an unknown open-class
# string falls back to "X" — same as OTHER; ``symbol:`` overrides either way).
REFDES_PREFIX: dict[str, str] = {
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
class PackageOption:
    """One available package choice a component offers (a picker entry).

    A component's logical identity (name/pins/class) is separate from the
    *footprints* it can be placed in. Every option is a reference into the
    package-family catalogue. ``name`` is the picker leaf (joined with the
    component ``symbol`` as ``symbol_name``); ``style`` overrides the list's
    ``footprint_style`` for this entry only.
    """

    name: str
    ref: str
    style: Appearance | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("PackageOption.name must not be empty")
        if self.ref == "":
            raise ValueError("PackageOption.ref must not be empty")


@dataclass(frozen=True, slots=True)
class ComponentDefinition:
    """Static definition of a component (a "type").

    Geometry is referenced only through ``package_options``. Package objects
    live in the package-family catalogue, never on the component definition.
    """

    name: str
    pin_names: dict[int, str]
    description: str = ""
    # Open classification tag: one of the KNOWN ``ComponentClass`` values (the
    # loader normalizes those to enum members) or any library-authored string
    # the code does not know yet — unknown classes load fine and behave like
    # OTHER, but keep the author's label (BOM / panels / future semantics).
    component_class: str = ComponentClass.OTHER
    metadata: dict[str, Any] = field(default_factory=dict)
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
    # Visual-only override (fill colour, corner radius) for every instance of
    # this type, layered on top of the package/theme defaults. A placed
    # ``ComponentInstance.appearance`` overrides this per part. ``None`` = inherit
    # the package/theme appearance. Geometry/mask/DRC/routing are unaffected.
    appearance: Appearance | None = None
    # Schematic symbol root (e.g. "R", "U", "LED"). Drives the refdes prefix and
    # the footprint-picker label root (``symbol_name``). ``None`` falls back to
    # ``REFDES_PREFIX[component_class]``. Inheritable via library ``extends``.
    symbol: str | None = None
    # Available packages this component can be placed in. The first entry is
    # the default; every entry resolves through the package-family catalogue.
    # See docs/footprints/authoring.md.
    package_options: tuple[PackageOption, ...] = ()
    # Parametric (variable-pin-count) entry: the library YAML declared a
    # ``package: {kind: …}`` with NO ``pins:``, so the pin count (and any open
    # generator parameters, e.g. a pin header's ``rows``) are chosen by the
    # user at placement time. The default option then points at a PREVIEW body.
    parametric_pins: bool = False

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ComponentDefinition.name must not be empty")
        if not self.package_options:
            raise ValueError("ComponentDefinition requires a package option")

    def default_footprint(
        self, families: dict[str, PackageFamily]
    ) -> Footprint:
        """Resolve the default package option into footprint geometry.

        For anything *placed*, use the instance-resolved geometry
        (``footprint_of`` / ``ComponentInstance.resolved_footprint``); reading
        the definition default for a placed component is the silent-wrong-body
        bug this method's name exists to make visible. Legitimate callers are
        instance-less contexts only: library preview, pre-placement ghost,
        BOM fallback.
        """
        from protoboard.domain.components.package.component_package import (
            resolve_component_package,
        )

        return resolve_component_package(self, None, families)

    def default_package_option(self) -> PackageOption | None:
        """The definition's default package choice, if it declares one."""
        return self.package_options[0] if self.package_options else None

    def default_package_ref(self) -> str | None:
        """The catalogue ref of the default package choice, if any."""
        option = self.default_package_option()
        return None if option is None else option.ref

    @property
    def pin_count(self) -> int:
        return len(self.pin_names)

    @property
    def refdes_prefix(self) -> str:
        """Refdes prefix: the explicit ``symbol`` if set, else the class default.

        ``symbol`` (e.g. 'R', 'U', 'LED') is also the footprint-picker label root.
        """
        return self.symbol or REFDES_PREFIX.get(self.component_class, "X")

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
            name: frozenset(pins) for name, pins in by_name.items() if len(pins) > 1
        }
