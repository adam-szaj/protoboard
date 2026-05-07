// SPDX-License-Identifier: MIT
//! Parallel-universe A* using rayon.
//!
//! Strategy: for each of the k path iterations, run multiple independent A*
//! searches in parallel (different starting sides, rayon work-stealing).
//! When the first universe finds a path, the others receive a node budget
//! of N * (1 + extra_budget_ratio) to look for a cheaper alternative.
//! The cheapest result across all universes wins.

use std::collections::{BinaryHeap, HashMap, HashSet};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use rayon::prelude::*;

use crate::types::*;

// Re-export so lib.rs can call run_parallel.
pub use run_parallel_impl as run_parallel;

// ---------------------------------------------------------------------------
// Public entry point
// ---------------------------------------------------------------------------

pub fn run_parallel_impl(input: &AstarInput) -> Vec<RawResult> {
    let mut all_results: Vec<RawResult> = Vec::new();
    let mut forbidden = input.forbidden.clone();

    for _iter in 0..input.k {
        let result = parallel_astar_one(input, &forbidden);
        if let Some(r) = result {
            // Forbid all edges of the found path for the next iteration.
            for w in r.path.windows(2) {
                let (a, as_) = w[0];
                let (b, bs_) = w[1];
                forbidden.insert(((a, as_), (b, bs_)));
                forbidden.insert(((b, bs_), (a, as_)));
            }
            all_results.push(r);
        } else {
            break;
        }
    }

    all_results
}

// ---------------------------------------------------------------------------
// Single parallel A* search
// ---------------------------------------------------------------------------

fn parallel_astar_one(
    input: &AstarInput,
    forbidden: &HashSet<((IPoint, ISide), (IPoint, ISide))>,
) -> Option<RawResult> {
    // Determine which starting sides to try.
    // On multi-layer boards: try both top and bottom simultaneously.
    // On single-layer: only the specified source side.
    let mut sides_to_try: Vec<ISide> = vec![input.source_side];
    if !input.vias.is_empty()
        && input.valid[other_side(input.source_side) as usize].contains(&input.source)
    {
        sides_to_try.push(other_side(input.source_side));
    }

    // Spawn cpu_count universes total, spread across available sides.
    // More universes = more cores used = faster search on complex boards.
    // Each universe has a unique seq_offset so heap entries don't collide.
    let cpu_count = rayon::current_num_threads().max(2);
    let n_sides = sides_to_try.len();
    let tasks: Vec<ISide> = (0..cpu_count)
        .map(|i| sides_to_try[i % n_sides])
        .collect();

    // Shared state: first_found node count (0 = nobody found yet).
    let first_found: Arc<AtomicUsize> = Arc::new(AtomicUsize::new(0));
    let results: Arc<Mutex<Vec<RawResult>>> = Arc::new(Mutex::new(Vec::new()));

    let forbidden_arc = Arc::new(forbidden.clone());

    tasks.par_iter().enumerate().for_each(|(task_idx, &start_side)| {
        let first_found = Arc::clone(&first_found);
        let results = Arc::clone(&results);
        let forbidden = Arc::clone(&forbidden_arc);

        let result = universe_astar(
            input,
            start_side,
            task_idx as i64 * 1_000_000,
            &forbidden,
            &first_found,
            input.extra_budget_ratio,
        );

        if let Some(r) = result {
            results.lock().unwrap().push(r);
        }
    });

    // Return the cheapest result found.
    let mut collected: Vec<RawResult> = results.lock().unwrap().drain(..).collect();
    collected.sort_by(|a, b| a.cost.partial_cmp(&b.cost).unwrap_or(std::cmp::Ordering::Equal));
    collected.into_iter().next()
}

// ---------------------------------------------------------------------------
// One universe A* worker
// ---------------------------------------------------------------------------

#[derive(Clone)]
struct HeapEntry {
    neg_f: i64,
    g_scaled: i64,
    neg_seq: i64,
    pos: IPoint,
    side: ISide,
    dir: (ICoord, ICoord),
    path: Arc<Vec<RawPathStep>>,
}

