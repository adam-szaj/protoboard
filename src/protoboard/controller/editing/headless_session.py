# SPDX-License-Identifier: MIT
"""HeadlessSession — Qt-free mutable state substrate for board editing.

Holds the current Board, Layout, Schematic, component instances/definitions
and the CommandStack, plus the read caches (connectivity graph, pad index,
spatial indexes, wire-net map, …) that every pre-command read depends on.

This is the state layer both the GUI and a headless CLI share. ``EditorSession``
(``protoboard.gui.session``) subclasses it, adding a ``QObject`` and the Qt
signals panels observe; the pure-Python caches and lookups live here so a
CLI / test front-end can drive the same :class:`EditController` verbs with no
display server.

The command-execution substrate itself is already headless — this class closes
the remaining gap, which is the *reads before a command* (conflict scan,
footprint resolution, connectivity queries).
"""

from __future__ import annotations

import os
import time
import uuid
from collections import defaultdict
from collections.abc import Iterable
from typing import TYPE_CHECKING

import structlog as _structlog

if TYPE_CHECKING:
    from protoboard.domain.board.pad_state import PadIndex
    from protoboard.domain.components.footprints import Footprint
    from protoboard.domain.layout.group_tree import GroupTree
    from protoboard.domain.layout.pad_conflict import ConflictScanner
    from protoboard.domain.layout.wire import Wire
    from protoboard.controller.editing.controller import EditController
    from protoboard.controller.editing.item_handle import ItemId
    from protoboard.controller.selection.service import SelectionService

from protoboard.controller.highlight_stack import HighlightStackBase
from protoboard.domain.axis_labels_settings import AxisLabelSettings
from protoboard.domain.board.board import Board
from protoboard.domain.commands.base import CommandStack, ComponentState
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package import PackageFamily
from protoboard.domain.connectivity.incremental import ConnectivityGraph
from protoboard.domain.layout.layout import Layout
from protoboard.domain.routing_settings import RoutingSettings
from protoboard.domain.layout.placement import pin_world_position
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point, Rect
from protoboard.services.drc.engine import DRCEngine, DRCIssue
from protoboard.services.library.loader import ComponentLibrary
from protoboard.services.library.package_family_loader import (
    load_full_package_catalogue,
)

_click_log = _structlog.get_logger("protoboard.gui.session.click")
_CLICK_PROFILE = os.environ.get("PROTOBOARD_CLICK_PROFILE") not in (None, "", "0")


def _build_comp_spatial_index(
    layout: Layout,
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    families: dict[str, PackageFamily] | None = None,
) -> dict[tuple[int, int], list[uuid.UUID]]:
    """Build grid-cell → comp_id index from current placements."""
    from protoboard.domain.components.package import resolved_footprint
    idx: dict[tuple[int, int], list[uuid.UUID]] = defaultdict(list)
    for comp_id, placement in layout.placements.items():
        inst = instances.get(comp_id)
        if inst is None:
            continue
        defn = components.get(inst.definition_ref)
        if defn is None:
            continue
        fp = resolved_footprint(defn, inst, families)
        seen_cells: set[tuple[int, int]] = set()
        for n in range(fp.pin_count()):
            p = pin_world_position(fp, placement, n + 1)
            cell = (round(p.x), round(p.y))
            if cell not in seen_cells:
                seen_cells.add(cell)
                idx[cell].append(comp_id)
    return dict(idx)


def _build_wire_spatial_index(layout: Layout) -> dict[tuple[int, int], list[uuid.UUID]]:
    """Build grid-cell → wire_id index from current wires."""
    idx: dict[tuple[int, int], list[uuid.UUID]] = defaultdict(list)
    for wire in layout.wires:
        seen_cells: set[tuple[int, int]] = set()
        for pt in wire.path:
            cell = (round(pt.x), round(pt.y))
            if cell not in seen_cells:
                seen_cells.add(cell)
                idx[cell].append(wire.id)
    return dict(idx)


