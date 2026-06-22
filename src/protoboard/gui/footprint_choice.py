# SPDX-License-Identifier: MIT
"""Footprint-choice seam between the library panel and the package model.

The picker dialogs (:mod:`gui.dialogs.footprint_picker_dialog`,
:mod:`gui.dialogs.kind_variant_dialog`) are pure UI: they render whatever a
:class:`FootprintChoiceProvider` hands them and never import the package /
parameter model. This keeps the package-type knowledge on the provider (the
boundary that owns it) and lets a richer, model-backed provider replace the
default one without touching the dialogs.

The view types are plain data the dialogs display; the provider answers four
questions the picker needs:

* which footprints does this component offer, and is each a *package* (placed as
  is) or a *kind* (needs parameterising — opens the second pop-up)?
* which of them were used recently (already prepared / filtered — the UI does not
  compute the scope)?
* for a kind, what parameters must the user supply, and what variants of it were
  built recently?
* build a concrete (package) address from a kind address + the user's values.

:class:`DefaultFootprintChoiceProvider` ships today: every footprint resolves as
a *package* unless its resolved body carries a ``kind`` (then it is reported as a
kind), no kind parameters are offered, and ``build_variant`` is a pass-through.
With it the first pop-up works and the second never triggers; when the parameter
model lands behind a new provider, the second pop-up comes alive unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.package.family import PackageFamily


@dataclass(frozen=True, slots=True)
class FootprintOptionView:
    """One selectable footprint in the first pop-up.

    ``is_kind`` True → confirming this row opens the kind-variant pop-up; False →
    it is a fully-specified package, placed directly.
    """

    label: str
    address: str
    is_kind: bool = False


@dataclass(frozen=True, slots=True)
class ParamSpec:
    """One user-supplied parameter of a kind (an input box in the second pop-up).

    ``type`` is ``"float"`` / ``"int"`` / ``"str"`` — used to validate the input.
    """

    name: str
    type: str = "str"


@dataclass(frozen=True, slots=True)
class KindVariantView:
    """A previously-built variant of a kind (a row in the second pop-up).

    ``address`` is an already-built *package* address — re-picking it needs no
    further parameters.
    """

    label: str
    address: str


@runtime_checkable
class FootprintChoiceProvider(Protocol):
    """What the picker dialogs ask of the package model (see module docstring)."""

    def options(self, defn: ComponentDefinition) -> list[FootprintOptionView]:
        """The footprints *defn* offers, in declared order."""
        ...

    def recent(
        self, defn_ref: str, offered: set[str]
    ) -> list[FootprintOptionView]:
        """Recently-used footprints for *defn_ref*, prepared and filtered to
        *offered*, most-recent first. The UI renders these on top as-is."""
        ...

    def kind_params(self, address: str) -> list[ParamSpec]:
        """Parameters the user must supply for the kind at *address* (empty when
        *address* is a package or the kind needs nothing)."""
        ...

    def recent_variants(self, address: str) -> list[KindVariantView]:
        """Previously-built variants of the kind at *address*, most-recent first."""
        ...

    def build_variant(self, address: str, values: dict[str, str]) -> str | None:
        """Build a concrete package address from a kind *address* + user *values*;
        ``None`` when the values are invalid."""
        ...


class DefaultFootprintChoiceProvider:
    """Today's behaviour: footprints are packages unless their resolved body
    carries a ``kind``; no kind parameters / recent variants are offered.

    *package_families* is the session catalogue (used to resolve ref addresses
    and to read ``kind`` off the resolved body). It may be empty before a session
    is wired — then ``is_kind`` simply reports ``False`` (never blocks placement).
    """

    def __init__(self, package_families: dict[str, PackageFamily] | None = None):
        self._families: dict[str, PackageFamily] = package_families or {}

    def set_package_families(
        self, families: dict[str, PackageFamily] | None
    ) -> None:
        self._families = families or {}

    def options(self, defn: ComponentDefinition) -> list[FootprintOptionView]:
        from protoboard.services.library.loader import inline_option_address

        out: list[FootprintOptionView] = []
        root = defn.refdes_prefix
        if defn.footprints:
            for opt in defn.footprints:
                if opt.ref is not None:
                    addr = opt.ref
                    is_kind = self._ref_is_kind(opt.ref)
                else:
                    addr = inline_option_address(defn.name, opt.name)
                    is_kind = self._package_is_kind(opt.package)
                out.append(
                    FootprintOptionView(
                        label=f"{root}_{opt.name}", address=addr, is_kind=is_kind
                    )
                )
            return out

        # A component-level ``package: {kind: ...}`` (no footprints list, e.g. a
        # pin-header / IC) is the part's only body. Surface it as a single option
        # carrying its kind flag, so the picker can offer to parameterise it; a
        # plain inline package is not reported here (it places with no choice).
        if self._package_is_kind(defn.package):
            return [
                FootprintOptionView(
                    label=f"{root}_{defn.package.kind}",  # type: ignore[union-attr]
                    address="",  # inherits defn.package; no instance variant
                    is_kind=True,
                )
            ]

        # Legacy fallback — catalogue families fitting the singular footprint.
        if not self._families:
            return out
        from protoboard.domain.components.package.generated_families import (
            offered_body_families,
        )

        for pkg in offered_body_families(defn.footprint, self._families):
            for v in pkg.variants:
                label = f"{pkg.family_id} / {v.variant_id}"
                if v.label and v.label != v.variant_id:
                    label = f"{label} — {v.label}"
                out.append(
                    FootprintOptionView(
                        label=label,
                        address=f"{pkg.family_id}/{v.variant_id}",
                        is_kind=self._package_is_kind(v.package),
                    )
                )
        return out

    def recent(
        self, defn_ref: str, offered: set[str]
    ) -> list[FootprintOptionView]:
        from protoboard.gui import recent_footprints

        out: list[FootprintOptionView] = []
        for r in recent_footprints.load():
            if r.definition_ref == defn_ref and r.address in offered:
                # A recorded pick is an already-built package address, so it is
                # never a kind (re-using it is one confirm, no second pop-up).
                out.append(
                    FootprintOptionView(
                        label=r.label, address=r.address, is_kind=False
                    )
                )
        return out

    def kind_params(self, address: str) -> list[ParamSpec]:
        # No parameter model yet: a kind builds from the component's own data
        # (pin count / declared rows). The model-backed provider fills this in.
        return []

    def recent_variants(self, address: str) -> list[KindVariantView]:
        return []

    def build_variant(self, address: str, values: dict[str, str]) -> str | None:
        # No parameters to apply — the kind address resolves as-is.
        return address

    # -- helpers -------------------------------------------------------------

    def _ref_is_kind(self, address: str) -> bool:
        from protoboard.domain.components.package.family import resolve_variant

        variant = resolve_variant(self._families, address)
        return variant is not None and self._package_is_kind(variant.package)

    @staticmethod
    def _package_is_kind(package: object) -> bool:
        return getattr(package, "kind", None) is not None
