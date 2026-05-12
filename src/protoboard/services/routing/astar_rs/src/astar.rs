// SPDX-License-Identifier: MIT
//! Sequential A* implementation — pure Rust, no GIL, no Python objects.

use std::collections::{BinaryHeap, HashMap, HashSet};
use std::cmp::Ordering;
use std::sync::Arc;

use crate::types::*;

// ---------------------------------------------------------------------------
// Heap entry
// ---------------------------------------------------------------------------

/// Heap entry for the A* open set.
/// BinaryHeap is a max-heap, so we negate f to get min-heap behaviour.
#[derive(Clone)]
pub struct HeapEntry {
    /// Negated f-score (×10000 as i64) for max-heap → min-heap ordering.
    pub neg_f: i64,
    /// Actual g-score ×10000.
    pub g_scaled: i64,
    /// Monotone sequence counter for stable ordering on equal f.
    pub neg_seq: i64,
    pub pos: IPoint,
    pub side: ISide,
    /// Last direction taken (dx_ic, dy_ic) — used for turn-cost.
    pub dir: (ICoord, ICoord),
    /// Accumulated path so far — Arc for cheap clone on branching.
    pub path: Arc<Vec<RawPathStep>>,
}

impl PartialEq for HeapEntry {
    fn eq(&self, other: &Self) -> bool {
        self.neg_f == other.neg_f && self.neg_seq == other.neg_seq
    }
}
impl Eq for HeapEntry {}

