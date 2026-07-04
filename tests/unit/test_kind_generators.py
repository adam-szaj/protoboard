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

    def test_dip_narrow_vs_wide(self) -> None:
        narrow = build_kind_package(
            "dip_ic", GeneratorContext(pin_count=16)).package
        wide = build_kind_package(
            "wdip_ic", GeneratorContext(pin_count=16)).package
        assert isinstance(narrow, Prism) and isinstance(wide, Prism)
        assert narrow.kind == "dip_ic" and wide.kind == "wdip_ic"
        assert narrow.lineage == ("prism", "dip_ic")
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
