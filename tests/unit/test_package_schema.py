# SPDX-License-Identifier: MIT
"""Tests for the package parameter schema: parse / type-match / apply."""

import pytest

from protoboard.domain.components.package.schema import (
    AUTO,
    FieldSpec,
    FieldStatus,
    FieldView,
    ParamSchema,
    SchemaError,
    parse_schema,
)


class TestParse:
    def test_path_mirrors_value_tree(self) -> None:
        sch = parse_schema(
            {
                "dims": {"width": {"type": "float", "required": True}},
                "leads": {
                    "direction": {
                        "type": ["float", "float", "float"],
                        "default": [0.0, 0.0, -1.0],
                    }
                },
            }
        )
        assert set(sch.fields) == {"dims.width", "leads.direction"}
        assert sch.fields["dims.width"].required is True
        assert sch.fields["leads.direction"].has_default is True

    def test_leaf_is_node_with_type(self) -> None:
        sch = parse_schema({"dims": {"r": {"type": "float", "default": 0.0}}})
        assert "dims.r" in sch.fields
        assert sch.fields["dims.r"].default == 0.0

    def test_section_without_type_recurses(self) -> None:
        sch = parse_schema({"dims": {"a": {"type": "int"}, "b": {"type": "int"}}})
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
            fs.validate(0.0, "v")  # scalar, not a vector

    def test_union_scalar_or_vector(self) -> None:
        # radius: a scalar OR a [float,float] OR a [float,[float,float]]
        fs = FieldSpec(
            type=["float", ["float", "float"], ["float", ["float", "float"]]]
        )
        fs.validate(0.0, "r")
        fs.validate([1.0, 2.0], "r")
        fs.validate([0.5, [1.0, 2.0]], "r")
        with pytest.raises(SchemaError):
            fs.validate([1.0, 2.0, 3.0], "r")  # matches none

    def test_auto_always_accepted(self) -> None:
        FieldSpec(type="float").validate(AUTO, "len")  # no raise


class TestListConstructor:
    """``{list: <shape>}`` — variable-length list of a repeated shape."""

    def test_empty_list_matches(self) -> None:
        FieldSpec(type={"list": ["float", "float", "float"]}).validate([], "v")

    def test_all_elements_match(self) -> None:
        fs = FieldSpec(type={"list": ["float", "float", "float"]})
        fs.validate([[0.0, 0.0, -1.0], [0.0, 0.0, 1.0]], "v")

    def test_one_element_mismatch_fails(self) -> None:
        fs = FieldSpec(type={"list": ["float", "float", "float"]})
        with pytest.raises(SchemaError):
            fs.validate([[0.0, 0.0, -1.0], [0.0, 0.0]], "v")  # 2nd is 2-vec

    def test_scalar_is_not_a_list(self) -> None:
        with pytest.raises(SchemaError):
            FieldSpec(type={"list": "float"}).validate(0.0, "v")

    def test_len_pins_exact_length(self) -> None:
        fs = FieldSpec(type={"list": ["float", "float"], "len": 2})
        fs.validate([[1.0, 2.0], [3.0, 4.0]], "v")
        with pytest.raises(SchemaError):
            fs.validate([[1.0, 2.0]], "v")  # wrong length

    def test_bad_len_raises(self) -> None:
        with pytest.raises(SchemaError, match="len"):
            FieldSpec(type={"list": "int", "len": "two"}).validate([1], "v")


class TestSeqConstructor:
    """``{seq: [...]}`` — fixed-length positional tuple of differing members."""

    def test_runlength_pair_matches(self) -> None:
        fs = FieldSpec(type={"seq": ["int", ["float", "float"]]})
        fs.validate([3, [1.0, 2.0]], "s")

    def test_wrong_length_fails(self) -> None:
        fs = FieldSpec(type={"seq": ["int", ["float", "float"]]})
        with pytest.raises(SchemaError):
            fs.validate([3], "s")
        with pytest.raises(SchemaError):
            fs.validate([3, [1.0, 2.0], 4], "s")

    def test_positional_mismatch_fails(self) -> None:
        fs = FieldSpec(type={"seq": ["int", ["float", "float"]]})
        with pytest.raises(SchemaError):
            fs.validate([[1.0, 2.0], 3], "s")  # swapped positions

    def test_seq_must_be_list(self) -> None:
        with pytest.raises(SchemaError, match="seq"):
            FieldSpec(type={"seq": "int"}).validate([1], "s")


