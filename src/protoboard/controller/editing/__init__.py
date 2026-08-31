# SPDX-License-Identifier: MIT
"""Application-layer editing: the presentation-free edit-verb layer.

This package is the *Controller* tier of the MVC split (CLAUDE.md §3
"Application Layer"). It is Qt-free — importable by ``gui/`` and by
headless CLI/test front-ends alike — the same mechanical rule that keeps
``domain/`` display-independent.

* :class:`~protoboard.controller.editing.controller.EditController`
  — the stateless verb surface that both GUI tools and a CLI drive.
"""

from protoboard.controller.editing.contexts import EditingContext
from protoboard.controller.editing.controller import EditController

__all__ = ["EditController", "EditingContext"]
