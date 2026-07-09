# SPDX-License-Identifier: MIT
"""``HelpOverlayPort`` — the ``[?]`` shortcut-help surface (qt-ports §2.5).

Low value alone, but it is the last widget API in ToolHost: with it, the
host becomes headless-drivable.  ``_render_shortcuts_html`` (pure) stays in
ToolHost; only showing/hiding the rendered panel goes behind the port.
"""

from __future__ import annotations

from typing import Protocol


class HelpOverlayPort(Protocol):
    """Show/hide the floating shortcut-help panel."""

    def show_help(self, html: str) -> None: ...

    def hide_help(self) -> None: ...

    @property
    def help_visible(self) -> bool: ...
