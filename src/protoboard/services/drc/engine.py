# SPDX-License-Identifier: MIT
"""DRCEngine — checks SHORT, MISSING_NET, OVERLAP."""

from __future__ import annotations

import threading
import uuid
from typing import TYPE_CHECKING

import structlog

from protoboard.controller.ports.drc import DRCIssue
from protoboard.domain.axis_labels_settings import (
    AxisLabelFormat,
    AxisLabelSettings,
    format_index,
)
from protoboard.domain.board.board import Board
from protoboard.domain.board.layer import LayerId
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package.kind import is_generic_package
from protoboard.domain.components.package.mask import (
    PadCell,
    build_pad_mask,
    masks_collide,
)
from protoboard.domain.components.package.pin_mapping import (
    meta_pin_for_package_pin,
)
from protoboard.domain.components.package.reach import is_pin_bendable
from protoboard.domain.components.resolution import (
    iter_placed_components,
    resolve_placed_component,
)
from protoboard.domain.connectivity.incremental import ConnectivityGraph
from protoboard.domain.layout.group_tree import GroupTree
from protoboard.domain.layout.knot import JointKnot, WirePinKnot
from protoboard.domain.layout.layout import ROOT_GROUP_ID, Layout
from protoboard.domain.layout.placement import (
    iter_footprint_pin_positions,
    placement_transform,
    resolve_pin_world_position,
)
from protoboard.domain.layout.wire_utils import (
    board_pads_for_side,
    board_pads_union,
    plated_through_pads,
    point_on_pads,
)
from protoboard.domain.schematic.net import PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point, segments_cross_strict

if TYPE_CHECKING:
    from protoboard.domain.board.pad_state import PadIndex
    from protoboard.domain.components.package.shapes import Package
    from protoboard.domain.layout.placement import ComponentPlacement
    from protoboard.domain.layout.wire import Wire

_log = structlog.get_logger(__name__)
# ---------------------------------------------------------------------------
# Human-readable formatting helpers
# ---------------------------------------------------------------------------


def _format_location(p: Point, axis_labels: AxisLabelSettings | None) -> str:
    """Render a board point as the user-facing column/row label.

    Pad centres sit at half-integer coordinates (col+0.5, row+0.5), so the
    integer column index is ``int(p.x)`` and row index is ``int(p.y)``.
    When axis labels are configured, both halves are formatted via the
    user's chosen scheme (numeric/alphabetic).  When labels are disabled
    or unset, we fall back to a plain ``col N, row M`` form so the message
    still makes sense.
    """
    col_i = int(p.x)
    row_i = int(p.y)
    if axis_labels is None or not axis_labels.any_enabled():
        return f"col {col_i}, row {row_i}"
    col_str = (
        format_index(axis_labels.columns, col_i)
        if axis_labels.columns != AxisLabelFormat.DISABLED
        else str(col_i)
    )
    row_str = (
        format_index(axis_labels.rows, row_i)
        if axis_labels.rows != AxisLabelFormat.DISABLED
        else str(row_i)
    )
    # Compact form when both axes use single-char alphabetic + 1-based numeric
    # (the typical case): "B6".  Otherwise fall back to a labelled form.
    if (
        axis_labels.columns == AxisLabelFormat.ALPHABETIC
        or axis_labels.rows == AxisLabelFormat.ALPHABETIC
    ):
        return f"{col_str}{row_str}"
    return f"col {col_str}, row {row_str}"


def _format_pin(
    ref: PinRef,
    instances: dict[uuid.UUID, ComponentInstance],
) -> str:
    """Render a PinRef as ``RefDes/pin`` (e.g. ``R12/2``).

    Falls back to a short UUID prefix when the instance can't be resolved
    so messages remain non-empty even on stale references.
    """
    inst = instances.get(ref.component_id)
    if inst is not None and inst.refdes:
        return f"{inst.refdes}/{ref.pin_number}"
    return f"<{str(ref.component_id)[:8]}>/{ref.pin_number}"


def _format_pin_list(
    refs: list[PinRef],
    instances: dict[uuid.UUID, ComponentInstance],
    max_items: int = 3,
) -> str:
    """Comma-joined pin list, truncated to *max_items* with an overflow tag."""
    parts = [_format_pin(r, instances) for r in refs[:max_items]]
    if len(refs) > max_items:
        parts.append(f"and {len(refs) - max_items} more")
    return ", ".join(parts)


def _format_nets(
    net_ids: list[uuid.UUID],
    schematic: Schematic,
) -> str:
    """Comma-joined quoted net names for the given net IDs."""
    names: list[str] = []
    for nid in net_ids:
        net = schematic.nets.get(nid)
        if net is not None:
            names.append(f"{net.name!r}")
        else:
            names.append(f"<{str(nid)[:8]}>")
    return ", ".join(names)


