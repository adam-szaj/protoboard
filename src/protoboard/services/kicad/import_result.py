# SPDX-License-Identifier: MIT
"""Result type returned by a full netlist import."""

from __future__ import annotations

from dataclasses import dataclass, field

from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.schematic.schematic import Schematic
from protoboard.services.kicad.footprint_map import FootprintMap


@dataclass
class NetlistImportResult:
    """Full result of importing a KiCAD netlist.

    Attributes:
        schematic:     Logical netlist (nets + pin references).
        instances:     One ComponentInstance per component in the netlist,
                       keyed by UUID.  Instances whose KiCAD footprint was
                       unresolved receive a generic ``SIL_N`` definition_ref.
        footprint_map: The map that was used (auto-generated or user-supplied).
                       Save this to ``footprint_map.yaml`` for user review.
        unresolved_fps: KiCAD footprint strings that had no entry in the map
                        (empty definition_ref *and* pin_count == 0).  These
                        components still appear in *instances* as SIL_0
                        placeholders — user must fill in pin_count in the map
                        and re-import.
    """

    schematic: Schematic
    instances: dict[str, ComponentInstance] = field(default_factory=dict)
    footprint_map: FootprintMap = field(default_factory=FootprintMap)
    unresolved_fps: list[str] = field(default_factory=list)

    @property
    def has_unresolved(self) -> bool:
        return bool(self.unresolved_fps)
