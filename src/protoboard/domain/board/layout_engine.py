# SPDX-License-Identifier: MIT
"""Two-pass layout engine for the box model.

Pass 1 (intrinsic): compute each box's natural size — atoms report their
inherent dimensions, containers aggregate their children. FIXED boxes
report the declared size. FILL boxes report 0 in the FILL dimension.

Pass 2 (layout): walk the tree top-down assigning absolute positions and
final sizes. FILL space is distributed by subtracting non-FILL siblings'
sizes and gaps from the parent's available space and giving the remainder
to the single FILL child (multiple FILL siblings on the same axis are
forbidden).

Public API: `layout(box, available=None) -> LayoutNode` and
`intrinsic_size(box) -> Size`. Errors raise `LayoutError`.

A `LayoutNode` carries the *center* of the box's bounding rectangle and its
resolved `Size`, in raster units; `bounds` derives the top-left `Rect`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from protoboard.domain.board.boxes import (
    BoardGrid,
    Box,
    GapAtom,
    HBox,
    PadAtom,
    SizePolicy,
    StripAtom,
    VBox,
)
from protoboard.errors import LayoutError
from protoboard.geometry import Point, Rect, Size


@dataclass(frozen=True, slots=True)
class LayoutNode:
    """One node of the resolved layout tree.

    ``center`` is the middle of the node's bounding box in world (root)
    coordinates — named so the centre-vs-corner convention is impossible
    to misread; ``bounds`` gives the top-left-origin rectangle.
    """

    box: Box
    center: Point
    size: Size
    children: list[LayoutNode] = field(default_factory=list)

    @property
    def bounds(self) -> Rect:
        return Rect.from_center(self.center, self.size)


# ---------------------------------------------------------------------------
# Pass 1 — intrinsic size
# ---------------------------------------------------------------------------


def intrinsic_size(box: Box) -> Size:
    """Return the natural size of a box (without margins).

    For FIXED axes the declared dimension is used; for HUG axes the size
    is derived from contents; for FILL axes the size is 0 (real value
    assigned later in the layout pass).
    """
    return _intrinsic(box)


def _intrinsic(box: Box) -> Size:
    iw, ih = _intrinsic_content(box)
    w = _resolve_axis_intrinsic(box.width_policy, box.width, iw)
    h = _resolve_axis_intrinsic(box.height_policy, box.height, ih)
    return Size(w, h)


def _resolve_axis_intrinsic(
    policy: SizePolicy, declared: float | None, content: float
) -> float:
    if policy is SizePolicy.FIXED:
        assert declared is not None  # guaranteed by Box validation
        return declared
    if policy is SizePolicy.HUG:
        return content
    # FILL: intrinsic is 0 — real size is decided by the parent's layout pass.
    return 0.0


def _intrinsic_content(box: Box) -> tuple[float, float]:
    """Content (pre-margin, pre-policy) intrinsic size for the given box."""
    if isinstance(box, PadAtom):
        return 1.0, 1.0
    if isinstance(box, StripAtom):
        if box.length == "fill":
            length = 0.0
        else:
            length = float(box.length)
        if box.orientation == "horizontal":
            return length, 1.0
        return 1.0, length
    if isinstance(box, GapAtom):
        return 0.0, 0.0
    if isinstance(box, HBox):
        return _hbox_intrinsic_content(box)
    if isinstance(box, VBox):
        return _vbox_intrinsic_content(box)
    if isinstance(box, BoardGrid):
        return _grid_intrinsic_content(box)
    raise LayoutError(f"Unknown box type: {type(box).__name__}")


def _child_intrinsic_with_margin(child: Box) -> Size:
    """Child intrinsic size including its margins (the parent reasons in
    these terms when summing children)."""
    inner = _intrinsic(child)
    return Size(
        inner.lx + child.margin.horizontal,
        inner.ly + child.margin.vertical,
    )


def _hbox_intrinsic_content(box: HBox) -> tuple[float, float]:
    if not box.children:
        return 0.0, 0.0
    sizes = [_child_intrinsic_with_margin(c) for c in box.children]
    width = sum(s.lx for s in sizes) + box.gap * (len(box.children) - 1)
    height = max((s.ly for s in sizes), default=0.0)
    return width, height


def _vbox_intrinsic_content(box: VBox) -> tuple[float, float]:
    if not box.children:
        return 0.0, 0.0
    sizes = [_child_intrinsic_with_margin(c) for c in box.children]
    width = max((s.lx for s in sizes), default=0.0)
    height = sum(s.ly for s in sizes) + box.gap * (len(box.children) - 1)
    return width, height


def _grid_intrinsic_content(box: BoardGrid) -> tuple[float, float]:
    assert box.cell is not None
    cell = _child_intrinsic_with_margin(box.cell)
    width = cell.lx * box.cols + box.col_gap * (box.cols - 1)
    height = cell.ly * box.rows + box.row_gap * (box.rows - 1)
    return width, height


# ---------------------------------------------------------------------------
# Pass 2 — layout
# ---------------------------------------------------------------------------


def layout(box: Box, available: Size | None = None) -> LayoutNode:
    """Lay out the box tree and return the resolved layout.

    The root must have HUG or FIXED policy; FILL on the root is invalid
    because there is no outer context to fill.
    """
    if box.width_policy is SizePolicy.FILL or box.height_policy is SizePolicy.FILL:
        raise LayoutError("FILL is not allowed on the root box")
    resolved = _resolve_size(box, available)
    return _place(box, origin_x=0.0, origin_y=0.0, resolved=resolved)


def _resolve_size(box: Box, available: Size | None) -> Size:
    """Resolve the box's actual size given its policy and available space."""
    del available  # the root cannot be FILL, so available is unused for now
    intrinsic = _intrinsic(box)
    width = _resolve_axis_actual(box.width_policy, box.width, intrinsic.lx)
    height = _resolve_axis_actual(box.height_policy, box.height, intrinsic.ly)
    return Size(width, height)