class DRCEngine:
    def check(
        self,
        board: Board,
        schematic: Schematic,
        layout: Layout,
        components: dict[str, ComponentDefinition],
        instances: dict[uuid.UUID, ComponentInstance],
        axis_labels: AxisLabelSettings | None = None,
        pad_index: PadIndex | None = None,
    ) -> list[DRCIssue]:
        graph = ConnectivityGraph(
            board=board,
            layout=layout,
            components=components,
            instances=instances,
        )
        return self.check_with_graph(
            schematic=schematic,
            layout=layout,
            components=components,
            instances=instances,
            graph=graph,
            board=board,
            axis_labels=axis_labels,
            pad_index=pad_index,
        )

    def check_with_graph(
        self,
        schematic: Schematic,
        layout: Layout,
        components: dict[str, ComponentDefinition],
        instances: dict[uuid.UUID, ComponentInstance],
        graph: ConnectivityGraph,
        cancel_flag: threading.Event | None = None,
        axis_labels: AxisLabelSettings | None = None,
        board: Board | None = None,
        pad_index: PadIndex | None = None,
    ) -> list[DRCIssue]:
        # Cancellation: callers running the engine off the main thread
        # pass an Event; the engine bails between checks and inside the
        # most expensive nested loop (interior cross is O(N²·M²) on wire
        # paths).  Returning the partial issue list keeps the contract
        # the same regardless of whether cancel fired — the caller is
        # responsible for discarding the result on a true cancel.
        def _cancelled() -> bool:
            return cancel_flag is not None and cancel_flag.is_set()

        issues: list[DRCIssue] = []
        issues += _check_overlap(layout, components, instances, axis_labels)
        if _cancelled():
            return issues
        issues += _check_body_overlap(layout, components, instances, axis_labels)
        if _cancelled():
            return issues
        issues += _check_short(
            schematic,
            layout,
            graph,
            instances,
            components=components,
            axis_labels=axis_labels,
        )
        if _cancelled():
            return issues
        issues += _check_missing_net(schematic, graph, instances)
        if _cancelled():
            return issues
        issues += _check_floating_pins(schematic, layout, instances)
        if _cancelled():
            return issues
        issues += _check_bare_wire_cross(schematic, layout, graph, axis_labels)
        if _cancelled():
            return issues
        issues += _check_bare_wire_interior_cross(
            layout,
            graph,
            axis_labels,
            cancel_flag=cancel_flag,
        )
        if _cancelled():
            return issues
        if board is not None:
            issues += _check_off_pad_joint(
                layout,
                board=board,
                axis_labels=axis_labels,
                pad_index=pad_index,
            )
            if _cancelled():
                return issues
            issues += _check_off_board_pins(
                board,
                layout,
                components,
                instances,
                axis_labels,
                pad_index=pad_index,
            )
            if _cancelled():
                return issues
            issues += _check_wire_pin_on_plated_through(
                layout,
                board=board,
                axis_labels=axis_labels,
                pad_index=pad_index,
            )
            if _cancelled():
                return issues
        issues += _check_half_orphan_wires(layout, axis_labels)
        if _cancelled():
            return issues
        issues += _check_group_exports(schematic, layout, instances)
        counts: dict[str, int] = {"error": 0, "warning": 0, "info": 0}
        for iss in issues:
            counts[iss.severity] = counts.get(iss.severity, 0) + 1
        _log.info(
            "drc.complete",
            total=len(issues),
            errors=counts["error"],
            warnings=counts["warning"],
            infos=counts["info"],
            codes=[iss.code for iss in issues],
        )
        return issues


def _check_overlap(
    layout: Layout,
    components: dict[str, ComponentDefinition],
    instances: dict[uuid.UUID, ComponentInstance],
    axis_labels: AxisLabelSettings | None = None,
) -> list[DRCIssue]:
    """Detect two component pins occupying the same pad on the same side.

    Skipped when every conflicting package pin is bendable, so sharing the
    hole is intentional.
    Partial bendability (only some pins declare it) still flags as a
    conflict so accidental overlaps aren't silently swallowed.
    """
    # Build: (x, y, side) → list of (comp_id, pin)
    pad_occupancy: dict[tuple[float, float, str], list[tuple[uuid.UUID, int]]] = {}
    for component in iter_placed_components(layout.placements, instances, components):
        comp_id = component.placement.component_id
        footprint = component.instance.resolved_footprint()
        for pin_num, wp in iter_footprint_pin_positions(footprint, component.placement):
            key = (wp.x, wp.y, component.placement.side)
            pad_occupancy.setdefault(key, []).append((comp_id, pin_num))

    issues: list[DRCIssue] = []
    for (x, y, side), occupants in pad_occupancy.items():
        if len(occupants) <= 1:
            continue
        # Filter out pairs where every occupant has the pin in shared_pins
        # (intentional sharing agreed by the user).
        unshared = [
            (cid, pnum)
            for cid, pnum in occupants
            if pnum
            not in (
                layout.placements[cid].shared_pins
                if cid in layout.placements
                else frozenset()
            )
        ]
        if len(unshared) <= 1:
            continue

        # Skip when every conflicting pin is declared bendable in its
        # resolved package — the user has marked those leads as
        # legitimately shareable.
        def _is_bendable(cid: uuid.UUID, pin_num: int) -> bool:
            component = resolve_placed_component(
                cid, layout.placements, instances, components
            )
            if component is None:
                return False
            pkg = component.instance.resolved_package()
            return is_pin_bendable(pkg, pin_num)

        if all(_is_bendable(cid, p) for cid, p in unshared):
            continue
        affected_pins: list[PinRef] = []
        for cid, package_pin_number in occupants:
            placed = resolve_placed_component(
                cid, layout.placements, instances, components
            )
            if placed is None:
                continue
            meta_pin_index = meta_pin_for_package_pin(
                placed.definition, placed.instance, package_pin_number
            )
            if meta_pin_index is not None:
                affected_pins.append(PinRef(cid, meta_pin_index))
        affected_components = sorted({cid for cid, _ in occupants}, key=str)
        loc_str = _format_location(Point(x, y), axis_labels)
        pin_list = _format_pin_list(affected_pins, instances, max_items=4)
        issues.append(
            DRCIssue(
                severity="error",
                code="OVERLAP",
                message=(
                    f"Multiple component pins share a pad at {loc_str} "
                    f"({side}): {pin_list}"
                ),
                locations=[Point(x, y)],
                affected_nets=[],
                affected_pins=affected_pins,
                affected_components=affected_components,
            )
        )
    return issues


