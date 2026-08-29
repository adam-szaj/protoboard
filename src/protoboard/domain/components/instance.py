# SPDX-License-Identifier: MIT
"""ComponentInstance — a placed-or-placeable copy of a definition.

The `id` is a UUID; `refdes` (e.g. "U1", "R5") is a user-facing label and
may be renamed without affecting connectivity (which references the UUID).
"""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from protoboard.domain.components.appearance import Appearance
from protoboard.domain.schematic.net import PinIdentity, PinRef

if TYPE_CHECKING:
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.footprints import Footprint
    from protoboard.domain.components.package.family import PackageFamily
    from protoboard.domain.components.package.shapes import Package


@dataclass(frozen=True, slots=True)
class ComponentInstance:
    """A placed-or-placeable copy of a definition.

    The instance is the factory for this component's pin electrical identity:
    pins that share a name (several ``GND``) get one shared :class:`PinIdentity`
    via :meth:`build_pin_identities`, and :meth:`pin_ref` hands out
    :class:`PinRef`\\ s carrying it. ``PinIdentity`` is internal to the
    component and never escapes except via the refs the instance produces.

    The instance itself stays immutable (``frozen``): ``pin_identities`` is a
    mutable container, but only the ``net_id`` *inside* each identity is ever
    mutated — the field binding never changes, so the frozen contract holds.
    """

    id: uuid.UUID
    definition_ref: str
    refdes: str
    value: str
    type: str = ""          # optional per-instance category override
    # Per-instance override of the package-family variant as
    # ``"{family}/{variant_id}"``; None → use the definition's
    # default ``PackageOption`` (or inline ``package``).
    package_variant: str | None = None
    pin_variant: str | None = None   # variant id from ComponentDefinition.variants or None
    # Manufacturing tolerance of ``value`` (e.g. "5%", "1%") — free text like
    # ``value`` itself; renderers decode it (the resistor tolerance band).
    # None = unspecified.
    tolerance: str | None = None
    # Per-instance visual-only override (fill colour, corner radius), layered
    # over the definition's appearance and the package/theme defaults. None =
    # inherit. Geometry/mask/DRC/routing are unaffected.
    appearance: Appearance | None = None
    # pin_number → shared PinIdentity for same-name pin groups. Excluded from
    # eq/hash (compare=False): two instances are equal by their value fields,
    # not by transient electrical state. Populated by build_pin_identities.
    pin_identities: dict[int, PinIdentity] = field(
        default_factory=dict, compare=False, hash=False
    )
    # Live back-reference to the definition this instance was placed from, and
    # the package-family catalogue, bound by :meth:`bind` once the instance
    # enters a project (the same ritual as build_pin_identities). NEVER
    # serialized — the loader reconstructs them from ``definition_ref`` + the
    # session catalogue. compare/hash excluded: identity is the value fields,
    # not these context handles. A placed instance is always bound; an unbound
    # instance (freshly deserialized, or a bare test stub) falls back to its
    # definition's default footprint, which is correct only when no variant was
    # chosen — so callers in a placement context must work on bound instances.
    definition: "ComponentDefinition | None" = field(
        default=None, compare=False, hash=False
    )
    families: "dict[str, PackageFamily] | None" = field(
        default=None, compare=False, hash=False
    )

    def __post_init__(self) -> None:
        if not self.definition_ref:
            raise ValueError("definition_ref must not be empty")
        if not self.refdes:
            raise ValueError("refdes must not be empty")

    def bind(
        self,
        definition: "ComponentDefinition",
        families: "dict[str, PackageFamily] | None",
    ) -> "ComponentInstance":
        """Return a copy of this instance carrying its definition + catalogue.

        The binding ritual that makes a placed instance self-sufficient for
        geometry: with ``definition`` and ``families`` bound, the instance can
        resolve its concrete footprint (variant-aware) without the caller
        threading the catalogue through every signature. Frozen-safe — returns
        a ``replace``'d copy; the live ``pin_identities`` dict is carried over
        by reference (replace preserves compare=False fields). Call wherever an
        instance enters the project (see ``build_pin_identities_for``).
        """
        return dataclasses.replace(
            self, definition=definition, families=families
        )

    def resolved_package(self) -> "Package":
        """The resolved package of this placed instance.

        The single source of truth for "the body this placed component
        actually carries": the instance's ``package_variant``, then the
        definition's family ref, then its inline package (see
        ``resolve_component_package``). Requires :meth:`bind`; an unbound
        instance raises :class:`~protoboard.errors.UnboundInstanceError` —
        falling back to ``defn.package`` in a placement context is the exact
        silent-wrong-body bug this accessor removes. ``None`` means the part
        genuinely declares no package.
        """
        if self.definition is None:
            from protoboard.errors import UnboundInstanceError

            raise UnboundInstanceError(
                f"ComponentInstance {self.refdes!r} (def "
                f"{self.definition_ref!r}) is not bound to a definition; call "
                "bind() before resolving its geometry"
            )
        from protoboard.domain.components.package.component_package import (
            resolve_component_package,
        )

        return resolve_component_package(
            self.definition, self, self.families or {}
        )

    def resolved_footprint(self) -> "Footprint":
        """The footprint of this instance's *resolved* package variant.

        The single source of truth for "the footprint this placed component
        actually uses" — and the ONLY supported way to get a placed
        component's geometry. Requires :meth:`bind` to have run; if this
        instance is unbound (no ``definition``) it raises
        :class:`~protoboard.errors.UnboundInstanceError`, because falling back
        to the definition default in a placement context is the exact
        silent-wrong-body bug this model removes.

        When the instance *is* bound but no variant/family resolves, the
        definition's ``package`` (always set) is its genuine default geometry
        — not a fallback. The distinction is deliberate: unbound = programming
        error (raise); no variant = correct default.
        """
        from protoboard.domain.components.package.component_package import (
            package_to_footprint,
        )

        return package_to_footprint(self.resolved_package())

    @staticmethod
    def new(
        definition_ref: str, refdes: str, value: str, type: str = "",
        pin_variant: str | None = None,
        package_variant: str | None = None,
        appearance: Appearance | None = None,
        tolerance: str | None = None,
    ) -> ComponentInstance:
        """Create an instance with a freshly generated UUID."""
        return ComponentInstance(
            id=uuid.uuid4(),
            definition_ref=definition_ref,
            refdes=refdes,
            value=value,
            type=type,
            pin_variant=pin_variant,
            package_variant=package_variant,
            appearance=appearance,
            tolerance=tolerance,
        )

    def build_pin_identities(self, defn: "ComponentDefinition") -> None:
        """(Re)build one electrical identity for every MetaPin."""
        self.pin_identities.clear()
        for meta_pin_index in range(1, len(defn.meta_pins) + 1):
            self.pin_identities[meta_pin_index] = PinIdentity(
                key=(self.id, meta_pin_index)
            )

    def pin_ref(self, pin_number: int) -> PinRef:
        """Return a PinRef for *pin_number*, carrying its shared PinIdentity.

        Call :meth:`build_pin_identities` first to attach the MetaPin's live
        electrical identity.
        """
        return PinRef(self.id, pin_number, identity=self.pin_identities.get(pin_number))
