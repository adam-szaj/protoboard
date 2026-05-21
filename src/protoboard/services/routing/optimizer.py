# SPDX-License-Identifier: MIT
"""Pluggable wire-optimizer protocol.

A *wire optimizer* takes the output of a router (or another optimizer) and
returns a transformed AutorouteResult.  Typical jobs:

- shorten routes that turned out to have a cheaper detour now that we
  know the final wire layout (the existing simplify pass),
- rebalance side assignments between top and bottom layers to reduce
  congestion,
- insert/remove jumpers where insulated crossings could become bare
  layer-changes instead,
- convert insulated wires to bare where the surrounding state proves it
  is now safe.

Optimizers are **chained**: the worker runs the router, then each
selected optimizer in order, each one's output feeding the next.  Each
optimizer reads the same immutable ``RouteRequest`` (board + original
schematic + components) but can produce arbitrarily different wire
lists.

External plugins register themselves via the ``protoboard.wire_optimizers``
entry-point group; the loader is the same one that handles routers and
shares its safety guarantees (failures logged, never crash the host).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from protoboard.services.routing.autorouter import AutorouteResult
    from protoboard.services.routing.protocol import ProgressSink
    from protoboard.services.routing.snapshot import RouteRequest


@dataclass(frozen=True)
class OptimizerCapabilities:
    """What an optimizer claims it can do.

    The UI reads these flags to render an informative pick-list.  Setting
    a flag does not impose obligations on the implementation — it is a
    hint to the user, not a contract.  Default: all-False.
    """

    # Moves wires between top and bottom to reduce per-side congestion.
    rebalances_sides: bool = False
    # Replaces insulated jumpers that cross other wires with a layer-change
    # via a plated-through pad.
    inserts_jumpers: bool = False
    # Reverses jumpers: collapses a top/bottom transition back into a
    # single-side path when nothing on the other layer needs the via.
    removes_jumpers: bool = False
    # Converts insulated wires to bare where the surrounding state proves
    # crossings cannot short.
    insulated_to_bare: bool = False
    # Converts bare wires to insulated when keeping the crossing structurally
    # safe is impossible without the insulation.
    bare_to_insulated: bool = False
    # Shortens routes by finding cheaper subpaths.  The legacy "simplify"
    # pass falls into this category.
    shortens_paths: bool = False
    # Free-form description shown in the picker tooltip.
    notes: str = ""


@runtime_checkable
class WireOptimizer(Protocol):
    """Pluggable wire optimizer.

    Contract:
    - ``optimize(request, result, sink)`` returns a fresh AutorouteResult
      (do not mutate the input).
    - The optimizer must NOT touch session state — only the immutable
      RouteRequest and the result it received.
    - Long-running work polls ``sink.cancelled()`` and raises
      :class:`AutorouteCancelled` when it returns True.
    - Optimizers are composable; their output is fed into the next one
      in the chain, so each must accept any reasonable wire layout
      (including the empty list).
    """

    name: str
    display_name: str

    def optimize(
        self,
        request: "RouteRequest",
        result: "AutorouteResult",
        sink: "ProgressSink",
    ) -> "AutorouteResult": ...

    def capabilities(self) -> OptimizerCapabilities: ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

@dataclass
class _OptimizerRegistry:
    """Process-global optimizer registry.

    Module-level instance is exposed as :data:`registry`.  Keep the
    indirection so tests can construct a fresh registry instead of
    polluting global state.
    """

    optimizers: dict[str, WireOptimizer] = field(default_factory=dict)

    def register(self, optimizer: WireOptimizer) -> WireOptimizer:
        """Add or replace an optimizer by ``optimizer.name``.

        Re-registration is allowed (overrides a previous entry).
        """
        if not getattr(optimizer, "name", None):
            raise ValueError("WireOptimizer must define a non-empty `name`")
        self.optimizers[optimizer.name] = optimizer
        return optimizer

    def get(self, name: str) -> WireOptimizer:
        try:
            return self.optimizers[name]
        except KeyError:
            available = ", ".join(sorted(self.optimizers)) or "<none>"
            raise KeyError(
                f"No optimizer registered as {name!r}; available: {available}"
            ) from None

    def list_available(self) -> list[str]:
        return sorted(self.optimizers)


registry = _OptimizerRegistry()


__all__ = [
    "WireOptimizer",
    "OptimizerCapabilities",
    "_OptimizerRegistry",
    "registry",
]
