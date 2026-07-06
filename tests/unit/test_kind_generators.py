# SPDX-License-Identifier: MIT
"""Kind-generator registry: contract, dispatch, residuals, plugins, picker."""

from __future__ import annotations

from pathlib import Path

import pytest

from protoboard.domain.components.package.generators import (
    KIND_GENERATORS,
    GeneratorContext,
    PackageGenerator,
    ResidualKind,
    prism_from_footprint,
    register_generator,
)
from protoboard.domain.components.package.kind import (
    build_kind_package,
    is_kind,
)
from protoboard.domain.components.package.loader import PackageDefError
from protoboard.domain.components.package.schema import parse_schema
from protoboard.domain.components.package.shapes import Prism
from protoboard.services.library.plugin_discovery import (
    load_generator_plugins,
)


@pytest.fixture()
def scratch_registry():
    """Snapshot KIND_GENERATORS and restore it after the test."""
    before = dict(KIND_GENERATORS)
    yield KIND_GENERATORS
    KIND_GENERATORS.clear()
    KIND_GENERATORS.update(before)


@pytest.fixture()
def builtin_kind_nodes():
    """Bind the builtin declarative kind nodes (assets/packages/kinds.yaml).

    Binding otherwise rides on the first library load, so a dispatch test
    that asserts node-path behaviour must request it explicitly — without
    this the result would depend on test order (whether any earlier test
    loaded a library).
    """
    from protoboard.services.library.loader import _load_builtin_package_defs
    _load_builtin_package_defs()


class TestRegistration:

    def test_builtins_registered(self) -> None:
        for kind in ("dip_ic", "wdip_ic", "dip_switch", "sil",
                     "pin_header", "jst"):
            assert is_kind(kind), kind

    def test_declaring_generates_registers(self, scratch_registry) -> None:
        class G(PackageGenerator):
            generates = "_t_reg"

            def generate(self, ctx: GeneratorContext) -> GeneratorContext:
                raise NotImplementedError

        assert scratch_registry["_t_reg"] is G

    def test_register_generator_decorator(self, scratch_registry) -> None:
        # The decorator spelling — the counterpart of @register_renderer.
        @register_generator(kind="_t_deco")
        class G(PackageGenerator):
            def generate(self, ctx: GeneratorContext) -> GeneratorContext:
                raise NotImplementedError

        assert scratch_registry["_t_deco"] is G

    def test_register_generator_tuple_of_kinds(self, scratch_registry) -> None:
        @register_generator(kind=("_t_deco_a", "_t_deco_b"))
        class G(PackageGenerator):
            def generate(self, ctx: GeneratorContext) -> GeneratorContext:
                raise NotImplementedError

        assert scratch_registry["_t_deco_a"] is G
        assert scratch_registry["_t_deco_b"] is G

    def test_register_generator_requires_a_kind(self) -> None:
        with pytest.raises(ValueError):
            register_generator(kind=())

    def test_subclass_without_generates_keeps_parent_slot(
        self, scratch_registry
    ) -> None:
        class Parent(PackageGenerator):
            generates = "_t_parent"

            def generate(self, ctx: GeneratorContext) -> GeneratorContext:
                raise NotImplementedError

        class Child(Parent):  # no ``generates`` of its own
            pass

        assert scratch_registry["_t_parent"] is Parent
        assert Child  # silence unused-name

    def test_later_registration_wins(self, scratch_registry) -> None:
        class First(PackageGenerator):
            generates = "_t_dup"

            def generate(self, ctx: GeneratorContext) -> GeneratorContext:
                raise NotImplementedError

        class Second(PackageGenerator):
            generates = "_t_dup"

            def generate(self, ctx: GeneratorContext) -> GeneratorContext:
                raise NotImplementedError

        assert First is not Second
        assert scratch_registry["_t_dup"] is Second


