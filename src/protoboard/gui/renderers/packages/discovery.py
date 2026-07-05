# SPDX-License-Identifier: MIT
"""Renderer plugin discovery — load ``renderers/*.py`` from library dirs.

A user library directory (any entry of ``component_library_dirs``) may carry a
``renderers/`` subdirectory.  Every ``*.py`` file in it is imported at library
load; a module registers its renderers simply by *defining* a
:class:`~protoboard.gui.renderers.packages.base.PackageRenderer` subclass
decorated with
:func:`~protoboard.gui.renderers.packages.base.register_renderer` (or
carrying the legacy ``renders`` attribute) — registration happens at class
creation, so no explicit call is needed.  Example plugin
(``<library>/renderers/to220.py``)::

    from protoboard.gui.renderers.packages import (
        PrismRenderer,
        register_renderer,
    )

    @register_renderer(family="TO-220")
    class TO220Renderer(PrismRenderer):

        def render_body(self, positions):
            items = super().render_body(positions)
            items.append(self._metal_tab(...))  # paint state: self.paint
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
