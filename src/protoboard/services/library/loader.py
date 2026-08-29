# SPDX-License-Identifier: MIT
"""Component library loader.

Reads YAML files from one or more directories and exposes a name-keyed
component dictionary. When the same name appears in multiple
directories, the *first* directory wins (so users can override built-ins).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from protoboard.domain.components.appearance import Appearance, parse_style
from protoboard.domain.components.definition import (
    ComponentClass,
    ComponentDefinition,
    PackageOption,
    PinBendSpec,
    PinVariant,
)
from protoboard.domain.components.footprints import (
    SIL,
    Footprint,
)
from protoboard.domain.components.package import (
    Package,
    PackageDefError,
    PackageFamily,
    PackageVariant,
    package_to_footprint,
    Prism,
    build_package,
    is_rigid_package,
    resolve_extends,
    resolve_variant,
)
from protoboard.domain.components.package.generators import ResidualKind
from protoboard.errors import SerializationError
from protoboard.paths import BUILTIN_ASSETS_DIR
from protoboard.services.debug_trace import traced

_log = structlog.get_logger(__name__)
_yaml = YAML(typ="safe")


def _load_builtin_package_defs() -> dict[str, dict[str, Any]]:
    """Raw builtin package definitions (name → dict) from assets/packages/*.yaml.

    These are the generics/named bases a component's inline ``package:`` may
    ``extends``. Cached after first load. Every call (re)binds the declarative
    kind nodes the files carry (``kinds.yaml``) — binding identical content is
    silent, so this keeps the global KIND_NODES registry deterministic even
    after a test snapshot/restore cleared it.
    """
    from protoboard.domain.components.package.kind_nodes import (
        KIND_NODES,
        register_kind_nodes,
    )
    cached = getattr(_load_builtin_package_defs, "_cache", None)
    if cached is not None:
        if any(
                isinstance(d.get("kind"), str) and d["kind"] not in KIND_NODES
                for d in cached.values()):
            register_kind_nodes(cached)
        return cached  # type: ignore[no-any-return]
    defs: dict[str, dict[str, Any]] = {}
    pkg_dir = BUILTIN_ASSETS_DIR / "packages"
    if pkg_dir.is_dir():
        for yaml_path in sorted(pkg_dir.glob("*.yaml")):
            data = _yaml.load(yaml_path.read_text())
            for d in (data or {}).get("packages", []):
                if isinstance(d, dict) and isinstance(d.get("name"), str):
                    defs[d["name"]] = d
    register_kind_nodes(defs)
    _load_builtin_package_defs._cache = defs  # type: ignore[attr-defined]
    return defs


def _component_pin_count(data: dict[str, Any]) -> int | None:
    """The component's declared pin count, or ``None``.

    Accepts ``pins: {count: N}`` (with optional ``names:``) or a bare ``pins: N``.
    This is the logical-identity pin count a ``kind:`` package is parameterized
    by; legacy parts without ``pins:`` derive it from the footprint instead.
    """
    pins = data.get("pins")
    if pins is None:
        return None
    if isinstance(pins, dict):
        count = pins.get("count")
        return None if count is None else int(count)
    return int(pins)


def _geometry_from_option(
    option: PackageOption,
    families: dict[str, PackageFamily],
) -> tuple[Package | None, Footprint | None, bool]:
    """Resolve a footprint option to ``(package, footprint, drop_bendable)``.

    An inline option carries its package directly; a reference resolves through
    the family catalogue. ``drop_bendable`` is True for a rigid catalogue body
    (its fixed pin spacing supersedes per-pin bend). Returns ``(None, None,
    False)`` when a reference does not resolve (a stale ref degrades gracefully).
    """
    if option.package is not None:
        return option.package, package_to_footprint(option.package), False
    if option.ref is not None:
        variant = resolve_variant(families, option.ref)
        if variant is not None:
            return (
                variant.package,
                package_to_footprint(variant.package),
                is_rigid_package(variant.package),
            )
    return None, None, False


def _parse_footprints(
    data: dict[str, Any],
    pin_count: int | None,
    source: Path,
    families: dict[str, PackageFamily],
    generics: dict[str, dict[str, Any]] | None = None,
) -> tuple[PackageOption, ...]:
    """Parse a component's ``footprints:`` list into ``PackageOption``s.

    Each entry is either a reference (``ref: "family/variant"`` — or a bare
    string) or an inline ``package:`` block (built via the kind/extends machinery,
    parameterized by *pin_count*). A list-wide ``footprint_style:`` default is
    overlaid by a per-entry ``style:``. ``name`` defaults to the ref/family token.
    """
    raw_list = data.get("footprints")
    if raw_list is None:
        return ()
    if not isinstance(raw_list, list):
        raise SerializationError(f"{source}: 'footprints' must be a list")
    cname = data["name"]
    try:
        list_style = parse_style(data.get("footprint_style"),
                                 ctx=f"{cname} footprint_style")
    except (TypeError, ValueError) as exc:
        raise SerializationError(f"{source}: {exc}") from exc

    options: list[PackageOption] = []
    for entry in raw_list:
        ref: str | None = None
        package: Package | None = None
        name_raw: object = None
        entry_style: Appearance | None = None
        if isinstance(entry, str):
            ref = entry
            name_raw = entry
        elif isinstance(entry, dict):
            if isinstance(entry.get("package"), dict):
                built = _build_component_package(cname, entry["package"],
                                                 pin_count, generics)
                if isinstance(built, ResidualKind):
                    # Partially-parameterised kind: the option becomes a
                    # pending-kind placeholder (generic-bbox geometry, target
                    # kind + holes stamped) — the picker's second pop-up
                    # collects the rest, exactly like the component-level
                    # ``package:`` path.  A provisional single-row pin cloud
                    # stands in for the geometry until the concrete variant is
                    # built.
                    if not pin_count:
                        raise SerializationError(
                            f"{source}: footprint entry for {cname!r} leaves "
                            f"kind {built.kind!r} parameters unfilled and the "
                            f"component declares no pin count")
                    package = _pending_kind_placeholder(
                        built, SIL(n=pin_count))
                    if package is None:
                        raise SerializationError(
                            f"{source}: footprint entry for {cname!r} leaves "
                            f"kind {built.kind!r} parameters unfilled")
                else:
                    package = built
            elif entry.get("ref") is not None:
                ref = str(entry["ref"])
            else:
                raise SerializationError(
                    f"{source}: footprint entry for {cname!r} needs 'ref' or "
                    f"'package'")
            name_raw = entry.get("name", ref)
            try:
                entry_style = parse_style(entry.get("style"),
                                          ctx=f"{cname} footprint style")
            except (TypeError, ValueError) as exc:
                raise SerializationError(f"{source}: {exc}") from exc
        else:
            raise SerializationError(
                f"{source}: footprint entry for {cname!r} must be a string or "
                f"mapping")
        name = str(name_raw) if name_raw is not None else ""
        style = Appearance.resolve(entry_style, list_style)
        options.append(
            PackageOption(
                name=name,
                ref=ref,
                package=package,
                style=style if not style.is_empty else None,
            ))
    return tuple(options)


def _pending_kind_placeholder(
    residual: ResidualKind, footprint: Footprint,
) -> Package | None:
    """A generic-bbox stand-in carrying a partially-applied kind.

    Geometry stays the synthetic generic bbox over *footprint*'s pin cloud
    (``_generic`` remains in the lineage, so generic consumers — DRC, bend
    specs — keep treating it as such), but the target kind + its parameter
    state ride along so the picker can offer the remaining holes and rebuild
    a concrete body.  ``None`` when no placeholder can be synthesized.
    """
    from protoboard.domain.components.package.generators import (
        KIND_GENERATORS,
    )
    from protoboard.domain.components.package.kind import (
        synth_generic_package,
    )

    synth = synth_generic_package(footprint)
    if not isinstance(synth, Prism):
        return synth
    gen_cls = KIND_GENERATORS.get(residual.kind)
    return dataclasses.replace(
        synth,
        kind=residual.kind,
        schema=gen_cls(residual.kind).params if gen_cls is not None else None,
        raw_values=dict(residual.values),
    )


def _parametric_preview_count(raw: dict[str, Any]) -> int | None:
    """Preview pin count for a parametric ``package: {kind: …}`` block.

    A kind package whose component declares no ``pins:`` anywhere is a
    PARAMETRIC (variable-pin-count) entry — the user chooses the count at
    placement (see ``services/library/parametric.py``). The preview body
    (library-panel icon, pre-placement ghost) is built at the last-step
    generator's ``preview_pin_count``. ``None`` when the block is not a
    kind package (then the ordinary "needs a pin count" error applies).
    """
    from protoboard.domain.components.package.kind import (
        KIND_GENERATORS,
        is_kind,
    )

    kind = raw.get("kind")
    if is_kind(kind):
        last = str(kind)
    elif isinstance(kind, list) and kind and all(is_kind(k) for k in kind):
        last = str(kind[-1])
    else:
        return None
    gen_cls = KIND_GENERATORS.get(last)
    return None if gen_cls is None else gen_cls.preview_pin_count


@traced()
def _build_component_package(
    name: str,
    raw: dict[str, Any],
    pin_count: int | None = None,
    generics: dict[str, dict[str, Any]] | None = None,
) -> Package | ResidualKind:
    """Build a Package from a component's inline ``package:`` block.

    A ``kind:`` field (e.g. ``dip_ic``) names a parameterized package whose
    geometry is built from *pin_count* via the kind generator — the look is known
    but the size follows the part. A kind whose required parameters are only
    partially supplied returns a :class:`ResidualKind` (the package-picker
    collects the rest at place time). Otherwise the block may ``extends`` a
    generic from *generics* (default: the builtin registry; ``load_library``
    passes the builtins merged with the library files' own ``packages:``
    sections); we register it as a temporary named package alongside them,
    resolve the extends chain, and build. Raises ``PackageDefError`` on a
    malformed / incompletely-closed definition (or an unrealisable kind).
    """
    from protoboard.domain.components.package.kind import (
        GeneratorContext,
        build_kind_package,
        is_kind,
    )

    # Load the builtin package defs BEFORE kind dispatch — the load binds the
    # declarative kind nodes (kinds.yaml), which the kind driver consults.
    registry = dict(
        _load_builtin_package_defs() if generics is None else generics)

    kind = raw.get("kind")
    # ``kind:`` may be a single name or a list — a left→right generator chain
    # (compute steps deriving values, the last step building the body).
    chain: tuple[str, ...] | None = None
    if is_kind(kind):
        chain = (str(kind),)
    elif isinstance(kind, list) and kind and all(is_kind(k) for k in kind):
        chain = tuple(str(k) for k in kind)
    if chain is not None:
        n = pin_count if raw.get("pins") is None else int(raw["pins"])
        if n is None:
            raise PackageDefError(
                f"{name}: package kind {kind!r} needs a pin count "
                f"(component pins or package 'pins')")
        # Forward any remaining package fields (e.g. a connector's ``rows``) as
        # generator values; the builder ignores keys it does not use.
        values = {k: v for k, v in raw.items() if k not in ("kind", "pins")}
        ctx = build_kind_package(
            chain, GeneratorContext(pin_count=n, values=values))
        if ctx.residual is not None:
            return ctx.residual
        assert ctx.package is not None  # driver sets package XOR residual
        return ctx.package

    node = {**raw, "name": name}
    registry[name] = node
    merged = resolve_extends(name, registry)
    from protoboard.domain.components.package.loader import extends_chain
    return build_package(
        merged, name, lineage=extends_chain(name, registry))


@dataclass
class ComponentLibrary:
    _by_name: dict[str, ComponentDefinition] = field(default_factory=dict)
    _source: dict[str, Path] = field(default_factory=dict)
    # Families contributed by the library files' own ``families:`` sections
    # (already merged into the catalogue used for resolution during the load).
    # The session merges these into its picker catalogue so library-defined
    # bodies are offered alongside the builtin ones.
    package_families: dict[str, PackageFamily] = field(default_factory=dict)

    def names(self) -> Iterable[str]:
        return self._by_name.keys()

    def get(self, name: str) -> ComponentDefinition:
        """The definition *name* (raises ``KeyError`` when unknown).

        A missing name that parses against a parametric base entry
        (``PinHeader_1x05`` → base ``PinHeader``) synthesizes the concrete
        definition, caches and returns it — a project saved with such a
        ``definition_ref`` keeps resolving (services/library/parametric.py).
        """
        found = self._by_name.get(name)
        if found is not None:
            return found
        from protoboard.services.library.parametric import (
            resolve_parametric_name,
        )
        synth = resolve_parametric_name(self._by_name, name)
        if synth is None:
            raise KeyError(name)
        self._by_name[synth.name] = synth
        return synth

    def source_path(self, name: str) -> Path | None:
        """Return the YAML file that defines *name*, or None if unknown."""
        return self._source.get(name)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._by_name


# Inline footprint options have no catalogue family of their own, so we register
# each as a one-variant family under this address shape. The picker hands the
# instance a normal "family/variant" address and ``resolve_component_package``
# resolves it through the same ``resolve_variant`` path as any catalogue body —
# no special branch in the hot resolution function.
_INLINE_VARIANT = "std"


def inline_option_address(defn_name: str, option_name: str) -> str:
    """The catalogue address for an inline footprint option of *defn_name*."""
    return f"@{defn_name}/{option_name}/{_INLINE_VARIANT}"


def inline_option_families(
    library: ComponentLibrary, ) -> dict[str, PackageFamily]:
    """One-variant families for every inline footprint option in *library*.

    Merge these into the package-family catalogue so the picker can offer inline
    options and an instance can record its choice as a normal address.
    """
    out: dict[str, PackageFamily] = {}
    for name in library.names():
        defn = library.get(name)
        for option in defn.package_options:
            if option.package is None:
                continue
            family_id = f"@{name}/{option.name}"
            out[family_id] = PackageFamily(
                family_id=family_id,
                label=option.name,
                variants=(PackageVariant(
                    variant_id=_INLINE_VARIANT,
                    label=option.name,
                    package=option.package,
                ), ),
            )
    return out


def load_library(
    directories: Iterable[Path],
    package_families: dict[str, PackageFamily] | None = None,
) -> ComponentLibrary:
    """Load a library from the given directories. Earlier directories win.

    Besides ``components:``, a library file may carry ``packages:`` (reusable
    generics that inline ``package:`` blocks and ``families:`` entries may
    ``extends``) and ``families:`` (catalogue families for ``ref:``) — see
    docs/footprints/definition-variants.md.
    Builtins win on a name clash. The library-contributed families ride on
    the returned ``ComponentLibrary.package_families``.

    *package_families* is the package-family catalogue. A ``footprints:``
    reference to a rigid package (an SMD chip, a DIP — fixed pin geometry)
    derives its footprint from that package and drops bendable leads, so the
    body fixes the pad spacing. When ``None`` (the default), the full shipped
    catalogue is loaded — matching the production path — so "load the real
    library" works out of the box. Pass an explicit ``{}`` for fixture
    libraries that resolve no references.
    """
    if package_families is None:
        from protoboard.services.library.package_family_loader import (
            load_full_package_catalogue,
        )
        families = load_full_package_catalogue()
    else:
        families = package_families
    # Pass 1 — collect every raw component dict (templates included) across all
    # files, keyed by name, with first-wins (earlier directory/file keeps the
    # name). The global index lets ``extends`` reference a base in another file.
    # Library files may also carry ``packages:`` (reusable generics for
    # ``extends``) and ``families:`` (catalogue entries for ``ref:``) sections
    # — collected here, merged below.
    raw_by_name: dict[str, Any] = {}
    raw_source: dict[str, Path] = {}
    lib_packages: dict[str, dict[str, Any]] = {}
    lib_pkg_source: dict[str, Path] = {}
    lib_family_entries: list[tuple[dict[str, Any], Path]] = []
    for directory in directories:
        if not directory.exists():
            raise FileNotFoundError(
                f"Library directory not found: {directory}")
        for yaml_path in sorted(directory.glob("**/*.yaml")):
            sections = _read_library_file(yaml_path)
            for raw in sections.components:
                name = raw["name"]
                if name not in raw_by_name:
                    raw_by_name[name] = raw
                    raw_source[name] = yaml_path
            for raw in sections.packages:
                if raw["name"] not in lib_packages:
                    lib_packages[raw["name"]] = raw
                    lib_pkg_source[raw["name"]] = yaml_path
            lib_family_entries.extend(
                (entry, yaml_path) for entry in sections.families)

    # Generics registry: builtins plus the library files' ``packages:``. A
    # builtin wins on a name clash (warn — the library entry is ignored), the
    # same first-wins rule as components (the builtin dir is scanned first).
    generics = dict(_load_builtin_package_defs())
    for pkg_name, raw in lib_packages.items():
        if pkg_name in generics:
            _log.warning("library.package_name_clash", package=pkg_name,
                         path=str(lib_pkg_source[pkg_name]))
            continue
        generics[pkg_name] = raw
        # A library ``packages:`` entry may itself be a declarative kind node
        # (a plugin shipping generator + node together) — bind it like the
        # builtin ones. Resolution runs against the merged generics registry.
        if isinstance(raw.get("kind"), str):
            from protoboard.domain.components.package.kind_nodes import (
                register_kind_node,
            )
            try:
                register_kind_node(pkg_name, generics)
            except PackageDefError as exc:
                _log.warning("library.kind_node_skipped", package=pkg_name,
                             path=str(lib_pkg_source[pkg_name]),
                             error=str(exc))

    # Families from library files, parsed against the merged generics. The
    # passed-in catalogue wins on an id clash. The library-contributed ones are
    # kept on the returned ComponentLibrary so the session can merge them into
    # its picker catalogue.
    lib_families: dict[str, PackageFamily] = {}
    if lib_family_entries:
        from protoboard.services.library.package_family_loader import (
            parse_family,
        )
        for entry, yaml_path in lib_family_entries:
            try:
                fam = parse_family(entry, yaml_path, generics)
            except ValueError as exc:
                _log.warning("library.family_skipped", path=str(yaml_path),
                             error=str(exc))
                continue
            if fam.family_id in families or fam.family_id in lib_families:
                _log.warning("library.family_name_clash",
                             family_id=fam.family_id, path=str(yaml_path))
                continue
            lib_families[fam.family_id] = fam
    families = {**families, **lib_families}

    # Pass 2 — resolve extends against the global index and build the placeable
    # definitions (``_``-prefixed names are templates only).
    lib = ComponentLibrary(package_families=lib_families)
    for name, raw in raw_by_name.items():
        if name.startswith("_"):
            continue
        path = raw_source[name]
        resolved = _resolve_extends(raw, raw_by_name, source=path, chain=[])
        try:
            defn = _component_from_dict(resolved,
                                        source=path,
                                        families=families,
                                        generics=generics)
        except PackageDefError as exc:
            # A malformed parametric package must not sink the whole library.
            # Skip this component; the user re-places it with a fresh definition.
            _log.warning(
                "library.package_skipped",
                component=name,
                path=str(path),
                error=str(exc),
            )
            continue
        if lib._by_name.setdefault(defn.name, defn) is defn:
            lib._source[defn.name] = path
        # aliases: extra names resolving to the same definition (the parent under
        # a different name) — inherit every field, replace only the name.
        for alias in resolved.get("aliases") or []:
            if isinstance(alias, str) and alias:
                aliased = dataclasses.replace(defn, name=alias)
                if lib._by_name.setdefault(alias, aliased) is aliased:
                    lib._source[alias] = path
    return lib


