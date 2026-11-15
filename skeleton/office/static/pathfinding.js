// Grid pathfinding for the office floor: pure logic, no Three.js import.
//
// A `Grid` wraps the blocked-cell data exported by layout.js (0.5 m cells,
// cells whose center lies within CLEARANCE of furniture or a wall are blocked).
// `findPath` runs A* over free cells and returns world-space cell centers.
// `smoothPath` drops waypoints that have a clear line of sight to each other.
//
// All coordinates are meters: x to the right, z toward the camera. Cell indexes
// are `{ i, j }` with i along x and j along z.

export const DEFAULT_RADIUS = 0.25;

const SQRT2 = Math.SQRT2;
const GEOMETRY_EPS = 1e-6;

export class Grid {
  // `layout` is the object exported by layout.js (or anything with the same
  // `grid`, `clearance` and `obstacles` fields).
  constructor(layout) {
    const g = layout.grid;
    this.cols = g.cols;
    this.rows = g.rows;
    this.size = g.size;
    this.originX = g.originX;
    this.originZ = g.originZ;
    this.blocked = g.blocked;
    this.clearance = layout.clearance ?? DEFAULT_RADIUS;
    this.obstacles = layout.obstacles ?? [];
  }

  inBounds(i, j) {
    return Number.isInteger(i) && Number.isInteger(j) && i >= 0 && j >= 0 && i < this.cols && j < this.rows;
  }

  // Cell coordinates in, like layout.isBlockedCell. Outside the grid is blocked.
  isBlocked(i, j) {
    return !this.inBounds(i, j) || this.blocked[j * this.cols + i] === 1;
  }

  worldToCell(x, z) {
    return { i: Math.floor((x - this.originX) / this.size), j: Math.floor((z - this.originZ) / this.size) };
  }

  cellToWorld(i, j) {
    return { x: this.originX + (i + 0.5) * this.size, z: this.originZ + (j + 0.5) * this.size };
  }

  isBlockedWorld(x, z) {
    const { i, j } = this.worldToCell(x, z);
    return this.isBlocked(i, j);
  }
}

// ---- nearest free cell ------------------------------------------------------

// Nearest free cell to `cell` within `maxRadius` cells (Chebyshev), by Euclidean
// distance. Ties break by row, then column, so results are deterministic.
// `accept(cell)` is an optional extra filter (e.g. "not reserved by someone else").
// Returns `{ i, j }` or null.
export function nearestFreeCell(grid, cell, maxRadius = 4, accept = null) {
  const r = Math.max(0, Math.floor(maxRadius));
  const candidates = [];
  for (let dj = -r; dj <= r; dj++) {
    for (let di = -r; di <= r; di++) {
      const i = cell.i + di;
      const j = cell.j + dj;
      if (!grid.isBlocked(i, j)) candidates.push({ i, j, d2: di * di + dj * dj });
    }
  }
  candidates.sort((a, b) => a.d2 - b.d2 || a.j - b.j || a.i - b.i);
  for (const c of candidates) {
    const out = { i: c.i, j: c.j };
    if (!accept || accept(out)) return out;
  }
  return null;
}

// ---- A* ---------------------------------------------------------------------

class MinHeap {
  constructor() {
    this.f = [];
    this.n = [];   // insertion order, makes ties deterministic
    this.v = [];
    this.counter = 0;
  }
  get size() { return this.v.length; }
  _less(a, b) {
    return this.f[a] < this.f[b] || (this.f[a] === this.f[b] && this.n[a] < this.n[b]);
  }
  _swap(a, b) {
    [this.f[a], this.f[b]] = [this.f[b], this.f[a]];
    [this.n[a], this.n[b]] = [this.n[b], this.n[a]];
    [this.v[a], this.v[b]] = [this.v[b], this.v[a]];
  }
  push(f, value) {
    this.f.push(f); this.n.push(this.counter++); this.v.push(value);
    let k = this.v.length - 1;
    while (k > 0) {
      const p = (k - 1) >> 1;
      if (!this._less(k, p)) break;
      this._swap(k, p);
      k = p;
    }
  }
  pop() {
    const top = this.v[0];
    const last = this.v.length - 1;
    this._swap(0, last);
    this.f.pop(); this.n.pop(); this.v.pop();
    let k = 0;
    const len = this.v.length;
    for (;;) {
      const l = 2 * k + 1;
      const r = l + 1;
      let m = k;
      if (l < len && this._less(l, m)) m = l;
      if (r < len && this._less(r, m)) m = r;
      if (m === k) break;
      this._swap(k, m);
      k = m;
    }
    return top;
  }
}