def _placed_pad_mask(
    package: Package,
    defn: ComponentDefinition,
    placement: ComponentPlacement,
) -> dict[Point, PadCell]:
    """World-space side-aware pad mask for a placed packaged component.

    *package* comes from ``instance.resolved_package()`` and follows the placed
    variant rather than the definition's default option. ``build_pad_mask``
    works in the package's local frame (pin 1 at the origin, no rotation);
    we transform each hole into
    world coordinates with the same ``placement_transform`` as
    ``pin_world_position`` — the bottom-side flip reflection about the package
    axis, rotation, translation. ``mount_side`` is the placement side so the
    mask lights the correct PIN_/BODY_ side bits; ``body_blocks`` follows the
    definition flag so a flush/metal body lights the pin-side bits too.
    """
    side: LayerId = "bottom" if placement.side == "bottom" else "top"
    local = build_pad_mask(
        package,
        mount_side=side,
        body_blocks=getattr(defn, "body_blocks_routing", False),
    )
    to_world = placement_transform(package, placement)
    out: dict[Point, PadCell] = {}
    for hole, bits in local.items():
        world = to_world.apply(hole)
        out[world] = out.get(world, PadCell(0)) | bits
    return out


def _check_body_overlap(
    layout: Layout,
    components: dict[str, ComponentDefinition],
    instances: dict[uuid.UUID, ComponentInstance],
    axis_labels: AxisLabelSettings | None = None,
) -> list[DRCIssue]:
    """Detect two component bodies physically colliding (package-mask AND).

    Each packaged component contributes a side-aware pad mask; two masks
    collide where their bitwise AND is non-zero on any shared hole (see
    ``mask.masks_collide``). This catches body-on-body overlap and a part
    mounted under a blocking body — cases the pin-on-pad ``OVERLAP`` check
    does not cover.

    Pin-on-pad conflicts that ``_check_overlap`` already owns *with* the
    bendable/shared-pin exemption are suppressed here: a contested hole whose
    only shared bits are pin bits, and whose pins are all bendable or shared,
    is intentional and is not re-reported. Bodies (BODY_/blocking) always flag.
    """
    # The mask geometry comes from instance.resolved_package():
    # the definition's default body may be a different footprint option than
    # the variant actually placed (an SMD chip must not collide via the
    # phantom axial body of its part's default option).
    placed: list[tuple[uuid.UUID, ComponentPlacement, ComponentDefinition, Package]] = (
        []
    )
    for component in iter_placed_components(layout.placements, instances, components):
        cid = component.placement.component_id
        pkg = component.instance.resolved_package()
        if is_generic_package(pkg):
            continue
        placed.append((cid, component.placement, component.definition, pkg))
    if len(placed) < 2:
        return []

    masks: dict[uuid.UUID, dict[Point, PadCell]] = {
        cid: _placed_pad_mask(pkg, defn, pl) for cid, pl, defn, pkg in placed
    }
    _PIN_BITS = PadCell.PIN | PadCell.PIN_TOP | PadCell.PIN_BOTTOM
    _BODY_BITS = PadCell.BODY_TOP | PadCell.BODY_BOTTOM

    def _pin_at(cid: uuid.UUID, hole: Point) -> int | None:
        inst = instances[cid]
        pl = layout.placements[cid]
        footprint = inst.resolved_footprint()
        for pin_num, wp in iter_footprint_pin_positions(footprint, pl):
            if wp.x == hole.x and wp.y == hole.y:
                return pin_num
        return None

    def _pin_exempt(cid: uuid.UUID, hole: Point) -> bool:
        """Whether the pin (if any) of *cid* at *hole* is intentionally shared."""
        pin_num = _pin_at(cid, hole)
        if pin_num is None:
            return False
        pl = layout.placements[cid]
        if pin_num in pl.shared_pins:
            return True
        pkg = instances[cid].resolved_package()
        return is_pin_bendable(pkg, pin_num)

    issues: list[DRCIssue] = []
    ids = [cid for cid, _, _, _ in placed]
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            contested = masks_collide(masks[a], masks[b])
            real: dict[Point, PadCell] = {}
            for hole, shared in contested.items():
                # Suppress holes whose only contest is intentional pin sharing.
                if not (shared & ~_PIN_BITS):
                    if _pin_exempt(a, hole) and _pin_exempt(b, hole):
                        continue
                real[hole] = shared
            if not real:
                continue
            locs = sorted(real.keys(), key=lambda p: (p.y, p.x))
            loc_str = _format_location(locs[0], axis_labels)
            ra = instances[a].refdes
            rb = instances[b].refdes
            # The PIN bit pulls pure pin-in-hole conflicts into this check
            # (e.g. two through-hole leads in one hole from opposite sides).
            # Keep the message truthful: only call it a body overlap when a
            # body bit is actually involved on at least one contested hole.
            body_involved = any(bits & _BODY_BITS for bits in real.values())
            if body_involved:
                msg = (
                    f"Component bodies overlap: {ra} and {rb} "
                    f"({len(real)} hole(s), e.g. {loc_str})"
                )
            else:
                msg = (
                    f"Component pins collide in a hole: {ra} and {rb} "
                    f"({len(real)} hole(s), e.g. {loc_str})"
                )
            issues.append(
                DRCIssue(
                    severity="error",
                    code="BODY_OVERLAP",
                    message=msg,
                    locations=locs,
                    affected_nets=[],
                    affected_pins=[],
                    affected_components=sorted({a, b}, key=str),
                )
            )
    return issues


