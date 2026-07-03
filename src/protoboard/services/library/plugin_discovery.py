# SPDX-License-Identifier: MIT
"""Library plugin discovery — import ``<subdir>/*.py`` from library dirs.

The shared loader behind the two plugin surfaces:

* ``renderers/`` — ``PackageRenderer`` subclasses (GUI; see
  :mod:`protoboard.gui.renderers.packages.discovery`);
* ``generators/`` — ``PackageGenerator`` subclasses (pure domain; loadable
  headless; see :mod:`protoboard.domain.components.package.generators`).

A module registers its classes simply by *defining* them — registration
happens in ``__init_subclass__``, so no explicit call is needed. A failing
plugin is logged and skipped — one broken file never blocks the rest of the
library. Plugins are ordinary Python executed in the application process (the
same trust level as the application itself).
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import Iterable
from pathlib import Path

import structlog

_log = structlog.get_logger(__name__)


def _module_name(namespace: str, path: Path) -> str:
    """A unique, importable module name derived from the plugin path."""
    stem = re.sub(r"\W+", "_", str(path.with_suffix("")))
    return f"{namespace}.{stem}"


def load_plugin_modules(
    library_dirs: Iterable[Path], subdir: str, *, namespace: str,
) -> list[Path]:
    """Import every ``<subdir>/*.py`` under *library_dirs*; return loaded paths.

    Import order is deterministic (dirs as given, files sorted), so with the
    registries' last-wins semantics a later directory's plugin overrides an
    earlier one's for the same key — mirroring how the component library
    itself resolves name collisions. A module already imported (same resolved
    path) is skipped, so repeated library reloads stay idempotent.
    ``_``-prefixed files are skipped (private helpers).
    """
    loaded: list[Path] = []
    for base_dir in library_dirs:
        plugin_dir = Path(base_dir) / subdir
        if not plugin_dir.is_dir():
            continue
        for py_file in sorted(plugin_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            name = _module_name(namespace, py_file.resolve())
            if name in sys.modules:
                loaded.append(py_file)
                continue
            spec = importlib.util.spec_from_file_location(name, py_file)
            if spec is None or spec.loader is None:
                _log.warning(f"{subdir} plugin unloadable", path=str(py_file))
                continue
            module = importlib.util.module_from_spec(spec)
            try:
                sys.modules[name] = module
                spec.loader.exec_module(module)
            except Exception:
                sys.modules.pop(name, None)
                _log.warning(
                    f"{subdir} plugin failed to load",
                    path=str(py_file),
                    exc_info=True,
                )
                continue
            _log.info(f"{subdir} plugin loaded", path=str(py_file))
            loaded.append(py_file)
    return loaded


def load_generator_plugins(library_dirs: Iterable[Path]) -> list[Path]:
    """Import every ``generators/*.py`` under *library_dirs*.

    A plugin defines a ``PackageGenerator`` subclass with a ``generates``
    attribute; the class body registers it in ``KIND_GENERATORS``::

        from protoboard.domain.components.package.generators import (
            PackageGenerator, prism_from_footprint,
        )

        class MyKindGenerator(PackageGenerator):
            generates = "my_kind"
            def generate(self, ctx):
                return ctx.with_package(...)
    """
    return load_plugin_modules(
        library_dirs, "generators", namespace="protoboard_generator_plugins")


__all__ = ["load_generator_plugins", "load_plugin_modules"]
