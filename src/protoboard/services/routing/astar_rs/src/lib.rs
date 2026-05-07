// SPDX-License-Identifier: MIT
//! PyO3 bindings for the parallel-universe A* router.
//!
//! Exposes `find_paths_parallel(...)` to Python. All computation happens
//! with the GIL released (`py.allow_threads`), so true OS threads are used.

use std::collections::{HashMap, HashSet};

use pyo3::prelude::*;

mod types;
mod parallel;

use types::*;
use parallel::run_parallel;

// ---------------------------------------------------------------------------
// Conversion helpers: Python tuples → Rust types
// ---------------------------------------------------------------------------

#[inline]
fn parse_side(s: &str) -> ISide {
    side_idx(s)
}

fn build_point_set(pts: Vec<(f64, f64)>) -> HashSet<IPoint> {
    pts.into_iter().map(|(x, y)| to_ip(x, y)).collect()
}

fn build_ps_set(items: Vec<(f64, f64, String)>) -> HashSet<(IPoint, ISide)> {
    items.into_iter().map(|(x, y, s)| (to_ip(x, y), side_idx(&s))).collect()
}

fn build_ps_map(items: Vec<(f64, f64, String, f64)>) -> HashMap<(IPoint, ISide), f64> {
    items.into_iter().map(|(x, y, s, v)| ((to_ip(x, y), side_idx(&s)), v)).collect()
}

fn build_bare_segs(items: Vec<(f64, f64, f64, f64, String)>) -> Vec<(IPoint, IPoint, ISide)> {
    items.into_iter().map(|(x1, y1, x2, y2, s)| {
        (to_ip(x1, y1), to_ip(x2, y2), side_idx(&s))
    }).collect()
}

fn build_forbidden(
    items: Vec<(f64, f64, String, f64, f64, String)>,
) -> HashSet<((IPoint, ISide), (IPoint, ISide))> {
    items.into_iter().map(|(x1, y1, s1, x2, y2, s2)| {
        ((to_ip(x1, y1), side_idx(&s1)), (to_ip(x2, y2), side_idx(&s2)))
    }).collect()
}

// ---------------------------------------------------------------------------
// Python-visible function
// ---------------------------------------------------------------------------

/// Find up to `k` cheapest paths from `source` to any point in `targets`.
///
/// All occupancy/cost data is passed as flat Python lists of tuples.
/// Computation runs with GIL released — true OS-thread parallelism.
///
/// Returns: list of (path, cost) where path = list of (x, y, side_str).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn find_paths_parallel(
    py: Python<'_>,
    // valid nodes per side
    valid_top: Vec<(f64, f64)>,
    valid_bottom: Vec<(f64, f64)>,
    // source
    source_x: f64,
    source_y: f64,
    source_side: String,
    // targets
    targets: Vec<(f64, f64)>,
    // cost model
    cost_free_pad: f64,
    cost_same_net: f64,
    cost_occupied: f64,
    cost_direction_change: f64,
    cost_layer_change: f64,
    cost_crossing_insulated: f64,
    cost_crossing_bare: f64,
    near_pin_cost: f64,
    insulated: bool,
    // occupancy
    occupied: Vec<(f64, f64, String)>,
    same_net: Vec<(f64, f64, String)>,
    near_pins: Vec<(f64, f64, String)>,
    penalised: Vec<(f64, f64, String, f64)>,
    bare_segs: Vec<(f64, f64, f64, f64, String)>,
    vias: Vec<(f64, f64)>,
    // k-path forbidden edges
    forbidden: Vec<(f64, f64, String, f64, f64, String)>,
    // settings
    extra_budget_ratio: f64,
    k: usize,
) -> PyResult<Vec<(Vec<(f64, f64, String)>, f64)>> {
    let input = AstarInput {
        valid: [
            build_point_set(valid_top),
            build_point_set(valid_bottom),
        ],
        source: to_ip(source_x, source_y),
        source_side: parse_side(&source_side),
        targets: build_point_set(targets),

        cost_free_pad,
        cost_same_net,
        cost_occupied,
        cost_direction_change,
        cost_layer_change,
        cost_crossing_insulated,
        cost_crossing_bare,
        near_pin_cost,
        insulated,

        occupied: build_ps_set(occupied),
        same_net: build_ps_set(same_net),
        near_pins: build_ps_set(near_pins),
        penalised: build_ps_map(penalised),
        bare_segs: build_bare_segs(bare_segs),
        vias: build_point_set(vias),
        forbidden: build_forbidden(forbidden),

        extra_budget_ratio,
        k,
    };

    // Release the GIL for the entire computation.
    let raw_results = py.allow_threads(|| run_parallel(&input));

    // Convert results back to Python-friendly tuples.
    let scale_inv = 1.0 / SCALE;
    let py_results = raw_results.into_iter().map(|r| {
        let path: Vec<(f64, f64, String)> = r.path.into_iter().map(|((ix, iy), iside)| {
            (ix as f64 * scale_inv, iy as f64 * scale_inv, side_str(iside).to_string())
        }).collect();
        (path, r.cost)
    }).collect();

    Ok(py_results)
}

// ---------------------------------------------------------------------------
// Module definition
// ---------------------------------------------------------------------------

#[pymodule]
fn protoboard_astar_rs(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(find_paths_parallel, m)?)?;
    Ok(())
}
