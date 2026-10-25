# SPDX-License-Identifier: MIT
"""Per-package body shape renderers — compatibility surface.

The implementation lives in :mod:`protoboard.services.rendering.packages`: one
renderer class per package lineage node (``CylinderRenderer`` /
``PrismRenderer`` / ``DiscRenderer`` + user plugins), dispatched by
:func:`~protoboard.services.rendering.packages.renderer_for` over the package's
``lineage``.  This module keeps the historical entry points stable:

- :func:`body_shape_for` — the production dispatch (unchanged signature);
- :func:`PackageBody` — constructor-compatible factory returning the
  lineage-dispatched renderer (tests build bodies directly through it);
- ``GenericBody`` — a plain bbox renderer for direct rendering clients.

The legacy hand-coded ``TO92Body`` / ``TO220Body`` are gone: TO-92 renders
through the flat-sided radial :class:`CylinderRenderer` and TO-220 through the
``TO-220`` renderer plugin (``library/renderers/to220.py``), each guarded by
its parity tests.

Usage::

    shape = body_shape_for(defn)
    items = shape.draw(positions, fill, border, alpha, z, theme)
    for item in items:
        scene.addItem(item)
"""

from __future__ import annotations

from protoboard.domain.components.appearance import Appearance
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package.component_package import (
    resolve_component_package,
)
from protoboard.domain.components.package.family import PackageFamily
from protoboard.domain.components.package.pin_mapping import resolve_package_option
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.services.rendering.packages import (
    BodyShape,
    GenericBody,
    PackageRenderer,
)

__all__ = [
    "BodyShape",
    "GenericBody",
    "PackageBody",
    "body_shape_for",
]


# Historically ``PackageBody`` was one class whose ``draw`` branched on the
# package shape; the branches are now ``PackageRenderer`` subclasses and the
# dispatch lives in ``PackageRenderer.__new__`` (constructing the base class
# yields the lineage-matched subclass).  The old name stays valid for both
# construction (``PackageBody(pkg)``) and ``isinstance`` checks.
PackageBody = PackageRenderer


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def body_shape_for(
    defn: ComponentDefinition,
    placement: ComponentPlacement | None = None,
    instance: ComponentInstance | None = None,
    package_families: dict[str, PackageFamily] | None = None,
) -> BodyShape:
    """Return the appropriate :class:`BodyShape` for *defn*.

    A parametric package (resolved from the instance/definition/catalogue via
    :func:`resolve_component_package`) is the source of truth for the body and
    renders through the lineage-dispatched :class:`PackageRenderer` subclass
    (see :func:`protoboard.services.rendering.packages.renderer_for`). Missing
    package references raise; plain pin-cloud bodies use ``kind: generic``.
    """
    cls = defn.component_class
    vf = placement.vertical_flipped if placement is not None else False
    side = placement.side if placement is not None else "top"
    catalogue = package_families
    if catalogue is None:
        catalogue = instance.families if instance is not None else None
    catalogue = catalogue if catalogue is not None else {}
    option = resolve_package_option(
        defn, instance.package_variant if instance is not None else None, catalogue
    )
    # Visual-only override (fill colour, corner radius) layered over the
    # package/theme defaults, per field: an explicit instance override wins,
    # then the placed footprint option's own ``style:`` (an SMD chip may be
    # near-black while the axial variants keep the theme colour), then the
    # component-wide definition style.
    eff_appearance = Appearance.resolve(
        instance.appearance if instance is not None else None,
        option.style,
        defn.appearance,
    )

    # Package geometry comes from the selected catalogue address or the
    # definition's default PackageOption reference.
    # Polarity / anchor come from the class and placement, not the
    # (class-agnostic) package.
    pkg = resolve_component_package(defn, instance, catalogue)
    return PackageBody(
        pkg,
        component_class=cls,
        vertical_flipped=vf,
        appearance=eff_appearance,
        side=side,
        placement=placement,
        instance=instance,
    )
