# SPDX-License-Identifier: MIT
"""Tests for the package parameter schema: parse / type-match / apply."""

import pytest

from protoboard.domain.components.package.schema import (
    AUTO,
    FieldSpec,
    ParamSchema,
    SchemaError,
    parse_schema,
)


class TestParse:
    def test_path_mirrors_value_tree(self) -> None:
        sch = parse_schema({
            "dims": {"width": {"type": "float", "required": True}},
            "leads": {"direction": {"type": ["float", "float", "float"],
                                    "default": [0.0, 0.0, -1.0]}},
        })
        assert set(sch.fields) == {"dims.width", "leads.direction"}
        assert sch.fields["dims.width"].required is True
        assert sch.fields["leads.direction"].has_default is True

    def test_leaf_is_node_with_type(self) -> None:
        sch = parse_schema({"dims": {"r": {"type": "float", "default": 0.0}}})
        assert "dims.r" in sch.fields
        assert sch.fields["dims.r"].default == 0.0

    def test_section_without_type_recurses(self) -> None:
        sch = parse_schema({"dims": {"a": {"type": "int"},
                                     "b": {"type": "int"}}})
        assert set(sch.fields) == {"dims.a", "dims.b"}

    def test_none_is_empty(self) -> None:
        assert parse_schema(None).is_empty()

    def test_non_mapping_raises(self) -> None:
        with pytest.raises(SchemaError):
            parse_schema([1, 2, 3])

    def test_bad_node_raises(self) -> None:
        with pytest.raises(SchemaError):
            parse_schema({"dims": {"x": 5}})  # neither leaf nor section


class TestTypeMatch:
    def test_float_accepts_int_and_float_not_bool(self) -> None:
        fs = FieldSpec(type="float")
        fs.validate(1, "x")
        fs.validate(1.5, "x")
        with pytest.raises(SchemaError):
            fs.validate(True, "x")
        with pytest.raises(SchemaError):
            fs.validate("nope", "x")

    def test_int_rejects_float(self) -> None:
        fs = FieldSpec(type="int")
        fs.validate(3, "x")
        with pytest.raises(SchemaError):
            fs.validate(3.5, "x")

    def test_flat_list_is_one_vector_shape(self) -> None:
        fs = FieldSpec(type=["float", "float", "float"])
        fs.validate([0.0, 0.0, -1.0], "v")
        with pytest.raises(SchemaError):
            fs.validate([0.0, 0.0], "v")  # wrong length
        with pytest.raises(SchemaError):
            fs.validate(0.0, "v")         # scalar, not a vector

    def test_union_scalar_or_vector(self) -> None:
        # radius: a scalar OR a [float,float] OR a [float,[float,float]]
        fs = FieldSpec(
            type=["float", ["float", "float"], ["float", ["float", "float"]]])
        fs.validate(0.0, "r")
        fs.validate([1.0, 2.0], "r")
        fs.validate([0.5, [1.0, 2.0]], "r")
        with pytest.raises(SchemaError):
            fs.validate([1.0, 2.0, 3.0], "r")  # matches none

    def test_auto_always_accepted(self) -> None:
        FieldSpec(type="float").validate(AUTO, "len")  # no raise


class TestApply:
    def _schema(self) -> ParamSchema:
        return parse_schema({
            "dims": {
                "width": {"type": "float", "required": True},
                "radius": {"type": "float", "default": 0.0},
            },
        })

    def test_default_fills_absent(self) -> None:
        out = self._schema().apply_to({"dims": {"width": 5.0}})
        assert out["dims"]["radius"] == 0.0
        assert out["dims"]["width"] == 5.0

    def test_default_does_not_override_present(self) -> None:
        out = self._schema().apply_to({"dims": {"width": 5.0, "radius": 2.0}})
        assert out["dims"]["radius"] == 2.0

    def test_required_missing_raises(self) -> None:
        with pytest.raises(SchemaError, match="dims.width"):
            self._schema().check_required({"dims": {"radius": 1.0}})

    def test_required_satisfied_by_auto(self) -> None:
        self._schema().check_required({"dims": {"width": AUTO}})  # no raise

    def test_validate_present_fields(self) -> None:
        with pytest.raises(SchemaError):
            self._schema().validate({"dims": {"width": "bad"}})

    def test_loose_unknown_field_passes(self) -> None:
        # flat_radius has no schema leaf → untouched, no error.
        out = self._schema().apply_to(
            {"dims": {"width": 5.0, "flat_radius": 1.1}})
        assert out["dims"]["flat_radius"] == 1.1
        self._schema().validate({"dims": {"width": 5.0, "flat_radius": 1.1}})

    def test_apply_does_not_mutate_input(self) -> None:
        src = {"dims": {"width": 5.0}}
        self._schema().apply_to(src)
        assert "radius" not in src["dims"]