def _resolve_axis_actual(
    policy: SizePolicy,
    declared: float | None,
    intrinsic: float,
) -> float:
    if policy is SizePolicy.FIXED:
        assert declared is not None
        return declared
    if policy is SizePolicy.HUG:
        return intrinsic
    raise LayoutError("FILL axis requires a parent to provide a size")


def _place(box: Box, origin_x: float, origin_y: float, resolved: Size) -> LayoutNode:
    """Produce a LayoutNode for `box` with its top-left at (origin_x, origin_y)
    and the given resolved outer size."""
    if isinstance(box, (PadAtom, StripAtom, GapAtom)):
        return _atom_node(box, origin_x, origin_y, resolved)
    if isinstance(box, HBox):
        return _layout_hbox(box, origin_x, origin_y, resolved)
    if isinstance(box, VBox):
        return _layout_vbox(box, origin_x, origin_y, resolved)
    if isinstance(box, BoardGrid):
        return _layout_grid(box, origin_x, origin_y, resolved)
    raise LayoutError(f"Unknown box type: {type(box).__name__}")


def _atom_node(
    box: Box, origin_x: float, origin_y: float, resolved: Size
) -> LayoutNode:
    cx = origin_x + resolved.lx / 2.0
    cy = origin_y + resolved.ly / 2.0
    return LayoutNode(box=box, center=Point(cx, cy), size=resolved,
                      children=[])


# ---------------------------------------------------------------------------
# Container layout helpers (HBox / VBox / BoardGrid)
# ---------------------------------------------------------------------------


def _bounded_outer_size(box: Box, resolved: Size, axis: str) -> float | None:
    """Return the container's outer size on the given axis if it is bounded
    (FIXED or already-resolved FILL), otherwise None (HUG)."""
    if axis == "width":
        if box.width_policy is SizePolicy.FIXED:
            assert box.width is not None
            return box.width
        if box.width_policy is SizePolicy.FILL:
            return resolved.lx
        return None
    if box.height_policy is SizePolicy.FIXED:
        assert box.height is not None
        return box.height
    if box.height_policy is SizePolicy.FILL:
        return resolved.ly
    return None


