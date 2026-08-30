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
    MetaPin,
    PackageOption,
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
    Prism,
    build_package,
    package_to_footprint,
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
            for d in cached.values()
        ):
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

    ``meta_pins`` is the source of truth. A missing list denotes a parametric
    component whose count is chosen during placement.
    """
    meta_pins = data.get("meta_pins")
    return len(meta_pins) if isinstance(meta_pins, list) else None


def _geometry_from_option(
    option: PackageOption,
    families: dict[str, PackageFamily],
) -> tuple[Package | None, Footprint | None]:
    """Resolve a footprint option to ``(package, footprint)``.

    Every option resolves through the family catalogue. Returns ``(None, None)``
    when a reference does not resolve.
    """
    variant = resolve_variant(families, option.ref)
    if variant is not None:
        return variant.package, package_to_footprint(variant.package)
    return None, None


def _register_component_package(
    package: Package,
    *,
    family_id: str,
    label: str,
    families: dict[str, PackageFamily],
    contributed_families: dict[str, PackageFamily],
) -> str:
    if family_id in families:
        raise PackageDefError(f"package family {family_id!r} already exists")
    family = PackageFamily(
        family_id=family_id,
        label=label,
        variants=(
            PackageVariant(
                variant_id=_INLINE_VARIANT,
                label=label,
                package=package,
            ),
        ),
    )
    families[family_id] = family
    contributed_families[family_id] = family
    return f"{family_id}/{_INLINE_VARIANT}"


def _parse_package_options(
    data: dict[str, Any],
    pin_count: int | None,
    source: Path,
    families: dict[str, PackageFamily],
    contributed_families: dict[str, PackageFamily],
    generics: dict[str, dict[str, Any]] | None = None,
) -> tuple[PackageOption, ...]:
    """Parse a component's ``package_options:`` into runtime options.

    Every reference uses an authoring prefix (``@``, ``#``, or ``$``). Inline
    ``package:`` entries are built through the same package machinery.
    """
    raw_list = data.get("package_options")
    if raw_list is None:
        return ()
    if not isinstance(raw_list, list):
        raise SerializationError(f"{source}: 'package_options' must be a list")
    cname = data["name"]
    try:
        list_style = parse_style(
            data.get("footprint_style"), ctx=f"{cname} footprint_style"
        )
    except (TypeError, ValueError) as exc:
        raise SerializationError(f"{source}: {exc}") from exc

    options: list[PackageOption] = []
    for entry in raw_list:
        ref: str | None = None
        package: Package | None = None
        name_raw: object = None
        entry_style: Appearance | None = None
        pin_map: dict[int, tuple[int, ...]] = {}
        if isinstance(entry, str):
            ref = entry
            name_raw = entry
            has_explicit_name = False
        elif isinstance(entry, dict):
            pin_map = _parse_pin_map(entry.get("pin_map"), source, component_name=cname)
            if isinstance(entry.get("package"), dict):
                built = _build_component_package(
                    cname, entry["package"], pin_count, generics
                )
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
                            f"component declares no pin count"
                        )
                    package = _pending_kind_placeholder(built, SIL(n=pin_count))
                    if package is None:
                        raise SerializationError(
                            f"{source}: footprint entry for {cname!r} leaves "
                            f"kind {built.kind!r} parameters unfilled"
                        )
                else:
                    package = built
            elif entry.get("ref") is not None:
                ref = str(entry["ref"])
            else:
                raise SerializationError(
                    f"{source}: footprint entry for {cname!r} needs 'ref' or "
                    f"'package'"
                )
            name_raw = entry.get("name", ref)
            has_explicit_name = "name" in entry
            try:
                entry_style = parse_style(
                    entry.get("style"), ctx=f"{cname} footprint style"
                )
            except (TypeError, ValueError) as exc:
                raise SerializationError(f"{source}: {exc}") from exc
        else:
            raise SerializationError(
                f"{source}: footprint entry for {cname!r} must be a string or "
                f"mapping"
            )
        name = str(name_raw) if name_raw is not None else ""
        if ref is not None:
            prefix, target = ref[:1], ref[1:]
            if prefix not in {"@", "#", "$"} or not target:
                raise SerializationError(
                    f"{source}: package option ref {ref!r} must use @, #, or $"
                )
            if prefix == "@":
                ref = target
                if not has_explicit_name:
                    name = ref
                if resolve_variant(families, ref) is None:
                    raise SerializationError(
                        f"{source}: package option ref @{ref} did not resolve"
                    )
            else:
                package_raw = {"extends": target} if prefix == "#" else {"kind": target}
                built = _build_component_package(
                    cname, package_raw, pin_count, generics
                )
                if isinstance(built, ResidualKind):
                    raise SerializationError(
                        f"{source}: package option ref {ref!r} requires "
                        "additional generator parameters"
                    )
                package = built
        if package is not None:
            family_id = f"{cname}-{name.lstrip('@#$')}-package"
            ref = _register_component_package(
                package,
                family_id=family_id,
                label=name,
                families=families,
                contributed_families=contributed_families,
            )
        style = Appearance.resolve(entry_style, list_style)
        options.append(
            PackageOption(
                name=name,
                ref=ref or "",
                style=style if not style.is_empty else None,
                pin_map=pin_map,
            )
        )
    return tuple(options)


def _parse_pin_map(
    raw: object,
    source: Path,
    *,
    component_name: str,
) -> dict[int, tuple[int, ...]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SerializationError(
            f"{source}: pin_map for {component_name!r} must be a mapping"
        )
    parsed: dict[int, tuple[int, ...]] = {}
    for raw_meta_index, raw_package_pins in raw.items():
        try:
            meta_index = int(raw_meta_index)
        except (TypeError, ValueError) as exc:
            raise SerializationError(
                f"{source}: pin_map MetaPin index {raw_meta_index!r} "
                "is not an integer"
            ) from exc
        if meta_index < 1:
            raise SerializationError(
                f"{source}: pin_map MetaPin index must be positive"
            )
        if not isinstance(raw_package_pins, list):
            raise SerializationError(f"{source}: pin_map[{meta_index}] must be a list")
        try:
            parsed[meta_index] = tuple(int(pin) for pin in raw_package_pins)
        except (TypeError, ValueError) as exc:
            raise SerializationError(
                f"{source}: pin_map[{meta_index}] contains a non-integer pin"
            ) from exc
    return parsed


def _parse_meta_pins(
    raw: object,
    source: Path,
    *,
    component_name: str,
) -> tuple[MetaPin, ...]:
    if not isinstance(raw, list) or not raw:
        raise SerializationError(
            f"{source}: meta_pins for {component_name!r} must be a non-empty list"
        )
    meta_pins: list[MetaPin] = []
    for entry in raw:
        labels_raw = entry if isinstance(entry, list) else [entry]
        try:
            meta_pins.append(MetaPin(tuple(str(label) for label in labels_raw)))
        except ValueError as exc:
            raise SerializationError(
                f"{source}: invalid MetaPin for {component_name!r}: {exc}"
            ) from exc
    return tuple(meta_pins)


def _validate_pin_map(
    option: PackageOption,
    meta_pin_count: int,
    package_pin_count: int,
    source: Path,
) -> None:
    physical_pins: list[int] = []
    for meta_index, package_pins in option.pin_map.items():
        if not 1 <= meta_index <= meta_pin_count:
            raise SerializationError(
                f"{source}: pin_map references MetaPin {meta_index} outside "
                f"1..{meta_pin_count}"
            )
        for package_pin in package_pins:
            if not 1 <= package_pin <= package_pin_count:
                raise SerializationError(
                    f"{source}: pin_map references package pin {package_pin} "
                    f"outside 1..{package_pin_count}"
                )
            physical_pins.append(package_pin)
    if len(set(physical_pins)) != len(physical_pins):
        raise SerializationError(
            f"{source}: a physical package pin maps to more than one MetaPin"
        )


def _pending_kind_placeholder(
    residual: ResidualKind,
    footprint: Footprint,
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
    registry = dict(_load_builtin_package_defs() if generics is None else generics)

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
                f"(component pins or package 'pins')"
            )
        # Forward any remaining package fields (e.g. a connector's ``rows``) as
        # generator values; the builder ignores keys it does not use.
        values = {k: v for k, v in raw.items() if k not in ("kind", "pins")}
        ctx = build_kind_package(chain, GeneratorContext(pin_count=n, values=values))
        if ctx.residual is not None:
            return ctx.residual
        assert ctx.package is not None  # driver sets package XOR residual
        return ctx.package

    node = {**raw, "name": name}
    registry[name] = node
    merged = resolve_extends(name, registry)
    from protoboard.domain.components.package.loader import extends_chain

    return build_package(merged, name, lineage=extends_chain(name, registry))


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

        synth = resolve_parametric_name(self._by_name, name, self.package_families)
        if synth is None:
            raise KeyError(name)
        self._by_name[synth.name] = synth
        return synth

    def source_path(self, name: str) -> Path | None:
        """Return the YAML file that defines *name*, or None if unknown."""
        return self._source.get(name)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._by_name


_INLINE_VARIANT = "std"


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

    *package_families* is the package-family catalogue. A ``package_options:``
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
            raise FileNotFoundError(f"Library directory not found: {directory}")
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
            lib_family_entries.extend((entry, yaml_path) for entry in sections.families)

    # Generics registry: builtins plus the library files' ``packages:``. A
    # builtin wins on a name clash (warn — the library entry is ignored), the
    # same first-wins rule as components (the builtin dir is scanned first).
    generics = dict(_load_builtin_package_defs())
    for pkg_name, raw in lib_packages.items():
        if pkg_name in generics:
            _log.warning(
                "library.package_name_clash",
                package=pkg_name,
                path=str(lib_pkg_source[pkg_name]),
            )
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
                _log.warning(
                    "library.kind_node_skipped",
                    package=pkg_name,
                    path=str(lib_pkg_source[pkg_name]),
                    error=str(exc),
                )

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
                _log.warning(
                    "library.family_skipped", path=str(yaml_path), error=str(exc)
                )
                continue
            if fam.family_id in families or fam.family_id in lib_families:
                _log.warning(
                    "library.family_name_clash",
                    family_id=fam.family_id,
                    path=str(yaml_path),
                )
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
            defn = _component_from_dict(
                resolved,
                source=path,
                families=families,
                contributed_families=lib_families,
                generics=generics,
            )
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
            _log.warning("library.unknown_section", path=str(path), section=str(key))

    def _named_dicts(section: str) -> list[dict[str, Any]]:
        raw_list = data.get(section, [])
        if not isinstance(raw_list, list):
            raise SerializationError(f"{path}: '{section}' must be a list")
        out: list[dict[str, Any]] = []
        for raw in raw_list:
            if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
                raise SerializationError(
                    f"{path}: each {section[:-1]} must be a mapping with " f"'name'"
                )
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
        raise SerializationError(f"{source}: 'extends' must be a string in {name!r}")
    parent_raw = index.get(parent_name)
    if parent_raw is None:
        raise SerializationError(
            f"{source}: {name!r} extends unknown template {parent_name!r}"
        )
    # Recursively resolve the parent first.
    resolved_parent = _resolve_extends(parent_raw, index, source, chain + [name])
    # Child fields override parent; 'extends' key is dropped.
    merged: dict[str, Any] = {**resolved_parent, **raw}
    merged.pop("extends", None)
    return merged


def _component_from_dict(
    data: Any,
    source: Path,
    families: dict[str, PackageFamily] | None = None,
    contributed_families: dict[str, PackageFamily] | None = None,
    generics: dict[str, dict[str, Any]] | None = None,
) -> ComponentDefinition:
    if not isinstance(data, dict):
        raise SerializationError(f"{source}: each component must be a mapping")
    if "name" not in data or not isinstance(data["name"], str):
        raise SerializationError(f"{source}: component requires string 'name'")
    legacy_fields = {
        "pins",
        "pin" + "_names",
        "footprints",
        "package_family" + "_ref",
    }.intersection(data)
    if legacy_fields:
        field = sorted(legacy_fields)[0]
        raise SerializationError(
            f"{source}: component {data['name']!r} uses removed field {field!r}"
        )
    family_catalogue = families if families is not None else {}
    contributed = contributed_families if contributed_families is not None else {}
    if "package" in data and "package_options" in data:
        raise SerializationError(
            f"{source}: component {data['name']!r} cannot declare both "
            "'package' and 'package_options'"
        )

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
        built = _build_component_package(
            data["name"], package_raw, build_pin_count, generics
        )
        if isinstance(built, ResidualKind):
            pending_kind = built
        else:
            package_obj = built

    # The available packages (the picker offers these). The default/first entry
    # is the singular geometry source when there is no inline ``package``
    # block, so instance-less consumers see one default geometry.
    package_options = _parse_package_options(
        data,
        comp_pin_count,
        source,
        family_catalogue,
        contributed,
        generics,
    )

    # The removed legacy field. A loud, actionable error — raised as
    # PackageDefError so ONE legacy part is skipped (with the migration hint
    # in the log) instead of sinking the whole library load.
    if "footprint" in data:
        raise PackageDefError(
            f"{data['name']}: the 'footprint:' field was removed — declare "
            f"geometry via 'package:' or 'package_options:'"
        )

    options = list(package_options)
    if package_obj is not None:
        ref = _register_component_package(
            package_obj,
            family_id=f"{data['name']}-package",
            label=f"{data['name']}-package",
            families=family_catalogue,
            contributed_families=contributed,
        )
        options.insert(0, PackageOption(name=f"{data['name']}-package", ref=ref))
    elif pending_kind is not None and comp_pin_count:
        placeholder = _pending_kind_placeholder(pending_kind, SIL(n=comp_pin_count))
        if placeholder is None:
            raise SerializationError(
                f"{source}: cannot build pending package for {data['name']!r}"
            )
        ref = _register_component_package(
            placeholder,
            family_id=f"{data['name']}-package",
            label=f"{data['name']}-package",
            families=family_catalogue,
            contributed_families=contributed,
        )
        options.insert(0, PackageOption(name=f"{data['name']}-package", ref=ref))
    elif not options and comp_pin_count:
        from protoboard.domain.components.package.kind import synth_generic_package

        fallback = synth_generic_package(SIL(n=comp_pin_count))
        if fallback is None:
            raise SerializationError(
                f"{source}: cannot build fallback package for {data['name']!r}"
            )
        ref = _register_component_package(
            fallback,
            family_id=f"{data['name']}-package",
            label=f"{data['name']}-package",
            families=family_catalogue,
            contributed_families=contributed,
        )
        options.append(PackageOption(name=f"{data['name']}-package", ref=ref))

    if not options:
        raise SerializationError(
            f"{source}: component {data.get('name')!r} requires 'package' "
            "or 'package_options'"
        )
    default_package, _ = _geometry_from_option(options[0], family_catalogue)
    if default_package is None:
        raise PackageDefError(
            f"{source}: component {data['name']!r} default package "
            f"{options[0].ref!r} did not resolve"
        )
    pin_count = comp_pin_count or default_package.pin_count()

    if "meta_pins" not in data and not parametric_pins:
        raise SerializationError(
            f"{source}: component {data['name']!r} requires 'meta_pins'"
        )
    meta_pins = (
        _parse_meta_pins(data["meta_pins"], source, component_name=data["name"])
        if "meta_pins" in data
        else tuple(MetaPin((f"P{pin}",)) for pin in range(1, pin_count + 1))
    )
    if package_obj is not None and options:
        options[0] = dataclasses.replace(
            options[0],
            pin_map=_parse_pin_map(
                data.get("pin_map"), source, component_name=data["name"]
            ),
        )
    for index, option in enumerate(options):
        option_package, _ = _geometry_from_option(option, family_catalogue)
        if option_package is None:
            raise SerializationError(
                f"{source}: package option {option.ref!r} did not resolve"
            )
        _validate_pin_map(
            options[index], len(meta_pins), option_package.pin_count(), source
        )

    metadata_raw = data.get("metadata") or {}
    if not isinstance(metadata_raw, dict):
        raise SerializationError(
            f"{source}: metadata for {data['name']!r} must be a mapping"
        )

    # component_class is an OPEN tag: a known value normalizes to its
    # ComponentClass member; an unknown one loads as-is (warned — it is a
    # typo-catcher, not a gate) and behaves like OTHER everywhere, so newer
    # libraries keep loading on older code.
    raw_class = str(data.get("component_class", "other"))
    if not raw_class:
        raise SerializationError(
            f"{source}: empty component_class for {data['name']!r}"
        )
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

    default_value_raw = data.get("default_value")
    default_value: str = ""
    if default_value_raw is not None:
        if not isinstance(default_value_raw, str):
            raise SerializationError(
                f"{source}: default_value for {data['name']!r} must be a "
                f"string; got {default_value_raw!r}"
            )
        default_value = default_value_raw

    try:
        appearance = parse_style(data.get("style"), ctx=f"{data['name']} style")
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

    try:
        return ComponentDefinition(
            name=data["name"],
            meta_pins=meta_pins,
            description=str(data.get("description", "")),
            component_class=comp_class,
            metadata=dict(metadata_raw),
            default_value=default_value,
            appearance=appearance,
            symbol=symbol,
            package_options=tuple(options),
            parametric_pins=parametric_pins,
        )
    except ValueError as exc:
        raise SerializationError(
            f"{source}: invalid component {data['name']!r}: {exc}"
        ) from exc


__all__ = ["ComponentLibrary", "load_library"]