def _check_short(
    schematic: Schematic,
    layout: Layout,
    graph: ConnectivityGraph,
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition] | None = None,
    axis_labels: AxisLabelSettings | None = None,
) -> list[DRCIssue]:
    """Detect pads from different nets connected in the physical layout."""
    issues: list[DRCIssue] = []
    # Gather physical node → set of logical net IDs and which pins land there.
    node_nets: dict[tuple[float, float, str], set[uuid.UUID]] = {}
    node_pins: dict[tuple[float, float, str], list[PinRef]] = {}
    for ref, net in schematic.pin_net_map().items():
        node = graph.node_for_pin(ref)
        if node is None:
            continue
        node_nets.setdefault(node, set()).add(net.id)
        node_pins.setdefault(node, []).append(ref)

    # Wires whose endpoints land on the shorted node are likely the
    # physical bridge — surface them so the GUI can highlight them on click.
    def _wires_touching(node: tuple[float, float, str]) -> list[uuid.UUID]:
        nx, ny, _ = node
        out: list[uuid.UUID] = []
        for w in layout.wires:
            for pt in (w.path[0], w.path[-1]):
                if pt.x == nx and pt.y == ny:
                    out.append(w.id)
                    break
        return out

    # Resolve a pin world position (used to point the GUI's "centre on
    # SHORT" action at a real, visible pad instead of the electric set's
    # arbitrary canonical key — which is the lex-min (x, y, side) of all
    # members and can fall on (0, 0) for a multi-net cluster spanning
    # the whole board).  Thin wrapper over the canonical safety chain.
    def _pin_pos(ref: PinRef) -> Point | None:
        if components is None:
            return None
        return resolve_pin_world_position(
            ref.component_id,
            ref.pin_number,
            placements=layout.placements,
            instances=instances,
            components=components,
        )

    for node, net_ids in node_nets.items():
        if len(net_ids) > 1:
            nx, ny, side = node
            net_id_list = sorted(net_ids, key=str)
            pins_here = node_pins.get(node, [])
            wires_here = _wires_touching(node)
            # Pick a visible location for the GUI's "centre on issue"
            # action.  Preference order: first pin endpoint resolved
            # via the components/instances lookup; failing that, first
            # endpoint of any wire that touches the shorted node;
            # falling back to the canonical electric-set key (which
            # may be (0, 0) — not visually useful but always set).
            loc_pt: Point = Point(nx, ny)
            for ref in pins_here:
                p = _pin_pos(ref)
                if p is not None:
                    loc_pt = p
                    break
            else:
                if wires_here:
                    w = layout.wires.get(wires_here[0])
                    if w is not None and w.path:
                        loc_pt = w.path[0]
            net_str = _format_nets(net_id_list, schematic)
            pin_str = _format_pin_list(pins_here, instances, max_items=4)
            loc_str = _format_location(loc_pt, axis_labels)
            msg = (
                f"Short between nets {net_str} at {loc_str} ({side}): {pin_str}"
                if pins_here
                else f"Short between nets {net_str} at {loc_str} ({side})"
            )
            issues.append(
                DRCIssue(
                    severity="error",
                    code="SHORT",
                    message=msg,
                    locations=[loc_pt],
                    affected_nets=net_id_list,
                    affected_pins=pins_here,
                    affected_wires=wires_here,
                )
            )
    return issues


def _check_floating_pins(
    schematic: Schematic,
    layout: Layout,
    instances: dict[uuid.UUID, ComponentInstance],
) -> list[DRCIssue]:
    """Warn about nets whose pins are not placed on the board at all."""
    issues: list[DRCIssue] = []
    for net in schematic.nets.values():
        for ref in net.pins:
            if ref.component_id not in layout.placements:
                issues.append(
                    DRCIssue(
                        severity="warning",
                        code="FLOATING_PIN",
                        message=(
                            f"Net {net.name!r}: pin {_format_pin(ref, instances)} "
                            f"is not placed on the board"
                        ),
                        locations=[],
                        affected_nets=[net.id],
                        affected_pins=[ref],
                    )
                )
    return issues