def _validate_single_fill(children: list[Box], axis: str) -> None:
    """Raise if more than one child has FILL along the given axis."""
    attr = "width_policy" if axis == "width" else "height_policy"
    fills = [c for c in children if getattr(c, attr) is SizePolicy.FILL]
    if len(fills) > 1:
        raise LayoutError(
            f"multiple FILL siblings in {axis} dimension; only one is allowed"
        )


def _resolve_container_inner_size(
    box: Box, content_w: float, content_h: float, resolved: Size
) -> tuple[float, float]:
    """Return the container's outer size given:

    - FIXED: declared size (validated against content),
    - HUG: derived from content,
    - FILL: the size assigned by the parent (in `resolved`).
    """
    if box.width_policy is SizePolicy.FIXED:
        assert box.width is not None
        if box.width + 1e-9 < content_w:
            raise LayoutError(
                f"FIXED width={box.width} too small for content (needs {content_w})"
            )
        outer_w = box.width
    elif box.width_policy is SizePolicy.FILL:
        if resolved.lx + 1e-9 < content_w:
            raise LayoutError(
                f"FILL width={resolved.lx} too small for content "
                f"(needs {content_w})"
            )
        outer_w = resolved.lx
    else:
        outer_w = content_w
    if box.height_policy is SizePolicy.FIXED:
        assert box.height is not None
        if box.height + 1e-9 < content_h:
            raise LayoutError(
                f"FIXED height={box.height} too small for content (needs {content_h})"
            )
        outer_h = box.height
    elif box.height_policy is SizePolicy.FILL:
        if resolved.ly + 1e-9 < content_h:
            raise LayoutError(
                f"FILL height={resolved.ly} too small for content "
                f"(needs {content_h})"
            )
        outer_h = resolved.ly
    else:
        outer_h = content_h
    return outer_w, outer_h


def _layout_hbox(
    box: HBox, origin_x: float, origin_y: float, resolved: Size
) -> LayoutNode:
    children = box.children
    _validate_single_fill(children, "width")

    # Per-child intrinsic (pre-margin) sizes.
    intrinsics = [_intrinsic(c) for c in children]
    margins_w = [c.margin.horizontal for c in children]
    margins_h = [c.margin.vertical for c in children]

    # Compute total width of HUG/FIXED children (incl. margins) along main axis.
    non_fill_widths: list[float] = []
    fill_index: int | None = None
    for i, child in enumerate(children):
        if child.width_policy is SizePolicy.FILL:
            fill_index = i
            non_fill_widths.append(0.0)  # placeholder
        else:
            non_fill_widths.append(intrinsics[i].lx + margins_w[i])
    n = len(children)
    gaps_total = box.gap * max(0, n - 1)

    # FILL handling along x. The available outer width is either box.width
    # (FIXED) or resolved.width (when this HBox is itself FILL/FIXED via parent).
    fill_width = 0.0
    fill_inner_width = 0.0
    if fill_index is not None:
        bounded_w = _bounded_outer_size(box, resolved, axis="width")
        if bounded_w is None:
            raise LayoutError(
                "FILL child requires a bounded (FIXED or FILL-resolved) parent "
                "in that dimension"
            )
        used = sum(non_fill_widths) + gaps_total
        fill_width = bounded_w - used
        if fill_width < -1e-9:
            raise LayoutError(
                f"parent width={bounded_w} too small for content "
                f"(used {used} before FILL)"
            )
        fill_width = max(0.0, fill_width)
        fill_inner_width = fill_width - margins_w[fill_index]

    # Compute per-child outer widths (incl. margins).
    outer_widths: list[float] = []
    inner_widths: list[float] = []
    for i, child in enumerate(children):
        if child.width_policy is SizePolicy.FILL:
            inner_widths.append(max(0.0, fill_inner_width))
            outer_widths.append(fill_width)
        else:
            inner_widths.append(intrinsics[i].lx)
            outer_widths.append(intrinsics[i].lx + margins_w[i])

    # Heights: FIXED parent uses declared height; otherwise use content height
    # (max of children outer heights). Also handle FILL height for children.
    content_h = max(
        (intrinsics[i].ly + margins_h[i] for i in range(n)),
        default=0.0,
    )
    content_w = sum(outer_widths) + gaps_total
    outer_w, outer_h = _resolve_container_inner_size(
        box, content_w, content_h, resolved
    )

    # Per-child height resolution.
    inner_heights: list[float] = []
    for i, child in enumerate(children):
        if child.height_policy is SizePolicy.FILL:
            inner_heights.append(max(0.0, outer_h - margins_h[i]))
        elif child.height_policy is SizePolicy.FIXED:
            assert child.height is not None
            inner_heights.append(child.height)
        else:
            inner_heights.append(intrinsics[i].ly)

    # Place children left-to-right.
    child_nodes: list[LayoutNode] = []
    cursor = origin_x
    for i, child in enumerate(children):
        # x position: cursor + left-margin (top-left of inner box).
        child_x = cursor + child.margin.left
        child_y = origin_y + child.margin.top
        node = _place(
            child,
            origin_x=child_x,
            origin_y=child_y,
            resolved=Size(inner_widths[i], inner_heights[i]),
        )
        child_nodes.append(node)
        cursor += outer_widths[i]
        if i < n - 1:
            cursor += box.gap

    return LayoutNode(
        box=box,
        center=Point(origin_x + outer_w / 2.0, origin_y + outer_h / 2.0),
        size=Size(outer_w, outer_h),
        children=child_nodes,
    )