class TestBuiltinDispatch:
    """Parity: the registry builds the same bodies the old table did."""

    def test_dip_narrow_vs_wide(self, builtin_kind_nodes) -> None:
        narrow = build_kind_package(
            "dip_ic", GeneratorContext(pin_count=16)).package
        wide = build_kind_package(
            "wdip_ic", GeneratorContext(pin_count=16)).package
        assert isinstance(narrow, Prism) and isinstance(wide, Prism)
        assert narrow.kind == "dip_ic" and wide.kind == "wdip_ic"
        # Leaf checks, not tuple equality: the node path carries the full
        # extends ancestry, the legacy path only (shape, kind) — both must
        # end in the kind and start at a prism root.
        assert narrow.lineage[-1] == "dip_ic"
        assert "prism" in narrow.lineage
        # Wide rows are further apart → a wider bbox across the rows.
        assert wide.bbox().width > narrow.bbox().width

    def test_dip_rejects_odd_pin_count(self) -> None:
        with pytest.raises(PackageDefError):
            build_kind_package("dip_ic", GeneratorContext(pin_count=7))

    def test_sil(self) -> None:
        pkg = build_kind_package("sil", GeneratorContext(pin_count=5)).package
        assert isinstance(pkg, Prism)
        assert pkg.kind == "sil"

    def test_pin_header_rows_param_and_color(self) -> None:
        single = build_kind_package(
            "pin_header", GeneratorContext(pin_count=6)).package
        dual = build_kind_package(
            "pin_header",
            GeneratorContext(pin_count=6, values={"rows": 2})).package
        assert isinstance(single, Prism) and isinstance(dual, Prism)
        assert single.color == "#222222"
        # 2×N rows sit side by side → shorter along the row than 1×N.
        assert dual.bbox().height < single.bbox().height
        # The declared params schema rides on the body for the picker.
        assert dual.schema is not None and "rows" in dual.schema.fields

    def test_jst_single_row_color(self) -> None:
        pkg = build_kind_package("jst", GeneratorContext(pin_count=3)).package
        assert pkg is not None and pkg.color == "#dddddd"

    def test_unknown_kind_raises(self) -> None:
        with pytest.raises(PackageDefError):
            build_kind_package("_no_such", GeneratorContext(pin_count=2))


class TestResiduals:

    def test_missing_required_returns_residual(self, scratch_registry) -> None:
        class Needs(PackageGenerator):
            generates = "_t_needs"
            params = parse_schema(
                {"width": {"type": "float", "required": True}})

            def generate(self, ctx: GeneratorContext) -> GeneratorContext:
                raise AssertionError("must not run with required missing")

        out = build_kind_package("_t_needs", GeneratorContext(pin_count=4))
        assert out.package is None
        assert out.residual == ResidualKind(kind="_t_needs", values={})

    def test_explicit_null_hole_uses_default_and_keeps_hole(
        self, scratch_registry
    ) -> None:
        seen: dict[str, object] = {}

        class Holey(PackageGenerator):
            generates = "_t_hole"
            params = parse_schema({"rows": {"type": "int", "default": 1}})

            def generate(self, ctx: GeneratorContext) -> GeneratorContext:
                seen.update(ctx.values)
                from protoboard.domain.components.footprints import SIL
                pkg = prism_from_footprint(SIL(n=ctx.pin_count))
                assert pkg is not None
                return ctx.with_package(pkg)

        out = build_kind_package(
            "_t_hole", GeneratorContext(pin_count=3, values={"rows": None}))
        assert seen["rows"] == 1                       # default fed the build
        assert out.package is not None
        assert out.package.raw_values == {"rows": None}  # hole preserved

    def test_redirect_to_other_kind_builds(self, scratch_registry) -> None:
        class Redirect(PackageGenerator):
            generates = "_t_redir"

            def generate(self, ctx: GeneratorContext) -> GeneratorContext:
                return ctx.with_residual(ResidualKind("sil", {}))

        out = build_kind_package("_t_redir", GeneratorContext(pin_count=4))
        assert out.package is not None
        assert out.package.kind == "sil"          # stamped as the final kind
        assert out.package.lineage == ("prism", "sil")

    def test_redirect_to_incomplete_kind_surfaces_residual(
        self, scratch_registry
    ) -> None:
        class Target(PackageGenerator):
            generates = "_t_target"
            params = parse_schema(
                {"depth": {"type": "float", "required": True}})

            def generate(self, ctx: GeneratorContext) -> GeneratorContext:
                raise AssertionError("incomplete target must not run")

        class Redirect(PackageGenerator):
            generates = "_t_redir2"

            def generate(self, ctx: GeneratorContext) -> GeneratorContext:
                return ctx.with_residual(ResidualKind("_t_target", {}))

        out = build_kind_package("_t_redir2", GeneratorContext(pin_count=4))
        assert out.package is None
        assert out.residual is not None and out.residual.kind == "_t_target"

    def test_redirect_cycle_raises(self, scratch_registry) -> None:
        class Loop(PackageGenerator):
            generates = "_t_loop"

            def generate(self, ctx: GeneratorContext) -> GeneratorContext:
                return ctx.with_residual(ResidualKind("_t_loop", {}))

        with pytest.raises(PackageDefError, match="cycle"):
            build_kind_package("_t_loop", GeneratorContext(pin_count=2))


