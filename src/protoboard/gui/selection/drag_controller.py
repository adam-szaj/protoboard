# SPDX-License-Identifier: MIT
"""``DragController`` — the drag / floating gesture as a headless service.

This is the Phase-3 reimplementation of SelectTool's ~2100-line drag subsystem,
extracted into a Qt-free controller that owns the whole lifecycle:

* **arm** (construction, on press) — snapshot a :class:`DragSet`, the original
  placements / wire paths, the grab anchor, and a *trial* :class:`PadIndex`
  cloned once;
* **update** (per move) — past a start threshold, translate every member and
  internal wire by one shared delta, re-sync the trial PadIndex, run
  :class:`DragDetector` for a live :class:`DragCommitDecision`, and report it
  through the injected ``on_preview`` seam;
* **commit** (on release) — below threshold, restore and report "this was a
  click" (``False``); otherwise resolve the conflict decision into a
  :class:`MoveDragSetCommand` and execute it (CLEAR / auto-* preferences) or
  hand off to a *floating* sub-state (SOFT / HARD) that waits for the user to
  confirm or cancel.

Everything geometric and decision-making is pure: the same
``classify_drag_commit`` helper the legacy tool used builds the
:class:`DragSnapshot`, and :class:`DragDetector` classifies it.  The Qt-touching
parts — snapping the cursor to the grid, drawing the conflict overlay, and the
ask-/block- conflict dialogs — are injected as callables, so a test can drive a
full drag with fakes and no ``QApplication``.

Mirrors legacy ``SelectTool`` drag math verbatim: anchor-snap with grab offset
(``select_tool.py:3576``), one-delta translation (``:3611``), trial PadIndex
clone-and-resync (``:2693``), ``MoveDragSetCommand`` build (``:2230``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Protocol

import structlog

from protoboard.geometry import Point, Vector
from protoboard.gui.drag_set import DragSet
from protoboard.gui.selection.mouse_input import Intent, Mods
from protoboard.gui.tools.drag_commit_decision import DragSeverity

if TYPE_CHECKING:
    from collections.abc import Callable

    from protoboard.domain.commands.base import Command, ComponentState
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.layout.group import ComponentGroup
    from protoboard.domain.layout.placement import ComponentPlacement
    from protoboard.domain.layout.wire import Wire
    from protoboard.domain.schematic.schematic import Schematic
    from protoboard.gui.drag_preferences import DragPreferences
    from protoboard.gui.tools.drag_commit_decision import DragCommitDecision

__all__ = ["DragContext", "DragController", "DragOutcome", "FloatingPhase"]

_log = structlog.get_logger(__name__)

#: Below this travel (raster units) from the press point, a press+release is a
#: plain click, not a drag (legacy ``_DRAG_START_THRESHOLD``).
_DRAG_START_THRESHOLD = 0.5


class FloatingPhase(Enum):
    """Where a drag is in its lifecycle after :meth:`DragController.commit`."""

    NONE = "none"
    """Not started, or committed/cancelled (terminal)."""
    COMMITTED = "committed"
    """The move was applied directly (CLEAR + auto-accept)."""
    FLOATING = "floating"
    """Awaiting the user's confirm / cancel (SOFT or HARD conflict)."""


@dataclass(frozen=True)
class DragOutcome:
    """What :meth:`DragController.commit` decided.

    A small value object so the tool can react (status bar, post-commit
    autoroute) without re-reading controller internals.
    """

    phase: FloatingPhase
    severity: DragSeverity = DragSeverity.CLEAR
    dropped_wire_count: int = 0
    reroute_net_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)


class _LayoutLike(Protocol):
    """The layout surface the controller touches (placements / wires / groups)."""

    placements: dict[uuid.UUID, ComponentPlacement]
    wires: list[Wire]
    groups: dict[uuid.UUID, ComponentGroup]


class DragContext(Protocol):
    """Narrow seam onto the session the controller reads / mutates.

    Mirrors the subset of :class:`EditorSession` the drag path touches — the
    layout, the instance / definition maps, the schematic, the live PadIndex and
    the wire→net cache (both consulted by the classifier).  A test supplies a
    fake with the same surface to exercise the controller headless.
    """

    @property
    def layout(self) -> _LayoutLike: ...
    @property
    def instances(self) -> dict[uuid.UUID, ComponentInstance]: ...
    @property
    def components(self) -> dict[str, ComponentDefinition]: ...
    @property
    def schematic(self) -> Schematic: ...

    def pad_index(self) -> object: ...
    def cached_wire_net_map(self) -> dict[uuid.UUID, uuid.UUID]: ...