def _check_bare_wire_cross(
    schematic: Schematic,
    layout: Layout,
    graph: ConnectivityGraph,
    axis_labels: AxisLabelSettings | None = None,
) -> list[DRCIssue]:
    """Detect bare wires of different nets sharing a pad (electrical cross).

    A bare wire is assigned to the net whose pins it directly connects to
    (checking the pin-world-positions against wire endpoints). Two bare wires
    that share a pad and belong to different nets constitute an illegal cross.
    """
    bare_wires = [w for w in layout.wires if not w.insulated]
    if len(bare_wires) < 2:
        return []

    # Map (x, y, side) pad → set of net IDs whose bare wires pass through it.
    # We use the schematic-level net assignment: a pad that belongs to a net
    # (via pin_world_position) tags every bare wire passing through it.
    pad_net_ids: dict[tuple[float, float, str], set[uuid.UUID]] = {}

    # Build pad → net mapping from placed component pins.
    # Register both sides for each pad so that plated-through pads (where top
    # and bottom are one electrical node) are found regardless of wire side.
    for ref, net in schematic.pin_net_map().items():
        n = graph.node_for_pin(ref)
        if n is None:
            continue
        nx, ny, _ = n
        for s in ("top", "bottom"):
            pad_net_ids.setdefault((nx, ny, s), set()).add(net.id)

    # For each bare wire path point, collect which nets are declared at that pad.
    wire_pad_nets: dict[uuid.UUID, set[uuid.UUID]] = {}
    for wire in bare_wires:
        for pt in wire.path:
            key = (pt.x, pt.y, wire.side)
            nets_here = pad_net_ids.get(key, set())
            wire_pad_nets.setdefault(wire.id, set()).update(nets_here)

    # Find pads shared by two bare wires from different declared nets.
    pad_wire_ids: dict[tuple[float, float, str], list[uuid.UUID]] = {}
    for wire in bare_wires:
        for pt in wire.path:
            key = (pt.x, pt.y, wire.side)
            pad_wire_ids.setdefault(key, []).append(wire.id)

    issues: list[DRCIssue] = []
    reported: set[frozenset[uuid.UUID]] = set()
    for (x, y, side), wids in pad_wire_ids.items():
        if len(wids) < 2:
            continue
        # Collect all net IDs from wires that pass through this pad.
        nets_at_pad: set[uuid.UUID] = set()
        for wid in wids:
            nets_at_pad.update(wire_pad_nets.get(wid, set()))
        if len(nets_at_pad) < 2:
            continue
        key_f = frozenset(nets_at_pad)
        if key_f in reported:
            continue
        reported.add(key_f)
        nets_sorted = sorted(nets_at_pad, key=str)
        issues.append(
            DRCIssue(
                severity="error",
                code="BARE_WIRE_CROSS",
                message=(
                    f"Bare wires of different nets ({_format_nets(nets_sorted, schematic)}) "  # noqa: E501
                    f"cross at {_format_location(Point(x, y), axis_labels)} ({side})"
                ),
                locations=[Point(x, y)],
                affected_nets=nets_sorted,
                affected_wires=list(wids),
            )
        )
    return issues


def _check_bare_wire_interior_cross(
    layout: Layout,
    graph: ConnectivityGraph,
    axis_labels: AxisLabelSettings | None = None,
    cancel_flag: threading.Event | None = None,
) -> list[DRCIssue]:
    """Detect bare wires on the same side that cross at an off-pad interior
    point and belong to different electric sets — a physical short the
    pad-overlap check cannot see because the crossing is between pads.

    Dominant check by cost on real boards: O(N²·M²) where N = wires per
    side and M = path-segments per wire.  Accepts ``cancel_flag`` so a
    worker can bail out promptly when the user keeps making layout
    changes; the partial result is returned and the caller decides
    whether to discard it.
    """
    bare = [w for w in layout.wires if not w.insulated]
    if len(bare) < 2:
        return []

    by_side: dict[str, list[Wire]] = {}
    for w in bare:
        by_side.setdefault(w.side, []).append(w)

    issues: list[DRCIssue] = []
    reported: set[tuple[float, float, str]] = set()
    for side, ws in by_side.items():
        for i in range(len(ws)):
            # Check the cancel flag once per outer-loop iteration — fine
            # granularity for ms-level responsiveness even on multi-second
            # checks, and the overhead vs the inner work is negligible.
            if cancel_flag is not None and cancel_flag.is_set():
                return issues
            wi = ws[i]
            k_i = graph.node_for_pad(wi.path[0], side) or graph.node_for_pad(
                wi.path[-1], side
            )
            for j in range(i + 1, len(ws)):
                wj = ws[j]
                k_j = graph.node_for_pad(wj.path[0], side) or graph.node_for_pad(
                    wj.path[-1], side
                )
                # Same electric set — geometric crossing is self-overlap,
                # electrically fine.
                if k_i is not None and k_j is not None and k_i == k_j:
                    continue
                for a in range(len(wi.path) - 1):
                    for b in range(len(wj.path) - 1):
                        if not segments_cross_strict(
                            wi.path[a],
                            wi.path[a + 1],
                            wj.path[b],
                            wj.path[b + 1],
                        ):
                            continue
                        # Locate the intersection (line-line) for reporting.
                        ax1, ay1 = wi.path[a].x, wi.path[a].y
                        ax2, ay2 = wi.path[a + 1].x, wi.path[a + 1].y
                        bx1, by1 = wj.path[b].x, wj.path[b].y
                        bx2, by2 = wj.path[b + 1].x, wj.path[b + 1].y
                        dax, day = ax2 - ax1, ay2 - ay1
                        dbx, dby = bx2 - bx1, by2 - by1
                        denom = dax * dby - day * dbx
                        if denom == 0.0:
                            px, py = ax1, ay1
                        else:
                            t = ((bx1 - ax1) * dby - (by1 - ay1) * dbx) / denom
                            px = ax1 + t * dax
                            py = ay1 + t * day
                        key = (round(px, 4), round(py, 4), side)
                        if key in reported:
                            continue
                        reported.add(key)
                        issues.append(
                            DRCIssue(
                                severity="error",
                                code="BARE_WIRE_INTERIOR_CROSS",
                                message=(
                                    f"Bare wires cross between pads near "
                                    f"{_format_location(Point(px, py), axis_labels)} "
                                    f"({side})"
                                ),
                                locations=[Point(px, py)],
                                affected_nets=[],
                                affected_wires=[wi.id, wj.id],
                            )
                        )
    return issues


