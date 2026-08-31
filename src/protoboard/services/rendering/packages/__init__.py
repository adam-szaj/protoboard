# SPDX-License-Identifier: MIT
"""Package renderer hierarchy — one renderer class per package lineage node.

The body of a component is drawn by a :class:`PackageRenderer` subclass picked
by :func:`renderer_for`, which walks the package's ``lineage`` (shape root →
``extends`` chain → ``kind``) leaf-first and returns the most specific
registered renderer — unless a ``component_class`` registration claims the
part first (see :func:`register_renderer`).  Built-in renderers cover the
three shape roots (``cylinder`` / ``prism`` / ``disc``); user plugins dropped
into a library's ``renderers/`` directory subclass them and register with the
:func:`register_renderer` decorator (or the legacy ``renders`` attribute; see
:mod:`.discovery`).
"""

from protoboard.services.rendering.packages.base import (
    BODY_RENDERERS,
    CLASS_RENDERERS,
    BodyShape,
    GenericBody,
    PackageRenderer,
    register_renderer,
    renderer_for,
)
from protoboard.services.rendering.packages.canvas import (
    BodyCanvas,
    PaintState,
    qcolor_from_css,
)
from protoboard.services.rendering.packages.cylinder import CylinderRenderer
from protoboard.services.rendering.packages.disc import DiscRenderer
from protoboard.services.rendering.packages.discovery import load_renderer_plugins
from protoboard.services.rendering.packages.prism import (
    DevboardRenderer,
    PrismRenderer,
)
from protoboard.services.rendering.packages.prism_kinds import (
    DipIcRenderer,
    PlainBodyRenderer,
    SilHeaderRenderer,
    SmdChipRenderer,
)

__all__ = [
    "BodyCanvas",
    "BODY_RENDERERS",
    "BodyShape",
    "CLASS_RENDERERS",
    "CylinderRenderer",
    "DevboardRenderer",
    "DipIcRenderer",
    "DiscRenderer",
    "GenericBody",
    "PackageRenderer",
    "PaintState",
    "PlainBodyRenderer",
    "PrismRenderer",
    "SilHeaderRenderer",
    "SmdChipRenderer",
    "load_renderer_plugins",
    "qcolor_from_css",
    "register_renderer",
    "renderer_for",
]
