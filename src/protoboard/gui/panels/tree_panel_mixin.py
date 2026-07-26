# SPDX-License-Identifier: MIT
"""TreePanelMixin — shared boilerplate for panels built around a QTreeWidget.

Provides:
  - _session / _syncing state management
  - refresh() template method that calls _rebuild()
  - sync_selection() with a guarded clear + scroll pattern
  - _find_item() recursive UUID search helper
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem

from protoboard.services.debug_trace import traced

if TYPE_CHECKING:
    from protoboard.gui.session import EditorSession

_ROLE = Qt.ItemDataRole.UserRole


class TreePanelMixin:
    """Mixin for QWidget panels that display a QTreeWidget driven by EditorSession.

    Concrete classes must:
      1. Assign ``self._tree: QTreeWidget`` before calling any mixin method.
      2. Override ``_rebuild()`` to populate the tree from ``self._session``.
      3. Override ``_id_col`` if UUID data is not stored in column 0.
    """

    _tree: QTreeWidget
    _session: "EditorSession | None" = None
    _syncing: bool = False

    # Column that stores UUID data (default 0).  Override in subclass if needed.
    _id_col: int = 0

    @traced()
    def refresh(self, session: "EditorSession") -> None:
        """Store session, (re)subscribe to ``selection_changed``, and rebuild.

        The panel owns its own board -> panel sync: it listens directly to the
        session rather than having ``MainWindow`` fan selection out to it.  On a
        session swap (open project) ``refresh`` is called with the new session,
        so we move the subscription across — the old session would otherwise keep
        a dead panel on its signal.
        """
        if self._session is not session:
            if self._session is not None:
                try:
                    self._session.selection_changed.disconnect(
                        self._on_session_selection
                    )
                except (TypeError, RuntimeError):
                    pass
            try:
                session.selection_changed.connect(self._on_session_selection)
            except (TypeError, RuntimeError):
                pass
        self._session = session
        self._rebuild()

    @traced()
    def _on_session_selection(
        self, comp_ids: object, wire_ids: object, net_id: object
    ) -> None:
        """Board -> panel: forward the broadcast selection to ``sync_selection``.

        Default behaviour; a panel needing more than row-highlighting can
        override ``sync_selection`` (not this) so the subscription stays uniform.
        """
        self.sync_selection(comp_ids, wire_ids, net_id)

    @traced()
    def _rebuild(self) -> None:  # pragma: no cover
        """Populate self._tree from self._session.  Must be overridden."""
        raise NotImplementedError

    @traced()
    def sync_selection(
        self,
        comp_ids: object,
        wire_ids: object,
        net_id: object,
    ) -> None:
        """Highlight tree rows that correspond to the current board selection.

        Default implementation matches rows by *comp_ids* (frozenset[UUID]).
        Override for panels that use wire_ids or net_id instead.
        """
        if not self.isVisible():  # type: ignore[attr-defined]
            return
        self._syncing = True
        try:
            self._tree.clearSelection()
            first_match: QTreeWidgetItem | None = None
            for raw_id in (comp_ids if isinstance(comp_ids, frozenset) else frozenset()):
                item = self._find_item(str(raw_id))
                if item is not None:
                    item.setSelected(True)
                    if first_match is None:
                        first_match = item
            if first_match is not None:
                self._tree.scrollToItem(first_match)
        finally:
            self._syncing = False

    # ------------------------------------------------------------------
    # Item search helpers
    # ------------------------------------------------------------------

    @traced()
    def _find_item(self, id_str: str) -> QTreeWidgetItem | None:
        """Return the first tree item whose _id_col UserRole data equals id_str."""
        col = self._id_col

        @traced()
        def _search(parent: QTreeWidgetItem) -> QTreeWidgetItem | None:
            for i in range(parent.childCount()):
                child = parent.child(i)
                if child is None:
                    continue
                if child.data(col, _ROLE) == id_str:
                    return child
                found = _search(child)
                if found is not None:
                    return found
            return None

        for i in range(self._tree.topLevelItemCount()):
            top = self._tree.topLevelItem(i)
            if top is None:
                continue
            if top.data(col, _ROLE) == id_str:
                return top
            found = _search(top)
            if found is not None:
                return found
        return None

    @traced()
    def _find_item_by_uuid(self, uid: uuid.UUID) -> QTreeWidgetItem | None:
        """Convenience wrapper: find by UUID object."""
        return self._find_item(str(uid))

    @traced()
    def _select_row_by_uuid(self, uid: uuid.UUID) -> None:
        """Select and scroll to the row matching *uid*. No-op if not found."""
        item = self._find_item_by_uuid(uid)
        if item is not None:
            self._tree.clearSelection()
            item.setSelected(True)
            self._tree.scrollToItem(item)
