# SPDX-License-Identifier: MIT
"""Declarative kind nodes — YAML package nodes bound to kind generators.

A *kind node* is a package definition carrying a ``kind:`` field
(``assets/packages/kinds.yaml``, or a library file's ``packages:`` section)::

    - name: dip_ic
      kind: dip_ic          # linkage to the registered generator
      extends: prism_dil    # ordinary extends — schema + leads inherited
      color: "#2b2b30"
      dims: {width: auto, length: auto}
      leads: {body_offset: auto}

The node declares everything parameter-INdependent; every slot whose value
depends on the parameter set (chiefly the owning component's pin count) reads
``auto`` and is filled by the generator's ``resolve_auto`` value transform when
the body builds through the ordinary ``build_package`` flow (package-model.md
§5.4). A node may also declare its own ``schema:`` block for NEW kind
parameters (e.g. a pin-header's ``rows``) — that schema is bound into
:data:`~.generators.base.KIND_DECLARED_SCHEMAS` and served through
``PackageGenerator.params``, so the YAML is the source of truth and callers
keep talking only to the generator.

Registration happens when the package definitions load (the services loader
binds the builtin ``assets/packages`` files and any library ``packages:``
sections). A kind with no node keeps the legacy pure-code path — plugins that
only register a generator keep working unchanged.

Pure domain: no Qt, no I/O.
"""

from __future__ import annotations

import dataclasses

import structlog

from protoboard.domain.components.package.generators.base import (
    KIND_DECLARED_SCHEMAS,
    KIND_GENERATORS,
)
from protoboard.domain.components.package.loader import (
    PackageDict,
    extends_chain,
    resolve_extends,
)
from protoboard.domain.components.package.schema import parse_schema

_log = structlog.get_logger(__name__)


@dataclasses.dataclass(frozen=True, slots=True)
class KindNode:
    """One bound kind node: the extends-resolved value tree + its lineage.

    ``merged`` is the fully extends-flattened definition (``schema:`` included,
    ``name``/``kind`` stripped) — the base tree the kind driver deep-merges the
    caller's values onto before ``build_package``. ``lineage`` is the extends
    ancestry root-first with the kind name as the leaf, so a renderer
    registered for the kind wins dispatch.
    """

    kind: str
    merged: PackageDict
    lineage: tuple[str, ...]


# kind name → bound node. Populated by :func:`register_kind_node`; later
# registrations win (mirroring KIND_GENERATORS), a *conflicting* rebind warns.
KIND_NODES: dict[str, KindNode] = {}


def register_kind_node(name: str, registry: dict[str, PackageDict]) -> None:
    """Bind the kind node *name* from *registry* (a name → raw-def map).

    Resolves the node's ``extends`` chain against *registry*, parses its own
    ``schema:`` block (when present) into :data:`KIND_DECLARED_SCHEMAS`, and
    enters the :class:`KindNode` into :data:`KIND_NODES`. Idempotent for
    identical content (re-loading the same files is silent); a rebind with
    *different* content warns and the later one wins. Warnings, not gates:
    a name≠kind mismatch, an unknown generator (it may be a plugin registered
    later) and a generator whose ``code_params`` the node's schema shadows
    are all logged and registration proceeds.
    """
    raw = registry[name]
    kind = str(raw["kind"])
    if kind != name:
        _log.warning("kind_node.name_mismatch", node=name, kind=kind)
    if kind not in KIND_GENERATORS:
        _log.warning("kind_node.unknown_generator", node=name, kind=kind)
    merged = resolve_extends(name, registry)
    merged.pop("name", None)
    merged.pop("kind", None)
    # Lineage leaf is the KIND (renderer dispatch key), whatever the node name.
    lineage = (*extends_chain(name, registry)[:-1], kind)
    node = KindNode(kind=kind, merged=merged, lineage=lineage)
    prev = KIND_NODES.get(kind)
    if prev is not None and prev != node:
        _log.warning("kind_node.rebound", kind=kind)
    KIND_NODES[kind] = node

    own_schema = raw.get("schema")
    if own_schema is not None:
        gen_cls = KIND_GENERATORS.get(kind)
        if gen_cls is not None and not gen_cls.code_params.is_empty():
            _log.warning("kind_node.schema_shadows_code_params", kind=kind)
        KIND_DECLARED_SCHEMAS[kind] = parse_schema(own_schema)


def register_kind_nodes(registry: dict[str, PackageDict]) -> None:
    """Bind every ``kind:``-bearing node in *registry*."""
    for name, node in registry.items():
        if isinstance(node.get("kind"), str):
            register_kind_node(name, registry)


__all__ = ["KIND_NODES", "KindNode", "register_kind_node",
           "register_kind_nodes"]
