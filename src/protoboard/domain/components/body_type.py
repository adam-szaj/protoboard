# SPDX-License-Identifier: MIT
"""Physical body-type definitions for cylindrical through-hole components.

Body types are loaded from ``assets/body_types/*.yaml`` at application start
and are independent of the component library.  A ``ComponentInstance`` stores
an optional ``body_variant`` reference in the format ``"{family}/{variant_id}"``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BodyVariant:
    """One physical variant within a body-type family (e.g. '1/4 W resistor')."""

    id: str
    label: str
    diameter_mm: float
    length_mm: float
    lead_diameter_mm: float = 0.5

    def min_vertical_pitch_mm(self) -> float:
        """Minimum pin spacing for vertical mounting.

        A vertically-mounted cylinder needs at least half its own diameter plus
        half the lead wire diameter between the two pad centres.
        """
        return (self.diameter_mm + self.lead_diameter_mm) / 2.0


@dataclass(frozen=True)
class BodyTypeSpec:
    """A family of body variants sharing the same shape class."""

    family: str
    shape: str          # "cylinder" — only shape supported for now
    polar: bool         # True → component has polarity (diode, polar cap)
    variants: tuple[BodyVariant, ...]

    def default_variant(self) -> BodyVariant:
        return self.variants[0]

    def get_variant(self, variant_id: str) -> BodyVariant | None:
        for v in self.variants:
            if v.id == variant_id:
                return v
        return None
