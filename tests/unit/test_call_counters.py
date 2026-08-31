# SPDX-License-Identifier: MIT
"""Tests for the @counted decorator and runtime control surface."""

from __future__ import annotations

import pytest

from protoboard.instrumentation.call_counters import (
    counted,
    counters_enabled,
    current_path,
    delta,
    disable,
    enable,
    fresh_counters,
    get_max_depth,
    get_path_filter,
    is_enabled,
    reset,
    rollup,
    set_max_depth,
    set_path_filter,
    snapshot,
)


@pytest.fixture(autouse=True)
def _clean_state():
    """Each test starts from a known-clean state."""
    prev = is_enabled()
    reset()
    yield
    if prev:
        enable()
    else:
        disable()
    reset()


class TestDecoratorBasic:
    def test_disabled_decorator_does_not_count(self) -> None:
        @counted("noop")
        def f(x: int) -> int:
            return x + 1

        disable()
        for _ in range(10):
            f(0)
        assert snapshot() == {}

    def test_enable_then_call_records(self) -> None:
        @counted("hot")
        def f() -> None:
            pass

        enable()
        f()
        f()
        f()
        assert snapshot()["hot"] == 3

    def test_function_return_value_preserved(self) -> None:
        @counted("ret")
        def f(x: int, y: int) -> int:
            return x * y

        enable()
        assert f(3, 4) == 12
        assert f(5, 6) == 30
        assert snapshot()["ret"] == 2

    def test_method_decoration_binds_self(self) -> None:
        class Holder:
            def __init__(self) -> None:
                self.n = 7

            @counted("method")
            def get_n(self) -> int:
                return self.n

        h = Holder()
        enable()
        assert h.get_n() == 7
        assert h.get_n() == 7
        assert snapshot()["method"] == 2

    def test_multiple_labels_are_independent(self) -> None:
        @counted("a")
        def fa() -> None: ...

        @counted("b")
        def fb() -> None: ...

        enable()
        fa()
        fa()
        fa()
        fb()
        s = snapshot()
        assert s["a"] == 3
        assert s["b"] == 1

    def test_shared_label_accumulates(self) -> None:
        @counted("shared")
        def f1() -> None: ...

        @counted("shared")
        def f2() -> None: ...

        enable()
        f1()
        f2()
        f1()
        assert snapshot()["shared"] == 3


class TestContextManager:
    def test_counters_enabled_block(self) -> None:
        @counted("ctx")
        def f() -> None: ...

        disable()
        f()  # not counted
        with counters_enabled():
            f()
            f()
        f()  # not counted again
        assert snapshot()["ctx"] == 2

    def test_counters_enabled_restores_disabled_state(self) -> None:
        disable()
        with counters_enabled():
            assert is_enabled()
        assert not is_enabled()

    def test_counters_enabled_preserves_enabled_state(self) -> None:
        enable()
        with counters_enabled():
            assert is_enabled()
        assert is_enabled()

    def test_fresh_counters_resets_then_yields(self) -> None:
        @counted("fresh")
        def f() -> None: ...

        # Pre-populate.
        enable()
        f()
        f()
        f()
        assert snapshot()["fresh"] == 3

        with fresh_counters() as cnts:
            assert cnts == {}
            f()
            assert cnts["fresh"] == 1
        # State preserved post-exit (counters_enabled was previously
        # enabled when we entered).
        assert snapshot()["fresh"] == 1

    def test_nested_counters_enabled(self) -> None:
        @counted("nest")
        def f() -> None: ...

        disable()
        with counters_enabled():
            f()
            with counters_enabled():
                f()
                f()
            f()
        assert snapshot()["nest"] == 4


