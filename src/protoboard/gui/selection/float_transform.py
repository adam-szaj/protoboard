# SPDX-License-Identifier: MIT
"""``FloatTransform`` — one armed sub-tool for rotate **and** move.

The unified float-transform mode (merges the old ``ArmedRotate`` /
``ArmedMove``).  Armed on a selection, it holds a single *float state*:

* ``committed_original`` — a deep copy of each member's placement at arm
  time, plus the original geometry of every wire/free-knot the transform
  touches.  Snapshotted **once** and never changed for the session; it is the
  baseline both ``cancel`` and the commit's undo restore.
* ``current`` — the live ``layout`` itself, mutated off the undo stack by each
  operation (rotate / move / flip).

Operations apply directly to ``current`` (rotate via
:func:`rotate_components_around`, move by translating placements + owned
wires).  Because every op writes the shared layout, the user can freely
*mix* rotate and move — and a conflict raised by one can be cleared by the
other, which is the whole point.

``accept`` diffs ``committed_original → current`` into one
:class:`TransformDragSetCommand` (optionally wrapped with a
:class:`DropWiresCommand` for soft-conflict drops) — a single undo entry.
``cancel`` restores ``committed_original`` — the pose *before any* operation,
never an intermediate one.  Both honour the conditional-disarm contract: an
un-committable ``accept`` (HARD conflict) stays armed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, cast

from PySide6.QtCore import Qt

from protoboard.domain.layout.placement import (
        ComponentPlacement,
        PlacementMap,
    )
from protoboard.geometry import Point, Vector

if TYPE_CHECKING:
    from PySide6.QtGui import QKeyEvent, QWheelEvent

    from protoboard.domain.commands.base import Command, ComponentState
    from protoboard.domain.layout.layout import Layout
    from protoboard.domain.layout.wire import Fragment
    from protoboard.gui.drag_preferences import DragPreferences
    from protoboard.gui.selection.drag_controller import DragContext
    from protoboard.gui.selection.drop_resolution import ResolveFn
    from protoboard.gui.tools.drag_commit_decision import DragCommitDecision

    SnapFn = Callable[[Point], "Point | None"]
    ExecuteFn = Callable[["Command[ComponentState]"], bool]
    StatusFn = Callable[[str], None]
    Matcher = Callable[["QKeyEvent", str], bool]


# One mouse-wheel detent in Qt ``angleDelta`` units (15° × 8).
_WHEEL_NOTCH = 120

# Action ids this sub-tool reacts to (resolved against SelectTool's KeyMap via
# the injected ``matches`` callback — same ids the old ArmedRotate/ArmedMove
# used).  ``[R]`` arms/steps rotate, ``[G]`` arms move, ``[F]``-family flips.
_ACT_ROTATE = "select-tool.action.rotate"
_ACT_MOVE = "select-tool.action.move"
_ACT_FLIP = "select-tool.action.flip"
# Arrow / WASD nudge ids → unit grid step; in rotate sub-mode up = CW, down =
# CCW (see ``on_key``), in move sub-mode they translate.
_STEP_VECTORS: dict[str, tuple[float, float]] = {
    "select-tool.action.step_left": (-1.0, 0.0),
    "select-tool.action.step_right": (1.0, 0.0),
    "select-tool.action.step_up": (0.0, -1.0),
    "select-tool.action.step_down": (0.0, 1.0),
}


@dataclass
class _Original:
    """Immutable snapshot of the pre-transform pose (the undo / cancel base)."""

    placements: PlacementMap
    wire_paths: dict[uuid.UUID, list[Point]]
    wire_fragments: dict[uuid.UUID, list[Fragment]]
    knot_positions: dict[uuid.UUID, Point]


class FloatTransform:
    """Unified float rotate+move sub-tool (an ``ArmedSubTool``)."""

    def __init__(
        self,
        ctx: DragContext,
        *,
        comp_ids: frozenset[uuid.UUID],
        group_id: uuid.UUID | None,
        snap: SnapFn,
        execute: ExecuteFn,
        matches: Matcher,
        mode: str = "rotate",
        pivot: Point | None = None,
        preview: Callable[[DragCommitDecision | None], None] | None = None,
        status: StatusFn | None = None,
        preferences: DragPreferences | None = None,
        resolve_free: ResolveFn | None = None,
        resolve_sgroup: ResolveFn | None = None,
        resolve_disconnect: ResolveFn | None = None,
    ) -> None:
        self._ctx = ctx
        self._comp_ids = comp_ids
        self._group_id = group_id
        self._snap = snap
        self._execute = execute
        self._matches = matches
        self._mode = mode  # "rotate" | "move" — which the arrow keys drive
        self._pivot = pivot
        self._preview = preview
        self._status = status
        self._prefs = preferences
        self._resolve_free = resolve_free
        self._resolve_sgroup = resolve_sgroup
        self._resolve_disconnect = resolve_disconnect
        self._finished = False
        self._wheel_accum = 0
        # Snapshot the pristine pose once — the baseline for cancel + undo.
        self._original = self._snapshot_original()

    @property
    def _layout(self) -> Layout:
        """The full :class:`Layout` (via the ``ComponentState`` view).

        ``DragContext.layout`` is a *narrow* protocol (no ``knots`` /
        ``WireCollection``); the live session's layout is a real ``Layout``,
        so access it through the ``ComponentState`` seam every layout helper
        already uses.
        """
        return cast("ComponentState", self._ctx).layout

    # -- introspection ------------------------------------------------------

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def comp_ids(self) -> frozenset[uuid.UUID]:
        return self._comp_ids

    @property
    def pivot(self) -> Point | None:
        return self._pivot

    @property
    def mode(self) -> str:
        """The active sub-mode — ``"rotate"`` or ``"move"``."""
        return self._mode

    def status_hint(self) -> str:
        if self._mode == "rotate":
            piv = "shared pad pivot" if self._pivot is not None else "per-object"
            return (
                f"Rotate ({piv})  ·  wheel/[↑]/[R] CW  ·  [↓] CCW  ·  "
                "[G] move  ·  click a pad = pivot  ·  [Enter] accept  ·  [Esc]"
            )
        return (
            "Move  ·  arrows / WASD nudge  ·  [R] rotate  ·  "
            "[Enter] accept  ·  [Esc] cancel"
        )

    # -- snapshot -----------------------------------------------------------

    def _affected_wire_ids(self) -> frozenset[uuid.UUID]:
        """Wires structurally owned by the members (rotated/translated as a unit).

        A wire is affected when *both* its endpoints anchor to members of this
        transform — those travel rigidly with the pose.  Pin-anchored endpoints
        of half-owned wires resolve lazily, so they need no snapshot.
        """
        from protoboard.domain.layout.group_ownership import find_owned_wires

        layout = self._layout
        groups = getattr(layout, "groups", {})
        if self._group_id is not None and self._group_id in groups:
            from protoboard.domain.layout.group_tree import GroupTree

            tree = GroupTree(groups)
            owned = find_owned_wires(
                list(layout.wires),
                groups[self._group_id],
                wire_net_map=self._ctx.cached_wire_net_map(),
                knots=dict(layout.knots),
                group_comp_ids=tree.all_component_ids(self._group_id),
                schematic=self._ctx.schematic,
            )
            return frozenset(owned)
        return frozenset()

    def _snapshot_original(self) -> _Original:
        layout = self._layout
        placements: dict[uuid.UUID, ComponentPlacement] = {}
        for cid in self._comp_ids:
            pl = layout.placements.get(cid)
            if isinstance(pl, ComponentPlacement):
                placements[cid] = pl.copy()
        wire_paths: dict[uuid.UUID, list[Point]] = {}
        wire_fragments: dict[uuid.UUID, list[Fragment]] = {}
        knot_positions: dict[uuid.UUID, Point] = {}
        for wid in self._affected_wire_ids():
            wire = layout.wires.get(wid)
            if wire is None:
                continue
            wire_paths[wid] = list(wire.path)
            wire_fragments[wid] = list(wire.fragments) if wire.fragments else []
            for ref in (wire.a, wire.b):
                if ref is None:
                    continue
                knot = layout.knots.get(ref.id)
                pos = getattr(knot, "position", None)
                if isinstance(pos, Point):
                    knot_positions[ref.id] = pos
        return _Original(placements, wire_paths, wire_fragments, knot_positions)

    # -- input --------------------------------------------------------------

    def on_key(self, event: QKeyEvent) -> bool:
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.accept()
            return True
        if key == Qt.Key.Key_Escape:
            self.cancel()
            return True
        if self._matches(event, _ACT_FLIP):
            self._flip()
            return True
        # [R] arms the rotate sub-mode; pressed *while already* rotating it
        # steps CW.  [G] arms move.  Switching sub-mode never commits and
        # keeps the float pose (it just re-aims the arrow keys).
        if self._matches(event, _ACT_ROTATE):
            if not self._enter_mode("rotate"):
                self._rotate(90)
            return True
        if self._matches(event, _ACT_MOVE):
            self._enter_mode("move")
            return True
        # Arrow / WASD keys: rotate-step (up = CW, down = CCW) or nudge,
        # by the active sub-mode.
        step = self._step_for(event)
        if step is not None:
            dx, dy = step
            if self._mode == "rotate":
                self._rotate(90 if dy < 0 else -90)
            else:
                self._move(dx, dy)
            return True
        return False

    def switch_mode(self, mode: str) -> None:
        """Public re-aim of the active sub-mode (the ``[R]``/``[G]`` entry
        points call this when a transform is already armed, so the float pose
        is preserved instead of re-arming)."""
        self._enter_mode(mode)

    def _enter_mode(self, mode: str) -> bool:
        """Switch the active sub-mode; return True if it actually changed.

        A changed mode only re-aims the arrow keys (no transform step); an
        unchanged mode returns False so the caller performs its step.
        """
        if self._mode == mode:
            return False
        self._mode = mode
        if self._status is not None:
            self._status(self.status_hint())
        return True

    def _step_for(self, event: QKeyEvent) -> tuple[float, float] | None:
        for action_id, vec in _STEP_VECTORS.items():
            if self._matches(event, action_id):
                return vec
        return None

    def wants_wheel(self, event: QWheelEvent) -> bool:
        return True  # the wheel rotates

    def on_wheel(self, event: QWheelEvent) -> bool:
        dy = event.angleDelta().y()
        if dy == 0:
            return True
        if (dy > 0) != (self._wheel_accum >= 0):
            self._wheel_accum = 0
        self._wheel_accum += dy
        while self._wheel_accum >= _WHEEL_NOTCH:
            self._wheel_accum -= _WHEEL_NOTCH
            self._rotate(90)
        while self._wheel_accum <= -_WHEEL_NOTCH:
            self._wheel_accum += _WHEEL_NOTCH
            self._rotate(-90)
        return True

    def on_click(self, scene_pt: Point) -> bool:
        pad = self._snap(scene_pt)
        if pad is None:
            return False
        same = (
            self._pivot is not None
            and self._pivot.x == pad.x
            and self._pivot.y == pad.y
        )
        self._pivot = None if same else pad
        if self._status is not None:
            self._status(self.status_hint())
        self._emit_preview()
        return True

    # -- operations (mutate the live layout off-stack) ----------------------

    def _rotate(self, delta: int) -> None:
        from protoboard.domain.commands.group_cmds import (
            rotate_components_around,
        )

        state = cast("ComponentState", self._ctx)
        wire_ids = self._affected_wire_ids()
        if self._pivot is not None:
            rotate_components_around(
                state, self._comp_ids, self._pivot, delta, wire_ids=wire_ids,
            )
        else:
            # Per-object: rotate each about its own current centre.
            for cid in self._comp_ids:
                pl = self._layout.placements.get(cid)
                if isinstance(pl, ComponentPlacement):
                    rotate_components_around(
                        state, frozenset({cid}), pl.position, delta,
                        wire_ids=wire_ids,
                    )
        self._emit_preview()

    def _move(self, dx: float, dy: float) -> None:
        from protoboard.domain.layout.wire import transform_wire_geometry

        layout = self._layout
        delta = Vector(dx, dy)
        for cid in self._comp_ids:
            pl = layout.placements.get(cid)
            if isinstance(pl, ComponentPlacement):
                moved = pl.copy()
                moved.position = pl.position + delta
                layout.placements[cid] = moved
        for wid in self._affected_wire_ids():
            wire = layout.wires.get(wid)
            if wire is None:
                continue
            transform_wire_geometry(wire, lambda p: p + delta)
            for ref in (wire.a, wire.b):
                if ref is None:
                    continue
                knot = layout.knots.get(ref.id)
                pos = getattr(knot, "position", None)
                if isinstance(pos, Point) and knot is not None:
                    layout.knots[ref.id] = knot.moved_to(pos + delta)
        layout.invalidate_interior_vertex_index()
        self._emit_preview()

    def _flip(self) -> None:
        """Flip 2-pin members in place (keep the existing pin-count gate)."""
        layout = self._layout
        for cid in self._comp_ids:
            pl = layout.placements.get(cid)
            if not isinstance(pl, ComponentPlacement):
                continue
            defn = self._defn_for(cid)
            if defn is None or self._pin_count(defn) != 2:
                continue
            flipped = pl.copy()
            flipped.vertical_flipped = not pl.vertical_flipped
            layout.placements[cid] = flipped
        self._emit_preview()

    def _defn_for(self, cid: uuid.UUID) -> object | None:
        inst = self._ctx.instances.get(cid)
        if inst is None:
            return None
        return self._ctx.components.get(inst.definition_ref)

    @staticmethod
    def _pin_count(defn: object) -> int:
        fp = getattr(defn, "footprint", None)
        count = getattr(fp, "pin_count", None)
        return count() if callable(count) else 0

    # -- commit-readiness + terminal ---------------------------------------

    def _classify(self) -> DragCommitDecision | None:
        from protoboard.gui.placement_conflict import classify_drag_commit

        if not self._is_transformed():
            return None
        try:
            return classify_drag_commit(
                session=cast("object", self._ctx),  # type: ignore[arg-type]
                moved_comp_ids=set(self._comp_ids),
                dragged_wire_ids=self._affected_wire_ids(),
                drag_set_members=self._comp_ids,
            )
        except Exception:  # noqa: BLE001 — classifier best-effort
            return None

    def _is_transformed(self) -> bool:
        """True when current placements differ from the original snapshot."""
        for cid, orig in self._original.placements.items():
            cur = self._layout.placements.get(cid)
            if not isinstance(cur, ComponentPlacement):
                continue
            if (
                cur.position != orig.position
                or cur.rotation != orig.rotation
                or cur.side != orig.side
                or cur.vertical_flipped != orig.vertical_flipped
            ):
                return True
        return False

    def can_commit(self) -> bool:
        decision = self._classify()
        return decision is None or decision.can_commit

    def accept(self) -> None:
        decision = self._classify()
        if decision is not None and not decision.can_commit:
            # Conditional disarm: a conflicting transform parks armed.
            if self._status is not None:
                self._status(
                    "Transform: conflict here — step to a clear spot or [Esc]"
                )
            self._emit_preview()
            return
        if not self._is_transformed():
            self._finished = True
            if self._status is not None:
                self._status("Transform: no change")
            return
        cmd = self._build_commit_command(decision)
        # The live layout already holds the transformed pose; rewind to the
        # original so the command's apply is the authoritative mutation (and
        # redo replays cleanly).
        self._restore_original()
        self._finished = True
        if cmd is not None:
            self._execute(cmd)
        if self._status is not None:
            self._status("Transform applied")

    def cancel(self) -> None:
        self._restore_original()
        self._finished = True
        if self._status is not None:
            self._status("Transform cancelled")
        self._emit_preview()

    # -- command assembly ---------------------------------------------------

    def _build_commit_command(
        self, decision: DragCommitDecision | None
    ) -> Command[ComponentState] | None:
        from protoboard.domain.commands.drag_cmds import TransformDragSetCommand

        layout = self._layout
        members: dict[
            uuid.UUID, tuple[ComponentPlacement, ComponentPlacement]
        ] = {}
        for cid, old_pl in self._original.placements.items():
            new_pl = layout.placements.get(cid)
            if isinstance(new_pl, ComponentPlacement):
                members[cid] = (old_pl, new_pl.copy())
        if not members:
            return None

        wire_paths: dict[uuid.UUID, tuple[list[Point], list[Point]]] = {}
        wire_fragments: dict[
            uuid.UUID, tuple[list[Fragment], list[Fragment]]
        ] = {}
        for wid, old_path in self._original.wire_paths.items():
            wire = layout.wires.get(wid)
            if wire is None:
                continue
            wire_paths[wid] = (list(old_path), list(wire.path))
            old_frags = self._original.wire_fragments.get(wid, [])
            wire_fragments[wid] = (
                list(old_frags), list(wire.fragments) if wire.fragments else [],
            )
        knot_positions: dict[uuid.UUID, tuple[Point, Point]] = {}
        for kid, old_pos in self._original.knot_positions.items():
            knot = layout.knots.get(kid)
            cur = getattr(knot, "position", None)
            if isinstance(cur, Point):
                knot_positions[kid] = (old_pos, cur)

        transform = TransformDragSetCommand(
            members=members,
            wire_paths=wire_paths,
            wire_fragments=wire_fragments,
            knot_positions=knot_positions,
        )

        dropped = self._resolve_soft_drops(decision)
        if not dropped:
            return transform

        from protoboard.domain.commands.base import MacroCommand
        from protoboard.domain.commands.component_cmds import DropWiresCommand

        n = len(members)
        return MacroCommand(
            [transform, DropWiresCommand(wire_ids=frozenset(dropped))],
            description=f"Transform {n} component{'s' if n != 1 else ''}",
        )

    def _resolve_soft_drops(
        self, decision: DragCommitDecision | None
    ) -> set[uuid.UUID]:
        if decision is None:
            return set()
        from protoboard.gui.selection.drop_resolution import resolve_drops

        resolved = resolve_drops(
            decision,
            preferences=self._prefs,
            resolve_free=self._resolve_free,
            resolve_sgroup=self._resolve_sgroup,
            resolve_disconnect=self._resolve_disconnect,
        )
        if resolved is None:
            return set()
        dropped, _reroute = resolved
        return dropped

    # -- preview / restore --------------------------------------------------

    def _restore_original(self) -> None:
        layout = self._layout
        for cid, pl in self._original.placements.items():
            layout.placements[cid] = pl.copy()
        for wid, path in self._original.wire_paths.items():
            wire = layout.wires.get(wid)
            if wire is not None:
                wire.path = list(path)
        for wid, frags in self._original.wire_fragments.items():
            wire = layout.wires.get(wid)
            if wire is not None:
                wire.fragments = list(frags)
        for kid, pos in self._original.knot_positions.items():
            knot = layout.knots.get(kid)
            if knot is not None:
                layout.knots[kid] = knot.moved_to(pos)
        layout.invalidate_interior_vertex_index()

    def _emit_preview(self) -> None:
        if self._preview is not None:
            self._preview(self._classify())
