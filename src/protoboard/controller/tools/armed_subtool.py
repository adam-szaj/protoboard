# SPDX-License-Identifier: MIT
"""The ``ArmedSubTool`` contract — a two-level tool lifecycle.

Inner (operation) lifecycle: ``commit_op`` / ``cancel_op`` act on the *current
operation* and keep the tool armed. Outer (tool) lifecycle: ``can_exit`` gates
the single ``[Esc]`` exit. See
``docs/superpowers/specs/2026-07-24-armed-subtool-two-level-lifecycle-design.md``.

Partial conformance is intentional: a tool with no rich op-table returns
``False`` from ``on_key`` for verbs it does not own; a tool with no conflict
concept may return a constant ``True`` from ``can_commit``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from protoboard.controller.ports import KeyInput


@runtime_checkable
class ArmedSubTool(Protocol):
    # --- operation lifecycle (inner) ---
    def commit_op(self) -> bool:
        """Commit the current operation. Return True if committed, False if it
        parked (HARD conflict). Either way the tool STAYS armed."""
        ...

    def cancel_op(self) -> bool:
        """Revert the pending operation. Return True if something was pending."""
        ...

    def has_pending_op(self) -> bool:
        """Is there an in-flight operation to commit/cancel?"""
        ...

    def can_commit(self) -> bool:
        """False ⇒ a commit would land in a HARD conflict ⇒ park (stay armed)."""
        ...

    # --- tool lifecycle (outer) ---
    def on_key(self, inp: KeyInput) -> bool:
        """Op-table dispatch. Return False for verbs this tool does not own."""
        ...

    def can_exit(self) -> bool:
        """May ``[Esc]`` exit the tool now? Usually ``not has_pending_op()``."""
        ...
