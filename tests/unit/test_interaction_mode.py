# SPDX-License-Identifier: MIT
"""Unit tests for HighlightProfile + selection_axes primitives."""

import pytest

from protoboard.gui.interaction_mode import HighlightProfile
from protoboard.gui.selection_axes import (
    RELATION_LABELS,
    RELATION_PROFILES,
    RelationClass,
    RelationProfile,
    SelectionFilter,
    filter_from_tokens,
    filter_to_tokens,
    relation_from_str,
)


def test_highlight_profile_is_frozen() -> None:
    p = HighlightProfile()
    with pytest.raises((AttributeError, TypeError)):
        p.dim_components = 0.5  # type: ignore[misc]


def test_selection_filter_all_is_union() -> None:
    assert SelectionFilter.COMPONENTS in SelectionFilter.ALL
    assert SelectionFilter.WIRES in SelectionFilter.ALL
    assert SelectionFilter.GROUPS in SelectionFilter.ALL


def test_relation_labels_cover_all_relations() -> None:
    for r in RelationClass:
        assert r in RELATION_LABELS
        assert RELATION_LABELS[r].strip(), f"{r} has empty label"


def test_relation_profiles_cover_all_relations() -> None:
    for r in RelationClass:
        assert r in RELATION_PROFILES
        p = RELATION_PROFILES[r]
        assert isinstance(p, RelationProfile)
        assert 0.0 <= p.dim_components <= 1.0
        assert 0.0 <= p.dim_wires <= 1.0
        assert p.pad_marker_radius >= 0.0
        for rgba in (
            p.pad_marker_rgba,
            p.unconnected_rgba,
            p.neighbor_rect_rgba,
            p.neighbor_pen_rgba,
        ):
            for c in rgba:
                assert 0 <= c <= 255


def test_relation_profile_is_frozen() -> None:
    p = RelationProfile()
    with pytest.raises((AttributeError, TypeError)):
        p.dim_components = 0.5  # type: ignore[misc]


def test_filter_tokens_roundtrip() -> None:
    for f in (
        SelectionFilter.ALL,
        SelectionFilter.COMPONENTS,
        SelectionFilter.COMPONENTS | SelectionFilter.WIRES,
        SelectionFilter.WIRES | SelectionFilter.GROUPS,
    ):
        assert filter_from_tokens(filter_to_tokens(f)) == f


def test_filter_from_tokens_missing_defaults_to_all() -> None:
    assert filter_from_tokens(None) == SelectionFilter.ALL
    # Empty list also collapses to ALL (no point in selecting nothing).
    assert filter_from_tokens([]) == SelectionFilter.ALL


def test_filter_from_tokens_ignores_unknown() -> None:
    assert filter_from_tokens(["components", "garbage"]) == SelectionFilter.COMPONENTS


def test_relation_from_str_defaults_to_same_logical_net() -> None:
    assert relation_from_str(None) == RelationClass.SAME_LOGICAL_NET
    assert relation_from_str("") == RelationClass.SAME_LOGICAL_NET
    assert relation_from_str("not_a_real_value") == RelationClass.SAME_LOGICAL_NET


def test_relation_from_str_roundtrip() -> None:
    for r in RelationClass:
        assert relation_from_str(r.value) == r
