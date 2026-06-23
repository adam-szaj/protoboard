# SPDX-License-Identifier: MIT
"""Tests for the SelectionService pick API and Stop predicate (headless).

These exercise the active-pick machinery — the core that makes SelectTool a
service other tools/commands call (e.g. EditWire requesting wire-vertices then
confirm-terminated pad waypoints).  No Qt: a minimal fake Selectable supplies
just ``to_hit`` / ``identity``.
"""

from __future__ import annotations

import uuid
from typing import cast

import pytest  # pyright: ignore[reportMissingImports]  # resolvable; worktree lacks [tool.pyright] venv

from protoboard.gui.selection import Selectable, SelectionService, Stop
from protoboard.gui.selection_fsm import Hit


class _FakeSelectable:
    """Minimal Selectable stand-in — only the members the service touches."""

    def __init__(self, n: int) -> None:
        self._n = n
        self._id = uuid.UUID(int=n)

    def identity(self) -> tuple[object, ...]:
        return ("component", self._n)

    def to_hit(self) -> Hit:
        return Hit("component", self._id)


def _sel(n: int) -> Selectable:
    # ``_FakeSelectable`` is a deliberate partial stand-in (only ``identity`` /
    # ``to_hit`` — the members the pick machinery touches).  The service speaks
    # the full :class:`Selectable` protocol, so present the fake under that type.
    return cast(Selectable, _FakeSelectable(n))


def _wire(wire_id: uuid.UUID | None = None):  # type: ignore[no-untyped-def]
    """A minimal valid Wire (the domain requires a >=2-point path)."""
    from protoboard.domain.layout.wire import Wire
    from protoboard.geometry import Point

    return Wire(id=wire_id or uuid.uuid4(), path=[Point(0.0, 0.0), Point(1.0, 0.0)])


# ---------------------------------------------------------------------------
# Stop predicate
# ---------------------------------------------------------------------------


class TestStop:
    def test_count_satisfied_at_n(self) -> None:
        stop = Stop.count(2)
        assert stop.satisfied([]) is False
        assert stop.satisfied([_sel(1)]) is False
        assert stop.satisfied([_sel(1), _sel(2)]) is True

    def test_count_rejects_non_positive(self) -> None:
        with pytest.raises(ValueError):
            Stop.count(0)

    def test_confirm_never_auto_satisfied(self) -> None:
        stop = Stop.confirm()
        assert stop.satisfied([_sel(1), _sel(2), _sel(3)]) is False
        assert stop.user_confirmable is True

    def test_until_uses_predicate(self) -> None:
        stop = Stop.until(lambda xs: len(xs) == 3)
        assert stop.satisfied([_sel(1)]) is False
        assert stop.satisfied([_sel(1), _sel(2), _sel(3)]) is True
        assert stop.user_confirmable is False

    def test_count_confirm_caps_but_never_auto_satisfies(self) -> None:
        stop = Stop.count_confirm(2)
        assert stop.satisfied([_sel(1), _sel(2)]) is False  # user-driven
        assert stop.at_capacity([_sel(1), _sel(2)]) is True
        assert stop.at_capacity([_sel(1)]) is False
        assert stop.user_confirmable is True
        assert stop.revisable is True

    def test_count_confirm_rejects_non_positive(self) -> None:
        with pytest.raises(ValueError, match="count_confirm requires n >= 1"):
            Stop.count_confirm(0)


# ---------------------------------------------------------------------------
# request_pick (callback primitive)
# ---------------------------------------------------------------------------