def _check_missing_net(
    schematic: Schematic,
    graph: ConnectivityGraph,
    instances: dict[uuid.UUID, ComponentInstance],
) -> list[DRCIssue]:
    """Detect nets whose pins are not fully connected in the physical layout.

    Groups placed pins by their physical electric-set node.  A net with
    pins landing in two or more distinct nodes is incomplete; the message
    lists the disconnected pin groups so the user can see *which* pins
    need a wire (not just that the net is broken).
    """
    issues: list[DRCIssue] = []
    for net in schematic.nets.values():
        pins = list(net.pins)
        if len(pins) < 2:
            continue
        # Bucket placed pins by their physical electric-set node.
        groups: dict[tuple[float, float, str], list[PinRef]] = {}
        for ref in pins:
            node = graph.node_for_pin(ref)
            if node is None:
                continue  # unplaced — handled by _check_floating_pins
            groups.setdefault(node, []).append(ref)
        if len(groups) < 2:
            continue  # fully connected (or fewer than 2 placed pins)

        # Build a flat list of "GROUP1 pins vs. GROUP2 pins vs. …" for the
        # message, truncated to keep it readable.  affected_pins gets the
        # full list so the GUI can highlight every disconnected pin.
        group_lists = list(groups.values())
        all_disconnected: list[PinRef] = []
        group_strs: list[str] = []
        for grp in group_lists[:3]:
            all_disconnected.extend(grp)
            group_strs.append(f"[{_format_pin_list(grp, instances, max_items=3)}]")
        if len(group_lists) > 3:
            extra_pins = sum(len(g) for g in group_lists[3:])
            group_strs.append(
                f"and {len(group_lists) - 3} more group(s), {extra_pins} pins"
            )
            for grp in group_lists[3:]:
                all_disconnected.extend(grp)

        issues.append(
            DRCIssue(
                severity="error",
                code="MISSING_NET",
                message=(
                    f"Net {net.name!r}: pins in {len(group_lists)} disconnected "
                    f"groups — " + " vs. ".join(group_strs)
                ),
                locations=[],
                affected_nets=[net.id],
                affected_pins=all_disconnected,
            )
        )
    return issues


def _check_off_pad_joint(
    layout: Layout,
    board: Board,
    axis_labels: AxisLabelSettings | None,
    *,
    pad_index: PadIndex | None = None,
) -> list[DRCIssue]:
    """v2-model invariant: every JointKnot must sit on a board pad.

    Off-pad t-joints are physically meaningless — the wires meeting there
    would not actually be in galvanic contact.  Migration from legacy
    projects can generate them when a wire endpoint sits mid-trace
    between pads (typically a data-entry error).  This check surfaces
    each as an error so the user can resolve them by hand.

    Fast path: when *pad_index* is supplied, "is this position a
    pad?" reduces to a single dict lookup per joint via
    :meth:`PadIndex.pad_at`. The legacy ``board_pads_for_side`` set
    rebuild fires only when the engine is called without a pad_index
    (DRC-from-disk smoke tests, scripts).
    """
    if not layout.knots:
        return []
    all_pads: set[Point] | None = None
    if pad_index is None:
        all_pads = board_pads_union(board)
    issues: list[DRCIssue] = []
    for knot in layout.knots.values():
        if not isinstance(knot, JointKnot):
            continue
        pos = knot.position
        if pad_index is not None:
            if pad_index.pad_at(pos) is not None:
                continue
        elif point_on_pads(pos, all_pads or set()):
            continue
        affected_wires = [wid for wid in knot.wire_ids]
        loc_label = _format_location(pos, axis_labels)
        issues.append(
            DRCIssue(
                severity="error",
                code="OFF_PAD_JOINT",
                message=(
                    f"T-joint at {loc_label} is not on any board pad. "
                    f"Move the joint or remove the wires meeting there."
                ),
                locations=[pos],
                affected_nets=[],
                affected_wires=affected_wires,
            )
        )
    return issues