# Top-level sections a library YAML file may carry. Anything else is warned
# about — a typo (``familes:``) must not vanish silently.
_KNOWN_SECTIONS = frozenset({"components", "packages", "families"})


@dataclass(frozen=True)
class _LibraryFileSections:
    """The raw top-level sections of one library YAML file."""

    components: list[dict[str, Any]]
    packages: list[dict[str, Any]]
    families: list[dict[str, Any]]


def _read_library_file(path: Path) -> _LibraryFileSections:
    """Parse a library file into its raw top-level sections.

    ``components:`` and ``packages:`` entries must be mappings with a string
    ``name``; ``families:`` entries are validated later by the family parser.
    Unknown top-level keys are logged (not fatal).
    """
    try:
        data = _yaml.load(path.read_text())
    except YAMLError as exc:
        raise SerializationError(f"YAML parse error in {path}: {exc}") from exc
    if data is None:
        return _LibraryFileSections([], [], [])
    if not isinstance(data, dict):
        raise SerializationError(f"{path}: top-level must be a mapping")
    for key in data:
        if key not in _KNOWN_SECTIONS:
            _log.warning("library.unknown_section", path=str(path),
                         section=str(key))

    def _named_dicts(section: str) -> list[dict[str, Any]]:
        raw_list = data.get(section, [])
        if not isinstance(raw_list, list):
            raise SerializationError(f"{path}: '{section}' must be a list")
        out: list[dict[str, Any]] = []
        for raw in raw_list:
            if not isinstance(raw, dict) or not isinstance(
                    raw.get("name"), str):
                raise SerializationError(
                    f"{path}: each {section[:-1]} must be a mapping with "
                    f"'name'")
            out.append(raw)
        return out

    families_raw = data.get("families", [])
    if not isinstance(families_raw, list):
        raise SerializationError(f"{path}: 'families' must be a list")
    return _LibraryFileSections(
        components=_named_dicts("components"),
        packages=_named_dicts("packages"),
        families=[f for f in families_raw if isinstance(f, dict)],
    )