class TestRequestPick:
    def test_count_completes_after_n_feeds(self) -> None:
        svc = SelectionService()
        done: list[list[Selectable]] = []
        session = svc.request_pick(
            "wire-vertex", Stop.count(2), on_done=lambda xs: done.append(xs)
        )
        assert svc.active_pick is session
        assert session.feed(_sel(1)) is False  # not done yet
        assert not done
        assert session.feed(_sel(2)) is True  # done on the 2nd
        assert len(done) == 1
        assert len(done[0]) == 2
        assert svc.active_pick is None  # cleared on completion

    def test_confirm_collects_then_user_ends(self) -> None:
        svc = SelectionService()
        done: list[list[Selectable]] = []
        progress: list[int] = []
        session = svc.request_pick(
            "pad",
            Stop.confirm(),
            on_done=lambda xs: done.append(xs),
            on_progress=lambda xs: progress.append(len(xs)),
        )
        session.feed(_sel(10))
        session.feed(_sel(11))
        session.feed(_sel(12))
        assert not done  # confirm never auto-completes
        assert progress == [1, 2, 3]  # on_progress per pick
        assert session.confirm() is True
        assert len(done) == 1
        assert len(done[0]) == 3
        assert svc.active_pick is None

    def test_confirm_noop_for_count_run(self) -> None:
        svc = SelectionService()
        session = svc.request_pick("pad", Stop.count(3), on_done=lambda xs: None)
        assert session.confirm() is False  # only confirm-runs end via confirm()
        assert session.finished is False

    def test_cancel_fires_on_cancel_and_discards(self) -> None:
        svc = SelectionService()
        cancelled: list[bool] = []
        session = svc.request_pick(
            "component",
            Stop.count(5),
            on_done=lambda xs: pytest.fail("on_done must not fire on cancel"),
            on_cancel=lambda: cancelled.append(True),
        )
        session.feed(_sel(20))
        session.cancel()
        assert cancelled == [True]
        assert session.finished is True
        assert svc.active_pick is None

    def test_until_predicate_terminates(self) -> None:
        svc = SelectionService()
        done: list[list[Selectable]] = []
        session = svc.request_pick(
            "pad",
            Stop.until(lambda xs: len(xs) == 2),
            on_done=lambda xs: done.append(xs),
        )
        assert session.feed(_sel(1)) is False
        assert session.feed(_sel(2)) is True
        assert len(done[0]) == 2

    def test_starting_a_new_pick_cancels_the_old(self) -> None:
        svc = SelectionService()
        old_cancelled: list[bool] = []
        old = svc.request_pick(
            "pad",
            Stop.confirm(),
            on_done=lambda xs: None,
            on_cancel=lambda: old_cancelled.append(True),
        )
        new = svc.request_pick("component", Stop.count(1), on_done=lambda xs: None)
        assert old.finished is True
        assert old_cancelled == [True]
        assert svc.active_pick is new

    def test_feed_after_finish_is_idempotent(self) -> None:
        svc = SelectionService()
        done: list[list[Selectable]] = []
        session = svc.request_pick(
            "wire-vertex", Stop.count(1), on_done=lambda xs: done.append(xs)
        )
        assert session.feed(_sel(1)) is True
        # Further feeds after completion are no-ops, don't re-fire on_done.
        assert session.feed(_sel(2)) is True
        assert len(done) == 1


class TestCorrectionPhase:
    """Stop.count_confirm + PickSession.undo — revise picks before finalizing."""

    def test_count_confirm_does_not_auto_complete(self) -> None:
        svc = SelectionService()
        done: list[list[Selectable]] = []
        session = svc.request_pick(
            "pad", Stop.count_confirm(2), on_done=lambda xs: done.append(xs)
        )
        assert session.feed(_sel(1)) is False
        assert session.feed(_sel(2)) is False  # at count, but NOT auto-done
        assert not done
        assert session.at_capacity is True

    def test_count_confirm_caps_collection(self) -> None:
        svc = SelectionService()
        session = svc.request_pick(
            "pad", Stop.count_confirm(2), on_done=lambda xs: None
        )
        session.feed(_sel(1))
        session.feed(_sel(2))
        # A 3rd click while at capacity is ignored (no growth past n).
        assert session.feed(_sel(3)) is False
        assert len(session.collected) == 2

    def test_undo_drops_last_then_accepts_again(self) -> None:
        svc = SelectionService()
        progress: list[int] = []
        session = svc.request_pick(
            "pad",
            Stop.count_confirm(2),
            on_done=lambda xs: None,
            on_progress=lambda xs: progress.append(len(xs)),
        )
        session.feed(_sel(1))
        session.feed(_sel(2))
        assert session.at_capacity is True
        assert session.undo() is True  # drop the 2nd
        assert session.at_capacity is False
        assert len(session.collected) == 1
        # Capacity freed → a fresh pick is accepted again.
        assert session.feed(_sel(9)) is False
        assert len(session.collected) == 2
        assert progress == [1, 2, 1, 2]  # fires on each feed AND each undo

    def test_confirm_finalizes_count_confirm(self) -> None:
        svc = SelectionService()
        done: list[list[Selectable]] = []
        session = svc.request_pick(
            "pad", Stop.count_confirm(2), on_done=lambda xs: done.append(xs)
        )
        session.feed(_sel(1))
        session.feed(_sel(2))
        assert session.confirm() is True  # Enter finalizes
        assert len(done) == 1
        assert len(done[0]) == 2
        assert svc.active_pick is None

    def test_undo_noop_for_plain_count(self) -> None:
        svc = SelectionService()
        session = svc.request_pick(
            "pad", Stop.count(3), on_done=lambda xs: None
        )
        session.feed(_sel(1))
        # Plain count is not revisable — undo is a no-op.
        assert session.undo() is False
        assert len(session.collected) == 1


