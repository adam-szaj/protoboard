# SPDX-License-Identifier: MIT
"""Tests for `PadStrategy` Protocol and its three concrete implementations.

Each strategy is exercised against a small Pad fixture covering the
common verdict transitions: same-net allow, foreign-net reject,
orphan inheritance, cross-layer T-junction gates, via on non-plated
pad, and the strict-Simplify additional rejections.
"""

from __future__ import annotations

import uuid

import pytest

from protoboard.domain.board.pad_state import (
    SHORT_CIRCUIT,
    BareWireItem,
    InsulatedWireCrossing,
    Pad,
)
from protoboard.domain.board.pads import PadLayers
from protoboard.geometry import Point
from protoboard.services.pad_strategy import (
    AutorouterPermissiveStrategy,
    Decision,
    DRCEnforcingStrategy,
    PadStrategy,
    SimplifyStrictStrategy,
    StrategyConfig,
    Verdict,
    make_strategy,
)


def _net() -> uuid.UUID:
    return uuid.uuid4()


def _plated_pad() -> Pad:
    return Pad(
        position=Point(0.5, 0.5),
        layers=PadLayers(top=True, bottom=True, plated_through=True),
    )


def _isolated_pad() -> Pad:
    return Pad(
        position=Point(1.5, 1.5),
        layers=PadLayers(top=True, bottom=True, plated_through=False),
    )


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_all_three_strategies_are_protocol_instances(self) -> None:
        for sub in ("drc", "autoroute", "simplify"):
            s = make_strategy(sub)  # type: ignore[arg-type]
            assert isinstance(s, PadStrategy)

    def test_unknown_subsystem_raises(self) -> None:
        with pytest.raises(ValueError):
            make_strategy("bogus")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# can_form_t_junction
# ---------------------------------------------------------------------------


class TestCanFormTJunction:
    def test_same_net_allows(self) -> None:
        s = DRCEnforcingStrategy()
        n = _net()
        d = s.can_form_t_junction(_plated_pad(), "top", n, n, cross_layer=False)
        assert d.verdict is Verdict.ALLOW

    def test_different_named_nets_rejects(self) -> None:
        s = DRCEnforcingStrategy()
        a, b = _net(), _net()
        d = s.can_form_t_junction(_plated_pad(), "top", a, b, cross_layer=False)
        assert d.verdict is Verdict.REJECT
        assert d.reason is not None and "short" in d.reason.lower()

    def test_orphan_incoming_inherits_through_net(self) -> None:
        s = DRCEnforcingStrategy()
        n = _net()
        d = s.can_form_t_junction(_plated_pad(), "top", n, None, cross_layer=False)
        assert d.verdict is Verdict.ASSIGN_NET
        assert d.net_to_assign == n

    def test_orphan_through_inherits_incoming_net(self) -> None:
        s = DRCEnforcingStrategy()
        n = _net()
        d = s.can_form_t_junction(_plated_pad(), "top", None, n, cross_layer=False)
        assert d.verdict is Verdict.ASSIGN_NET
        assert d.net_to_assign == n

    def test_orphan_orphan_allows(self) -> None:
        s = DRCEnforcingStrategy()
        d = s.can_form_t_junction(_plated_pad(), "top", None, None, cross_layer=False)
        assert d.verdict is Verdict.ALLOW

    def test_cross_layer_on_plated_allows(self) -> None:
        s = DRCEnforcingStrategy()
        n = _net()
        d = s.can_form_t_junction(_plated_pad(), "top", n, n, cross_layer=True)
        assert d.verdict is Verdict.ALLOW

    def test_cross_layer_on_isolated_rejects(self) -> None:
        s = DRCEnforcingStrategy()
        n = _net()
        d = s.can_form_t_junction(_isolated_pad(), "top", n, n, cross_layer=True)
        assert d.verdict is Verdict.REJECT
        assert d.reason is not None and "plated-through" in d.reason


# ---------------------------------------------------------------------------
# can_place_pin (rule 8)
# ---------------------------------------------------------------------------


