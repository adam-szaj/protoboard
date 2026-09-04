# Test-suite audit

## Scope and baseline

This audit treats a test as valuable when it would fail after an accidental
change to a user-visible result or to a contract between application layers.
It treats assertions on private state, exact tuning constants, and framework
mechanics as change detectors unless that detail is itself a documented
contract.

Baseline on branch `test-suite-audit`, from `domain-model-reset`:

- collection: 4,485 tests (4,481 selected by the default markers);
- unit suite: 2,890 passed, 68 skipped, 1 deselected;
- GUI suite: 1,509 passed, 1 xfailed, 1 warning;
- unit + GUI coverage: 80% overall.

The worktree contains pre-existing untracked files, including
`src/protoboard/gui/scene_items.py`, `tests/gui/test_scene_items.py`, and
`worktree/`; they are outside this audit.

## Keep: high-value contracts

The strongest part of the suite is the domain and application contract layer:

- connectivity and DRC assert electrical invariants rather than data
  structure;
- wire creation, splitting, merging, insulation, sides, and net binding are
  tested through resulting layout and graph state;
- commands test apply/revert/redo semantics;
- persistence tests use save/load boundaries and legacy input;
- KiCad import/export tests assert semantic nets, pins, and valid output;
- headless CLI journeys exercise the same controller verbs used by the GUI;
- selection synchronisation and port conformance tests protect module seams.

These tests should be preferred when a lower-level test and a contract test
cover the same behavior.

## Remove or rewrite candidates

The following patterns are coupled to implementation details:

- `tests/gui/test_board_view.py`: refresh timers, display-list caches, and
  private scene-group dictionaries;
- `tests/gui/test_place_tool.py`: `_phase`, `_current_drag_pin`, and private
  preview calculations;
- `tests/gui/test_simplify_tool.py`: `_draft_path` and `_draft_wire_id`;
- `tests/gui/test_nets_editor_panel.py`: private Qt tree widgets and private
  handlers;
- `tests/gui/test_main_window_shortcuts.py`: private QAction maps and handler
  objects;
- `tests/gui/test_wire_edit_tool.py`: private armed/edit state;
- `tests/unit/test_call_counters.py`: the diagnostic instrumentation API,
  rather than an application contract.

These are not all safe to delete immediately: some encode state-machine
transitions that have no equivalent public read model yet. The safer migration
is to replace them with headless journeys and observable session/layout,
schematic, notification, and command-stack outcomes as those seams exist.

This pass removes the standalone unit performance-regression tests that
asserted private cache identity and internal call counts, plus GUI tests that
only inspected background-cache/display-list internals. The remaining
performance benchmarks stay in `tests/benchmarks/`, where timing and cache
behaviour are explicitly non-functional measurements.

## Configuration tests

Exact defaults are retained only when they define a user-facing compatibility
or persistence contract. Tests that merely pin tuning choices are removed in
this pass. The useful configuration tests are:

- custom value round-trip;
- missing or legacy sections using a valid fallback;
- malformed input validation/clamping;
- a setting changing an observable result, such as wire defaults or rendered
  axis labels.

## Functional gaps

Coverage identifies gaps, but coverage percentage is not a quality target by
itself. The most relevant uncovered behavior is in:

- `main_window.py` and `preferences_dialog.py`;
- project wire migration;
- hierarchical routing;
- several dialogs with no direct coverage;
- failure/cancellation branches of the large autorouter.

The highest-value additions are end-to-end scenarios, not line-by-line tests:

1. create/place/connect → DRC → save → load → export;
2. failed autoroute leaves layout and schematic unchanged;
3. group/lock/edit/undo/redo preserves ownership and connectivity;
4. old project files migrate to the current wire representation;
5. GUI smoke tests drive public actions and assert application outcomes.

## Recommended test architecture

Use four layers:

1. **Domain properties** — pure invariants for geometry, connectivity, DRC,
   routing and command reversibility.
2. **Application contracts** — `HeadlessSession`, `EditController`, ports and
   persistence boundaries; these are the primary interaction tests.
3. **Small GUI smoke suite** — real clicks/keys through public actions, with
   no assertions on widget layout or private controller fields unless the
   field is deliberately promoted to a contract.
4. **Compatibility/performance** — legacy YAML fixtures, selected property
   tests and benchmarks kept separate from functional correctness.

The suite should be organised by user capability and invariant, not by every
private class. A feature matrix in the test documentation should link each
important user journey to at least one application-contract test and one
relevant domain invariant.