class HeadlessSession:
    """Qt-free state container shared by the GUI session and headless drivers."""

    def __init__(self, board: Board, library: ComponentLibrary) -> None:
        self.board = board
        self.library = library
        self.schematic = Schematic(nets={})
        self._layout: Layout = Layout()
        self._wire_resolver_to(self._layout)
        self._layout.ensure_root_group()
        # Pre-populate components from library so renderers can look up
        # definitions. A ComponentCatalog synthesizes parametric variants
        # (PinHeader_1x05) on miss, so instance refs from loaded projects and
        # the placement dialog resolve transparently.
        from protoboard.services.library.parametric import ComponentCatalog
        self.instances: dict[uuid.UUID, ComponentInstance] = {}
        # Package-family body catalogue, keyed by family id: the authored
        # two-pin families plus the generated in-line bodies (dip-N / wdip-N /
        # sil-N), plus families contributed by the library files' own
        # ``families:`` sections. The builtin catalogue wins on an id clash
        # (matching the loader's first-wins rule).
        self.package_families: dict[str, PackageFamily] = {
            **library.package_families,
            **load_full_package_catalogue(),
        }
        self.components: dict[str, ComponentDefinition] = ComponentCatalog(
            ((name, library.get(name)) for name in library.names()),
            package_families=self.package_families,
        )
        self.stack: CommandStack[ComponentState] = CommandStack()
        self._graph: ConnectivityGraph | None = None  # rebuilt lazily; None = stale
        self._pad_index: PadIndex | None = None
        # PadStrategy overrides keyed by subsystem name. Empty by default —
        # `strategy_for(...)` falls back to the project- and subsystem-
        # specific defaults assembled by `StrategyConfig.from_session`.
        # Tests inject mocks via `override_strategy`; production tools
        # may inject a tuned strategy per session.
        self._strategy_overrides: dict[str, object] = {}
        self._comp_spatial_index: dict[tuple[int, int], list[uuid.UUID]] | None = None
        self._wire_spatial_index: dict[tuple[int, int], list[uuid.UUID]] | None = None
        # Cached pad-walk sets used by post-hoc cost analysis.  Both depend
        # only on board + placements; `_plated_through_pads_cache` is board-
        # only (never invalidates after construction), while
        # `_under_component_pads_cache` is invalidated alongside the graph.
        # Without these, every selection-changed event would re-walk every
        # board atom — visible as multi-hundred-ms freeze when the user
        # clicks a wire on a large board.
        self._plated_through_pads_cache: set[Point] | None = None
        self._under_component_pads_cache: set[Point] | None = None
        # wire.id → net.id mapping, cached because both BoardView and the
        # inspector / performance panels call ``build_wire_net_map`` on
        # every selection click.  Invalidated alongside the graph since
        # it derives from ConnectivityGraph.
        self._wire_net_map_cache: dict[uuid.UUID, uuid.UUID] | None = None
        # wire.id → Wire lookup, populated lazily by
        # ``wire_by_id()``.  SelectTool's click handlers used to scan
        # ``layout.wires`` for every highlight redraw — O(N²) when the
        # active net has many wires.  Invalidated on every layout change.
        self._wire_by_id_cache: dict[uuid.UUID, Wire] | None = None
        # Axis-label settings feed DRC (off-board / label checks). A plain
        # domain default here; the GUI exposes a settings panel over it.
        self.axis_label_settings: AxisLabelSettings = AxisLabelSettings()
        # One EditController per session — the stateless verb surface both the
        # GUI tools and a headless CLI drive. Built lazily so the base module
        # never imports the controller at construction time.
        self._edit_controller: EditController | None = None
        # Current selection as tagged ItemIds — the model-side selection the
        # controller's selection-relative verbs (rotate()/flip() with no ids)
        # read. A headless session owns it directly; EditorSession overrides the
        # accessors to bridge its Hit-based SelectionService.
        self._selection: frozenset[ItemId] = frozenset()
        # Routing/selection state the controller tools read/write. These are
        # domain/controller concepts (RoutingSettings is domain, SelectionService
        # is controller) that merely happened to be initialised on the GUI
        # session during the migration — declared here so a headless driver
        # carries them too and the ``EditState`` contract can name them.
        self.routing_settings: RoutingSettings = RoutingSettings()
        self.selection_service: SelectionService | None = None
        # The shared highlight stack the controller tools push/pop dimming onto.
        # Qt-free base here (headless: the ``_changed`` hook is a no-op); the GUI
        # session overrides with the signal-emitting ``gui.highlight_state``
        # subclass so BoardView re-applies opacity.
        self.highlight_stack: HighlightStackBase = HighlightStackBase()
        self._routing_net_id: uuid.UUID | None = None

    @property
    def edit_controller(self) -> EditController:
        """The session's :class:`EditController` (built once, on first use)."""
        if self._edit_controller is None:
            from protoboard.controller.editing.controller import EditController
            self._edit_controller = EditController(self)
        return self._edit_controller

    def notify_changed(self) -> None:
        """Change hook fired by :class:`EditController` after each command.

        No-op in the headless base — a bare driver has no panels to refresh.
        ``EditorSession`` overrides it to emit ``layout_changed``.
        """

    def notify_active_tool_changed(self) -> None:
        """Hook fired by :meth:`EditController.set_active_tool`.

        No-op in the headless base — a bare driver has no KeyMap layer to
        swap. ``EditorSession`` overrides it to emit ``active_tool_changed``.
        """

    def notify_selection(
        self,
        comp_ids: frozenset[uuid.UUID],
        wire_ids: frozenset[uuid.UUID],
        net_id: uuid.UUID | None,
    ) -> None:
        """Broadcast selection state (SelectTool → panels).

        No-op in the headless base (no panels). ``EditorSession`` overrides it
        to emit ``selection_changed``.
        """

    def set_routing_net(self, net_id: uuid.UUID | None) -> None:
        """Mark a net as actively routed (RouteTool while drawing); None clears.

        Headless just records it; ``EditorSession`` also emits
        ``routing_net_changed`` for the view to union into the highlight.
        """
        self._routing_net_id = net_id

    @property
    def routing_net_id(self) -> uuid.UUID | None:
        """The net RouteTool is currently drawing (None when idle)."""
        return self._routing_net_id

    # ------------------------------------------------------------------
    # Selection (model-side, ItemId-based)
    # ------------------------------------------------------------------

    def selection(self) -> frozenset[ItemId]:
        """The current selection as tagged :class:`ItemId`s."""
        return self._selection

    def select(self, ids: Iterable[ItemId]) -> None:
        """Replace the current selection with *ids*."""
        self._selection = frozenset(ids)

    def run_drc(self) -> list[DRCIssue]:
        """Run DRC synchronously on the calling thread and return the issues.

        The Qt-free compute path shared by the CLI / headless validation and by
        the GUI ``EditorSession.run_drc`` (which additionally records the result
        + emits ``drc_changed``). Hands the live pad_index over so the fast
        checks kick in.
        """
        engine = DRCEngine()
        return engine.check_with_graph(
            schematic=self.schematic,
            layout=self.layout,
            components=self.components,
            instances=self.instances,
            graph=self.connectivity_graph(),
            axis_labels=self.axis_label_settings,
            board=self.board,
            pad_index=self.pad_index(),
        )

    # ------------------------------------------------------------------
    # Layout (with PinKnot resolver rebinding on swap)
    # ------------------------------------------------------------------

    @property
    def layout(self) -> Layout:
        return self._layout

    @layout.setter
    def layout(self, value: Layout) -> None:
        """Swap the layout, re-binding the PinKnot resolver to ``self``.

        ``replace_wire_fragments`` needs a resolver to validate ``PinKnot``
        positions; without re-binding it on every layout swap, the loader's
        fresh Layout instance comes in without one and v2 commands crash
        the moment the user touches a wire.
        """
        self._layout = value
        self._wire_resolver_to(value)

    def _wire_resolver_to(self, layout: Layout) -> None:
        layout.set_pin_resolver(
            lambda pin: self._resolve_pin_for_knot(pin)
        )
        layout.set_footprint_resolver(self._resolve_footprint_for_flip)

    def _resolve_footprint_for_flip(
        self, comp_id: uuid.UUID
    ) -> Footprint | None:
        """Resolve *comp_id*'s chosen-variant footprint for the seam's group-flip
        compose branch.  Returns None (a flipped group then raises loudly) when
        the chain instance→definition→footprint can't be resolved."""
        from protoboard.domain.components.package import resolved_footprint
        inst = self.instances.get(comp_id)
        if inst is None:
            return None
        defn = self.components.get(inst.definition_ref)
        if defn is None:
            return None
        try:
            return resolved_footprint(defn, inst, self.package_families)
        except (IndexError, ValueError):
            return None

    # ------------------------------------------------------------------
    # Cache invalidation + read caches
    # ------------------------------------------------------------------

    def invalidate_graph(self) -> None:
        """Mark the cached ConnectivityGraph, PadIndex and spatial indexes as stale."""
        self._graph = None
        self._pad_index = None
        self._comp_spatial_index = None
        self._wire_spatial_index = None
        # Under-component pad set depends on placements; invalidate alongside
        # the graph.  Plated-through is board-only and survives.
        self._under_component_pads_cache = None
        # Wire→net map depends on the graph; recompute lazily next time.
        self._wire_net_map_cache = None
        # Wire-by-id index depends only on the wires list; rebuilt on
        # demand.  layout_changed invalidates the graph (via this method),
        # so this also stays in sync.
        self._wire_by_id_cache = None

    def rebuild_all_pin_identities(self) -> None:
        """Bind every placed instance and (re)build its PinIdentity groups.

        Call after bulk-loading instances (project open, KiCAD import). Two
        things are seeded per instance, both of which must persist across
        layout-change rebuilds:

        * the live ``definition`` + ``package_families`` back-reference, so the
          instance resolves its own variant-aware footprint
          (``inst.resolved_footprint()``) without the caller threading the
          catalogue;
        * the same-name ``pin_identities`` (and the ``net_id`` they carry).

        Binding returns a fresh frozen copy, stored back into ``instances``.
        Unlike the graph/PadIndex this is NOT tied to ``invalidate_graph``:
        these persist across layout changes, so they are rebuilt only on bulk
        entry, not on every refresh.
        """
        for comp_id, inst in list(self.instances.items()):
            defn = self.components.get(inst.definition_ref)
            if defn is not None:
                bound = inst.bind(defn, self.package_families)
                self.instances[comp_id] = bound
                bound.build_pin_identities(defn)

    def cached_under_component_pads(self) -> set[Point]:
        """Return cached set of top-layer pads sitting under a component body.

        Used by the post-hoc cost analyzer.  Rebuilt on first access after
        ``invalidate_graph`` (which the layout_changed signal triggers).
        Keeping this on the session avoids re-walking every board atom for
        every panel refresh / selection click.
        """
        cache = self._under_component_pads_cache
        if cache is None:
            from protoboard.services.routing.cost_analyzer import (
                _under_component_pads,
            )
            cache = _under_component_pads(
                self.board, self.layout, self.components, self.instances,
            )
            self._under_component_pads_cache = cache
        return cache

    def wire_by_id(self, wire_id: uuid.UUID) -> Wire | None:
        """O(1) wire lookup by id.

        Replaces the ``next((w for w in layout.wires if w.id == X), None)``
        idiom that select_tool was running per highlight redraw — at O(N)
        per call, that becomes O(N²) when the active net is large.
        """
        cache = self._wire_by_id_cache
        if cache is None:
            cache = {w.id: w for w in self.layout.wires}
            self._wire_by_id_cache = cache
        return cache.get(wire_id)

    def cached_wire_net_map(self) -> dict[uuid.UUID, uuid.UUID]:
        """Return cached wire.id → net.id mapping.

        Multiple panels build this map on every selection click — caching
        it on the session collapses the work to one build per layout
        change.  Recomputed lazily after ``invalidate_graph``.
        """
        cache = self._wire_net_map_cache
        if cache is None:
            _t0 = time.monotonic() if _CLICK_PROFILE else 0.0
            from protoboard.domain.connectivity.wire_net_map import (
                build_wire_net_map,
            )
            cache = build_wire_net_map(
                self.schematic, self.layout, self.connectivity_graph()
            )
            self._wire_net_map_cache = cache
            if _CLICK_PROFILE:
                _click_log.info(
                    "click_profile.session.cached_wire_net_map.build",
                    ms=round((time.monotonic() - _t0) * 1000, 1),
                    n_wires=len(cache),
                )
        return cache

    def cached_plated_through_pads(self) -> set[Point]:
        """Return cached set of plated-through pad positions on this board.

        Board topology is fixed for the lifetime of the project, so this
        cache is built once and reused forever.
        """
        cache = self._plated_through_pads_cache
        if cache is None:
            from protoboard.services.routing.cost_analyzer import (
                _plated_through_pads,
            )
            cache = _plated_through_pads(self.board)
            self._plated_through_pads_cache = cache
        return cache

    def _record_build_time(self, kind: str, ms: float) -> None:
        """Hook: record a lazy-cache (re)build time.

        No-op in the headless base; ``EditorSession`` overrides it to feed
        its GUI ``PerfMetrics`` (``graph_build_ms`` / ``pad_index_build_ms``)
        so the performance panel keeps reporting cache-rebuild cost.
        """

    def connectivity_graph(self) -> ConnectivityGraph:
        """Return the cached graph, rebuilding it if stale."""
        if self._graph is None:
            t0 = time.monotonic()
            self._graph = ConnectivityGraph(
                board=self.board,
                layout=self.layout,
                components=self.components,
                instances=self.instances,
            )
            dt = (time.monotonic() - t0) * 1000
            self._record_build_time("graph", dt)
            if _CLICK_PROFILE:
                _click_log.info(
                    "click_profile.session.connectivity_graph.build",
                    ms=round(dt, 1),
                )
        return self._graph

    def pad_index(self) -> PadIndex:
        """Return the cached PadIndex, rebuilding it if stale.

        PadIndex is the single source of truth for per-pad occupancy
        (electrical net + physical blockers) used by simplify, autoroute,
        drag-conflict and DRC.  Lifetime parallels ``connectivity_graph()``.
        """
        if self._pad_index is None:
            from protoboard.domain.board.pad_state import PadIndex
            t0 = time.monotonic()
            self._pad_index = PadIndex(
                board=self.board,
                layout=self.layout,
                schematic=self.schematic,
                components=self.components,
                instances=self.instances,
            )
            self._record_build_time("pad_index", (time.monotonic() - t0) * 1000)
        return self._pad_index

    def comp_spatial_index(self) -> dict[tuple[int, int], list[uuid.UUID]]:
        """Return grid-cell → comp_id index, rebuilding lazily after layout_changed."""
        if self._comp_spatial_index is None:
            self._comp_spatial_index = _build_comp_spatial_index(
                self.layout, self.instances, self.components,
                self.package_families,
            )
        return self._comp_spatial_index

    def wire_spatial_index(self) -> dict[tuple[int, int], list[uuid.UUID]]:
        """Return grid-cell → wire_id index, rebuilding lazily after layout_changed."""
        if self._wire_spatial_index is None:
            self._wire_spatial_index = _build_wire_spatial_index(self.layout)
        return self._wire_spatial_index

    # ------------------------------------------------------------------
    # Group / conflict query facades
    # ------------------------------------------------------------------

    def group_tree(self) -> GroupTree:
        """A :class:`GroupTree` view over the live ``layout.groups`` dict.

        The convenient entry point for group-hierarchy queries
        (``session.group_tree().all_component_ids(gid)`` etc.) instead of the
        legacy free functions in ``domain/layout/group.py``. Cheap — wraps the
        live dict by reference, no caching.
        """
        from protoboard.domain.layout.group_tree import GroupTree

        return GroupTree(self._layout.groups)

    def conflict_scanner(
        self, *, pad_index: PadIndex | None = None
    ) -> ConflictScanner:
        """A :class:`ConflictScanner` over this session's live layout context.

        The convenient entry point for pad/wire-conflict queries
        (``session.conflict_scanner(pad_index=idx).check_group_move(...)`` etc.)
        instead of re-passing ``placements / instances / components /
        schematic`` to every free function in ``domain/layout/pad_conflict.py``.

        ``pad_index`` is taken explicitly because conflict callers usually want
        a *post-move* index (a ``PadIndex.clone()`` reflecting the dragged
        positions), which differs from the stale ``session.pad_index()``; when
        omitted the legacy from-layout paths run. Cheap — wraps live dicts by
        reference, no caching.
        """
        from protoboard.domain.layout.pad_conflict import ConflictScanner

        return ConflictScanner.from_session(self, pad_index=pad_index)

    def strategy_for(self, subsystem: str) -> object:
        """Return the :class:`PadStrategy` bound to *subsystem* for this
        session.

        Order of precedence:
        1. Test override set via :meth:`override_strategy` — returned as-is.
        2. Default strategy assembled by :func:`pad_strategy.make_strategy`,
           using project knobs from ``self.settings`` (currently only
           ``strict_tape_joints``) and the subsystem-specific defaults
           from :meth:`StrategyConfig.from_session`.

        ``subsystem`` accepts ``"drc"``, ``"autoroute"``, ``"simplify"``.
        The return type is ``object`` to keep this module light on
        imports — callers receive a :class:`PadStrategy` Protocol
        instance and can rely on duck typing.
        """
        override = self._strategy_overrides.get(subsystem)
        if override is not None:
            return override
        from protoboard.services.pad_strategy import (
            StrategyConfig,
            make_strategy,
        )
        cfg = StrategyConfig.from_session(self, subsystem)  # type: ignore[arg-type]
        return make_strategy(subsystem, cfg)  # type: ignore[arg-type]

    def override_strategy(self, subsystem: str, strategy: object | None) -> None:
        """Install (or clear, when *strategy* is None) a custom
        :class:`PadStrategy` for *subsystem*.

        Intended for unit tests that need a deterministic verdict.
        Production tools should rely on :meth:`strategy_for` and
        adjust knobs via :class:`StrategyConfig`/``EditorSession``
        settings instead.
        """
        if strategy is None:
            self._strategy_overrides.pop(subsystem, None)
        else:
            self._strategy_overrides[subsystem] = strategy

    # ------------------------------------------------------------------
    # Component / pin lookups
    # ------------------------------------------------------------------

    def _resolve_pin_for_knot(self, pin: object) -> Point:
        """PinKnot resolver — bridges Layout's resolver protocol to session.

        Called by ``Layout._knot_position`` whenever a v2 mutator needs the
        world position of a ``PinKnot``.  Returns the current placement
        position; raises if the component vanished from the layout (a stale
        knot — caller should drop it).
        """
        pos = self.get_pin_position(pin.component_id, pin.pin_number)  # type: ignore[attr-defined]
        if pos is None:
            raise RuntimeError(
                f"PinKnot resolver: pin {pin} cannot be resolved (component absent)"
            )
        return pos

    def get_pin_position(
        self,
        comp_id: uuid.UUID,
        pin_number: int,
    ) -> Point | None:
        """Return the world position of *pin_number* on *comp_id*, or None.

        Thin wrapper over
        :func:`protoboard.domain.layout.placement.resolve_pin_world_position`
        — the canonical pin-resolution chain.  Returns None on any
        missing intermediate (no placement / instance / definition,
        or pin number out of range).
        """
        from protoboard.domain.layout.placement import (
            resolve_pin_world_position,
        )
        return resolve_pin_world_position(
            comp_id, pin_number,
            placements=self.layout.placements,
            instances=self.instances,
            components=self.components,
        )

    def iter_pin_positions(
        self,
        comp_id: uuid.UUID,
    ) -> list[tuple[int, Point]]:
        """Return [(pin_number, world_pos), …] for every pin of *comp_id*.

        Returns an empty list if the component or its definition is not found.
        Every pin resolution delegates to the canonical safety chain.
        """
        from protoboard.domain.layout.pad_conflict import (
            resolved_footprint_or_default,
        )
        from protoboard.domain.layout.placement import (
            resolve_pin_world_position,
        )
        inst = self.instances.get(comp_id)
        if inst is None:
            return []
        defn = self.components.get(inst.definition_ref)
        if defn is None:
            return []
        result: list[tuple[int, Point]] = []
        for n in range(resolved_footprint_or_default(defn, inst).pin_count()):
            pin_num = n + 1
            pos = resolve_pin_world_position(
                comp_id, pin_num,
                placements=self.layout.placements,
                instances=self.instances,
                components=self.components,
                families=self.package_families,
            )
            if pos is not None:
                result.append((pin_num, pos))
        return result

    def meta_pin_for_package_pin(
        self,
        comp_id: uuid.UUID,
        package_pin_number: int,
    ) -> int | None:
        """Translate a physical package pin to its logical MetaPin ordinal."""
        from protoboard.domain.components.package import meta_pin_for_package_pin

        inst = self.instances.get(comp_id)
        if inst is None:
            return None
        defn = self.components.get(inst.definition_ref)
        if defn is None:
            return None
        return meta_pin_for_package_pin(defn, inst, package_pin_number)

    def component_bbox(self, comp_id: uuid.UUID) -> Rect | None:
        """Return the world-space bounding rect of *comp_id*, or None.

        Returns None when the component lacks a placement or its
        definition / footprint is unresolvable.  Canonical GUI-facing
        wrapper around :func:`domain.layout.placement.component_bbox`
        so renderers/tools never have to inline the (placement,
        instance, definition) lookup themselves.
        """
        from protoboard.domain.components.package import resolved_footprint
        from protoboard.domain.layout.placement import component_bbox
        placement = self.layout.placements.get(comp_id)
        if placement is None:
            return None
        inst = self.instances.get(comp_id)
        if inst is None:
            return None
        defn = self.components.get(inst.definition_ref)
        if defn is None:
            return None
        try:
            fp = resolved_footprint(defn, inst, self.package_families)
            return component_bbox(fp, placement, defn.component_class)
        except Exception:  # noqa: BLE001
            return None

    def component_definition(
        self, comp_id: uuid.UUID,
    ) -> ComponentDefinition | None:
        """Return the ComponentDefinition for *comp_id*, or None.

        Resolves the placement → instance → definition chain so renderers
        and tools needing the footprint don't have to inline it.
        """
        inst = self.instances.get(comp_id)
        if inst is None:
            return None
        return self.components.get(inst.definition_ref)


__all__ = ["HeadlessSession"]
