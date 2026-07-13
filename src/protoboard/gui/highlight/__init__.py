# SPDX-License-Identifier: MIT
"""Highlight layer — the standalone highlight service for the SelectTool rewrite.

:class:`HighlightController` is a thin service over the existing, already-clean
:class:`~protoboard.gui.highlight_state.HighlightStack` opacity-dimming layer.
SelectTool is its default client, but any tool that needs to highlight entities
(RouteTool's target net, a DRC error focus) can drive it.

The controller covers the dimming side: turning a set of :class:`Selectable`
entities into the stack's auto-tier ``HighlightEntry`` so ``BoardView`` dims the
rest.  (A per-selectable ``draw_highlight`` overlay member was once planned on
the ``Selectable`` protocol; it never gained a consumer and was removed
2026-07-09 — the stack pipeline is the highlight mechanism.)
"""

from __future__ import annotations

from protoboard.controller.highlight_controller import HighlightController

__all__ = ["HighlightController"]