def _check_off_board_pins(
    board: Board,
    layout: Layout,
    components: dict[str, ComponentDefinition],
    instances: dict[uuid.UUID, ComponentInstance],
    axis_labels: AxisLabelSettings | None,
    *,
    pad_index: PadIndex | None = None,
) -> list[DRCIssue]:
    """Every placed component pin must land on an existing board pad.

    Covers two failure modes the user cares about together: the component
    is dragged off the board outline, or it spans a hole between pad
    clusters (cuts, missing strip segments).  Per-pin granularity rather
    than bbox so that *partial* off-board placements surface the exact
    pins that won't have a routing target.

    Pin is checked against pads accessible from the component's own side
    (``board_pads_for_side`` already includes plated-through pads on both
    sides).  Reuses code ``OFF_BOARD_PIN`` so the autoroute precondition
    treats engine-time and runtime detections identically.

    Fast path: when *pad_index* is supplied, each pin's "on a board
    pad?" check reduces to one :meth:`PadIndex.pad_at` lookup +
    :meth:`PadLayers.has_layer` test.  Legacy fallback (no pad_index)
    rebuilds the two ``board_pads_for_side`` sets per call.
    """
    pads_by_side: dict[str, set[Point]] | None = None
    if pad_index is None:
        pads_by_side = {
            "top": board_pads_for_side(board, "top"),
            "bottom": board_pads_for_side(board, "bottom"),
        }

    issues: list[DRCIssue] = []
    for component in iter_placed_components(layout.placements, instances, components):
        comp_id = component.placement.component_id
        side = component.placement.side
        fp = component.instance.resolved_footprint()
        for pin_num, wp in iter_footprint_pin_positions(fp, component.placement):
            on_pad = False
            if pad_index is not None:
                pad = pad_index.pad_at(wp)
                # Pin is accessible from `side` when either the pad
                # has copper on that side or the pad is plated-through
                # (the pin physically bridges both layers through the
                # hole — rule 3 / 5).
                if pad is not None and (
                    pad.layers.has_layer(side) or pad.layers.plated_through
                ):
                    on_pad = True
            else:
                assert pads_by_side is not None
                if point_on_pads(wp, pads_by_side[side]):
                    on_pad = True
            if on_pad:
                continue
            loc_label = _format_location(wp, axis_labels)
            meta_pin_index = meta_pin_for_package_pin(
                component.definition, component.instance, pin_num
            )
            pin_name = (
                str(pin_num)
                if meta_pin_index is None
                else component.definition.meta_pin(meta_pin_index).labels[0]
            )
            affected_pins = (
                []
                if meta_pin_index is None
                else [PinRef(component_id=comp_id, pin_number=meta_pin_index)]
            )
            issues.append(
                DRCIssue(
                    severity="error",
                    code="OFF_BOARD_PIN",
                    message=(
                        f"Pin {pin_num} ({pin_name}) of {component.instance.refdes} at "
                        f"{loc_label} is not on a board pad on the "
                        f"{component.placement.side} side. Move the component onto "
                        f"the board."
                    ),
                    locations=[wp],
                    affected_nets=[],
                    affected_pins=affected_pins,
                    affected_components=[comp_id],
                )
            )
    return issues


def _check_group_exports(
    schematic: Schematic,
    layout: Layout,
    instances: dict[uuid.UUID, ComponentInstance],
) -> list[DRCIssue]:
    """Validate that every non-root group's ``exported_pins`` reflects its
    external interface.

    For each group G (recursive — includes nested sub-groups, skips ROOT):
      * Every entry in ``G.exported_pins`` must satisfy the encapsulated
        re-export chain — the pin's component is either a direct member
        of G or has been re-exported by a direct child of G.  Otherwise
        emit ``GROUP_INVALID_EXPORT`` (error).  Checked first so chain
        violations don't mask the other classes below.
      * For every *external net* of G — one whose pins include at least one
        component outside G's recursive component set, **or** is a global
        net (VCC/GND) — at least one of G's pins on that net must appear in
        ``G.exported_pins``.  Otherwise emit ``GROUP_MISSING_EXPORT``
        (error; autoroute precondition).
      * Every entry in ``G.exported_pins`` whose net is *not* external
        (purely internal to G, non-global) is a stale export — emit
        ``GROUP_BOGUS_EXPORT`` (warning).

    Pins in ``exported_pins`` that don't reference any net at all are
    silently ignored — they're either orphan exports or unplaced pins, and
    the user explicitly chose to suppress that case.
    """
    issues: list[DRCIssue] = []
    tree = GroupTree(layout.groups)
    for gid, group in layout.groups.items():
        if gid == ROOT_GROUP_ID:
            continue

        # INVALID: exported pin breaks the encapsulated re-export chain.
        # Checked first because a grandchild pin floating in G.exported_pins
        # would otherwise satisfy MISSING for the net it lives on while
        # bypassing the intermediate group's interface.
        invalid = tree.validate_exported_pins(gid)
        for ep in invalid:
            ref = PinRef(component_id=ep.component_id, pin_number=ep.pin_number)
            group_label = group.name or f"<{str(gid)[:8]}>"
            msg = (
                f"Group {group_label!r} exports pin {_format_pin(ref, instances)} "
                f"but the pin lives on a component that is neither a direct "
                f"member of this group nor re-exported by a direct sub-group. "
                f"Re-export the pin at each intermediate group or remove the "
                f"export."
            )
            issues.append(
                DRCIssue(
                    severity="error",
                    code="GROUP_INVALID_EXPORT",
                    message=msg,
                    locations=[],
                    affected_nets=[],
                    affected_pins=[ref],
                    affected_components=[ref.component_id],
                )
            )

        member_ids = tree.all_component_ids(gid)
        if not member_ids:
            continue

        # Pre-compute which nets are external to *this* group.
        external_net_ids: set[uuid.UUID] = set()
        # For each external net: which of G's pins are on it?
        external_pins_by_net: dict[uuid.UUID, list[PinRef]] = {}
        for net in schematic.nets.values():
            member_pins = [p for p in net.pins if p.component_id in member_ids]
            if not member_pins:
                continue
            has_outside = any(p.component_id not in member_ids for p in net.pins)
            if has_outside or net.global_net:
                external_net_ids.add(net.id)
                external_pins_by_net[net.id] = member_pins

        exported_set: set[PinRef] = {
            PinRef(component_id=ep.component_id, pin_number=ep.pin_number)
            for ep in group.exported_pins
        }

        # MISSING: external net with zero exported coverage.
        for net_id, member_pins in external_pins_by_net.items():
            if not any(p in exported_set for p in member_pins):
                external_net = schematic.nets[net_id]
                group_label = group.name or f"<{str(gid)[:8]}>"
                msg = (
                    f"Group {group_label!r} connects to external net "
                    f"{external_net.name!r} via "
                    f"{_format_pin_list(member_pins, instances)} "
                    f"but exports no pin on that net. Add at least one pin to "
                    f"the group's exported interface."
                )
                issues.append(
                    DRCIssue(
                        severity="error",
                        code="GROUP_MISSING_EXPORT",
                        message=msg,
                        locations=[],
                        affected_nets=[net_id],
                        affected_pins=list(member_pins),
                        affected_components=[p.component_id for p in member_pins],
                    )
                )

        # BOGUS: exported pin whose net is not external.  Skip entries
        # already flagged as INVALID to avoid stacking warnings on errors.
        for ep in group.exported_pins:
            if ep in invalid:
                continue
            ref = PinRef(component_id=ep.component_id, pin_number=ep.pin_number)
            pin_net = schematic.net_for_pin(ref)
            if pin_net is None:
                continue  # orphan — user opted to ignore
            if pin_net.id in external_net_ids:
                continue
            group_label = group.name or f"<{str(gid)[:8]}>"
            msg = (
                f"Group {group_label!r} exports pin {_format_pin(ref, instances)} "
                f"but its net {pin_net.name!r} stays inside the group. Either "
                f"unexport the pin or connect the net to something outside "
                f"the group."
            )
            issues.append(
                DRCIssue(
                    severity="warning",
                    code="GROUP_BOGUS_EXPORT",
                    message=msg,
                    locations=[],
                    affected_nets=[pin_net.id],
                    affected_pins=[ref],
                    affected_components=[ref.component_id],
                )
            )
    return issues


