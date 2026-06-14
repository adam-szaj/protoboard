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
from protoboard.domain.components.package.loader import (
    PackageDefError,
    build_package,
    load_packages,
    resolve_extends,
)
from protoboard.domain.components.package.mask import (
    PadCell,
    build_pad_mask,
    has_collision,
    masks_collide,
)
from protoboard.domain.components.package.shapes import (
    BOARD_DEFAULT,
    BOARD_SOLVE,
    SHAPE_REGISTRY,
    Cylinder,
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
    "Leads",
    "MountOrientation",
    "OffsetRange",
    "Package",
    "PackageDefError",
    "PackageFootprint",
    "PadCell",
    "Prism",
    "axial_board_offset_range",
    "build_package",
    "build_pad_mask",
    "has_collision",
    "load_packages",
    "masks_collide",
    "resolve_extends",
]