const NEIGHBORS_4 = [[1, 0, 1], [-1, 0, 1], [0, 1, 1], [0, -1, 1]];
const NEIGHBORS_8 = [...NEIGHBORS_4, [1, 1, SQRT2], [1, -1, SQRT2], [-1, 1, SQRT2], [-1, -1, SQRT2]];

function octile(di, dj) {
  const dx = Math.abs(di);
  const dz = Math.abs(dj);
  return dx + dz + (SQRT2 - 2) * Math.min(dx, dz);
}

function isPoint(p) {
  return p && Number.isFinite(p.x) && Number.isFinite(p.z);
}

// Plan a path between two world points. Returns world-space cell centers from
// the start cell to the goal cell, or null when the goal is blocked, outside the
// grid, or unreachable. A blocked start cell (a character that slid onto a seat)
// is first moved to the nearest free cell.
export function findPath(grid, startWorld, goalWorld, { allowDiagonal = true, snapRadius = 4 } = {}) {
  if (!isPoint(startWorld) || !isPoint(goalWorld)) return null;

  let start = grid.worldToCell(startWorld.x, startWorld.z);
  if (grid.isBlocked(start.i, start.j)) {
    start = nearestFreeCell(grid, start, snapRadius);
    if (!start) return null;
  }
  const goal = grid.worldToCell(goalWorld.x, goalWorld.z);
  if (grid.isBlocked(goal.i, goal.j)) return null;

  const cols = grid.cols;
  const total = cols * grid.rows;
  const startIdx = start.j * cols + start.i;
  const goalIdx = goal.j * cols + goal.i;

  const moves = allowDiagonal ? NEIGHBORS_8 : NEIGHBORS_4;
  const h = allowDiagonal
    ? (i, j) => octile(i - goal.i, j - goal.j)
    : (i, j) => Math.abs(i - goal.i) + Math.abs(j - goal.j);

  const g = new Float64Array(total).fill(Infinity);
  const parent = new Int32Array(total).fill(-1);
  const closed = new Uint8Array(total);
  const open = new MinHeap();
  g[startIdx] = 0;
  open.push(h(start.i, start.j), startIdx);

  let found = startIdx === goalIdx;
  while (!found && open.size > 0) {
    const idx = open.pop();
    if (closed[idx]) continue;
    closed[idx] = 1;
    if (idx === goalIdx) { found = true; break; }
    const ci = idx % cols;
    const cj = (idx - ci) / cols;
    for (const [di, dj, cost] of moves) {
      const ni = ci + di;
      const nj = cj + dj;
      if (grid.isBlocked(ni, nj)) continue;
      // No corner cutting: a diagonal step needs both side cells free.
      if (di !== 0 && dj !== 0 && (grid.isBlocked(ci + di, cj) || grid.isBlocked(ci, cj + dj))) continue;
      const nIdx = nj * cols + ni;
      if (closed[nIdx]) continue;
      const ng = g[idx] + cost;
      if (ng < g[nIdx]) {
        g[nIdx] = ng;
        parent[nIdx] = idx;
        open.push(ng + h(ni, nj), nIdx);
      }
    }
  }
  if (!found) return null;

  const path = [];
  for (let idx = goalIdx; idx !== -1; idx = parent[idx]) {
    const i = idx % cols;
    path.push(grid.cellToWorld(i, (idx - i) / cols));
  }
  return path.reverse();
}

// ---- line of sight ----------------------------------------------------------