def _check_wire_pin_on_plated_through(
    layout: Layout,
    board: Board,
    axis_labels: AxisLabelSettings | None,
    *,
    pad_index: PadIndex | None = None,
) -> list[DRCIssue]:
    """Wire-pin invariant (docs/archive/wire-pin-implementation.md §2.1): every
    :class:`WirePinKnot` must sit on a board pad whose ``plated_through``
    flag is set.

    A wire-pin's whole point is the through-hole terminal (rule 5 — pin
    pierces every physical layer).  Off-pad wire-pins or wire-pins on
    non-plated pads have no fabricable interpretation and must be
    surfaced as an error.  Typical sources:

    * the user converted an endpoint and then dragged it to a pad that
      doesn't have ``plated_through``,
    * board geometry was later edited (a pad was downgraded to
      single-sided) without removing the wire-pin first.
    """
    if not layout.knots:
        return []
    issues: list[DRCIssue] = []
    for knot in layout.knots.values():
        if not isinstance(knot, WirePinKnot):
            continue
        pos = knot.position
        loc_label = _format_location(pos, axis_labels)
        plated = False
        if pad_index is not None:
            pad = pad_index.pad_at(pos)
            plated = pad is not None and pad.layers.plated_through
        else:
            plated = point_on_pads(pos, set(plated_through_pads(board)))
        if plated:
            continue
        affected_wires = list(knot.wire_ids)
        issues.append(
            DRCIssue(
                severity="error",
                code="WIRE_PIN_OFF_PLATED_THROUGH",
                message=(
                    f"Wire-pin at {loc_label} is not on a plated-through "
                    f"pad. Move the wire endpoint to a plated-through pad "
                    f"or convert it back to a free endpoint."
                ),
                locations=[pos],
                affected_nets=[],
                affected_wires=affected_wires,
            )
        )
    return issues


def _check_half_orphan_wires(
    layout: Layout,
    axis_labels: AxisLabelSettings | None,
) -> list[DRCIssue]:
    """Rule 9 (docs/pad-pin-wire-junction-semantics.md §192): a wire
    endpoint that has no solder mate is a *half-orphan* — warn so the
    user decides whether to delete the wire, extend it, or promote the
    endpoint to a wire-pin.

    "Solder mate" means the endpoint knot is shared with another wire
    (t-junction) or it is a :class:`WirePinKnot` (deliberate terminal).
    A lone :class:`JointKnot` with one incident wire is half-orphan.

    PinKnot endpoints are by definition anchored to a real component
    pin — never half-orphan even if no other wire shares the pin.
    """
    issues: list[DRCIssue] = []
    for wire in layout.wires:
        if wire.a is None or wire.b is None:
            continue
        for side, ref in (("a", wire.a), ("b", wire.b)):
            knot = layout.knots.get(ref.id)
            if not isinstance(knot, JointKnot):
                continue
            if len(knot.wire_ids) != 1:
                # Shared with another wire — that's a t-junction, not orphan.
                continue
            assert wire.id in knot.wire_ids
            pos = knot.position
            loc_label = _format_location(pos, axis_labels)
            issues.append(
                DRCIssue(
                    severity="warning",
                    code="HALF_ORPHAN_WIRE",
                    message=(
                        f"Wire endpoint at {loc_label} has no solder mate. "
                        f"Delete the wire, extend it, or convert the "
                        f"endpoint to a wire-pin."
                    ),
                    locations=[pos],
                    affected_nets=[],
                    affected_wires=[wire.id],
                )
            )
    _ = WirePinKnot  # mark used — type narrows JointKnot guard above
    return issues