# Injected Qt seams (all optional — the core works without them).
if TYPE_CHECKING:
    SnapFn = Callable[[Point, frozenset[uuid.UUID]], Point]
    """``(raw_anchor, exclude_ids) -> snapped`` — wraps ``view.snap``."""
    PreviewFn = Callable[["DragCommitDecision | None"], None]
    """Called per move with the live decision (or ``None`` pre-threshold)."""
    ResolveFreeFn = Callable[
        [str, frozenset[uuid.UUID]],
        "tuple[set[uuid.UUID], set[uuid.UUID]] | None",
    ]
    """``(preference, wire_ids) -> (drop, reroute_nets) | None`` (None = cancel)."""
    ExecuteFn = Callable[["Command[ComponentState]"], bool]
    """Execute a command against the session; return False if rejected."""


class DragController:
    """Headless drag / floating gesture over a :class:`DragContext`.

    Implements the
    :class:`~protoboard.gui.selection.mouse_input.MouseInputController` protocol
    (``on_move`` drives the drag, ``on_release`` finalizes, ``on_event`` handles
    CONFIRM / CANCEL while floating); armed by constructing it on press with the
    snapshot of what to drag.
    """

    def __init__(
        self,
        ctx: DragContext,
        *,
        drag_set: DragSet,
        anchor_id: uuid.UUID | None,
        anchor_pos: Point,
        press_pt: Point,
        grab_offset: tuple[float, float],
        wire_ids: frozenset[uuid.UUID] = frozenset(),
        snap: SnapFn | None = None,
        preview: PreviewFn | None = None,
        resolve_free: ResolveFreeFn | None = None,
        resolve_sgroup: ResolveFreeFn | None = None,
        resolve_disconnect: ResolveFreeFn | None = None,
        execute: ExecuteFn | None = None,
        preferences: DragPreferences | None = None,
        auto_accept_clear: bool = False,
    ) -> None:
        self._ctx = ctx
        self._drag_set = drag_set
        self._anchor_id = anchor_id
        self._anchor_pos = anchor_pos
        self._press_pt = press_pt
        self._grab = grab_offset
        self._snap = snap
        self._preview = preview
        self._resolve_free = resolve_free
        self._resolve_sgroup = resolve_sgroup
        self._resolve_disconnect = resolve_disconnect
        self._execute = execute
        self._prefs = preferences
        self._auto_accept_clear = auto_accept_clear

        groups_map = getattr(ctx.layout, "groups", {})
        self._member_ids: frozenset[uuid.UUID] = drag_set.all_component_ids(
            groups_map
        )
        self._explicit_wire_ids = wire_ids
        # Snapshot placements + wire paths so update() can translate from a
        # stable origin (re-applying delta, never compounding).
        self._orig_positions: dict[uuid.UUID, Point] = {}
        for cid in self._member_ids:
            pl = ctx.layout.placements.get(cid)
            if pl is not None:
                self._orig_positions[cid] = pl.position
        self._orig_wire_paths: dict[uuid.UUID, list[Point]] = {}
        for wire in ctx.layout.wires:
            if wire.id in self._dragged_wire_ids:
                self._orig_wire_paths[wire.id] = list(wire.path)

        self._threshold_crossed = False
        self._last_delta: Vector | None = None
        self._last_decision: DragCommitDecision | None = None
        self._phase = FloatingPhase.NONE
        self._trial_pad_index: object | None = None
        self._floating_orig: dict[uuid.UUID, ComponentPlacement] = {}
        self._was_click = False
        self._cancelled = False
        #: The outcome of the last terminal step (commit / cancel) — lets the
        #: tool report what happened (drop count, severity) after a release that
        #: auto-committed, without re-reading internals.
        self._last_outcome: DragOutcome | None = None

    # -- introspection (for the tool / tests) ------------------------------

    @property
    def phase(self) -> FloatingPhase:
        return self._phase

    @property
    def is_floating(self) -> bool:
        return self._phase is FloatingPhase.FLOATING

    @property
    def last_decision(self) -> DragCommitDecision | None:
        return self._last_decision

    @property
    def last_outcome(self) -> DragOutcome | None:
        """The outcome of the last commit / cancel — for the tool's status bar."""
        return self._last_outcome

    @property
    def member_ids(self) -> frozenset[uuid.UUID]:
        return self._member_ids

    @property
    def _dragged_wire_ids(self) -> frozenset[uuid.UUID]:
        """Wires that travel with the drag — explicit selection only here.

        Internal wires owned by moving components are picked up by the wire
        classifier at commit; this set is wires the user *also* selected.
        """
        return self._explicit_wire_ids

    # -- MouseInputController -----------------------------------------------

    def on_press(self, scene_pt: Point, mods: Mods) -> bool:
        """A drag is armed on construction; a further press isn't part of it."""
        return False

    def on_click(self, scene_pt: Point, mods: Mods) -> bool:
        """Drag is press-drag-release, not click-driven — ignored."""
        return False

    def on_double_click(self, scene_pt: Point, mods: Mods) -> bool:
        """A drag doesn't handle double-clicks."""
        return False

    def on_move(self, scene_pt: Point, mods: Mods) -> None:
        """Translate members + wires to follow the cursor, then classify.

        No-op until the cursor has travelled at least the start threshold from
        the press point — so a press with sub-pixel jitter then release stays a
        click (see legacy bug 2026-06-01).
        """
        if not self._threshold_crossed:
            dx = scene_pt.x - self._press_pt.x
            dy = scene_pt.y - self._press_pt.y
            if dx * dx + dy * dy < _DRAG_START_THRESHOLD * _DRAG_START_THRESHOLD:
                return
            self._threshold_crossed = True

        self._settle_at(scene_pt)

    def nudge_to(self, target: Point) -> None:
        """Keyboard analogue of :meth:`on_move`: settle the drag at *target*.

        Used by an armed keyboard-move (arrows / WSAD) — there is no cursor and
        no start threshold to cross, so this marks the drag started and settles
        directly.  ``target`` is the desired anchor *grab point*; the grab offset
        is applied inside :meth:`_settle_at` exactly as for the mouse, so the
        same snap / classify / preview pipeline runs.
        """
        self._threshold_crossed = True
        # The mouse path snapshots the pre-move placements in on_release (the
        # _floating_orig the commit reads).  Keyboard-move never releases, so
        # snapshot here on the first nudge — from the *original* positions, not
        # the already-nudged ones — so confirm_floating can build the command.
        if not self._floating_orig:
            self._floating_orig = {
                cid: pl
                for cid in self._member_ids
                if (pl := self._original_placement(cid)) is not None
            }
        self._settle_at(target)

    def _settle_at(self, scene_pt: Point) -> None:
        """Place the drag set at *scene_pt*, re-sync the index, re-classify.

        The single source of truth for "where is the drag right now": snap the
        anchor, translate every member + wire by the shared delta, refresh the
        trial PadIndex, classify the conflict, and report the live decision.
        Both :meth:`on_move` and :meth:`on_release` call it, so the decision the
        release acts on always reflects the *current* cursor position — a
        transient conflict the cursor passed *through* on the way here cannot
        stick and block a clean drop (2026-06-19).
        """
        # Snap the anchor (grab offset applied), then derive one shared delta.
        raw_anchor = Point(scene_pt.x + self._grab[0], scene_pt.y + self._grab[1])
        if self._snap is not None:
            anchor_new = self._snap(raw_anchor, self._member_ids)
        else:
            anchor_new = raw_anchor
        delta = Vector(
            anchor_new.x - self._anchor_pos.x, anchor_new.y - self._anchor_pos.y
        )
        self._last_delta = delta

        self._apply_delta(delta)
        self._resync_trial_pad_index()
        self._last_decision = self._classify()
        if self._preview is not None:
            self._preview(self._last_decision)

    def _apply_delta(self, delta: Vector) -> None:
        """Re-place every member + wire from its origin offset by *delta*."""
        for cid, orig in self._orig_positions.items():
            pl = self._ctx.layout.placements.get(cid)
            if pl is not None:
                pl.position = Point(orig.x + delta.x, orig.y + delta.y)
        for wire in self._ctx.layout.wires:
            orig_path = self._orig_wire_paths.get(wire.id)
            if orig_path is not None:
                wire.path = [Point(p.x + delta.x, p.y + delta.y) for p in orig_path]

    def _resync_trial_pad_index(self) -> None:
        """Clone the session PadIndex once, then re-sync moved components.

        Mirrors legacy ``_ensure_drag_pad_index`` — the trial index reflects the
        dragged positions so the detector's pad questions are answered against
        the hypothetical post-drag board.  Best-effort: on any failure the
        detector falls back to its from-layout paths.
        """
        if not self._member_ids:
            return
        try:
            from protoboard.domain.board.pad_state import PadIndex

            if self._trial_pad_index is None:
                self._trial_pad_index = self._ctx.pad_index().clone()  # type: ignore[attr-defined]
            assert isinstance(self._trial_pad_index, PadIndex)
            for cid in self._member_ids:
                pl = self._ctx.layout.placements.get(cid)
                inst = self._ctx.instances.get(cid)
                if pl is None or inst is None:
                    continue
                defn = self._ctx.components.get(inst.definition_ref)
                if defn is None:
                    continue
                self._trial_pad_index.remove_component(cid)
                self._trial_pad_index.add_component(
                    cid, pl, defn, schematic=self._ctx.schematic
                )
        except Exception as exc:  # noqa: BLE001 — best-effort; fall back to layout
            _log.warning("drag.pad_index.refresh_failed", error=str(exc))
            self._trial_pad_index = None

    def _classify(self) -> DragCommitDecision | None:
        """Run the shared classifier over the current (post-drag) layout."""
        from protoboard.gui.placement_conflict import classify_drag_commit

        try:
            return classify_drag_commit(
                session=self._ctx,  # type: ignore[arg-type]
                moved_comp_ids=set(self._member_ids),
                dragged_wire_ids=self._dragged_wire_ids,
                drag_set_members=self._member_ids,
            )
        except Exception as exc:  # noqa: BLE001 — classifier is best-effort
            _log.warning("drag.classify_failed", error=str(exc))
            return None

    # -- MouseInputController: release / events / finished -----------------

    def on_release(self, scene_pt: Point, mods: Mods) -> None:
        """Finish the drag on release.

        Restores the world and marks the interaction a no-op click when the
        gesture never crossed its start threshold.  On a real drag, resolves the
        conflict decision: a committable drop (CLEAR or SOFT) under auto-accept
        commits now; a HARD drop (body-on-body, cross-net collision) stays
        *floating* and waits for the user to move somewhere legal or cancel.
        """
        if not self._threshold_crossed:
            self._restore_origin()
            self._phase = FloatingPhase.NONE
            self._was_click = True
            return

        # Re-classify for the *release* position, not whatever the last on_move
        # left in ``_last_decision``.  Qt may deliver the release a hair off the
        # final move, and — more importantly — a conflict the cursor merely
        # passed through must not stick and block a drop on a now-clear spot.
        self._settle_at(scene_pt)
        decision = self._last_decision

        # Snapshot the floating origin (for revert) before any further mutation.
        self._floating_orig = {
            cid: pl.copy()
            for cid in self._member_ids
            if (pl := self._original_placement(cid)) is not None
        }

        # MINIMUM auto-accept (2026-06-19): a committable drop (CLEAR *or* SOFT)
        # applies straight away — SOFT's auto-fixes (drop skewed wires, create
        # nets, disconnect cross-group) run through the resolvers below.  Only a
        # HARD drop is held floating, since the user genuinely cannot accept it.
        committable = decision is not None and decision.can_commit
        if committable and self._auto_accept_clear:
            self.confirm_floating()
            return
        self._phase = FloatingPhase.FLOATING

    def on_event(self, intent: Intent) -> bool:
        """React to a semantic intent — the single path for confirm / cancel.

        CANCEL aborts the drag at *any* stage (mid-drag or floating); CONFIRM
        only means something while floating.  The tool routes both here rather
        than calling :meth:`cancel` / :meth:`confirm_floating` directly, so the
        intent → action mapping lives in one place.
        """
        if self.finished:
            return False
        if intent is Intent.CANCEL:
            self.cancel()
            return True
        if intent is Intent.CONFIRM and self._phase is FloatingPhase.FLOATING:
            self.confirm_floating()
            return True
        return False

    @property
    def finished(self) -> bool:
        """True once the drag committed, was cancelled, or was a sub-threshold click.

        A FLOATING drag is NOT finished — it waits for a CONFIRM / CANCEL intent.
        """
        return (
            self._phase is FloatingPhase.COMMITTED
            or self._cancelled
            or self._was_click
        )

    @property
    def was_click(self) -> bool:
        """True when release fired below the start threshold (no move happened)."""
        return self._was_click

    def confirm_floating(self) -> DragOutcome:
        """Apply the move — build + execute :class:`MoveDragSetCommand`.

        Resolves the per-bucket wire-removal preferences (free / stationary-group
        / cross-group-disconnect) through the injected resolvers; a resolver
        returning ``None`` (user cancelled an ask-dialog, or a ``block``
        preference) aborts the commit and leaves the drag floating.
        """
        decision = self._last_decision
        delta = self._last_delta
        if decision is None or delta is None:
            self.cancel()
            return DragOutcome(phase=FloatingPhase.NONE)

        drops = self._resolve_drops(decision)
        if drops is None:
            return DragOutcome(phase=FloatingPhase.FLOATING, severity=decision.severity)
        dropped, reroute_nets = drops

        cmd = self._build_move_command(delta, dropped)
        if cmd is None:
            self.cancel()
            return DragOutcome(phase=FloatingPhase.NONE)

        if self._execute is not None and not self._execute(cmd):
            # Command rejected — leave floating so the user can retry / cancel.
            return DragOutcome(phase=FloatingPhase.FLOATING, severity=decision.severity)

        self._phase = FloatingPhase.COMMITTED
        self._trial_pad_index = None
        outcome = DragOutcome(
            phase=FloatingPhase.COMMITTED,
            severity=decision.severity,
            dropped_wire_count=len(dropped),
            reroute_net_ids=frozenset(reroute_nets),
        )
        self._last_outcome = outcome
        return outcome

    def _resolve_drops(
        self, decision: DragCommitDecision
    ) -> tuple[set[uuid.UUID], set[uuid.UUID]] | None:
        """Run the three preference resolvers; merge the kept drops + reroutes."""
        prefs = self._prefs
        free_pref = None if prefs is None else prefs.free_wire
        sgroup_pref = None if prefs is None else prefs.stationary_group_wire
        disc_pref = None if prefs is None else prefs.cross_group_disconnect
        free_ids = frozenset(decision.free_drop_wire_ids)
        sgroup_ids = frozenset(decision.stationary_group_drop_wire_ids)
        disc_ids = frozenset(decision.cross_group_disconnect_wire_ids)

        free = self._run_resolver(self._resolve_free, free_pref, free_ids)
        if free is None:
            return None
        sgroup = self._run_resolver(self._resolve_sgroup, sgroup_pref, sgroup_ids)
        if sgroup is None:
            return None
        disc = self._run_resolver(self._resolve_disconnect, disc_pref, disc_ids)
        if disc is None:
            return None

        dropped = free[0] | sgroup[0] | disc[0]
        reroute = free[1] | sgroup[1] | disc[1]
        return dropped, reroute

    @staticmethod
    def _run_resolver(
        resolver: ResolveFreeFn | None,
        preference: str | None,
        wire_ids: frozenset[uuid.UUID],
    ) -> tuple[set[uuid.UUID], set[uuid.UUID]] | None:
        """Resolve one bucket; default to dropping all when no resolver wired."""
        if not wire_ids:
            return set(), set()
        if resolver is None or preference is None:
            return set(wire_ids), set()  # headless default: auto-remove
        return resolver(preference, wire_ids)

    def _build_move_command(
        self, delta: Vector, dropped: set[uuid.UUID]
    ) -> Command[ComponentState] | None:
        """Assemble the atomic :class:`MoveDragSetCommand` for this drag."""
        from protoboard.domain.commands.drag_cmds import MoveDragSetCommand

        members: dict[
            uuid.UUID, tuple[ComponentPlacement, ComponentPlacement]
        ] = {}
        for cid in self._member_ids:
            new_pl = self._ctx.layout.placements.get(cid)
            old_pl = self._floating_orig.get(cid)
            if new_pl is None or old_pl is None:
                continue
            members[cid] = (old_pl, new_pl)
        if not members:
            return None

        decision = self._last_decision
        internal = (
            decision.wire_classification.travels_with_drag
            if decision is not None and decision.wire_classification is not None
            else self._dragged_wire_ids
        )
        return MoveDragSetCommand(
            members=members,
            internal_wire_ids=frozenset(internal),
            dropped_wire_ids=frozenset(dropped),
            delta=delta,
        )

    # -- cancel -------------------------------------------------------------

    def cancel(self) -> None:
        """Abort the drag — restore every member + wire to its press-time state."""
        self._restore_origin()
        self._phase = FloatingPhase.NONE
        self._cancelled = True
        self._trial_pad_index = None
        if self._preview is not None:
            self._preview(None)

    def _restore_origin(self) -> None:
        for cid, orig in self._orig_positions.items():
            pl = self._ctx.layout.placements.get(cid)
            if pl is not None:
                pl.position = orig
        for wire in self._ctx.layout.wires:
            orig_path = self._orig_wire_paths.get(wire.id)
            if orig_path is not None:
                wire.path = list(orig_path)

    # -- helpers ------------------------------------------------------------

    def _original_placement(self, cid: uuid.UUID) -> ComponentPlacement | None:
        """Reconstruct the press-time placement (current pl, but at orig pos)."""
        pl = self._ctx.layout.placements.get(cid)
        orig = self._orig_positions.get(cid)
        if pl is None or orig is None:
            return None
        snap = pl.copy()
        snap.position = orig
        return snap
