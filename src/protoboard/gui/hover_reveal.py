# SPDX-License-Identifier: MIT
"""Hover-reveal controller: dwell → label tooltip (2A) or pin-name reveal (2B).

Subscribes to the board view's hover stream.  When the ``labels`` layer is
hidden, a ~1 s dwell over a component shows its ``refdes value`` in a tooltip
at the cursor.  When the ``pin_names`` layer is hidden and Alt is held, the
dwell instead switches that one component's pin names on via
``BoardView._pin_names_reveal_cid`` — the same render path the layer uses,
for one component only.  Any move / leave / cid-change / Alt-release clears
both.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from protoboard.gui.hover_tooltip import HoverTooltip
from protoboard.gui.layer_state import LayerVisibility
from protoboard.gui.throttle import TimerWrap

if TYPE_CHECKING:
    from protoboard.controller.ports.hover import HoverEvent
    from protoboard.gui.board_view import BoardView


class HoverReveal:
    """Dwell-driven reveal of a hidden label (tooltip) or pin names (2B)."""

    def __init__(self, view: "BoardView") -> None:
        self._view = view
        self._tooltip = HoverTooltip(view.viewport())
        self._timer = TimerWrap(1000, self._on_dwell, restart_on_arm=True)
        self._pending_cid: uuid.UUID | None = None
        self._pending_view_pos: tuple[int, int] = (0, 0)
        self._pending_alt: bool = False

    def on_hover(self, event: "HoverEvent", view_pos: tuple[int, int]) -> None:
        """Re-arm the dwell for the hovered component (or clear on bare canvas)."""
        cid = event.objects[0] if event.objects else None
        alt = bool(event.mods.alt)
        # A move that changes the hovered component, or an Alt press/release,
        # clears the current reveal (branch 2A vs 2B depends on Alt).
        if cid != self._pending_cid or alt != self._pending_alt:
            self.clear()
        self._pending_cid = cid
        self._pending_view_pos = view_pos
        self._pending_alt = alt
        if cid is None:
            self._timer.stop()
            return
        self._timer.arm()

    def _on_dwell(self) -> None:
        cid = self._pending_cid
        if cid is None:
            return
        overrides = self._view.layer_overrides
        if (overrides.labels == LayerVisibility.HIDDEN
                and not self._pending_alt):
            text = self._label_for(cid)
            if text:
                self._tooltip.show_at(text, self._pending_view_pos)
            return
        if (overrides.pin_names == LayerVisibility.HIDDEN
                and self._pending_alt):
            self._view._pin_names_reveal_cid = cid
            self._view.refresh()

    def clear(self) -> None:
        """Hide the tooltip and drop any active pin-name reveal."""
        self._tooltip.hide()
        if self._view._pin_names_reveal_cid is not None:
            self._view._pin_names_reveal_cid = None
            self._view.refresh()

    def _label_for(self, cid: uuid.UUID) -> str | None:
        inst = self._view.session.instances.get(cid)
        if inst is None:
            return None
        return f"{inst.refdes} {inst.value}" if inst.value else inst.refdes
