# SPDX-License-Identifier: MIT
"""``ArmedTransform`` — the Qt-free armed rotate/move/flip core.

The unified float-transform mode (merges the old ``ArmedRotate`` /
``ArmedMove``).  Armed on a selection, it holds a single *float state*:

* ``committed_original`` — a deep copy of each member's placement at arm
  time, plus the original geometry of every wire/free-knot the transform
  touches, read from the **live** layout.  Snapshotted **once** and never
  changed for the session; it is the baseline both ``cancel`` and the commit's
  undo restore.
* ``current`` — a **preview** ``Layout`` (``live.preview_copy()``), mutated by
  each operation (rotate / move / flip).  The live session's layout is never
  touched mid-gesture (E5) — the preview carries the transformed pose, and the
  renderer draws *it* instead of the live one.

Operations apply directly to the preview (rotate via ``_rotation_about``, move
by translating placements + owned wires).  Because every op writes the same
preview layout, the user can freely *mix* rotate and move — and a conflict
raised by one can be cleared by the other, which is the whole point.  Snap and
conflict classification see the previewed pose through a
:class:`~protoboard.controller.editing.preview_state.PreviewState` (the live
session with the preview layout swapped in).

``accept`` diffs ``committed_original → current`` into one
:class:`TransformDragSetCommand` (optionally wrapped with a
:class:`DropWiresCommand` for soft-conflict drops) — a single undo entry.
Because the live layout stayed pristine, the command's ``apply`` is the *only*
mutation of it (no rewind needed).  ``cancel`` simply drops the preview.  Both
honour the conditional-disarm contract: an un-committable ``accept`` (HARD
conflict) stays armed.

Pure application logic: this class imports zero Qt.  Every Qt seam (snapping a
raw pointer, executing the built command, resolving key chords, drawing the
preview, showing status) arrives as an injected callback, so the GUI keeps only
a thin adapter and this core is testable headless.  The controller owns one
(:meth:`EditController.begin_transform`); the GUI ``ArmedTransformTool`` drives
it through the controller's verbs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, cast

from protoboard.domain.layout.placement import (
    ComponentPlacement,
    PlacementMap,
)
from protoboard.geometry import Point, Vector
from protoboard.controller.ports import WheelInput
from protoboard.controller.ports.inputs_details import WheelNotchAccumulator

if TYPE_CHECKING:
    from protoboard.domain.commands.base import Command, ComponentState
    from protoboard.domain.layout.layout import Layout
    from protoboard.domain.layout.wire import Fragment
    from protoboard.geometry import Transform2D
    from protoboard.controller.editing.drag_preferences import DragPreferences
    from protoboard.controller.ports import KeyInput
    from protoboard.controller.selection.drop_resolution import ResolveFn
    from protoboard.controller.tools.drag_commit_decision import DragCommitDecision
    from protoboard.controller.editing.controller import EditState

    SnapFn = Callable[[Point], "Point | None"]
    ExecuteFn = Callable[["Command[ComponentState]"], bool]
    StatusFn = Callable[[str], None]
    Matcher = Callable[["KeyInput", str], bool]


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


class ArmedTransform:
    """Unified float rotate+move sub-tool (an ``ArmedSubTool``)."""

    def __init__(
        self,
        ctx: EditState,
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
        self._wheel = WheelNotchAccumulator()
        # Snapshot the pristine pose once, from the LIVE layout — the baseline
        # for cancel + undo.  Do this *before* forking the preview so the
        # baseline is unarguably the un-transformed pose.
        self._original = self._snapshot_original(self._live_layout)
        # Fork a preview layout: every op mutates THIS, the live one stays
        # pristine (E5).  Reads (snap / classify / render) go through it.
        self._preview_layout = self._live_layout.preview_copy()

    @property
    def _live_layout(self) -> Layout:
        """The session's real :class:`Layout` — the pristine, on-stack pose.

        Accessed through the ``ComponentState`` seam every layout helper uses.
        Only the arm-time snapshot and the committed command touch this; the
        gesture itself never mutates it.
        """
        return cast("ComponentState", self._ctx).layout

    @property
    def _layout(self) -> Layout:
        """The **preview** layout the operations mutate (the float pose).

        Every ``_rotate`` / ``_move`` / ``_flip`` and every pose read
        (snapshot-diff, ``_is_transformed``) goes through here, so the gesture
        is isolated from the live session until ``accept`` commits a command.
        """
        return self._preview_layout

    def preview_layout(self) -> Layout:
        """The live float pose, for the renderer to draw instead of the session's."""
        return self._preview_layout

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

    def _affected_wire_ids(
        self, layout: Layout | None = None
    ) -> frozenset[uuid.UUID]:
        """Wires structurally owned by the members (rotated/translated as a unit).

        A wire is affected when *both* its endpoints anchor to members of this
        transform — those travel rigidly with the pose.  Pin-anchored endpoints
        of half-owned wires resolve lazily, so they need no snapshot.

        Ownership is pose-independent (topology, not geometry), so live and
        preview agree; *layout* lets the arm-time snapshot query the live one
        before the preview exists.  Defaults to the preview.
        """
        from protoboard.domain.layout.group_ownership import find_owned_wires

        layout = layout if layout is not None else self._layout
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

    def _snapshot_original(self, layout: Layout) -> _Original:
        # Snapshot the pristine pose off the LIVE *layout* (passed in — the
        # preview does not exist yet at arm time).
        # Placements are snapshotted in their stored LOCAL (group-relative)
        # frame — writes to the collection store locals verbatim, so restore
        # and the commit command must speak the same frame (a composed world
        # read written back double-composes for any non-identity group).
        placements: dict[uuid.UUID, ComponentPlacement] = {}
        for cid in self._comp_ids:
            pl = layout.placements.local_get(cid)
            if isinstance(pl, ComponentPlacement):
                placements[cid] = pl.copy()
        wire_paths: dict[uuid.UUID, list[Point]] = {}
        wire_fragments: dict[uuid.UUID, list[Fragment]] = {}
        knot_positions: dict[uuid.UUID, Point] = {}
        for wid in self._affected_wire_ids(layout):
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

    def on_key(self, inp: KeyInput) -> bool:
        # Accept / cancel do NOT live here: the owner (SelectTool) translates
        # Enter / Esc through its Intent map and calls accept() / cancel().
        if self._matches(inp, _ACT_FLIP):
            self._flip()
            return True
        # [R] arms the rotate sub-mode; pressed *while already* rotating it
        # steps CW.  [G] arms move.  Switching sub-mode never commits and
        # keeps the float pose (it just re-aims the arrow keys).
        if self._matches(inp, _ACT_ROTATE):
            if not self._enter_mode("rotate"):
                self._rotate(90)
            return True
        if self._matches(inp, _ACT_MOVE):
            self._enter_mode("move")
            return True
        # Arrow / WASD keys: rotate-step (up = CW, down = CCW) or nudge,
        # by the active sub-mode.
        step = self._step_for(inp)
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

    def _step_for(self, inp: KeyInput) -> tuple[float, float] | None:
        for action_id, vec in _STEP_VECTORS.items():
            if self._matches(inp, action_id):
                return vec
        return None

    def wants_wheel(self, inp: WheelInput) -> bool:
        return True  # the wheel rotates

    def on_wheel(self, inp: WheelInput) -> bool:
        notches = self._wheel.add(inp.delta_y)
        for _ in range(abs(notches)):
            self._rotate(90 if notches > 0 else -90)
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
    #
    # Every op is a WORLD-frame rigid transform O.  Placements store the
    # member's LOCAL (group-relative) pose and compose to world on read, so
    # writing the op means conjugating it into each member's frame:
    # ``local' = (W⁻¹ ∘ O ∘ W) · local`` where W is the member's composed
    # ancestor-group transform.  For a loose component (or an identity
    # group) W = I and this degenerates to applying O directly — the
    # historical behaviour.  Wires are stored in world coordinates, so their
    # geometry takes O verbatim.

    def _write_world_op(self, cid: uuid.UUID, op: Transform2D) -> None:
        """Store the local-frame equivalent of world op *op* for member *cid*."""
        from protoboard.domain.layout.group_tree import GroupTree
        from protoboard.domain.layout.placement import compose_placement

        layout = self._layout
        local = layout.placements.local_get(cid)
        if not isinstance(local, ComponentPlacement):
            return
        w = GroupTree(getattr(layout, "groups", {})).world_transform(cid)
        conj = w.inverse().compose(op).compose(w)
        layout.placements[cid] = compose_placement(conj, local).copy()

    def _transform_wires(self, op: Transform2D) -> None:
        """Apply world op *op* to the owned wires' geometry + free endpoints."""
        from protoboard.domain.layout.wire import transform_wire_geometry

        layout = self._layout
        for wid in self._affected_wire_ids():
            wire = layout.wires.get(wid)
            if wire is None:
                continue
            transform_wire_geometry(wire, op.apply)
            for ref in (wire.a, wire.b):
                if ref is None:
                    continue
                knot = layout.knots.get(ref.id)
                pos = getattr(knot, "position", None)
                if isinstance(pos, Point) and knot is not None:
                    layout.knots[ref.id] = knot.moved_to(op.apply(pos))

    def _rotate(self, delta: int) -> None:
        from protoboard.domain.commands.group_cmds import _rotation_about

        if self._pivot is not None:
            op = _rotation_about(self._pivot, delta)
            for cid in self._comp_ids:
                self._write_world_op(cid, op)
            self._transform_wires(op)
        else:
            # Per-object: rotate each about its own current WORLD centre
            # (wires follow each member's op — parity with the historical
            # per-cid rotate_components_around calls).
            for cid in self._comp_ids:
                pl = self._layout.placements.get(cid)
                if not isinstance(pl, ComponentPlacement):
                    continue
                stepped = self._smd_diagonal_step(cid, pl, delta)
                if stepped is not None:
                    self._layout.placements[cid] = stepped
                    continue
                op = _rotation_about(pl.position, delta)
                self._write_world_op(cid, op)
                self._transform_wires(op)
        self._layout.invalidate_interior_vertex_index()
        self._emit_preview()

    def _smd_diagonal_step(
        self, cid: uuid.UUID, pl: ComponentPlacement, delta: int
    ) -> ComponentPlacement | None:
        """The 45° orientation step for a lone 2-pin SMD chip, or ``None``.

        The 45° step is defined solely about the part's own pin-1 anchor
        (decided 2026-07-27), so it applies only in per-object mode on a
        single-component selection.  Group members are excluded: they store
        group-relative locals, which the canonical world-frame rewrite would
        corrupt — they keep the plain 90° step.  ``None`` → caller falls back
        to the 90° rotation op.
        """
        from protoboard.domain.components.package.reach import (
            is_two_pin_smd,
            smd_orientation_step,
        )
        from protoboard.domain.layout.group_tree import GroupTree
        from protoboard.domain.layout.pad_conflict import (
            resolved_footprint_or_default,
        )
        from protoboard.geometry import Transform2D

        if len(self._comp_ids) != 1:
            return None
        tree = GroupTree(getattr(self._layout, "groups", {}))
        if tree.world_transform(cid) != Transform2D.identity():
            return None
        inst = self._ctx.instances.get(cid)
        if inst is None:
            return None
        defn = self._ctx.components.get(inst.definition_ref)
        if defn is None:
            return None
        families = getattr(self._ctx, "package_families", None)
        if not is_two_pin_smd(defn, inst, families):
            return None
        fp = resolved_footprint_or_default(defn, inst)
        return smd_orientation_step(fp, pl, 1 if delta > 0 else -1)

    def _move(self, dx: float, dy: float) -> None:
        from protoboard.geometry import Transform2D

        op = Transform2D.translation(Vector(dx, dy))
        for cid in self._comp_ids:
            self._write_world_op(cid, op)
        self._transform_wires(op)
        self._layout.invalidate_interior_vertex_index()
        self._emit_preview()

    def _flip(self) -> None:
        """Flip 2-pin members in place (keep the existing pin-count gate)."""
        layout = self._layout
        for cid in self._comp_ids:
            pl = layout.placements.local_get(cid)
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
        count = cast(Callable[[], int] | None, getattr(fp, "pin_count", None))
        return count() if count is not None else 0

    # -- commit-readiness + terminal ---------------------------------------

    def _classify(self) -> DragCommitDecision | None:
        from protoboard.controller.editing.drag_commit import (
            classify_drag_commit,
        )
        from protoboard.controller.editing.preview_state import PreviewState

        if not self._is_transformed():
            return None
        try:
            # Classify against the PREVIEW pose — the live layout is pristine,
            # so the moved-component conflict must be read through the preview
            # (its PadIndex / snapshot come off ``session.layout``).
            return classify_drag_commit(
                session=PreviewState(self._ctx, self._preview_layout),
                moved_comp_ids=set(self._comp_ids),
                dragged_wire_ids=self._affected_wire_ids(),
                drag_set_members=self._comp_ids,
            )
        except Exception:  # noqa: BLE001 — classifier best-effort
            return None

    def _is_transformed(self) -> bool:
        """True when current placements differ from the original snapshot."""
        for cid, orig in self._original.placements.items():
            cur = self._layout.placements.local_get(cid)
            if not isinstance(cur, ComponentPlacement):
                continue
            if (
                cur.position != orig.position
                or cur.rotation != orig.rotation
                or cur.side != orig.side
                or cur.vertical_flipped != orig.vertical_flipped
                # A 2-pin SMD 45° step changes only the pin-2 offset.
                or cur.pin_offsets != orig.pin_offsets
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
        # The live layout is still at the original pose (the gesture only ever
        # touched the preview), so the command's apply — original → preview — is
        # the authoritative and only mutation of it.  No rewind needed.
        self._finished = True
        if cmd is not None:
            self._execute(cmd)
        if self._status is not None:
            self._status("Transform applied")

    def cancel(self) -> None:
        # Nothing to undo: the live layout was never mutated, only the preview,
        # which is dropped when the sub-tool finishes.  No preview emit — the
        # controller clears the armed transform right after this returns and the
        # owning tool refreshes from the (pristine) live layout.
        self._finished = True
        if self._status is not None:
            self._status("Transform cancelled")

    # -- command assembly ---------------------------------------------------

    def _build_commit_command(
        self, decision: DragCommitDecision | None
    ) -> Command[ComponentState] | None:
        from protoboard.domain.commands.drag_cmds import TransformDragSetCommand

        layout = self._layout
        members: dict[
            uuid.UUID, tuple[ComponentPlacement, ComponentPlacement]
        ] = {}
        # Local-frame diff — TransformDragSetCommand writes placements
        # verbatim (i.e. locals), so both ends of the snapshot must be local.
        for cid, old_pl in self._original.placements.items():
            new_pl = layout.placements.local_get(cid)
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
        from protoboard.controller.selection.drop_resolution import resolve_drops

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

    # -- preview ------------------------------------------------------------

    def _emit_preview(self) -> None:
        if self._preview is not None:
            self._preview(self._classify())
