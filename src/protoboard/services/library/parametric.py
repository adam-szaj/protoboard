# SPDX-License-Identifier: MIT
"""Parametric (variable-pin-count) component definitions.

A library entry whose inline ``package: {kind: …}`` block comes with NO
``pins:`` declares a connector-style parametric part (``PinHeader``,
``JST_XH``): the pin count — and any open generator parameters, e.g. a pin
header's ``rows`` — are chosen by the user at placement time.

The chosen parameters :func:`concretize` the entry into an ordinary
:class:`ComponentDefinition` under a deterministic name built from the
generator's ``variant_suffix`` (``PinHeader_1x05``, ``JST_XH_4pin`` — the
historical catalogue names). Persistence therefore keeps working unchanged:
an instance saved with such a ``definition_ref`` re-synthesizes its
definition on lookup (:func:`resolve_parametric_name` /
:class:`ComponentCatalog`) instead of being dropped as dangling.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

import structlog

from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.package.kind import (
    KIND_GENERATORS,
    GeneratorContext,
    build_kind_package,
)
from protoboard.domain.components.package.loader import PackageDefError

_log = structlog.get_logger(__name__)

__all__ = [
    "ComponentCatalog",
    "concretize",
    "parametric_generator_kind",
    "resolve_parametric_name",
]


def parametric_generator_kind(defn: ComponentDefinition) -> str | None:
    """The generator kind a parametric *defn* builds through, or ``None``.

    The preview package carries the kind stamped by the build driver; a
    definition without one (or a kind no generator claims) cannot be
    concretized.
    """
    kind = getattr(defn.package, "kind", None)
    if isinstance(kind, str) and kind in KIND_GENERATORS:
        return kind
    return None


def concretize(
    defn: ComponentDefinition,
    pin_count: int,
    values: Mapping[str, Any] | None = None,
) -> ComponentDefinition:
    """Build the concrete definition of parametric *defn* for *pin_count*.

    *values* are the generator parameters chosen at placement (e.g.
    ``{"rows": 2}``). Raises :class:`PackageDefError` when *defn* is not
    parametric, its kind has no generator, or the generator cannot realise a
    body for the chosen parameters (odd count on a 2×N header, count < 2).
    """
    if not defn.parametric_pins:
        raise PackageDefError(f"{defn.name}: not a parametric definition")
    kind = parametric_generator_kind(defn)
    if kind is None:
        raise PackageDefError(
            f"{defn.name}: parametric definition has no generator kind")
    vals: dict[str, Any] = dict(values or {})
    ctx = build_kind_package(
        kind, GeneratorContext(pin_count=pin_count, values=vals))
    if ctx.package is None:
        raise PackageDefError(
            f"{defn.name}: kind {kind!r} left parameters unfilled for "
            f"pin_count={pin_count}, values={vals!r}")
    suffix = KIND_GENERATORS[kind](kind).variant_suffix(pin_count, vals)
    return dataclasses.replace(
        defn,
        name=f"{defn.name}_{suffix}",
        package=ctx.package,
        parametric_pins=False,
    )


def resolve_parametric_name(
    components: Mapping[str, ComponentDefinition],
    name: str,
) -> ComponentDefinition | None:
    """Synthesize the definition *name* refers to, or ``None``.

    Looks for a parametric base in *components* whose name prefixes *name*
    (``PinHeader_1x05`` → base ``PinHeader``) and whose generator parses the
    remaining suffix. A suffix the generator cannot realise (an odd count on
    a 2×N header) resolves to ``None`` — the caller treats the name as
    missing, same as any dangling reference.
    """
    for base_name, base in components.items():
        if not base.parametric_pins:
            continue
        prefix = base_name + "_"
        if not name.startswith(prefix):
            continue
        kind = parametric_generator_kind(base)
        if kind is None:
            continue
        parsed = KIND_GENERATORS[kind](kind).parse_variant_suffix(
            name[len(prefix):])
        if parsed is None:
            continue
        pin_count, values = parsed
        try:
            synth = concretize(base, pin_count, values)
        except PackageDefError:
            _log.warning(
                "parametric name does not realise", name=name, base=base_name)
            continue
        if synth.name == name:
            return synth
    return None


class ComponentCatalog(dict[str, ComponentDefinition]):
    """Definitions by name, synthesizing parametric variants on miss.

    A drop-in ``dict[str, ComponentDefinition]`` (the session's
    ``components`` mapping): a lookup of a name like ``PinHeader_2x08`` that
    is not present but parses against a parametric base entry synthesizes the
    concrete definition, caches it and returns it — both ``catalog[name]``
    and ``catalog.get(name)``.
    """

    def __missing__(self, name: str) -> ComponentDefinition:
        synth = resolve_parametric_name(self, name)
        if synth is None:
            raise KeyError(name)
        self[synth.name] = synth
        return synth

    # dict.get bypasses __missing__ — route it through __getitem__.
    def get(  # type: ignore[override]
        self, name: str, default: ComponentDefinition | None = None,
    ) -> ComponentDefinition | None:
        try:
            return self[name]
        except KeyError:
            return default
