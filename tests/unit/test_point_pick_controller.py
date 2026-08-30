# SPDX-License-Identifier: MIT
"""Tests for PointPickController — the pick-as-MouseInputController adapter.

Drives the controller directly (on_click / on_event / on_move with Point / Mods /
Intent, no QApplication), wrapping a real PickSession.  A fake resolver turns a
clicked point into a Selectable, so the mapping from the mouse-input surface onto
the session's feed / undo / confirm / cancel is exercised headlessly.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from protoboard.controller.selection.mouse_input import Intent, Mods
from protoboard.controller.selection.point_pick_controller import PointPickController
from protoboard.controller.selection.selectable import Selectable, Stop
from protoboard.controller.selection.service import PickSession
from protoboard.controller.selection_fsm import Hit
from protoboard.geometry import Point, Rect

if TYPE_CHECKING:
    from protoboard.controller.selection.selectable import ConnQuery
    from protoboard.types import NetId

_NO_MODS = Mods()


@dataclass(frozen=True)
class _FakeSel:
    """Minimal Selectable carrying an id, for assertions.

    Implements the full :class:`Selectable` protocol so it type-checks as one;
    only :meth:`identity` / :meth:`to_hit` are exercised by these tests, the
    rest are inert stubs that are never called.
    """

    n: int

    def identity(self) -> tuple[object, ...]:
        return ("fake", self.n)

    def to_hit(self) -> Hit:
        return Hit("component", uuid.uuid4())

    def bbox(self) -> Rect:
        return Rect.of(0.0, 0.0, 0.0, 0.0)

    def net_ids(self, conn: ConnQuery) -> frozenset[NetId]:
        return frozenset()

    def to_drag_member(self) -> object | None:
        return None


def _session(
    stop: Stop,
    *,
    done: list[list[Selectable]] | None = None,
    cancelled: list[bool] | None = None,
    progress: list[int] | None = None,
) -> PickSession:
    return PickSession(
        what="point",
        stop=stop,
        on_done=(lambda xs: done.append(xs)) if done is not None else (lambda xs: None),
        on_cancel=(lambda: cancelled.append(True)) if cancelled is not None else None,
        on_progress=(
            (lambda xs: progress.append(len(xs))) if progress is not None else None
        ),
    )


def _const_resolver(sel: Selectable | None):
    return lambda _pt, _mods: sel


class TestClickFeeds:
    def test_click_feeds_resolved_selectable(self) -> None:
        done: list[list[Selectable]] = []
        sess = _session(Stop.count(1), done=done)
        ctrl = PointPickController(sess, _const_resolver(_FakeSel(7)))
        assert ctrl.on_click(Point(1.0, 1.0), _NO_MODS) is True
        assert len(done) == 1
        assert done[0][0] == _FakeSel(7)
        assert ctrl.finished is True

    def test_missed_click_is_swallowed_not_fed(self) -> None:
        done: list[list[Selectable]] = []
        sess = _session(Stop.count(1), done=done)
        ctrl = PointPickController(sess, _const_resolver(None))  # resolver misses
        assert ctrl.on_click(Point(1.0, 1.0), _NO_MODS) is True  # consumed
        assert done == []  # nothing fed
        assert ctrl.finished is False

    def test_count_two_completes_on_second_click(self) -> None:
        done: list[list[Selectable]] = []
        sess = _session(Stop.count(2), done=done)
        ctrl = PointPickController(sess, _const_resolver(_FakeSel(1)))
        ctrl.on_click(Point(0, 0), _NO_MODS)
        assert ctrl.finished is False
        ctrl.on_click(Point(1, 0), _NO_MODS)
        assert ctrl.finished is True
        assert len(done[0]) == 2


class TestIntents:
    def test_confirm_ends_confirm_run(self) -> None:
        done: list[list[Selectable]] = []
        sess = _session(Stop.confirm(), done=done)
        ctrl = PointPickController(sess, _const_resolver(_FakeSel(1)))
        ctrl.on_click(Point(0, 0), _NO_MODS)
        ctrl.on_click(Point(1, 0), _NO_MODS)
        assert ctrl.finished is False  # confirm never auto-completes
        assert ctrl.on_event(Intent.CONFIRM) is True
        assert ctrl.finished is True
        assert len(done[0]) == 2

    def test_undo_drops_last_pick(self) -> None:
        progress: list[int] = []
        sess = _session(Stop.count_confirm(2), progress=progress)
        ctrl = PointPickController(sess, _const_resolver(_FakeSel(1)))
        ctrl.on_click(Point(0, 0), _NO_MODS)
        ctrl.on_click(Point(1, 0), _NO_MODS)  # at capacity (2/2)
        assert sess.at_capacity is True
        assert ctrl.on_event(Intent.UNDO) is True
        assert sess.at_capacity is False
        assert progress == [1, 2, 1]  # feed, feed, undo

    def test_cancel_aborts(self) -> None:
        cancelled: list[bool] = []
        sess = _session(Stop.confirm(), cancelled=cancelled)
        ctrl = PointPickController(sess, _const_resolver(_FakeSel(1)))
        ctrl.on_click(Point(0, 0), _NO_MODS)
        assert ctrl.on_event(Intent.CANCEL) is True
        assert cancelled == [True]
        assert ctrl.finished is True

    def test_event_after_finish_is_noop(self) -> None:
        sess = _session(Stop.count(1))
        ctrl = PointPickController(sess, _const_resolver(_FakeSel(1)))
        ctrl.on_click(Point(0, 0), _NO_MODS)  # completes the count(1) run
        assert ctrl.finished is True
        assert ctrl.on_event(Intent.CANCEL) is False  # already finished


class TestPreview:
    def test_move_drives_preview_seam(self) -> None:
        seen: list[tuple[float, float]] = []
        sess = _session(Stop.confirm())
        ctrl = PointPickController(
            sess,
            _const_resolver(_FakeSel(1)),
            on_preview=lambda pt, _mods: seen.append((pt.x, pt.y)),
        )
        ctrl.on_move(Point(2.0, 3.0), _NO_MODS)
        assert seen == [(2.0, 3.0)]

    def test_press_and_release_are_inert(self) -> None:
        done: list[list[Selectable]] = []
        sess = _session(Stop.count(1), done=done)
        ctrl = PointPickController(sess, _const_resolver(_FakeSel(1)))
        # A pick acts on the click, not raw press/release.
        assert ctrl.on_press(Point(0, 0), _NO_MODS) is True
        ctrl.on_release(Point(0, 0), _NO_MODS)
        assert done == []  # press/release alone fed nothing
        assert ctrl.finished is False


def test_is_structurally_a_mouse_input_controller() -> None:
    from protoboard.controller.selection.mouse_input import MouseInputController

    sess = _session(Stop.count(1))
    ctrl = PointPickController(sess, _const_resolver(_FakeSel(1)))
    assert isinstance(ctrl, MouseInputController)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