impl PartialEq for HeapEntry {
    fn eq(&self, o: &Self) -> bool { self.neg_f == o.neg_f && self.neg_seq == o.neg_seq }
}
impl Eq for HeapEntry {}
impl PartialOrd for HeapEntry {
    fn partial_cmp(&self, o: &Self) -> Option<std::cmp::Ordering> { Some(self.cmp(o)) }
}
impl Ord for HeapEntry {
    fn cmp(&self, o: &Self) -> std::cmp::Ordering {
        // We want a min-heap on f. BinaryHeap is a max-heap.
        // self > other means self should come OUT FIRST (higher priority).
        // Lower f = higher priority, so self > other when self.f < other.f.
        // self.neg_f = -self.f, so self.neg_f > o.neg_f when self.f < o.f.
        // Therefore: compare self.neg_f against o.neg_f normally.
        self.neg_f.cmp(&o.neg_f).then(self.neg_seq.cmp(&o.neg_seq))
    }
}

// Key: (pos, side) only — direction is embedded in the g-cost via turn penalty,
// not in the visited key. Using direction in the key allows revisiting the same
// pad from different directions, which creates cycles in the path.
type VisitedKey = (IPoint, ISide);

#[inline]
fn h(pos: IPoint, targets: &HashSet<IPoint>) -> i64 {
    targets.iter().map(|t| {
        let dx = (pos.0 - t.0).abs() as i64;
        let dy = (pos.1 - t.1).abs() as i64;
        let mx = dx.max(dy);
        let mn = dx.min(dy);
        mx * 10 + mn * 4
    }).min().unwrap_or(0)
}

#[inline]
fn step_cost(npos: IPoint, side: ISide, dist_mult_scaled: i64, input: &AstarInput) -> f64 {
    let key = (npos, side);
    if input.occupied.contains(&key) { return input.cost_occupied; }
    let base = if input.same_net.contains(&key) { input.cost_same_net } else { input.cost_free_pad };
    let scaled = base * dist_mult_scaled as f64 / 1000.0;
    let near = if input.near_pins.contains(&key) { input.near_pin_cost } else { 0.0 };
    scaled + near + input.penalised.get(&key).copied().unwrap_or(0.0)
}

#[inline]
fn crossing_cost(p1: IPoint, p2: IPoint, side: ISide, input: &AstarInput) -> f64 {
    let cost_c = if input.insulated { input.cost_crossing_insulated } else { input.cost_crossing_bare };
    if input.bare_segs.is_empty() || cost_c >= f64::MAX / 2.0 {
        return if cost_c >= f64::MAX / 2.0 && !input.bare_segs.is_empty() { cost_c } else { 0.0 };
    }
    let dx = p2.0 - p1.0;
    let dy = p2.1 - p1.1;
    let is_diag = dx != 0 && dy != 0;
    let check = |a1: IPoint, a2: IPoint| -> bool {
        input.bare_segs.iter().any(|&(bs, bd, bs_side)| bs_side == side && segs_cross(a1, a2, bs, bd))
    };
    let crosses = if is_diag {
        let m1 = (p1.0 + dx, p1.1);
        let m2 = (p1.0, p1.1 + dy);
        check(p1, m1) || check(m1, p2) || check(p1, m2) || check(m2, p2)
    } else {
        check(p1, p2)
    };
    if crosses { cost_c } else { 0.0 }
}

fn segs_cross(a1: IPoint, a2: IPoint, b1: IPoint, b2: IPoint) -> bool {
    let ah = a1.1 == a2.1;
    let bh = b1.1 == b2.1;
    if ah == bh { return false; }
    if ah {
        let (ax0, ax1) = (a1.0.min(a2.0), a1.0.max(a2.0));
        let (by0, by1) = (b1.1.min(b2.1), b1.1.max(b2.1));
        ax0 < b1.0 && b1.0 < ax1 && by0 < a1.1 && a1.1 < by1
    } else {
        let (bx0, bx1) = (b1.0.min(b2.0), b1.0.max(b2.0));
        let (ay0, ay1) = (a1.1.min(a2.1), a1.1.max(a2.1));
        bx0 < a1.0 && a1.0 < bx1 && ay0 < b1.1 && b1.1 < ay1
    }
}

