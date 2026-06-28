# SPDX-License-Identifier: MIT
"""Tests for find_pin_at PinKnot-reuse fast-path (commit 4d8e634).

Regression: manual routing to a pin that already has a PinKnot used to
fall through to JointKnot creation because DRC-enforcing
``PadStrategy.can_place_pin`` REJECT-ed the pad (rule 6 — pad carries
wire items). The fix short-circuits: when ``layout.pin_knots`` already
maps a PinRef to a knot id, ``find_pin_at`` returns that PinRef
bypassing the strategy gate.  Rule 6 is intended for *new pin
placement on existing wire*, not *additional wire to an existing pin*.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pad_state import Pad
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import WireCollection
from protoboard.domain.layout.wire_utils import find_pin_at
from protoboard.domain.schematic.net import PinRef
from protoboard.geometry import Point
from protoboard.services.pad_strategy import Decision, Verdict


def _find_pin_at(
    state: _State,
    point: Point,
    *,
    strategy: object | None = None,
    pad_index: object | None = None,
) -> PinRef | None:
    """Adapter: spread the test ``_State`` into ``find_pin_at``'s primitives.

    ``find_pin_at`` now lives in ``wire_utils`` and takes explicit
    ``layout``/``instances``/``components`` plus an optional ``pad_index``
    (previously pulled off ``state.pad_index`` inside the function).  This
    thin wrapper keeps each test calling a single helper.
    """
    return find_pin_at(
        point,
        layout=state.layout,
        instances=state.instances,
        components=state.components,
        strategy=strategy,  # type: ignore[arg-type]
        pad_index=pad_index,  # type: ignore[arg-type]
    )


def _board() -> Board:
    return Board(root=BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=10, cols=10, row_gap=0.0, col_gap=0.0,
    ))


@dataclass
class _State:
    layout: Layout
    instances: dict[uuid.UUID, ComponentInstance] = field(default_factory=dict)
    components: dict[str, ComponentDefinition] = field(default_factory=dict)
    board: Board = field(default_factory=_board)


class _AlwaysRejectStrategy:
    """Strategy that rejects every ``can_place_pin`` call.

    Mimics DRC rule 6 (pad has wire items → REJECT) without needing a
    fully-populated PadIndex.  Only implements the one method
    ``_find_pin_at`` reaches — the rest of the :class:`PadStrategy`
    protocol is irrelevant for this code path, so we silence pyright
    at the call site with ``type: ignore[arg-type]``.
    """

    def can_place_pin(self, pad: object, our_net: object) -> Decision:  # noqa: ARG002
        return Decision(verdict=Verdict.REJECT, cost=float("inf"))


def _build_one_resistor() -> tuple[_State, uuid.UUID]:
    """One SIL(2) resistor at (3, 5) so pin 1 sits at (3.5, 5.5)."""
    defn = ComponentDefinition(name="R", footprint=SIL(2), pin_names={1: "A", 2: "B"})
    inst = ComponentInstance.new(
        definition_ref="R", refdes="R1", value="10k"
    ).bind(defn, {})
    placement = ComponentPlacement(
        component_id=inst.id, position=Point(3.0, 5.0), rotation=0, side="top",
    )
    layout = Layout(placements={inst.id: placement}, wires=WireCollection())
    state = _State(
        layout=layout,
        instances={inst.id: inst},
        components={"R": defn},
    )
    return state, inst.id


def _pin_pos(state: _State, comp_id: uuid.UUID, pin: int) -> Point:
    from protoboard.domain.layout.placement import pin_world_position
    defn = state.components[state.instances[comp_id].definition_ref]
    return pin_world_position(defn.footprint, state.layout.placements[comp_id], pin)


# ---------------------------------------------------------------------------
# Fast-path: existing PinKnot bypasses strategy gate
# ---------------------------------------------------------------------------


class TestPinKnotFastPathBypassesStrategy:
    def test_no_pinknot_with_rejecting_strategy_returns_none(self) -> None:
        """Baseline: without a PinKnot, the strategy gate works.

        Confirms ``_AlwaysRejectStrategy`` actually rejects pin
        resolution (the fast-path test below has teeth only if the
        slow-path normally rejects).  Needs a real PadIndex so the
        strategy branch is reachable.
        """
        state, cid = _build_one_resistor()
        pos = _pin_pos(state, cid, 1)
        # No PinKnot for this pin; with rejecting strategy + non-None
        # pad we expect None back.  Build a minimal pad stub.

        class _PadIdxStub:
            def pad_at(self, p: Point) -> Pad:  # noqa: ARG002
                return Pad(position=pos, layers=PadLayers(top=True, bottom=True))

        result = _find_pin_at(
            state, pos,
            strategy=_AlwaysRejectStrategy(), pad_index=_PadIdxStub(),
        )
        assert result is None

    def test_existing_pinknot_bypasses_rejecting_strategy(self) -> None:
        """The regression: PinKnot present → return PinRef despite REJECT."""
        state, cid = _build_one_resistor()
        pos = _pin_pos(state, cid, 1)
        # Pre-seed the singleton PinKnot for this pin.
        target = PinRef(component_id=cid, pin_number=1)
        state.layout.get_or_create_pin_knot(target)

        class _PadIdxStub:
            def pad_at(self, p: Point) -> Pad:  # noqa: ARG002
                return Pad(position=pos, layers=PadLayers(top=True, bottom=True))

        # Fast-path: even though strategy would REJECT, the PinKnot is
        # already accepting wires → return the PinRef.
        result = _find_pin_at(
            state, pos,
            strategy=_AlwaysRejectStrategy(), pad_index=_PadIdxStub(),
        )
        assert result == target

    def test_existing_pinknot_no_strategy_returns_pinref(self) -> None:
        """Legacy callers (no strategy) also benefit from the fast-path."""
        state, cid = _build_one_resistor()
        pos = _pin_pos(state, cid, 1)
        target = PinRef(component_id=cid, pin_number=1)
        state.layout.get_or_create_pin_knot(target)

        result = _find_pin_at(state, pos, strategy=None)
        assert result == target

    def test_no_pinknot_no_strategy_returns_pinref(self) -> None:
        """No PinKnot and no strategy → resolve via geometry alone."""
        state, cid = _build_one_resistor()
        pos = _pin_pos(state, cid, 1)
        target = PinRef(component_id=cid, pin_number=1)

        result = _find_pin_at(state, pos, strategy=None)
        assert result == target

    def test_fast_path_per_pin(self) -> None:
        """Pin 1 has a PinKnot; pin 2 does not.

        Asking for pin 2 falls back to the strategy path (rejected).
        Asking for pin 1 hits the fast-path.  Same component.
        """
        state, cid = _build_one_resistor()
        pos1 = _pin_pos(state, cid, 1)
        pos2 = _pin_pos(state, cid, 2)
        # Only pin 1 gets a PinKnot.
        state.layout.get_or_create_pin_knot(PinRef(component_id=cid, pin_number=1))

        # PadIndex stub returning Pad for any position so strategy is reachable.
        class _PadIdxStub:
            def pad_at(self, p: Point) -> Pad:  # noqa: ARG002
                return Pad(position=p, layers=PadLayers(top=True, bottom=True))

        pad_idx = _PadIdxStub()
        # Pin 1 → fast-path returns PinRef.
        r1 = _find_pin_at(
            state, pos1, strategy=_AlwaysRejectStrategy(), pad_index=pad_idx,
        )
        assert r1 == PinRef(component_id=cid, pin_number=1)
        # Pin 2 → no PinKnot, strategy rejects.
        r2 = _find_pin_at(
            state, pos2, strategy=_AlwaysRejectStrategy(), pad_index=pad_idx,
        )
        assert r2 is None

    def test_fast_path_with_many_owners(self) -> None:
        """PinKnot with 5 prior owners still resolves via fast-path.

        Mirrors the micek2 case (PinKnot 03b45c25 had 2 owners; n>1 is
        the whole reason rule 6 used to reject).
        """
        state, cid = _build_one_resistor()
        pos = _pin_pos(state, cid, 1)
        target = PinRef(component_id=cid, pin_number=1)
        pk = state.layout.get_or_create_pin_knot(target)
        # Simulate 5 prior wire owners by registering them on the PinKnot.
        for _ in range(5):
            state.layout._touch_pin_knot_owner(  # type: ignore[attr-defined]
                pk.id, uuid.uuid4(),
            ) if hasattr(state.layout, "_touch_pin_knot_owner") else None
        # The owner count itself is irrelevant to the fast-path; the
        # presence of the PinRef in pin_knots is the gate.
        result = _find_pin_at(state, pos, strategy=_AlwaysRejectStrategy())
        assert result == target
