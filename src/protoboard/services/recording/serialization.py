# SPDX-License-Identifier: MIT
"""Serialization registry for Command objects.

Each known command type is registered via :func:`_register`, which captures
both a ``to_dict`` and a ``from_dict``.  The dispatcher reads the ``type``
field of a record to find the right deserializer.

Only the *input* parameters of a command are serialized — fields that the
command fills during ``apply()`` for undo (e.g. ``_saved_wire``) are
intentionally omitted, because replay always proceeds forward.
"""

from __future__ import annotations

import uuid
from typing import Any, Callable

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import StripAtom
from protoboard.domain.commands.base import Command, MacroCommand
from protoboard.domain.commands.board_cmds import (
    ResizeBoardCommand,
    SplitStripCommand,
)
from protoboard.domain.commands.component_cmds import (
    AddWireCommand,
    AddWireWithNetsCommand,
    ConvertEndpointToWirePinCommand,
    ConvertWirePinToEndpointCommand,
    DisconnectPinCommand,
    DropWiresCommand,
    FlipWireFragmentCommand,
    FlipWireSideCommand,
    FuseTouchingWiresCommand,
    LockComponentCommand,
    MoveComponentCommand,
    PlaceComponentCommand,
    RemoveComponentCommand,
    RemoveWireCommand,
    ReplaceWireFragmentsCommand,
    SetComponentAppearanceCommand,
    SetComponentPackageVariantCommand,
    SetPinVariantCommand,
    SetWireInsulatedCommand,
    SetWireSideCommand,
)
from protoboard.domain.commands.drag_cmds import (
    MoveDragSetCommand,
    TransformDragSetCommand,
)
from protoboard.domain.commands.group_cmds import (
    AddToGroupCommand,
    CreateGroupCommand,
    DisbandGroupCommand,
    FlipGroupCommand,
    RemoveFromGroupCommand,
    RenameGroupCommand,
    RotateAroundPointCommand,
    RotateComponentsCommand,
    RotateGroupCommand,
    SetExportedPinsCommand,
)
from protoboard.domain.commands.net_cmds import (
    AssignPinToNetCommand,
    CreateNetCommand,
    DeleteNetCommand,
    RenameNetCommand,
    SetNetColorCommand,
    SetNetGlobalCommand,
    UnassignPinCommand,
)
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.schematic.net import PinRef
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import Wire
from protoboard.errors import SerializationError
from protoboard.geometry import Point, Transform2D, Vector
from protoboard.services.persistence.board_yaml import board_from_yaml, board_to_yaml


class UnknownCommandError(SerializationError):
    """Raised when a recorded command has a ``type`` field we don't know."""


# ---------------------------------------------------------------------------
# Primitive helpers
# ---------------------------------------------------------------------------


def _pt_to(p: Point) -> dict[str, float]:
    return {"x": float(p.x), "y": float(p.y)}


def _vec_to(v: Vector) -> dict[str, float]:
    return {"x": float(v.dx), "y": float(v.dy)}


def _pt_from(d: dict[str, Any]) -> Point:
    return Point(float(d["x"]), float(d["y"]))


def _vec_from(d: dict[str, Any]) -> Vector:
    return Vector(float(d["x"]), float(d["y"]))


def _uuid_to(u: uuid.UUID) -> str:
    return str(u)


def _uuid_from(s: str) -> uuid.UUID:
    return uuid.UUID(s)


def _placement_to(pl: ComponentPlacement) -> dict[str, Any]:
    out: dict[str, Any] = {
        "component_id": _uuid_to(pl.component_id),
        "position": _pt_to(pl.position),
        "rotation": int(pl.rotation),
        "side": pl.side,
    }
    if pl.label_offset != Vector(0.05, 0.05):
        out["label_offset"] = _vec_to(pl.label_offset)
    if pl.pin_offsets:
        out["pin_offsets"] = {
            str(k): _vec_to(v) for k, v in pl.pin_offsets.items()
        }
    if pl.shared_pins:
        out["shared_pins"] = sorted(pl.shared_pins)
    if pl.vertical_flipped:
        out["vertical_flipped"] = True
    if pl.locked:
        out["locked"] = True
    return out


def _placement_from(d: dict[str, Any]) -> ComponentPlacement:
    raw_lo = d.get("label_offset")
    label_offset = _vec_from(raw_lo) if raw_lo else Vector(0.05, 0.05)
    pin_offsets = {
        int(k): _vec_from(v) for k, v in (d.get("pin_offsets") or {}).items()
    }
    return ComponentPlacement(
        component_id=_uuid_from(d["component_id"]),
        position=_pt_from(d["position"]),
        rotation=int(d["rotation"]),
        side=d["side"],
        label_offset=label_offset,
        pin_offsets=pin_offsets,
        shared_pins=frozenset(int(p) for p in (d.get("shared_pins") or [])),
        vertical_flipped=bool(d.get("vertical_flipped", False)),
        locked=bool(d.get("locked", False)),
    )


def _instance_to(inst: ComponentInstance) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": _uuid_to(inst.id),
        "definition_ref": inst.definition_ref,
        "refdes": inst.refdes,
        "value": inst.value,
    }
    if inst.type:
        out["type"] = inst.type
    if inst.tolerance:
        out["tolerance"] = inst.tolerance
    if inst.package_variant:
        out["package_variant"] = inst.package_variant
    if inst.pin_variant:
        out["pin_variant"] = inst.pin_variant
    return out


