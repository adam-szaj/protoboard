# SPDX-License-Identifier: MIT
"""Canonical placed-component context resolution."""

from __future__ import annotations

import uuid

import pytest

from protoboard.domain.components.definition import (
    ComponentDefinition,
    MetaPin,
    PackageOption,
)
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package.generated_families import sil_family
from protoboard.domain.components.resolution import resolve_component
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.geometry import Point


def _context(*, bound: bool = True, package_variant: str | None = None) -> tuple[
    uuid.UUID,
    ComponentPlacement,
    ComponentInstance,
    ComponentDefinition,
    object,
]:
    family = sil_family(2)
    assert family is not None
    definition = ComponentDefinition(
        name="J2",
        meta_pins=(MetaPin(("1",)), MetaPin(("2",))),
        package_options=(
            PackageOption(
                name="SIL-2",
                ref="sil-2/std",
                pin_map={1: (1,), 2: (2,)},
            ),
        ),
    )
    instance = ComponentInstance.new(
        definition_ref="J2",
        refdes="J1",
        value="",
        package_variant=package_variant,
    )
    if bound:
        instance = instance.bind(definition, {"sil-2": family})
    placement = ComponentPlacement(
        component_id=instance.id,
        position=Point(2.5, 3.5),
        rotation=0,
        side="top",
    )
    return instance.id, placement, instance, definition, family.variants[0].package


def test_resolve_component_returns_complete_context() -> None:
    cid, placement, instance, definition, package = _context()

    resolved = resolve_component(
        cid,
        {cid: placement},
        {cid: instance},
        {definition.name: definition},
    )

    assert resolved is not None
    assert resolved.instance is instance
    assert resolved.definition is definition
    assert resolved.placement is placement
    assert resolved.package_option is definition.package_options[0]
    assert resolved.package is package


@pytest.mark.parametrize("missing", ["placement", "instance", "definition"])
def test_resolve_component_returns_none_for_missing_structural_record(
    missing: str,
) -> None:
    cid, placement, instance, definition, _ = _context()
    placements = {} if missing == "placement" else {cid: placement}
    instances = {} if missing == "instance" else {cid: instance}
    definitions = {} if missing == "definition" else {definition.name: definition}

    assert resolve_component(cid, placements, instances, definitions) is None


def test_resolve_component_keeps_unbound_package_explicit() -> None:
    cid, placement, instance, definition, _ = _context(bound=False)

    resolved = resolve_component(
        cid,
        {cid: placement},
        {cid: instance},
        {definition.name: definition},
    )

    assert resolved is not None
    assert resolved.package_option is definition.package_options[0]
    assert resolved.package is None


def test_resolve_component_does_not_hide_invalid_package_selection() -> None:
    cid, placement, instance, definition, _ = _context(package_variant="sil-2/missing")

    with pytest.raises(ValueError, match="package option"):
        resolve_component(
            cid,
            {cid: placement},
            {cid: instance},
            {definition.name: definition},
        )
