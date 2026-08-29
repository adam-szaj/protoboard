# SPDX-License-Identifier: MIT
"""Logical MetaPin to physical Package pin mapping contracts."""

import pytest

from protoboard.domain.components.definition import (
    ComponentDefinition,
    MetaPin,
    PackageOption,
)
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package.pin_mapping import (
    meta_pin_for_package_pin,
    package_pins_for_meta_pin,
    resolve_package_option,
)


def _definition() -> ComponentDefinition:
    return ComponentDefinition(
        name="Q",
        meta_pins=(MetaPin(("B",)), MetaPin(("C",)), MetaPin(("E",))),
        package_options=(
            PackageOption(
                name="TO92-A",
                ref="to92/a",
                pin_map={1: (2,), 2: (1,), 3: (3,)},
            ),
            PackageOption(
                name="TO92-B",
                ref="to92/b",
                pin_map={1: (1, 3), 2: (2,)},
            ),
        ),
    )


def test_default_option_maps_metapin_to_physical_pin() -> None:
    definition = _definition()

    assert resolve_package_option(definition, None).ref == "to92/a"
    assert package_pins_for_meta_pin(definition, None, 1) == (2,)
    assert meta_pin_for_package_pin(definition, None, 2) == 1


def test_instance_variant_selects_its_own_one_to_many_mapping() -> None:
    definition = _definition()
    instance = ComponentInstance.new(
        definition_ref="Q",
        refdes="Q1",
        value="BC547",
        package_variant="to92/b",
    )

    assert package_pins_for_meta_pin(definition, instance, 1) == (1, 3)
    assert meta_pin_for_package_pin(definition, instance, 3) == 1
    assert package_pins_for_meta_pin(definition, instance, 3) == ()


def test_unmapped_physical_pin_has_no_metapin() -> None:
    definition = _definition()
    instance = ComponentInstance.new(
        definition_ref="Q",
        refdes="Q1",
        value="BC547",
        package_variant="to92/b",
    )

    assert meta_pin_for_package_pin(definition, instance, 4) is None


def test_unknown_instance_variant_does_not_use_default_mapping() -> None:
    definition = _definition()
    instance = ComponentInstance.new(
        definition_ref="Q",
        refdes="Q1",
        value="BC547",
        package_variant="to92/unknown",
    )

    with pytest.raises(ValueError, match="package option"):
        resolve_package_option(definition, instance)