def _instance_from(d: dict[str, Any]) -> ComponentInstance:
    return ComponentInstance(
        id=_uuid_from(d["id"]),
        definition_ref=str(d["definition_ref"]),
        refdes=str(d["refdes"]),
        value=str(d["value"]),
        type=str(d.get("type", "")),
        tolerance=str(d["tolerance"]) if d.get("tolerance") else None,
        package_variant=(
            str(d["package_variant"]) if d.get("package_variant") else None
        ),
        pin_variant=str(d["pin_variant"]) if d.get("pin_variant") else None,
    )


def _wire_to(w: Wire) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": _uuid_to(w.id),
        "path": [_pt_to(p) for p in w.path],
        "insulated": bool(w.insulated),
        "side": w.side,
    }
    if w.color:
        out["color"] = w.color
    return out


def _wire_from(d: dict[str, Any]) -> Wire:
    return Wire(
        id=_uuid_from(d["id"]),
        path=[_pt_from(p) for p in d["path"]],
        insulated=bool(d["insulated"]),
        side=d["side"],
        color=d.get("color"),
    )


def _exported_pins_to(pins: frozenset[PinRef]) -> list[dict[str, Any]]:
    return [
        {"component_id": _uuid_to(ep.component_id), "pin_number": ep.pin_number}
        for ep in sorted(pins, key=lambda e: (str(e.component_id), e.pin_number))
    ]


def _exported_pins_from(raw: Any) -> frozenset[PinRef]:
    return frozenset(
        PinRef(
            component_id=_uuid_from(d["component_id"]),
            pin_number=int(d["pin_number"]),
        )
        for d in (raw or [])
    )


def _group_to(g: ComponentGroup) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": _uuid_to(g.id),
        "name": g.name,
    }
    if g.component_ids:
        out["component_ids"] = sorted(_uuid_to(c) for c in g.component_ids)
    if g.child_group_ids:
        out["child_group_ids"] = sorted(_uuid_to(c) for c in g.child_group_ids)
    if g.exported_pins:
        out["exported_pins"] = _exported_pins_to(g.exported_pins)
    if g.transform != Transform2D.identity():
        out["transform"] = list(g.transform.coeffs())
    return out


