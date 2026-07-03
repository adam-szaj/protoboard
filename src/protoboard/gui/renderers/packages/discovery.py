# SPDX-License-Identifier: MIT
"""Renderer plugin discovery — load ``renderers/*.py`` from library dirs.

A user library directory (any entry of ``component_library_dirs``) may carry a
``renderers/`` subdirectory.  Every ``*.py`` file in it is imported at library
load; a module registers its renderers simply by *defining* a
:class:`~protoboard.gui.renderers.packages.base.PackageRenderer` subclass with
a ``renders`` attribute — registration happens in ``__init_subclass__``, so no
explicit call is needed.  Example plugin (``<library>/renderers/to220.py``)::

    from protoboard.gui.renderers.packages import PrismRenderer

    class TO220Renderer(PrismRenderer):
        renders = "to220"

        def render_body(self, positions, fill, border, alpha, z, theme):
            items = super().render_body(positions, fill, border, alpha, z, theme)
            items.append(self._metal_tab(...))
            return items

A failing plugin is logged and skipped — one broken file never blocks the
rest of the library.  Note that plugins are ordinary Python executed in the
application process (the same trust level as the application itself).
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import Iterable
from pathlib import Path

import structlog

_log = structlog.get_logger(__name__)

_PLUGIN_PACKAGE = "protoboard_renderer_plugins"


def _module_name(path: Path) -> str:
    """A unique, importable module name derived from the plugin path."""
    stem = re.sub(r"\W+", "_", str(path.with_suffix("")))
    return f"{_PLUGIN_PACKAGE}.{stem}"


def load_renderer_plugins(library_dirs: Iterable[Path]) -> list[Path]:
    """Import every ``renderers/*.py`` under *library_dirs*; return loaded paths.

    Import order is deterministic (dirs as given, files sorted), so with the
    registry's last-wins semantics a later directory's plugin overrides an
    earlier one's for the same ``renders`` key — mirroring how the component
    library itself resolves name collisions. A module already imported (same
    resolved path) is skipped, so repeated library reloads stay idempotent.
    """
    loaded: list[Path] = []
    for base_dir in library_dirs:
        renderers_dir = Path(base_dir) / "renderers"
        if not renderers_dir.is_dir():
            continue
        for py_file in sorted(renderers_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            name = _module_name(py_file.resolve())
            if name in sys.modules:
                loaded.append(py_file)
                continue
            spec = importlib.util.spec_from_file_location(name, py_file)
            if spec is None or spec.loader is None:
                _log.warning("renderer plugin unloadable", path=str(py_file))
                continue
            module = importlib.util.module_from_spec(spec)
            try:
                sys.modules[name] = module
                spec.loader.exec_module(module)
            except Exception:
                sys.modules.pop(name, None)
                _log.warning(
                    "renderer plugin failed to load",
                    path=str(py_file),
                    exc_info=True,
                )
                continue
            _log.info("renderer plugin loaded", path=str(py_file))
            loaded.append(py_file)
    return loaded
