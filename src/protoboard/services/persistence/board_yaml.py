# SPDX-License-Identifier: MIT
"""YAML serializer/deserializer for Board (box trees).

The mapping is one YAML object per box. Each object has a `type` discriminator
matching the class name, the standard size-policy/dimension fields, and any
type-specific fields (e.g. `children`, `cell`, `length`, `layers`).
"""

from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from protoboard.domain.board.board import Board
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
from protoboard.domain.board.pads import PadLayers
from protoboard.errors import SerializationError
from protoboard.geometry import Insets

_yaml = YAML(typ="safe")
_yaml.default_flow_style = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_board(path: Path) -> Board:
    text = path.read_text()
    return board_from_yaml(text)


def board_from_yaml(text: str) -> Board:
    try:
        data = _yaml.load(text)
    except Exception as exc:
        raise SerializationError(f"YAML parse error: {exc}") from exc
    if not isinstance(data, dict):
        raise SerializationError("Top-level YAML must be a mapping")
    root = _box_from_dict(data)
    return Board(root=root)


def board_to_yaml(board: Board) -> str:
    data = _box_to_dict(board.root)
    buf = io.StringIO()
    _yaml.dump(data, buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Deserialization
# ---------------------------------------------------------------------------


def _policy_from_str(value: Any, field_name: str) -> SizePolicy:
    if not isinstance(value, str):
        raise SerializationError(
            f"{field_name} must be a string ('fixed', 'hug', 'fill')"
        )
    try:
        return SizePolicy(value)
    except ValueError as exc:
        raise SerializationError(f"invalid {field_name}: {value!r}") from exc


def _insets_from_dict(value: Any) -> Insets:
    if value is None:
        return Insets.zero()
    if not isinstance(value, dict):
        raise SerializationError("margin must be a mapping or omitted")
    return Insets(
        top=float(value.get("top", 0.0)),
        right=float(value.get("right", 0.0)),
        bottom=float(value.get("bottom", 0.0)),
        left=float(value.get("left", 0.0)),
    )


def _layers_from_dict(value: Any) -> PadLayers:
    if not isinstance(value, dict):
        raise SerializationError("layers must be a mapping")
    return PadLayers(
        top=bool(value.get("top", True)),
        bottom=bool(value.get("bottom", False)),
        plated_through=bool(value.get("plated_through", False)),
    )


def _common_box_kwargs(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "width_policy": _policy_from_str(
            data.get("width_policy", "hug"), "width_policy"
        ),
        "height_policy": _policy_from_str(
            data.get("height_policy", "hug"), "height_policy"
        ),
        "width": data.get("width"),
        "height": data.get("height"),
        "margin": _insets_from_dict(data.get("margin")),
    }


def _box_from_dict(data: dict[str, Any]) -> Box:
    kind = data.get("type")
    if not isinstance(kind, str):
        raise SerializationError("Each box requires a string 'type' field")
    deserializer = _DESERIALIZERS.get(kind)
    if deserializer is None:
        raise SerializationError(f"Unknown box type: {kind}")
    try:
        result: Box = deserializer(data)
    except SerializationError:
        raise
    except (TypeError, ValueError) as exc:
        raise SerializationError(f"Invalid {kind}: {exc}") from exc
    return result


def _pad_atom_from_dict(data: dict[str, Any]) -> PadAtom:
    return PadAtom(
        **_common_box_kwargs(data),
        layers=_layers_from_dict(data.get("layers", {})),
        label=data.get("label"),
    )


def _strip_atom_from_dict(data: dict[str, Any]) -> StripAtom:
    if "length" not in data:
        raise SerializationError("StripAtom requires 'length'")
    if "orientation" not in data:
        raise SerializationError("StripAtom requires 'orientation'")
    return StripAtom(
        **_common_box_kwargs(data),
        length=data["length"],
        orientation=data["orientation"],
        layers=_layers_from_dict(data.get("layers", {})),
        cuttable=bool(data.get("cuttable", True)),
        label=data.get("label"),
    )


def _gap_atom_from_dict(data: dict[str, Any]) -> GapAtom:
    return GapAtom(**_common_box_kwargs(data))


def _hbox_from_dict(data: dict[str, Any]) -> HBox:
    children_raw = data.get("children", [])
    if not isinstance(children_raw, list):
        raise SerializationError("HBox 'children' must be a list")
    children = [_box_from_dict(c) for c in children_raw]
    return HBox(
        **_common_box_kwargs(data),
        children=children,
        gap=float(data.get("gap", 1.0)),
    )


def _vbox_from_dict(data: dict[str, Any]) -> VBox:
    children_raw = data.get("children", [])
    if not isinstance(children_raw, list):
        raise SerializationError("VBox 'children' must be a list")
    children = [_box_from_dict(c) for c in children_raw]
    return VBox(
        **_common_box_kwargs(data),
        children=children,
        gap=float(data.get("gap", 1.0)),
    )


def _board_grid_from_dict(data: dict[str, Any]) -> BoardGrid:
    if "cell" not in data or not isinstance(data["cell"], dict):
        raise SerializationError("BoardGrid requires a 'cell' mapping")
    if "rows" not in data or "cols" not in data:
        raise SerializationError("BoardGrid requires 'rows' and 'cols'")
    return BoardGrid(
        **_common_box_kwargs(data),
        cell=_box_from_dict(data["cell"]),
        rows=int(data["rows"]),
        cols=int(data["cols"]),
        row_gap=float(data.get("row_gap", 1.0)),
        col_gap=float(data.get("col_gap", 1.0)),
    )


_DESERIALIZERS: dict[str, Callable[[dict[str, Any]], Box]] = {
    "PadAtom": _pad_atom_from_dict,
    "StripAtom": _strip_atom_from_dict,
    "GapAtom": _gap_atom_from_dict,
    "HBox": _hbox_from_dict,
    "VBox": _vbox_from_dict,
    "BoardGrid": _board_grid_from_dict,
}


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _insets_to_dict(insets: Insets) -> dict[str, float] | None:
    if insets == Insets.zero():
        return None
    return {
        "top": insets.top,
        "right": insets.right,
        "bottom": insets.bottom,
        "left": insets.left,
    }


def _layers_to_dict(layers: PadLayers) -> dict[str, bool]:
    return {
        "top": layers.top,
        "bottom": layers.bottom,
        "plated_through": layers.plated_through,
    }


def _common_box_dict(box: Box) -> dict[str, Any]:
    out: dict[str, Any] = {
        "width_policy": box.width_policy.value,
        "height_policy": box.height_policy.value,
    }
    if box.width is not None:
        out["width"] = box.width
    if box.height is not None:
        out["height"] = box.height
    margin = _insets_to_dict(box.margin)
    if margin is not None:
        out["margin"] = margin
    return out


def _box_to_dict(box: Box) -> dict[str, Any]:
    if isinstance(box, PadAtom):
        out = {"type": "PadAtom", **_common_box_dict(box)}
        out["layers"] = _layers_to_dict(box.layers)
        if box.label is not None:
            out["label"] = box.label
        return out
    if isinstance(box, StripAtom):
        out = {
            "type": "StripAtom",
            **_common_box_dict(box),
            "length": box.length,
            "orientation": box.orientation,
            "layers": _layers_to_dict(box.layers),
            "cuttable": box.cuttable,
        }
        if box.label is not None:
            out["label"] = box.label
        return out
    if isinstance(box, GapAtom):
        return {"type": "GapAtom", **_common_box_dict(box)}
    if isinstance(box, HBox):
        return {
            "type": "HBox",
            **_common_box_dict(box),
            "gap": box.gap,
            "children": [_box_to_dict(c) for c in box.children],
        }
    if isinstance(box, VBox):
        return {
            "type": "VBox",
            **_common_box_dict(box),
            "gap": box.gap,
            "children": [_box_to_dict(c) for c in box.children],
        }
    if isinstance(box, BoardGrid):
        assert box.cell is not None
        return {
            "type": "BoardGrid",
            **_common_box_dict(box),
            "rows": box.rows,
            "cols": box.cols,
            "row_gap": box.row_gap,
            "col_gap": box.col_gap,
            "cell": _box_to_dict(box.cell),
        }
    raise SerializationError(f"Cannot serialize box type: {type(box).__name__}")


__all__ = ["board_from_yaml", "board_to_yaml", "load_board"]
