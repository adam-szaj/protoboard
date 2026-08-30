# SPDX-License-Identifier: MIT
"""Protoboard Designer — connectivity-aware perfboard/stripboard layout tool."""

# --- Process-wide workarounds (applied at package import) ---------------
#
# PySide6's Wayland platform plugin crashes on libwayland-client >= 1.24
# with FreeType 2.14 ("render glyph err=62" → SIGSEGV).  Forcing XCB
# (XWayland) sidesteps the crash.  We set this here (not just in app.py)
# so that *any* script that touches `protoboard.*` — including headless
# autoroute helpers that never import the GUI — inherits the workaround.
import os as _os

_os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

# Silent SIGSEGV in worker threads (seen in CPython 3.14.2's GC
# `mark_stacks` since the 2026-05-15 system upgrade) leaves nothing in
# stdout — only a dmesg line.  `faulthandler` prints a native traceback
# when a fatal signal fires, so we know which Python frame was active.
import faulthandler as _faulthandler

_faulthandler.enable()
del _os, _faulthandler

__version__ = "0.1.0"
__all__: list[str] = []
