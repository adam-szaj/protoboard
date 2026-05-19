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
from protoboard.domain.layout.wire import WireSegment
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
    # axis_labels.yaml: written only when at least one axis is enabled — keeps
    # the project directory tidy for the common "no axis labels" case.
    axis_path = directory / "axis_labels.yaml"
    if axis_label_settings is not None and axis_label_settings.any_enabled():
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


def _layout_to_yaml(layout: Layout) -> str:
    data: dict[str, Any] = {
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
    wires: list[WireSegment] = []
    for raw in data.get("wires") or []:
        path = [Point(float(p["x"]), float(p["y"])) for p in raw["path"]]
        wires.append(
            WireSegment(
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
    layout = Layout(placements=placements, wires=wires, groups=groups)
    layout.ensure_root_group()
    return layout


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
        "allow_jumpers": s.allow_jumpers,
        "max_detour_factor": s.max_detour_factor,
        "skip_autoroute_dialog": s.skip_autoroute_dialog,
        "push_and_shove": s.push_and_shove,
        "enable_simplify_pass": s.enable_simplify_pass,
        "simplify_mode": s.simplify_mode.value,
        "cost_model": {
            "direction_change": s.direction_change,
            "crossing_insulated_bare": s.crossing_insulated_bare,
            "crossing_bare_bare_diagonal": s.crossing_bare_bare_diagonal,
            "layer_change": s.layer_change,
            "under_component_top": s.under_component_top,
        },
    }
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
    cm = data.get("cost_model") or {}
    wire_mode = data.get("wire_mode", "auto")
    if wire_mode not in ("bare", "insulated", "auto", "bare_diagonal"):
        wire_mode = "auto"
    from protoboard.services.routing.postprocess import SimplifyMode
    simplify_mode_raw = data.get("simplify_mode", SimplifyMode.BARE_PARITY.value)
    try:
        simplify_mode = SimplifyMode(simplify_mode_raw)
    except ValueError:
        simplify_mode = SimplifyMode.BARE_PARITY
    return RoutingSettings(
        preferred_side=data.get("preferred_side", "bottom"),
        allow_layer_change=bool(data.get("allow_layer_change", True)),
        max_rip_up_passes=int(data.get("max_rip_up_passes", 3)),
        max_optimize_iterations=int(data.get("max_optimize_iterations", 10)),
        direction_change=float(cm.get("direction_change", 1.0)),
        crossing_insulated_bare=float(cm.get("crossing_insulated_bare", 5.0)),
        crossing_bare_bare_diagonal=float(
            cm.get("crossing_bare_bare_diagonal", 2.0)
        ),
        layer_change=float(cm.get("layer_change", 2.0)),
        under_component_top=float(cm.get("under_component_top", 3.0)),
        wire_mode=wire_mode,
        allow_jumpers=bool(data.get("allow_jumpers", True)),
        max_detour_factor=float(data.get("max_detour_factor", 0.0)),
        skip_autoroute_dialog=bool(data.get("skip_autoroute_dialog", False)),
        push_and_shove=bool(data.get("push_and_shove", True)),
        enable_simplify_pass=bool(data.get("enable_simplify_pass", True)),
        simplify_mode=simplify_mode,
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
