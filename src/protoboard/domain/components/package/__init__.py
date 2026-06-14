# SPDX-License-Identifier: MIT
"""Generic parametric package model (pure domain, no Qt).

Implements the model specified in ``docs/footprints/spec.md``: body vs board
offsets, axial solver, bend, collision masks. Geometry is computed by code
(``shapes.py``); YAML is normalised into plain structures by the loader before
reaching this layer.
"""
