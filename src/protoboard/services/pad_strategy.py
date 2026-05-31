# SPDX-License-Identifier: MIT
"""Pad strategy interface — pluggable rules layer above ``PadIndex``.

Algorithms (DRC, autorouter, simplify, drag-conflict, T-junction gates)
delegate pad-related verdicts to a :class:`PadStrategy` instead of
inspecting pad internals directly. This lets the same physical state
be interpreted by different policies: a permissive autorouter, a
strict simplify tool, an enforcing DRC engine.

Three concrete strategies are bundled:

* :class:`DRCEnforcingStrategy` — hard rules 0–9 from
  ``docs/pad-pin-wire-junction-semantics.md``; verdict reasons populated
  for DRC messages.
* :class:`AutorouterPermissiveStrategy` — same-net bare overlap is
  free; foreign-net contact returns ``inf`` cost rather than REJECT so
  A* sees it as "very expensive" and can still backtrack to a legal
  alternative.
* :class:`SimplifyStrictStrategy` — rejects bare/insulated crossings on
  pads even when DRC would only flag them as preferable-to-avoid
  (soft rule strict for simplify).

Strategies are constructed from :class:`StrategyConfig` which composes
project-level knobs (``EditorSession.settings``) with per-subsystem
overrides (``AutorouteParams``, ``SimplifyParams``, future
``DRCParams``). See :meth:`StrategyConfig.from_session`.

This module deliberately has no dependency on ``protoboard.gui`` —
algorithms in the domain layer can import it without dragging in Qt.
The session adapter lives in ``gui/session.py``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from protoboard.domain.board.layer import LayerId
from protoboard.domain.board.pad_state import (
    SHORT_CIRCUIT,
    ComponentBody,
    InsulatedWireCrossing,
    InsulatedWireItem,
    Pad,
)

if TYPE_CHECKING:
    pass

NetId = uuid.UUID
WireType = Literal["bare", "insulated"]


# ---------------------------------------------------------------------------
# Decision objects
# ---------------------------------------------------------------------------


class Verdict(Enum):
    """Outcome of a strategy call.

    ``ALLOW`` — operation may proceed unchanged.
    ``REJECT`` — operation must not proceed (DRC error / hard block).
    ``ASSIGN_NET`` — operation may proceed, but the caller must first
        propagate the resolved net to the receiving side (orphan
        endpoint inheriting a named net from its mate).
    """

    ALLOW = "allow"
    REJECT = "reject"
    ASSIGN_NET = "assign_net"


@dataclass(frozen=True, slots=True)
class Decision:
    """One strategy verdict + supporting fields.

    Single return type for every strategy method so DRC, autoroute, and
    simplify can consume verdicts uniformly. DRC reads ``reason`` to
    populate the user-facing message; autoroute reads ``cost``; both
    branch on ``verdict``.

    ``net_to_assign`` is set when ``verdict is ASSIGN_NET`` — the
    caller must mutate the orphan endpoint's schematic net to this
    value before proceeding (parallels the existing
    ``TJunctionVerdict.ASSIGN_NET_TO_INCOMING_PIN`` flow in
    ``wire_utils.can_form_t_junction``).
    """

    verdict: Verdict
    reason: str | None = None
    cost: float = 0.0
    net_to_assign: NetId | None = None

    @classmethod
    def allow(cls, cost: float = 0.0) -> "Decision":
        return cls(Verdict.ALLOW, cost=cost)

    @classmethod
    def reject(cls, reason: str) -> "Decision":
        return cls(Verdict.REJECT, reason=reason, cost=float("inf"))

    @classmethod
    def assign(cls, net: NetId, reason: str | None = None) -> "Decision":
        return cls(Verdict.ASSIGN_NET, reason=reason, net_to_assign=net)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


SubsystemName = Literal["drc", "autoroute", "simplify"]


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    """Composable knobs that shape a :class:`PadStrategy` instance.

    Defaults match the **permissive** policy (autoroute baseline) so a
    bare instantiation ``StrategyConfig()`` produces something
    autoroute-shaped. DRC and simplify override their knobs via
    :meth:`from_session` or by passing explicit kwargs.

    Project-level knobs (currently just ``strict_tape_joints`` —
    placeholder for the kapton follow-up) live on ``EditorSession`` so
    every subsystem sees them; per-subsystem knobs live on the
    autoroute/simplify params dataclass and are merged into the
    strategy config at construction time.
    """

    # ------------------------------------------------------------------
    # Project-level
    # ------------------------------------------------------------------

    strict_tape_joints: bool = False
    """When True, T-joints on a kapton-taped layer become DRC errors.
    Placeholder — kapton is out of MVP scope; reads default to False."""

    # ------------------------------------------------------------------
    # Per-subsystem behaviour
    # ------------------------------------------------------------------

    allow_bare_crossing_on_pad: bool = True
    """Whether a bare wire vertex may share a pad with a bare wire of
    the same net. Autoroute / DRC: True (same-net reuse is legal).
    Simplify: False (the soft rule "avoid crossings on pads" is
    enforced strictly)."""

    allow_insulated_crossing_on_pad: bool = True
    """Whether an insulated wire may cross a pad already carrying
    another insulated wire of a different net. Autoroute: True
    (returns a cost penalty). DRC: True (no electrical short).
    Simplify: False."""

    foreign_net_via_orphan: Literal["allow", "reject"] = "reject"
    """Policy for orphan wires joining a named-net joint. ``reject``
    matches the post-2026-05-30 net gate in ``_find_joint_at``."""

    # ------------------------------------------------------------------
    # Cost knobs (autoroute)
    # ------------------------------------------------------------------

    crossing_cost: float = 1.0
    """Per-step penalty added when a wire vertex lands on an otherwise
    free pad (the baseline step cost). Autoroute uses this as
    ``cost_model.free_pad``."""

    via_cost: float = 5.0
    """Per-via penalty added when a layer-change lands on a pad.
    Autoroute uses this as ``cost_model.layer_change``."""

    component_body_cost: float = float("inf")
    """Hard block — a ``ComponentBody`` blocker always rejects."""

    @classmethod
    def from_session(
        cls,
        session: "object",
        subsystem: SubsystemName,
        **overrides: float | bool | str,
    ) -> "StrategyConfig":
        """Compose a config from a session's project settings plus the
        subsystem's defaults.

        The session is intentionally typed as ``object`` so this module
        stays independent of ``protoboard.gui``. Accessed attribute is
        ``settings.strict_tape_joints`` (falsy default tolerated).

        ``subsystem`` selects per-subsystem defaults:

        * ``"drc"``        — `foreign_net_via_orphan="reject"`, costs unused
        * ``"autoroute"`` — `allow_*_crossing_on_pad=True`,
                           `foreign_net_via_orphan="reject"`
        * ``"simplify"``   — `allow_*_crossing_on_pad=False`

        ``overrides`` win over both project- and subsystem-level
        defaults — useful for unit tests and per-tool tweaks.
        """
        # Project-level — read defensively so EditorSession with no
        # ``settings`` attribute (legacy / unit-test sessions) works.
        proj_settings = getattr(session, "settings", None) if session else None
        strict_tape = bool(
            getattr(proj_settings, "strict_tape_joints", False)
        )

        if subsystem == "drc":
            defaults: dict[str, float | bool | str] = dict(
                strict_tape_joints=strict_tape,
                allow_bare_crossing_on_pad=True,
                allow_insulated_crossing_on_pad=True,
                foreign_net_via_orphan="reject",
            )
        elif subsystem == "autoroute":
            defaults = dict(
                strict_tape_joints=strict_tape,
                allow_bare_crossing_on_pad=True,
                allow_insulated_crossing_on_pad=True,
                foreign_net_via_orphan="reject",
            )
        elif subsystem == "simplify":
            defaults = dict(
                strict_tape_joints=strict_tape,
                allow_bare_crossing_on_pad=False,
                allow_insulated_crossing_on_pad=False,
                foreign_net_via_orphan="reject",
            )
        else:
            raise ValueError(f"unknown subsystem: {subsystem!r}")

        defaults.update(overrides)
        return cls(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Strategy protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class PadStrategy(Protocol):
    """The contract every concrete strategy implements.

    Methods accept :class:`Pad` instances (the runtime occupancy view
    from :class:`PadIndex`) rather than the static :class:`PadLayers`
    so the strategy can reason about *current* occupants — pins,
    wires, joints — not just the board's static structure.

    Every method returns a :class:`Decision`. ``cost`` is meaningful
    only for the cost methods; the predicate methods set ``cost=0``
    when ALLOW and ``cost=inf`` when REJECT (mirror of
    ``Decision.reject``) so autoroute can uniformly sum them.
    """

    def can_solder_wire_to_pad(
        self,
        pad: Pad,
        layer: LayerId,
        our_net: NetId | None,
        wire_type: WireType,
        is_endpoint: bool,
    ) -> Decision:
        """May a wire vertex of *wire_type* on *layer* contact *pad*?

        ``is_endpoint=True`` for the wire's first / last vertex (where
        a solder joint forms); ``False`` for an interior crossing.
        """
        ...

    def can_route_through(
        self,
        pad: Pad,
        layer: LayerId,
        our_net: NetId | None,
        wire_type: WireType,
    ) -> Decision:
        """May a wire of *wire_type* pass over *pad* on *layer*?

        Differs from :meth:`can_solder_wire_to_pad` semantically only
        for insulated wires: an insulated interior may cross an
        otherwise-occupied pad (the wire floats above the copper) but
        an insulated endpoint contacts the solder joint.
        """
        ...

    def can_form_t_junction(
        self,
        pad: Pad,
        layer: LayerId,
        through_wire_net: NetId | None,
        incoming_wire_net: NetId | None,
        cross_layer: bool,
    ) -> Decision:
        """May the wire endpoint join an existing wire's interior at
        *pad*, forming a T-junction (potentially a via when
        ``cross_layer=True``)?"""
        ...

    def can_place_pin(
        self,
        pad: Pad,
        our_net: NetId | None,
    ) -> Decision:
        """May a pin of net *our_net* (or net-less) be soldered into
        *pad*? Rule 8 — paralleling :meth:`can_form_t_junction` for pin
        endpoints. Currently the only callsite is ``_find_pin_at``."""
        ...

    def crossing_cost(
        self,
        pad: Pad,
        layer: LayerId,
        our_net: NetId | None,
        wire_type: WireType,
    ) -> Decision:
        """Cost A* should charge for passing through *pad* on *layer*.

        ``Decision.cost`` is the additive penalty; ``Decision.verdict``
        is ``ALLOW`` for finite costs, ``REJECT`` (cost=inf) for hard
        blocks. ``ASSIGN_NET`` is never returned here — cost queries
        don't mutate."""
        ...

    def via_cost(
        self,
        pad: Pad,
        from_layer: LayerId,
        to_layer: LayerId,
        our_net: NetId | None,
    ) -> Decision:
        """Cost of placing a via at *pad* changing layers.

        Plated-through pads are the only legal via locations (rule 3).
        Off-plated → ``REJECT``. On plated, return a per-strategy via
        cost so autoroute can balance vias vs longer paths.
        """
        ...

    def is_half_orphan_destination(
        self,
        pad: Pad,
        layer: LayerId,
        our_net: NetId | None,
    ) -> Decision:
        """May a wire of *our_net* terminate at *pad*'s existing
        half-orphan endpoint? Rule 9 — autorouter consumes half-orphan
        wires as candidate destinations. MVP returns ALLOW everywhere
        with cost=0; the autorouter will gate by its own target
        scoring."""
        ...


