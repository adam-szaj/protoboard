# SPDX-License-Identifier: MIT
"""Package renderer hierarchy — one renderer class per package lineage node.

The body of a component is drawn by a :class:`PackageRenderer` subclass picked
by :func:`renderer_for`, which walks the package's ``lineage`` (shape root →
``extends`` chain → ``kind``) leaf-first and returns the most specific
registered renderer.  Built-in renderers cover the three shape roots
(``cylinder`` / ``prism`` / ``disc``); user plugins dropped into a library's
``renderers/`` directory subclass them and register by declaring ``renders``
(see :mod:`.discovery`).
"""

from protoboard.gui.renderers.packages.base import (
    BODY_RENDERERS,
    BodyShape,
    GenericBody,
    PackageRenderer,
    qcolor_from_css,
    renderer_for,
)
from protoboard.gui.renderers.packages.cylinder import CylinderRenderer
from protoboard.gui.renderers.packages.disc import DiscRenderer
from protoboard.gui.renderers.packages.discovery import load_renderer_plugins
from protoboard.gui.renderers.packages.prism import PrismRenderer

__all__ = [
    "BODY_RENDERERS",
    "BodyShape",
    "CylinderRenderer",
    "DiscRenderer",
    "GenericBody",
    "PackageRenderer",
    "PrismRenderer",
    "load_renderer_plugins",
    "qcolor_from_css",
    "renderer_for",
]