impl PartialOrd for HeapEntry {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for HeapEntry {
    fn cmp(&self, other: &Self) -> Ordering {
        // Primary: neg_f descending (= f ascending)
        // Secondary: neg_seq descending (= seq ascending) for stability
        other.neg_f.cmp(&self.neg_f)
            .then(other.neg_seq.cmp(&self.neg_seq))
    }
}

// ---------------------------------------------------------------------------
// Visited key
// ---------------------------------------------------------------------------

type VisitedKey = (IPoint, ISide, ICoord, ICoord); // (pos, side, dx, dy)

// ---------------------------------------------------------------------------
// Heuristic
// ---------------------------------------------------------------------------

#[inline]
fn heuristic(pos: IPoint, targets: &HashSet<IPoint>) -> i64 {
    targets.iter().map(|t| {
        let dx = (pos.0 - t.0).abs() as i64;
        let dy = (pos.1 - t.1).abs() as i64;
        // Euclidean ×SCALE (already scaled) ×10000/SCALE = ×10
        // We compute integer approximation: max(dx,dy)*10 + min(dx,dy)*4
        // (approximates √2 cheaply; exact would need sqrt)
        let mx = dx.max(dy);
        let mn = dx.min(dy);
        mx * 10 + mn * 4  // ≈ (mx + mn*(√2-1)) * 10, slightly underestimates
    }).min().unwrap_or(0)
}

// ---------------------------------------------------------------------------
// Crossing detection
// ---------------------------------------------------------------------------

/// True if axis-aligned integer segments A1→A2 and B1→B2 cross in their interiors.
fn segs_cross(a1: IPoint, a2: IPoint, b1: IPoint, b2: IPoint) -> bool {
    let a_horiz = a1.1 == a2.1;
    let b_horiz = b1.1 == b2.1;
    if a_horiz == b_horiz {
        return false;
    }
    if a_horiz {
        let bx = b1.0;
        let ay = a1.1;
        let ax_min = a1.0.min(a2.0);
        let ax_max = a1.0.max(a2.0);
        let by_min = b1.1.min(b2.1);
        let by_max = b1.1.max(b2.1);
        ax_min < bx && bx < ax_max && by_min < ay && ay < by_max
    } else {
        let ax = a1.0;
        let by = b1.1;
        let bx_min = b1.0.min(b2.0);
        let bx_max = b1.0.max(b2.0);
        let ay_min = a1.1.min(a2.1);
        let ay_max = a1.1.max(a2.1);
        bx_min < ax && ax < bx_max && ay_min < by && by < ay_max
    }
}

fn crossing_cost(
    p1: IPoint, p2: IPoint, side: ISide,
    bare_segs: &[(IPoint, IPoint, ISide)],
    cost_crossing: f64,
) -> f64 {
    if bare_segs.is_empty() {
        return 0.0;
    }
    let dx = p2.0 - p1.0;
    let dy = p2.1 - p1.1;
    let is_diag = dx != 0 && dy != 0;

    let check = |a1: IPoint, a2: IPoint| -> bool {
        bare_segs.iter().any(|(bs, bd, bs_side)| {
            *bs_side == side && segs_cross(a1, a2, *bs, *bd)
        })
    };

    let crosses = if is_diag {
        let mid1 = (p1.0 + dx, p1.1);
        let mid2 = (p1.0, p1.1 + dy);
        check(p1, mid1) || check(mid1, p2) || check(p1, mid2) || check(mid2, p2)
    } else {
        check(p1, p2)
    };

    if crosses { cost_crossing } else { 0.0 }
}

// ---------------------------------------------------------------------------
// Step cost
// ---------------------------------------------------------------------------

#[inline]
fn step_cost(
    npos: IPoint, side: ISide, dist_mult_scaled: i64,
    input: &AstarInput,
) -> f64 {
    let key = (npos, side);
    if input.occupied.contains(&key) {
        return input.cost_occupied;
    }
    let base = if input.same_net.contains(&key) {
        input.cost_same_net
    } else {
        input.cost_free_pad
    };
    let base_scaled = base * dist_mult_scaled as f64 / 1000.0;
    let near = if input.near_pins.contains(&key) { input.near_pin_cost } else { 0.0 };
    let penalty = input.penalised.get(&key).copied().unwrap_or(0.0);
    base_scaled + near + penalty
}

// ---------------------------------------------------------------------------
// Public A* entry point
// ---------------------------------------------------------------------------

/// Run A* search. Returns up to `input.k` results, sorted by cost.
/// `cancel_nodes` — if Some(n), stop each universe after expanding n nodes
///   past first-found (for parallel budget control — pass None for sequential).
pub fn run_astar(input: &AstarInput) -> Vec<RawResult> {
    let mut results: Vec<RawResult> = Vec::new();
    let mut forbidden = input.forbidden.clone();

    // Trivial: source == target
    if input.targets.contains(&input.source) {
        results.push(RawResult {
            path: vec![(input.source, input.source_side)],
            cost: 0.0,
        });
        return results;
    }

    for _iter in 0..input.k {
        if let Some(r) = astar_once(input, &forbidden) {
            // Mark all edges of this path as forbidden for next iteration
            for w in r.path.windows(2) {
                let (a, as_) = w[0];
                let (b, bs_) = w[1];
                forbidden.insert(((a, as_), (b, bs_)));
                forbidden.insert(((b, bs_), (a, as_)));
            }
            results.push(r);
        } else {
            break;
        }
    }

    results
}

/// Single A* search — one path from source to any target.
pub fn astar_once(
    input: &AstarInput,
    forbidden: &HashSet<((IPoint, ISide), (IPoint, ISide))>,
) -> Option<RawResult> {
    astar_once_with_budget(input, forbidden, None)
}

/// Single A* search with optional node budget (for parallel universe).
/// If `budget` is Some(n), stop after expanding n nodes (returns None if not found within budget).
pub fn astar_once_with_budget(
    input: &AstarInput,
    forbidden: &HashSet<((IPoint, ISide), (IPoint, ISide))>,
    budget: Option<usize>,
) -> Option<RawResult> {
    if !input.valid[input.source_side as usize].contains(&input.source) {
        return None;
    }

    let mut heap: BinaryHeap<HeapEntry> = BinaryHeap::new();
    let mut visited: HashMap<VisitedKey, i64> = HashMap::new();
    let mut seq: i64 = 0;

    let h0 = heuristic(input.source, &input.targets);
    heap.push(HeapEntry {
        neg_f: -(h0),
        g_scaled: 0,
        neg_seq: 0,
        pos: input.source,
        side: input.source_side,
        dir: (0, 0),
        path: Arc::new(vec![(input.source, input.source_side)]),
    });

    let mut nodes_expanded: usize = 0;

    while let Some(entry) = heap.pop() {
        let HeapEntry { neg_f: _, g_scaled, neg_seq: _, pos, side, dir, path } = entry;

        let vkey: VisitedKey = (pos, side, dir.0, dir.1);
        if let Some(&best) = visited.get(&vkey) {
            if best <= g_scaled {
                continue;
            }
        }
        visited.insert(vkey, g_scaled);
        nodes_expanded += 1;

        if let Some(bud) = budget {
            if nodes_expanded > bud {
                return None;
            }
        }

        if input.targets.contains(&pos) {
            let cost = g_scaled as f64 / 10000.0;
            return Some(RawResult {
                path: Arc::try_unwrap(path).unwrap_or_else(|arc| (*arc).clone()),
                cost,
            });
        }

        // Same-layer neighbours
        for &(ndx, ndy, dist_mult_scaled) in &NEIGHBOURS {
            let npos = (pos.0 + ndx, pos.1 + ndy);
            if !input.valid[side as usize].contains(&npos) {
                continue;
            }
            if forbidden.contains(&((pos, side), (npos, side))) {
                continue;
            }

            let sc = step_cost(npos, side, dist_mult_scaled, input);
            if sc >= f64::MAX / 2.0 {
                continue;
            }

            let crossing_c = crossing_cost(
                pos, npos, side,
                &input.bare_segs,
                if input.insulated { input.cost_crossing_insulated } else { input.cost_crossing_bare },
            );
            if crossing_c >= f64::MAX / 2.0 {
                continue;
            }

            let turn = if (dir.0 != 0 || dir.1 != 0) && (ndx != dir.0 || ndy != dir.1) {
                input.cost_direction_change
            } else {
                0.0
            };

            let ng_f64 = g_scaled as f64 / 10000.0 + sc + crossing_c + turn;
            let ng_scaled = (ng_f64 * 10000.0) as i64;

            let nvkey: VisitedKey = (npos, side, ndx, ndy);
            if let Some(&best) = visited.get(&nvkey) {
                if best <= ng_scaled {
                    continue;
                }
            }

            let h = heuristic(npos, &input.targets);
            seq -= 1;
            let mut new_path = (*path).clone();
            new_path.push((npos, side));
            heap.push(HeapEntry {
                neg_f: -(ng_scaled + h),
                g_scaled: ng_scaled,
                neg_seq: seq,
                pos: npos,
                side,
                dir: (ndx, ndy),
                path: Arc::new(new_path),
            });
        }

        // Layer change at via
        if input.vias.contains(&pos) {
            let oside = other_side(side);
            if input.valid[oside as usize].contains(&pos) {
                if !forbidden.contains(&((pos, side), (pos, oside))) {
                    let ng_f64 = g_scaled as f64 / 10000.0 + input.cost_layer_change;
                    let ng_scaled = (ng_f64 * 10000.0) as i64;
                    let nvkey: VisitedKey = (pos, oside, 0, 0);
                    if visited.get(&nvkey).map_or(true, |&best| best > ng_scaled) {
                        let h = heuristic(pos, &input.targets);
                        seq -= 1;
                        let mut new_path = (*path).clone();
                        new_path.push((pos, oside));
                        heap.push(HeapEntry {
                            neg_f: -(ng_scaled + h),
                            g_scaled: ng_scaled,
                            neg_seq: seq,
                            pos,
                            side: oside,
                            dir: (0, 0),
                            path: Arc::new(new_path),
                        });
                    }
                }
            }
        }
    }

    None
}
