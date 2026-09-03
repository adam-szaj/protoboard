# SPDX-License-Identifier: MIT
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).parents[2] / "src" / "protoboard"

_LEGACY_GROUP_QUERIES = frozenset(
    {
        "all_component_ids",
        "exposable_surface",
        "external_pins_for_group",
        "internal_net_ids_for_components",
        "parent_group_of",
        "promote_selection_to_top_level",
        "schematic_projection_for_group",
        "top_level_group_for",
        "top_level_group_under",
        "validate_exported_pins",
        "would_create_cycle",
    }
)


def _imported_modules(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            imports.extend((node.lineno, alias.name) for alias in node.names)
    return imports


def _assert_no_imports(layer: str, forbidden: tuple[str, ...]) -> None:
    offenders: list[str] = []
    for path in sorted((SRC / layer).rglob("*.py")):
        for lineno, module in _imported_modules(path):
            if any(
                module == root or module.startswith(f"{root}.") for root in forbidden
            ):
                relative = path.relative_to(SRC)
                offenders.append(f"{relative}:{lineno}: {module}")
    assert not offenders, f"{layer} has forbidden imports:\n" + "\n".join(offenders)


def test_controller_does_not_import_outer_layers() -> None:
    _assert_no_imports(
        "controller",
        ("protoboard.services", "protoboard.gui"),
    )


def test_services_do_not_import_controller_implementation() -> None:
    _assert_no_imports(
        "services",
        (
            "protoboard.controller.editing",
            "protoboard.controller.selection",
            "protoboard.controller.tools",
        ),
    )


def test_domain_does_not_import_outer_layers() -> None:
    _assert_no_imports(
        "domain",
        (
            "protoboard.controller",
            "protoboard.services",
            "protoboard.gui",
        ),
    )


def test_instrumentation_is_dependency_neutral() -> None:
    _assert_no_imports(
        "instrumentation",
        (
            "protoboard.domain",
            "protoboard.controller",
            "protoboard.services",
            "protoboard.gui",
        ),
    )


def test_services_do_not_import_gui_layer_state() -> None:
    offenders: list[str] = []
    for path in sorted((SRC / "services").rglob("*.py")):
        for _lineno, module in _imported_modules(path):
            if module == "protoboard.gui.layer_state":
                offenders.append(str(path.relative_to(SRC)))
    assert not offenders, f"services import GUI-owned LayerState: {offenders}"


def test_export_snapshot_uses_a_structural_session_contract() -> None:
    path = SRC / "services/export/snapshot.py"
    modules = [module for _lineno, module in _imported_modules(path)]
    assert "protoboard.gui.session" not in modules


def test_export_paint_uses_service_rendering_helpers() -> None:
    path = SRC / "services/export/paint.py"
    modules = [module for _lineno, module in _imported_modules(path)]
    assert "protoboard.gui.renderers.net_color" not in modules
    forbidden = {
        "protoboard.gui.renderers.board_renderer",
        "protoboard.gui.renderers.component_renderer",
        "protoboard.gui.renderers.pin_pad_renderer",
        "protoboard.gui.renderers.wire_renderer",
    }
    assert not forbidden.intersection(modules)


def test_group_queries_use_group_tree_without_legacy_shims() -> None:
    group_path = SRC / "domain/layout/group.py"
    group_tree = ast.parse(
        group_path.read_text(encoding="utf-8"), filename=str(group_path)
    )
    definitions = {
        node.name
        for node in group_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in _LEGACY_GROUP_QUERIES
    }

    imports: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "protoboard.domain.layout.group":
                continue
            for alias in node.names:
                if alias.name in _LEGACY_GROUP_QUERIES:
                    imports.append(
                        f"{path.relative_to(SRC)}:{node.lineno}: {alias.name}"
                    )

    assert not definitions, f"legacy group query definitions: {sorted(definitions)}"
    assert not imports, "legacy group query imports:\n" + "\n".join(imports)


def test_group_tree_declares_required_domain_dependencies_at_module_scope() -> None:
    """GroupTree's non-optional domain collaborators must be visible in imports."""
    path = SRC / "domain/layout/group_tree.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    required = {
        "protoboard.domain.components.resolution",
        "protoboard.domain.layout.placement",
        "protoboard.domain.schematic.net",
        "protoboard.domain.schematic.schematic",
        "protoboard.geometry",
    }
    deferred: list[str] = []

    class _Visitor(ast.NodeVisitor):
        function_depth = 0

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.function_depth += 1
            self.generic_visit(node)
            self.function_depth -= 1

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if self.function_depth and node.module in required:
                deferred.append(f"{node.lineno}: {node.module}")
            self.generic_visit(node)

        def visit_Import(self, node: ast.Import) -> None:
            if self.function_depth:
                for alias in node.names:
                    if alias.name in required:
                        deferred.append(f"{node.lineno}: {alias.name}")
            self.generic_visit(node)

    _Visitor().visit(tree)
    assert not deferred, "GroupTree defers required imports:\n" + "\n".join(deferred)