class TestPluginDiscovery:

    def test_plugin_file_registers_kind(
        self, tmp_path: Path, scratch_registry
    ) -> None:
        plug_dir = tmp_path / "generators"
        plug_dir.mkdir()
        (plug_dir / "mykind.py").write_text(
            "from protoboard.domain.components.footprints import SIL\n"
            "from protoboard.domain.components.package.generators import (\n"
            "    PackageGenerator, prism_from_footprint,\n"
            ")\n"
            "class MyKind(PackageGenerator):\n"
            "    generates = '_t_plugin_kind'\n"
            "    def generate(self, ctx):\n"
            "        return ctx.with_package(\n"
            "            prism_from_footprint(SIL(n=ctx.pin_count)))\n"
        )
        loaded = load_generator_plugins([tmp_path])
        assert loaded and loaded[0].name == "mykind.py"
        assert is_kind("_t_plugin_kind")
        pkg = build_kind_package(
            "_t_plugin_kind", GeneratorContext(pin_count=3)).package
        assert pkg is not None and pkg.kind == "_t_plugin_kind"

    def test_broken_plugin_is_skipped(self, tmp_path: Path) -> None:
        plug_dir = tmp_path / "generators"
        plug_dir.mkdir()
        (plug_dir / "broken.py").write_text("raise RuntimeError('boom')\n")
        assert load_generator_plugins([tmp_path]) == []


class TestLoaderIntegration:
    """Component YAML → kind package / pending-kind placeholder."""

    def _load_one(self, tmp_path: Path, body: str):
        from protoboard.services.library.loader import load_library
        (tmp_path / "lib.yaml").write_text(body)
        return load_library([tmp_path])

    def test_fixed_params_build_concrete_package(self, tmp_path: Path) -> None:
        lib = self._load_one(tmp_path, (
            "components:\n"
            "  - name: HDR_2x3\n"
            "    component_class: connector\n"
            "    pins: {count: 6}\n"
            "    package: {kind: pin_header, rows: 2}\n"
        ))
        defn = lib.get("HDR_2x3")
        assert defn is not None and defn.package is not None
        assert defn.package.kind == "pin_header"
        from protoboard.domain.components.package.kind import (
            is_generic_package,
        )
        assert not is_generic_package(defn.package)

    def test_partial_params_yield_pending_placeholder(
        self, tmp_path: Path, scratch_registry
    ) -> None:
        class NeedsRows(PackageGenerator):
            generates = "_t_pending"
            params = parse_schema(
                {"rows": {"type": "int", "required": True}})

            def generate(self, ctx: GeneratorContext) -> GeneratorContext:
                raise AssertionError("required missing — must not run")

        lib = self._load_one(tmp_path, (
            "components:\n"
            "  - name: PART_X\n"
            "    component_class: connector\n"
            "    pins: {count: 4}\n"
            "    package: {kind: _t_pending}\n"
        ))
        defn = lib.get("PART_X")
        assert defn is not None and defn.package is not None
        # The placeholder keeps generic semantics (DRC / bend consumers)...
        from protoboard.domain.components.package.kind import (
            GENERIC_PACKAGE_KIND,
            is_generic_package,
        )
        assert is_generic_package(defn.package)
        assert GENERIC_PACKAGE_KIND in defn.package.lineage
        # ...but carries the pending kind + its parameter state for the picker.
        assert defn.package.kind == "_t_pending"
        assert defn.package.schema is not None
        assert "rows" in defn.package.schema.fields