def _layout_vbox(
    box: VBox, origin_x: float, origin_y: float, resolved: Size
) -> LayoutNode:
    children = box.children
    _validate_single_fill(children, "height")

    intrinsics = [_intrinsic(c) for c in children]
    margins_w = [c.margin.horizontal for c in children]
    margins_h = [c.margin.vertical for c in children]

    non_fill_heights: list[float] = []
    fill_index: int | None = None
    for i, child in enumerate(children):
        if child.height_policy is SizePolicy.FILL:
            fill_index = i
            non_fill_heights.append(0.0)
        else:
            non_fill_heights.append(intrinsics[i].ly + margins_h[i])
    n = len(children)
    gaps_total = box.gap * max(0, n - 1)

    fill_height = 0.0
    fill_inner_height = 0.0
    if fill_index is not None:
        bounded_h = _bounded_outer_size(box, resolved, axis="height")
        if bounded_h is None:
            raise LayoutError(
                "FILL child requires a bounded (FIXED or FILL-resolved) parent "
                "in that dimension"
            )
        used = sum(non_fill_heights) + gaps_total
        fill_height = bounded_h - used
        if fill_height < -1e-9:
            raise LayoutError(
                f"parent height={bounded_h} too small for content "
                f"(used {used} before FILL)"
            )
        fill_height = max(0.0, fill_height)
        fill_inner_height = fill_height - margins_h[fill_index]

    outer_heights: list[float] = []
    inner_heights: list[float] = []
    for i, child in enumerate(children):
        if child.height_policy is SizePolicy.FILL:
            inner_heights.append(max(0.0, fill_inner_height))
            outer_heights.append(fill_height)
        else:
            inner_heights.append(intrinsics[i].ly)
            outer_heights.append(intrinsics[i].ly + margins_h[i])

    content_w = max((intrinsics[i].lx + margins_w[i] for i in range(n)), default=0.0)
    content_h = sum(outer_heights) + gaps_total
    outer_w, outer_h = _resolve_container_inner_size(
        box, content_w, content_h, resolved
    )

    inner_widths: list[float] = []
    for i, child in enumerate(children):
        if child.width_policy is SizePolicy.FILL:
            inner_widths.append(max(0.0, outer_w - margins_w[i]))
        elif child.width_policy is SizePolicy.FIXED:
            assert child.width is not None
            inner_widths.append(child.width)
        else:
            inner_widths.append(intrinsics[i].lx)

    child_nodes: list[LayoutNode] = []
    cursor = origin_y
    for i, child in enumerate(children):
        child_x = origin_x + child.margin.left
        child_y = cursor + child.margin.top
        node = _place(
            child,
            origin_x=child_x,
            origin_y=child_y,
            resolved=Size(inner_widths[i], inner_heights[i]),
        )
        child_nodes.append(node)
        cursor += outer_heights[i]
        if i < n - 1:
            cursor += box.gap

    return LayoutNode(
        box=box,
        center=Point(origin_x + outer_w / 2.0, origin_y + outer_h / 2.0),
        size=Size(outer_w, outer_h),
        children=child_nodes,
    )


