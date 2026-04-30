# SPDX-License-Identifier: MIT
"""Dialog shown when a placed/moved component conflicts with an existing pin."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
)

from protoboard.domain.layout.pad_conflict import ConflictKind, PadConflict


class PadConflictDialog(QDialog):
    """Informs the user about pad conflicts and asks for resolution.

    Returns via exec():
      - QDialog.DialogCode.Accepted  → user chose to share (allowed conflicts only)
      - QDialog.DialogCode.Rejected  → user cancelled / conflict is unresolvable
    """

    def __init__(
        self,
        conflicts: list[PadConflict],
        incoming_refdes: str,
        existing_refdes_map: dict[int, str],  # existing_comp index → refdes
        parent: object = None,
    ) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        self.setWindowTitle("Pad Conflict")
        self.setMinimumWidth(420)
        self.setModal(True)

        layout = QVBoxLayout(self)

        # Classify conflicts.
        blocked = [c for c in conflicts if c.kind == ConflictKind.DIFFERENT_NETS]
        auto_ok = [c for c in conflicts if c.kind == ConflictKind.SAME_NET]
        needs_decision = [
            c for c in conflicts
            if c.kind in (ConflictKind.ONE_UNCONNECTED, ConflictKind.BOTH_UNCONNECTED)
        ]

        self._can_accept = len(blocked) == 0

        if blocked:
            title = QLabel("<b>Placement blocked — net conflict</b>")
            title.setStyleSheet("color: #c00;")
            layout.addWidget(title)
            for c in blocked:
                ex_ref = existing_refdes_map.get(id(c), "?")
                msg = (
                    f"  Pin {c.incoming_pin} of <b>{incoming_refdes}</b> "
                    f"({c.incoming_net_name or 'no net'}) would share pad "
                    f"({c.position.x:.2f}, {c.position.y:.2f}) with "
                    f"pin {c.existing_pin} of <b>{ex_ref}</b> "
                    f"({c.existing_net_name or 'no net'}) — different nets."
                )
                lbl = QLabel(msg)
                lbl.setWordWrap(True)
                lbl.setTextFormat(Qt.TextFormat.RichText)
                layout.addWidget(lbl)

        if auto_ok:
            info = QLabel(
                f"<b>{len(auto_ok)} pin(s)</b> will share a pad with the same net — OK."
            )
            info.setStyleSheet("color: #080;")
            info.setTextFormat(Qt.TextFormat.RichText)
            layout.addWidget(info)

        if needs_decision and not blocked:
            q = QLabel(
                "<b>The following pins will share a pad with an unconnected pin.\n"
                "Allow shared placement?</b>"
            )
            q.setWordWrap(True)
            q.setTextFormat(Qt.TextFormat.RichText)
            layout.addWidget(q)
            for c in needs_decision:
                ex_ref = existing_refdes_map.get(id(c), "?")
                if c.kind == ConflictKind.ONE_UNCONNECTED:
                    if c.existing_net_id is not None:
                        detail = (
                            f"  Pin {c.incoming_pin} of <b>{incoming_refdes}</b> "
                            f"(no net) → will join net <b>{c.existing_net_name}</b> "
                            f"via pin {c.existing_pin} of <b>{ex_ref}</b>."
                        )
                    else:
                        detail = (
                            f"  Pin {c.incoming_pin} of <b>{incoming_refdes}</b> "
                            f"({c.incoming_net_name}) with pin {c.existing_pin} "
                            f"of <b>{ex_ref}</b> (no net) → existing pin joins net."
                        )
                else:
                    detail = (
                        f"  Pin {c.incoming_pin} of <b>{incoming_refdes}</b> "
                        f"and pin {c.existing_pin} of <b>{ex_ref}</b> "
                        f"(both unconnected) → new net will be created."
                    )
                lbl = QLabel(detail)
                lbl.setWordWrap(True)
                lbl.setTextFormat(Qt.TextFormat.RichText)
                layout.addWidget(lbl)

        # Buttons.
        if self._can_accept and (needs_decision or auto_ok):
            if needs_decision:
                buttons = QDialogButtonBox(
                    QDialogButtonBox.StandardButton.Yes
                    | QDialogButtonBox.StandardButton.No
                )
                buttons.accepted.connect(self.accept)
                buttons.rejected.connect(self.reject)
            else:
                # All same-net → auto-proceed, just an info OK button.
                buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
                buttons.accepted.connect(self.accept)
        else:
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(self.reject)
            buttons.accepted.connect(self.reject)

        layout.addWidget(buttons)

    @property
    def can_accept(self) -> bool:
        return self._can_accept
