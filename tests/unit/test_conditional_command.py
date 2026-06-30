# SPDX-License-Identifier: MIT
"""Tests for the ConditionalCommand base class.

ConditionalCommand factors out the hand-rolled ``_applied`` guard shared by
the move/drag/transform commands: ``apply`` decides at runtime whether the
command committed anything (a locked or blocked move silently does nothing),
records that via ``mark_applied``, and ``revert`` is a no-op unless the
command really committed — in which case it delegates to ``_do_revert``.

These tests pin the base-class contract directly, independent of any concrete
command, so the behaviour survives future edits to the subclasses.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from protoboard.domain.commands.base import ConditionalCommand


@dataclass
class _Recorder(ConditionalCommand["list[str]"]):
    """Minimal concrete ConditionalCommand that logs into the state list."""

    commit: bool = True
    _reverted: bool = field(default=False, init=False, repr=False)

    def apply(self, state: list[str]) -> None:
        if not self.commit:
            self.mark_applied(False)
            return
        self.mark_applied(True)
        state.append("applied")

    def _do_revert(self, state: list[str]) -> None:
        self._reverted = True
        state.append("reverted")


def test_base_class_is_abstract() -> None:
    # _do_revert is abstract — the bare base cannot be instantiated.
    with pytest.raises(TypeError):
        ConditionalCommand()  # type: ignore[abstract]


def test_applied_defaults_false_before_apply() -> None:
    cmd = _Recorder()
    assert cmd._applied is False


def test_revert_is_noop_before_apply() -> None:
    cmd = _Recorder()
    state: list[str] = []
    cmd.revert(state)
    assert state == []
    assert cmd._reverted is False


def test_commit_then_revert_delegates_to_do_revert() -> None:
    cmd = _Recorder(commit=True)
    state: list[str] = []
    cmd.apply(state)
    assert cmd._applied is True
    assert state == ["applied"]

    cmd.revert(state)
    assert cmd._reverted is True
    assert state == ["applied", "reverted"]


def test_noop_apply_makes_revert_noop() -> None:
    # apply() declined to commit (mark_applied(False)) → revert must not
    # touch the state and must not call _do_revert.
    cmd = _Recorder(commit=False)
    state: list[str] = []
    cmd.apply(state)
    assert cmd._applied is False
    assert state == []

    cmd.revert(state)
    assert cmd._reverted is False
    assert state == []


def test_mark_applied_defaults_to_true() -> None:
    cmd = _Recorder()
    cmd.mark_applied()
    assert cmd._applied is True


def test_mark_applied_false_resets() -> None:
    cmd = _Recorder()
    cmd.mark_applied(True)
    assert cmd._applied is True
    cmd.mark_applied(False)
    assert cmd._applied is False
