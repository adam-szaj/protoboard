# SPDX-License-Identifier: MIT
"""KeyMap matcher invariants — focus on the shifted-symbol reconciliation.

The interesting case: ``?`` is bound bare (``"?"``) but reaches the matcher as
``Key_Question`` + ``ShiftModifier``.  These tests pin both the fix (``?``
matches despite the live Shift) and the non-regression (``Shift+R`` stays
distinct from ``R``, because for a *letter* Shift is additive).
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence

from protoboard.gui.keymap import GlobalKeyMap, KeyMap, _has_shift, _is_symbol_key

_SHIFT = Qt.KeyboardModifier.ShiftModifier
_NONE = Qt.KeyboardModifier.NoModifier


@pytest.fixture()
def km() -> KeyMap:
    return GlobalKeyMap.with_globals()


class TestShiftedSymbol:
    def test_shifted_question_matches_bare_binding(self, km: KeyMap) -> None:
        # The genuine ``?`` keystroke (Shift + Key_Question) must match.
        assert km.matches(
            int(Qt.Key.Key_Question), _SHIFT, "global.action.show_shortcuts"
        )

    def test_bare_question_also_matches(self, km: KeyMap) -> None:
        # A layout / synthetic event delivering Key_Question with no modifier.
        assert km.matches(
            int(Qt.Key.Key_Question), _NONE, "global.action.show_shortcuts"
        )


class TestLetterShiftStaysDistinct:
    def test_shift_r_matches_rotate_ccw(self, km: KeyMap) -> None:
        assert km.matches(int(Qt.Key.Key_R), _SHIFT, "global.action.rotate_ccw")

    def test_shift_r_does_not_match_bare_rotate_cw(self, km: KeyMap) -> None:
        # The whole point of the symbol-gate: Shift+R is NOT R.
        assert not km.matches(int(Qt.Key.Key_R), _SHIFT, "global.action.rotate_cw")

    def test_plain_r_matches_rotate_cw(self, km: KeyMap) -> None:
        assert km.matches(int(Qt.Key.Key_R), _NONE, "global.action.rotate_cw")

    def test_plain_r_does_not_match_rotate_ccw(self, km: KeyMap) -> None:
        assert not km.matches(int(Qt.Key.Key_R), _NONE, "global.action.rotate_ccw")


class TestHelpers:
    def test_is_symbol_key(self) -> None:
        assert _is_symbol_key(int(Qt.Key.Key_Question))
        assert not _is_symbol_key(int(Qt.Key.Key_R))
        assert not _is_symbol_key(int(Qt.Key.Key_0))

    def test_has_shift(self) -> None:
        assert not _has_shift(QKeySequence("?"))
        assert _has_shift(QKeySequence("Shift+R"))