def _group_from(d: dict[str, Any]) -> ComponentGroup:
    # Old recordings may still carry the legacy stored (origin, rotation)
    # pair instead of a transform — fold it in, mirroring the project-file
    # v2→v3 migration, so replays of pre-conversion sessions keep the pose.
    raw_transform = d.get("transform")
    if raw_transform is not None:
        a, b, c, d2, tx, ty = (float(v) for v in raw_transform)
        transform = Transform2D(a, b, c, d2, tx, ty)
    else:
        raw_origin = d.get("origin")
        rotation = int(d.get("rotation", 0))
        if rotation != 0 and raw_origin:
            from protoboard.domain.commands.group_cmds import _rotation_about
            transform = _rotation_about(_pt_from(raw_origin), rotation)
        else:
            transform = Transform2D.identity()
    return ComponentGroup(
        id=_uuid_from(d["id"]),
        name=str(d["name"]),
        component_ids=frozenset(_uuid_from(c) for c in (d.get("component_ids") or [])),
        child_group_ids=frozenset(
            _uuid_from(c) for c in (d.get("child_group_ids") or [])
        ),
        exported_pins=_exported_pins_from(d.get("exported_pins")),
        transform=transform,
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


# Each serializer is registered for a *specific* command type, but the
# registry stores them under their common erased signature.  The dispatcher
# looks up by ``type(cmd)`` so the cast is always safe at runtime.
_Serializer = Callable[[Any], dict[str, Any]]
_Deserializer = Callable[[dict[str, Any]], Command[Any]]

_TYPE_TO_NAME: dict[type, str] = {}
_NAME_TO_DESERIALIZER: dict[str, _Deserializer] = {}
_TYPE_TO_SERIALIZER: dict[type, _Serializer] = {}


def _register(
    cmd_type: type,
    name: str,
    to_dict: Callable[[Any], dict[str, Any]],
    from_dict: _Deserializer,
) -> None:
    _TYPE_TO_NAME[cmd_type] = name
    _TYPE_TO_SERIALIZER[cmd_type] = to_dict
    _NAME_TO_DESERIALIZER[name] = from_dict


def serialize_command(cmd: Command[Any]) -> dict[str, Any]:
    """Return a YAML-safe dict for *cmd*.  Raises if its type isn't registered."""
    cmd_type = type(cmd)
    serializer = _TYPE_TO_SERIALIZER.get(cmd_type)
    if serializer is None:
        raise UnknownCommandError(
            f"No serializer registered for {cmd_type.__name__}"
        )
    data = serializer(cmd)
    return {"type": _TYPE_TO_NAME[cmd_type], **data}


def deserialize_command(record: dict[str, Any]) -> Command[Any]:
    """Reconstruct a Command from a record produced by :func:`serialize_command`."""
    name = record.get("type")
    if not isinstance(name, str):
        raise UnknownCommandError(f"Record missing 'type' field: {record!r}")
    deserializer = _NAME_TO_DESERIALIZER.get(name)
    if deserializer is None:
        raise UnknownCommandError(f"Unknown command type: {name!r}")
    return deserializer(record)


# ---------------------------------------------------------------------------
# Component commands
# ---------------------------------------------------------------------------


def _place_to(cmd: PlaceComponentCommand) -> dict[str, Any]:
    return {
        "component_id": _uuid_to(cmd.component_id),
        "placement": _placement_to(cmd.placement),
        "instance": _instance_to(cmd.instance),
    }


def _place_from(d: dict[str, Any]) -> PlaceComponentCommand:
    return PlaceComponentCommand(
        component_id=_uuid_from(d["component_id"]),
        placement=_placement_from(d["placement"]),
        instance=_instance_from(d["instance"]),
    )


_register(PlaceComponentCommand, "place_component", _place_to, _place_from)


def _move_to(cmd: MoveComponentCommand) -> dict[str, Any]:
    return {
        "component_id": _uuid_to(cmd.component_id),
        "old_placement": _placement_to(cmd.old_placement),
        "new_placement": _placement_to(cmd.new_placement),
        "skip_conflict_check": bool(cmd.skip_conflict_check),
        "exclude_wire_ids": sorted(_uuid_to(w) for w in cmd.exclude_wire_ids),
    }


def _move_from(d: dict[str, Any]) -> MoveComponentCommand:
    return MoveComponentCommand(
        component_id=_uuid_from(d["component_id"]),
        new_placement=_placement_from(d["new_placement"]),
        old_placement=_placement_from(d["old_placement"]),
        skip_conflict_check=bool(d.get("skip_conflict_check", False)),
        exclude_wire_ids=frozenset(
            _uuid_from(w) for w in (d.get("exclude_wire_ids") or [])
        ),
    )


_register(MoveComponentCommand, "move_component", _move_to, _move_from)


def _remove_component_to(cmd: RemoveComponentCommand) -> dict[str, Any]:
    return {"component_id": _uuid_to(cmd.component_id)}


def _remove_component_from(d: dict[str, Any]) -> RemoveComponentCommand:
    return RemoveComponentCommand(component_id=_uuid_from(d["component_id"]))


_register(
    RemoveComponentCommand, "remove_component",
    _remove_component_to, _remove_component_from,
)


def _add_wire_to(cmd: AddWireCommand) -> dict[str, Any]:
    return {"wire": _wire_to(cmd.wire)}


def _add_wire_from(d: dict[str, Any]) -> AddWireCommand:
    return AddWireCommand(wire=_wire_from(d["wire"]))


_register(AddWireCommand, "add_wire", _add_wire_to, _add_wire_from)


def _add_wire_with_nets_to(cmd: AddWireWithNetsCommand) -> dict[str, Any]:
    # The schematic mutations are *already* applied to state when this command
    # is executed, so for replay it is enough to re-add the wire — the schematic
    # snapshots at load time already reflect the net changes (we replay against
    # an empty schematic which is rebuilt from earlier net commands).
    return {"wire": _wire_to(cmd.wire)}


def _add_wire_with_nets_from(d: dict[str, Any]) -> AddWireCommand:
    # Downgrade to plain AddWireCommand for replay; net mutations are tracked
    # by separate net commands or by the initial project state.
    return AddWireCommand(wire=_wire_from(d["wire"]))


_register(
    AddWireWithNetsCommand, "add_wire_with_nets",
    _add_wire_with_nets_to, _add_wire_with_nets_from,
)


def _remove_wire_to(cmd: RemoveWireCommand) -> dict[str, Any]:
    return {"wire_id": _uuid_to(cmd.wire_id)}


def _remove_wire_from(d: dict[str, Any]) -> RemoveWireCommand:
    return RemoveWireCommand(wire_id=_uuid_from(d["wire_id"]))


_register(RemoveWireCommand, "remove_wire", _remove_wire_to, _remove_wire_from)


def _fragment_to(frag: object) -> dict[str, Any]:
    return {
        "path": [_pt_to(p) for p in frag.path],  # type: ignore[attr-defined]
        "side": frag.side,  # type: ignore[attr-defined]
    }


def _fragment_from(d: dict[str, Any]) -> object:
    from protoboard.domain.layout.wire import Fragment
    return Fragment(
        path=[_pt_from(p) for p in d["path"]],
        side=d["side"],
    )


def _replace_wire_frags_to(cmd: ReplaceWireFragmentsCommand) -> dict[str, Any]:
    return {
        "wire_id": _uuid_to(cmd.wire_id),
        "new_fragments": [_fragment_to(f) for f in cmd.new_fragments],
        "old_fragments": [_fragment_to(f) for f in cmd.old_fragments],
    }


def _replace_wire_frags_from(d: dict[str, Any]) -> ReplaceWireFragmentsCommand:
    return ReplaceWireFragmentsCommand(
        wire_id=_uuid_from(d["wire_id"]),
        new_fragments=tuple(_fragment_from(f) for f in d["new_fragments"]),  # type: ignore[misc]
        old_fragments=tuple(_fragment_from(f) for f in (d.get("old_fragments") or [])),  # type: ignore[misc]
    )


_register(
    ReplaceWireFragmentsCommand, "replace_wire_fragments",
    _replace_wire_frags_to, _replace_wire_frags_from,
)


def _set_wire_insulated_to(cmd: SetWireInsulatedCommand) -> dict[str, Any]:
    return {"wire_id": _uuid_to(cmd.wire_id), "insulated": bool(cmd.insulated)}


def _set_wire_insulated_from(d: dict[str, Any]) -> SetWireInsulatedCommand:
    return SetWireInsulatedCommand(
        wire_id=_uuid_from(d["wire_id"]),
        insulated=bool(d["insulated"]),
    )


_register(
    SetWireInsulatedCommand, "set_wire_insulated",
    _set_wire_insulated_to, _set_wire_insulated_from,
)


def _set_wire_side_to(cmd: SetWireSideCommand) -> dict[str, Any]:
    return {"wire_id": _uuid_to(cmd.wire_id), "side": cmd.side}


def _set_wire_side_from(d: dict[str, Any]) -> SetWireSideCommand:
    return SetWireSideCommand(
        wire_id=_uuid_from(d["wire_id"]),
        side=str(d["side"]),
    )


_register(
    SetWireSideCommand, "set_wire_side",
    _set_wire_side_to, _set_wire_side_from,
)


def _flip_wire_side_to(cmd: FlipWireSideCommand) -> dict[str, Any]:
    return {"wire_id": _uuid_to(cmd.wire_id)}


def _flip_wire_side_from(d: dict[str, Any]) -> FlipWireSideCommand:
    return FlipWireSideCommand(wire_id=_uuid_from(d["wire_id"]))


_register(
    FlipWireSideCommand, "flip_wire_side",
    _flip_wire_side_to, _flip_wire_side_from,
)


def _set_pin_variant_to(cmd: SetPinVariantCommand) -> dict[str, Any]:
    return {
        "component_id": _uuid_to(cmd.component_id),
        "new_variant": cmd.new_variant,
    }


def _set_pin_variant_from(d: dict[str, Any]) -> SetPinVariantCommand:
    return SetPinVariantCommand(
        component_id=_uuid_from(d["component_id"]),
        new_variant=d.get("new_variant"),
    )


_register(
    SetPinVariantCommand, "set_pin_variant",
    _set_pin_variant_to, _set_pin_variant_from,
)


def _set_package_variant_to(
    cmd: SetComponentPackageVariantCommand,
) -> dict[str, Any]:
    return {
        "component_id": _uuid_to(cmd.component_id),
        "new_variant": cmd.new_variant,
    }


def _set_package_variant_from(
    d: dict[str, Any],
) -> SetComponentPackageVariantCommand:
    return SetComponentPackageVariantCommand(
        component_id=_uuid_from(d["component_id"]),
        new_variant=d.get("new_variant"),
    )


_register(
    SetComponentPackageVariantCommand, "set_package_variant",
    _set_package_variant_to, _set_package_variant_from,
)


def _set_appearance_to(cmd: SetComponentAppearanceCommand) -> dict[str, Any]:
    return {
        "component_id": _uuid_to(cmd.component_id),
        "style": (
            cmd.new_appearance.to_style_string()
            if cmd.new_appearance is not None
            else None
        ),
    }


def _set_appearance_from(d: dict[str, Any]) -> SetComponentAppearanceCommand:
    from protoboard.domain.components.appearance import parse_style
    return SetComponentAppearanceCommand(
        component_id=_uuid_from(d["component_id"]),
        new_appearance=parse_style(d.get("style"), ctx="set_appearance"),
    )


_register(
    SetComponentAppearanceCommand, "set_appearance",
    _set_appearance_to, _set_appearance_from,
)


def _flip_wire_frag_to(cmd: FlipWireFragmentCommand) -> dict[str, Any]:
    return {
        "wire_id": _uuid_to(cmd.wire_id),
        "start_idx": int(cmd.start_idx),
        "end_idx": int(cmd.end_idx),
        "target_side": cmd.target_side,
    }


def _flip_wire_frag_from(d: dict[str, Any]) -> FlipWireFragmentCommand:
    return FlipWireFragmentCommand(
        wire_id=_uuid_from(d["wire_id"]),
        start_idx=int(d["start_idx"]),
        end_idx=int(d["end_idx"]),
        target_side=str(d["target_side"]),
    )


_register(
    FlipWireFragmentCommand, "flip_wire_fragment",
    _flip_wire_frag_to, _flip_wire_frag_from,
)


def _convert_to_wire_pin_to(cmd: ConvertEndpointToWirePinCommand) -> dict[str, Any]:
    return {
        "wire_id": _uuid_to(cmd.wire_id),
        "endpoint": cmd.endpoint,
        "net_id": _uuid_to(cmd.net_id) if cmd.net_id is not None else None,
    }


def _convert_to_wire_pin_from(d: dict[str, Any]) -> ConvertEndpointToWirePinCommand:
    raw_net = d.get("net_id")
    return ConvertEndpointToWirePinCommand(
        wire_id=_uuid_from(d["wire_id"]),
        endpoint=str(d["endpoint"]),
        net_id=_uuid_from(raw_net) if raw_net is not None else None,
    )


_register(
    ConvertEndpointToWirePinCommand, "convert_endpoint_to_wire_pin",
    _convert_to_wire_pin_to, _convert_to_wire_pin_from,
)


def _convert_from_wire_pin_to(cmd: ConvertWirePinToEndpointCommand) -> dict[str, Any]:
    return {"wire_pin_id": _uuid_to(cmd.wire_pin_id)}


def _convert_from_wire_pin_from(d: dict[str, Any]) -> ConvertWirePinToEndpointCommand:
    return ConvertWirePinToEndpointCommand(wire_pin_id=_uuid_from(d["wire_pin_id"]))


_register(
    ConvertWirePinToEndpointCommand, "convert_wire_pin_to_endpoint",
    _convert_from_wire_pin_to, _convert_from_wire_pin_from,
)


def _drop_wires_to(cmd: DropWiresCommand) -> dict[str, Any]:
    return {"wire_ids": sorted(_uuid_to(w) for w in cmd.wire_ids)}


def _drop_wires_from(d: dict[str, Any]) -> DropWiresCommand:
    return DropWiresCommand(
        wire_ids=frozenset(_uuid_from(w) for w in (d.get("wire_ids") or []))
    )


_register(DropWiresCommand, "drop_wires", _drop_wires_to, _drop_wires_from)


def _fuse_wires_to(cmd: FuseTouchingWiresCommand) -> dict[str, Any]:
    # wire_ids is None → whole-layout scope; else the caller-supplied scope.
    return {
        "wire_ids": (
            None if cmd.wire_ids is None
            else sorted(_uuid_to(w) for w in cmd.wire_ids)
        ),
    }


def _fuse_wires_from(d: dict[str, Any]) -> FuseTouchingWiresCommand:
    raw = d.get("wire_ids")
    return FuseTouchingWiresCommand(
        wire_ids=None if raw is None else frozenset(_uuid_from(w) for w in raw)
    )


_register(
    FuseTouchingWiresCommand, "fuse_touching_wires",
    _fuse_wires_to, _fuse_wires_from,
)


def _disconnect_pin_to(cmd: DisconnectPinCommand) -> dict[str, Any]:
    return {
        "component_id": _uuid_to(cmd.component_id),
        "pin_number": int(cmd.pin_number),
    }


def _disconnect_pin_from(d: dict[str, Any]) -> DisconnectPinCommand:
    return DisconnectPinCommand(
        component_id=_uuid_from(d["component_id"]),
        pin_number=int(d["pin_number"]),
    )


_register(
    DisconnectPinCommand, "disconnect_pin",
    _disconnect_pin_to, _disconnect_pin_from,
)


def _lock_component_to(cmd: LockComponentCommand) -> dict[str, Any]:
    return {
        "component_id": _uuid_to(cmd.component_id),
        "locked": bool(cmd.locked),
    }


def _lock_component_from(d: dict[str, Any]) -> LockComponentCommand:
    return LockComponentCommand(
        component_id=_uuid_from(d["component_id"]),
        locked=bool(d["locked"]),
    )


_register(
    LockComponentCommand, "lock_component",
    _lock_component_to, _lock_component_from,
)


# ---------------------------------------------------------------------------
# Board commands
# ---------------------------------------------------------------------------


def _split_strip_to(cmd: SplitStripCommand) -> dict[str, Any]:
    # SplitStripCommand holds a direct reference to the *target* StripAtom in
    # the current board.  After replay starts the board has been rebuilt, so
    # the identity of the atom changes; we serialize its identifying
    # parameters instead and re-locate the matching atom at replay time.
    target = cmd.target
    return {
        "cut_index": int(cmd.cut_index),
        "cut_between": bool(cmd.cut_between),
        "target": {
            "length": int(target.length) if isinstance(target.length, int) else 1,
            "orientation": target.orientation,
            "layers": target.layers,
            "isolated": bool(target.isolated),
        },
    }


def _split_strip_from(d: dict[str, Any]) -> "_DeferredSplitStrip":
    return _DeferredSplitStrip(
        target_spec=d["target"],
        cut_index=int(d["cut_index"]),
        cut_between=bool(d["cut_between"]),
    )


_register(SplitStripCommand, "split_strip", _split_strip_to, _split_strip_from)


class _DeferredSplitStrip(Command[Any]):
    """SplitStripCommand wrapper that resolves its target atom at apply time.

    Recording captures the StripAtom by value; on replay we must find the
    matching atom in the *current* board (which may have been split by earlier
    replayed commands).  We look up the first cuttable StripAtom matching the
    recorded length+orientation+layers+isolated tuple.
    """

    description = "Cut strip (recorded)"

    def __init__(
        self,
        target_spec: dict[str, Any],
        cut_index: int,
        cut_between: bool,
    ) -> None:
        self._target_spec = target_spec
        self._cut_index = cut_index
        self._cut_between = cut_between
        self._delegate: SplitStripCommand | None = None

    def _find_target(self, board: Board) -> StripAtom:
        from protoboard.domain.board.boxes import StripAtom as _SA
        wanted_len = int(self._target_spec.get("length", 1))
        wanted_orient = self._target_spec.get("orientation")
        wanted_layers = self._target_spec.get("layers")
        wanted_iso = bool(self._target_spec.get("isolated", False))
        for atom, _center in board.atoms():
            if not isinstance(atom, _SA):
                continue
            atom_len = int(atom.length) if isinstance(atom.length, int) else 1
            if atom_len != wanted_len:
                continue
            if wanted_orient is not None and atom.orientation != wanted_orient:
                continue
            if wanted_layers is not None and atom.layers != wanted_layers:
                continue
            if atom.isolated != wanted_iso:
                continue
            return atom
        raise SerializationError(
            f"No matching strip found for recorded split: {self._target_spec!r}"
        )

    def apply(self, state: Any) -> None:
        target = self._find_target(state.board)
        self._delegate = SplitStripCommand(
            target=target,
            cut_index=self._cut_index,
            cut_between=self._cut_between,
        )
        self._delegate.apply(state)

    def revert(self, state: Any) -> None:
        if self._delegate is not None:
            self._delegate.revert(state)


def _resize_board_to(cmd: ResizeBoardCommand) -> dict[str, Any]:
    return {"new_board": board_to_yaml(cmd.new_board)}


def _resize_board_from(d: dict[str, Any]) -> ResizeBoardCommand:
    return ResizeBoardCommand(new_board=board_from_yaml(str(d["new_board"])))


_register(
    ResizeBoardCommand, "resize_board",
    _resize_board_to, _resize_board_from,
)


# ---------------------------------------------------------------------------
# Net commands
# ---------------------------------------------------------------------------


def _rename_net_to(cmd: RenameNetCommand) -> dict[str, Any]:
    return {"net_id": _uuid_to(cmd.net_id), "new_name": cmd.new_name}


def _rename_net_from(d: dict[str, Any]) -> RenameNetCommand:
    return RenameNetCommand(
        net_id=_uuid_from(d["net_id"]),
        new_name=str(d["new_name"]),
    )


_register(RenameNetCommand, "rename_net", _rename_net_to, _rename_net_from)


def _set_net_global_to(cmd: SetNetGlobalCommand) -> dict[str, Any]:
    return {"net_id": _uuid_to(cmd.net_id), "global_net": bool(cmd.global_net)}


def _set_net_global_from(d: dict[str, Any]) -> SetNetGlobalCommand:
    return SetNetGlobalCommand(
        net_id=_uuid_from(d["net_id"]),
        global_net=bool(d["global_net"]),
    )


_register(
    SetNetGlobalCommand, "set_net_global",
    _set_net_global_to, _set_net_global_from,
)


def _set_net_color_to(cmd: SetNetColorCommand) -> dict[str, Any]:
    return {"net_id": _uuid_to(cmd.net_id), "new_color": cmd.new_color}


def _set_net_color_from(d: dict[str, Any]) -> SetNetColorCommand:
    return SetNetColorCommand(
        net_id=_uuid_from(d["net_id"]),
        new_color=d.get("new_color"),
    )


_register(
    SetNetColorCommand, "set_net_color",
    _set_net_color_to, _set_net_color_from,
)


def _create_net_to(cmd: CreateNetCommand) -> dict[str, Any]:
    return {"name": cmd.name, "net_id": _uuid_to(cmd.net_id)}


def _create_net_from(d: dict[str, Any]) -> CreateNetCommand:
    return CreateNetCommand(
        name=str(d["name"]),
        net_id=_uuid_from(d["net_id"]),
    )


_register(CreateNetCommand, "create_net", _create_net_to, _create_net_from)


def _delete_net_to(cmd: DeleteNetCommand) -> dict[str, Any]:
    return {"net_id": _uuid_to(cmd.net_id)}


def _delete_net_from(d: dict[str, Any]) -> DeleteNetCommand:
    return DeleteNetCommand(net_id=_uuid_from(d["net_id"]))


_register(DeleteNetCommand, "delete_net", _delete_net_to, _delete_net_from)


def _assign_pin_to(cmd: AssignPinToNetCommand) -> dict[str, Any]:
    return {
        "component_id": _uuid_to(cmd.component_id),
        "pin_number": int(cmd.pin_number),
        "target_net_id": _uuid_to(cmd.target_net_id),
    }


def _assign_pin_from(d: dict[str, Any]) -> AssignPinToNetCommand:
    return AssignPinToNetCommand(
        component_id=_uuid_from(d["component_id"]),
        pin_number=int(d["pin_number"]),
        target_net_id=_uuid_from(d["target_net_id"]),
    )


_register(
    AssignPinToNetCommand, "assign_pin_to_net",
    _assign_pin_to, _assign_pin_from,
)


def _unassign_pin_to(cmd: UnassignPinCommand) -> dict[str, Any]:
    return {
        "component_id": _uuid_to(cmd.component_id),
        "pin_number": int(cmd.pin_number),
    }


def _unassign_pin_from(d: dict[str, Any]) -> UnassignPinCommand:
    return UnassignPinCommand(
        component_id=_uuid_from(d["component_id"]),
        pin_number=int(d["pin_number"]),
    )


_register(
    UnassignPinCommand, "unassign_pin",
    _unassign_pin_to, _unassign_pin_from,
)


# ---------------------------------------------------------------------------
# Group commands
# ---------------------------------------------------------------------------


def _create_group_to(cmd: CreateGroupCommand) -> dict[str, Any]:
    return {"group": _group_to(cmd.group)}


def _create_group_from(d: dict[str, Any]) -> CreateGroupCommand:
    return CreateGroupCommand(group=_group_from(d["group"]))


_register(
    CreateGroupCommand, "create_group",
    _create_group_to, _create_group_from,
)


def _disband_group_to(cmd: DisbandGroupCommand) -> dict[str, Any]:
    return {"group_id": _uuid_to(cmd.group_id)}


def _disband_group_from(d: dict[str, Any]) -> DisbandGroupCommand:
    return DisbandGroupCommand(group_id=_uuid_from(d["group_id"]))


_register(
    DisbandGroupCommand, "disband_group",
    _disband_group_to, _disband_group_from,
)


def _add_to_group_to(cmd: AddToGroupCommand) -> dict[str, Any]:
    return {
        "group_id": _uuid_to(cmd.group_id),
        "component_ids": sorted(_uuid_to(c) for c in cmd.component_ids),
        "child_group_ids": sorted(_uuid_to(c) for c in cmd.child_group_ids),
    }


def _add_to_group_from(d: dict[str, Any]) -> AddToGroupCommand:
    return AddToGroupCommand(
        group_id=_uuid_from(d["group_id"]),
        component_ids=frozenset(
            _uuid_from(c) for c in (d.get("component_ids") or [])
        ),
        child_group_ids=frozenset(
            _uuid_from(c) for c in (d.get("child_group_ids") or [])
        ),
    )


_register(
    AddToGroupCommand, "add_to_group",
    _add_to_group_to, _add_to_group_from,
)


def _remove_from_group_to(cmd: RemoveFromGroupCommand) -> dict[str, Any]:
    return {
        "group_id": _uuid_to(cmd.group_id),
        "component_ids": sorted(_uuid_to(c) for c in cmd.component_ids),
        "child_group_ids": sorted(_uuid_to(c) for c in cmd.child_group_ids),
    }


def _remove_from_group_from(d: dict[str, Any]) -> RemoveFromGroupCommand:
    return RemoveFromGroupCommand(
        group_id=_uuid_from(d["group_id"]),
        component_ids=frozenset(
            _uuid_from(c) for c in (d.get("component_ids") or [])
        ),
        child_group_ids=frozenset(
            _uuid_from(c) for c in (d.get("child_group_ids") or [])
        ),
    )


_register(
    RemoveFromGroupCommand, "remove_from_group",
    _remove_from_group_to, _remove_from_group_from,
)


def _rename_group_to(cmd: RenameGroupCommand) -> dict[str, Any]:
    return {"group_id": _uuid_to(cmd.group_id), "new_name": cmd.new_name}


def _rename_group_from(d: dict[str, Any]) -> RenameGroupCommand:
    return RenameGroupCommand(
        group_id=_uuid_from(d["group_id"]),
        new_name=str(d["new_name"]),
    )


_register(
    RenameGroupCommand, "rename_group",
    _rename_group_to, _rename_group_from,
)


def _set_exported_pins_to(cmd: SetExportedPinsCommand) -> dict[str, Any]:
    return {
        "group_id": _uuid_to(cmd.group_id),
        "new_exported": _exported_pins_to(cmd.new_exported),
    }


def _set_exported_pins_from(d: dict[str, Any]) -> SetExportedPinsCommand:
    return SetExportedPinsCommand(
        group_id=_uuid_from(d["group_id"]),
        new_exported=_exported_pins_from(d.get("new_exported")),
    )


_register(
    SetExportedPinsCommand, "set_exported_pins",
    _set_exported_pins_to, _set_exported_pins_from,
)


def _rotate_group_to(cmd: RotateGroupCommand) -> dict[str, Any]:
    return {"group_id": _uuid_to(cmd.group_id), "delta": int(cmd.delta)}


def _rotate_group_from(d: dict[str, Any]) -> RotateGroupCommand:
    return RotateGroupCommand(
        group_id=_uuid_from(d["group_id"]),
        delta=int(d["delta"]),
    )


_register(
    RotateGroupCommand, "rotate_group",
    _rotate_group_to, _rotate_group_from,
)


def _rotate_components_to(cmd: RotateComponentsCommand) -> dict[str, Any]:
    return {
        "comp_ids": sorted(_uuid_to(c) for c in cmd.comp_ids),
        "delta": int(cmd.delta),
    }


def _rotate_components_from(d: dict[str, Any]) -> RotateComponentsCommand:
    return RotateComponentsCommand(
        comp_ids=frozenset(_uuid_from(c) for c in (d.get("comp_ids") or [])),
        delta=int(d["delta"]),
    )


_register(
    RotateComponentsCommand, "rotate_components",
    _rotate_components_to, _rotate_components_from,
)


def _rotate_around_point_to(cmd: RotateAroundPointCommand) -> dict[str, Any]:
    return {
        "comp_ids": sorted(_uuid_to(c) for c in cmd.comp_ids),
        "origin": _pt_to(cmd.origin),
        "delta": int(cmd.delta),
    }


def _rotate_around_point_from(d: dict[str, Any]) -> RotateAroundPointCommand:
    return RotateAroundPointCommand(
        comp_ids=frozenset(_uuid_from(c) for c in (d.get("comp_ids") or [])),
        origin=_pt_from(d["origin"]),
        delta=int(d["delta"]),
    )


_register(
    RotateAroundPointCommand, "rotate_around_point",
    _rotate_around_point_to, _rotate_around_point_from,
)


def _flip_group_to(cmd: FlipGroupCommand) -> dict[str, Any]:
    return {
        "group_id": _uuid_to(cmd.group_id),
        "axis": cmd.axis,
        "pivot": _pt_to(cmd.pivot) if cmd.pivot is not None else None,
    }


def _flip_group_from(d: dict[str, Any]) -> FlipGroupCommand:
    raw_pivot = d.get("pivot")
    return FlipGroupCommand(
        group_id=_uuid_from(d["group_id"]),
        axis=d["axis"],
        pivot=_pt_from(raw_pivot) if raw_pivot is not None else None,
    )


_register(FlipGroupCommand, "flip_group", _flip_group_to, _flip_group_from)


# ---------------------------------------------------------------------------
# Drag commands — the atomic move / transform of a drag set
# ---------------------------------------------------------------------------


def _member_pair_to(
    pair: tuple[ComponentPlacement, ComponentPlacement]
) -> dict[str, Any]:
    old_pl, new_pl = pair
    return {"old": _placement_to(old_pl), "new": _placement_to(new_pl)}


def _member_pair_from(
    d: dict[str, Any]
) -> tuple[ComponentPlacement, ComponentPlacement]:
    return (_placement_from(d["old"]), _placement_from(d["new"]))


def _members_to(
    members: dict[uuid.UUID, tuple[ComponentPlacement, ComponentPlacement]]
) -> list[dict[str, Any]]:
    # A list of {id, old, new} keeps the record order-stable and YAML-friendly
    # (dict keys would be UUID objects).  Sorted by id for deterministic output.
    return [
        {"id": _uuid_to(cid), **_member_pair_to(pair)}
        for cid, pair in sorted(members.items(), key=lambda kv: str(kv[0]))
    ]


def _members_from(
    raw: list[dict[str, Any]]
) -> dict[uuid.UUID, tuple[ComponentPlacement, ComponentPlacement]]:
    return {_uuid_from(m["id"]): _member_pair_from(m) for m in raw}


def _move_drag_set_to(cmd: MoveDragSetCommand) -> dict[str, Any]:
    return {
        "members": _members_to(cmd.members),
        "internal_wire_ids": sorted(_uuid_to(w) for w in cmd.internal_wire_ids),
        "dropped_wire_ids": sorted(_uuid_to(w) for w in cmd.dropped_wire_ids),
        "delta": _vec_to(cmd.delta),
    }


def _move_drag_set_from(d: dict[str, Any]) -> MoveDragSetCommand:
    return MoveDragSetCommand(
        members=_members_from(d.get("members") or []),
        internal_wire_ids=frozenset(
            _uuid_from(w) for w in (d.get("internal_wire_ids") or [])
        ),
        dropped_wire_ids=frozenset(
            _uuid_from(w) for w in (d.get("dropped_wire_ids") or [])
        ),
        delta=_vec_from(d["delta"]),
    )


_register(
    MoveDragSetCommand, "move_drag_set",
    _move_drag_set_to, _move_drag_set_from,
)


def _path_pair_to(pair: tuple[list[Point], list[Point]]) -> dict[str, Any]:
    old_path, new_path = pair
    return {
        "old": [_pt_to(p) for p in old_path],
        "new": [_pt_to(p) for p in new_path],
    }


def _path_pair_from(d: dict[str, Any]) -> tuple[list[Point], list[Point]]:
    return (
        [_pt_from(p) for p in d["old"]],
        [_pt_from(p) for p in d["new"]],
    )


def _frag_pair_to(pair: tuple[list[Any], list[Any]]) -> dict[str, Any]:
    old_frags, new_frags = pair
    return {
        "old": [_fragment_to(f) for f in old_frags],
        "new": [_fragment_to(f) for f in new_frags],
    }


def _frag_pair_from(d: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    return (
        [_fragment_from(f) for f in d["old"]],
        [_fragment_from(f) for f in d["new"]],
    )


def _pt_pair_to(pair: tuple[Point, Point]) -> dict[str, Any]:
    return {"old": _pt_to(pair[0]), "new": _pt_to(pair[1])}


def _pt_pair_from(d: dict[str, Any]) -> tuple[Point, Point]:
    return (_pt_from(d["old"]), _pt_from(d["new"]))


def _keyed_to(
    mapping: dict[uuid.UUID, Any], value_to: Any
) -> list[dict[str, Any]]:
    return [
        {"id": _uuid_to(k), "v": value_to(v)}
        for k, v in sorted(mapping.items(), key=lambda kv: str(kv[0]))
    ]


def _keyed_from(
    raw: list[dict[str, Any]], value_from: Any
) -> dict[uuid.UUID, Any]:
    return {_uuid_from(e["id"]): value_from(e["v"]) for e in raw}


def _transform_drag_set_to(cmd: TransformDragSetCommand) -> dict[str, Any]:
    return {
        "members": _members_to(cmd.members),
        "wire_paths": _keyed_to(cmd.wire_paths, _path_pair_to),
        "wire_fragments": _keyed_to(cmd.wire_fragments, _frag_pair_to),
        "knot_positions": _keyed_to(cmd.knot_positions, _pt_pair_to),
    }


def _transform_drag_set_from(d: dict[str, Any]) -> TransformDragSetCommand:
    return TransformDragSetCommand(
        members=_members_from(d.get("members") or []),
        wire_paths=_keyed_from(d.get("wire_paths") or [], _path_pair_from),
        wire_fragments=_keyed_from(
            d.get("wire_fragments") or [], _frag_pair_from
        ),
        knot_positions=_keyed_from(
            d.get("knot_positions") or [], _pt_pair_from
        ),
    )


_register(
    TransformDragSetCommand, "transform_drag_set",
    _transform_drag_set_to, _transform_drag_set_from,
)


# ---------------------------------------------------------------------------
# Macro command — recursive
# ---------------------------------------------------------------------------


def _macro_to(cmd: MacroCommand[Any]) -> dict[str, Any]:
    return {
        "description": cmd.description,
        "commands": [serialize_command(c) for c in cmd._commands],  # noqa: SLF001
    }


def _macro_from(d: dict[str, Any]) -> MacroCommand[Any]:
    return MacroCommand(
        commands=[deserialize_command(r) for r in (d.get("commands") or [])],
        description=str(d.get("description", "Macro")),
    )


_register(MacroCommand, "macro", _macro_to, _macro_from)


__all__ = [
    "UnknownCommandError",
    "deserialize_command",
    "serialize_command",
]