# ---------------------------------------------------------------------------
# request_pick_async (Future façade)
# ---------------------------------------------------------------------------


class TestRequestPickAsync:
    def test_future_resolves_with_picked_items(self) -> None:
        svc = SelectionService()
        future = svc.request_pick_async("wire-vertex", Stop.count(2))
        assert not future.done()
        assert svc.active_pick is not None
        svc.active_pick.feed(_sel(1))
        assert not future.done()
        svc.active_pick.feed(_sel(2))
        assert future.done()
        assert len(future.result()) == 2

    def test_future_cancelled_on_user_cancel(self) -> None:
        svc = SelectionService()
        future = svc.request_pick_async("pad", Stop.confirm())
        assert svc.active_pick is not None
        svc.active_pick.cancel()
        assert future.cancelled()


# ---------------------------------------------------------------------------
# Selectable adapters — identity + to_hit (the Hit.kind switch, replaced)
# ---------------------------------------------------------------------------


class TestAdapters:
    def test_component_adapter(self) -> None:
        from protoboard.domain.layout.placement import ComponentPlacement
        from protoboard.geometry import Point
        from protoboard.gui.selection import ComponentSelectable

        cid = uuid.uuid4()
        pl = ComponentPlacement(
            component_id=cid, position=Point(1.0, 2.0), rotation=0, side="top"
        )
        sel = ComponentSelectable(component_id=cid, placement=pl, pin_number=3)
        assert sel.identity() == ("component", cid)
        hit = sel.to_hit()
        assert hit.kind == "component"
        assert hit.id == cid
        assert hit.pin_number == 3

    def test_wire_adapter(self) -> None:
        from protoboard.gui.selection import WireSelectable

        wid = uuid.uuid4()
        sel = WireSelectable(wire=_wire(wid))
        assert sel.identity() == ("wire", wid)
        assert sel.to_hit() == Hit("wire", wid)

    def test_group_adapter(self) -> None:
        from protoboard.domain.layout.group import ComponentGroup
        from protoboard.gui.selection import GroupSelectable

        gid = uuid.uuid4()
        sel = GroupSelectable(group=ComponentGroup(id=gid, name="G"))
        assert sel.identity() == ("group", gid)
        assert sel.to_hit() == Hit("group", gid)

    def test_pin_adapter_bridges_to_component_hit(self) -> None:
        from protoboard.domain.schematic.net import PinRef
        from protoboard.gui.selection import PinSelectable

        cid = uuid.uuid4()
        sel = PinSelectable(pin=PinRef(component_id=cid, pin_number=2))
        assert sel.identity() == ("pin", cid, 2)
        # Pin picks bridge to a pin-scoped component Hit (the FSM has no pin kind).
        assert sel.to_hit() == Hit("component", cid, pin_number=2)

    def test_wire_vertex_adapter_bridges_to_wire_hit(self) -> None:
        from protoboard.gui.selection import WireVertexSelectable

        wid = uuid.uuid4()
        sel = WireVertexSelectable(wire_id=wid, index=1)
        assert sel.identity() == ("wire-vertex", wid, 1)
        # Vertex picks bridge to the parent-wire Hit (index lives in identity).
        assert sel.to_hit() == Hit("wire", wid)

    def test_adapters_are_structurally_selectable(self) -> None:
        from protoboard.gui.selection import Selectable, WireSelectable

        # @runtime_checkable: the adapter exposes the full protocol surface.
        assert isinstance(WireSelectable(wire=_wire()), Selectable)


class TestServiceWithAdapters:
    def test_set_then_selected_returns_adapter(self) -> None:
        from protoboard.gui.selection import WireSelectable

        svc = SelectionService()
        wire_sel = WireSelectable(wire=_wire())
        svc.set(wire_sel)
        selected = svc.selected()
        assert len(selected) == 1
        assert selected[0].identity() == wire_sel.identity()
        assert svc.is_empty() is False

    def test_clear_empties_selection(self) -> None:
        from protoboard.gui.selection import WireSelectable

        svc = SelectionService()
        svc.set(WireSelectable(wire=_wire()))
        svc.clear()
        assert svc.is_empty() is True
