# SPDX-License-Identifier: MIT
"""Tests for ComponentDefinition and ComponentInstance."""

import dataclasses
import uuid

import pytest

from protoboard.domain.components.definition import (
    REFDES_PREFIX,
    ComponentClass,
    ComponentDefinition,
)
from protoboard.domain.components.footprints import DIP, SIL
from protoboard.domain.components.instance import ComponentInstance


class TestComponentClass:
    def test_refdes_prefix_total(self) -> None:
        # refdes_prefix indexes REFDES_PREFIX with no fallback, so every class
        # MUST have a prefix or a placed component of that class crashes.
        for cls in ComponentClass:
            assert cls in REFDES_PREFIX, f"REFDES_PREFIX missing {cls}"

    def test_devboard_prefix(self) -> None:
        assert REFDES_PREFIX[ComponentClass.DEVBOARD] == "U"


class TestComponentDefinition:
    def test_full_pin_names(self) -> None:
        defn = ComponentDefinition(
            name="LM358",
            footprint=DIP(8),
            pin_names={
                1: "OUT_A",
                2: "IN-_A",
                3: "IN+_A",
                4: "VEE",
                5: "IN+_B",
                6: "IN-_B",
                7: "OUT_B",
                8: "VCC",
            },
        )
        assert defn.name == "LM358"
        assert defn.pin_name(1) == "OUT_A"
        assert defn.pin_name(8) == "VCC"

    def test_partial_pin_names_default_pn(self) -> None:
        # Per user decision: missing pin names default to "P{n}".
        defn = ComponentDefinition(
            name="LM358",
            footprint=DIP(8),
            pin_names={1: "OUT_A", 4: "VEE", 8: "VCC"},
        )
        assert defn.pin_name(1) == "OUT_A"
        assert defn.pin_name(2) == "P2"
        assert defn.pin_name(3) == "P3"
        assert defn.pin_name(4) == "VEE"
        assert defn.pin_name(7) == "P7"
        assert defn.pin_name(8) == "VCC"

    def test_no_pin_names_all_defaults(self) -> None:
        defn = ComponentDefinition(name="R", footprint=SIL(2), pin_names={})
        assert defn.pin_name(1) == "P1"
        assert defn.pin_name(2) == "P2"

    def test_pin_name_out_of_range_raises(self) -> None:
        defn = ComponentDefinition(name="R", footprint=SIL(2), pin_names={})
        with pytest.raises(ValueError):
            defn.pin_name(0)
        with pytest.raises(ValueError):
            defn.pin_name(3)

    def test_pin_names_with_invalid_pin_number_raises(self) -> None:
        # Names referencing non-existent pins are a definition error.
        with pytest.raises(ValueError):
            ComponentDefinition(
                name="R",
                footprint=SIL(2),
                pin_names={1: "A", 5: "BOGUS"},
            )

    def test_pin_count_property(self) -> None:
        defn = ComponentDefinition(name="LM358", footprint=DIP(8), pin_names={})
        assert defn.pin_count == 8

    def test_optional_description(self) -> None:
        defn = ComponentDefinition(
            name="LM358",
            footprint=DIP(8),
            pin_names={},
            description="Dual op-amp",
        )
        assert defn.description == "Dual op-amp"

    def test_default_description_is_empty(self) -> None:
        defn = ComponentDefinition(name="R", footprint=SIL(2), pin_names={})
        assert defn.description == ""

    def test_metadata_default_empty(self) -> None:
        defn = ComponentDefinition(name="R", footprint=SIL(2), pin_names={})
        assert defn.metadata == {}

    def test_metadata_carries_user_data(self) -> None:
        defn = ComponentDefinition(
            name="R",
            footprint=SIL(2),
            pin_names={},
            metadata={"datasheet": "https://example.com/r.pdf"},
        )
        assert defn.metadata["datasheet"] == "https://example.com/r.pdf"

    def test_frozen(self) -> None:
        defn = ComponentDefinition(name="R", footprint=SIL(2), pin_names={})
        with pytest.raises(dataclasses.FrozenInstanceError):
            defn.name = "C"  # type: ignore[misc]


class TestComponentInstance:
    def test_construct(self) -> None:
        inst_id = uuid.uuid4()
        inst = ComponentInstance(
            id=inst_id,
            definition_ref="LM358",
            refdes="U1",
            value="LM358",
        )
        assert inst.id == inst_id
        assert inst.definition_ref == "LM358"
        assert inst.refdes == "U1"
        assert inst.value == "LM358"

    def test_two_instances_have_distinct_ids_when_created_with_helper(self) -> None:
        # ComponentInstance.new() is the convenience constructor that
        # generates a fresh UUID.
        a = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        b = ComponentInstance.new(definition_ref="R", refdes="R2", value="10k")
        assert a.id != b.id

    def test_refdes_can_change_independently_of_id(self) -> None:
        # The same instance can be renamed (refdes change) without affecting
        # connectivity references — the ID is the stable key.
        inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        # Construct a "renamed" copy.
        renamed = dataclasses.replace(inst, refdes="R5")
        assert renamed.id == inst.id
        assert renamed.refdes == "R5"

    def test_empty_refdes_raises(self) -> None:
        with pytest.raises(ValueError):
            ComponentInstance.new(definition_ref="R", refdes="", value="10k")

    def test_empty_definition_ref_raises(self) -> None:
        with pytest.raises(ValueError):
            ComponentInstance.new(definition_ref="", refdes="R1", value="10k")

    def test_frozen(self) -> None:
        inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        with pytest.raises(dataclasses.FrozenInstanceError):
            inst.refdes = "R2"  # type: ignore[misc]
