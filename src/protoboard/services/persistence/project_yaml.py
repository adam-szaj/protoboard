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
from typing import (
    TYPE_CHECKING,
    Any,
    Iterable,
    Protocol,
    cast,
    runtime_checkable,
)

from ruamel.yaml import YAML

if TYPE_CHECKING:
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.package import PackageFamily

from protoboard.domain.board.board import Board
from protoboard.domain.components.appearance import Appearance, parse_style
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.normalize import (
    NormalizeReport,
    normalize_layout_positions,
)
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import Wire, transform_wire_geometry
from protoboard.domain.axis_labels_settings import (
    AxisLabelFormat,
    AxisLabelSettings,
)
from protoboard.domain.routing_settings import RoutingSettings
from protoboard.gui.layer_state import LayerOverrides, LayerVisibility
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.errors import SerializationError
from protoboard.geometry import Point, Transform2D, Vector

# Identity transform sentinel — a group with this transform is a no-op in the
# scene-graph model, so it is not written to disk (keeps files clean).
_IDENTITY_TRANSFORM = Transform2D.identity()


def _transform_from_raw(raw: object) -> Transform2D:
    """Parse a group ``transform`` field (``[a,b,c,d,tx,ty]``) → Transform2D.

    Absent / malformed → identity (a group without a stored transform is a
    no-op; older files have no ``transform`` key at all)."""
    if not isinstance(raw, (list, tuple)) or len(raw) != 6:
        return _IDENTITY_TRANSFORM
    a, b, c, d, tx, ty = (float(v) for v in raw)
    return Transform2D(a, b, c, d, tx, ty)
import structlog
from protoboard.services.persistence.board_yaml import board_from_yaml, board_to_yaml

_log = structlog.get_logger(__name__)
_yaml = YAML(typ="safe")
_yaml.default_flow_style = False


@runtime_checkable
class _MigrationLibrary(Protocol):
    """The subset of ``ComponentLibrary`` the migration passes duck-type.

    The migration global is intentionally typed ``object | None`` (the domain
    layer must not import the library), so call sites narrow to this protocol
    via ``cast`` after a ``hasattr`` guard. ``get`` may return ``None`` for a
    missing definition in the duck-typed legacy/test handles used here.
    """

    def get(self, name: str) -> ComponentDefinition | None: ...

    def names(self) -> Iterable[str]: ...


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
    # Canonicalise before encoding: any wires that meet end-to-end on an
    # off-pin pad with matching ``insulated`` get fused into a single
    # Wire so the on-disk format never represents the same physical
    # artifact as multiple ``Wire`` instances.  Run interior-vertex
    # split first so T-junctions get the shared knot before the merge
    # pass evaluates degree-2 conditions.  Merging can re-introduce
    # interior collisions (a merged wire's interior may now coincide
    # with a third wire's endpoint), so alternate split / merge until
    # both report no progress.  Idempotent for projects already saved
    # in canonical form.
    from protoboard.domain.layout.wire_utils import (
        merge_endpoint_wires,
        split_wires_at_endpoint_collisions,
    )
    _splits_on_save = 0
    _fused_on_save = 0
    # Components dict from the migration-registered library — needed so
    # split_wires_at_endpoint_collisions can reject net-conflicting
    # T-junctions (insulated wire crossing another insulated wire on a
    # different net must stay a crossing, not collapse into a shared
    # hub).  Falls back to None when no library is registered (legacy
    # tests); the split pass then behaves geometry-only as before.
    _components_for_split: "dict[str, object] | None" = None
    if _library_for_migration is not None and hasattr(
        _library_for_migration, "names",
    ):
        _components_for_split = {
            _name: _library_for_migration.get(_name)  # type: ignore[union-attr]
            for _name in _library_for_migration.names()  # type: ignore[union-attr]
        }
    for _ in range(16):
        _s = split_wires_at_endpoint_collisions(
            layout,
            schematic=schematic,
            instances=instances,
            components=_components_for_split,  # type: ignore[arg-type]
            board=board,
        )
        _f = merge_endpoint_wires(
            layout, board,
            schematic=schematic,
            instances=instances,
            components=_components_for_split,  # type: ignore[arg-type]
        )
        _splits_on_save += _s
        _fused_on_save += _f
        if _s == 0 and _f == 0:
            break
    if _splits_on_save:
        _log.info("project.endpoint_split_on_save", splits=_splits_on_save)
    if _fused_on_save:
        _log.info("project.endpoint_merge_on_save", fused=_fused_on_save)
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