class TestChainDriver:
    """Multi-step chains: compute steps transform values, the final step
    builds; ``schema`` rides on the context for ``auto`` resolution."""

    def _register_chain(self, record: dict) -> None:
        """``_t_dims`` computes dims.length from the pin count (resolving an
        ``auto`` hole); ``_t_body`` requires that value and builds a prism."""
        from protoboard.domain.components.package.schema import AUTO

        class Dims(PackageGenerator):
            generates = "_t_dims"

            def generate(self, ctx: GeneratorContext) -> GeneratorContext:
                record["dims_schema"] = ctx.schema
                values = dict(ctx.values)
                dims = dict(values.get("dims") or {})
                if dims.get("length") in (None, AUTO):
                    dims["length"] = ctx.pin_count * 2.54
                values["dims"] = dims
                return ctx.with_values(values)

        class Body(PackageGenerator):
            generates = "_t_body"

            def generate(self, ctx: GeneratorContext) -> GeneratorContext:
                from protoboard.domain.components.footprints import SIL
                dims = ctx.values.get("dims") or {}
                record["seen_length"] = dims.get("length")
                assert isinstance(dims.get("length"), float)
                pkg = prism_from_footprint(SIL(n=ctx.pin_count))
                assert pkg is not None
                return ctx.with_package(pkg)

    def test_compute_step_feeds_final_step(self, scratch_registry) -> None:
        from protoboard.domain.components.package.schema import AUTO
        record: dict = {}
        self._register_chain(record)
        out = build_kind_package(
            ("_t_dims", "_t_body"),
            GeneratorContext(pin_count=4,
                             values={"dims": {"length": AUTO}}),
        )
        assert out.package is not None
        # The compute step resolved the auto hole; the final step saw it.
        assert record["seen_length"] == pytest.approx(4 * 2.54)
        # The built body is stamped with the FINAL kind.
        assert out.package.kind == "_t_body"
        assert out.package.lineage[-1] == "_t_body"

    def test_schema_rides_on_the_context(self, scratch_registry) -> None:
        record: dict = {}
        self._register_chain(record)
        schema = parse_schema({"dims": {"length": {"type": "float"}}})
        build_kind_package(
            ("_t_dims", "_t_body"), GeneratorContext(pin_count=4),
            schema=schema,
        )
        assert record["dims_schema"] is schema

    def test_single_kind_string_unchanged(self, scratch_registry) -> None:
        # Back-compat: a plain string is a one-step chain.
        out = build_kind_package("sil", GeneratorContext(pin_count=3))
        assert out.package is not None and out.package.kind == "sil"

    def test_compute_step_must_not_build_a_package(
        self, scratch_registry
    ) -> None:
        class Builds(PackageGenerator):
            generates = "_t_builds"

            def generate(self, ctx: GeneratorContext) -> GeneratorContext:
                from protoboard.domain.components.footprints import SIL
                pkg = prism_from_footprint(SIL(n=ctx.pin_count))
                assert pkg is not None
                return ctx.with_package(pkg)

        with pytest.raises(PackageDefError, match="must not build"):
            build_kind_package(
                ("_t_builds", "sil"), GeneratorContext(pin_count=3))

    def test_compute_step_missing_required_is_an_error_not_a_residual(
        self, scratch_registry
    ) -> None:
        class NeedsX(PackageGenerator):
            generates = "_t_needsx"
            params = parse_schema({"x": {"type": "int", "required": True}})

            def generate(self, ctx: GeneratorContext) -> GeneratorContext:
                raise AssertionError("required missing — must not run")

        with pytest.raises(PackageDefError, match="compute step"):
            build_kind_package(
                ("_t_needsx", "sil"), GeneratorContext(pin_count=3))

    def test_final_step_residual_carries_computed_values(
        self, scratch_registry
    ) -> None:
        # The chain's computed values land in the surfaced residual, so the
        # picker can re-run just the final kind and reproduce the result.
        record: dict = {}
        self._register_chain(record)

        class FinalNeeds(PackageGenerator):
            generates = "_t_finalneeds"
            params = parse_schema({"rows": {"type": "int", "required": True}})

            def generate(self, ctx: GeneratorContext) -> GeneratorContext:
                raise AssertionError("required missing — must not run")

        out = build_kind_package(
            ("_t_dims", "_t_finalneeds"), GeneratorContext(pin_count=4))
        assert out.package is None and out.residual is not None
        assert out.residual.kind == "_t_finalneeds"
        dims = out.residual.values.get("dims") or {}
        assert dims.get("length") == pytest.approx(4 * 2.54)

    def test_empty_chain_raises(self) -> None:
        with pytest.raises(PackageDefError, match="empty"):
            build_kind_package((), GeneratorContext(pin_count=2))

    def test_yaml_kind_list_builds_through_the_chain(
        self, tmp_path: Path, scratch_registry
    ) -> None:
        record: dict = {}
        self._register_chain(record)
        from protoboard.services.library.loader import load_library
        (tmp_path / "lib.yaml").write_text(
            "components:\n"
            "  - name: PART_CHAIN\n"
            "    component_class: connector\n"
            "    pins: {count: 6}\n"
            "    package: {kind: [_t_dims, _t_body]}\n"
        )
        defn = load_library([tmp_path], package_families={}).get("PART_CHAIN")
        assert defn.package is not None
        assert defn.package.kind == "_t_body"
        assert record["seen_length"] == pytest.approx(6 * 2.54)


