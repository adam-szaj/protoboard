# SPDX-License-Identifier: MIT
"""Generic parametric package model (pure domain, no Qt).

Implements the model specified in ``docs/footprints/spec.md``: body vs board
offsets, axial solver, bend, collision masks. Geometry is computed by code
(``shapes.py``); YAML is normalised into plain structures by the loader before
reaching this layer.

Public API::

    from protoboard.domain.components.package import (
        Package, Cylinder, Prism, Leads,           # shapes
        OffsetRange, MountOrientation,             # solver
        PadCell, build_pad_mask, masks_collide,    # collision
        load_packages, PackageDefError,            # loader
    )
"""

from protoboard.domain.components.package.adapter import PackageFootprint
from protoboard.domain.components.package.body import (
    DiscProfile,
    EdgeStyle,
    LeadArrangement,
)
from protoboard.domain.components.package.family import (
    PackageFamily,
    PackageVariant,
    resolve_variant,
)
from protoboard.domain.components.package.loader import (
    PackageDefError,
    build_package,
    load_packages,
    resolve_extends,
)
from protoboard.domain.components.package.mask import (
    PadCell,
    body_holes,
    build_pad_mask,
    has_collision,
    masks_collide,
)
from protoboard.domain.components.package.reach import (
    PinReach,
    is_pin_bendable,
    pin_reaches,
)
from protoboard.domain.components.package.shapes import (
    BOARD_DEFAULT,
    BOARD_SOLVE,
    SHAPE_REGISTRY,
    Cylinder,
    Disc,
    Leads,
    Package,
    Prism,
)
from protoboard.domain.components.package.solver import (
    RASTER_MM,
    MountOrientation,
    OffsetRange,
    axial_board_offset_range,
)

__all__ = [
    "BOARD_DEFAULT",
    "BOARD_SOLVE",
    "RASTER_MM",
    "SHAPE_REGISTRY",
    "Cylinder",
    "Disc",
    "DiscProfile",
    "EdgeStyle",
    "LeadArrangement",
    "Leads",
    "MountOrientation",
    "OffsetRange",
    "Package",
    "PackageDefError",
    "PackageFamily",
    "PackageFootprint",
    "PackageVariant",
    "PadCell",
    "PinReach",
    "Prism",
    "axial_board_offset_range",
    "body_holes",
    "resolve_variant",
    "build_package",
    "build_pad_mask",
    "has_collision",
    "is_pin_bendable",
    "load_packages",
    "masks_collide",
    "pin_reaches",
    "resolve_extends",
]
