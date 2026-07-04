# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Kind generators completed: option partials, chain driver, recent variants (2026-07-04)

* **Partial kinds in `footprints:` options.** An option whose inline kind
  leaves required parameters unfilled loads as a per-option pending-kind
  placeholder (it used to fail the load); the picker's second pop-up collects
  the rest and builds the concrete variant. Also fixes a latent
  `resolve_variant` bug: inline-option addresses (`@defn/option/variant`)
  never resolved (first-slash split) and silently fell back to the default
  body.
* **Generator chain driver.** `kind:` may be a list — compute steps transform
  values (resolving `auto` holes against the schema riding on `ctx.schema`)
  and feed the next step; only the final step builds the body.
* **Recent variants in the kind dialog.** Built parameter sets persist per
  kind (`recent_kind_variants.json`); the kind pop-up re-offers them as
  one-confirm rows, rebuilt for the current part's pin count.

### Group scene-graph completed: transform is the single source of truth (2026-07-04)

`ComponentGroup` no longer stores the legacy `rotation`/`origin` pair —
`transform` carries the whole pose. `rotation` is a derived view
(`Transform2D.rotation_steps()`), the bbox top-left computes on demand
(`GroupTree.group_origin`), and the rotate pivot derives from the
transform's fixed point (sticky across consecutive rotations, follows
drags — exactly the old stored-origin behaviour, now by construction).
The per-move origin-recompute machinery is deleted. Project files stop
writing the pair; old v2 files still load (the migration folds the stored
pair into `transform`), and the command recorder migrates old records the
same way.

### Group scene-graph: float-transform frame fix + group flip in the GUI (2026-07-04)

* **Float-transform preview/commit now frame-correct for transformed groups.**
  `FloatTransform` read composed (world) placements and wrote them back as
  locals, so on any group with a non-identity transform (after a group
  rotate/flip) a `[G]` nudge teleported members, `[Esc]` corrupted the pose
  and a rotate step over-rotated. Every op is now a world-frame rigid
  transform conjugated into each member's local frame
  (`local' = (W⁻¹∘O∘W)·local`); snapshot/cancel/commit diff local placements.
  Loose parts and identity groups behave exactly as before; nested groups
  compose through the full ancestor chain.
* **Group flip is reachable from the GUI.** With a group selected, Flip
  (`Alt+R`) emits `FlipGroupCommand` (rigid-body mirror about the pin
  centroid) instead of flipping 2-pin members individually.

### Body-variant picker restored — offer derived from the explicit body (2026-07-04)

The library/properties body picker offers alternatives again (it was stubbed
to `[]` by the footprint→body cleanup). `offered_body_families` now derives
the offer from the component's **explicit body**, never the footprint type:
a kind-generator body asks its generator for pin-count-matching alternatives
(new `PackageGenerator.offered_family_ids` hook — DIP-16 offers
`dip-16`/`wdip-16`, headers offer `sil-N`), a plain two-pin body (or a bare
two-pin part) offers every two-pin family. The loader's synthetic `_generic`
package is no longer surfaced by the footprint picker as a parameterisable
kind option.

### Appearance override completed: per-corner radius render, GenericBody, GUI editor (2026-07-04)

The visual `style:` override (fill colour + corner radius) is now feature-complete:

* **Per-corner corner radius renders.** The 1–4 `corner-radius` values are
  drawn per corner using the CSS `border-radius` expansion
  (`Appearance.corner_radius_mm_corners` → top-left, top-right, bottom-right,
  bottom-left). New `rounded_rect_path` helper draws mixed radii; uniform
  radii keep the exact historical `addRoundedRect` look. Applied across every
  body path: bbox/devboard, radial top-view, SMD chip, DIP/SIL silhouettes.
* **`GenericBody` honours the override** — the bare-bbox fallback (no package)
  now applies the style colour and corner radius like the package renderers.
* **GUI editor for per-instance style.** The Properties panel gained two text
  fields (colour `#RRGGBB[AA]`, corner radius in mm, 1–4 values) committed via
  Apply; an empty field clears the override. Backed by the new
  `SetComponentAppearanceCommand` (undo/redo) and registered in the command
  recorder as `set_appearance` (replay-safe).

### Modular package rendering: lineage-dispatched renderer hierarchy + plugins (2026-07-03)

Rendering of component bodies is now modular and user-extensible:

* **Package lineage.** Every built `Package` carries `lineage` — its ancestry
  root-first: shape (`cylinder`/`prism`/`disc`) → resolved `extends` chain →
  family id → variant (or the `kind` for generator-built bodies). Stamped by
  the loaders (`load_packages`, inline component packages, family variants,
  kind generators); pure metadata, geometry never reads it.
* **Renderer hierarchy + registry.** The `PackageBody` god-class in
  `gui/renderers/body_shapes.py` is split into
  `gui/renderers/packages/` — `PackageRenderer` (shared radial machinery,
  colours, placement, bbox fallback) with `CylinderRenderer` /
  `PrismRenderer` / `DiscRenderer` subclasses. Dispatch walks `lineage`
  leaf-first over the `BODY_RENDERERS` registry, so the most specific
  renderer wins and unknown names degrade towards the shape root — rendering
  never blocks on an unknown package. `PackageBody` stays valid as a name
  (constructing the base class dispatches via `__new__`); `body_shape_for`
  is unchanged for callers.
* **Renderer plugins.** Any component-library directory may carry a
  `renderers/` subdirectory; every `*.py` in it is imported at library load
  (and on preferences reload). Declaring a `PackageRenderer` subclass with
  `renders = "<lineage-name>"` registers it automatically
  (`__init_subclass__`) — dropping a file in is the whole registration.
  A broken plugin is logged and skipped.

167 body/parity tests pass unchanged; +13 new registry/lineage/discovery
tests. Groundwork for per-kind silhouette renderers and the TO-220 plugin.

### Kind silhouettes as registry renderers; legacy TO92Body/TO220Body deleted (2026-07-04)

The remaining hard-wired body looks moved into the renderer registry:

* **Per-kind renderers** (`gui/renderers/packages/prism_kinds.py`) replace the
  `_KIND_SILHOUETTE` table: `DipIcRenderer` (`dip_ic`/`wdip_ic` — notch),
  `SilHeaderRenderer` (`sil` — housing), `PlainBodyRenderer`
  (`dip_switch`/`pin_header`/`jst` — plain rect). The declared look is an
  override of the new `PrismRenderer._silhouette()` hook; the base keeps the
  column guess, so an unregistered kind still renders. One dispatch system —
  `silhouette_for_prism` and the kind table are gone.
* **Legacy `TO92Body` / `TO220Body` deleted** (dead since the parametric
  package model took over): TO-92 renders through the flat-sided radial
  `CylinderRenderer`, TO-220 through the renderer plugin; behaviour guarded by
  the parity/plugin tests.
* Devboard stays gated by `ComponentClass.DEVBOARD` (a class trait valid for
  any package) — extraction noted in docs/todo.md.

### Fix: false BODY_OVERLAP for SMD variants — two collision-model defects (2026-07-04)

An 0805 resistor flagged BODY_OVERLAP against a TO-220 five rasters away.
Two independent defects compounded (3 + 2 = 5 holes of phantom footprint):

* **DRC used the definition's default body, not the placed variant.** The
  body-overlap mask came from ``defn.package`` (for `R`: the axial DIN-0207
  default) regardless of ``instance.package_variant``. New strict helper
  ``package_of(defn, inst)`` (the body-side twin of ``footprint_of``, backed
  by ``ComponentInstance.resolved_package()``) now feeds the mask, the filter,
  the placement transform, and both bendable-pin exemptions in the engine.
* **Explicit prism dims were axis-transposed for row-along-x packages.**
  ``Prism.bbox()`` hardcoded length→y; a TO-220 (10 mm ALONG its 3-pin row,
  authored along x) claimed ±2 holes perpendicular to the row instead. The
  dims now orient by the DOMINANT axis of the local pin cloud (devboards with
  rows along y are unchanged), and the radial / SMD renderers read
  ``(along, across)`` through the same rule (new ``lead_axis_extents``), so
  geometry, render, and mask agree. SMD-0805/0603 masks shrink to their real
  pad-pair footprint as a side effect.

### Footprint-option `style:` now renders (2026-07-04)

A `footprints:` entry's `style:` (and the list-wide `footprint_style:`) was
parsed and stored on `FootprintOption.style` but never consumed. The body
dispatcher (`body_shape_for`) now matches the placed option — the instance's
`package_variant` address, or the default first entry — and layers its style
into the appearance resolution: instance override > footprint entry >
component `style:` > package colour > theme. An SMD chip option can be
near-black while the axial options of the same part keep the theme colour.

### Kind generators: pluggable registry, partial application, picker wiring (2026-07-04)

Package **kinds** (parameterized bodies — `package: {kind: pin_header, rows: 2}`)
are now generated through a pluggable registry mirroring the renderer registry:

* **Generator contract** (`domain/components/package/generators/`): a
  `PackageGenerator` subclass declares `generates = "<kind>"` (registration at
  class-creation, later wins), its parameter set as a `ParamSchema` (`params` —
  one source of truth for validation, the picker, and docs), an optional body
  `color`, and implements `generate(ctx) -> ctx`. The old hardcoded
  `_KIND_GEOMETRY` / `_KIND_COLOR` tables are gone; the builtin kinds
  (`dip_ic`/`wdip_ic`/`dip_switch`/`sil`/`pin_header`/`jst`) are now ordinary
  registered generators.
* **Driver** (`build_kind_package`): default-fill + required/type checks
  against the generator's schema, totality guard, `kind`/`lineage`/colour/
  schema stamping. Explicit-`null` parameter values are *holes*: the build
  substitutes the default, the hole is preserved on the body for the picker.
* **Partial application + redirects** (`ResidualKind`): a kind with missing
  required parameters loads as a generic-bbox placeholder carrying the pending
  kind + bound values (still generic for DRC/bend consumers); a generator may
  redirect to a *different* kind — complete redirects re-dispatch
  (cycle-guarded), incomplete ones surface to the picker.
* **Generator plugins**: `<library>/generators/*.py` auto-import at startup and
  preferences reload (`services/library/plugin_discovery.py` — the same
  machinery now also backs the renderer plugins).
* **Package-picker wired**: `DefaultFootprintChoiceProvider.kind_params` turns
  the body's stamped holes into input rows; `build_variant` re-runs the
  generator with the user's values and registers the concrete variant in the
  session catalogue (fixes a latent bug: an empty catalogue dict was silently
  replaced by a private copy).

### Refactor: radial silhouettes are per-shape, base renderer slimmed (2026-07-04)

`PackageRenderer` no longer type-branches on Disc/Cylinder/Prism inside its
radial machinery. `_draw_radial` keeps only what is shape-agnostic (lead
stubs + pad centroid) and delegates to a new `_radial_silhouette()` hook —
the base default is the bbox rounded-rect (prism/generic look). The cylinder
disc / D-shape / `-` segment moved to `CylinderRenderer`
(`_flat_side_world`, `_radial_disc`, `_d_shape`, `_disc_segment`); the
edge-mounted lens moved to `DiscRenderer` (`_radial_disc_side`).
`_radial_body_radius` follows the same pattern (bbox default, round-shape
overrides). Dead `_big_body_alpha` removed. Pure refactor — no visual change;
guarded by the existing renderer/parity suites.

### Fix: false BODY_OVERLAP for adjacent radial parts — Cylinder/Disc bbox centred (2026-07-03)

Two 2.5 mm LEDs placed side by side tripped DRC `BODY_OVERLAP`. Root cause:
`Cylinder.bbox()` (and `Disc.bbox()`) returned a `d×d` square **anchored at
pin 1** (`Rect(0, 0, d, d)`), while the physical (and rendered) radial body is
a disc centred on the **pad centroid** — the collision mask rasterised from
that bbox claimed a phantom row of holes below every radial part, colliding
with the neighbour's pins. `bbox()` is now computed from the lead geometry,
like `Prism` always did: centred on the pin centroid + `body_eccentricity`;
RADIAL cylinder → `d×d` around the centre, AXIAL cylinder → `L×d` oriented
along the pin axis (AABB), Disc → `diameter×thickness` lens along the axis.
Consequences: the led25 mask claims exactly its two pin holes (tangent
neighbours are legal); axial bodies (resistors) now honestly claim the
in-between holes their body lies over. Regression tests in
`test_package_mask.py::TestRadialCylinderMask`.