def _appearance_to_style_field(appearance: Appearance | None) -> dict[str, str]:
    """``{"style": "color: …; corner-radius: …"}`` when set, else ``{}``.

    Kept out of the instance dict entirely when there is nothing to store, so
    untouched instances do not gain a ``style:`` key.
    """
    if appearance is None or appearance.is_empty:
        return {}
    return {"style": appearance.to_style_string()}


def _instances_to_yaml(instances: dict[uuid.UUID, ComponentInstance]) -> str:
    data: dict[str, Any] = {
        "instances": [
            {
                "id": str(inst.id),
                "definition_ref": inst.definition_ref,
                "refdes": inst.refdes,
                "value": inst.value,
                **({"type": inst.type} if inst.type else {}),
                **(
                    {"package_variant": inst.package_variant}
                    if inst.package_variant else {}
                ),
                **({"pin_variant": inst.pin_variant} if inst.pin_variant else {}),
                **_appearance_to_style_field(inst.appearance),
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
    from protoboard.domain.layout.knot import WirePinKnot as _WirePin
    if isinstance(knot, _Pin):
        return {
            "id": str(knot.id),
            "kind": "pin",
            "pin": {
                "component_id": str(knot.pin.component_id),
                "pin_number": knot.pin.pin_number,
            },
        }
    if isinstance(knot, _WirePin):
        return {
            "id": str(knot.id),
            "kind": "wire_pin",
            "position": {"x": knot.position.x, "y": knot.position.y},
            "net_id": str(knot.net_id) if knot.net_id is not None else None,
        }
    if isinstance(knot, _Joint):
        return {
            "id": str(knot.id),
            "kind": "joint",
            "position": {"x": knot.position.x, "y": knot.position.y},
        }
    raise SerializationError(f"unknown Knot type: {type(knot).__name__}")


def _wire_to_yaml_dict(wire: object) -> dict[str, Any]:
    from protoboard.domain.layout.wire import Wire as _Wire
    if not isinstance(wire, _Wire):
        raise SerializationError(f"expected Wire, got {type(wire).__name__}")
    if wire.a is None or wire.b is None:
        raise SerializationError(
            f"Wire {wire.id} is missing knot references (a/b); "
            f"call Layout.ensure_wire_knots before saving"
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
    # Single knot-aware format (format_version: 2).  Save ensures every wire
    # has endpoint knots so the on-disk format is uniform; downstream tools
    # rely on this invariant.
    for wire in layout.wires:
        layout.ensure_wire_knots(wire)
    data: dict[str, Any] = {
        "format_version": 2,
        "placements": [
            {
                "component_id": str(pl.component_id),
                "position": {"x": pl.position.x, "y": pl.position.y},
                "rotation": pl.rotation,
                "side": pl.side,
                **(
                    {"label_offset": {"x": pl.label_offset.x, "y": pl.label_offset.y}}
                    if pl.label_offset != Vector(0.05, 0.05)
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
        "knots": [_knot_to_yaml_dict(k) for k in layout.knots.values()],
        "wires": [_wire_to_yaml_dict(w) for w in layout.wires],
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
                        **(
                            {"transform": list(g.transform.coeffs())}
                            if g.transform != _IDENTITY_TRANSFORM else {}
                        ),
                        **(
                            {"lock_internal_wires": True}
                            if g.lock_internal_wires else {}
                        ),
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


def _drop_dangling_instances(
    instances: dict[uuid.UUID, ComponentInstance],
    layout: Layout,
    library: object | None,
) -> None:
    """Remove component instances whose definition is missing from *library*.

    A definition can be absent because its parametric package failed to build
    and the library skipped it (see library loader graceful degradation). Such
    instances are dropped from ``instances``, ``layout.placements``, every
    group's ``component_ids`` and any ``pin_knots`` anchored to them — the
    project loads and the user re-places the part with a fresh definition.

    No-op when there is no library (the migration global is unset).
    """
    if library is None or not hasattr(library, "get"):
        return
    lib = cast(_MigrationLibrary, library)
    dangling = {
        cid
        for cid, inst in instances.items()
        if lib.get(inst.definition_ref) is None
    }
    if not dangling:
        return

    for cid in dangling:
        inst = instances.pop(cid, None)
        layout.placements.pop(cid, None)
        _log.warning(
            "project.component_dropped",
            component_id=str(cid),
            refdes=inst.refdes if inst is not None else "?",
            definition_ref=inst.definition_ref if inst is not None else "?",
        )

    # Prune dropped components from every group's membership (replace preserves
    # all other group fields).
    import dataclasses

    for gid, group in list(layout.groups.items()):
        kept = group.component_ids - dangling
        if kept != group.component_ids:
            layout.groups[gid] = dataclasses.replace(group, component_ids=kept)

    # Prune pin knots anchored to dropped components.
    for pin in [p for p in layout.pin_knots if p.component_id in dangling]:
        layout.pin_knots.pop(pin, None)


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

    # Drop components whose definition is unavailable (e.g. its parametric
    # package failed to build and the library skipped it). The project still
    # loads; the user re-places the part with a fresh definition. Done BEFORE
    # the migration passes so they never operate on dangling instances.
    _drop_dangling_instances(instances, layout, _library_for_migration)

    # Bind each loaded instance to its definition + the package catalogue, so
    # every downstream migration pass (v1→v2 pin index, stale-path snap) and
    # the returned instances can resolve their chosen-variant footprint via the
    # strict ``resolved_footprint`` path. Mirrors ``session.py`` /
    # ``recording.headless`` — the model requires binding wherever an instance
    # enters the project. Instances whose definition is unavailable (duck-typed
    # legacy/test handles) are left unbound; the migration passes already guard
    # for a missing defn before resolving geometry.
    if _library_for_migration is not None and hasattr(
        _library_for_migration, "get",
    ):
        _mig_lib = cast(_MigrationLibrary, _library_for_migration)
        _mig_families = cast(
            "dict[str, PackageFamily] | None", _families_for_migration,
        )
        for _cid, _inst in list(instances.items()):
            _defn = _mig_lib.get(_inst.definition_ref)
            if _defn is not None:
                instances[_cid] = _inst.bind(_defn, _mig_families)

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

    # Legacy projects (no `knots:` / no `a_knot_id` per wire) get their
    # endpoint knots backfilled now using the standard wire migration.  The
    # session will re-wire its own pin resolver later; this one is enough
    # for the in-place upgrade because we only need to validate during
    # initial knot resolution.
    if any(w.a is None or w.b is None for w in layout.wires):
        # Build pin index by enumerating placements + their definitions.
        # We need the library to look up footprints; library load happens
        # before load_project at the call site, so plumb it via the
        # `_library_for_migration` module global set by load_project below.
        from protoboard.domain.components.package import (
            resolved_footprint as _resolved_footprint,
        )
        from protoboard.domain.layout.placement import (
            pin_world_position as _pwp,
        )
        pin_index: dict[Point, PinRef] = {}
        library = _library_for_migration
        if library is not None:
            for comp_id, pl in layout.placements.items():
                inst = instances.get(comp_id)
                if inst is None:
                    continue
                defn = (
                    cast(_MigrationLibrary, library).get(inst.definition_ref)
                    if hasattr(library, "get")
                    else None
                )
                if defn is None:
                    continue
                fp = _resolved_footprint(
                    defn, inst, _families_for_migration)  # type: ignore[arg-type]
                n_pins = fp.pin_count()
                for pin_num in range(1, n_pins + 1):
                    try:
                        pos = _pwp(fp, pl, pin_num)
                    except (IndexError, ValueError):
                        continue
                    pin_index[pos] = PinRef(component_id=comp_id, pin_number=pin_num)
        # Use migrate_layout_to_v2 if we have any pin resolution path.
        if pin_index:
            from protoboard.services.persistence.wire_migration import (
                migrate_layout_to_v2,
            )

            def _resolve(comp_id: uuid.UUID, pin_num: int) -> Point | None:
                pl = layout.placements.get(comp_id)
                inst = instances.get(comp_id)
                if pl is None or inst is None or library is None:
                    return None
                defn = library.get(inst.definition_ref)  # type: ignore[attr-defined]
                if defn is None:
                    return None
                try:
                    fp = _resolved_footprint(
                        defn, inst, _families_for_migration)  # type: ignore[arg-type]
                    return _pwp(fp, pl, pin_num)
                except (IndexError, ValueError):
                    return None

            migrate_layout_to_v2(layout, board, pin_index, _resolve)
        else:
            # No library — fall back to lazy backfill (every endpoint
            # becomes a JointKnot).  Production load always has a library.
            for w in list(layout.wires):
                layout.ensure_wire_knots(w)

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
    # Canonicalise wires: legacy projects (and projects saved before the
    # AddWireCommand endpoint-merge fix) may contain pairs of separate
    # ``Wire`` instances meeting end-to-end on an off-pin pad — the
    # underlying model carries them as two artifacts with a degree-1
    # JointKnot each, but they represent one continuous solderable
    # piece.  Run the endpoint-merge pass once at load so the runtime
    # model matches what the user sees.  Run the interior-vertex split
    # first so T-junctions (W1 endpoint on W2 mid-vertex) get cut into
    # three wires meeting at a shared JointKnot.
    #
    # Merging can produce new interior-vertex collisions (the merged
    # wire's interior may now coincide with a third wire's endpoint), so
    # alternate split / merge until both passes report no progress.
    from protoboard.domain.layout.wire_utils import (
        merge_endpoint_wires,
        split_wires_at_endpoint_collisions,
    )
    _total_splits = 0
    _total_fused = 0
    # Same net-conflict gate as save_project (see note there).  Build the
    # components dict from the registered library so cross-net insulated
    # crossings stay as crossings instead of getting spliced into shared
    # hubs.  No-op when no library was registered (legacy tests).
    _components_for_split: "dict[str, object] | None" = None
    if _library_for_migration is not None and hasattr(
        _library_for_migration, "names",
    ):
        _components_for_split = {
            _name: _library_for_migration.get(_name)  # type: ignore[union-attr]
            for _name in _library_for_migration.names()  # type: ignore[union-attr]
        }
    # Canonicalisation: split+merge wire endpoints until stable.  This
    # is a best-effort cleanup — if the saved layout violates internal
    # invariants (orphan knot refs, dangling wire endpoints, etc.), the
    # merge step can raise ValueError.  We catch it so the load still
    # succeeds; the user opens the project in a "potentially
    # inconsistent" state and DRC will flag the issues.
    _canon_failed_at: tuple[str, str] | None = None  # (phase, error_str)
    for _ in range(16):  # hard cap; typical convergence in ≤ 3 rounds
        try:
            _s = split_wires_at_endpoint_collisions(
                layout,
                schematic=schematic,
                instances=instances,
                components=_components_for_split,  # type: ignore[arg-type]
                board=board,
            )
        except (ValueError, KeyError) as exc:
            _canon_failed_at = ("split", str(exc))
            break
        try:
            _f = merge_endpoint_wires(
                layout, board,
                schematic=schematic,
                instances=instances,
                components=_components_for_split,  # type: ignore[arg-type]
            )
        except (ValueError, KeyError) as exc:
            _canon_failed_at = ("merge", str(exc))
            break
        _total_splits += _s
        _total_fused += _f
        if _s == 0 and _f == 0:
            break
    if _total_splits:
        _log.info("project.endpoint_split", splits=_total_splits)
    if _total_fused:
        _log.info("project.endpoint_merge", fused=_total_fused)
    if _canon_failed_at is not None:
        _log.warning(
            "project.canonicalisation_failed",
            phase=_canon_failed_at[0],
            error=_canon_failed_at[1],
            note=(
                "Project loaded with stale internal state. Run DRC to find "
                "and fix orphan/dangling wires."
            ),
        )

    # Prune duplicate-connection wires.  Two wires sharing the same
    # unordered ``{a_knot_id, b_knot_id}`` + side + insulated represent
    # the same physical conductor — legacy projects accumulated these
    # when autoroute commits ran on layouts with stale wire.path data
    # (rotate/drag pipelines pre-2026-06-05 sometimes left wire.path
    # de-synced from knot positions; the router's connectivity graph
    # missed the existing connection and routed a parallel wire).  Now
    # detected and discarded silently at load.  Each kept wire is the
    # *first* one encountered (load order); the dropped ones lose their
    # uuid, but no electrical content.
    if layout.wires:
        from protoboard.domain.layout.wire import Wire as _W
        seen_connection_sigs: set[tuple[object, ...]] = set()
        kept_wires: list[_W] = []
        dropped: list[tuple[str, tuple[object, ...]]] = []
        for w in list(layout.wires):
            if w.a is None or w.b is None:
                kept_wires.append(w)  # type: ignore[arg-type]
                continue
            endpoint_xor = w.a.id.int ^ w.b.id.int
            sig = ("knots", endpoint_xor, w.side, w.insulated)
            if sig in seen_connection_sigs:
                dropped.append((str(w.id), sig))
                # Detach the dropped wire from its endpoint knots so
                # knot.wire_ids no longer references the dead uuid.
                for ref in (w.a, w.b):
                    k = layout.knots.get(ref.id)
                    if k is None:
                        continue
                    new_ids = k.wire_ids - {w.id}
                    # Replace knot in-place.  All three Knot subtypes
                    # take wire_ids as kwarg.
                    from protoboard.domain.layout.knot import (
                        JointKnot as _JK,
                        PinKnot as _PK,
                        WirePinKnot as _WK,
                    )
                    if isinstance(k, _PK):
                        layout.knots[ref.id] = _PK(
                            id=k.id, pin=k.pin, wire_ids=new_ids,
                        )
                    elif isinstance(k, _WK):
                        layout.knots[ref.id] = _WK(
                            id=k.id, position=k.position,
                            net_id=k.net_id, wire_ids=new_ids,
                        )
                    elif isinstance(k, _JK):
                        # Sole-owner free joint with the dead wire as
                        # its only incident → drop the knot.
                        if new_ids:
                            layout.knots[ref.id] = _JK(
                                id=k.id, position=k.position,
                                wire_ids=new_ids,
                            )
                        else:
                            del layout.knots[ref.id]
                continue
            seen_connection_sigs.add(sig)
            kept_wires.append(w)  # type: ignore[arg-type]
        if dropped:
            layout.wires[:] = kept_wires  # type: ignore[assignment]
            layout.invalidate_interior_vertex_index()
            _log.info(
                "project.duplicate_wires_pruned",
                n_dropped=len(dropped),
                n_kept=len(kept_wires),
                n_total_before=len(dropped) + len(kept_wires),
                first_examples=[
                    {"dropped_id": d[0][:8]} for d in dropped[:10]
                ],
            )

    # Snap stale PinKnot-anchored wire endpoints back to their pin's
    # resolved world position.  A previous session's partial macro
    # commit (Move applied but ReplaceWireFragments failed in the same
    # macro) saved a wire whose path[0]/path[-1] is offset from the
    # pin it nominally connects to — usually by the drag delta.  On
    # reload the misalignment seeds the next drag's macro into the
    # same failure mode: ``replace_wire_fragments: new path[0]
    # disagrees with PinKnot position``.  Snap each offending endpoint
    # back to the pin position so the layout opens consistent.
    if _library_for_migration is not None and layout.wires and layout.knots:
        from protoboard.domain.components.package import (
            resolved_footprint as _resolved_footprint,
        )
        from protoboard.domain.layout.knot import PinKnot as _PK
        from protoboard.domain.layout.placement import pin_world_position as _pwp
        snapped_endpoints: list[dict[str, object]] = []
        for w in layout.wires:
            if w.a is None or w.b is None:
                continue
            for endpoint_label, ref, path_idx in (
                ("a", w.a, 0), ("b", w.b, -1),
            ):
                k = layout.knots.get(ref.id)
                if not isinstance(k, _PK):
                    continue
                comp_id = k.pin.component_id
                pl = layout.placements.get(comp_id)
                inst = instances.get(comp_id)
                if pl is None or inst is None:
                    continue
                defn = (
                    _library_for_migration.get(inst.definition_ref)  # type: ignore[attr-defined]
                    if hasattr(_library_for_migration, "get") else None
                )
                if defn is None:
                    continue
                try:
                    # Use the chosen variant's footprint, not the definition
                    # default — else a part saved as e.g. an SMD body (1-hole
                    # pin pitch) is measured against its default axial body
                    # (3.2-hole pitch) and every pin "moves", snapping the whole
                    # wire and dragging neighbours.
                    fp = _resolved_footprint(
                        defn, inst, _families_for_migration)  # type: ignore[arg-type]
                    pin_pos = _pwp(fp, pl, k.pin.pin_number)
                except (IndexError, ValueError):
                    continue
                if not w.path:
                    continue
                cur = w.path[path_idx]
                dv = pin_pos - cur  # Point − Point → Vector
                # Sub-grid drift (displacement < 0.1 raster) is float noise;
                # leave alone.  Sub-pad mismatch (>= 0.4) is what the
                # group-ownership sanity check already rejects — exactly the
                # bug we're repairing.  Snap aggressively.
                if dv.length() < 0.1:
                    continue
                # Translate the WHOLE wire by dv so the other endpoint and
                # intermediate vertices move together — otherwise a
                # single-vertex snap would skew the path.  The other
                # endpoint is checked independently in this loop; if it
                # doesn't need snapping, the per-endpoint delta will be
                # (0, 0) and the wire stops drifting.
                transform_wire_geometry(w, lambda p: p + dv)  # Point + Vector → Point
                snapped_endpoints.append({
                    "wire_id": str(w.id)[:8],
                    "endpoint": endpoint_label,
                    "comp": str(comp_id)[:8],
                    "pin": k.pin.pin_number,
                    "delta": (round(dv.x, 3), round(dv.y, 3)),
                })
        if snapped_endpoints:
            _log.info(
                "project.stale_wire_paths_snapped",
                n_snapped=len(snapped_endpoints),
                examples=snapped_endpoints[:10],
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


# Library handle for in-load legacy v1→v2 migration.  MainWindow sets this
# via ``set_migration_library`` before calling load_project so the migrator
# can resolve pin world positions.  ``None`` means "no library available";
# the load falls back to JointKnot-only knot backfill (acceptable for tests
# that don't exercise PinKnot persistence).
_library_for_migration: "object | None" = None
_families_for_migration: "object | None" = None


def set_migration_library(library: "object | None") -> None:
    """Register the ComponentLibrary used during legacy v1→v2 wire migration."""
    global _library_for_migration
    _library_for_migration = library


def set_migration_families(families: "object | None") -> None:
    """Register the package-family catalogue used at load to resolve each
    component's chosen variant footprint (so stale-path snapping measures pin
    positions against the variant, not the definition default)."""
    global _families_for_migration
    _families_for_migration = families


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
        try:
            appearance = parse_style(raw.get("style"), ctx=f"instance {inst_id}")
        except (TypeError, ValueError) as exc:
            raise SerializationError(str(exc)) from exc
        result[inst_id] = ComponentInstance(
            id=inst_id,
            definition_ref=str(raw["definition_ref"]),
            refdes=str(raw["refdes"]),
            value=str(raw["value"]),
            type=str(raw.get("type", "")),
            # A legacy ``body_variant`` key (pre-package-model projects) is
            # ignored — the user re-picks the body from the package catalogue.
            package_variant=(
                str(raw["package_variant"]) if raw.get("package_variant") else None
            ),
            pin_variant=str(raw["pin_variant"]) if raw.get("pin_variant") else None,
            appearance=appearance,
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
            Vector(float(raw_lo["x"]), float(raw_lo["y"]))
            if raw_lo
            else Vector(0.05, 0.05)
        )
        raw_po = raw.get("pin_offsets") or {}
        pin_offsets = {
            int(k): Vector(float(v["x"]), float(v["y"]))
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
    # Wire format detection: a knot-aware entry carries ``a_knot_id``.
    # Legacy entries (path/side/insulated only) are converted to knot-aware
    # wires later in ``load_project`` once a pin resolver is available.
    # Transition-period projects exist where ``format_version: 2`` was set
    # but the ``wires:`` block still held legacy entries (``wires_v2:`` was
    # the active block); inspect the actual shape rather than trusting the
    # header.
    raw_wires_block = data.get("wires") or []
    raw_wires_v2_block = data.get("wires_v2") or []
    # Use whichever block contains the knot-aware entries.
    if raw_wires_v2_block:
        knot_aware_wires_block = raw_wires_v2_block
        knot_aware = True
    elif raw_wires_block and isinstance(raw_wires_block[0], dict) and "a_knot_id" in raw_wires_block[0]:
        knot_aware_wires_block = raw_wires_block
        knot_aware = True
    else:
        knot_aware_wires_block = []
        knot_aware = False

    legacy_wires: list[Wire] = []
    if not knot_aware:
        for raw in raw_wires_block:
            path = [Point(float(p["x"]), float(p["y"])) for p in raw["path"]]
            legacy_wires.append(
                Wire(
                    id=uuid.UUID(raw["id"]),
                    path=path,
                    insulated=bool(raw["insulated"]),
                    side=raw["side"],
                    color=raw.get("color"),
                )
            )
    from protoboard.domain.layout.group import ComponentGroup

    groups: dict[uuid.UUID, ComponentGroup] = {}
    for raw in data.get("groups") or []:
        gid = uuid.UUID(raw["id"])
        exported_pins: frozenset[PinRef] = frozenset(
            PinRef(
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
            transform=_transform_from_raw(raw.get("transform")),
            lock_internal_wires=bool(raw.get("lock_internal_wires", False)),
        )

    _migrate_encapsulated_exports(groups)

    layout = Layout(
        placements=placements,  # type: ignore[arg-type]  # dict normalised in Layout.__post_init__
        groups=groups,
    )
    if knot_aware:
        _decode_knots_and_wires(data, layout, knot_aware_wires_block)
    else:
        # v1 fallback: legacy wires without knots.  Insert them directly into
        # the storage; the caller (load_project) will invoke ensure_wire_knots
        # once the pin resolver is wired up.
        for w in legacy_wires:
            layout.wires.append(w)
        layout.invalidate_interior_vertex_index()

    layout.ensure_root_group()
    return layout


def _migrate_encapsulated_exports(
    groups: "dict[uuid.UUID, Any]",
) -> None:
    """Strip exports that violate the encapsulated re-export chain.

    Walks *groups* leaves → root, dropping every entry in each group's
    ``exported_pins`` whose component is neither a direct member of the
    group nor re-exported by a direct child.  Required because legacy
    projects (saved before docs/archive/group-export-encapsulation.md was
    enforced) freely listed grandchild pins in any ancestor's flat
    ``exported_pins`` set; with encapsulation in force those entries
    become invisible to the snap layer and renderer, so removing them
    silently is safer than carrying inconsistent state forward.

    The traversal is bottom-up so each parent sees its children's
    *already-migrated* exported sets when computing legality.  Mutates
    *groups* in place (frozen dataclass replacement).
    """
    from protoboard.domain.layout.group import (
        ComponentGroup,
        validate_exported_pins,
    )

    if not groups:
        return

    children_of: dict[uuid.UUID, frozenset[uuid.UUID]] = {
        gid: g.child_group_ids for gid, g in groups.items()
    }
    in_degree = {gid: len(c) for gid, c in children_of.items()}
    # Leaves first (groups with no child groups).
    ordered: list[uuid.UUID] = []
    pending = [gid for gid, d in in_degree.items() if d == 0]
    parent_of: dict[uuid.UUID, uuid.UUID] = {}
    for gid, g in groups.items():
        for cid in g.child_group_ids:
            parent_of[cid] = gid
    while pending:
        gid = pending.pop()
        ordered.append(gid)
        parent = parent_of.get(gid)
        if parent is not None:
            in_degree[parent] -= 1
            if in_degree[parent] == 0:
                pending.append(parent)
    # If there are cycles (shouldn't happen — invariant enforced
    # elsewhere) we still process the remaining groups in arbitrary order;
    # the invariant won't be perfectly enforced for the cycle members but
    # we don't want migration to crash the load.
    for gid in groups:
        if gid not in ordered:
            ordered.append(gid)

    for gid in ordered:
        invalid = validate_exported_pins(groups, gid)
        if not invalid:
            continue
        g = groups[gid]
        new_exported = g.exported_pins - invalid
        # ``replace`` carries every field (incl. ``transform``) — re-listing
        # them by hand would silently drop new fields.
        groups[gid] = g.replace(exported_pins=new_exported)
        _log.info(
            "load.migrate_encapsulated_exports",
            group_id=str(gid)[:8],
            group_name=g.name,
            stripped=len(invalid),
            kept=len(new_exported),
        )


def _decode_knots_and_wires(
    data: dict[str, Any],
    layout: Layout,
    raw_wires: list[dict[str, Any]] | None = None,
) -> None:
    from protoboard.domain.layout.knot import (
        JointKnot, Knot, PinKnot, WirePinKnot,
    )
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
        elif kind == "wire_pin":
            pos = Point(
                float(raw["position"]["x"]),
                float(raw["position"]["y"]),
            )
            raw_net = raw.get("net_id")
            net_id = uuid.UUID(raw_net) if raw_net is not None else None
            knot = WirePinKnot(id=kid, position=pos, net_id=net_id)
        else:
            raise SerializationError(f"unknown knot kind: {kind!r}")
        layout.knots[kid] = knot

    wires_to_decode: list[dict[str, Any]] = (
        raw_wires if raw_wires is not None else (data.get("wires") or [])
    )
    for raw in wires_to_decode:
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
        layout.add_wire(wire)


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
        "hierarchical": s.hierarchical,
        "enable_simplify_pass": s.enable_simplify_pass,
        "simplify_after_each_wire": s.simplify_after_each_wire,
        "simplify_all_after_autoroute": s.simplify_all_after_autoroute,
        "simplify_all_max_iterations": s.simplify_all_max_iterations,
        "visual_unweave_side_flip": s.visual_unweave_side_flip,
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
        hierarchical=bool(data.get("hierarchical", False)),
        enable_simplify_pass=bool(data.get("enable_simplify_pass", True)),
        simplify_after_each_wire=bool(data.get("simplify_after_each_wire", False)),
        simplify_all_after_autoroute=bool(data.get("simplify_all_after_autoroute", True)),
        simplify_all_max_iterations=int(data.get("simplify_all_max_iterations", 50)),
        visual_unweave_side_flip=bool(data.get("visual_unweave_side_flip", False)),
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
