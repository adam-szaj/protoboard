# SPDX-License-Identifier: MIT
"""Declarative kind nodes: registration, binding, and the generate flow."""

from __future__ import annotations

from pathlib import Path

import pytest

from protoboard.domain.components.package.generators import (
    KIND_DECLARED_SCHEMAS,
    KIND_GENERATORS,
    GeneratorContext,
    PackageGenerator,
    register_generator,
)
from protoboard.domain.components.package.kind import build_kind_package
from protoboard.domain.components.package.kind_nodes import (
    KIND_NODES,
    register_kind_node,
    register_kind_nodes,
)
from protoboard.domain.components.package.loader import (
    PackageDefError,
    load_packages,
)
from protoboard.domain.components.package.schema import AUTO
from protoboard.domain.components.package.shapes import Prism
from protoboard.geometry import Size3D


@pytest.fixture()
def scratch_registries():
    """Snapshot every kind registry and restore after the test."""
    gens = dict(KIND_GENERATORS)
    nodes = dict(KIND_NODES)
    schemas = dict(KIND_DECLARED_SCHEMAS)
    yield
    KIND_GENERATORS.clear()
    KIND_GENERATORS.update(gens)
    KIND_NODES.clear()
    KIND_NODES.update(nodes)
    KIND_DECLARED_SCHEMAS.clear()
    KIND_DECLARED_SCHEMAS.update(schemas)


def _base_registry() -> dict[str, dict]:
    """A minimal abstract base + one kind node, mirroring kinds.yaml."""
    return {
        "prismish": {
            "name": "prismish",
            "abstract": True,
            "shape": "prism",
            "schema": {
                "dims": {
                    "ly": {"type": "float"},
                    "lx": {"type": "float"},
                },
            },
            "leads": {"direction": [0, 0, -1], "board_offset": "default"},
            "bend": 0.0,
        },
        "_t_nodekind": {
            "name": "_t_nodekind",
            "kind": "_t_nodekind",
            "extends": "prismish",
            "color": "#123456",
            "dims": {"ly": "auto", "lx": "auto"},
            "leads": {"body_offset": "auto"},
        },
    }


class _NodeGen(PackageGenerator):
    """Scratch generator whose generate fills the node's auto slots."""

    def generate(self, ctx: GeneratorContext) -> GeneratorContext:
        values = dict(ctx.values)
        dims = dict(values.get("dims") or {})
        leads = dict(values.get("leads") or {})
        if dims.get("ly") == AUTO:
            dims["ly"] = 5.0
        if dims.get("lx") == AUTO:
            dims["lx"] = float(ctx.pin_count) * 2.5
        if leads.get("body_offset") == AUTO:
            leads["body_offset"] = [[2.5, 0.0]] * (ctx.pin_count - 1)
        values["dims"] = dims
        values["leads"] = leads
        return ctx.with_values(values)


class TestRegistration:

    def test_register_binds_node_with_kind_as_lineage_leaf(
        self, scratch_registries
    ) -> None:
        register_kind_node("_t_nodekind", _base_registry())
        node = KIND_NODES["_t_nodekind"]
        assert node.kind == "_t_nodekind"
        assert node.lineage == ("prismish", "_t_nodekind")
        # name/kind stripped; extends resolved (parent schema merged in).
        assert "name" not in node.merged and "kind" not in node.merged
        assert node.merged["shape"] == "prism"
        assert node.merged["dims"]["ly"] == "auto"

    def test_register_kind_nodes_walks_only_kind_bearing_entries(
        self, scratch_registries
    ) -> None:
        register_kind_nodes(_base_registry())
        assert "_t_nodekind" in KIND_NODES
        assert "prismish" not in KIND_NODES

    def test_own_schema_binds_into_declared_schemas_and_params(
        self, scratch_registries
    ) -> None:
        @register_generator(kind="_t_schemakind")
        class Gen(_NodeGen):
            pass

        registry = _base_registry()
        registry["_t_schemakind"] = {
            "name": "_t_schemakind",
            "kind": "_t_schemakind",
            "extends": "prismish",
            "schema": {"rows": {"type": "int", "default": 1}},
            "dims": {"ly": 1.0, "lx": 1.0},
            "leads": {"body_offset": [[2.5, 0.0]]},
        }
        register_kind_node("_t_schemakind", registry)
        assert "rows" in KIND_DECLARED_SCHEMAS["_t_schemakind"].fields
        # The generator serves the bound schema when instantiated FOR the
        # kind — and its code-side schema otherwise.
        assert "rows" in Gen("_t_schemakind").params.fields
        assert Gen().params.is_empty()

    def test_unknown_extends_raises(self, scratch_registries) -> None:
        registry = {
            "_t_orphan": {
                "name": "_t_orphan",
                "kind": "_t_orphan",
                "extends": "_no_such_base",
            },
        }
        with pytest.raises(PackageDefError):
            register_kind_node("_t_orphan", registry)