class TestOneofConstructor:
    """``{oneof: [...]}`` — named union; branches may be shapes, dicts, consts."""

    def test_each_branch_matches(self) -> None:
        # bare [x,y] OR run-length {seq: [count, [x,y]]}
        fs = FieldSpec(
            type={"oneof": [["float", "float"], {"seq": ["int", ["float", "float"]]}]}
        )
        fs.validate([1.0, 2.0], "o")  # bare
        fs.validate([3, [1.0, 2.0]], "o")  # run-length

    def test_none_match_fails(self) -> None:
        fs = FieldSpec(
            type={"oneof": [["float", "float"], {"seq": ["int", ["float", "float"]]}]}
        )
        with pytest.raises(SchemaError):
            fs.validate("nope", "o")

    def test_oneof_must_be_list(self) -> None:
        with pytest.raises(SchemaError, match="oneof"):
            FieldSpec(type={"oneof": "float"}).validate(1.0, "o")


class TestConstConstructor:
    """``{const: <value>}`` — literal equality (for sentinels)."""

    def test_const_string_matches_exact(self) -> None:
        fs = FieldSpec(type={"const": "default"})
        fs.validate("default", "b")
        with pytest.raises(SchemaError):
            fs.validate("solve", "b")

    def test_const_inside_oneof(self) -> None:
        fs = FieldSpec(type={"oneof": [{"const": "default"}, {"const": "solve"}]})
        fs.validate("default", "b")
        fs.validate("solve", "b")
        with pytest.raises(SchemaError):
            fs.validate("other", "b")


class TestBadDictConstructor:
    def test_unknown_dict_key_raises(self) -> None:
        with pytest.raises(SchemaError, match="list.*seq.*oneof.*const"):
            FieldSpec(type={"nope": 1}).validate(0, "x")


class TestLeadsRoundTrip:
    """Every real YAML form (proposal §1) validates against its §4 declaration.

    The declarations follow the recommended §3a / §5b shape: element-only list
    matching (loader owns arity) and the ``{unit,value}`` wrapper assumed already
    unwrapped, so ``body_offset`` is a bare offset list by validation time.
    """

    _OFFSET_ELEM = {
        "oneof": [
            ["float", "float"],  # bare [x, y]
            {"seq": ["int", ["float", "float"]]},  # run-length
        ]
    }  # [count,[x,y]]

    def _direction(self) -> FieldSpec:
        return FieldSpec(
            type={
                "oneof": [
                    ["float", "float", "float"],  # form 1: broadcast
                    {"list": ["float", "float", "float"]},  # form 2: per-pin
                    {
                        "list": {
                            "seq": [
                                {"list": "int"},  # form 3: grouped
                                ["float", "float", "float"],
                            ]
                        }
                    },
                ]
            }
        )

    def _body_offset(self) -> FieldSpec:
        return FieldSpec(
            type={"oneof": ["float", {"list": self._OFFSET_ELEM}]}  # scalar sugar
        )

    def _board_offset(self) -> FieldSpec:
        return FieldSpec(
            type={
                "oneof": [
                    {"const": "default"},
                    {"const": "solve"},
                    {"list": self._OFFSET_ELEM},
                ]
            }
        )

    def test_direction_form1_broadcast(self) -> None:
        self._direction().validate([0.0, 0.0, -1.0], "leads.direction")

    def test_direction_form2_per_pin(self) -> None:
        self._direction().validate([[0, 0, -1], [0, 0, 1]], "leads.direction")

    def test_direction_form3_grouped(self) -> None:
        self._direction().validate(
            [[[1, 2, 3, 4], [0, 0, -1]], [[5, 6, 7, 8], [0, 0, 1]]], "leads.direction"
        )

    def test_direction_garbage_rejected(self) -> None:
        with pytest.raises(SchemaError):
            self._direction().validate("down", "leads.direction")

    def test_body_offset_scalar(self) -> None:
        self._body_offset().validate(0, "leads.body_offset")

    def test_body_offset_runlength_and_bare_mixed(self) -> None:
        self._body_offset().validate(
            [[14, [0, 1]], [7, 0], [14, [0, -1]]], "leads.body_offset"
        )

    def test_body_offset_single_runlength(self) -> None:
        self._body_offset().validate([[1, 0]], "leads.body_offset")

    def test_board_offset_default_sentinel(self) -> None:
        self._board_offset().validate("default", "leads.board_offset")

    def test_board_offset_solve_sentinel(self) -> None:
        self._board_offset().validate("solve", "leads.board_offset")

    def test_board_offset_explicit_list(self) -> None:
        self._board_offset().validate([[2, [1, 0]]], "leads.board_offset")

    def test_board_offset_unknown_sentinel_rejected(self) -> None:
        with pytest.raises(SchemaError):
            self._board_offset().validate("magic", "leads.board_offset")