def test_pad_index_declares_required_domain_dependencies_at_module_scope() -> None:
    """PadIndex must expose its topology collaborators explicitly."""
    path = SRC / "domain/board/pad_state.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    required = {
        "protoboard.domain.board.boxes",
        "protoboard.domain.components.package.component_package",
        "protoboard.domain.components.package.mask",
        "protoboard.domain.components.package.pin_mapping",
        "protoboard.domain.components.resolution",
        "protoboard.domain.layout.placement",
    }
    deferred: list[str] = []

    class _Visitor(ast.NodeVisitor):
        function_depth = 0

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.function_depth += 1
            self.generic_visit(node)
            self.function_depth -= 1

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if self.function_depth and node.module in required:
                deferred.append(f"{node.lineno}: {node.module}")
            self.generic_visit(node)

        def visit_Import(self, node: ast.Import) -> None:
            if self.function_depth:
                for alias in node.names:
                    if alias.name in required:
                        deferred.append(f"{node.lineno}: {alias.name}")
            self.generic_visit(node)

    _Visitor().visit(tree)
    assert not deferred, "PadIndex defers required imports:\n" + "\n".join(deferred)


def test_group_ownership_declares_required_domain_dependencies_at_module_scope() -> (
    None
):
    """Group ownership must expose its pure domain collaborators explicitly."""
    path = SRC / "domain/layout/group_ownership.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    required = {
        "protoboard.domain.components.package.component_package",
        "protoboard.domain.components.package.pin_mapping",
        "protoboard.domain.layout.group_tree",
        "protoboard.domain.layout.knot",
        "protoboard.domain.layout.placement",
    }
    deferred: list[str] = []

    class _Visitor(ast.NodeVisitor):
        function_depth = 0

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.function_depth += 1
            self.generic_visit(node)
            self.function_depth -= 1

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if self.function_depth and node.module in required:
                deferred.append(f"{node.lineno}: {node.module}")
            self.generic_visit(node)

        def visit_Import(self, node: ast.Import) -> None:
            if self.function_depth:
                for alias in node.names:
                    if alias.name in required:
                        deferred.append(f"{node.lineno}: {alias.name}")
            self.generic_visit(node)

    _Visitor().visit(tree)
    assert not deferred, "Group ownership defers required imports:\n" + "\n".join(
        deferred
    )


def test_wire_utils_declares_required_wire_dependencies_at_module_scope() -> None:
    """Wire utilities must expose their runtime wire/knot collaborators."""
    path = SRC / "domain/layout/wire_utils.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    required = {
        "protoboard.domain.layout.knot",
        "protoboard.domain.layout.pad_strategy",
        "protoboard.domain.layout.placement",
        "protoboard.domain.layout.wire",
    }
    deferred: list[str] = []

    class _Visitor(ast.NodeVisitor):
        function_depth = 0

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.function_depth += 1
            self.generic_visit(node)
            self.function_depth -= 1

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if self.function_depth and node.module in required:
                deferred.append(f"{node.lineno}: {node.module}")
            self.generic_visit(node)

        def visit_Import(self, node: ast.Import) -> None:
            if self.function_depth:
                for alias in node.names:
                    if alias.name in required:
                        deferred.append(f"{node.lineno}: {alias.name}")
            self.generic_visit(node)

    _Visitor().visit(tree)
    assert not deferred, "Wire utilities defer required imports:\n" + "\n".join(
        deferred
    )