// Visit every cell the segment a->b touches (supercover: when the segment passes
// exactly through a cell corner, both side cells are visited). `visit(i, j)`
// returns false to stop early. Returns false if stopped.
function traverseCells(grid, ax, az, bx, bz, visit) {
  const s = grid.size;
  const au = (ax - grid.originX) / s;
  const av = (az - grid.originZ) / s;
  const bu = (bx - grid.originX) / s;
  const bv = (bz - grid.originZ) / s;
  let i = Math.floor(au);
  let j = Math.floor(av);
  if (visit(i, j) === false) return false;

  const du = bu - au;
  const dv = bv - av;
  const stepI = du > 0 ? 1 : -1;
  const stepJ = dv > 0 ? 1 : -1;
  const tDeltaU = du !== 0 ? 1 / Math.abs(du) : Infinity;
  const tDeltaV = dv !== 0 ? 1 / Math.abs(dv) : Infinity;
  let tMaxU = du > 0 ? (i + 1 - au) * tDeltaU : du < 0 ? (au - i) * tDeltaU : Infinity;
  let tMaxV = dv > 0 ? (j + 1 - av) * tDeltaV : dv < 0 ? (av - j) * tDeltaV : Infinity;

  const limit = 2 * (grid.cols + grid.rows) + 8;   // guards against NaN or absurd input
  for (let n = 0; n < limit; n++) {
    const t = Math.min(tMaxU, tMaxV);
    if (t >= 1 - 1e-9) break;
    if (Math.abs(tMaxU - tMaxV) < 1e-9) {
      if (visit(i + stepI, j) === false) return false;
      if (visit(i, j + stepJ) === false) return false;
      i += stepI; j += stepJ;
      tMaxU += tDeltaU; tMaxV += tDeltaV;
    } else if (tMaxU < tMaxV) {
      i += stepI; tMaxU += tDeltaU;
    } else {
      j += stepJ; tMaxV += tDeltaV;
    }
    if (visit(i, j) === false) return false;
  }
  return visit(Math.floor(bu), Math.floor(bv)) !== false;
}

// Does the closed segment a->b hit the interior of an obstacle grown by the
// character radius? This is the exact version of "the body stays out of furniture".
function segmentHitsObstacle(grid, a, b) {
  const grow = grid.clearance - GEOMETRY_EPS;
  const dx = b.x - a.x;
  const dz = b.z - a.z;
  for (const o of grid.obstacles) {
    let t0 = 0;
    let t1 = 1;
    const slabs = [
      [a.x, dx, o.minX - grow, o.maxX + grow],
      [a.z, dz, o.minZ - grow, o.maxZ + grow],
    ];
    let hit = true;
    for (const [p, d, lo, hi] of slabs) {
      if (Math.abs(d) < 1e-12) {
        if (p < lo || p > hi) { hit = false; break; }
      } else {
        let ta = (lo - p) / d;
        let tb = (hi - p) / d;
        if (ta > tb) [ta, tb] = [tb, ta];
        t0 = Math.max(t0, ta);
        t1 = Math.min(t1, tb);
        if (t0 > t1) { hit = false; break; }
      }
    }
    if (hit) return true;
  }
  return false;
}

// Can a character of the given radius walk the straight segment a->b?
// Checks (1) every cell the center line touches is free, (2) points offset to
// both sides by ~the radius, sampled along the segment, land in free cells, and
// (3) the segment stays out of every obstacle grown by the clearance.
export function hasLineOfSight(grid, a, b, radius = DEFAULT_RADIUS) {
  if (!isPoint(a) || !isPoint(b)) return false;
  const ok = traverseCells(grid, a.x, a.z, b.x, b.z, (i, j) => !grid.isBlocked(i, j));
  if (!ok) return false;

  const dx = b.x - a.x;
  const dz = b.z - a.z;
  const len = Math.hypot(dx, dz);
  if (len > 1e-9) {
    // Slightly under the radius so an axis-aligned run along cell centers stays
    // inside its own row of cells instead of landing on the cell boundary.
    const off = radius * 0.9;
    const nx = -dz / len;
    const nz = dx / len;
    const steps = Math.max(1, Math.ceil(len / (grid.size / 4)));
    for (let k = 0; k <= steps; k++) {
      const px = a.x + (dx * k) / steps;
      const pz = a.z + (dz * k) / steps;
      if (grid.isBlockedWorld(px + nx * off, pz + nz * off)) return false;
      if (grid.isBlockedWorld(px - nx * off, pz - nz * off)) return false;
    }
  }
  return !segmentHitsObstacle(grid, a, b);
}

// Remove intermediate waypoints that have a clear line of sight. Greedy: from the
// current anchor, jump to the farthest waypoint that can be seen. The first and
// last points are kept. Returns a new array.
export function smoothPath(grid, waypoints, { radius = DEFAULT_RADIUS } = {}) {
  if (!Array.isArray(waypoints) || waypoints.length <= 2) return (waypoints ?? []).map((p) => ({ x: p.x, z: p.z }));
  const out = [waypoints[0]];
  let anchor = 0;
  while (anchor < waypoints.length - 1) {
    let next = anchor + 1;   // adjacent A* steps are valid even if the sampled check is stricter
    for (let k = waypoints.length - 1; k > anchor + 1; k--) {
      if (hasLineOfSight(grid, waypoints[anchor], waypoints[k], radius)) { next = k; break; }
    }
    out.push(waypoints[next]);
    anchor = next;
  }
  return out.map((p) => ({ x: p.x, z: p.z }));
}