/// Run A* for one universe. Stops early if budget exceeded.
fn universe_astar(
    input: &AstarInput,
    start_side: ISide,
    seq_offset: i64,
    forbidden: &HashSet<((IPoint, ISide), (IPoint, ISide))>,
    first_found: &Arc<AtomicUsize>,
    extra_budget_ratio: f64,
) -> Option<RawResult> {
    // Trivial case
    if input.targets.contains(&input.source) {
        return Some(RawResult {
            path: vec![(input.source, start_side)],
            cost: 0.0,
        });
    }

    if !input.valid[start_side as usize].contains(&input.source) {
        return None;
    }

    let mut heap: BinaryHeap<HeapEntry> = BinaryHeap::new();
    let mut visited: HashMap<VisitedKey, i64> = HashMap::new();
    let mut seq: i64 = seq_offset * -1_000_000;

    let h0 = h(input.source, &input.targets);
    heap.push(HeapEntry {
        neg_f: -h0,
        g_scaled: 0,
        neg_seq: seq,
        pos: input.source,
        side: start_side,
        dir: (0, 0),
        path: Arc::new(vec![(input.source, start_side)]),
    });

    let mut nodes_expanded: usize = 0;

    while let Some(entry) = heap.pop() {
        // Budget check: stop searching once another universe found the target
        // AND we've expanded more nodes than allowed.
        // first_found > 0 means another universe reached the target.
        let ff = first_found.load(Ordering::Relaxed);
        if ff > 0 {
            let budget = ff + (ff as f64 * extra_budget_ratio) as usize;
            if nodes_expanded > budget {
                return None;
            }
        }

        let HeapEntry { g_scaled, pos, side, dir, path, .. } = entry;

        let vkey: VisitedKey = (pos, side);
        if visited.get(&vkey).map_or(false, |&b| b <= g_scaled) { continue; }
        visited.insert(vkey, g_scaled);
        nodes_expanded += 1;

        if input.targets.contains(&pos) {
            // Record node count when first found — used as budget reference for other universes.
            first_found.compare_exchange(0, nodes_expanded, Ordering::Relaxed, Ordering::Relaxed).ok();
            return Some(RawResult {
                path: Arc::try_unwrap(path).unwrap_or_else(|a| (*a).clone()),
                cost: g_scaled as f64 / 10000.0,
            });
        }

        // Same-layer neighbours.
        for &(ndx, ndy, dm) in &NEIGHBOURS {
            let npos = (pos.0 + ndx, pos.1 + ndy);
            if !input.valid[side as usize].contains(&npos) { continue; }
            if forbidden.contains(&((pos, side), (npos, side))) { continue; }

            let sc = step_cost(npos, side, dm, input);
            if sc >= f64::MAX / 2.0 { continue; }
            let cc = crossing_cost(pos, npos, side, input);
            if cc >= f64::MAX / 2.0 { continue; }
            let turn = if (dir.0 != 0 || dir.1 != 0) && (ndx != dir.0 || ndy != dir.1) {
                input.cost_direction_change
            } else { 0.0 };

            let ng = g_scaled as f64 / 10000.0 + sc + cc + turn;
            let ng_s = (ng * 10000.0) as i64;
            let nvk: VisitedKey = (npos, side);
            if visited.get(&nvk).map_or(false, |&b| b <= ng_s) { continue; }

            let hn = h(npos, &input.targets);
            seq -= 1;
            let mut np = (*path).clone();
            np.push((npos, side));
            heap.push(HeapEntry {
                neg_f: -(ng_s + hn),
                g_scaled: ng_s,
                neg_seq: seq,
                pos: npos,
                side,
                dir: (ndx, ndy),
                path: Arc::new(np),
            });
        }

        // Layer change at via.
        if input.vias.contains(&pos) {
            let os = other_side(side);
            if input.valid[os as usize].contains(&pos)
                && !forbidden.contains(&((pos, side), (pos, os)))
                && !input.occupied.contains(&(pos, os))
            {
                let ng = g_scaled as f64 / 10000.0 + input.cost_layer_change;
                let ng_s = (ng * 10000.0) as i64;
                let nvk: VisitedKey = (pos, os);
                if visited.get(&nvk).map_or(true, |&b| b > ng_s) {
                    let hn = h(pos, &input.targets);
                    seq -= 1;
                    let mut np = (*path).clone();
                    np.push((pos, os));
                    heap.push(HeapEntry {
                        neg_f: -(ng_s + hn),
                        g_scaled: ng_s,
                        neg_seq: seq,
                        pos,
                        side: os,
                        dir: (0, 0),
                        path: Arc::new(np),
                    });
                }
            }
        }
    }

    None
}
