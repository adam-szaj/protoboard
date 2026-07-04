# SPDX-License-Identifier: MIT
"""Package-kind generator base class and the dispatch registry.

A *kind* is a parameterized package family (``package: {kind: dip_ic}``) — a
recipe, not a body: it cannot be rendered or assigned until a **generator**
turns the parameter set *{K}* into a concrete :class:`Package`. This module
defines the generator contract; :func:`~..kind.build_kind_package` is the
driver that dispatches through the registry and enforces the contract.

Registration is declarative and mirrors the renderer registry
(:mod:`protoboard.gui.renderers.packages.base`), with the same two spellings:
the :func:`register_generator` decorator (``@register_generator(
kind="dip_ic")``, the preferred one — the counterpart of
``@register_renderer``) or a subclass that sets ``generates = "<kind>"``;
either enters ``KIND_GENERATORS`` the moment the class body executes —
dropping a plugin file into a library's ``generators/`` directory is all it
takes (see :mod:`protoboard.services.library.plugin_discovery`).
Later registrations win, so a user plugin overrides a builtin. A generator is
correlated with an (optional) renderer purely by the shared kind name — the
generator side is pure domain, the renderer side is GUI; neither imports the
other.

Contract for :meth:`PackageGenerator.generate`:

* input: a :class:`GeneratorContext` whose ``values`` have been default-filled
  and validated against :attr:`PackageGenerator.params` by the driver;
* output: a context with **either** ``package`` set and fully concrete (no
  holes, no ``auto`` left — the driver guards totality) **or** ``residual``
  set: a :class:`ResidualKind` naming the kind whose remaining parameters the
  package-picker must collect. The residual may name a *different* kind (a
  different parameter set) — the driver re-dispatches such redirects and
  surfaces incomplete ones to the picker;
* parameters the kind cannot realise (an odd pin count for a DIP) raise
  :class:`~protoboard.domain.components.package.loader.PackageDefError`;
* pure function of the context: no I/O, no Qt, no global state.

A generator normally never sees missing *required* parameters — the driver
turns those into a :class:`ResidualKind` itself before calling ``generate``.

Pure domain: no Qt, no I/O.
"""

from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from typing import Any, ClassVar, TypeVar

import structlog

from protoboard.domain.components.footprints import Footprint
from protoboard.domain.components.package.loader import (
    PackageDefError,
    build_package,
)
from protoboard.domain.components.package.schema import ParamSchema
from protoboard.domain.components.package.shapes import Package

_log = structlog.get_logger(__name__)

# Package-domain raster (mm per hole) — scales hole-step offsets to mm so a
# generated prism's pins read back as exact hole coordinates (mm ÷ raster).
RASTER_MM = 2.5
# Body margin around the pin cloud, in holes (legacy SVG bodies' 0.5-hole inset).
MARGIN_HOLES = 0.5


@dataclasses.dataclass(frozen=True, slots=True)
class ResidualKind:
    """A partially-applied kind: values bound so far + the kind still to fill.

    A generator (or the driver, on missing required parameters) hands this back
    instead of a package when the supplied values cannot yet realise a body.
    The package-picker collects the remaining parameters — the fields of
    ``KIND_GENERATORS[kind].params`` that are still holes in ``values`` — and
    re-runs the build. ``kind`` may differ from the kind originally dispatched:
    a generator may *redirect* to another kind with a different parameter set
    (``values`` then carries whatever it already computed/remapped).
    """

    kind: str
    values: Mapping[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True, slots=True)
class GeneratorContext:
    """Input/output of a package-kind generator.

    A generator is ``generate(ctx) -> new_ctx``: it reads ``pin_count`` and
    ``values`` (default-filled by the driver; may include values computed by an
    earlier generator in a chain) and returns a *new* context with ``package``
    — or ``residual`` — set (and possibly more ``values``). Frozen so the
    steps compose cleanly; the chain driver runs steps left→right (see
    :func:`~protoboard.domain.components.package.kind.build_kind_package` and
    docs/footprints/package-model.md §6).

    - ``pin_count``: the owning component's pin count (drives DIP/SIL geometry).
    - ``values``: extra package fields (e.g. a connector's ``rows``); a
      generator ignores keys it does not use. Carries earlier-computed values too.
    - ``schema``: the owning package's declared :class:`ParamSchema` (the YAML
      value-tree schema), when the caller has one — a compute step consults it
      to resolve ``auto`` holes into concrete values. ``None`` outside that path.
    - ``package``: the built body — ``None`` on input, set on output.
    - ``residual``: set instead of ``package`` when parameters are still
      missing (or the generator redirects to another kind) — see
      :class:`ResidualKind`.  An intermediate chain step may leave *both*
      unset — a pure value transform whose output ``values`` feed the next step.
    """

    pin_count: int
    values: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    package: Package | None = None
    residual: ResidualKind | None = None
    schema: ParamSchema | None = None

    def with_package(self, package: Package) -> GeneratorContext:
        """Return a copy carrying *package* (the generator's output body)."""
        return dataclasses.replace(self, package=package, residual=None)

    def with_residual(self, residual: ResidualKind) -> GeneratorContext:
        """Return a copy carrying *residual* (parameters still to collect)."""
        return dataclasses.replace(self, package=None, residual=residual)

    def with_values(self, values: Mapping[str, Any]) -> GeneratorContext:
        """Return a copy carrying *values* — a compute step's augmented tree
        (no package, no residual: the chain driver feeds it to the next step).
        """
        return dataclasses.replace(
            self, values=dict(values), package=None, residual=None,
        )


