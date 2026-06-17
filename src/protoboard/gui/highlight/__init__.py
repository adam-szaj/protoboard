# SPDX-License-Identifier: MIT
"""Highlight layer — the standalone highlight service for the SelectTool rewrite.

:class:`HighlightController` is a thin service over the existing, already-clean
:class:`~protoboard.gui.highlight_state.HighlightStack` opacity-dimming layer.
SelectTool is its default client, but any tool that needs to highlight entities
(RouteTool's target net, a DRC error focus) can drive it.

Phase 1 covers the dimming side only: turning a set of :class:`Selectable`
entities into the stack's auto-tier ``HighlightEntry`` so ``BoardView`` dims the
rest.  The QGraphicsItem overlay drawing (``Selectable.draw_highlight``) lands
in Phase 2 with the new SelectTool, its first consumer.
"""

from __future__ import annotations

from protoboard.gui.highlight.controller import HighlightController

__all__ = ["HighlightController"]