def _resolve_extends(
    raw: dict[str, Any],
    index: dict[str, Any],
    source: Path,
    chain: list[str],
) -> dict[str, Any]:
    """Return a raw dict with all 'extends' fields merged (child overrides parent)."""
    name = raw.get("name", "<unknown>")
    if name in chain:
        raise SerializationError(
            f"{source}: circular extends detected: {' -> '.join(chain + [name])}"
        )
    parent_name = raw.get("extends")
    if parent_name is None:
        return raw
    if not isinstance(parent_name, str):
        raise SerializationError(
            f"{source}: 'extends' must be a string in {name!r}")
    parent_raw = index.get(parent_name)
    if parent_raw is None:
        raise SerializationError(
            f"{source}: {name!r} extends unknown template {parent_name!r}")
    # Recursively resolve the parent first.
    resolved_parent = _resolve_extends(parent_raw, index, source,
                                       chain + [name])
    # Child fields override parent; 'extends' key is dropped.
    merged: dict[str, Any] = {**resolved_parent, **raw}
    merged.pop("extends", None)
    # pin_names: child overrides individual entries (not whole dict replacement).
    parent_pins: dict[str, Any] = resolved_parent.get("pin_names") or {}
    child_pins: dict[str, Any] = raw.get("pin_names") or {}
    if parent_pins or child_pins:
        merged["pin_names"] = {**parent_pins, **child_pins}
    return merged


