# SPDX-License-Identifier: MIT
"""A headless CLI front-end driving the same :class:`EditController` verbs as the
GUI — the MVC acceptance harness (Qt-free except a PDF/PNG export device).

The driver holds the interaction state the stateless controller deliberately
does not know — the current selection (``set[UUID]``) and the active group — and
maps a line grammar onto the verbs:

    new perfboard 20 30
    place R at 3,5
    place NE555 at 4,4 rot 90 pkg DIP-8
    select R1 R2
    rotate sel 90            rotate sel 90 pivot 3,5
    flip sel
    delete sel
    group sel as Amp
    wire top bare 2,2 5,2
    autoroute                autoroute NetA NetB
    drc
    save out/proj
    export pdf out.pdf
    undo                     redo

Components are addressed by refdes (``R1``); the driver resolves them to UUIDs.
That the journey ``new → place → wire → autoroute → drc → save → export`` is
fully expressible here — while the driver holds the *only* interaction state —
is the proof of the M/V/C split: the verbs are the same ones the GUI tools call.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from protoboard.controller.editing.headless_session import HeadlessSession
from protoboard.controller.editing.item_handle import ItemId
from protoboard.controller.editing.placement_conflict import (
    PadConflictRequest,
    PlacementPrompts,
    WireConflictAction,
    WireConflictRequest,
)
from protoboard.domain.layout.layout import ROOT_GROUP_ID
from protoboard.domain.routing_settings import RoutingSettings
from protoboard.geometry import Point
from protoboard.paths import BUILTIN_LIBRARY_DIR
from protoboard.services.library.loader import load_library

if TYPE_CHECKING:
    from protoboard.domain.board.board import Board
    from protoboard.domain.schematic.net import Net


class CliError(Exception):
    """A user-facing CLI grammar / resolution error."""


# Batch prompt policy: accept soft pad conflicts, cancel hard wire conflicts,
# and merge two-net wire joins under the first net's name. A real interactive
# CLI would drive these through readline; batch runs need a deterministic one.
def _accept_pad(_req: PadConflictRequest) -> bool:
    return True


def _cancel_wire(_req: WireConflictRequest) -> WireConflictAction | None:
    return None


def _merge_first(net_a: Net, _net_b: Net) -> str | None:
    return net_a.name


_BATCH_PROMPTS = PlacementPrompts(pad=_accept_pad, wire=_cancel_wire)


@dataclass
class CliDriver:
    """Owns a :class:`HeadlessSession` and the active-group interaction state.

    The selection is the session's own model-side selection (``ItemId`` set) —
    the same one ``EditController``'s selection-relative verbs read; the CLI no
    longer keeps a private copy. Refdes tokens address components (the CLI's
    only nameable entity); a bare ``sel`` reuses the current session selection.
    """

    session: HeadlessSession
    active_group: uuid.UUID = ROOT_GROUP_ID

    @classmethod
    def new(cls, kind: str = "perfboard", rows: int = 20, cols: int = 30) -> CliDriver:
        library = load_library(
            [BUILTIN_LIBRARY_DIR] if BUILTIN_LIBRARY_DIR.exists() else []
        )
        board = _make_board(kind, rows, cols)
        return cls(HeadlessSession(board=board, library=library))

    # -- refdes <-> id resolution ------------------------------------------

    def _resolve(self, refdes: str) -> uuid.UUID:
        for cid, inst in self.session.instances.items():
            if inst.refdes == refdes:
                return cid
        raise CliError(f"no component with refdes {refdes!r}")

    def _sel_or_refs(self, tokens: list[str]) -> set[ItemId]:
        """The addressed items as ItemIds: a bare ``sel`` reuses the session
        selection, otherwise each refdes token resolves to a component."""
        if tokens == ["sel"]:
            return set(self.session.selection())
        return {ItemId.component(self._resolve(t)) for t in tokens}

    # -- command dispatch ---------------------------------------------------

    def execute(self, line: str) -> str:
        """Run one grammar line; returns a short human-readable result string."""
        parts = line.split()
        if not parts or parts[0].startswith("#"):
            return ""
        cmd, args = parts[0], parts[1:]
        handler = getattr(self, f"_cmd_{cmd}", None)
        if handler is None:
            raise CliError(f"unknown command: {cmd!r}")
        result: str = handler(args)
        return result

    def run(self, lines: list[str]) -> None:
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            self.execute(line)

    # -- verbs --------------------------------------------------------------

    def _cmd_new(self, args: list[str]) -> str:
        kind = args[0] if args else "perfboard"
        rows = int(args[1]) if len(args) > 1 else 20
        cols = int(args[2]) if len(args) > 2 else 30
        self.session = HeadlessSession(
            board=_make_board(kind, rows, cols),
            library=self.session.library,
        )
        self.active_group = ROOT_GROUP_ID
        return f"new {kind} {rows}x{cols}"

    def _cmd_place(self, args: list[str]) -> str:
        # place <REF> at R,C [rot D] [pkg NAME]
        ref = args[0]
        opts = _parse_opts(args[1:])
        row, col = _parse_rc(opts["at"])
        resolved = self.session.edit_controller.resolve_placement(
            ref,
            position=Point(float(row), float(col)),
            rotation=int(opts.get("rot", "0")),
            package_variant=opts.get("pkg"),
            prompts=_BATCH_PROMPTS,
        )
        if resolved is None:
            raise CliError(f"placement of {ref!r} blocked")
        cid = self.session.edit_controller.place_component(resolved)
        self.session.select({ItemId.component(cid)})
        return f"placed {resolved.instance.refdes}"

    def _cmd_select(self, args: list[str]) -> str:
        sel = {ItemId.component(self._resolve(r)) for r in args}
        self.session.select(sel)
        return f"selected {len(sel)}"

    def _cmd_rotate(self, args: list[str]) -> str:
        # rotate sel D [pivot R,C]
        ids = self._sel_or_refs(args[:1])
        delta = int(args[1])
        pivot = None
        if len(args) >= 4 and args[2] == "pivot":
            r, c = _parse_rc(args[3])
            pivot = Point(float(r), float(c))
        self.session.edit_controller.rotate(
            ids,
            delta_deg=delta,
            pivot=pivot,
            active_group_id=self.active_group,
        )
        return f"rotated {len(ids)} by {delta}"

    def _cmd_flip(self, args: list[str]) -> str:
        ids = self._sel_or_refs(args[:1])
        self.session.edit_controller.flip(ids, active_group_id=self.active_group)
        return f"flipped {len(ids)}"

    def _cmd_delete(self, args: list[str]) -> str:
        ids = self._sel_or_refs(args[:1])
        ran = self.session.edit_controller.delete(
            ids,
            active_group_id=self.active_group,
        )
        self.session.select(self.session.selection() - ids)
        return f"deleted {len(ids)}" if ran else "nothing deleted"

    def _cmd_group(self, args: list[str]) -> str:
        # group <sel|REF...> [as NAME]
        name: str | None = None
        if "as" in args:
            i = args.index("as")
            sel_tokens, name = args[:i], (args[i + 1] if i + 1 < len(args) else None)
        else:
            sel_tokens = args
        ids = self._sel_or_refs(sel_tokens)
        gid = self.session.edit_controller.create_group(
            ids,
            active_group_id=self.active_group,
            name=name,
        )
        if gid is None:
            raise CliError("group: empty selection")
        return f"grouped {len(ids)} as {name or gid}"

    def _cmd_wire(self, args: list[str]) -> str:
        # wire SIDE insulated|bare R,C R,C ...
        side = args[0]
        insulated = args[1] == "insulated"
        pts = [Point(*(float(v) for v in _parse_rc(tok))) for tok in args[2:]]
        wire_id = self.session.edit_controller.add_wire(
            pts,
            insulated=insulated,
            side=side,
            merge_prompt=_merge_first,
        )
        return "wired" if wire_id is not None else "wire cancelled"

    def _cmd_autoroute(self, args: list[str]) -> str:
        net_ids: set[uuid.UUID] | None = None
        if args:
            by_name = {n.name: n.id for n in self.session.schematic.nets.values()}
            net_ids = {by_name[name] for name in args if name in by_name}
        result = self.session.edit_controller.autoroute(
            net_ids,
            settings=RoutingSettings(),
        )
        applied = self.session.edit_controller.apply_autoroute(result)
        return f"autorouted {applied} wire(s)"

    def _cmd_drc(self, _args: list[str]) -> str:
        issues = self.session.edit_controller.run_drc()
        return f"drc: {len(issues)} issue(s)"

    def _cmd_save(self, args: list[str]) -> str:
        self.session.edit_controller.save(Path(args[0]))
        return f"saved {args[0]}"

    def _cmd_export(self, args: list[str]) -> str:
        # export KIND PATH
        self.session.edit_controller.export(args[0], Path(args[1]))
        return f"exported {args[0]} → {args[1]}"

    def _cmd_undo(self, _args: list[str]) -> str:
        if self.session.stack.can_undo():
            self.session.stack.undo(self.session)
            self.session.notify_changed()
        return "undo"

    def _cmd_redo(self, _args: list[str]) -> str:
        if self.session.stack.can_redo():
            self.session.stack.redo(self.session)
            self.session.notify_changed()
        return "redo"


def _make_board(kind: str, rows: int, cols: int) -> Board:
    from protoboard.domain.board.templates import make_perfboard, make_stripboard

    if kind == "perfboard":
        return make_perfboard(rows, cols)
    if kind == "stripboard":
        return make_stripboard(rows, cols)
    raise CliError(f"unknown board kind: {kind!r}")


def _parse_rc(token: str) -> tuple[int, int]:
    try:
        r, c = token.split(",")
        return int(r), int(c)
    except ValueError as e:
        raise CliError(f"expected R,C got {token!r}") from e


def _parse_opts(tokens: list[str]) -> dict[str, str]:
    """Parse ``at 3,5 rot 90 pkg DIP-8`` into ``{at:'3,5', rot:'90', ...}``."""
    opts: dict[str, str] = {}
    it = iter(tokens)
    for key in it:
        try:
            opts[key] = next(it)
        except StopIteration as e:
            raise CliError(f"option {key!r} missing a value") from e
    return opts


__all__ = ["CliDriver", "CliError"]