# ---------------------------------------------------------------------------
# Concrete strategies
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _BaseStrategy:
    """Shared verdict mechanics for the three concrete strategies."""

    config: StrategyConfig = field(default_factory=StrategyConfig)

    # Shared helpers ----------------------------------------------------

    @staticmethod
    def _pad_net(pad: Pad, layer: LayerId) -> NetId | None | object:
        return pad.electrical_net_on(layer)

    def _is_pad_blocked_by_body(self, pad: Pad, layer: LayerId) -> bool:
        return any(
            isinstance(it, ComponentBody)
            for it in pad.physically_blocked_on(layer)
        )

    def _insulated_conflict(
        self,
        pad: Pad,
        layer: LayerId,
        our_net: NetId | None,
    ) -> bool:
        """True if an insulated wire of *our_net* would tangle with an
        existing insulated occupant of a different net on this layer."""
        for it in pad.physically_blocked_on(layer):
            if isinstance(it, (InsulatedWireItem, InsulatedWireCrossing)):
                if our_net is None or it.net != our_net:
                    return True
        return False


@dataclass(frozen=True, slots=True)
class DRCEnforcingStrategy(_BaseStrategy):
    """Enforces rules 0–9 strictly. Reason strings populated for DRC
    messages. Cost knobs are ignored (DRC doesn't sum costs).
    """

    def can_solder_wire_to_pad(
        self,
        pad: Pad,
        layer: LayerId,
        our_net: NetId | None,
        wire_type: WireType,
        is_endpoint: bool,
    ) -> Decision:
        if self._is_pad_blocked_by_body(pad, layer):
            return Decision.reject(
                f"pad at {pad.position} on {layer} is blocked by a "
                f"component body"
            )
        net = self._pad_net(pad, layer)
        if net is SHORT_CIRCUIT:
            return Decision.reject(
                f"pad at {pad.position} on {layer} is already shorted"
            )
        if wire_type == "bare":
            if net is not None and our_net is not None and net != our_net:
                return Decision.reject(
                    f"bare wire of net {our_net} would short with net "
                    f"{net} at {pad.position} ({layer})"
                )
        else:  # insulated
            # Insulated endpoint = solder contact (treated as bare for
            # net conflicts). Insulated interior is a physical-only
            # blocker, no net conflict.
            if is_endpoint and net is not None and our_net is not None \
                    and net != our_net:
                return Decision.reject(
                    f"insulated wire endpoint of net {our_net} would "
                    f"short with net {net} at {pad.position} ({layer})"
                )
            if self._insulated_conflict(pad, layer, our_net):
                return Decision.reject(
                    f"insulated wire of net {our_net} would tangle "
                    f"with another insulated wire at {pad.position} "
                    f"({layer})"
                )
        return Decision.allow()

    def can_route_through(
        self,
        pad: Pad,
        layer: LayerId,
        our_net: NetId | None,
        wire_type: WireType,
    ) -> Decision:
        # "Route through" = interior segment crossing. Endpoint
        # semantics flow through can_solder_wire_to_pad instead.
        return self.can_solder_wire_to_pad(
            pad, layer, our_net, wire_type, is_endpoint=False,
        )

    def can_form_t_junction(
        self,
        pad: Pad,
        layer: LayerId,
        through_wire_net: NetId | None,
        incoming_wire_net: NetId | None,
        cross_layer: bool,
    ) -> Decision:
        if cross_layer and not pad.layers.plated_through:
            return Decision.reject(
                f"cross-layer T-junction at {pad.position} requires "
                f"a plated-through pad"
            )
        if through_wire_net is None and incoming_wire_net is None:
            return Decision.allow()  # orphan-orphan, no named bridge
        if through_wire_net is None:
            # Through is orphan, incoming has net → inherit
            assert incoming_wire_net is not None
            return Decision.assign(
                incoming_wire_net,
                reason="through-wire inherits net from incoming",
            )
        if incoming_wire_net is None:
            # Incoming is orphan, through has net → inherit
            return Decision.assign(
                through_wire_net,
                reason="incoming wire inherits net from through",
            )
        if through_wire_net == incoming_wire_net:
            return Decision.allow()
        return Decision.reject(
            f"T-junction would short nets {through_wire_net} and "
            f"{incoming_wire_net} at {pad.position}"
        )

    def can_place_pin(
        self,
        pad: Pad,
        our_net: NetId | None,
    ) -> Decision:
        # A pin's electrical contribution lands on every physical
        # layer of the board. If any layer already carries a
        # different net, that's an immediate short.
        for layer in pad.layers.layer_ids():
            net = self._pad_net(pad, layer)
            if net is SHORT_CIRCUIT:
                return Decision.reject(
                    f"pin at {pad.position} would land on an already "
                    f"shorted pad ({layer})"
                )
            if net is not None and our_net is not None and net != our_net:
                return Decision.reject(
                    f"pin of net {our_net} would short with net "
                    f"{net} already on pad at {pad.position} ({layer})"
                )
        return Decision.allow()

    def crossing_cost(
        self,
        pad: Pad,
        layer: LayerId,
        our_net: NetId | None,
        wire_type: WireType,
    ) -> Decision:
        # DRC doesn't summarise costs; reuse the route-through verdict
        # so a caller iterating crossings still gets a coherent result.
        d = self.can_route_through(pad, layer, our_net, wire_type)
        return d

    def via_cost(
        self,
        pad: Pad,
        from_layer: LayerId,
        to_layer: LayerId,
        our_net: NetId | None,
    ) -> Decision:
        if not pad.layers.plated_through:
            return Decision.reject(
                f"via at {pad.position} requires a plated-through pad"
            )
        return Decision.allow()

    def is_half_orphan_destination(
        self,
        pad: Pad,
        layer: LayerId,
        our_net: NetId | None,
    ) -> Decision:
        # MVP placeholder — rule 9 follow-up will replace this with a
        # real check against half-orphan wire endpoints recorded on
        # the pad.
        return Decision.allow()


