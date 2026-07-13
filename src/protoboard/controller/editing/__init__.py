# SPDX-License-Identifier: MIT
"""Application-layer editing: the presentation-free edit-verb layer.

This package is the *Controller* tier of the MVC split (CLAUDE.md §3
"Application Layer"). It is Qt-free — importable by ``gui/`` and by
headless CLI/test front-ends alike — the same mechanical rule that keeps
``domain/`` display-independent.

* :class:`~protoboard.controller.editing.headless_session.HeadlessSession`
  — the mutable state substrate (board / layout / schematic / instances /
  components + CommandStack + read caches) with no Qt dependency.
  ``EditorSession`` is this base plus a ``QObject`` and its signals.
* :class:`~protoboard.controller.editing.controller.EditController`
  — the stateless verb surface that both GUI tools and a CLI drive.
"""

from protoboard.controller.editing.controller import EditController, EditState
from protoboard.controller.editing.headless_session import HeadlessSession

__all__ = ["EditController", "EditState", "HeadlessSession"]