def _layout_grid(
    box: BoardGrid, origin_x: float, origin_y: float, resolved: Size
) -> LayoutNode:
    assert box.cell is not None
    cell = box.cell
    cell_intrinsic = _intrinsic(cell)
    margin_w = cell.margin.horizontal
    margin_h = cell.margin.vertical

    # Resolve cell width.
    if cell.width_policy is SizePolicy.FILL:
        bounded_w = _bounded_outer_size(box, resolved, axis="width")
        if bounded_w is None:
            raise LayoutError(
                "BoardGrid cell with FILL width requires a bounded grid width"
            )
        usable = bounded_w - box.col_gap * (box.cols - 1)
        cell_outer_w = usable / box.cols
        cell_inner_w = cell_outer_w - margin_w
    elif cell.width_policy is SizePolicy.FIXED:
        assert cell.width is not None
        cell_inner_w = cell.width
        cell_outer_w = cell_inner_w + margin_w
    else:
        cell_inner_w = cell_intrinsic.lx
        cell_outer_w = cell_inner_w + margin_w

    # Resolve cell height.
    if cell.height_policy is SizePolicy.FILL:
        bounded_h = _bounded_outer_size(box, resolved, axis="height")
        if bounded_h is None:
            raise LayoutError(
                "BoardGrid cell with FILL height requires a bounded grid height"
            )
        usable_h = bounded_h - box.row_gap * (box.rows - 1)
        cell_outer_h = usable_h / box.rows
        cell_inner_h = cell_outer_h - margin_h
    elif cell.height_policy is SizePolicy.FIXED:
        assert cell.height is not None
        cell_inner_h = cell.height
        cell_outer_h = cell_inner_h + margin_h
    else:
        cell_inner_h = cell_intrinsic.ly
        cell_outer_h = cell_inner_h + margin_h

    content_w = cell_outer_w * box.cols + box.col_gap * (box.cols - 1)
    content_h = cell_outer_h * box.rows + box.row_gap * (box.rows - 1)
    outer_w, outer_h = _resolve_container_inner_size(
        box, content_w, content_h, resolved
    )

    child_nodes: list[LayoutNode] = []
    for r in range(box.rows):
        for c in range(box.cols):
            cell_origin_x = (
                origin_x + c * (cell_outer_w + box.col_gap) + cell.margin.left
            )
            cell_origin_y = (
                origin_y + r * (cell_outer_h + box.row_gap) + cell.margin.top
            )
            node = _place(
                cell,
                origin_x=cell_origin_x,
                origin_y=cell_origin_y,
                resolved=Size(max(0.0, cell_inner_w), max(0.0, cell_inner_h)),
            )
            child_nodes.append(node)

    return LayoutNode(
        box=box,
        center=Point(origin_x + outer_w / 2.0, origin_y + outer_h / 2.0),
        size=Size(outer_w, outer_h),
        children=child_nodes,
    )


__all__ = ["LayoutError", "LayoutNode", "intrinsic_size", "layout"]
