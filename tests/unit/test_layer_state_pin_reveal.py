# SPDX-License-Identifier: MIT
"""Per-component pin-name reveal: one component shows names while hidden."""

from __future__ import annotations

import uuid

from protoboard.controller.view_options.layers import LayerOverrides, LayerVisibility
from protoboard.gui.layer_state import LayerState


def _hidden_pins_state(reveal=None) -> LayerState:
    return LayerState(
        overrides=LayerOverrides(meta_pin_labels=LayerVisibility.HIDDEN),
        meta_pin_labels_reveal_cid=reveal,
    )


def test_hidden_layer_zero_alpha_for_others() -> None:
    st = _hidden_pins_state(reveal=uuid.uuid4())
    assert st.alpha_for_meta_pin_labels("top", cid=uuid.uuid4()) == 0.0


def test_revealed_component_gets_full_alpha() -> None:
    cid = uuid.uuid4()
    st = _hidden_pins_state(reveal=cid)
    assert st.alpha_for_meta_pin_labels("top", cid=cid) > 0.0


def test_no_reveal_still_hidden() -> None:
    cid = uuid.uuid4()
    st = _hidden_pins_state(reveal=None)
    assert st.alpha_for_meta_pin_labels("top", cid=cid) == 0.0


def test_visible_layer_unaffected_by_cid() -> None:
    cid = uuid.uuid4()
    st = LayerState()  # meta_pin_labels defaults to FULL
    assert st.alpha_for_meta_pin_labels("top", cid=cid) > 0.0
    assert st.alpha_for_meta_pin_labels("top") > 0.0  # cid optional


def test_layer_visibility_cycles_through_all_states() -> None:
    assert LayerVisibility.FULL.cycle() is LayerVisibility.GHOST
    assert LayerVisibility.GHOST.cycle() is LayerVisibility.HIDDEN
    assert LayerVisibility.HIDDEN.cycle() is LayerVisibility.FULL


def test_layer_overrides_reset_restores_every_layer() -> None:
    overrides = LayerOverrides(
        board=LayerVisibility.HIDDEN,
        wires_top=LayerVisibility.GHOST,
        meta_pin_labels=LayerVisibility.HIDDEN,
    )
    assert not overrides.all_full()

    overrides.reset()

    assert overrides.all_full()
