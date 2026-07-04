# SPDX-License-Identifier: MIT
"""Test helpers for building *bound* ComponentInstances.

A placed ``ComponentInstance`` must be bound to its definition (+ package
catalogue) before any geometry/conflict/footprint code reads it — otherwise
``inst.resolved_footprint()`` (and the helpers that delegate to it) raise
``protoboard.errors.UnboundInstanceError``. Production binds at every project
entry point; tests that construct instances directly must do the same. These
helpers centralise the bind so individual tests don't repeat it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from protoboard.domain.components.instance import ComponentInstance

if TYPE_CHECKING:
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.package.family import PackageFamily


def bound_instance(
    defn: "ComponentDefinition",
    *,
    refdes: str,
    value: str = "",
    families: "dict[str, PackageFamily] | None" = None,
    **kwargs: object,
) -> ComponentInstance:
    """A fresh ComponentInstance bound to *defn* (and *families*).

    ``definition_ref`` is taken from ``defn.name``. When the part has no
    package, ``families`` may be ``{}`` / ``None`` — a bound instance with no
    package legitimately resolves to ``defn.default_footprint()`` (that is its real
    default geometry, not a silent fallback). Pass the real catalogue when the
    test exercises a chosen variant.
    """
    inst = ComponentInstance.new(
        definition_ref=defn.name, refdes=refdes, value=value,
        **kwargs,  # type: ignore[arg-type]
    )
    return inst.bind(defn, families or {})


def bind(
    inst: ComponentInstance,
    defn: "ComponentDefinition",
    families: "dict[str, PackageFamily] | None" = None,
) -> ComponentInstance:
    """Bind an already-constructed *inst* to *defn* (+ *families*).

    Convenience for tests that build the instance via ``ComponentInstance.new``
    with custom fields and just need to attach the binding afterwards.
    """
    return inst.bind(defn, families or {})
