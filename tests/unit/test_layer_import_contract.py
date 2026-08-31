# SPDX-License-Identifier: MIT
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).parents[2] / "src" / "protoboard"


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


def test_controller_does_not_import_gui() -> None:
    _assert_no_imports("controller", ("protoboard.gui",))


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
