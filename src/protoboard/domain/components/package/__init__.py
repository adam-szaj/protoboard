# SPDX-License-Identifier: MIT
"""Generic parametric package model (pure domain, no Qt).

Implements the model specified in ``docs/footprints/package-model.md``: body vs board
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

from protoboard.domain.components.package.bend import PinBendSpec
from protoboard.domain.components.package.body import (
    DiscProfile,
    EdgeStyle,
    LeadArrangement,
    MountStyle,
)
from protoboard.domain.components.package.component_package import (
    resolve_component_package,
    resolve_package_variant_footprint,
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
from protoboard.domain.components.package.pin_mapping import (
    meta_pin_for_package_pin,
    package_pins_for_meta_pin,
    resolve_package_option,
)
from protoboard.domain.components.package.reach import (
    PinReach,
    RedragResult,
    effective_bend_specs,
    is_pin_bendable,
    package_to_bend_specs,
    pin_reaches,
    redrag_feasibility,
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
    is_rigid_package,
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
    "PackageVariant",
    "PadCell",
    "PinReach",
    "PinBendSpec",
    "Prism",
    "axial_board_offset_range",
    "body_holes",
    "resolve_component_package",
    "resolve_package_variant_footprint",
    "resolve_variant",
    "build_package",
    "build_pad_mask",
    "has_collision",
    "is_rigid_package",
    "is_pin_bendable",
    "effective_bend_specs",
    "package_to_bend_specs",
    "redrag_feasibility",
    "RedragResult",
    "MountStyle",
    "load_packages",
    "masks_collide",
    "meta_pin_for_package_pin",
    "package_pins_for_meta_pin",
    "pin_reaches",
    "resolve_extends",
    "resolve_package_option",
]
