# SPDX-License-Identifier: MIT
"""Per-package body shape renderers — compatibility surface.

The implementation lives in :mod:`protoboard.gui.renderers.packages`: one
renderer class per package lineage node (``CylinderRenderer`` /
``PrismRenderer`` / ``DiscRenderer`` + user plugins), dispatched by
:func:`~protoboard.gui.renderers.packages.renderer_for` over the package's
``lineage``.  This module keeps the historical entry points stable:

- :func:`body_shape_for` — the production dispatch (unchanged signature);
- :func:`PackageBody` — constructor-compatible factory returning the
  lineage-dispatched renderer (tests build bodies directly through it);
- ``GenericBody`` — the bare-bbox fallback for a component with no package.

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
from protoboard.domain.components.package.family import PackageFamily
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.gui.renderers.packages import (
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
    (see :func:`protoboard.gui.renderers.packages.renderer_for`); a part
    without a package draws a generic bbox.
    """
    cls = defn.component_class
    vf = placement.vertical_flipped if placement is not None else False
    side = placement.side if placement is not None else "top"
    # Visual-only override (fill colour, corner radius) layered over the
    # package/theme defaults — instance wins over definition, per field.
    eff_appearance = Appearance.resolve(
        instance.appearance if instance is not None else None,
        defn.appearance,
    )

    # A parametric package (new spec model) is the source of truth for the
    # body geometry when one resolves: the instance's package_variant, the
    # definition's package_family_ref (both against the package-family
    # catalogue), or an inline defn.package — see resolve_component_package.
    # Polarity / anchor come from the class and placement, not the
    # (class-agnostic) package.
    from protoboard.domain.components.package import resolve_component_package
    pkg = resolve_component_package(defn, instance, package_families or {})
    if pkg is not None:
        return PackageBody(
            pkg, component_class=cls, vertical_flipped=vf,
            appearance=eff_appearance, side=side,
            placement=placement,
        )

    # No explicit package resolved: a footprint is only a pin cloud (pin count
    # + spacing) and does not determine a body — the same footprint fits many
    # bodies (a SIL-3 is a TO-92, a header, a trimmer…). A part that wants a
    # specific silhouette declares it via ``package`` / ``package_family_ref``
    # (resolved above). Without one, draw the generic bbox body.
    return GenericBody()