@dataclass(frozen=True, slots=True)
class AutorouterPermissiveStrategy(_BaseStrategy):
    """A* cost-aware strategy. Verdicts mirror DRC's hard blocks but
    soft conflicts are returned as finite costs rather than REJECT so
    the planner can backtrack via a cheaper route.
    """

    def can_solder_wire_to_pad(
        self,
        pad: Pad,
        layer: LayerId,
        our_net: NetId | None,
        wire_type: WireType,
        is_endpoint: bool,
    ) -> Decision:
        if self._is_pad_blocked_by_body(pad, layer):
            return Decision.reject("component body blocks pad")
        net = self._pad_net(pad, layer)
        if net is SHORT_CIRCUIT:
            return Decision.reject("pad already shorted")
        if wire_type == "bare":
            if net is None:
                return Decision.allow(cost=self.config.crossing_cost)
            if our_net is None:
                # Conservative — same as PadIndex.can_route_through
                # historical behaviour.
                return Decision.reject("unknown wire net on netted pad")
            if net != our_net:
                return Decision.reject(
                    f"bare wire would short {our_net} and {net}"
                )
            return Decision.allow(cost=0.0)  # same-net overlap is free
        # Insulated
        if is_endpoint and net is not None and our_net is not None \
                and net != our_net:
            return Decision.reject(
                f"insulated endpoint would short {our_net} and {net}"
            )
        if self._insulated_conflict(pad, layer, our_net):
            return Decision.reject(
                "insulated tangle with another insulated wire"
            )
        return Decision.allow(cost=self.config.crossing_cost)

    def can_route_through(
        self,
        pad: Pad,
        layer: LayerId,
        our_net: NetId | None,
        wire_type: WireType,
    ) -> Decision:
        return self.can_solder_wire_to_pad(
            pad, layer, our_net, wire_type, is_endpoint=False,
        )

    def can_form_t_junction(
        self,
        pad: Pad,
        layer: LayerId,
        through_wire_net: NetId | None,
        incoming_wire_net: NetId | None,
        cross_layer: bool,
    ) -> Decision:
        # Same hard rules as DRC — net conflicts are never legal.
        return DRCEnforcingStrategy(self.config).can_form_t_junction(
            pad, layer, through_wire_net, incoming_wire_net, cross_layer,
        )

    def can_place_pin(
        self,
        pad: Pad,
        our_net: NetId | None,
    ) -> Decision:
        return DRCEnforcingStrategy(self.config).can_place_pin(pad, our_net)

    def crossing_cost(
        self,
        pad: Pad,
        layer: LayerId,
        our_net: NetId | None,
        wire_type: WireType,
    ) -> Decision:
        return self.can_route_through(pad, layer, our_net, wire_type)

    def via_cost(
        self,
        pad: Pad,
        from_layer: LayerId,
        to_layer: LayerId,
        our_net: NetId | None,
    ) -> Decision:
        if not pad.layers.plated_through:
            return Decision.reject("via requires plated-through pad")
        return Decision.allow(cost=self.config.via_cost)

    def is_half_orphan_destination(
        self,
        pad: Pad,
        layer: LayerId,
        our_net: NetId | None,
    ) -> Decision:
        # Follow-up — for now treat every half-orphan as a viable target.
        return Decision.allow()


