# SPDX-License-Identifier: MIT
"""Project persistence — save/load a project folder.

Folder structure:
    my_project/
    ├── board.yaml
    ├── instances.yaml  # component instances (id, definition_ref, refdes, value)
    ├── layout.yaml     # placements, wires
    └── schematic.yaml  # nets
"""

from __future__ import annotations

import io
import uuid
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from protoboard.domain.board.board import Board
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.normalize import (
    NormalizeReport,
    normalize_layout_positions,
)
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import Wire
from protoboard.domain.axis_labels_settings import (
    AxisLabelFormat,
    AxisLabelSettings,
)
from protoboard.domain.routing_settings import RoutingSettings
from protoboard.gui.layer_state import LayerOverrides, LayerVisibility
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.errors import SerializationError
from protoboard.geometry import Point
import structlog
from protoboard.services.persistence.board_yaml import board_from_yaml, board_to_yaml

_log = structlog.get_logger(__name__)
_yaml = YAML(typ="safe")
_yaml.default_flow_style = False


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------


def save_project(
    directory: Path,
    board: Board,
    layout: Layout,
    schematic: Schematic,
    instances: dict[uuid.UUID, ComponentInstance] | None = None,
    routing_settings: RoutingSettings | None = None,
    layer_overrides: LayerOverrides | None = None,
    axis_label_settings: AxisLabelSettings | None = None,
) -> None:
    _log.info("project.saving", path=str(directory))
    directory.mkdir(parents=True, exist_ok=True)
    _write(directory / "board.yaml", board_to_yaml(board))
    _write(directory / "layout.yaml", _layout_to_yaml(layout))
    _write(directory / "schematic.yaml", _schematic_to_yaml(schematic))
    _write(directory / "instances.yaml", _instances_to_yaml(instances or {}))
    if routing_settings is not None:
        _write(directory / "routing.yaml", _routing_settings_to_yaml(routing_settings))
    if layer_overrides is not None and not layer_overrides.all_full():
        _write(directory / "layers.yaml", _layer_overrides_to_yaml(layer_overrides))
    else:
        layers_path = directory / "layers.yaml"
        if layers_path.exists():
            layers_path.unlink()
    # axis_labels.yaml: written only when settings differ from the built-in
    # defaults (NUMERIC_1 columns + ALPHABETIC rows).  Projects that accept
    # the defaults stay on disk without an extra file, and reload still
    # yields the same defaults via load_project()'s fallback.
    axis_path = directory / "axis_labels.yaml"
    if (axis_label_settings is not None
            and axis_label_settings != AxisLabelSettings()):
        _write(axis_path, _axis_labels_to_yaml(axis_label_settings))
    elif axis_path.exists():
        axis_path.unlink()
    _log.info(
        "project.saved",
        path=str(directory),
        placements=len(layout.placements),
        wires=len(layout.wires),
        nets=len(schematic.nets),
    )


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _instances_to_yaml(instances: dict[uuid.UUID, ComponentInstance]) -> str:
    data: dict[str, Any] = {
        "instances": [
            {
                "id": str(inst.id),
                "definition_ref": inst.definition_ref,
                "refdes": inst.refdes,
                "value": inst.value,
                **({"type": inst.type} if inst.type else {}),
                **({"body_variant": inst.body_variant} if inst.body_variant else {}),
                **({"pin_variant": inst.pin_variant} if inst.pin_variant else {}),
            }
            for inst in instances.values()
        ]
    }
    buf = io.StringIO()
    _yaml.dump(data, buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# v2 wire/knot encoding helpers
# ---------------------------------------------------------------------------


def _knot_to_yaml_dict(knot: object) -> dict[str, Any]:
    from protoboard.domain.layout.knot import JointKnot as _Joint
    from protoboard.domain.layout.knot import PinKnot as _Pin
    if isinstance(knot, _Pin):
        return {
            "id": str(knot.id),
            "kind": "pin",
            "pin": {
                "component_id": str(knot.pin.component_id),
                "pin_number": knot.pin.pin_number,
            },
        }
    if isinstance(knot, _Joint):
        return {
            "id": str(knot.id),
            "kind": "joint",
            "position": {"x": knot.position.x, "y": knot.position.y},
        }
    raise SerializationError(f"unknown Knot type: {type(knot).__name__}")


def _wire_v2_to_yaml_dict(wire: object) -> dict[str, Any]:
    from protoboard.domain.layout.wire import Wire as _Wire
    if not isinstance(wire, _Wire):
        raise SerializationError(f"expected Wire, got {type(wire).__name__}")
    if wire.a is None or wire.b is None:
        raise SerializationError(
            f"Wire {wire.id} is missing knot references (a/b); cannot encode as v2"
        )
    return {
        "id": str(wire.id),
        "a_knot_id": str(wire.a.id),
        "b_knot_id": str(wire.b.id),
        "insulated": wire.insulated,
        "fragments": [
            {
                "side": f.side,
                "path": [{"x": p.x, "y": p.y} for p in f.path],
            }
            for f in wire.fragments
        ],
        **({"color": wire.color} if wire.color else {}),
    }


def _layout_to_yaml(layout: Layout) -> str:
    # ``format_version`` lets future loaders refuse or migrate older files
    # outright after a deprecation window.  v2 introduces the knot/wire/
    # fragment model alongside the legacy ``wires`` list; both are written
    # while consumers complete migration in Commit B.
    # v2 detection: any wire carries explicit knot references, or there are
    # standalone knots in the layout.  `wires_v2` is the same WireCollection
    # as `wires`; filtering by ``a is not None`` distinguishes v2-style wires
    # (knot-aware) from legacy Wire-style wires that share the storage.
    v2_wires = [w for w in layout.wires if w.a is not None and w.b is not None]
    has_v2 = bool(v2_wires) or bool(layout.knots)
    data: dict[str, Any] = {
        "format_version": 2 if has_v2 else 1,
        "placements": [
            {
                "component_id": str(pl.component_id),
                "position": {"x": pl.position.x, "y": pl.position.y},
                "rotation": pl.rotation,
                "side": pl.side,
                **(
                    {"label_offset": {"x": pl.label_offset.x, "y": pl.label_offset.y}}
                    if pl.label_offset != Point(0.05, 0.05)
                    else {}
                ),
                **(
                    {"pin_offsets": {
                        str(pin): {"x": off.x, "y": off.y}
                        for pin, off in pl.pin_offsets.items()
                    }}
                    if pl.pin_offsets
                    else {}
                ),
                **(
                    {"shared_pins": sorted(pl.shared_pins)}
                    if pl.shared_pins
                    else {}
                ),
                **({"vertical_flipped": True} if pl.vertical_flipped else {}),
                **({"locked": True} if pl.locked else {}),
            }
            for pl in layout.placements.values()
        ],
        "wires": [
            {
                "id": str(w.id),
                "path": [{"x": p.x, "y": p.y} for p in w.path],
                "insulated": w.insulated,
                "side": w.side,
                **({"color": w.color} if w.color else {}),
            }
            for w in layout.wires
        ],
        **(
            {
                "knots": [_knot_to_yaml_dict(k) for k in layout.knots.values()]
            }
            if layout.knots else {}
        ),
        **(
            {
                "wires_v2": [_wire_v2_to_yaml_dict(w) for w in v2_wires]
            }
            if v2_wires else {}
        ),
        **(
            {
                "groups": [
                    {
                        "id": str(g.id),
                        "name": g.name,
                        **(
                            {"component_ids": [str(c) for c in sorted(g.component_ids)]}
                            if g.component_ids else {}
                        ),
                        **(
                            {"child_group_ids": [
                                str(c) for c in sorted(g.child_group_ids)
                            ]}
                            if g.child_group_ids else {}
                        ),
                        **(
                            {"exported_pins": [
                                {"component_id": str(ep.component_id), "pin_number": ep.pin_number}
                                for ep in sorted(g.exported_pins, key=lambda e: (str(e.component_id), e.pin_number))
                            ]}
                            if g.exported_pins else {}
                        ),
                        **({"rotation": g.rotation} if g.rotation != 0 else {}),
                        **({"origin": {"x": g.origin.x, "y": g.origin.y}} if g.origin is not None else {}),
                    }
                    for g in layout.groups.values()
                ]
            }
            if layout.groups else {}
        ),
    }
    buf = io.StringIO()
    _yaml.dump(data, buf)
    return buf.getvalue()


def _schematic_to_yaml(schematic: Schematic) -> str:
    data: dict[str, Any] = {
        "nets": [
            {
                "id": str(net.id),
                "name": net.name,
                **({"color": net.color} if net.color else {}),
                **({"global_net": True} if net.global_net else {}),
                "pins": [
                    {
                        "component_id": str(ref.component_id),
                        "pin_number": ref.pin_number,
                    }
                    for ref in net.pins
                ],
            }
            for net in schematic.nets.values()
        ],
    }
    buf = io.StringIO()
    _yaml.dump(data, buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------


def load_project(
    directory: Path,
) -> tuple[
    Board, Layout, Schematic, dict[uuid.UUID, ComponentInstance],
    RoutingSettings, LayerOverrides, AxisLabelSettings,
]:
    _log.info("project.loading", path=str(directory))
    try:
        board = board_from_yaml((directory / "board.yaml").read_text())
        layout = _layout_from_yaml((directory / "layout.yaml").read_text())
        schematic = _schematic_from_yaml((directory / "schematic.yaml").read_text())
        instances_path = directory / "instances.yaml"
        if instances_path.exists():
            instances = _instances_from_yaml(instances_path.read_text())
        else:
            instances = {}
        routing_path = directory / "routing.yaml"
        if routing_path.exists():
            routing_settings = _routing_settings_from_yaml(routing_path.read_text())
        else:
            routing_settings = RoutingSettings()
        layers_path = directory / "layers.yaml"
        if layers_path.exists():
            layer_overrides = _layer_overrides_from_yaml(layers_path.read_text())
        else:
            layer_overrides = LayerOverrides()
        axis_path = directory / "axis_labels.yaml"
        if axis_path.exists():
            axis_label_settings = _axis_labels_from_yaml(axis_path.read_text())
        else:
            axis_label_settings = AxisLabelSettings()
    except FileNotFoundError as exc:
        _log.warning("project.load_failed", path=str(directory), error=str(exc))
        raise SerializationError(f"Missing project file: {exc}") from exc
    # Enrich every insulated wire with pad-position vertices wherever its
    # path geometrically crosses a board pad.  Legacy projects saved before
    # this invariant got pad-node enrichment had wires without "handles" at
    # those crossings — simplify / unweave / edit tools had nothing to grab
    # there.  ``insert_pad_nodes`` is idempotent so already-enriched wires
    # are returned unchanged.
    from protoboard.domain.layout.wire import Wire as _Wire
    from protoboard.domain.layout.wire_utils import (
        board_pads_for_side,
        insert_pad_nodes,
    )
    _all_pads = (
        board_pads_for_side(board, "top")
        | board_pads_for_side(board, "bottom")
    )
    for _idx, _w in enumerate(layout.wires):
        if not _w.insulated:
            continue
        _new_path = insert_pad_nodes(list(_w.path), _all_pads)
        if len(_new_path) != len(_w.path):
            layout.wires[_idx] = _Wire(
                id=_w.id,
                path=_new_path,
                insulated=_w.insulated,
                side=_w.side,
                color=_w.color,
            )

    # Snap legacy fractional offsets (e.g. y=3.503 → 3.5) to the half-grid so
    # the connectivity graph indexes pins and matching board pads as the same
    # node.  Corrections > ±0.5 from the nearest grid intersection are left
    # alone — those are user data, not rounding artifacts.
    report = normalize_layout_positions(layout)
    if report.any_corrections:
        # Surface each correction so the user can audit them in the log.
        for pc in report.placements:
            inst = instances.get(pc.component_id)
            refdes = inst.refdes if inst is not None else "?"
            _log.info(
                "project.placement_snapped",
                refdes=refdes,
                component_id=str(pc.component_id),
                old=(pc.old.x, pc.old.y),
                new=(pc.new.x, pc.new.y),
            )
        for wc in report.wires:
            _log.info(
                "project.wire_point_snapped",
                wire_id=str(wc.wire_id),
                index=wc.point_index,
                old=(wc.old.x, wc.old.y),
                new=(wc.new.x, wc.new.y),
            )
        _log.info(
            "project.normalize_summary",
            placements=len(report.placements),
            wires=len(report.wires),
        )
    _log.info(
        "project.loaded",
        path=str(directory),
        placements=len(layout.placements),
        wires=len(layout.wires),
        nets=len(schematic.nets),
        instances=len(instances),
        snapped=report.count,
    )
    # Attach the report to the layout via a module-level dict so MainWindow
    # can pick it up and show a toast without changing the return signature.
    _last_load_report[directory] = report
    return (
        board, layout, schematic, instances,
        routing_settings, layer_overrides, axis_label_settings,
    )


# Per-directory record of the last normalize report so the GUI can pick it up
# after load_project returns.  Cleared when consumed.
_last_load_report: "dict[Path, NormalizeReport]" = {}


def pop_last_normalize_report(directory: Path) -> "NormalizeReport | None":
    """Return and clear the normalize report attached to the last load of
    *directory*.  Used by MainWindow to toast the user after open_project.
    """
    return _last_load_report.pop(directory, None)


def _instances_from_yaml(text: str) -> dict[uuid.UUID, ComponentInstance]:
    try:
        data = _yaml.load(text)
    except Exception as exc:
        raise SerializationError(f"Instances YAML error: {exc}") from exc
    if not isinstance(data, dict):
        raise SerializationError("instances.yaml must be a mapping")
    result: dict[uuid.UUID, ComponentInstance] = {}
    for raw in data.get("instances") or []:
        inst_id = uuid.UUID(raw["id"])
        result[inst_id] = ComponentInstance(
            id=inst_id,
            definition_ref=str(raw["definition_ref"]),
            refdes=str(raw["refdes"]),
            value=str(raw["value"]),
            type=str(raw.get("type", "")),
            body_variant=str(raw["body_variant"]) if raw.get("body_variant") else None,
            pin_variant=str(raw["pin_variant"]) if raw.get("pin_variant") else None,
        )
    return result


def _layout_from_yaml(text: str) -> Layout:
    try:
        data = _yaml.load(text)
    except Exception as exc:
        raise SerializationError(f"Layout YAML error: {exc}") from exc
    if not isinstance(data, dict):
        raise SerializationError("layout.yaml must be a mapping")
    placements: dict[uuid.UUID, ComponentPlacement] = {}
    for raw in data.get("placements") or []:
        comp_id = uuid.UUID(raw["component_id"])
        pos = Point(float(raw["position"]["x"]), float(raw["position"]["y"]))
        raw_lo = raw.get("label_offset")
        label_offset = (
            Point(float(raw_lo["x"]), float(raw_lo["y"]))
            if raw_lo
            else Point(0.05, 0.05)
        )
        raw_po = raw.get("pin_offsets") or {}
        pin_offsets = {
            int(k): Point(float(v["x"]), float(v["y"]))
            for k, v in raw_po.items()
        }
        shared_pins = frozenset(int(p) for p in (raw.get("shared_pins") or []))
        pl = ComponentPlacement(
            component_id=comp_id,
            position=pos,
            rotation=int(raw["rotation"]),
            side=raw["side"],
            label_offset=label_offset,
            pin_offsets=pin_offsets,
            shared_pins=shared_pins,
            vertical_flipped=bool(raw.get("vertical_flipped", False)),
            locked=bool(raw.get("locked", False)),
        )
        placements[comp_id] = pl
    wires: list[Wire] = []
    for raw in data.get("wires") or []:
        path = [Point(float(p["x"]), float(p["y"])) for p in raw["path"]]
        wires.append(
            Wire(
                id=uuid.UUID(raw["id"]),
                path=path,
                insulated=bool(raw["insulated"]),
                side=raw["side"],
                color=raw.get("color"),
            )
        )
    from protoboard.domain.layout.group import ComponentGroup, ExportedPin

    groups: dict[uuid.UUID, ComponentGroup] = {}
    for raw in data.get("groups") or []:
        gid = uuid.UUID(raw["id"])
        exported_pins: frozenset[ExportedPin] = frozenset(
            ExportedPin(
                component_id=uuid.UUID(ep["component_id"]),
                pin_number=int(ep["pin_number"]),
            )
            for ep in (raw.get("exported_pins") or [])
        )
        raw_origin = raw.get("origin")
        from protoboard.geometry import Point as _Point
        origin = _Point(float(raw_origin["x"]), float(raw_origin["y"])) if raw_origin else None
        groups[gid] = ComponentGroup(
            id=gid,
            name=str(raw["name"]),
            component_ids=frozenset(
                uuid.UUID(c) for c in (raw.get("component_ids") or [])
            ),
            child_group_ids=frozenset(
                uuid.UUID(c) for c in (raw.get("child_group_ids") or [])
            ),
            exported_pins=exported_pins,
            rotation=int(raw.get("rotation", 0)),
            origin=origin,
        )
    from protoboard.domain.layout.wire import WireCollection
    layout = Layout(
        placements=placements,
        wires=WireCollection(wires),
        groups=groups,
    )

    # v2 model: knots + wires_v2 are stored alongside legacy `wires` (the
    # latter stays populated for Commit-A-era consumers).  Projects saved
    # under format_version: 1 won't carry these blocks; they get
    # reconstructed by `migrate_layout_to_v2` once the caller can resolve
    # pin world positions (it needs the footprint library).
    _decode_knots_and_wires_v2(data, layout)

    layout.ensure_root_group()
    return layout


def _decode_knots_and_wires_v2(data: dict[str, Any], layout: Layout) -> None:
    from protoboard.domain.layout.knot import JointKnot, Knot, PinKnot
    from protoboard.domain.layout.wire import Fragment, KnotRef, Wire
    raw_knots = data.get("knots") or []
    for raw in raw_knots:
        kid = uuid.UUID(raw["id"])
        kind = str(raw["kind"])
        knot: Knot
        if kind == "pin":
            pin_raw = raw["pin"]
            pin = PinRef(
                component_id=uuid.UUID(pin_raw["component_id"]),
                pin_number=int(pin_raw["pin_number"]),
            )
            knot = PinKnot(id=kid, pin=pin)
            layout.pin_knots[pin] = kid
        elif kind == "joint":
            pos = Point(
                float(raw["position"]["x"]),
                float(raw["position"]["y"]),
            )
            knot = JointKnot(id=kid, position=pos)
        else:
            raise SerializationError(f"unknown knot kind: {kind!r}")
        layout.knots[kid] = knot

    raw_wires_v2 = data.get("wires_v2") or []
    for raw in raw_wires_v2:
        wid = uuid.UUID(raw["id"])
        a_id = uuid.UUID(raw["a_knot_id"])
        b_id = uuid.UUID(raw["b_knot_id"])
        if a_id not in layout.knots or b_id not in layout.knots:
            raise SerializationError(
                f"wire {wid} references unknown knot id "
                f"({a_id} or {b_id} missing)"
            )
        fragments_raw = raw.get("fragments") or []
        if not fragments_raw:
            raise SerializationError(f"wire {wid} has no fragments")
        fragments = [
            Fragment(
                path=[
                    Point(float(p["x"]), float(p["y"]))
                    for p in f["path"]
                ],
                side=f["side"],
            )
            for f in fragments_raw
        ]
        wire = Wire(
            id=wid,
            a=KnotRef(id=a_id),
            b=KnotRef(id=b_id),
            fragments=fragments,
            insulated=bool(raw["insulated"]),
            color=raw.get("color"),
        )
        layout.add_wire_v2(wire)


def _schematic_from_yaml(text: str) -> Schematic:
    try:
        data = _yaml.load(text)
    except Exception as exc:
        raise SerializationError(f"Schematic YAML error: {exc}") from exc
    if not isinstance(data, dict):
        raise SerializationError("schematic.yaml must be a mapping")
    nets: dict[uuid.UUID, Net] = {}
    for raw in data.get("nets") or []:
        net_id = uuid.UUID(raw["id"])
        pins = {
            PinRef(
                component_id=uuid.UUID(p["component_id"]),
                pin_number=int(p["pin_number"]),
            )
            for p in raw.get("pins") or []
        }
        nets[net_id] = Net(
            id=net_id,
            name=raw["name"],
            pins=pins,
            color=raw.get("color") or None,
            global_net=bool(raw.get("global_net", False)),
        )
    return Schematic(nets=nets)


def _routing_settings_to_yaml(s: RoutingSettings) -> str:
    data: dict[str, Any] = {
        "preferred_side": s.preferred_side,
        "allow_layer_change": s.allow_layer_change,
        "max_rip_up_passes": s.max_rip_up_passes,
        "max_optimize_iterations": s.max_optimize_iterations,
        "wire_mode": s.wire_mode,
        "router_name": s.router_name,
        "allow_jumpers": s.allow_jumpers,
        "max_detour_factor": s.max_detour_factor,
        "skip_autoroute_dialog": s.skip_autoroute_dialog,
        "push_and_shove": s.push_and_shove,
        "enable_simplify_pass": s.enable_simplify_pass,
        "simplify_mode": s.simplify_mode.value,
        # Cost preset reference + per-project overrides.  All cost weights
        # live here now; the legacy ``cost_model`` section has been
        # removed (use cost_overrides for per-project tweaks).
        "cost_preset": s.cost_preset,
    }
    if s.cost_overrides:
        data["cost_overrides"] = {k: float(v) for k, v in s.cost_overrides.items()}
    # Only emit the optimizer chain when it's non-empty — keeps YAML clean
    # for projects that haven't opted into plugins.  The loader treats a
    # missing key as an empty tuple.
    if s.optimizer_chain:
        data["optimizer_chain"] = list(s.optimizer_chain)
    buf = io.StringIO()
    _yaml.dump(data, buf)
    return buf.getvalue()


def _routing_settings_from_yaml(text: str) -> RoutingSettings:
    try:
        data = _yaml.load(text)
    except Exception as exc:
        raise SerializationError(f"Routing YAML error: {exc}") from exc
    if not isinstance(data, dict):
        return RoutingSettings()
    wire_mode = data.get("wire_mode", "auto")
    if wire_mode not in ("bare", "insulated", "auto", "bare_diagonal"):
        wire_mode = "auto"
    from protoboard.services.routing.postprocess import SimplifyMode
    simplify_mode_raw = data.get("simplify_mode", SimplifyMode.BARE_PARITY.value)
    try:
        simplify_mode = SimplifyMode(simplify_mode_raw)
    except ValueError:
        simplify_mode = SimplifyMode.BARE_PARITY
    # Optimizer chain: list of plugin names.  Anything that's not a list
    # of strings falls back to an empty chain so a corrupted YAML doesn't
    # poison the route.
    raw_chain = data.get("optimizer_chain", [])
    if isinstance(raw_chain, list) and all(isinstance(x, str) for x in raw_chain):
        optimizer_chain = tuple(raw_chain)
    else:
        optimizer_chain = ()
    # Cost preset / overrides.  Both optional; "default" preset + empty
    # overrides = use built-in cost values from cost_presets module.
    cost_preset = str(data.get("cost_preset", "default"))
    raw_overrides = data.get("cost_overrides")
    cost_overrides: dict[str, float] = {}
    if isinstance(raw_overrides, dict):
        cost_overrides = {
            str(k): float(v)
            for k, v in raw_overrides.items()
            if isinstance(k, str) and isinstance(v, (int, float))
        }
    # Backward-compat: if the old ``cost_model:`` block is present, fold
    # its entries into cost_overrides (with cost_overrides winning when
    # both name the same key).  Lets pre-refactor projects load without
    # silently dropping their tuned values.
    legacy_cm = data.get("cost_model")
    if isinstance(legacy_cm, dict):
        legacy: dict[str, float] = {
            str(k): float(v)
            for k, v in legacy_cm.items()
            if isinstance(k, str) and isinstance(v, (int, float))
        }
        # Existing cost_overrides override legacy values.
        legacy.update(cost_overrides)
        cost_overrides = legacy
    return RoutingSettings(
        preferred_side=data.get("preferred_side", "bottom"),
        allow_layer_change=bool(data.get("allow_layer_change", True)),
        max_rip_up_passes=int(data.get("max_rip_up_passes", 3)),
        max_optimize_iterations=int(data.get("max_optimize_iterations", 10)),
        router_name=str(data.get("router_name", "astar")),
        wire_mode=wire_mode,
        allow_jumpers=bool(data.get("allow_jumpers", True)),
        max_detour_factor=float(data.get("max_detour_factor", 0.0)),
        skip_autoroute_dialog=bool(data.get("skip_autoroute_dialog", False)),
        push_and_shove=bool(data.get("push_and_shove", True)),
        enable_simplify_pass=bool(data.get("enable_simplify_pass", True)),
        simplify_mode=simplify_mode,
        optimizer_chain=optimizer_chain,
        cost_preset=cost_preset,
        cost_overrides=cost_overrides,
    )


def _layer_overrides_to_yaml(overrides: LayerOverrides) -> str:
    data: dict[str, str] = {
        f: getattr(overrides, f).value
        for f in overrides.__dataclass_fields__
    }
    buf = io.StringIO()
    _yaml.dump(data, buf)
    return buf.getvalue()


def _layer_overrides_from_yaml(text: str) -> LayerOverrides:
    try:
        data = _yaml.load(text)
    except Exception as exc:
        raise SerializationError(f"Layers YAML error: {exc}") from exc
    if not isinstance(data, dict):
        return LayerOverrides()
    valid = {v.value for v in LayerVisibility}
    overrides = LayerOverrides()
    for f in overrides.__dataclass_fields__:
        raw = data.get(f)
        if isinstance(raw, str):
            if raw == "auto":
                raw = "full"  # migrate legacy AUTO → FULL
            if raw in valid:
                setattr(overrides, f, LayerVisibility(raw))
    return overrides


def _axis_labels_to_yaml(settings: AxisLabelSettings) -> str:
    data: dict[str, str] = {
        "columns": settings.columns.value,
        "rows": settings.rows.value,
    }
    buf = io.StringIO()
    _yaml.dump(data, buf)
    return buf.getvalue()


def _axis_labels_from_yaml(text: str) -> AxisLabelSettings:
    try:
        data = _yaml.load(text)
    except Exception as exc:
        raise SerializationError(f"Axis labels YAML error: {exc}") from exc
    if not isinstance(data, dict):
        return AxisLabelSettings()
    valid = {v.value for v in AxisLabelFormat}

    def _read(key: str) -> AxisLabelFormat:
        raw = data.get(key)
        if isinstance(raw, str) and raw in valid:
            return AxisLabelFormat(raw)
        return AxisLabelFormat.DISABLED

    return AxisLabelSettings(columns=_read("columns"), rows=_read("rows"))


__all__ = ["load_project", "save_project"]
