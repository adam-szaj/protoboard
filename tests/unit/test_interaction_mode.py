# SPDX-License-Identifier: MIT
import pytest

from protoboard.gui.interaction_mode import (
    INTERACTION_MODES,
    HighlightProfile,
    HighlightScope,
    InteractionMode,
    SelectionTarget,
    get_mode,
)


def test_all_modes_have_unique_ids() -> None:
    ids = [m.id for m in INTERACTION_MODES.values()]
    assert len(ids) == len(set(ids))


def test_dict_keys_match_mode_ids() -> None:
    for key, mode in INTERACTION_MODES.items():
        assert key == mode.id


def test_all_modes_have_labels() -> None:
    for mode in INTERACTION_MODES.values():
        assert mode.label.strip(), f"mode {mode.id!r} has empty label"


def test_highlight_profile_rgba_in_range() -> None:
    def check(rgba: tuple[int, int, int, int], name: str) -> None:
        for component in rgba:
            assert 0 <= component <= 255, f"{name}: value {component} out of [0,255]"

    for mode in INTERACTION_MODES.values():
        p = mode.highlight_profile
        check(p.pad_marker_rgba, f"{mode.id}.pad_marker_rgba")
        check(p.unconnected_rgba, f"{mode.id}.unconnected_rgba")
        check(p.neighbor_rect_rgba, f"{mode.id}.neighbor_rect_rgba")
        check(p.neighbor_pen_rgba, f"{mode.id}.neighbor_pen_rgba")


def test_highlight_profile_dim_values_in_range() -> None:
    for mode in INTERACTION_MODES.values():
        p = mode.highlight_profile
        assert 0.0 <= p.dim_components <= 1.0, f"{mode.id}.dim_components out of range"
        assert 0.0 <= p.dim_wires <= 1.0, f"{mode.id}.dim_wires out of range"


def test_highlight_profile_radius_non_negative() -> None:
    for mode in INTERACTION_MODES.values():
        assert mode.highlight_profile.pad_marker_radius >= 0.0


def test_get_mode_returns_correct_mode() -> None:
    for id_ in INTERACTION_MODES:
        m = get_mode(id_)
        assert m.id == id_


def test_get_mode_raises_for_unknown() -> None:
    with pytest.raises(KeyError):
        get_mode("this_does_not_exist")


def test_default_mode_has_no_highlight() -> None:
    m = get_mode("default")
    assert m.highlight_scope == HighlightScope.NONE
    assert m.selection_target == SelectionTarget.CLICKED_OBJECT


def test_inspect_only_does_not_change_selection() -> None:
    m = get_mode("inspect_only")
    assert m.selection_target == SelectionTarget.NOTHING
    assert m.highlight_scope != HighlightScope.NONE


def test_select_connected_comps_targets_correlated() -> None:
    m = get_mode("select_connected_comps")
    assert m.selection_target == SelectionTarget.CORRELATED_COMPS


def test_net_unconnected_uses_dashed_marker() -> None:
    m = get_mode("net_unconnected")
    assert m.highlight_profile.unconnected_dashed is True


def test_interaction_mode_is_frozen() -> None:
    m = get_mode("default")
    with pytest.raises((AttributeError, TypeError)):
        m.id = "changed"  # type: ignore[misc]


def test_highlight_profile_is_frozen() -> None:
    p = HighlightProfile()
    with pytest.raises((AttributeError, TypeError)):
        p.dim_components = 0.5  # type: ignore[misc]