class TestLoadPackagesSkip:

    def test_kind_nodes_are_not_built_eagerly(self) -> None:
        built = load_packages(list(_base_registry().values()))
        # The abstract base is skipped as before; the kind node's ``auto``
        # slots make it unbuildable without a generator — it must be skipped.
        assert built == {}


class TestNodeBuild:

    def test_generate_fills_and_build_package_validates(
        self, scratch_registries
    ) -> None:
        @register_generator(kind="_t_nodekind")
        class Gen(_NodeGen):
            pass

        register_kind_node("_t_nodekind", _base_registry())
        out = build_kind_package("_t_nodekind", GeneratorContext(pin_count=3))
        pkg = out.package
        assert isinstance(pkg, Prism)
        assert pkg.kind == "_t_nodekind"
        # build_package prepends the shape root when the extends chain does
        # not already pass through it (the real kinds.yaml chain does).
        assert pkg.lineage == ("prism", "prismish", "_t_nodekind")
        assert pkg.color == "#123456"  # node's declared colour
        assert pkg.size_mm == Size3D(7.5, 5.0)
        assert len(pkg.leads.body_offset_mm) == 2
        # raw_values keep the authored ``auto`` for the UI.
        assert pkg.raw_values is not None
        assert pkg.raw_values["dims"]["ly"] == "auto"

    def test_generate_receives_defaults_schema_and_source_context(
        self, scratch_registries
    ) -> None:
        calls = []

        @register_generator(kind="_t_nodekind")
        class Gen(_NodeGen):
            def generate(self, ctx: GeneratorContext) -> GeneratorContext:
                calls.append(ctx)
                return super().generate(ctx)

        registry = _base_registry()
        registry["_t_nodekind"]["schema"] = {
            "rows": {"type": "int", "default": 2},
        }
        register_kind_node("_t_nodekind", registry)
        component = {"name": "Source", "meta_pins": ["A", "B"]}
        option = {"name": "Size", "ref": "$_t_nodekind"}
        out = build_kind_package(
            "_t_nodekind",
            GeneratorContext(pin_count=2, component=component, option=option),
        )
        assert len(calls) == 1
        assert calls[0].values["rows"] == 2
        assert calls[0].values["dims"]["lx"] == AUTO
        assert calls[0].schema is not None
        assert "dims.lx" in calls[0].schema.fields
        assert out.component == component
        assert out.option == option

    def test_generate_can_return_a_package_with_a_template(
        self, scratch_registries
    ) -> None:
        from protoboard.domain.components.footprints import SIL
        from protoboard.domain.components.package.generators import prism_from_footprint

        @register_generator(kind="_t_nodekind")
        class Gen(PackageGenerator):
            def generate(self, ctx: GeneratorContext) -> GeneratorContext:
                package = prism_from_footprint(SIL(n=ctx.pin_count))
                assert package is not None
                return ctx.with_package(package)

        register_kind_node("_t_nodekind", _base_registry())
        pkg = build_kind_package("_t_nodekind", GeneratorContext(pin_count=2)).package
        assert isinstance(pkg, Prism)
        assert pkg.kind == "_t_nodekind"
        assert pkg.color == "#123456"
        assert pkg.lineage == ("prism", "prismish", "_t_nodekind")

    def test_generate_can_redirect_from_a_template(self, scratch_registries) -> None:
        from protoboard.domain.components.package.generators import ResidualKind

        @register_generator(kind="_t_nodekind")
        class Gen(PackageGenerator):
            def generate(self, ctx: GeneratorContext) -> GeneratorContext:
                return ctx.with_residual(ResidualKind(kind="sil"))

        register_kind_node("_t_nodekind", _base_registry())
        out = build_kind_package("_t_nodekind", GeneratorContext(pin_count=2))
        assert isinstance(out.package, Prism)
        assert out.package.kind == "sil"

    def test_explicit_color_beats_template_color(self, scratch_registries) -> None:
        @register_generator(kind="_t_nodekind")
        class Gen(_NodeGen):
            pass

        register_kind_node("_t_nodekind", _base_registry())
        pkg = build_kind_package(
            "_t_nodekind",
            GeneratorContext(pin_count=2, values={"color": "#abcdef"}),
        ).package
        assert isinstance(pkg, Prism)
        assert pkg.color == "#abcdef"

    def test_generator_color_is_only_a_fallback(self, scratch_registries) -> None:
        @register_generator(kind="_t_nodekind")
        class Gen(_NodeGen):
            color = "#abcdef"

        registry = _base_registry()
        del registry["_t_nodekind"]["color"]
        register_kind_node("_t_nodekind", registry)
        pkg = build_kind_package("_t_nodekind", GeneratorContext(pin_count=2)).package
        assert isinstance(pkg, Prism) and pkg.color == "#abcdef"

    def test_unresolved_auto_is_a_loud_error(self, scratch_registries) -> None:
        @register_generator(kind="_t_nodekind")
        class Identity(PackageGenerator):

            def generate(self, ctx: GeneratorContext) -> GeneratorContext:
                return ctx

        register_kind_node("_t_nodekind", _base_registry())
        with pytest.raises(PackageDefError, match="auto.*dims.(ly|lx)"):
            build_kind_package("_t_nodekind", GeneratorContext(pin_count=2))

    def test_explicit_value_beats_auto(self, scratch_registries) -> None:
        @register_generator(kind="_t_nodekind")
        class Gen(_NodeGen):
            pass

        register_kind_node("_t_nodekind", _base_registry())
        pkg = build_kind_package(
            "_t_nodekind",
            GeneratorContext(pin_count=3, values={"dims": {"ly": 9.0}}),
        ).package
        assert isinstance(pkg, Prism)
        assert pkg.size_mm is not None
        assert pkg.size_mm.ly == 9.0  # explicit override
        assert pkg.size_mm.lx == 7.5  # auto still filled


class TestLibraryPackagesSection:

    def test_library_file_can_ship_a_kind_node(
        self, tmp_path: Path, scratch_registries
    ) -> None:
        @register_generator(kind="_t_libnode")
        class Gen(_NodeGen):
            pass

        from protoboard.services.library.loader import load_library

        (tmp_path / "lib.yaml").write_text(
            "packages:\n"
            "  - name: _t_libnode\n"
            "    kind: _t_libnode\n"
            "    extends: prism_dil\n"
            "    color: '#654321'\n"
            "    dims: {ly: auto, lx: auto}\n"
            "    leads: {body_offset: auto}\n"
            "components:\n"
            "  - name: PART_NODE\n"
            "    component_class: ic\n"
            "    meta_pins: [P1, P2, P3, P4]\n"
            "    package: {kind: _t_libnode}\n"
        )
        lib = load_library([tmp_path], package_families={})
        defn = lib.get("PART_NODE")
        from protoboard.domain.components.package import resolve_component_package

        package = resolve_component_package(defn, None, lib.package_families)
        assert isinstance(package, Prism)
        assert package.kind == "_t_libnode"
        assert package.color == "#654321"
        assert package.lineage[-1] == "_t_libnode"
