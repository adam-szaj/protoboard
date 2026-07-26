# SPDX-License-Identifier: MIT
"""Qt-free behaviour of :class:`HighlightStackBase`.

The stack logic was lifted out of the ``QObject`` ``gui.highlight_state.
HighlightStack`` so a headless session can carry it. These tests run without
any QApplication — importing PySide6 here would defeat the point — and assert
the tier precedence, source-keyed auto entries, and the ``_changed`` hook that
the GUI subclass turns into a signal.
"""

from __future__ import annotations

import uuid

from protoboard.controller.highlight_entry import HighlightEntry
from protoboard.controller.highlight_stack import HighlightStackBase
from protoboard.controller.interaction_mode import HighlightProfile


def _entry(label: str = "x") -> HighlightEntry:
    return HighlightEntry(
        comp_ids=frozenset({uuid.uuid4()}),
        wire_ids=frozenset(),
        net_id=None,
        profile=HighlightProfile(),
        label=label,
    )


class _RecordingStack(HighlightStackBase):
    """Base + a call counter in place of the GUI's Qt signal."""

    def __init__(self) -> None:
        super().__init__()
        self.changes = 0

    def _changed(self) -> None:
        self.changes += 1


def test_starts_empty() -> None:
    stack = HighlightStackBase()
    assert stack.active() is None
    assert stack.is_empty()
    assert not stack.has_persistent()


def test_persistent_beats_transient_beats_auto() -> None:
    stack = HighlightStackBase()
    stack.push_auto(_entry("auto"), source_id="s")
    stack.push_transient(_entry("transient"))
    stack.push(_entry("persistent"))
    active = stack.active()
    assert active is not None and active.label == "persistent"
    # Remove the persistent one; transient becomes active.
    stack.pop()
    active = stack.active()
    assert active is not None and active.label == "transient"
    # Remove the transient one; auto is the last tier standing.
    stack.pop_transient()
    active = stack.active()
    assert active is not None and active.label == "auto"


def test_auto_entries_are_keyed_by_source() -> None:
    stack = HighlightStackBase()
    stack.push_auto(_entry("a"), source_id="a")
    stack.push_auto(_entry("b"), source_id="b")
    # Pushing again from source "a" replaces its entry, not "b"'s.
    stack.push_auto(_entry("a2"), source_id="a")
    stack.clear_auto("a")
    active = stack.active()
    assert active is not None and active.label == "b"  # b survives


def test_clear_drops_persistent_keeps_other_tiers() -> None:
    stack = HighlightStackBase()
    stack.push_auto(_entry("auto"), source_id="s")
    stack.push_transient(_entry("transient"))
    stack.push(_entry("persistent"))
    stack.clear()
    assert not stack.has_persistent()
    active = stack.active()
    assert active is not None and active.label == "transient"


def test_changed_hook_fires_only_on_real_mutation() -> None:
    stack = _RecordingStack()
    stack.push(_entry())
    assert stack.changes == 1
    # A pop_transient with no transient entry present must NOT fire the hook.
    stack.pop_transient()
    assert stack.changes == 1
    # clear_auto for an absent source is a no-op too.
    stack.clear_auto("nope")
    assert stack.changes == 1
    # A real clear (there is a persistent entry) fires once.
    stack.clear()
    assert stack.changes == 2