### Offset single-vector sugar implemented; LED family consolidated (2026-07-03)

Spec §2.3 always promised that a 2-pin package may write its one offset pair
as a bare vector (`body_offset: [1, 0]` ≡ `[[1, 0]]`), but `expand_offsets`
only implemented the scalar sugar — a bare vector aborted the whole family
file ("offset element must be a list"), which silently dropped the LED
families and made every LED fall back to the generic bbox **rectangle**
instead of the radial cylinder. The sugar now expands in `expand_offsets`
(one point for both offset domains; a top-level 2-list of numbers is
unambiguous) and the `schema` declarations accept the form. Library data:
LED sizes consolidated into one `LED` family (`led25` / `led30` / `led50`)
with a cathode flat side and translucent lens colours (`#rrggbbaa`).

### Body colours accept an alpha channel — CSS `#rrggbbaa` / `#rgba` (2026-07-03)

`style: "color: …"` (definition and instance) and the package-level `color:`
now accept 4- and 8-digit CSS hex colours with the alpha channel **last**
(`#ff000080` ≈ 50 % red — translucent LED lenses). The value is stored in the
authored CSS form; `qcolor_from_css` (`gui/renderers/packages`) reorders to
Qt's alpha-first form at the single QColor construction point. The declared
alpha **multiplies** with the layer alpha (ghosting / back-side dimming).
Previously an 8-digit colour failed validation and **aborted the whole library
load** (`SerializationError` on `leds.yaml`-style entries).

### TO-220 metal tab restored — as the reference renderer plugin (2026-07-03)

`library/renderers/to220.py` — `TO220Renderer(PrismRenderer)` with
`renders = "TO-220"` — draws the radial prism body via `super().render_body()`
and appends the light metal mounting tab (`theme.to220_tab_height_mm`), paying
off the "TO-220 metal tab render" debt from docs/todo.md. The builtin plugin
loads through the same `renderers/` discovery as user plugins (dogfooding).
The radial top-view branch moved from `draw()` into
`PackageRenderer.render_body()` (`_maybe_radial`), so subclasses extend any
body — radial included — with the plain `super().render_body()` + extra-items
pattern. Flipping the part flips the tab side (the pad axis carries the flip),
matching the physical package. `vertical_flipped`/family lineage covered by
`TestTO220Plugin`; legacy `TO92Body`/`TO220Body` deletion is now unblocked
(tracked in docs/todo.md).

### Fix: kalibracja kółka w armed-rotate (wheel-bounce) (2026-06-19)

Mysz emitująca kilka eventów `angleDelta` na jeden fizyczny step powodowała
przeskok rotacji o 180°/270° (każdy event = osobny krok 90°).
`ArmedRotate.on_wheel` **akumuluje** teraz deltę i kroki ±90° robi raz na pełny
notch (`_WHEEL_NOTCH=120`, standard Qt), przenosząc resztę. Jeden detent =
jeden krok niezależnie od liczby eventów; mocny obrót (np. 240) dalej kroku
proporcjonalnie; zmiana kierunku zrzuca stary carry. 2 testy (bounce 3×40→1
krok, hard-spin 240→2 kroki).

### Keybindingi: overlay z rejestru, kierunki jako akcje, fix przechwytywania (2026-06-19)

Trzy powiązane naprawy keybindingów:

* **Overlay `[?]` — sekcja Global z rejestru.** Była zahardkodowana lista ~9
  akcji ("Stage 1"); teraz `shortcuts_help.app_shortcut_sections` buduje ją z
  autorytatywnego `ALL_ACTIONS` + żywego `KeyBindings` (override'y usera),
  pogrupowane wg menu (File/Edit/Select/View/Analysis). Realne skróty
  (Save/Undo/DRC/zoom…) zamiast statycznej garstki.
* **Kierunki ruchu/obrotu jako akcje KeyMap** — strzałki/WSAD/R były literałami
  `Qt.Key.*` w `ArmedMove`/`ArmedRotate`. Teraz akcje `select-tool.action.step_*`
  z **podwójnym bindem** (`"Up|W"` itd.); KeyMap rozszerzony o `|`-alternatywy
  (`matches` sprawdza każdą, `resolve_display`→`"Up / W"`). Sub-toole dostają
  wstrzyknięty matcher i dispatchują po action-id — zero literałów, przebindowalne
  ([[every-action-has-keybinding]]).
* **Fix: WSAD przełączał tryby / strzałki scrollowały.** Armed-tryb nie wetował
  globalnych skrótów (S=Select, W=Route…) ani nie wyprzedzał arrow-scrolla.
  `wants_key` przejmuje teraz `_ARMED_ACTION_IDS` gdy uzbrojone (po action-id);
  `keyPressEvent` daje uzbrojonemu toolowi arrow PRZED scrollem (gated `wants_key`).
* Testy: armed-move veto (S/WASD/arrows claimed tylko gdy armed, released po
  disarm), overlay-pokazuje-realne-skróty, `KeyMap` multi-bind. 896 GUI green;
  4 zmienione moduły czyste pod `mypy --strict`.

### Wizualny feedback armed-rotate (2026-06-19)

Armed-rotate nie dawał sygnału, że trwa: obrót nie był widać, brak markera
„wokół czego", konflikty tylko czasem. Naprawione:

* **Obrót renderuje się** — `_rotate_preview` woła `view.refresh()` (re-render
  ciał w nowej orientacji) zamiast translacyjnego glide `drag_moved`, który
  ruszał tylko pozycję (per-object pivot = dx/dy 0 → ciało wyglądało
  nieobrócone). Move dalej używa taniego glide (czysta translacja).
* **Marker pivota** (`select_color` pomarańcz `⊕`) — pokazuje WOKÓŁ CZEGO się
  obraca: shared pivot (klik pada) = jeden duży marker na padzie; per-object =
  mały marker na środku każdego obracanego obiektu. Rysowany od uzbrojenia,
  re-drawn po każdym refreshu, czyszczony przy disarm + scene-clear.
* **Konflikty** — corner-text amber (SOFT) / red (HARD) wyciągnięty do
  współdzielonego `_show_conflict_corner` (drag + rotate identycznie).
* 1 test (`test_pivot_markers_shown_while_armed_cleared_on_disarm`); 894 GUI
  green; `ArmedRotate.comp_ids` property dla overlayu.

### Atomowe `[arm, action, disarm]` caller-API (2026-06-19)

`SelectTool._invoke_armed_atomic(arm, action)` — programmatyczna forma kształtu
„uzbrój → akcja → rozbrój" pod przyszłe menu/makra. *arm* wchodzi w sub-tool,
*action* robi jedną operację na nim (krok/flip przez publiczne wejście
protokołu), potem `accept()`.

* **Conditional-disarm honorowany**: bo `accept()` commituje tylko gdy
  `can_commit()`, operacja programowa na konfliktowej selekcji ZOSTAWIA tryb
  uzbrojony (zwraca False) zamiast forsować nielegalny commit; czysta operacja
  commituje + rozbraja (zwraca True). No-op gdy *arm* nic nie uzbroił (pusta
  selekcja). Cudza uzbrojona sesja jest najpierw anulowana.
* Helper-only — żadnych call-site'ów menu/makr (fundament pod nie). Etap 6
  z `docs/armed-rotate-and-subtool-delegation.md`.
* 3 testy (`TestArmedAtomic`: commit+disarm, no-op-empty, park-on-conflict);
  893 GUI green; czyste pod `mypy --strict`.

### Wire fragment-flip jako op-table wire-edit (2026-06-19)

`[F]` w wire-edit flipuje zaznaczony fragment druta na przeciwną warstwę —
dołącza do reroute `[W]` / autoroute `[A]` / delete `[Del]` jako kolejna
operacja na fragmencie (domyka stub `_flip_wire_fragment`, etap 4).

* `wire_edit.build_flip_fragment_command` (Qt-free, wzorem `build_delete_command`)
  owija `FlipWireFragmentCommand`; same-wire only — selekcja cross-wire nie ma
  jednego druta do flipu → fallback na whole-wire layer move.
* Konduktor zostaje jednym `Wire` (granice fragmentu = vias), zachowuje id +
  knoty. Akcja `select-tool.action.flip_fragment` (`F`), przebindowalna, w
  vecie `wants_key` i op-table; status-bar fragmentu pokazuje `[F] Flip layer`.
* 1 test (`test_f_flips_fragment_to_other_layer`); 890 GUI green; `wire_edit.py`
  czysty pod `mypy --strict`.

### ArmedSubTool protocol + conditional-disarm (2026-06-19)

Tryby uzbrajane (rotate `[R]` / move `[G]`) zunifikowane za jednym protokołem
zamiast rozgałęzień po „który tryb aktywny".

* **`gui/selection/armed.py`** — `ArmedSubTool` Protocol: `on_key`/`on_wheel`/
  `on_click` → `can_commit` → `accept`/`cancel` + `finished`. SelectTool trzyma
  jedno `self._armed` i dispatchuje jednolicie (znikły `_rotate_arm`,
  `_move_arm_active` i równoległe `_rotate_arm_on_key`/`_move_arm_on_key`).
  `ArmedRotate` (`armed_rotate.py`) i `ArmedMove` (`armed_move.py`) implementują
  protokół. Verdict polimorfizmu (skill `consider-polymorphism`): wspólny
  interfejs, bo tryby dzielą kształt arm→step→commit/cancel.
* **Conditional-disarm** (wymóg, który wymusił protokół): `accept()` commituje
  i kończy **tylko gdy `can_commit()`**; w konflikcie tryb ZOSTAJE uzbrojony i
  czeka (przywraca zachowanie sprzed refaktoru). Rotate jest teraz
  **preview-then-commit**: kroki podglądają poza undo-stackiem, `[Enter]`
  commituje JEDEN obrót o sumę (jeden wpis undo); obrót lądujący w jakimkolwiek
  konflikcie (SOFT/HARD) parkuje zamiast auto-commitu + usuwania drutów. Move
  owija `DragController` (jego FLOATING-on-HARD to już ten kontrakt).
* Zakres: rotate + move wciągnięte; wire-edit i redrag świadomie ad-hoc (luźniej
  pasują — fold-in później). `DragController.nudge_to` snapuje `_floating_orig`
  na pierwszym kroku, by keyboard-move commitował (nie ma release'u).
* 9 testów (`TestArmedRotate` ×6 w tym conditional-disarm, `TestArmedMove` ×3);
  2900 testów green; 3 nowe moduły czyste pod `mypy --strict`.

### Tryby uzbrajane: float-rotate [R] + keyboard float-move [G] (2026-06-19)

Rotacja i przesuwanie zaznaczenia działają teraz jak wire-edit (`[E]`) —
tryby *uzbrajane*, nie jednorazowe komendy.

* **`[R]` uzbraja float-rotate.** Selekcja (komponent / grupa / mix) zostaje
  „żywa": kółko / `[↑]` / `[↓]` / `[R]` krokują ±90° (każdy krok to realna,
  cofalna komenda — `_rotate_multi`/`_apply_rotate`, więc kolidujące druty są
  czyszczone). Domyślnie każdy obiekt obraca się wokół własnego środka; **klik
  w pad = wspólny pivot dla całego zaznaczenia** (`RotateAroundPointCommand`),
  ponowny klik resetuje do per-obiekt. `[Enter]` zatwierdza, `[Esc]` anuluje
  CAŁĄ sesję (cofa wszystkie kroki do undo-floor z chwili uzbrojenia). Stare
  natychmiastowe `R = obróć 90°` zniknęło; `Alt+R` (flip 2-pin) i `Ctrl+R`
  (re-drag) bez zmian. Legacy `begin_rotate_around_point` zasila wspólny pivot.
* **`[G]` uzbraja keyboard float-move** („grab"). Przesuwanie bez myszy:
  strzałki / WASD krokują kotwicę o jedną komórkę siatki. Reużywa CAŁY silnik
  `DragController` (snap / klasyfikacja konfliktów / live preview) przez nowe
  `DragController.nudge_to`. `[Enter]` upuszcza (commit), `[Esc]` anuluje.
* **Wheel-seam:** `_Tool.wants_wheel`/`on_wheel` konsultowane przez
  `BoardView.wheelEvent` PRZED zoomem — domyślnie off, więc zoom bez zmian;
  tylko uzbrojony float-rotate przejmuje kółko.
* Akcje namespaced w KeyMap (`select-tool.action.rotate`/`.move`/`.flip`),
  przebindowalne, widoczne w nakładce `[?]` i menu PPM (Rotate R / Move G /
  Flip Alt+R).
* 8 testów (`TestArmedRotate` ×5, `TestArmedMove` ×3); 2 testy `TestRotate`
  zaktualizowane do nowej semantyki arm-then-step. 2899 testów green.
* Design + staging: `docs/armed-rotate-and-subtool-delegation.md` (etapy 1-3
  DONE; ekstrakcja `ArmedSubTool` protocol + atomic caller-API dalej odłożone).

### Fix: [?] nie otwierał nakładki skrótów (2026-06-19)

`[?]` (overlay z helpem skrótów) nie reagował. Przyczyna w `KeyMap.matches`:
`?` to na większości układów `Shift+/` → Qt dostarcza `Key_Question` **z**
`ShiftModifier`, ale kanoniczna postać bindingu (`"?"`) ma shift wpieczony w
keycode i nie niesie osobnej flagi Shift — więc `event_to_sequence` dawał
`"Shift+?"`, co nie równa się `"?"`. Ogólny bug dla każdego symbolu pod
Shiftem (`?`, `:`, `+`…), nie tylko `?`.

* Fallback w `matches`: gdy klawisz jest **symbolem** (`_is_symbol_key` —
  keycode nie jest literą/cyfrą) a binding nie ma jawnej flagi Shift, próbuj
  dopasowania z usuniętym Shiftem. Bramka na symbol gwarantuje, że
  `Shift+<litera>` (np. `Shift+R` = rotate-CCW) NIE jest mylone z gołym
  klawiszem (`R` = rotate-CW) — dla liter Shift jest dodatkowym modyfikatorem.
* `@traced` na `KeyMap.matches` + nakładce (`_toggle/_show/_hide_shortcuts_overlay`)
  — gotowa diagnoza ścieżki klawisz→overlay (zero-overhead off).
* Testy: `tests/gui/test_keymap.py` (matcher matrix) + regresja w
  `test_board_view.py` driająca prawdziwy `keyPressEvent(Shift+Key_Question)`
  (stary test wołał tylko `_toggle…` bezpośrednio — dlatego bug przeszedł).

### Wire-edit jako akcja menu (2026-06-19)

Operacja wire-edit dostała wpis w pasku menu **Edit ► Design ► Wire ►
Edit Path**, domykając ostatnią lukę zadania „wire-edit w rejestrze akcji".
Po refaktorze namespaced-KeyMap (keybindings #1–#6) klawisz `[E]` nie jest
już zahardkodowany — to akcja `select-tool.action.edit` (dziedziczy globalny
`E`), przebindowalna, widoczna w nakładce `[?]` i menu PPM. Pozostawała tylko
powierzchnia paska menu.

* **Brak drugiego bindingu**: nowa akcja `ACTION_WIRE_EDIT` ma domyślny skrót
  pusty — `build_actions` nie tworzy dla niej `QShortcut`, więc żywy `[E]`
  należy wyłącznie do namespaced-KeyMap (zero konkurencyjnych bindów na jedną
  operację — zgodnie z wizją „bez równoległej maszynerii").
* Slot `MainWindow._wire_edit_action` deleguje do publicznego
  `SelectTool.arm_wire_edit()` — jedno zachowanie, dwie powierzchnie
  (klawisz + menu), wzorem `_exit_group` / `_redrag_pins`.
* Spełnia [[every-action-has-keybinding]]: pozycja menu ma `action-id`
  (skrót może być pusty).

### SelectTool przepisany na serwis (2026-06-18)

Monolit `gui/tools/select_tool.py` (9568 LoC, 195 metod, 60 pól w jednej
klasie — selekcja + highlight + drag + wire-edit + rotate + group-nav +
rubber-band + net-select naraz) zastąpiony **serwisowym** SelectTool
(2400 LoC). Wizja: tool jest serwisem — pasywnie „co zaznaczone/pod
kursorem", aktywnie **pick** (`request_pick(what, stop)`); klienci proszą,
nie reimplementują pętli klików.

* **Nowe pakiety** `gui/selection/` (Qt-free, headless-testowalne):
  `selectable.py` (Protocol + adaptery — wchłania dawny `Hit.kind` switch),
  `service.py` (`SelectionService` + pick-API: `Stop` count/confirm/until,
  `on_progress`, async-fasada), `hit_test.py` (`HitTester`), `queries.py`
  (oś relacji sieci), `wire_edit.py` / `wire_ops.py` (buildery + worker-prep),
  `mouse_input.py` + kontrolery (drag / rubber-band / point-pick).
  `gui/highlight/controller.py` — highlight jako osobny serwis.
* **6 faz** (Selectable+pick → HighlightController → tool MVP → drag/floating
  → wire-edit → net-select), potem swap + kasacja monolitu jednym cięciem.
* `mypy --strict` 503 → 385 (monolit niósł ~118 błędów baseline).
* Odłożone (`docs/todo.md`): egzekwowanie filtra selekcji w hit-teście,
  flip fragmentu wire jako wpis op-table.

### Parametryczny model package — migracja TwoPinBodySpec → package (2026-06-15)

Pełne zastąpienie render-only modelu body 2-pin (`TwoPinBodySpec` /
`TwoPinBodyPackage`) parametrycznym, geometrycznie-prawdziwym modelem
`Package` (`docs/footprints/spec.md`). Powód architektoniczny: stary typ
przeciekał do ~24 plików (renderery, exportery, picker, persistence) — objaw
geometrii żyjącej *poza* komponentem; nowy model to `Protocol` (geometrię
liczy kod), a komponent niesie własny package.

* **Domknięcie modelu package**: presentation na shape'ach
  (`color`/`edge`/`arrangement`), shape `Disc`, `min_radial_pitch_mm` /
  `min_axial_pitch_mm`. **Bez abstrakcji `Material`** — body niesie kolor
  (`#rrggbb`) wprost, renderer wyprowadza obramowanie przez przyciemnienie.
* **Renderer**: nowy `PackageBody` rysuje body z geometrii pakietu
  (cylinder→kapsuła side-view, prism/disc→bbox), z pełnym parytetem 2-pin
  starego renderera (cathode stripe diod, radial top-view disc,
  polarność z `component_class`/`vertical_flipped` — nie z pakietu).
* **Katalog wariantów**: `PackageFamily` (rodzina + warianty + default,
  adresowanie `"family/variant"`); 8 plików `assets/body_types/*.yaml`
  przeniesione do `assets/package_families/*.yaml` budujących realne pakiety
  (`material`→kolor, `lead_diameter_mm`→`lead_r`, leady przez `extends`
  generyków). Loader `load_package_families`.
* **Referencje na komponencie/instancji**: `ComponentDefinition.
  package_family_ref` + `ComponentInstance.package_variant`; resolver
  `resolve_component_package` (precedencja: instance → definition ref →
  inline `package` → None; stale-ref fall-through, bez crasha).
* **Picker**: dropdown body w Properties i picker place-time w bibliotece
  czytają `PackageFamily`; `SetComponentPackageVariantCommand` (undo/redo);
  `package_variant` round-trip w persistence + recording.

**Usunięte (legacy):** `domain/components/two_pin_body.py`,
`services/library/two_pin_body_loader.py`, `assets/body_types/`,
`TwoPinParametricBody` + `_resolve_two_pin_spec`,
`SetComponentBodyVariantCommand`, pola `default_body_variant` /
`body_variant`, threading `two_pin_body_families` (theme/session/exportery).
Stare projekty z kluczem `body_variant` ładują się (klucz ignorowany —
użytkownik wybiera body z katalogu pakietów).

### Unified drag model — atomic delta + preference-driven soft resolution (2026-06-02)

Pełen refactor drag-pipeline naprawiający silent cross-group wire data
loss opisany w `docs/bug-drag-skewed-wire-shorts.md`.

* **5-way wire classification** (`domain/layout/drag_classification.py`):
  per drag set wires partycjonowane na `internal` / `cross_group_disconnect`
  / `half_orphan_internal` / `half_orphan_external` / `external`. Net+pin
  oparta, bez geometrycznych heurystyk.
* **MoveDragSetCommand** (`domain/commands/drag_cmds.py`): atomic single-pass
  delta translation; zastępuje macro
  `[TranslateAnchoredWires + MoveComponent(placement_only=True) + RemoveWire×N]`
  które po drodze produkowało intermediate skewed wire states (źródło
  silent drop).
* **Preference-driven soft resolution**: 3 kategorie konfliktu (free wire
  / stationary group wire / cross-group disconnect) × 5 wartości
  (auto-remove / ask-remove / auto-reroute / ask-reroute / block).
  Domyślne wartości konserwatywne (preserve existing wires).
* **Dialogi z opt-out**: `DragDisconnectDialog` (Reroute/Drop/Cancel) i
  `DragAskRemoveDialog` (Yes/Cancel) z checkboxem "Don't ask again for
  this session" → `DragSessionOverrides` cache.
* **Live drag ghosting**: drop wires renderowane z opacity 0.3, cross-group
  disconnect z 0.5, normal z 1.0 (`BoardView.set_wire_drag_state`).
* **`ComponentGroup.lock_internal_wires` field**: opt-in promocja
  soft conflicts na HARD gdy foreign drag dotyka internal wires
  zabezpieczonej grupy.
* **PreferencesDialog "Drag" tab**: 3 combo-boxes + checkbox lock-default.

Legacy `MoveComponentCommand(placement_only=False)` +
`TranslateAnchoredWiresCommand` + `_update_wires_for_move` pozostają
jako deprecated shim dla back-compat z command recordings starszymi
niż 2026-06-02.

### Endpoint-merge at load and save (2026-05-28)

- New pure helpers ``wire_utils.merge_colocated_joints(layout)`` and
  ``wire_utils.merge_endpoint_wires(layout, board)``.  The first
  coalesces ``JointKnot``s that sit on the same pad — autoroute /
  legacy load paths sometimes leave two degree-1 joints at one pad
  instead of one shared degree-2 joint, hiding fusable wire chains
  from ``try_collapse_joint``.  The second then runs
  ``try_collapse_joint`` until every same-``insulated`` degree-2 joint
  has been resolved (chains of N wires need N-1 passes).
- ``load_project`` calls ``merge_endpoint_wires`` after the normalize
  pass — legacy projects automatically migrate to the canonical "one
  continuous artifact = one Wire" representation.
- ``save_project`` calls ``merge_endpoint_wires`` before encoding so
  the on-disk YAML is always canonical — saved files never represent
  the same physical artifact as multiple ``Wire`` instances.
- The micek project (255 wires, 0 multi-fragment, 28 degree-1 joints
  before) loads as 242 wires (13 fused), 7 multi-fragment wires for
  the real cross-side vias.

### Endpoint-merge in AddWireCommand (2026-05-28)

- ``AddWireCommand`` / ``AddWireWithNetsCommand`` now fuse the new wire
  with a neighbouring wire when their endpoints meet on an **off-pin
  pad** and share ``insulated``.  Previously each apply created a
  separate ``JointKnot`` per wire at the meeting pad — two wires sat
  end-to-end at the same coordinate but the underlying model held them
  as distinct ``Wire`` instances with degree-1 joints, contradicting
  the "one continuous solderable artifact = one Wire" intent.
- ``_resolve_endpoint_knot`` now reuses an existing ``JointKnot`` at the
  endpoint position when no pin is there, so the shared joint exists
  before ``add_wire`` is called.
- After insertion, ``_collapse_endpoint_joints`` runs
  ``Layout.try_collapse_joint`` on each endpoint joint: when degree=2
  and both wires share ``insulated``, the two wires fuse.  Same-side
  meetings collapse into one single-fragment Wire; cross-side meetings
  on a plated-through pad collapse into one multi-fragment Wire (via
  boundary at the pad).  ``insulated`` mismatch and pin endpoints
  prevent fusion — those remain as separate artifacts.
- Revert restores the pre-collapse state from snapshots
  (``_WireSnapshot`` per source wire + joint position), including any
  outer ``JointKnot``s that ``remove_wire`` pruned as orphans.

### Merge same-side fragments command (2026-05-27)

- New pure helper ``wire_utils.coalesce_same_side_fragments(fragments)``
  fuses consecutive fragments that share ``side`` into one fragment,
  preserving real via boundaries (top↔bottom).  Operates only on
  ``side``; ``insulated`` is per-wire and not consulted.
- Wired up as a user-triggered command ``ACTION_MERGE_WIRE_FRAGMENTS``
  ("Merge Same-Side Fragments"): appears in the wire context menu when
  any selected wire has ≥2 fragments, dispatched through main-window
  shortcut, and locked alongside other hard-mutate actions during
  autoroute.  Implementation calls ``coalesce_same_side_fragments`` on
  each selected wire and packs the resulting
  ``ReplaceWireFragmentsCommand``s into a ``MacroCommand`` (single
  undo step).  Wires with no adjacent same-side runs are silently
  skipped.
- The helper is **never** invoked automatically — domain constructors,
  load/save paths, and commit pipelines stay transparent.  Fusion only
  happens when the user explicitly asks for it.

### Wire-edit excise endpoint crash fix (2026-05-27)

- ``SelectTool._excise_cross_wire_range`` (and the matching single-wire
  branch in ``_commit_wire_edit``) previously used
  ``_replace_wire_path_cmd`` to shrink the kept fragment of a start /
  end wire when excising a chain.  The shrink moves the wire's *inner*
  endpoint to ``path[edit_idx]``, but the wire's pre-existing inner knot
  still points at the original endpoint — ``Layout.replace_wire_fragments``
  refused to move a ``PinKnot`` (or a shared ``JointKnot``) and raised
  ``ValueError`` mid-commit, leaving a half-applied macro and crashing
  the Qt event loop.  Replaced with ``RemoveWireCommand`` +
  ``AddWireCommand`` (fresh wire, fresh endpoint knots) so the new
  endpoints get resolved cleanly.

### Wire-edit ready-for-edit helper (2026-05-27)

- ``_ensure_wire_ready_for_edit`` (SelectTool) now enriches insulated
  wires as well as bare ones, and considers pads from **both** board
  sides so a wire passing geometrically over a pad on the opposite
  layer still gains a vertex handle.  Brings the on-edit helper in line
  with AddWireCommand / RouteTool which already enrich on insert.

### Unweave honours allow_layer_change (2026-05-28)

- `unweave_wires` gained an ``allow_layer_change`` kwarg that gates the
  side-changing branch of the swap algorithm.  When False, every candidate
  swap that would move a fragment onto the other board side is rejected
  via the pad-safety predicate and the fallback single-side glue is used;
  output wires never grow new top/bottom transitions.  ``SelectTool``
  threads ``session.routing_settings.allow_layer_change`` into the call so
  the user toggle takes effect for the manual unweave action too.

### Autoroute memory + off-board precondition (2026-05-27)

- A* (``astar.py``) previously carried the full ``list[PathStep]`` in
  every heap entry and grew it with ``path + [step]`` on each neighbour
  push.  On dense boards (80×60 plated-through perfboard) with no
  detour budget (``max_detour_factor=0.0``) the heap stored hundreds of
  thousands of entries × path length, pushing RSS past 10 GB on a
  single VCC route.  Replaced with a singly-linked-list path chain
  (one 2-tuple ``(parent, step)`` per push); shared prefixes coalesce
  in memory, ``_unfold_path_chain`` materialises the list only at goal
  / trace / rollback time.  Same routes produced; large drop in peak
  RSS on hard searches.
- New autoroute precondition: ``OFF_BOARD_PIN``.  Any pin in the
  schematic whose world position is not on the board grid raises
  ``AutoroutePreconditionError`` before A* runs.  An off-board pin has
  no valid node in the routing graph, so without an early bail-out
  A* explores the whole board attempting to reach an unreachable
  target — the same path-as-list explosion above.  Error message
  names the component / pin / net and tells the user to move the
  component back onto the board.

### Wire model refactor — Knot / Fragment domain (2026-05-27)

Two-commit refactor that introduces a richer wire model alongside the legacy
`WireSegment` list and fixes the segment-proliferation bug (d33ea56) by
preserving wire identity across all path-edit operations.

**Commit A — foundations (121d7b1):**
- New domain types in `src/protoboard/domain/layout/`:
  - `wire.py`: `Wire` (frozen, identity by id) + `Fragment` (same-side
    sub-path) + `KnotRef`.  Legacy `WireSegment` kept as a compat shim used
    by the v1 loader; removal scheduled for a future major.
  - `knot.py`: `Knot` protocol + `PinKnot` (anchored to `PinRef`, position
    resolved lazily) + `JointKnot` (free t-joint at a pad).
- `Layout` gained `knots`, `pin_knots`, `wires_v2` storage plus mutators
  that own the back-ref invariant: `get_or_create_pin_knot`,
  `create_joint_knot`, `add_wire_v2`, `remove_wire_v2`,
  `replace_wire_fragments` (preserves id — the headline fix),
  `try_collapse_joint` (same `insulated` AND (same `side` OR
  plated-through via)).
- Project YAML bumped to `format_version: 2`; legacy v1 files load
  unchanged.  New `services/persistence/wire_migration.py` rebuilds
  `wires_v2` + `knots` from legacy segments via iterative joint collapse.

**Commit B — consumers + identity preservation (6685177):**
- `ConnectivityGraph` gained `add_wire_v2(Wire) / remove_wire_v2(Wire)`
  that iterate fragments and aggregate merges into a single `WireRecord`
  per wire.id; plated-through transitions carry end-to-end electrical
  continuity through one record.
- `gui/renderers/wire_renderer.py`: `update_wire_groups_v2 /
  _build_wire_group_v2` render one `QGraphicsItemGroup` per Wire with
  per-fragment sub-items and via markers at side transitions; new theme
  keys `via_marker_color` / `via_marker_radius` (default fallbacks).
- `services/drc/engine.py`: `_check_off_pad_joint` raises `OFF_PAD_JOINT`
  errors for any `JointKnot` whose position is not on a board pad —
  enforces the §4 plan invariant.
- `services/routing/{autorouter,postprocess}.py`: `straighten_wire`,
  `corner_cut_wire`, `generic_simplify_wire`, `_nudge_wire`,
  `_try_mid_cell_reroute` now preserve `wire.id` across path edits.
- `services/routing/unweave.py`: `_from_logical_path` gained
  `seed_ids=[…]` so segment reconstruction reuses input ids in emission
  order; `_remerge` keeps the first segment's id when fusing;
  `_swap_fragment_between` preserves outer/middle ids; `_rebalance_sides`
  no longer mints fresh ids on attribute edits.
- New v2 commands in `domain/commands/component_cmds.py`:
  `AddWireV2Command`, `RemoveWireV2Command` (snapshots orphan joints for
  revert), `ReplaceWireFragmentsCommand` (id-preserving atomic
  fragment-list replacement).

Tests: 34 new unit tests in `test_wire_v2_model.py`,
`test_wire_v2_commands.py`, `test_unweave_id_preservation.py`.  Full
suite: 1256 passed, 61 skipped (was 1222 passed before refactor).
mypy --strict clean on all touched files (only pre-existing
pad_conflict.py + component_cmds.py legacy errors remain).

Plan: `~/.claude/plans/peppy-puzzling-hoare.md`.

### Phase 6 — Full DRC + Exports
- Added `FLOATING_PIN` (warning) and `BARE_WIRE_CROSS` (error) DRC checks
- SVG exporter (`services/export/svg.py`) — top-layer render: pads, wires, component outlines with refdes labels
- BOM CSV exporter (`services/export/bom.py`) — RefDes/Value/Footprint/Description, sorted
- Toolbar actions: Export SVG, Export BOM, Import KiCAD .net, Export KiCAD .net

### Phase 5 — KiCAD I/O
- `KiCadNetlistImporter` — parses `<export version="D">` XML netlist to `Schematic`
- Footprint heuristic: DIP-N, R_Axial (SIL-2), TO-92, PinHeader_MxN; unknown → `ImportWarning`
- `KiCadNetlistExporter` — serialises `Schematic` back to KiCAD XML; roundtrip preserves net names and pin counts
- 3 fixture netlists: `voltage_divider.net`, `minimal.net`, `unknown_footprint.net`

### Phase 4 — Routing Assistant
- `RoutingCostModel` — configurable per-step weights (free_pad, same_net_pad, occupied, direction_change)
- A* router on pad graph: 4-connectivity, top-K paths via edge-forbidding, Manhattan heuristic
- Route Wire tool shows top-3 hint paths as dashed green/yellow/orange overlay on first click
- `render_routing_hints()` overlay renderer

### GUI Bug Fixes (post Phase 3)
- **BUG 1**: `RouteTool._commit()` — preview lines were not cleared before emitting `layout_changed`; wire appeared to vanish after commit
- **BUG 2**: `CutTool` — position index was always wrong (`pos.x - pos.x == 0`); double `refresh()` call removed; strip geometry now correctly computed from center
- **BUG 3**: `SelectTool.deactivate()` — `_dragging` and `_drag_orig_pos` were not reset, causing stale drag state on reactivation
- **BUG 4/5**: `SelectTool.on_scene_cleared()` — now preserves `_selected_id` so in-progress drag can commit; highlight is redrawn after drag release
- **BUG 6**: `PlaceTool` undo — `session.instances` was mutated outside the command; moved to `PlaceComponentCommand` via `ComponentState` Protocol so undo removes the instance too
- `ComponentState` Protocol introduced in `component_cmds.py` — decouples commands from GUI layer
- 65 new `pytest-qt` GUI tests in `tests/gui/`; `tests/conftest.py` with shared `session` and `board_view` fixtures

### Phase 3 — GUI MVP
- [x] `pyproject.toml`, `uv`, `ruff`, `black`, `mypy --strict`, `pytest`, pre-commit hooks
- [x] Directory structure
- [x] CI: GitHub Actions (lint + mypy + pytest)
- [x] SPDX headers, `LICENSE` (MIT), `README.md` skeleton
- [x] `CHANGELOG.md` (Keep a Changelog)

### Phase 3 — GUI MVP
- [x] MainWindow with library dock, properties+DRC dock, toolbar, status bar
- [x] BoardView (QGraphicsView) — PCB-green background, gold pads, zoom/pan
- [x] Snap to nearest pad atom center
- [x] Tools: Select (S), Place (double-click library, R=rotate), Route Wire (W,
      multi-segment, Enter commit/Esc cancel), Cut Strip (C)
- [x] Properties panel — editable refdes/value/rotation per component
- [x] Undo/Redo Ctrl+Z/Y
- [x] Save/Load project folder (board.yaml + layout.yaml + schematic.yaml)
- [x] DRC panel updated on F5
- [x] Built-in component library (passive, opamps, transistors)

### Phase 2 — Components, Schematic, Layout, Connectivity, DRC, Commands
- [x] Schematic: PinRef, Net, Schematic with pins_for_component / net_for_pin
- [x] Physical layout: ComponentPlacement (4 rotations), WireSegment, StripCut, Layout
- [x] pin_world_position() with rotation support (0/90/180/270° CW)
- [x] ConnectivityGraph (networkx): pads → nodes, wires → edges, plated-through merge
- [x] DRCEngine: SHORT, MISSING_NET, OVERLAP checks
- [x] CommandStack (generic, TDD) + PlaceComponent / RemoveComponent commands
- [x] 309 tests, mypy --strict clean

### Phase 1 — Domain Model + Box Layout
- [x] Failing tests written first (TDD): units, geometry, pads, boxes,
      layout engine (intrinsic, layout, FILL, margins, errors, property-based),
      Board, YAML serializer, perfboard/stripboard 10×10 fixtures
- [x] `units.py` (RasterUnit, Millimeter, conversions), `geometry.py` (Point,
      Size, Insets, Rect)
- [x] `errors.py` (ProtoboardError, LayoutError, SerializationError)
- [x] Box model (`HBox`, `VBox`, `BoardGrid`, `PadAtom`, `StripAtom`, `GapAtom`)
      — frozen dataclasses with full validation
- [x] Two-pass layout engine: intrinsic-size + layout (positions = atom centers)
- [x] FILL distribution with single-FILL-per-axis rule and bounded-parent
      requirement
- [x] `Board` wrapper with cached layout, `atoms()` iterator, `bounds`, `size_mm`
- [x] YAML serializer/deserializer for boards (round-trip preserved)
- [x] 181 unit + property-based tests, all green
