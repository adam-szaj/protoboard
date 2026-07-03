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

The actual import machinery is shared with the generator plugins — see
:mod:`protoboard.services.library.plugin_discovery`.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from protoboard.services.library.plugin_discovery import load_plugin_modules


def load_renderer_plugins(library_dirs: Iterable[Path]) -> list[Path]:
    """Import every ``renderers/*.py`` under *library_dirs*; return loaded paths.

    See :func:`~protoboard.services.library.plugin_discovery.load_plugin_modules`
    for ordering / idempotency / error handling.
    """
    return load_plugin_modules(
        library_dirs, "renderers", namespace="protoboard_renderer_plugins")