class TestCanPlacePin:
    def test_empty_pad_allows(self) -> None:
        s = DRCEnforcingStrategy()
        d = s.can_place_pin(_plated_pad(), our_net=_net())
        assert d.verdict is Verdict.ALLOW

    def test_pad_with_same_net_wire_still_rejects(self) -> None:
        """Rule 6 strict: ANY wire on a pin's pad is rejected, even
        when the schematic nets match. The pin's leg physically
        obstructs any wire passing over the same pad — the user must
        route the wire to a separate hub.
        """
        s = DRCEnforcingStrategy()
        n = _net()
        pad = _plated_pad()
        pad._add_item(BareWireItem(id=uuid.uuid4(), net=n), "top")
        d = s.can_place_pin(pad, our_net=n)
        assert d.verdict is Verdict.REJECT
        assert d.reason is not None and "wire" in d.reason.lower()

    def test_pad_with_foreign_net_rejects(self) -> None:
        s = DRCEnforcingStrategy()
        a, b = _net(), _net()
        pad = _plated_pad()
        pad._add_item(BareWireItem(id=uuid.uuid4(), net=a), "top")
        d = s.can_place_pin(pad, our_net=b)
        assert d.verdict is Verdict.REJECT

    def test_shorted_pad_rejects(self) -> None:
        s = DRCEnforcingStrategy()
        a, b = _net(), _net()
        pad = _isolated_pad()
        pad._add_item(BareWireItem(id=uuid.uuid4(), net=a), "top")
        pad._add_item(BareWireItem(id=uuid.uuid4(), net=b), "top")
        assert pad.electrical_net_on("top") is SHORT_CIRCUIT
        d = s.can_place_pin(pad, our_net=_net())
        assert d.verdict is Verdict.REJECT

    def test_pin_checks_every_layer(self) -> None:
        # Isolated double-sided pad — foreign net on bottom must still
        # reject a pin even though we're asking from "top".
        s = DRCEnforcingStrategy()
        a, b = _net(), _net()
        pad = _isolated_pad()
        pad._add_item(BareWireItem(id=uuid.uuid4(), net=a), "bottom")
        d = s.can_place_pin(pad, our_net=b)
        assert d.verdict is Verdict.REJECT


# ---------------------------------------------------------------------------
# via_cost
# ---------------------------------------------------------------------------


class TestViaCost:
    def test_plated_pad_allows_with_cost(self) -> None:
        s = AutorouterPermissiveStrategy(StrategyConfig(via_cost=7.0))
        d = s.via_cost(_plated_pad(), "top", "bottom", our_net=_net())
        assert d.verdict is Verdict.ALLOW
        assert d.cost == 7.0

    def test_isolated_pad_rejects(self) -> None:
        s = AutorouterPermissiveStrategy()
        d = s.via_cost(_isolated_pad(), "top", "bottom", our_net=_net())
        assert d.verdict is Verdict.REJECT


# ---------------------------------------------------------------------------
# can_route_through / crossing_cost — autoroute permissive
# ---------------------------------------------------------------------------


class TestAutorouterPermissive:
    def test_free_pad_returns_baseline_cost(self) -> None:
        s = AutorouterPermissiveStrategy(StrategyConfig(crossing_cost=2.5))
        d = s.crossing_cost(_plated_pad(), "top", our_net=_net(), wire_type="bare")
        assert d.verdict is Verdict.ALLOW
        assert d.cost == 2.5

    def test_same_net_pad_is_free(self) -> None:
        s = AutorouterPermissiveStrategy()
        n = _net()
        pad = _plated_pad()
        pad._add_item(BareWireItem(id=uuid.uuid4(), net=n), "top")
        d = s.crossing_cost(pad, "top", our_net=n, wire_type="bare")
        assert d.verdict is Verdict.ALLOW
        assert d.cost == 0.0

    def test_foreign_net_pad_rejects(self) -> None:
        s = AutorouterPermissiveStrategy()
        a, b = _net(), _net()
        pad = _plated_pad()
        pad._add_item(BareWireItem(id=uuid.uuid4(), net=a), "top")
        d = s.crossing_cost(pad, "top", our_net=b, wire_type="bare")
        assert d.verdict is Verdict.REJECT


