# SPDX-License-Identifier: MIT
"""Package variant families — a named body standard with dimensional variants.

The legacy ``TwoPinBodyPackage`` collected dimensional variants (small / typ /
large) of one industry-standard body (DO-35, R-6, HC-49 …) so the GUI could
offer a picker.  This is the package-model equivalent: a :class:`PackageFamily`
groups built :class:`Package` instances under one ``family_id`` with stable
``variant_id``s and a default.

Addressing mirrors the legacy convention: ``"{family_id}/{variant_id}"`` is the
canonical reference stored on a component (replacing ``body_variant``).

Pure domain: a variant is a fully-built ``Package`` (geometry + presentation);
the loader assembles families from YAML, this layer only structures them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from protoboard.domain.components.package.shapes import Package


@dataclass(frozen=True, slots=True)
class PackageVariant:
    """One dimensional variant within a family: an id, a label, a built package."""

    variant_id: str
    label: str
    package: Package


@dataclass(frozen=True, slots=True)
class PackageFamily:
    """A named body standard (e.g. ``DO-35``) with dimensional variants.

    ``variants`` are ordered; the first is the default unless ``default_id``
    names another.  ``variant_id``s must be unique within the family.
    """

    family_id: str
    variants: tuple[PackageVariant, ...] = field(default_factory=tuple)
    label: str = ""
    default_id: str | None = None

    def __post_init__(self) -> None:
        if not self.variants:
            raise ValueError(f"package family {self.family_id!r} has no variants")
        ids = [v.variant_id for v in self.variants]
        if len(set(ids)) != len(ids):
            dups = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(
                f"package family {self.family_id!r} has duplicate variant ids: {dups}"
            )
        if self.default_id is not None and self.default_id not in ids:
            raise ValueError(
                f"package family {self.family_id!r} default {self.default_id!r} "
                f"is not one of its variants"
            )

    @property
    def display_label(self) -> str:
        return self.label or self.family_id

    def default_variant(self) -> PackageVariant:
        """The default variant — ``default_id`` if set, else the first."""
        if self.default_id is not None:
            return self.get_variant(self.default_id)  # type: ignore[return-value]
        return self.variants[0]

    def get_variant(self, variant_id: str) -> PackageVariant | None:
        for v in self.variants:
            if v.variant_id == variant_id:
                return v
        return None

    def address(self, variant_id: str) -> str:
        """Canonical ``"family_id/variant_id"`` reference for *variant_id*."""
        return f"{self.family_id}/{variant_id}"


def resolve_variant(
    families: dict[str, PackageFamily], address: str
) -> PackageVariant | None:
    """Resolve a ``"family_id/variant_id"`` address against *families*.

    Returns the variant, or ``None`` when the family or variant is unknown.
    A bare ``"family_id"`` resolves to that family's default.  A family id may
    itself contain ``/`` (the inline-option families are ``@defn/option``), so
    the split is at the LAST slash — splitting at the first silently failed
    every ``@defn/option/variant`` address and the caller fell through to the
    definition default (wrong body when a non-default inline option was
    picked).
    """
    family = families.get(address)
    if family is not None:
        return family.default_variant()
    family_id, sep, variant_id = address.rpartition("/")
    if not sep:
        return None
    family = families.get(family_id)
    if family is None:
        return None
    return family.get_variant(variant_id)
