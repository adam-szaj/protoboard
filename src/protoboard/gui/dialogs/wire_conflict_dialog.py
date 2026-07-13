# SPDX-License-Identifier: MIT
"""WireConflictDialog — lets the user choose what to do with wires that
collide with a component being placed or moved.

LEGACY in the drag-and-drop flow.  As of the drag-UX overhaul, component
drag-release uses the floating-overlay pipeline driven by
``classify_drag_commit`` in ``protoboard.controller.editing.drag_commit`` — wire
conflicts surface as the SOFT (yellow) overlay and commit silently
auto-removes them (Ctrl+Z to undo).  This dialog still backs the library
``PlaceTool`` (initial component placement) where a three-way decision is
useful before the part lands on the board.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QPushButton,
    QHBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt

# The decision enum lives with the (Qt-free) resolution policy in
# placement_conflict.py; re-exported here so dialog-side imports keep
# working (qt-ports D4).
from protoboard.gui.placement_conflict import WireConflictAction

if TYPE_CHECKING:
    from protoboard.domain.layout.pad_conflict import ConflictKind, PadConflict

__all__ = ["WireConflictAction", "WireConflictDialog"]


class WireConflictDialog(QDialog):
    """Single dialog summarising all wire collisions for a component move.

    The set of available actions depends on the conflict types present:

    * Any ``PIN_ON_WIRE_DIFFERENT_NET``  → only Cancel / Remove & reroute.
    * Only ``PIN_ON_WIRE_WITH_NET``      → Cancel / Add to net / Remove & reroute.
    * Only ``PIN_ON_WIRE_UNCONNECTED``   → Cancel / Create net / Remove & reroute.
    * Mixed WITH_NET + UNCONNECTED       → Cancel / Remove & reroute.
    """

    def __init__(
        self,
        conflicts: "list[PadConflict]",
        incoming_refdes: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Wire Conflict")
        self.setMinimumWidth(460)
        self.setModal(True)

        self._action = WireConflictAction.CANCEL

        from protoboard.domain.layout.pad_conflict import ConflictKind

        kinds = {c.kind for c in conflicts}
        has_different_net = ConflictKind.PIN_ON_WIRE_DIFFERENT_NET in kinds
        has_with_net = ConflictKind.PIN_ON_WIRE_WITH_NET in kinds
        has_unconnected = ConflictKind.PIN_ON_WIRE_UNCONNECTED in kinds

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        layout.addWidget(QLabel(
            f"Moving <b>{incoming_refdes}</b> would place pin(s) on existing wires:"
        ))

        tree = QTreeWidget()
        tree.setHeaderLabels(["Pin", "Wire net", "Conflict"])
        tree.setColumnWidth(0, 60)
        tree.setColumnWidth(1, 140)
        tree.setAlternatingRowColors(True)
        for c in conflicts:
            net_name = c.existing_net_name or "(unconnected)"
            kind_label = c.kind.name.replace("PIN_ON_WIRE_", "").replace("_", " ").title()
            item = QTreeWidgetItem([f"Pin {c.incoming_pin}", net_name, kind_label])
            tree.addTopLevelItem(item)
        layout.addWidget(tree)

        # Build action buttons.
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        can_add_to_net = has_with_net and not has_different_net and not has_unconnected
        can_create_net = has_unconnected and not has_different_net and not has_with_net

        if can_add_to_net:
            add_btn = QPushButton("Add pin to net")
            add_btn.setDefault(True)
            add_btn.clicked.connect(lambda: self._accept(WireConflictAction.ADD_TO_NET))
            btn_row.addWidget(add_btn)
        elif can_create_net:
            create_btn = QPushButton("Create net")
            create_btn.setDefault(True)
            create_btn.clicked.connect(lambda: self._accept(WireConflictAction.CREATE_NET))
            btn_row.addWidget(create_btn)

        remove_btn = QPushButton("Remove wire(s) && reroute")
        if not can_add_to_net and not can_create_net:
            remove_btn.setDefault(True)
        remove_btn.clicked.connect(lambda: self._accept(WireConflictAction.REMOVE_AND_REROUTE))
        btn_row.addWidget(remove_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        layout.addLayout(btn_row)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def chosen_action(self) -> WireConflictAction:
        return self._action

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _accept(self, action: WireConflictAction) -> None:
        self._action = action
        self.accept()
