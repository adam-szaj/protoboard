# SPDX-License-Identifier: MIT
"""BoardSnapshot — immutable view of editor state for headless rendering.

Bundles every piece of state a renderer needs into one dataclass so the
export pipeline does not have to thread a dozen objects through every
function call.  ``from_session`` is the canonical builder used by file
exporters: it captures the live ``EditorSession`` state but neutralises
the GUI-only inputs (camera side, view transform, active group, layer
visibility) so the resulting snapshot describes the *board itself*, not
the user's current viewport.

The snapshot is consumed by :mod:`protoboard.services.export.paint`,
which builds a transient :class:`QGraphicsScene` populated by the same
renderers BoardView uses and rasterises it onto whatever QPainter the
caller supplies (SVG generator, PDF printer, PNG image).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from protoboard.controller.view_options.layer_state import LayerState
from protoboard.controller.view_options.layers import LayerOverrides
from protoboard.controller.view_options.theme import DEFAULT_THEME, Theme
from protoboard.domain.layout.layout import ROOT_GROUP_ID

if TYPE_CHECKING:
    from protoboard.domain.board.board import Board
    from protoboard.domain.board.pad_state import PadIndex
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.components.package import PackageFamily
    from protoboard.domain.layout.layout import Layout
    from protoboard.domain.schematic.schematic import Schematic
    from protoboard.gui.session import EditorSession


CameraSide = Literal["top", "bottom"]
ViewTransform = Literal["normal", "mirror_h", "mirror_v", "mirror_hv"]


@dataclass(frozen=True)
class BoardSnapshot:
    """Immutable bundle of state required to render the board.

    Built by :meth:`from_session`.  Caller may swap individual fields
    via :meth:`with_view` to render multiple variants (top / bottom /
    mirrored) from the same source — used by the multi-panel template
    exporter.
    """

    board: Board
    layout: Layout
    components: dict[str, ComponentDefinition]
    instances: dict[uuid.UUID, ComponentInstance]
    schematic: Schematic
    pad_index: PadIndex
    package_families: dict[str, PackageFamily]
    theme: Theme = field(default_factory=lambda: DEFAULT_THEME)
    camera_side: CameraSide = "top"
    view_transform: ViewTransform = "normal"
    active_group_id: uuid.UUID = ROOT_GROUP_ID
    layer_overrides: LayerOverrides = field(default_factory=LayerOverrides)
    # Routing-target net (neon-yellow override).  Always ``None`` for
    # canonical export — the GUI's session.routing_net_id is a UI signal
    # we deliberately ignore for file output.
    routing_net_id: uuid.UUID | None = None

    @classmethod
    def from_session(
        cls,
        session: EditorSession,
        *,
        canonical: bool = True,
        theme: Theme | None = None,
    ) -> BoardSnapshot:
        """Capture session state into a snapshot.

        ``canonical=True`` (default) produces a layout-independent view:
        camera=top, view=normal, active_group=ROOT, every layer VISIBLE,
        no routing-target override.  Suitable for file exports — the
        output looks the same regardless of how the user has the editor
        configured at the moment of clicking "Export".

        ``canonical=False`` mirrors the live BoardView state — useful
        for a literal screenshot.  Not used by file exporters today;
        present for future "Export current view" features.
        """
        pad_index = session.pad_index()
        if canonical:
            return cls(
                board=session.board,
                layout=session.layout,
                components=session.components,
                instances=session.instances,
                schematic=session.schematic,
                pad_index=pad_index,
                package_families=session.package_families,
                theme=theme or DEFAULT_THEME,
                camera_side="top",
                view_transform="normal",
                active_group_id=ROOT_GROUP_ID,
                layer_overrides=LayerOverrides(),
                routing_net_id=None,
            )
        # Non-canonical: copy live layer overrides too.
        return cls(
            board=session.board,
            layout=session.layout,
            components=session.components,
            instances=session.instances,
            schematic=session.schematic,
            pad_index=pad_index,
            package_families=session.package_families,
            theme=theme or DEFAULT_THEME,
            camera_side="top",
            view_transform="normal",
            active_group_id=ROOT_GROUP_ID,
            layer_overrides=session.layer_overrides,
            routing_net_id=session.routing_net_id,
        )

    def with_view(
        self,
        *,
        camera_side: CameraSide | None = None,
        view_transform: ViewTransform | None = None,
    ) -> BoardSnapshot:
        """Return a copy with the camera / view transform overridden.

        Used by the multi-panel template exporter to render top, bottom,
        and mirrored variants from one snapshot.  Other state (layout,
        instances, theme) is shared — these are read-only inputs.
        """
        from dataclasses import replace as _replace

        kwargs: dict[str, object] = {}
        if camera_side is not None:
            kwargs["camera_side"] = camera_side
        if view_transform is not None:
            kwargs["view_transform"] = view_transform
        return _replace(self, **kwargs)  # type: ignore[arg-type]

    def layer_state(self) -> LayerState:
        """Derive a :class:`LayerState` from the snapshot's stored bits."""
        from dataclasses import replace as _replace

        s = LayerState(
            camera_side=self.camera_side,
            view_transform=self.view_transform,
            overrides=self.layer_overrides,
        )
        return _replace(
            s,
            back_alpha=self.theme.back_alpha,
            board_ghost_alpha=self.theme.board_ghost_alpha,
            ghost_comp_alpha=self.theme.ghost_comp_alpha,
            board_mid_alpha=self.theme.board_mid_alpha,
            back_filter_alpha=self.theme.back_filter_alpha,
        )


def build_snapshot(
    board: Board,
    layout: Layout,
    components: dict[str, ComponentDefinition],
    instances: dict[uuid.UUID, ComponentInstance],
    schematic: Schematic | None = None,
    theme: Theme = DEFAULT_THEME,
    package_families: dict[str, PackageFamily] | None = None,
) -> BoardSnapshot:
    """Build the canonical snapshot shared by file exporters."""
    from protoboard.domain.board.pad_state import PadIndex
    from protoboard.domain.schematic.schematic import Schematic as SchematicModel

    sch = schematic if schematic is not None else SchematicModel(nets={})
    pad_index = PadIndex(
        board=board,
        layout=layout,
        schematic=sch,
        components=components,
        instances=instances,
    )
    families = (
        package_families
        if package_families is not None
        else getattr(theme, "package_families", None)
    ) or {}
    return BoardSnapshot(
        board=board,
        layout=layout,
        components=components,
        instances=instances,
        schematic=sch,
        pad_index=pad_index,
        package_families=families,
        theme=theme,
    )


__all__ = ["BoardSnapshot", "CameraSide", "ViewTransform", "build_snapshot"]