class TestApply:
    def _schema(self) -> ParamSchema:
        return parse_schema(
            {
                "dims": {
                    "width": {"type": "float", "required": True},
                    "radius": {"type": "float", "default": 0.0},
                },
            }
        )

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
        out = self._schema().apply_to({"dims": {"width": 5.0, "flat_radius": 1.1}})
        assert out["dims"]["flat_radius"] == 1.1
        self._schema().validate({"dims": {"width": 5.0, "flat_radius": 1.1}})

    def test_apply_does_not_mutate_input(self) -> None:
        src = {"dims": {"width": 5.0}}
        self._schema().apply_to(src)
        assert "radius" not in src["dims"]


class TestFieldView:
    """Editability derives from the RESOLVED value: null ⇒ RW (a hole for the
    user); a set value / default / auto ⇒ read-only. See schema-design.md §2.5."""

    def _schema(self) -> ParamSchema:
        return parse_schema(
            {
                "dims": {
                    "width": {"type": "float", "required": True},  # hole → RW
                    "length": {"type": "float", "required": True},  # filled → RO
                    "radius": {"type": "float", "default": 0.0},  # default → RO
                    "pins": {"type": "int"},  # auto → RO
                },
            }
        )

    def _views(self, values: dict) -> dict[str, FieldView]:
        return {v.path: v for v in self._schema().fields_view(values)}

    def test_null_is_editable_required_empty(self) -> None:
        v = self._views(
            {"dims": {"width": None, "length": 5.0, "radius": 0.0, "pins": AUTO}}
        )["dims.width"]
        assert v.editable is True
        assert v.status is FieldStatus.REQUIRED_EMPTY

    def test_set_value_is_readonly(self) -> None:
        v = self._views(
            {"dims": {"width": None, "length": 5.0, "radius": 0.0, "pins": AUTO}}
        )["dims.length"]
        assert v.editable is False
        assert v.status is FieldStatus.SET

    def test_default_value_is_readonly(self) -> None:
        # radius omitted → apply_to fills default 0.0 → RO (a set value).
        sch = self._schema()
        filled = sch.apply_to({"dims": {"width": None, "length": 5.0, "pins": AUTO}})
        v = {x.path: x for x in sch.fields_view(filled)}["dims.radius"]
        assert v.editable is False
        assert v.status is FieldStatus.SET

    def test_auto_is_readonly_derived(self) -> None:
        v = self._views(
            {"dims": {"width": None, "length": 5.0, "radius": 0.0, "pins": AUTO}}
        )["dims.pins"]
        assert v.editable is False
        assert v.status is FieldStatus.AUTO

    def test_view_carries_spec_and_value(self) -> None:
        v = self._views(
            {"dims": {"width": None, "length": 5.0, "radius": 0.0, "pins": AUTO}}
        )["dims.length"]
        assert v.spec.type == "float"
        assert v.value == 5.0
        assert v.path == "dims.length"

    def test_missing_value_treated_as_null(self) -> None:
        # A required field with no value slot at all is still a hole → RW.
        views = self._views({"dims": {"length": 5.0, "radius": 0.0, "pins": AUTO}})
        assert views["dims.width"].editable is True
        assert views["dims.width"].status is FieldStatus.REQUIRED_EMPTY


class TestMappingConstructor:
    def test_typed_keys_and_scalar_or_list_values(self) -> None:
        spec = FieldSpec(
            type={
                "map": [
                    "positive_int",
                    {"oneof": ["positive_int", {"list": "positive_int"}]},
                ]
            }
        )
        spec.validate({1: 2, 2: [1, 4], 3: []}, "pins.map")
        spec.validate({}, "pins.map")

    @pytest.mark.parametrize(
        "value", [{0: 1}, {True: 1}, {"1": 1}, {1: 0}, {1: [False]}, []]
    )
    def test_rejects_invalid_mapping(self, value: object) -> None:
        with pytest.raises(SchemaError):
            FieldSpec(
                type={
                    "map": [
                        "positive_int",
                        {"oneof": ["positive_int", {"list": "positive_int"}]},
                    ]
                }
            ).validate(value, "pins.map")

    @pytest.mark.parametrize("value", [0, -1, True, 1.5, "3"])
    def test_positive_integer_rejects_non_positive_or_non_integer(
        self, value: object
    ) -> None:
        with pytest.raises(SchemaError):
            FieldSpec(type="positive_int").validate(value, "pins.count")
