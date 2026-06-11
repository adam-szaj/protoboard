# SPDX-License-Identifier: MIT
"""Parametric body description for 2-pin through-hole components.

Replaces the cylinder-only ``BodyTypeSpec`` with a structured spec that
captures three orthogonal axes:

- **lead arrangement** — axial (leads exit opposite faces along the body
  axis) vs radial (leads exit the same face, side by side);
- **body shape** — cylinder, prism (box), or disc;
- **edge style** — sharp or rounded edges.

These three knobs cover the vast majority of physical 2-pin packages
(resistors, diodes, axial inductors, capacitors of every kind, MLCC,
LEDs, small fuses).  The renderer reads them directly; library YAMLs
describe variants in terms of physical dimensions only.

The ``component_class`` (electrical function) and ``footprint`` (pad
grid) stay separate concerns: the body spec is independent of both.
A single ``ComponentDefinition`` may reference variants from different
families if the same electrical part is sold in different physical
shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class LeadArrangement(Enum):
    """How the two leads exit the body.

    AXIAL — leads exit opposite faces along the body's main axis
            (resistor laid down, axial inductor, axial diode).
    RADIAL — leads exit the same face, parallel to each other
            (electrolytic capacitor standing, LED, MLCC).
    """

    AXIAL = "axial"
    RADIAL = "radial"


class BodyShape(Enum):
    """Gross geometry of the body.

    CYLINDER — surface of revolution about the body axis (resistor,
               axial inductor, electrolytic cap from the side).
    PRISM    — rectangular box (small ceramic cap, some MLCC,
               brick-style power resistors).
    DISC     — flat or candy-shaped (M&M) disc whose face is normal
               to the lead axis (ceramic disc cap, button cell,
               disc varistor).
    """

    CYLINDER = "cylinder"
    PRISM = "prism"
    DISC = "disc"


class EdgeStyle(Enum):
    """Edge finish on the body, used to pick the corner-radius profile."""

    SHARP = "sharp"
    ROUNDED = "rounded"


class DiscProfile(Enum):
    """Sub-shape for ``BodyShape.DISC``.

    FLAT  — uniform thickness, sharp or rounded rim (ceramic disc cap).
    CANDY — domed top, "M&M" silhouette (some film caps, button cells
            seen edge-on).
    """

    FLAT = "flat"
    CANDY = "candy"


# Default millimetre values used when a variant omits an optional
# dimension.  Centralised so renderer fallbacks and validation share one
# source of truth.
_DEFAULT_LEAD_DIAMETER_MM = 0.5


@dataclass(frozen=True)
class TwoPinBodySpec:
    """Parametric description of one 2-pin component body.

    Lives independently of ``ComponentClass`` and ``Footprint``.  Library
    YAMLs reference variants of a family by ``"{family}/{variant_id}"``
    on ``ComponentInstance.body_variant``; the renderer reads the spec to
    draw the body in either horizontal or vertical projection.

    All linear dimensions are in millimetres.  Which dimensions are
    required depends on ``shape``; ``__post_init__`` validates this.

    Args:
        family: Library family identifier (matches the YAML file stem).
        variant_id: Stable identifier within the family.
        label: Human-readable label for the picker UI.
        lead_arrangement: AXIAL or RADIAL.
        shape: CYLINDER, PRISM, or DISC.
        edge: SHARP or ROUNDED edge style.  Defaults to ROUNDED.
        polar: True for components with polarity (electrolytic cap,
            diode).  Drives polarity-marker rendering.
        length_mm: Dimension along the lead axis.  Required for CYLINDER
            and PRISM; not used for DISC (use ``thickness_mm``).
        diameter_mm: Outer diameter.  Required for CYLINDER and DISC.
        width_mm: Body width perpendicular to the lead axis (horizontal
            face).  Required for PRISM.
        height_mm: Body height perpendicular to both the lead axis and
            ``width_mm``.  Required for PRISM.
        thickness_mm: Body thickness along the lead axis.  Required for
            DISC; ignored for CYLINDER / PRISM.
        disc_profile: FLAT or CANDY.  Required iff ``shape == DISC``.
        lead_diameter_mm: Diameter of the lead wire itself.  Default
            0.5 mm.

    Raises:
        ValueError: when shape-specific required dimensions are missing
            or contradictory (e.g. CYLINDER without ``diameter_mm``,
            DISC without ``disc_profile``).
    """

    family: str
    variant_id: str
    label: str
    lead_arrangement: LeadArrangement
    shape: BodyShape
    edge: EdgeStyle = EdgeStyle.ROUNDED
    polar: bool = False
    length_mm: float | None = None
    diameter_mm: float | None = None
    width_mm: float | None = None
    height_mm: float | None = None
    thickness_mm: float | None = None
    disc_profile: DiscProfile | None = None
    lead_diameter_mm: float = _DEFAULT_LEAD_DIAMETER_MM

    def __post_init__(self) -> None:
        # Required-dimension matrix per shape.  Each ``required`` list
        # names the fields whose value must be a positive float for the
        # spec to be physically meaningful.
        if self.shape is BodyShape.CYLINDER:
            required = (("length_mm", self.length_mm),
                        ("diameter_mm", self.diameter_mm))
            if self.disc_profile is not None:
                raise ValueError(
                    f"{self._loc}: disc_profile only valid for shape=DISC"
                )
        elif self.shape is BodyShape.PRISM:
            required = (("length_mm", self.length_mm),
                        ("width_mm", self.width_mm),
                        ("height_mm", self.height_mm))
            if self.disc_profile is not None:
                raise ValueError(
                    f"{self._loc}: disc_profile only valid for shape=DISC"
                )
        elif self.shape is BodyShape.DISC:
            required = (("diameter_mm", self.diameter_mm),
                        ("thickness_mm", self.thickness_mm))
            if self.disc_profile is None:
                raise ValueError(
                    f"{self._loc}: shape=DISC requires disc_profile"
                )
        else:  # pragma: no cover — exhaustive over BodyShape
            raise ValueError(f"{self._loc}: unknown shape {self.shape!r}")

        for name, value in required:
            if value is None or value <= 0:
                raise ValueError(
                    f"{self._loc}: shape={self.shape.value} requires positive "
                    f"{name}, got {value!r}"
                )

        if self.lead_diameter_mm <= 0:
            raise ValueError(
                f"{self._loc}: lead_diameter_mm must be positive, got "
                f"{self.lead_diameter_mm!r}"
            )

    @property
    def _loc(self) -> str:
        """Diagnostic prefix for error messages."""
        return f"TwoPinBodySpec({self.family}/{self.variant_id})"

    def min_radial_pitch_mm(self) -> float:
        """Minimum pin spacing when the component is mounted radially.

        Two leads exiting the same face must be far enough apart to clear
        the body itself.  For a radial-mounted cylinder or disc the
        constraint is half its widest cross-section plus the lead wire
        diameter; for a prism it's based on ``width_mm``.

        Returns the minimum centre-to-centre pad pitch in millimetres.
        Callers convert to raster units against the board pitch.
        """
        if self.shape is BodyShape.CYLINDER:
            assert self.diameter_mm is not None
            return (self.diameter_mm + self.lead_diameter_mm) / 2.0
        if self.shape is BodyShape.DISC:
            assert self.diameter_mm is not None
            return (self.diameter_mm + self.lead_diameter_mm) / 2.0
        # PRISM
        assert self.width_mm is not None
        return (self.width_mm + self.lead_diameter_mm) / 2.0

    def min_axial_pitch_mm(self) -> float:
        """Minimum pin spacing when the component is mounted axially.

        For AXIAL arrangement the pads sit at the ends of the body, so
        the lead exits the body and turns 90° to reach the pad — the pad
        must be at least one body length apart.  For a DISC the
        thickness drives this.
        """
        if self.shape is BodyShape.CYLINDER or self.shape is BodyShape.PRISM:
            assert self.length_mm is not None
            return self.length_mm + self.lead_diameter_mm
        # DISC
        assert self.thickness_mm is not None
        return self.thickness_mm + self.lead_diameter_mm


@dataclass(frozen=True)
class TwoPinBodyFamily:
    """A named collection of ``TwoPinBodySpec`` variants.

    Loaded from one YAML file; the family name matches the file stem.
    All variants in a family share polarity (the ``polar`` flag is
    encoded on every variant for self-containment), but may differ in
    shape, lead arrangement, edge style and dimensions.

    Args:
        family: Family identifier (e.g. ``"electrolytic_capacitors"``).
        variants: Ordered tuple of variants.  The first entry is the
            default returned by ``default_variant()``.

    Raises:
        ValueError: if the family is empty or any variant's ``family``
            does not match this family's id.
    """

    family: str
    variants: tuple[TwoPinBodySpec, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.variants:
            raise ValueError(f"family {self.family!r} has no variants")
        for v in self.variants:
            if v.family != self.family:
                raise ValueError(
                    f"family {self.family!r} contains variant from "
                    f"{v.family!r}/{v.variant_id!r}"
                )
        # Ensure variant_id uniqueness within the family.
        ids = [v.variant_id for v in self.variants]
        if len(set(ids)) != len(ids):
            dups = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(
                f"family {self.family!r} has duplicate variant ids: {dups}"
            )

    def default_variant(self) -> TwoPinBodySpec:
        """Return the family's first variant (declaration order)."""
        return self.variants[0]

    def get_variant(self, variant_id: str) -> TwoPinBodySpec | None:
        """Look up a variant by id; return None when not found."""
        for v in self.variants:
            if v.variant_id == variant_id:
                return v
        return None