class TestHierarchicalContext:
    def test_top_level_call_uses_bare_label(self) -> None:
        @counted("a")
        def f() -> None: ...

        enable()
        f()
        # Called at top level — no parent context, so path == "a".
        assert snapshot() == {"a": 1}

    def test_nested_calls_build_dotted_path(self) -> None:
        @counted("walk")
        def walk() -> None: ...

        @counted("refresh")
        def refresh() -> None:
            walk()
            walk()
            walk()

        enable()
        refresh()
        s = snapshot()
        assert s == {"refresh": 1, "refresh.walk": 3}

    def test_three_levels(self) -> None:
        @counted("leaf")
        def leaf() -> None: ...

        @counted("mid")
        def mid() -> None:
            leaf()

        @counted("top")
        def top() -> None:
            mid()
            mid()

        enable()
        top()
        s = snapshot()
        assert s == {
            "top": 1,
            "top.mid": 2,
            "top.mid.leaf": 2,
        }

    def test_same_leaf_under_different_parents(self) -> None:
        """Calling the same leaf under two different parents records
        them as distinct paths."""

        @counted("leaf")
        def leaf() -> None: ...

        @counted("parent_a")
        def parent_a() -> None:
            leaf()

        @counted("parent_b")
        def parent_b() -> None:
            leaf()
            leaf()

        enable()
        parent_a()
        parent_b()
        s = snapshot()
        assert s == {
            "parent_a": 1,
            "parent_a.leaf": 1,
            "parent_b": 1,
            "parent_b.leaf": 2,
        }

    def test_recursive_call_extends_path(self) -> None:
        """A counted function calling itself nests its own label.

        Each recursion level adds another segment, so 3-level
        recursion produces paths ``rec``, ``rec.rec``, ``rec.rec.rec``.
        Documents observed behaviour — if you don't want this, use a
        depth-guarded label scheme at the callsite.
        """
        depth = 0

        @counted("rec")
        def rec() -> None:
            nonlocal depth
            depth += 1
            if depth < 3:
                rec()

        enable()
        rec()
        s = snapshot()
        assert s == {"rec": 1, "rec.rec": 1, "rec.rec.rec": 1}

    def test_current_path_during_call(self) -> None:
        captured: list[tuple[str, ...]] = []

        @counted("inner")
        def inner() -> None:
            captured.append(current_path())

        @counted("outer")
        def outer() -> None:
            captured.append(current_path())
            inner()

        enable()
        outer()
        assert captured == [("outer",), ("outer", "inner")]

    def test_stack_unwinds_after_exception(self) -> None:
        """If the decorated body raises, the label stack must still be
        popped — otherwise subsequent calls would inherit a stale
        parent."""

        @counted("inner")
        def inner() -> None:
            pass

        @counted("boom")
        def boom() -> None:
            raise RuntimeError("intentional")

        enable()
        try:
            boom()
        except RuntimeError:
            pass
        # Stack must be empty post-exception.
        assert current_path() == ()
        # Subsequent top-level call records "inner" with no parent.
        inner()
        assert snapshot()["inner"] == 1


class TestRollup:
    def test_rollup_aggregates_ancestors(self) -> None:
        counts = {
            "refresh": 1,
            "refresh.walk": 5,
            "refresh.find_owned.pin_world": 8,
        }
        r = rollup(counts)
        assert r == {
            "refresh": 1 + 5 + 8,
            "refresh.walk": 5,
            "refresh.find_owned": 8,
            "refresh.find_owned.pin_world": 8,
        }

    def test_rollup_on_live_counts(self) -> None:
        @counted("walk")
        def walk() -> None: ...

        @counted("refresh")
        def refresh() -> None:
            walk()
            walk()

        enable()
        refresh()
        # Live rollup of the module-level _COUNTS.
        r = rollup()
        assert r["refresh"] == 1 + 2  # itself + 2 walks under it
        assert r["refresh.walk"] == 2


class TestDelta:
    def test_delta_returns_diff_per_key(self) -> None:
        @counted("d1")
        def f1() -> None: ...

        @counted("d2")
        def f2() -> None: ...

        enable()
        f1()
        f1()
        baseline = snapshot()
        f1()
        f1()
        f2()
        d = delta(baseline)
        assert d == {"d1": 2, "d2": 1}

    def test_delta_skips_unchanged_keys(self) -> None:
        @counted("a")
        def f() -> None: ...

        enable()
        f()
        baseline = snapshot()
        # No further calls.
        assert delta(baseline) == {}