# ---------------------------------------------------------------------------
# SimplifyStrictStrategy — adds rejections on top of DRC
# ---------------------------------------------------------------------------


class TestSimplifyStrict:
    def test_drc_legal_bare_crossing_rejected_by_simplify(self) -> None:
        # Same-net bare crossing — DRC tolerates, Simplify rejects.
        n = _net()
        pad = _plated_pad()
        pad._add_item(BareWireItem(id=uuid.uuid4(), net=n), "top")

        drc = DRCEnforcingStrategy()
        # Simplify defaults are configured via from_session — direct
        # SimplifyStrictStrategy() picks up the permissive defaults of
        # StrategyConfig so tests must opt in explicitly.
        sm = SimplifyStrictStrategy(StrategyConfig.from_session(None, "simplify"))
        ar = AutorouterPermissiveStrategy()

        d_drc = drc.can_solder_wire_to_pad(
            pad, "top", n, "bare", is_endpoint=False,
        )
        d_ar = ar.can_solder_wire_to_pad(
            pad, "top", n, "bare", is_endpoint=False,
        )
        d_sm = sm.can_solder_wire_to_pad(
            pad, "top", n, "bare", is_endpoint=False,
        )
        assert d_drc.verdict is Verdict.ALLOW
        assert d_ar.verdict is Verdict.ALLOW
        assert d_sm.verdict is Verdict.REJECT

    def test_insulated_crossing_rejected_by_simplify(self) -> None:
        a, b = _net(), _net()
        pad = _plated_pad()
        pad._add_item(InsulatedWireCrossing(id=uuid.uuid4(), net=a), "top")
        sm = SimplifyStrictStrategy(StrategyConfig.from_session(None, "simplify"))
        d = sm.can_solder_wire_to_pad(
            pad, "top", b, "insulated", is_endpoint=False,
        )
        assert d.verdict is Verdict.REJECT


# ---------------------------------------------------------------------------
# StrategyConfig.from_session
# ---------------------------------------------------------------------------


class TestStrategyConfigFromSession:
    def test_none_session_yields_defaults(self) -> None:
        cfg = StrategyConfig.from_session(None, "autoroute")
        assert cfg.allow_bare_crossing_on_pad is True
        assert cfg.strict_tape_joints is False

    def test_simplify_subsystem_flips_crossings(self) -> None:
        cfg = StrategyConfig.from_session(None, "simplify")
        assert cfg.allow_bare_crossing_on_pad is False
        assert cfg.allow_insulated_crossing_on_pad is False

    def test_session_project_strict_tape_propagates(self) -> None:
        class FakeSettings:
            strict_tape_joints = True

        class FakeSession:
            settings = FakeSettings()

        cfg = StrategyConfig.from_session(FakeSession(), "drc")
        assert cfg.strict_tape_joints is True

    def test_overrides_win(self) -> None:
        cfg = StrategyConfig.from_session(
            None, "autoroute", crossing_cost=10.0, via_cost=20.0,
        )
        assert cfg.crossing_cost == 10.0
        assert cfg.via_cost == 20.0


# ---------------------------------------------------------------------------
# Decision factory helpers
# ---------------------------------------------------------------------------


class TestDecisionHelpers:
    def test_allow_default(self) -> None:
        d = Decision.allow()
        assert d.verdict is Verdict.ALLOW
        assert d.cost == 0.0
        assert d.reason is None

    def test_reject_sets_inf_cost(self) -> None:
        d = Decision.reject("boom")
        assert d.verdict is Verdict.REJECT
        assert d.cost == float("inf")
        assert d.reason == "boom"

    def test_assign_carries_net(self) -> None:
        n = _net()
        d = Decision.assign(n, reason="why")
        assert d.verdict is Verdict.ASSIGN_NET
        assert d.net_to_assign == n
        assert d.reason == "why"