class TestPartialKindFootprintOption:
    """A ``footprints:`` entry with a partially-parameterised inline kind
    becomes a per-option pending-kind placeholder (it used to raise at load);
    the picker's second pop-up collects the rest and builds the concrete body.
    """

    def _register_needs_rows(self) -> None:
        class NeedsRows(PackageGenerator):
            generates = "_t_optpending"
            params = parse_schema(
                {"rows": {"type": "int", "required": True}})

            def generate(self, ctx: GeneratorContext) -> GeneratorContext:
                from protoboard.domain.components.footprints import SIL
                pkg = prism_from_footprint(SIL(n=ctx.pin_count))
                assert pkg is not None
                return ctx.with_package(pkg)

    def _load(self, tmp_path: Path):  # type: ignore[no-untyped-def]
        from protoboard.services.library.loader import load_library
        (tmp_path / "lib.yaml").write_text(
            "components:\n"
            "  - name: PART_OPT\n"
            "    component_class: connector\n"
            "    pins: {count: 4}\n"
            "    footprints:\n"
            "      - {name: matrix, package: {kind: _t_optpending}}\n"
        )
        return load_library([tmp_path], package_families={})

    def test_option_loads_as_pending_placeholder(
        self, tmp_path: Path, scratch_registry
    ) -> None:
        from protoboard.domain.components.package.kind import (
            GENERIC_PACKAGE_KIND,
            is_generic_package,
        )
        self._register_needs_rows()
        defn = self._load(tmp_path).get("PART_OPT")
        assert len(defn.footprints) == 1
        opt = defn.footprints[0]
        assert opt.package is not None
        # Generic semantics preserved (geometry is the synthetic bbox)...
        assert is_generic_package(opt.package)
        assert GENERIC_PACKAGE_KIND in opt.package.lineage
        # ...with the target kind + its holes riding along for the picker.
        assert opt.package.kind == "_t_optpending"
        assert opt.package.schema is not None
        assert "rows" in opt.package.schema.fields

    def test_picker_offers_kind_and_builds_concrete_variant(
        self, tmp_path: Path, scratch_registry
    ) -> None:
        from protoboard.domain.components.package.family import (
            resolve_variant,
        )
        from protoboard.domain.components.package.kind import (
            is_generic_package,
        )
        from protoboard.gui.footprint_choice import (
            DefaultFootprintChoiceProvider,
        )
        from protoboard.services.library.loader import (
            inline_option_address,
            inline_option_families,
        )

        self._register_needs_rows()
        lib = self._load(tmp_path)
        defn = lib.get("PART_OPT")
        families = dict(inline_option_families(lib))
        provider = DefaultFootprintChoiceProvider(families)

        opts = provider.options(defn)
        addr = inline_option_address("PART_OPT", "matrix")
        assert [(o.address, o.is_kind) for o in opts] == [(addr, True)]
        # The second pop-up asks for the missing required parameter...
        assert [p.name for p in provider.kind_params(addr)] == ["rows"]
        # ...and building with it yields a concrete, resolvable variant.
        built = provider.build_variant(addr, {"rows": "2"})
        assert built is not None and built != addr
        variant = resolve_variant(families, built)
        assert variant is not None
        assert not is_generic_package(variant.package)