class TestMaxDepth:
    def test_depth_zero_counts_nothing(self) -> None:
        @counted("a")
        def f() -> None: ...

        enable()
        set_max_depth(0)
        f()
        f()
        assert snapshot() == {}
        set_max_depth(None)

    def test_depth_one_counts_top_only(self) -> None:
        @counted("leaf")
        def leaf() -> None: ...

        @counted("mid")
        def mid() -> None:
            leaf()

        @counted("top")
        def top() -> None:
            mid()

        enable()
        set_max_depth(1)
        top()
        # Only ``top`` is recorded; both ``mid`` and ``leaf`` would be
        # past the cap, so they bypass entirely (depth gate skips both
        # counting and pushing — unlike the path filter which still
        # grows the stack).
        assert snapshot() == {"top": 1}
        set_max_depth(None)

    def test_depth_two_records_two_levels(self) -> None:
        @counted("leaf")
        def leaf() -> None: ...

        @counted("mid")
        def mid() -> None:
            leaf()

        @counted("top")
        def top() -> None:
            mid()

        enable()
        set_max_depth(2)
        top()
        assert snapshot() == {"top": 1, "top.mid": 1}
        set_max_depth(None)

    def test_get_max_depth_round_trip(self) -> None:
        set_max_depth(3)
        assert get_max_depth() == 3
        set_max_depth(None)
        assert get_max_depth() is None

    def test_negative_depth_rejected(self) -> None:
        with pytest.raises(ValueError):
            set_max_depth(-1)

    def test_context_manager_max_depth_scoped(self) -> None:
        @counted("top")
        def top() -> None:
            inner()

        @counted("inner")
        def inner() -> None: ...

        # Outside: full depth, both recorded.
        with counters_enabled():
            top()
            assert snapshot() == {"top": 1, "top.inner": 1}

        # Inside max_depth=1: only top.
        with fresh_counters(max_depth=1):
            top()
            assert snapshot() == {"top": 1}

        # After exit, cap is back to None.
        assert get_max_depth() is None


class TestPathFilter:
    def test_simple_prefix_filter_includes_matching(self) -> None:
        @counted("walk")
        def walk() -> None: ...

        @counted("refresh")
        def refresh() -> None:
            walk()
            walk()

        enable()
        set_path_filter("refresh.**")
        refresh()
        s = snapshot()
        # refresh itself doesn't match "refresh.**" (needs at least
        # one more segment) — but its children do.
        assert "refresh" not in s
        assert s["refresh.walk"] == 2
        set_path_filter(None)

    def test_exact_path_filter(self) -> None:
        @counted("a")
        def a() -> None:
            b()

        @counted("b")
        def b() -> None: ...

        enable()
        set_path_filter("a")
        a()
        s = snapshot()
        # Only "a" matches exactly.  "a.b" doesn't match the filter so
        # the call to b() isn't counted — but the stack still grew
        # under a(), so the (un-counted) path was "a.b" rather than
        # bare "b".
        assert s == {"a": 1}
        set_path_filter(None)

    def test_single_segment_wildcard(self) -> None:
        @counted("leaf")
        def leaf() -> None: ...

        @counted("p1")
        def p1() -> None:
            leaf()

        @counted("p2")
        def p2() -> None:
            leaf()

        enable()
        # Match exactly two segments ending in "leaf".
        set_path_filter("*.leaf")
        p1()
        p2()
        s = snapshot()
        assert "p1.leaf" in s and s["p1.leaf"] == 1
        assert "p2.leaf" in s and s["p2.leaf"] == 1
        # The parent paths "p1" / "p2" are single segments; they
        # don't match "*.leaf".
        assert "p1" not in s
        assert "p2" not in s
        set_path_filter(None)

    def test_double_star_matches_arbitrary_depth(self) -> None:
        @counted("z")
        def z() -> None: ...

        @counted("y")
        def y() -> None:
            z()

        @counted("x")
        def x() -> None:
            y()

        enable()
        # "x.**" matches one-or-more segments below "x".  Stacks
        # always grow regardless of the filter, so y and z see "x"
        # as their parent and end up under "x.y" / "x.y.z".  "x"
        # itself doesn't match "x.**", so it isn't counted.
        set_path_filter("x.**")
        x()
        s = snapshot()
        assert s == {"x.y": 1, "x.y.z": 1}
        set_path_filter(None)

    def test_filter_matching_top_and_below(self) -> None:
        @counted("z")
        def z() -> None: ...

        @counted("y")
        def y() -> None:
            z()

        @counted("x")
        def x() -> None:
            y()

        enable()
        # Two patterns: exact "x" plus everything below.
        set_path_filter("x;x.**")
        x()
        assert snapshot() == {"x": 1, "x.y": 1, "x.y.z": 1}
        set_path_filter(None)

    def test_semicolon_alternatives(self) -> None:
        """Format from the user request: ``a.b.c.*;g.h.c.*``."""

        @counted("leaf")
        def leaf() -> None: ...

        # Build call chains so paths are predictable.
        def build_chain(*labels: str):
            def innermost():
                leaf()

            fn = innermost
            for lab in reversed(labels):
                fn = counted(lab)(fn)
            return fn

        # a → b → c → leaf
        chain_abc = build_chain("a", "b", "c")
        # g → h → c → leaf
        chain_ghc = build_chain("g", "h", "c")
        # foo → bar → leaf (should be filtered out)
        chain_foo = build_chain("foo", "bar")

        enable()
        set_path_filter("a.b.c.*;g.h.c.*")
        chain_abc()
        chain_ghc()
        chain_foo()
        s = snapshot()
        # Children of a.b.c and g.h.c are kept; the prefix paths
        # themselves don't match (they're 3 segments, the patterns
        # require 4) so they bypass.
        assert s == {
            "a.b.c.leaf": 1,
            "g.h.c.leaf": 1,
        }
        set_path_filter(None)

    def test_empty_or_none_filter_disables(self) -> None:
        @counted("a")
        def f() -> None: ...

        enable()
        set_path_filter("a")
        f()
        assert snapshot() == {"a": 1}
        # None re-allows everything.
        reset()
        set_path_filter(None)
        f()
        assert snapshot() == {"a": 1}

    def test_get_path_filter_round_trip(self) -> None:
        set_path_filter("foo.**;bar.*")
        assert get_path_filter() == "foo.**;bar.*"
        set_path_filter(None)
        assert get_path_filter() is None

    def test_context_manager_filter_scoped(self) -> None:
        @counted("a")
        def a() -> None:
            b()

        @counted("b")
        def b() -> None: ...

        # No filter — both recorded.
        with fresh_counters():
            a()
            assert snapshot() == {"a": 1, "a.b": 1}

        # With filter — only a.b.
        with fresh_counters(path_filter="a.b"):
            a()
            assert snapshot() == {"a.b": 1}

        # Restored.
        assert get_path_filter() is None