@dataclass(frozen=True, slots=True)
class SimplifyStrictStrategy(_BaseStrategy):
    """Soft DRC rules made hard for the Simplify tool.

    The user explicitly chose Simplify to clean up a layout; crossings
    on pads that DRC tolerates as "preferable to avoid" are rejected
    so Simplify won't suggest moves that re-introduce them.
    """

    def _drc(self) -> DRCEnforcingStrategy:
        return DRCEnforcingStrategy(self.config)

    def can_solder_wire_to_pad(
        self,
        pad: Pad,
        layer: LayerId,
        our_net: NetId | None,
        wire_type: WireType,
        is_endpoint: bool,
    ) -> Decision:
        base = self._drc().can_solder_wire_to_pad(
            pad, layer, our_net, wire_type, is_endpoint,
        )
        if base.verdict is not Verdict.ALLOW:
            return base
        # Soft rules made strict: reject any extra occupant on the
        # pad of the same kind as us.
        if wire_type == "bare" and not self.config.allow_bare_crossing_on_pad:
            for it in pad.physically_blocked_on(layer):
                if it.id == 0:
                    continue  # synthetic strip item
                if not isinstance(it, (InsulatedWireItem, InsulatedWireCrossing)):
                    # Already a bare wire / pin / body — simplify
                    # refuses to pile another on.
                    return Decision.reject(
                        "simplify rejects bare-on-bare crossing at pad"
                    )
        if wire_type == "insulated" \
                and not self.config.allow_insulated_crossing_on_pad:
            for it in pad.physically_blocked_on(layer):
                if isinstance(it, (InsulatedWireItem, InsulatedWireCrossing)):
                    return Decision.reject(
                        "simplify rejects insulated-on-insulated "
                        "crossing at pad"
                    )
        return base

    def can_route_through(
        self,
        pad: Pad,
        layer: LayerId,
        our_net: NetId | None,
        wire_type: WireType,
    ) -> Decision:
        return self.can_solder_wire_to_pad(
            pad, layer, our_net, wire_type, is_endpoint=False,
        )

    def can_form_t_junction(
        self,
        pad: Pad,
        layer: LayerId,
        through_wire_net: NetId | None,
        incoming_wire_net: NetId | None,
        cross_layer: bool,
    ) -> Decision:
        return self._drc().can_form_t_junction(
            pad, layer, through_wire_net, incoming_wire_net, cross_layer,
        )

    def can_place_pin(
        self,
        pad: Pad,
        our_net: NetId | None,
    ) -> Decision:
        return self._drc().can_place_pin(pad, our_net)

    def crossing_cost(
        self,
        pad: Pad,
        layer: LayerId,
        our_net: NetId | None,
        wire_type: WireType,
    ) -> Decision:
        return self.can_route_through(pad, layer, our_net, wire_type)

    def via_cost(
        self,
        pad: Pad,
        from_layer: LayerId,
        to_layer: LayerId,
        our_net: NetId | None,
    ) -> Decision:
        return self._drc().via_cost(pad, from_layer, to_layer, our_net)

    def is_half_orphan_destination(
        self,
        pad: Pad,
        layer: LayerId,
        our_net: NetId | None,
    ) -> Decision:
        return Decision.allow()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def make_strategy(
    subsystem: SubsystemName,
    config: StrategyConfig | None = None,
) -> PadStrategy:
    """Construct the default strategy for *subsystem*.

    Used as the simple path; tests and tools that need a non-default
    strategy can instantiate the concrete class directly with their
    own :class:`StrategyConfig`.
    """
    cfg = config if config is not None else StrategyConfig.from_session(
        None, subsystem,
    )
    if subsystem == "drc":
        return DRCEnforcingStrategy(cfg)
    if subsystem == "autoroute":
        return AutorouterPermissiveStrategy(cfg)
    if subsystem == "simplify":
        return SimplifyStrictStrategy(cfg)
    raise ValueError(f"unknown subsystem: {subsystem!r}")


__all__ = [
    "Decision",
    "DRCEnforcingStrategy",
    "AutorouterPermissiveStrategy",
    "SimplifyStrictStrategy",
    "PadStrategy",
    "StrategyConfig",
    "SubsystemName",
    "Verdict",
    "WireType",
    "make_strategy",
]
