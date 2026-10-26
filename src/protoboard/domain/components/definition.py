# SPDX-License-Identifier: MIT
"""Logical component definitions and their package choices."""

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
class MetaPin:
    """One logical electrical terminal of a component."""

    labels: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.labels:
            raise ValueError("MetaPin.labels must not be empty")
        if any(not isinstance(label, str) or not label for label in self.labels):
            raise ValueError("MetaPin labels must be non-empty strings")


@dataclass(frozen=True, slots=True)
class PackageOption:
    """One available package choice a component offers (a picker entry).

    A component's logical identity (name/MetaPins/class) is separate from the
    packages it can be placed in. Every option is a reference into the
    package-family catalogue. ``group/name`` is the picker path; the loader
    defaults ``group`` to the authored reference. ``style`` overrides the list's
    ``footprint_style`` for this entry only.
    """

    name: str
    ref: str
    style: Appearance | None = None
    pin_map: dict[int, tuple[int, ...]] = field(default_factory=dict)

    # Presentation root, independent of the resolved catalogue address.
    group: str | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("PackageOption.name must not be empty")
        if self.ref == "":
            raise ValueError("PackageOption.ref must not be empty")
        if any(index < 1 for index in self.pin_map):
            raise ValueError("PackageOption MetaPin indices must be positive")


@dataclass(frozen=True, slots=True)
class ComponentDefinition:
    """Static definition of a component (a "type").

    Geometry is referenced only through ``package_options``. Package objects
    live in the package-family catalogue, never on the component definition.
    """

    name: str
    meta_pins: tuple[MetaPin, ...]
    description: str = ""
    # Open classification tag: one of the KNOWN ``ComponentClass`` values (the
    # loader normalizes those to enum members) or any library-authored string
    # the code does not know yet — unknown classes load fine and behave like
    # OTHER, but keep the author's label (BOM / panels / future semantics).
    component_class: str = ComponentClass.OTHER
    metadata: dict[str, Any] = field(default_factory=dict)
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
    # ``package: {kind: …}`` without ``meta_pins`` or a package ``pins:`` count,
    # so the pin count (and any open
    # generator parameters, e.g. a pin header's ``rows``) are chosen by the
    # user at placement time. The default option then points at a PREVIEW body.
    parametric_pins: bool = False

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ComponentDefinition.name must not be empty")
        if not self.meta_pins:
            raise ValueError("ComponentDefinition requires at least one MetaPin")
        if not self.package_options:
            raise ValueError("ComponentDefinition requires a package option")

    def default_footprint(self, families: dict[str, PackageFamily]) -> Footprint:
        """Resolve the default package option into footprint geometry.

        For anything *placed*, use the instance-resolved geometry
        (``ComponentInstance.resolved_footprint``); reading
        the definition default for a placed component is the silent-wrong-body
        bug this method's name exists to make visible. Legitimate callers are
        instance-less contexts only: library preview, pre-placement ghost,
        BOM fallback.
        """
        from protoboard.domain.components.package.component_package import (
            resolve_component_package,
        )

        return resolve_component_package(self, None, families)

    def default_package_option(self) -> PackageOption:
        """The first package choice, required by the definition invariant."""
        return self.package_options[0]

    def default_package_ref(self) -> str:
        """The catalogue ref of the required default package choice."""
        return self.default_package_option().ref

    def meta_pin(self, index: int) -> MetaPin:
        """Return the MetaPin at the given 1-based ordinal."""
        if index < 1 or index > len(self.meta_pins):
            raise ValueError(f"MetaPin {index} out of range (1..{len(self.meta_pins)})")
        return self.meta_pins[index - 1]

    @property
    def refdes_prefix(self) -> str:
        """Refdes prefix: the explicit ``symbol`` if set, else the class default.

        ``symbol`` (e.g. 'R', 'U', 'LED') is also the footprint-picker label root.
        """
        return self.symbol or REFDES_PREFIX.get(self.component_class, "X")
