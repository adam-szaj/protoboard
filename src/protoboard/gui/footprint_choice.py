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

:class:`DefaultFootprintChoiceProvider` is backed by the kind-generator
registry (:mod:`protoboard.domain.components.package.generators`): a footprint
resolves as a *kind* when its body carries one; the kind's parameters are the
*holes* (explicit-null fields) of the generator's ``params`` schema stamped on
the body; ``build_variant`` re-runs the generator with the user's values and
registers the concrete result in the session package catalogue.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.package.family import PackageFamily
    from protoboard.domain.components.package.schema import ParamSchema


@dataclass(frozen=True, slots=True)
class PackageOptionView:
    """One selectable footprint in the first pop-up.

    ``is_kind`` True → confirming this row opens the kind-variant pop-up; False →
    it is a fully-specified package, placed directly.
    """

    label: str
    address: str
    is_kind: bool = False
    namespace: tuple[str, ...] = ()


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

    def options(self, defn: ComponentDefinition) -> list[PackageOptionView]:
        """The footprints *defn* offers, in declared order."""
        ...

    def recent(self, defn_ref: str, offered: set[str]) -> list[PackageOptionView]:
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
    """Registry-backed provider: footprints are packages unless their resolved
    body carries a ``kind``; kind parameters come from the generator's schema.

    *package_families* is the session catalogue (used to resolve ref addresses,
    to read ``kind`` off the resolved body, and as the registration target for
    variants built by :meth:`build_variant` — the session shares the dict, so a
    freshly-built variant resolves at placement with no extra wiring). It may
    be empty before a session is wired — then ``is_kind`` simply reports
    ``False`` (never blocks placement).

    The provider remembers the definition of the last :meth:`options` call:
    the picker's follow-up questions (``kind_params`` / ``build_variant``)
    arrive with only an *address*, and the component-level inline kind
    (address ``""``) plus the pin count are properties of that definition.
    The dialogs run one interaction at a time, so the latch is safe.
    """

    def __init__(self, package_families: dict[str, PackageFamily] | None = None):
        # Keep the caller's dict object (even when empty): build_variant
        # registers freshly-built variants into it, and the session must see
        # them — ``or {}`` would silently swap in a private copy.
        self._families: dict[str, PackageFamily] = (
            package_families if package_families is not None else {}
        )
        self._last_defn: ComponentDefinition | None = None

    def set_package_families(self, families: dict[str, PackageFamily] | None) -> None:
        self._families = families if families is not None else {}

    def options(self, defn: ComponentDefinition) -> list[PackageOptionView]:
        self._last_defn = defn
        out: list[PackageOptionView] = []
        root = defn.refdes_prefix
        from protoboard.domain.components.package.generated_families import (
            offered_package_options,
        )

        for option in offered_package_options(defn, self._families):
            out.append(
                PackageOptionView(
                    label=f"{root}_{option.name}",
                    address=option.ref,
                    is_kind=self._ref_is_kind(option.ref),
                    namespace=tuple(option.ref.split("/")[:-1]),
                )
            )
        return out

    def accepts_selection(self, defn: ComponentDefinition, address: str) -> bool:
        """Whether *address* is a declared leaf or a built declared kind."""
        if any(option.address == address for option in self.options(defn)):
            return True
        from protoboard.domain.components.package.family import resolve_variant

        selected = resolve_variant(self._families, address)
        selected_kind = (
            getattr(selected.package, "kind", None) if selected is not None else None
        )
        if not isinstance(selected_kind, str):
            return False
        for option in defn.package_options:
            declared = resolve_variant(self._families, option.ref)
            if (
                declared is not None
                and getattr(declared.package, "kind", None) == selected_kind
            ):
                return True
        return False

    def recent(self, defn_ref: str, offered: set[str]) -> list[PackageOptionView]:
        from protoboard.gui import recent_footprints

        out: list[PackageOptionView] = []
        for r in recent_footprints.load():
            if r.definition_ref == defn_ref and r.address in offered:
                # A recorded pick is an already-built package address, so it is
                # never a kind (re-using it is one confirm, no second pop-up).
                out.append(
                    PackageOptionView(label=r.label, address=r.address, is_kind=False)
                )
        return out

    def kind_params(self, address: str) -> list[ParamSpec]:
        """The kind's *holes*: schema fields whose stamped value is null.

        The generator's ``params`` schema and the declared value tree ride on
        the resolved body (stamped by the kind driver / the pending-kind
        placeholder); a field is an input iff it is editable under the
        "editable iff null" rule. A concrete (fully-parameterised) kind body
        therefore offers nothing — one confirm, no second stage.
        """
        found = self._kind_package(address)
        if found is None:
            return []
        _kind, schema, declared = found
        if schema is None or schema.is_empty():
            return []
        out: list[ParamSpec] = []
        for view in schema.fields_view(declared):
            if view.editable:
                spec_type = view.spec.type
                out.append(
                    ParamSpec(
                        name=view.path,
                        type=spec_type if isinstance(spec_type, str) else "str",
                    )
                )
        return out

    def recent_variants(self, address: str) -> list[KindVariantView]:
        """Previously-built parameter sets of this kind, rebuilt for offer.

        A built variant's family lives only in the session catalogue, so each
        persisted parameter set is **re-run** through the generator here (and
        registered, so confirming the row resolves with no further work).  The
        rebuild uses the *current* component's pin count — the remembered
        thing is the parameter set, not a fixed geometry.  Entries whose kind
        no longer builds (plugin gone, parameters now unrealisable) are
        skipped silently.
        """
        found = self._kind_package(address)
        defn = self._last_defn
        if found is None or defn is None:
            return []
        kind, _schema, _declared = found

        from protoboard.gui import recent_kind_variants

        out: list[KindVariantView] = []
        seen: set[str] = set()
        for entry in recent_kind_variants.load():
            if entry.kind != kind:
                continue
            built = self._build_and_register(
                kind, dict(entry.values), len(defn.meta_pins)
            )
            if built is None:
                continue
            built_address, label = built
            if built_address in seen:
                continue
            seen.add(built_address)
            out.append(KindVariantView(label=label, address=built_address))
        return out

    def build_variant(self, address: str, values: dict[str, str]) -> str | None:
        """Run the kind generator with the user's *values*; register + address.

        Without values the kind resolves as-is (the component's own data —
        pin count, declared parameters — already fixes the body). With values,
        the concrete result is registered as a single-variant family in the
        session catalogue and its ``family/variant`` address returned; the
        placement then resolves it like any other package variant.  The full
        parameter set is remembered (``recent_kind_variants``) so the kind
        dialog can re-offer it as a one-confirm row.
        """
        if not values:
            return address
        found = self._kind_package(address)
        defn = self._last_defn
        if found is None or defn is None:
            return None
        kind, schema, declared = found

        merged = {k: v for k, v in declared.items() if v is not None}
        merged.update(self._coerce(values, schema))
        built = self._build_and_register(kind, merged, len(defn.meta_pins))
        if built is None:
            return None
        built_address, label = built
        from protoboard.gui import recent_kind_variants

        recent_kind_variants.add(kind, merged, label)
        return built_address

    def _build_and_register(
        self,
        kind: str,
        merged: dict[str, object],
        pin_count: int,
    ) -> tuple[str, str] | None:
        """Build *kind* from the full parameter set *merged* and register it.

        Returns ``(address, label)`` of the registered single-variant family
        in the session catalogue, or ``None`` when the parameters cannot
        realise a body (or leave required holes).  Shared by
        :meth:`build_variant` (fresh user input) and :meth:`recent_variants`
        (persisted parameter sets).
        """
        from protoboard.domain.components.package.family import (
            PackageFamily,
            PackageVariant,
        )
        from protoboard.domain.components.package.generated_families import STD
        from protoboard.domain.components.package.generators import (
            KIND_GENERATORS,
            GeneratorContext,
        )
        from protoboard.domain.components.package.kind import (
            build_kind_package,
        )
        from protoboard.errors import ProtoboardError

        ctx = GeneratorContext(pin_count=pin_count, values=merged)
        try:
            result = build_kind_package(kind, ctx)
        except (ProtoboardError, ValueError):
            # ValueError covers PackageDefError (unrealisable parameters).
            return None
        if result.package is None:
            return None  # still-missing parameters — nothing to place yet

        gen_cls = KIND_GENERATORS.get(kind)
        label = (
            gen_cls(kind).variant_label(kind, ctx)
            if gen_cls is not None
            else f"{kind}-{pin_count}"
        )
        slug = "".join(
            f"-{k}{merged[k]}" for k in sorted(merged) if merged[k] is not None
        )
        family_id = f"{kind}-{pin_count}{slug}"
        self._families[family_id] = PackageFamily(
            family_id=family_id,
            label=label,
            variants=(
                PackageVariant(variant_id=STD, label=label, package=result.package),
            ),
        )
        return f"{family_id}/{STD}", label

    # -- helpers -------------------------------------------------------------

    def _kind_package(
        self, address: str
    ) -> tuple[str, ParamSchema | None, dict[str, object]] | None:
        """Resolve *address* to ``(kind, schema, declared values)`` — or None.

        Address ``""`` is the component-level inline kind (the definition of
        the last ``options()`` call); anything else is a catalogue ref.

        The schema is the kind's OWN parameter set — ``generator.params``
        (which serves the YAML-declared schema when the kind node binds one).
        A node-built body also carries the package *value-tree* schema
        (``dims.*`` / ``leads.*``) and a full-tree ``raw_values`` — those are
        Properties-panel metadata, not picker parameters, so the declared
        values are filtered down to the parameter schema's fields (a DIP
        yields ``{}`` — one confirm, no second pop-up).
        """
        pkg: object | None = None
        if address:
            from protoboard.domain.components.package.family import (
                resolve_variant,
            )

            variant = resolve_variant(self._families, address)
            pkg = variant.package if variant is not None else None
        kind = getattr(pkg, "kind", None)
        if pkg is None or not isinstance(kind, str):
            return None
        from protoboard.domain.components.package.generators import (
            KIND_GENERATORS,
        )

        gen_cls = KIND_GENERATORS.get(kind)
        schema: ParamSchema | None
        if gen_cls is not None:
            schema = gen_cls(kind).params
        else:
            schema = getattr(pkg, "schema", None)  # generator gone — best effort
        raw = getattr(pkg, "raw_values", None)
        declared = dict(raw) if raw is not None else {}
        if schema is not None:
            declared = {k: v for k, v in declared.items() if k in schema.fields}
        return kind, schema, declared

    @staticmethod
    def _coerce(
        values: dict[str, str], schema: ParamSchema | None
    ) -> dict[str, object]:
        """User input (strings from the dialog) → typed parameter values."""
        specs = schema.fields if schema is not None else {}
        out: dict[str, object] = {}
        for name, raw in values.items():
            spec = specs.get(name)
            spec_type = spec.type if spec is not None else "str"
            try:
                if spec_type == "int":
                    out[name] = int(raw)
                elif spec_type == "float":
                    out[name] = float(raw)
                elif spec_type == "bool":
                    out[name] = raw.strip().lower() in ("1", "true", "yes")
                else:
                    out[name] = raw
            except ValueError:
                out[name] = raw
        return out

    def _ref_is_kind(self, address: str) -> bool:
        from protoboard.domain.components.package.family import resolve_variant

        variant = resolve_variant(self._families, address)
        return variant is not None and self._package_is_kind(variant.package)

    @staticmethod
    def _package_is_kind(package: object) -> bool:
        return getattr(package, "kind", None) is not None