def _component_from_dict(
    data: Any,
    source: Path,
    families: dict[str, PackageFamily] | None = None,
    generics: dict[str, dict[str, Any]] | None = None,
) -> ComponentDefinition:
    if not isinstance(data, dict):
        raise SerializationError(f"{source}: each component must be a mapping")
    if "name" not in data or not isinstance(data["name"], str):
        raise SerializationError(f"{source}: component requires string 'name'")

    # Parametric package (docs/footprints/package-model.md). When present, it
    # is the source of geometry (it implements the Footprint protocol directly).
    # A malformed package raises PackageDefError, which the caller turns into a
    # skipped component (the user re-places it with a fresh definition) rather
    # than failing the whole load.
    # A component may declare its pin count directly (``pins: {count: N}`` or
    # ``pins: N``) — the logical-identity-first authoring. A ``kind:`` package
    # is parameterized by it.
    comp_pin_count = _component_pin_count(data)

    package_obj: Package | None = None
    # A kind whose required parameters the component leaves unfilled: no body
    # can be built at load time — the placeholder synthesized below carries the
    # pending kind so the place-tool picker collects the rest.
    pending_kind: ResidualKind | None = None
    # Variable-pin-count entry: a kind package with no ``pins:`` anywhere —
    # the count is the user's placement-time choice; the load builds only a
    # preview body (see services/library/parametric.py).
    parametric_pins = False
    package_raw = data.get("package")
    if isinstance(package_raw, dict):
        build_pin_count = comp_pin_count
        if comp_pin_count is None and package_raw.get("pins") is None:
            preview_count = _parametric_preview_count(package_raw)
            if preview_count is not None:
                parametric_pins = True
                build_pin_count = preview_count
        built = _build_component_package(data["name"], package_raw,
                                         build_pin_count, generics)
        if isinstance(built, ResidualKind):
            pending_kind = built
        else:
            package_obj = built

    # The available footprints (the picker offers these). The default/first entry
    # is the singular geometry source when there is no inline ``package``
    # block, so instance-less consumers see one default geometry.
    footprints = _parse_footprints(data, comp_pin_count, source, families
                                   or {}, generics)

    if "package_family_ref" in data:
        raise PackageDefError(
            f"{data['name']}: 'package_family_ref:' was removed — declare "
            "catalogue geometry with 'footprints: [{name: ..., ref: "
            "family/variant}]'")

    # The removed legacy field. A loud, actionable error — raised as
    # PackageDefError so ONE legacy part is skipped (with the migration hint
    # in the log) instead of sinking the whole library load.
    if "footprint" in data:
        raise PackageDefError(
            f"{data['name']}: the 'footprint:' field was removed — declare "
            f"geometry via 'package:' (inline or kind:), 'footprints:' "
            "(catalogue refs)")

    # Geometry chain — the definition's DEFAULT body. Precedence: inline
    # ``package:`` → first ``footprints:`` option → the pending-kind placeholder.
    drop_bend_from_default = False
    if package_obj is not None:
        footprint: Footprint = package_to_footprint(package_obj)
    elif footprints:
        pkg, fp, drop_bend_from_default = _geometry_from_option(
            footprints[0], families or {})
        if pkg is None or fp is None:
                raise PackageDefError(
                    f"{source}: component {data['name']!r} default footprint "
                    f"{footprints[0].name!r} did not resolve to a package")
        package_obj, footprint = pkg, fp
    elif pending_kind is not None and comp_pin_count:
        # A partially-parameterised kind has no geometry yet — a provisional
        # single-row footprint stands in until the picker builds the concrete
        # variant (placement resolves geometry from that variant's package).
        footprint = SIL(n=comp_pin_count)
    else:
        raise SerializationError(
            f"{source}: component {data.get('name')!r} requires 'package', "
            "'footprints'")

    pin_names_raw = data.get("pin_names") or {}
    if not isinstance(pin_names_raw, dict):
        raise SerializationError(
            f"{source}: pin_names for {data['name']!r} must be a mapping")
    pin_names: dict[int, str] = {}
    for k, v in pin_names_raw.items():
        try:
            pin_names[int(k)] = str(v)
        except (TypeError, ValueError) as exc:
            raise SerializationError(
                f"{source}: pin_names key {k!r} is not an integer") from exc

    metadata_raw = data.get("metadata") or {}
    if not isinstance(metadata_raw, dict):
        raise SerializationError(
            f"{source}: metadata for {data['name']!r} must be a mapping")

    # component_class is an OPEN tag: a known value normalizes to its
    # ComponentClass member; an unknown one loads as-is (warned — it is a
    # typo-catcher, not a gate) and behaves like OTHER everywhere, so newer
    # libraries keep loading on older code.
    raw_class = str(data.get("component_class", "other"))
    if not raw_class:
        raise SerializationError(
            f"{source}: empty component_class for {data['name']!r}")
    comp_class: str
    try:
        comp_class = ComponentClass(raw_class)
    except ValueError:
        _log.warning(
            "library.unknown_component_class",
            component=data["name"],
            component_class=raw_class,
            path=str(source),
        )
        comp_class = raw_class

    from protoboard.geometry import Vector as _Vector

    def _parse_vector(raw: Any, ctx: str) -> _Vector:
        if isinstance(raw, dict):
            return _Vector(float(raw["x"]), float(raw["y"]))
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            return _Vector(float(raw[0]), float(raw[1]))
        raise SerializationError(f"{source}: invalid Vector {raw!r} in {ctx}")

    def _parse_point(raw: Any, ctx: str) -> _Vector:
        return _parse_vector(raw, ctx)

    # def _parse_point(raw: Any, ctx: str) -> _Point:
    #     if isinstance(raw, dict):
    #         return _Point(float(raw["x"]), float(raw["y"]))
    #     if isinstance(raw, (list, tuple)) and len(raw) == 2:
    #         return _Point(float(raw[0]), float(raw[1]))
    #     raise SerializationError(f"{source}: invalid Point {raw!r} in {ctx}")

    bendable_raw = data.get("bendable_pins") or {}
    bendable_pins: dict[int, PinBendSpec] = {}
    if isinstance(bendable_raw, dict):
        for k, spec_raw in bendable_raw.items():
            try:
                pin_num = int(k)
            except (TypeError, ValueError) as exc:
                raise SerializationError(
                    f"{source}: bendable_pins key {k!r} is not an integer"
                ) from exc

            ctx = f"bendable_pins[{pin_num}]"
            if isinstance(spec_raw, dict):
                if "min_reach" in spec_raw or "max_reach" in spec_raw:
                    # Format C — zakres 2D swobodny (nowy)
                    bendable_pins[pin_num] = PinBendSpec(
                        min_reach=float(spec_raw.get("min_reach", 0.0)),
                        max_reach=float(spec_raw.get("max_reach", 0.0)),
                        default_offset=_parse_vector(spec_raw["default"], ctx +
                                                     ".default")
                        if "default" in spec_raw else _Vector(0.0, 0.0),
                    )
                elif "min" in spec_raw and "max" in spec_raw:
                    # Format B — stary zakres kierunkowy; konwertuj do reach
                    import math as _math
                    min_pt = _parse_point(spec_raw["min"], ctx + ".min")
                    max_pt = _parse_point(spec_raw["max"], ctx + ".max")
                    bendable_pins[pin_num] = PinBendSpec(
                        min_reach=_math.hypot(min_pt.dx, min_pt.dy),
                        max_reach=_math.hypot(max_pt.dx, max_pt.dy),
                        default_offset=_parse_vector(spec_raw["default"], ctx +
                                                     ".default")
                        if "default" in spec_raw else _Vector(0.0, 0.0),
                    )
                else:
                    raise SerializationError(
                        f"{source}: {ctx} dict must have 'min_reach'/'max_reach'"
                        " or 'min'+'max'")
            else:
                raise SerializationError(
                    f"{source}: {ctx} must be a dict,"
                    f" got {type(spec_raw).__name__}")

    variants_raw = data.get("variants") or []
    variants: list[PinVariant] = []
    if isinstance(variants_raw, list):
        for v in variants_raw:
            if not isinstance(v, dict):
                raise SerializationError(
                    f"{source}: each variant for {data['name']!r} must be a mapping"
                )
            if "id" not in v:
                raise SerializationError(
                    f"{source}: variant for {data['name']!r} requires 'id'")
            v_pins: dict[int, str] = {}
            for k, val in (v.get("pin_names") or {}).items():
                try:
                    v_pins[int(k)] = str(val)
                except (TypeError, ValueError) as exc:
                    raise SerializationError(
                        f"{source}: variant pin_names key {k!r} is not an integer"
                    ) from exc
            variants.append(
                PinVariant(
                    id=str(v["id"]),
                    label=str(v.get("label", v["id"])),
                    pin_names=v_pins,
                ))

    default_value_raw = data.get("default_value")
    default_value: str = ""
    if default_value_raw is not None:
        if not isinstance(default_value_raw, str):
            raise SerializationError(
                f"{source}: default_value for {data['name']!r} must be a "
                f"string; got {default_value_raw!r}")
        default_value = default_value_raw

    try:
        appearance = parse_style(data.get("style"),
                                 ctx=f"{data['name']} style")
    except (TypeError, ValueError) as exc:
        raise SerializationError(f"{source}: {exc}") from exc

    symbol_raw = data.get("symbol")
    symbol: str | None = None
    if symbol_raw is not None:
        if not isinstance(symbol_raw, str) or not symbol_raw:
            raise SerializationError(
                f"{source}: symbol for {data['name']!r} must be a non-empty string"
            )
        symbol = symbol_raw

    # A rigid default footprint (resolved above) fixes the pad spacing, so its
    # per-pin bend is dropped.
    if drop_bend_from_default:
        bendable_pins = {}

    # Invariant: every definition carries a package. When none was declared
    # (axial diodes / R / C placed as THT, crystals, screw terminals), synthesize
    # a NON-rigid generic bbox package from the footprint so consumers can always
    # read ``defn.package`` instead of branching on ``None``. It is tagged
    # ``kind="_generic"`` and non-rigid (bend > 0) so ``is_rigid_package`` stays
    # False and the part keeps its authored bendable leads — bend-spec and DRC
    # consumers exclude the generic tag to preserve their pre-invariant behaviour.
    if package_obj is None and footprint.pin_count() >= 1:
        if pending_kind is not None:
            # Pending-kind placeholder — see _pending_kind_placeholder.
            package_obj = _pending_kind_placeholder(pending_kind, footprint)
        else:
            # Local import (mirrors the build_kind_package import above) to
            # avoid a module-load cycle: package.kind imports this package's
            # loader.
            from protoboard.domain.components.package.kind import (
                synth_generic_package,
            )
            package_obj = synth_generic_package(footprint)
    if package_obj is None:
        # ComponentDefinition.package is required — a part whose geometry
        # cannot produce even a synthesized generic body is unloadable.
        raise SerializationError(
            f"{source}: component {data['name']!r} yields no package "
            f"(footprint has no pins?)")

    try:
        return ComponentDefinition(
            name=data["name"],
            pin_names=pin_names,
            description=str(data.get("description", "")),
            component_class=comp_class,
            metadata=dict(metadata_raw),
            bendable_pins=bendable_pins,
            variants=tuple(variants),
            default_value=default_value,
            package=package_obj,
            appearance=appearance,
            symbol=symbol,
            footprints=footprints,
            parametric_pins=parametric_pins,
        )
    except ValueError as exc:
        raise SerializationError(
            f"{source}: invalid component {data['name']!r}: {exc}") from exc


__all__ = ["ComponentLibrary", "load_library"]
