# SPDX-License-Identifier: MIT
"""DragDisconnectDialog — user decision for cross-group disconnect wires.

Shown at drag-commit when a drag set would tear apart one or more wires
connecting the drag set to the rest of the board (one PinKnot moves, the
other anchored to a stationary component).

Three actions:

* **Auto-reroute** — drop the disconnected wires and dispatch an autoroute
  pass on every affected net immediately after commit.  Default action
  (Enter key); covers the common case where the user wants the topology
  preserved.
* **Drop** — remove the wires without rerouting.  Leaves the net split;
  DRC will report disconnected pins (informational, not a SHORT).
* **Cancel** — refuse the commit, return to floating state.  Esc key.

Plus a session-level opt-out checkbox: when ticked, the chosen action
becomes the default for subsequent cross-group disconnects in this run
of the app — SelectTool caches it in :class:`DragSessionOverrides`.

Configurable via ``Preferences → Drag → Cross-group disconnect``:
``ask-reroute`` (default) opens this dialog; ``auto-reroute`` / ``auto-remove``
short-circuit it; ``block`` re-classifies the drag as HARD instead.
"""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


__all__ = ["DragDisconnectAction", "DragDisconnectDialog"]


class DragDisconnectAction(Enum):
    """User's chosen action for cross-group disconnect wires."""

    REROUTE = "reroute"
    DROP = "drop"
    CANCEL = "cancel"


class DragDisconnectDialog(QDialog):
    """Modal dialog: Reroute / Drop / Cancel + 'Don't ask again' checkbox."""

    def __init__(
        self,
        n_wires: int,
        affected_nets: list[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Drag would disconnect wires")
        self.setMinimumWidth(420)
        self.setModal(True)
        self._action: DragDisconnectAction = DragDisconnectAction.CANCEL
        self._opt_out: bool = False

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        plural = "wire" if n_wires == 1 else "wires"
        layout.addWidget(QLabel(
            f"Committing this drag will disconnect <b>{n_wires}</b> "
            f"{plural} from the rest of the board.",
        ))

        # First 5 affected net names, then "..." if there are more.
        if affected_nets:
            preview = ", ".join(affected_nets[:5])
            if len(affected_nets) > 5:
                preview += f", … (+{len(affected_nets) - 5})"
            nets_label = QLabel(f"Affected nets: <i>{preview}</i>")
            nets_label.setWordWrap(True)
            layout.addWidget(nets_label)

        layout.addWidget(QLabel(
            "How should I handle them?",
        ))

        self._opt_out_checkbox = QCheckBox("Don't ask again for this session")
        layout.addWidget(self._opt_out_checkbox)

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        reroute_btn = QPushButton("Auto-reroute")
        reroute_btn.setDefault(True)
        reroute_btn.setShortcut(Qt.Key.Key_Return)
        reroute_btn.clicked.connect(
            lambda: self._accept(DragDisconnectAction.REROUTE),
        )
        btn_row.addWidget(reroute_btn)

        drop_btn = QPushButton("Drop")
        drop_btn.clicked.connect(
            lambda: self._accept(DragDisconnectAction.DROP),
        )
        btn_row.addWidget(drop_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setShortcut(Qt.Key.Key_Escape)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        layout.addLayout(btn_row)

    # ------------------------------------------------------------------
    # Result accessors (used by SelectTool after exec()).
    # ------------------------------------------------------------------

    def chosen_action(self) -> DragDisconnectAction:
        """The action the user picked.  Falls back to CANCEL on reject()."""
        return self._action

    def session_opt_out(self) -> bool:
        """True iff the user ticked 'Don't ask again for this session'."""
        return self._opt_out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _accept(self, action: DragDisconnectAction) -> None:
        self._action = action
        self._opt_out = self._opt_out_checkbox.isChecked()
        self.accept()
