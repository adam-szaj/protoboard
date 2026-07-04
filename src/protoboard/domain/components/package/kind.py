# SPDX-License-Identifier: MIT
"""Named, parameterized package *kinds*.

A ``kind`` is a field in a package definition (``package: {kind: dip_ic}``), not
a new entity. It sits *between* a generic solid (everything still to specify) and
a fully-specified footprint (nothing to specify): the look is known (a DIP-IC is a
dual-row prism with a notch; a DIP-switch is a plain dual-row prism) but the
geometry stays **parameterized** — chiefly by the owning component's pin count.

Each kind maps to a :class:`~.generators.base.PackageGenerator` registered in
``KIND_GENERATORS`` (declaratively, by defining a subclass — builtin modules
under :mod:`.generators` and library plugins register the same way). This
module keeps the **driver**: :func:`build_kind_package` dispatches through the
registry, default-fills / validates the parameter set against the generator's
``params`` schema, guards totality, and stamps ``kind`` / ``lineage`` / the
generator's colour onto the built body.

The renderer may honour the kind for a richer body (the notch); when it does not,
it falls back to the generic solid + pin layout (today's ``silhouette_for``), so a
kind never blocks rendering.

Pure domain: no Qt, no I/O.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from functools import lru_cache
from typing import Any

from protoboard.domain.components.footprints import Footprint
from protoboard.domain.components.package.generators import (
    KIND_GENERATORS,
    GeneratorContext,
    ResidualKind,
)
from protoboard.domain.components.package.generators.base import (
    MARGIN_HOLES as _MARGIN_HOLES,
)
from protoboard.domain.components.package.generators.base import (
    RASTER_MM as _RASTER_MM,
)
from protoboard.domain.components.package.loader import (
    PackageDefError,
    build_package,
)
from protoboard.domain.components.package.schema import ParamSchema, SchemaError
from protoboard.domain.components.package.shapes import (
    Cylinder,
    Disc,
    Package,
    Prism,
)

# Marker stamped on the synthetic fallback package (``Prism.kind``) so consumers
# can tell a *declared* body apart from this generic bbox stand-in and keep their
# pre-invariant behaviour (authored ``bendable_pins``, DRC body-overlap skip).
GENERIC_PACKAGE_KIND = "_generic"


def is_generic_package(package: object) -> bool:
    """True when *package* is the synthetic generic bbox fallback (not declared).

    Single predicate so the ``_generic`` tag-check is not duplicated across
    ``effective_bend_specs`` and the DRC engine. Checked on the ``kind`` tag
    *and* the lineage: a pending-kind placeholder (a partially-parameterised
    kind awaiting the package-picker) carries the target kind in ``kind`` but
    keeps ``_generic`` in its lineage — its geometry is still the synthetic
    bbox, so generic consumers must keep treating it as such.
    """
    if getattr(package, "kind", None) == GENERIC_PACKAGE_KIND:
        return True
    return GENERIC_PACKAGE_KIND in (getattr(package, "lineage", ()) or ())


@lru_cache(maxsize=None)
def _synth_generic_by_pins(
    pins: tuple[tuple[float, float], ...], bend_holes: float
) -> Package | None:
    if len(pins) < 1:
        return None
    steps = [
        [pins[i + 1][0] - pins[i][0], pins[i + 1][1] - pins[i][1]]
        for i in range(len(pins) - 1)
    ]
    spec = {
        "shape": "prism",
        "dims": {},  # bbox derived from pins + margin
        "bend": bend_holes,  # > 0 → non-rigid (is_rigid_package stays False)
        "margin": _MARGIN_HOLES * _RASTER_MM,
        "leads": {
            "direction": [0, 0, -1],
            "body_offset": {"unit": _RASTER_MM, "value": steps},
            "board_offset": "default",
        },
    }
    try:
        pkg = build_package(spec, "synth/generic")
    except PackageDefError:
        return None
    if isinstance(pkg, Prism):
        pkg = dataclasses.replace(
            pkg,
            kind=GENERIC_PACKAGE_KIND,
            lineage=("prism", GENERIC_PACKAGE_KIND),
        )
    return pkg


def synth_generic_package(
    fp: Footprint, *, bend_holes: float = 1.0
) -> Package | None:
    """A NON-rigid generic bbox package whose pins follow *fp*.

    The fallback body for a library part that declares no real package, so
    ``ComponentDefinition.package`` can be an invariant (always set). Geometry is
    the pin-cloud bbox + 0.5-hole margin (matching the legacy ``component_bbox``
    body footprint); ``bend > 0`` keeps ``is_rigid_package`` False so the part's
    bendable leads survive. Tagged ``kind="_generic"`` so bend-spec / DRC
    consumers can tell it from a declared body and keep today's behaviour.
    Returns ``None`` for a footprint with no pins. Cached by pin-cloud (the ~187
    library parts collapse to a handful of distinct clouds)."""
    pins = tuple((p.x, p.y) for p in fp.pin_positions())
    return _synth_generic_by_pins(pins, bend_holes)


def is_kind(name: object) -> bool:
    """True when *name* is a registered package kind."""
    return isinstance(name, str) and name in KIND_GENERATORS


def _shape_root(pkg: Package) -> str:
    if isinstance(pkg, Cylinder):
        return "cylinder"
    if isinstance(pkg, Disc):
        return "disc"
    return "prism"


def build_kind_package(
    kind: str | Sequence[str],
    ctx: GeneratorContext,
    schema: ParamSchema | None = None,
) -> GeneratorContext:
    """Generate the body for *kind* — one generator or a left→right chain.

    The driver around the registered :class:`PackageGenerator`s. For each step:

    1. **Default fill + validation.** The accumulated values are filled from
       the step's ``params`` schema; required/type checks run before the
       generator does. An explicit ``null`` value is a *hole* (a picker-editable
       declaration, per the "editable iff null" rule) — the build substitutes
       the default, but the hole is preserved in the stamped ``raw_values`` so
       the place-tool picker can offer it for input.
    2. **Generate.** ``generate(ctx)`` runs with *schema* (the owning package's
       declared value-tree schema, when the caller has one) riding on the
       context — a compute step consults it to resolve ``auto`` holes.
    3. **Stamp (final step only).** The declared *kind*, a ``lineage`` of
       ``(shape_root, kind)`` (so a renderer registered for the kind wins
       dispatch), the generator's convenience ``color``, and — when the
       generator declares parameters — its ``params`` schema + the declared
       value tree.

    **Chain.** *kind* may be a sequence of kinds: every step before the last is
    a **compute step** — a pure ``(ctx) -> ctx`` value transform (e.g. deriving
    ``dims`` from the pin count) whose output ``values`` feed the next step.  A
    compute step must be parameter-complete (its missing *required* parameters
    are a :class:`PackageDefError`, never a picker residual — users parameterise
    the final kind, not the plumbing) and must not build a package.  Only the
    **final** step builds the body, so a residual it surfaces carries every
    value the chain computed — re-running just that kind (what the picker's
    ``build_variant`` does) reproduces the chain's result without knowing the
    chain.

    **Partial application (final step).** Missing *required* parameters are not
    an error: the driver returns a context whose ``residual`` names the kind and
    the values bound so far. A generator may itself return a residual to
    *redirect* to another kind (a different parameter set); the driver
    re-dispatches redirects whose parameters are complete and surfaces
    incomplete ones (final step) or rejects them (compute step). Callers that
    need a concrete body must check ``result.package``.

    Raises :class:`PackageDefError` for an unknown kind, an empty chain, a
    redirect cycle, or parameters the kind cannot realise (e.g. an odd pin
    count for a DIP or 2×N connector).
    """
    steps = (kind,) if isinstance(kind, str) else tuple(kind)
    if not steps:
        raise PackageDefError("empty package kind chain")
    ctx = dataclasses.replace(ctx, schema=schema)
    current_values: dict[str, Any] = dict(ctx.values)

    for idx, step in enumerate(steps):
        final = idx == len(steps) - 1
        seen: set[str] = set()
        current = step
        while True:
            if current in seen:
                raise PackageDefError(
                    f"package kind redirect cycle: "
                    f"{' -> '.join([*seen, current])}")
            seen.add(current)
            gen_cls = KIND_GENERATORS.get(current)
            if gen_cls is None:
                raise PackageDefError(f"unknown package kind {current!r}")
            gen = gen_cls()
            # The declared tree keeps explicit-null holes; the build tree
            # replaces them with defaults (a hole is "ask the user", never a
            # build input).
            declared = gen.params.apply_to(current_values)
            concrete = {
                k: v for k, v in current_values.items() if v is not None
            }
            values = gen.params.apply_to(concrete)
            try:
                gen.params.check_required(values)
            except SchemaError as exc:
                if not final:
                    raise PackageDefError(
                        f"chain compute step {current!r} is missing required "
                        f"parameters: {exc}") from exc
                # Partially-applied kind: hand the remaining parameters to the
                # picker instead of failing the build.
                return dataclasses.replace(
                    ctx, package=None,
                    residual=ResidualKind(kind=current, values=declared))
            gen.params.validate(values)
            out = gen.generate(
                dataclasses.replace(ctx, values=values, package=None,
                                    residual=None))
            if out.residual is not None:
                # Redirect to another kind — loop; the completeness check at
                # the top decides whether it builds or surfaces.
                current = out.residual.kind
                current_values = dict(out.residual.values)
                continue
            if not final:
                if out.package is not None:
                    raise PackageDefError(
                        f"chain compute step {current!r} must not build a "
                        f"package (only the final step does)")
                current_values = dict(out.values)
                break  # next chain step
            pkg = out.package
            if pkg is None:
                raise PackageDefError(
                    f"kind {current!r} generator returned no package for "
                    f"{ctx.pin_count} pins")
            if not isinstance(pkg, (Prism, Cylinder, Disc)):
                return out.with_package(pkg)  # unknown shape — nothing to stamp
            field_names = {f.name for f in dataclasses.fields(pkg)}
            changes: dict[str, Any] = {
                # A kind-generated body descends from its shape root; the kind
                # name is the leaf, so a renderer registered for it wins the
                # dispatch.
                "lineage": (_shape_root(pkg), current),
            }
            if "kind" in field_names:
                changes["kind"] = current
            if gen.color is not None and getattr(pkg, "color", None) is None:
                changes["color"] = gen.color
            if not gen.params.is_empty() and pkg.schema is None:
                changes["schema"] = gen.params
                changes["raw_values"] = declared
            pkg = dataclasses.replace(pkg, **changes)
            return out.with_package(pkg)
    raise PackageDefError("unreachable: chain ended without a final step")


__all__ = ["GeneratorContext", "KIND_GENERATORS", "build_kind_package", "is_kind"]