class TestDepthAndFilterTogether:
    def test_depth_caps_even_when_filter_would_match(self) -> None:
        @counted("a")
        def a() -> None:
            b()

        @counted("b")
        def b() -> None:
            c()

        @counted("c")
        def c() -> None: ...

        enable()
        # Filter allows full tree but depth caps at 2.
        set_path_filter("a.**;a")
        set_max_depth(2)
        a()
        s = snapshot()
        # ``a`` (depth 1) bypasses filter? — "a" doesn't match "a.**"
        # but matches plain "a", so it gets counted.  Then ``a.b``
        # (depth 2) matches "a.**" so it's counted.  ``a.b.c`` would
        # be depth 3 but the cap is 2 → bypass.
        assert s == {"a": 1, "a.b": 1}
        set_max_depth(None)
        set_path_filter(None)


class TestZeroOverheadInPrinciple:
    """Sanity: when disabled, calling a decorated function takes
    roughly the same wall time as the bare function.

    Not a tight assertion — Python timing is noisy and the wrapper
    still does one attribute load + one branch — but a 5×+ slowdown
    when disabled would indicate the fast path is broken.
    """

    @pytest.mark.serial
    def test_disabled_wrapper_not_dramatically_slower(self) -> None:
        import time

        @counted("nop")
        def wrapped() -> int:
            return 1

        def bare() -> int:
            return 1

        disable()
        N = 100_000
        t0 = time.perf_counter()
        for _ in range(N):
            bare()
        bare_t = time.perf_counter() - t0

        t0 = time.perf_counter()
        for _ in range(N):
            wrapped()
        wrapped_t = time.perf_counter() - t0

        # 10× headroom for noise.  Bare Python function call is ~50ns;
        # the wrapper adds one attribute load + branch (~30ns).  Total
        # well under 5× even on a slow CI runner.
        assert wrapped_t < bare_t * 10, (
            f"counted wrapper too slow when disabled: {wrapped_t/bare_t:.1f}× "
            "the bare call cost"
        )
