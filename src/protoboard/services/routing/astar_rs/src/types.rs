// SPDX-License-Identifier: MIT
//! Internal types for the parallel-universe A* router.

use std::collections::{HashMap, HashSet};

/// Grid coordinate scaled by 1000 to avoid f64 hashing.
/// e.g. pad at x=1.5 → ix=1500
pub type ICoord = i32;
pub type IPoint = (ICoord, ICoord);
pub type ISide = u8; // 0=top, 1=bottom

/// Scale factor: f64 pad coordinate → ICoord
pub const SCALE: f64 = 1000.0;

#[inline]
pub fn to_ic(v: f64) -> ICoord {
    (v * SCALE).round() as ICoord
}

#[inline]
pub fn to_ip(x: f64, y: f64) -> IPoint {
    (to_ic(x), to_ic(y))
}

#[inline]
pub fn side_idx(s: &str) -> ISide {
    if s == "top" { 0 } else { 1 }
}

#[inline]
pub fn side_str(s: ISide) -> &'static str {
    if s == 0 { "top" } else { "bottom" }
}

#[inline]
pub fn other_side(s: ISide) -> ISide {
    1 - s
}

/// All inputs needed for one A* search.
pub struct AstarInput {
    pub valid: [HashSet<IPoint>; 2],  // [top, bottom]
    pub source: IPoint,
    pub source_side: ISide,
    pub targets: HashSet<IPoint>,

    // cost model
    pub cost_free_pad: f64,
    pub cost_same_net: f64,
    pub cost_occupied: f64,
    pub cost_direction_change: f64,
    pub cost_layer_change: f64,
    pub cost_crossing_insulated: f64,
    pub cost_crossing_bare: f64,
    pub near_pin_cost: f64,
    pub insulated: bool,

    // occupancy (point, side)
    pub occupied: HashSet<(IPoint, ISide)>,
    pub same_net: HashSet<(IPoint, ISide)>,
    pub near_pins: HashSet<(IPoint, ISide)>,
    pub penalised: HashMap<(IPoint, ISide), f64>,

    // bare wire segments for crossing detection: (src, dst, side)
    pub bare_segs: Vec<(IPoint, IPoint, ISide)>,

    // plated-through vias
    pub vias: HashSet<IPoint>,

    // forbidden edges for k-path: ((from_point, from_side), (to_point, to_side))
    pub forbidden: HashSet<((IPoint, ISide), (IPoint, ISide))>,

    pub extra_budget_ratio: f64,
    pub k: usize,
}

/// One step in a routed path: (x_scaled, y_scaled, side)
pub type RawPathStep = (IPoint, ISide);

/// Raw result before converting back to Python types.
pub struct RawResult {
    pub path: Vec<RawPathStep>,
    pub cost: f64,
}

// 8-connectivity neighbours: (dx, dy, distance_multiplier * 1000 as i32)
// Stored as (dx_ic, dy_ic, dist_mult_scaled) where dist_mult_scaled = mult * 1000
pub const NEIGHBOURS: [(ICoord, ICoord, i64); 8] = [
    ( SCALE as ICoord,  0,               1000),
    (-(SCALE as ICoord), 0,              1000),
    ( 0,                 SCALE as ICoord, 1000),
    ( 0,               -(SCALE as ICoord), 1000),
    ( SCALE as ICoord,  SCALE as ICoord,  1414), // √2 * 1000
    ( SCALE as ICoord, -(SCALE as ICoord), 1414),
    (-(SCALE as ICoord), SCALE as ICoord,  1414),
    (-(SCALE as ICoord), -(SCALE as ICoord), 1414),
];
