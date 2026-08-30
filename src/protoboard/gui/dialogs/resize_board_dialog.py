# SPDX-License-Identifier: MIT
"""Dialog for resizing an existing board."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
)

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, VBox
from protoboard.domain.board.templates import (
    make_breadboard,
    make_perfboard,
    make_stripboard,
)

if TYPE_CHECKING:
    import uuid

    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.layout.layout import Layout


def _detect_board_type(board: Board) -> str:
    """Return 'Perfboard', 'Stripboard', or 'Breadboard'."""
    root = board.root
    if isinstance(root, BoardGrid):
        return "Perfboard"
    if isinstance(root, VBox):
        # Stripboard children are HBox rows; Breadboard has GapAtoms too.
        from protoboard.domain.board.boxes import GapAtom

        if any(isinstance(c, GapAtom) for c in root.children):
            return "Breadboard"
        return "Stripboard"
    return "Perfboard"


def _detect_dimensions(board: Board) -> tuple[int, int]:
    """Return (rows, cols) for the current board."""
    root = board.root
    if isinstance(root, BoardGrid):
        return root.rows, root.cols
    if isinstance(root, VBox):
        from protoboard.domain.board.boxes import HBox, StripAtom

        # Stripboard: count VBox children that are HBox strips.
        strip_rows = sum(1 for c in root.children if isinstance(c, HBox))
        # Find first StripAtom to get length.
        cols = 1
        for child in root.children:
            if isinstance(child, HBox):
                for atom in child.children:
                    if isinstance(atom, StripAtom):
                        cols = int(atom.length) if isinstance(atom.length, int) else 1
                        break
                break
        if strip_rows > 0:
            return strip_rows, cols
        # Breadboard — approximate from board bounds.
        b = board.bounds
        return int(round(b.ly)), int(round(b.lx))
    return 20, 30


def components_outside_bounds(
    new_board: Board,
    layout: Layout,
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
) -> list[tuple[str, float, float]]:
    """Return list of (refdes, x, y) for components outside new board bounds."""
    from protoboard.domain.layout.placement import pin_world_position

    bounds = new_board.bounds
    outside = []
    from protoboard.domain.components.package import resolved_footprint

    for comp_id, placement in layout.placements.items():
        inst = instances.get(comp_id)
        defn = components.get(inst.definition_ref) if inst else None
        if defn is None:
            continue
        fp = resolved_footprint(defn, inst, inst.families if inst else None)
        for pin_num in range(1, fp.pin_count() + 1):
            pt = pin_world_position(fp, placement, pin_num)
            if not (
                bounds.x <= pt.x <= bounds.x + bounds.lx
                and bounds.y <= pt.y <= bounds.y + bounds.ly
            ):
                refdes = inst.refdes if inst else str(comp_id)[:8]
                outside.append((refdes, pt.x, pt.y))
                break  # one violation per component is enough
    return outside


class ResizeBoardDialog(QDialog):
    """Modal dialog for choosing new board dimensions."""

    def __init__(
        self,
        board: Board,
        layout: Layout,
        instances: dict[uuid.UUID, ComponentInstance],
        components: dict[str, ComponentDefinition],
        parent: object = None,
    ) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        self.setWindowTitle("Resize Board")
        self._layout_ref = layout
        self._instances = instances
        self._components = components

        self._board_type = _detect_board_type(board)
        rows, cols = _detect_dimensions(board)
        self._is_breadboard = self._board_type == "Breadboard"

        root = QVBoxLayout(self)
        root.setSpacing(8)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        type_label = QLabel(f"<b>{self._board_type}</b>")
        form.addRow("Board type:", type_label)

        if not self._is_breadboard:
            self._rows_spin = QSpinBox()
            self._rows_spin.setRange(2, 200)
            self._rows_spin.setValue(rows)
            form.addRow("Rows:", self._rows_spin)
        else:
            self._rows_spin = None  # type: ignore[assignment]

        self._cols_spin = QSpinBox()
        self._cols_spin.setRange(2, 200)
        self._cols_spin.setValue(cols)
        form.addRow("Columns:", self._cols_spin)

        root.addLayout(form)

        self._warning_label = QLabel()
        self._warning_label.setWordWrap(True)
        self._warning_label.setStyleSheet("color: #cc4400;")
        self._warning_label.hide()
        root.addWidget(self._warning_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        # Connect spinboxes to live validation.
        self._cols_spin.valueChanged.connect(self._validate)
        if self._rows_spin is not None:
            self._rows_spin.valueChanged.connect(self._validate)

        self._validate()

    def _validate(self) -> None:
        new_board = self.new_board()
        outside = components_outside_bounds(
            new_board, self._layout_ref, self._instances, self._components
        )
        if outside:
            names = ", ".join(r for r, _, _ in outside[:5])
            extra = f" and {len(outside) - 5} more" if len(outside) > 5 else ""
            self._warning_label.setText(
                f"⚠ {len(outside)} component(s) outside new bounds: {names}{extra}.\n"
                "Move them first or choose a larger size."
            )
            self._warning_label.show()
            self._ok_btn.setEnabled(False)
        else:
            self._warning_label.hide()
            self._ok_btn.setEnabled(True)

    def new_board(self) -> Board:
        cols = self._cols_spin.value()
        if self._is_breadboard:
            return make_breadboard(cols)
        # Not a breadboard, so _rows_spin was created (see __init__).
        assert self._rows_spin is not None
        rows = self._rows_spin.value()
        if self._board_type == "Stripboard":
            return make_stripboard(rows, cols)
        return make_perfboard(rows, cols)