# kind name → generator class. Populated declaratively — by the
# ``register_generator`` decorator or by ``PackageGenerator.__init_subclass__``
# (``generates``); later registrations win (a user plugin overrides a builtin,
# mirroring the renderer registry).
KIND_GENERATORS: dict[str, type[PackageGenerator]] = {}


def _register(key: str, cls: type[PackageGenerator], axis: str) -> None:
    """Enter *cls* under *key*; later registrations win, collisions are loud."""
    prev = KIND_GENERATORS.get(key)
    if prev is not None and prev is not cls:
        _log.warning(
            "kind generator overridden",
            axis=axis,
            kind=key,
            old=f"{prev.__module__}.{prev.__qualname__}",
            new=f"{cls.__module__}.{cls.__qualname__}",
        )
    KIND_GENERATORS[key] = cls


_GeneratorT = TypeVar("_GeneratorT", bound="type[PackageGenerator]")


def register_generator(
    *,
    kind: str | tuple[str, ...],
) -> Callable[[_GeneratorT], _GeneratorT]:
    """Register a :class:`PackageGenerator` subclass for one or more kinds.

    The preferred registration spelling — the domain-side counterpart of the
    renderer registry's ``@register_renderer`` decorator, so plugins register
    both halves of a kind through one consistent interface::

        @register_generator(kind="dip_ic")
        class DipGenerator(PackageGenerator): ...

    Same registry and semantics as the legacy ``generates = "<kind>"``
    attribute: one name or a tuple, later registrations win (a plugin
    overrides a builtin), a collision on the same kind logs a warning.
    """
    keys = (kind,) if isinstance(kind, str) else tuple(kind)
    if not keys:
        raise ValueError("register_generator: at least one kind is required")

    def _decorate(cls: _GeneratorT) -> _GeneratorT:
        for key in keys:
            _register(key, cls, "kind")
        return cls

    return _decorate


class PackageGenerator(ABC):
    """Turn a package *kind* + parameter set into a concrete :class:`Package`.

    Subclasses declare which kind(s) they generate (``generates``), which
    parameters *{K}* they accept (``params`` — one source of truth for build
    validation, the place-tool picker rows, and documentation), and implement
    :meth:`generate`. The driver (``build_kind_package``) owns everything
    around the call: default fill, required/type checks, totality, and the
    ``kind`` / ``lineage`` stamp on the built body.
    """

    #: Kind name(s) this class generates. Declaring it on a subclass registers
    #: the class in ``KIND_GENERATORS`` at class-creation time. The attribute
    #: is intentionally looked up on ``cls.__dict__`` only — a subclass that
    #: does NOT declare its own ``generates`` inherits its parent's *behaviour*
    #: without stealing its registry slot.
    generates: ClassVar[str | tuple[str, ...] | None] = None

    #: Declaration of the parameter set {K}: names, types, defaults, required.
    #: The driver default-fills and validates ``ctx.values`` against it before
    #: :meth:`generate` runs; the place-tool picker renders its editable holes.
    params: ClassVar[ParamSchema] = ParamSchema({})

    #: Convenience body fill colour (CSS hex). Stamped by the driver onto the
    #: built body unless the generator already set one itself.
    color: ClassVar[str | None] = None

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        declared = cls.__dict__.get("generates")
        if declared is None:
            return
        keys = (declared,) if isinstance(declared, str) else tuple(declared)
        for key in keys:
            _register(key, cls, "generates")

    @abstractmethod
    def generate(self, ctx: GeneratorContext) -> GeneratorContext:
        """Build the concrete body for *ctx* (see the module-level contract)."""

    def offered_family_ids(self, pin_count: int) -> list[str]:
        """Generated family ids the body-variant picker should offer as
        alternatives for a part whose explicit body is this kind.

        The naming convention is the generated-family one (``dip-N`` /
        ``wdip-N`` / ``sil-N``, see
        :mod:`~protoboard.domain.components.package.generated_families`).
        Default: no alternatives — a kind opts in by overriding.
        """
        del pin_count
        return []

    def variant_label(self, kind: str, ctx: GeneratorContext) -> str:
        """Human label for a variant built from *ctx* (picker / catalogue).

        The default is ``KIND-N`` plus any non-default parameter values.
        """
        extras = ",".join(
            f"{k}={v}" for k, v in sorted(ctx.values.items()) if v is not None
        )
        base = f"{kind}-{ctx.pin_count}"
        return f"{base} ({extras})" if extras else base


def prism_from_footprint(fp: Footprint) -> Package | None:
    """Build a plain prism :class:`Package` whose pins follow *fp*.

    Geometry only: the body bbox is derived from the pin cloud plus a uniform
    margin, leads point rigidly down. The shared funnel of the builtin
    generators — richer per-kind silhouettes (DIP notch, etc.) are a renderer
    concern. Returns ``None`` for a footprint with no body (< 2 pins).
    """
    pins = fp.pin_positions()
    if len(pins) < 2:
        return None
    steps = [
        [pins[i + 1].x - pins[i].x, pins[i + 1].y - pins[i].y]
        for i in range(len(pins) - 1)
    ]
    spec = {
        "shape": "prism",
        "dims": {},  # bbox derived from pins + margin
        "bend": 0,
        "margin": MARGIN_HOLES * RASTER_MM,
        "leads": {
            "direction": [0, 0, -1],
            "body_offset": {"unit": RASTER_MM, "value": steps},
            "board_offset": "default",
        },
    }
    try:
        return build_package(spec, "synth/kind")
    except PackageDefError:
        return None


__all__ = [
    "KIND_GENERATORS",
    "GeneratorContext",
    "MARGIN_HOLES",
    "PackageGenerator",
    "RASTER_MM",
    "ResidualKind",
    "prism_from_footprint",
]
