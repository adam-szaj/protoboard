# SPDX-License-Identifier: MIT
"""Package-kind generators: contract, registry, and the builtin recipes.

Importing this package registers the builtin generators (the module-level
``@register_generator`` decorators run as the class bodies execute); library
plugins under a ``generators/`` directory register the same way when the
service layer imports them
(:mod:`protoboard.services.library.plugin_discovery`).
"""

from protoboard.domain.components.package.generators import (  # noqa: F401
    connectors as _connectors,
)
from protoboard.domain.components.package.generators import (  # noqa: F401
    dip as _dip,
)
from protoboard.domain.components.package.generators import (  # noqa: F401
    generic as _generic,
)
from protoboard.domain.components.package.generators import (  # noqa: F401
    generic_prism as _generic_prism,
)
from protoboard.domain.components.package.generators import (  # noqa: F401
    sil as _sil,
)
from protoboard.domain.components.package.generators.base import (
    KIND_DECLARED_SCHEMAS,
    KIND_GENERATORS,
    GeneratorContext,
    PackageGenerator,
    ResidualKind,
    prism_from_footprint,
    register_generator,
)

__all__ = [
    "KIND_DECLARED_SCHEMAS",
    "KIND_GENERATORS",
    "GeneratorContext",
    "PackageGenerator",
    "ResidualKind",
    "prism_from_footprint",
    "register_generator",
]