class TestDeclarativeNodes:
    """kinds.yaml nodes: DIP kinds build through the ordinary build_package
    flow (full lineage, schema, raw_values, declared color) with geometry
    identical to the legacy pure-code path."""

    @pytest.mark.parametrize("kind,pins", [
        ("dip_ic", 16), ("wdip_ic", 16), ("dip_switch", 8),
    ])
    def test_node_body_equivalent_to_legacy(
        self, builtin_kind_nodes, kind: str, pins: int
    ) -> None:
        from protoboard.domain.components.package.generators import (
            KIND_GENERATORS,
        )
        from protoboard.domain.components.package.shapes import (
            is_rigid_package,
        )

        node_pkg = build_kind_package(
            kind, GeneratorContext(pin_count=pins)).package
        # The generator's own generate() is the node-less fallback — the
        # legacy body, built without consulting the node registry.
        legacy_pkg = KIND_GENERATORS[kind](kind).generate(
            GeneratorContext(pin_count=pins)).package
        assert isinstance(node_pkg, Prism) and isinstance(legacy_pkg, Prism)
        assert node_pkg.pin_positions() == legacy_pkg.pin_positions()
        assert node_pkg.width_mm == legacy_pkg.width_mm
        assert node_pkg.length_mm == legacy_pkg.length_mm
        assert node_pkg.leads == legacy_pkg.leads
        assert is_rigid_package(node_pkg) == is_rigid_package(legacy_pkg)

    @pytest.mark.parametrize("kind,color", [
        ("dip_ic", "#2b2b30"), ("wdip_ic", "#2b2b30"),
        ("dip_switch", "#c8433c"),
    ])
    def test_node_stamps_kind_color_lineage(
        self, builtin_kind_nodes, kind: str, color: str
    ) -> None:
        pkg = build_kind_package(kind, GeneratorContext(pin_count=8)).package
        assert isinstance(pkg, Prism)
        assert pkg.kind == kind
        assert pkg.color == color
        assert pkg.lineage == ("package", "prism", "prism_dil", kind)

    def test_raw_values_keep_auto_for_the_ui(self, builtin_kind_nodes) -> None:
        pkg = build_kind_package(
            "dip_ic", GeneratorContext(pin_count=8)).package
        assert isinstance(pkg, Prism)
        assert pkg.raw_values is not None
        assert pkg.raw_values["dims"]["width"] == "auto"
        assert pkg.raw_values["dims"]["length"] == "auto"
        assert pkg.schema is not None
        assert "dims.width" in pkg.schema.fields  # inherited prism schema
        # ...and the geometry itself is concrete.
        assert isinstance(pkg.width_mm, float)

    def test_component_override_beats_auto_and_merges(
        self, builtin_kind_nodes
    ) -> None:
        pkg = build_kind_package(
            "dip_ic",
            GeneratorContext(
                pin_count=16,
                values={"dims": {"width": 7.0, "height": 4.0}}),
        ).package
        assert isinstance(pkg, Prism)
        assert pkg.width_mm == 7.0          # explicit beats auto
        assert pkg.height_mm == 4.0         # sibling key deep-merges
        assert pkg.length_mm is not None and pkg.length_mm > 0  # auto filled

    def test_odd_pin_count_raises_through_the_node_path(
        self, builtin_kind_nodes
    ) -> None:
        with pytest.raises(PackageDefError, match="even count"):
            build_kind_package("dip_ic", GeneratorContext(pin_count=7))

    def test_component_yaml_end_to_end_and_one_confirm_picker(
        self, tmp_path: Path, builtin_kind_nodes
    ) -> None:
        from protoboard.gui.footprint_choice import (
            DefaultFootprintChoiceProvider,
        )
        from protoboard.services.library.loader import load_library

        (tmp_path / "lib.yaml").write_text(
            "components:\n"
            "  - name: IC_8\n"
            "    component_class: ic\n"
            "    pins: {count: 8}\n"
            "    package: {kind: dip_ic}\n"
        )
        defn = load_library([tmp_path], package_families={}).get("IC_8")
        assert defn.package is not None
        assert defn.package.kind == "dip_ic"
        assert defn.package.color == "#2b2b30"
        assert defn.package.lineage == (
            "package", "prism", "prism_dil", "dip_ic")
        # The picker offers the kind, but with no parameter holes — a DIP's
        # value-tree schema/raw_values are Properties metadata, not picker
        # inputs, so placement stays one confirm (no second pop-up).
        provider = DefaultFootprintChoiceProvider({})
        opts = provider.options(defn)
        assert [o.is_kind for o in opts] == [True]
        assert provider.kind_params(opts[0].address) == []
