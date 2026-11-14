// Walking: path following with eased turning, plus the occupied-cell rule.
// Pure logic, no Three.js import.
//
// Yaw convention matches layout.js: a character at yaw `r` faces (sin r, cos r)
// in (x, z), so yaw 0 faces +z.

import { findPath, smoothPath, hasLineOfSight, nearestFreeCell } from "./pathfinding.js";

export const WALK_SPEED = 1.2;                 // m/s
export const MAX_TURN_RATE = 2 * Math.PI;      // rad/s, 360 degrees per second
export const OCCUPIED_WAIT_SECONDS = 1;

const TURN_GAIN = 10;          // 1/s, how hard the yaw pulls toward the target (ease-out)
const TURN_RESPONSE = 25;      // 1/s, how fast the turn rate follows its target (ease-in)
const TURN_IN_PLACE = Math.PI / 2;   // do not walk while facing more than 90 degrees off
const MAX_SUBSTEP = 1 / 30;

const TWO_PI = Math.PI * 2;

// Wrap to (-PI, PI].
export function normalizeAngle(a) {
  let r = a % TWO_PI;
  if (r > Math.PI) r -= TWO_PI;
  else if (r <= -Math.PI) r += TWO_PI;
  return r;
}

// Shortest signed angle from `from` to `to`.
export function angleDelta(to, from) {
  return normalizeAngle(to - from);
}

export class Walker {
  constructor({ x = 0, z = 0, yaw = 0, speed = WALK_SPEED, maxTurnRate = MAX_TURN_RATE } = {}) {
    this.x = x;
    this.z = z;
    this.yaw = normalizeAngle(yaw);
    this.speed = speed;
    this.maxTurnRate = maxTurnRate;
    this.moving = false;
    this.arrived = false;   // true once the current path has been completed
    this._path = [];
    this._index = 0;
    this._yawVel = 0;
  }

  setPose(x, z, yaw = this.yaw) {
    this.x = x;
    this.z = z;
    this.yaw = normalizeAngle(yaw);
    this._yawVel = 0;
  }

  // Start following `points` ([{x, z}, ...]) from the current position.
  // An empty path counts as already arrived.
  setPath(points) {
    this._path = (points ?? []).map((p) => ({ x: p.x, z: p.z }));
    this._index = 0;
    this._yawVel = 0;
    this.moving = this._path.length > 0;
    this.arrived = this._path.length === 0;
  }

  stop() {
    this._path = [];
    this._index = 0;
    this._yawVel = 0;
    this.moving = false;
    this.arrived = false;
  }

  // Distance left along the current path, in meters.
  get remaining() {
    let d = 0;
    let px = this.x;
    let pz = this.z;
    for (let k = this._index; k < this._path.length; k++) {
      d += Math.hypot(this._path[k].x - px, this._path[k].z - pz);
      px = this._path[k].x;
      pz = this._path[k].z;
    }
    return d;
  }

  update(dt) {
    if (!(dt > 0) || !Number.isFinite(dt) || !this.moving) return;
    let left = dt;
    while (left > 1e-12 && this.moving) {
      const step = Math.min(left, MAX_SUBSTEP);
      this._step(step);
      left -= step;
    }
  }

  _step(dt) {
    // Skip waypoints we are already standing on.
    while (this._index < this._path.length &&
           Math.hypot(this._path[this._index].x - this.x, this._path[this._index].z - this.z) < 1e-9) {
      this._index++;
    }
    if (this._index >= this._path.length) { this._finish(); return; }

    const target = this._path[this._index];
    const desired = Math.atan2(target.x - this.x, target.z - this.z);
    this._turnToward(desired, dt);
    if (Math.abs(angleDelta(desired, this.yaw)) > TURN_IN_PLACE) return;   // turn first, then walk

    let budget = this.speed * dt;
    while (budget > 1e-12 && this._index < this._path.length) {
      const p = this._path[this._index];
      const dx = p.x - this.x;
      const dz = p.z - this.z;
      const d = Math.hypot(dx, dz);
      if (d <= budget) {
        this.x = p.x;
        this.z = p.z;
        budget -= d;
        this._index++;
      } else {
        this.x += (dx / d) * budget;
        this.z += (dz / d) * budget;
        budget = 0;
      }
    }
    if (this._index >= this._path.length) this._finish();
  }

  // Eased turn: the turn rate follows a rate that is proportional to the angle
  // left (so it slows into the target), capped at maxTurnRate, and the actual
  // rate lags that target a little (so it does not start with a jump).
  _turnToward(desired, dt) {
    const delta = angleDelta(desired, this.yaw);
    const wanted = Math.max(-this.maxTurnRate, Math.min(this.maxTurnRate, delta * TURN_GAIN));
    this._yawVel += (wanted - this._yawVel) * (1 - Math.exp(-TURN_RESPONSE * dt));
    const move = this._yawVel * dt;
    if (Math.abs(move) >= Math.abs(delta)) {
      this.yaw = normalizeAngle(desired);
      this._yawVel = 0;
    } else {
      this.yaw = normalizeAngle(this.yaw + move);
    }
  }

  _finish() {
    this._path = [];
    this._index = 0;
    this._yawVel = 0;
    this.moving = false;
    this.arrived = true;
  }
}

// ---- occupancy ----------------------------------------------------------------

const cellKey = (c) => `${c.i},${c.j}`;

// Who stands on, or is walking to, which cell. One id holds at most one cell and a
// cell has at most one id, so two characters can never end on the same cell.
export class OccupancyMap {
  // `clock` returns seconds; used by walkTo when no explicit time is given.
  constructor(clock = () => globalThis.performance.now() / 1000) {
    this.now = clock;
    this._byCell = new Map();   // key -> id
    this._byId = new Map();     // id -> cell
    this._waits = new Map();    // id -> { key, since }
  }

  // True when nobody except `id` holds the cell.
  isFree(cell, id) {
    const owner = this._byCell.get(cellKey(cell));
    return owner === undefined || owner === id;
  }

  ownerOf(cell) {
    return this._byCell.get(cellKey(cell));
  }

  cellOf(id) {
    const c = this._byId.get(id);
    return c ? { i: c.i, j: c.j } : null;
  }

  // Take `cell` for `id`, giving up the cell `id` held before. Returns false (and
  // changes nothing) when someone else holds it.
  reserve(cell, id) {
    if (!this.isFree(cell, id)) return false;
    this.release(id);
    this._byCell.set(cellKey(cell), id);
    this._byId.set(id, { i: cell.i, j: cell.j });
    return true;
  }

  // Give up whatever `id` holds and forget its wait timer.
  release(id) {
    const c = this._byId.get(id);
    if (c) this._byCell.delete(cellKey(c));
    this._byId.delete(id);
    this._waits.delete(id);
  }

  // Wait timer: the time `id` started waiting for `cell`. Starts the timer at
  // `now` if `id` was not already waiting for that cell.
  waitStart(id, cell, now) {
    const key = cellKey(cell);
    const w = this._waits.get(id);
    if (w && w.key === key) return w.since;
    this._waits.set(id, { key, since: now });
    return now;
  }

  clearWait(id) {
    this._waits.delete(id);
  }
}

// Decide where `id` should really go. Returns
//   { status: "free",       x, z, cell }  target cell is available
//   { status: "redirected", x, z, cell }  target stayed occupied for 1 s, this is the
//                                         nearest free neighbor cell (its center)
//   { status: "waiting",    x: null, z: null, cell: null, waited }  keep calling
//                                         with a later `now`
// The caller reserves the returned cell. Free targets keep their exact world point.
// `opts.accept(cell)` can veto neighbor candidates (walkTo uses it for reachability).
export function resolveTarget(grid, occupancy, id, targetWorld, now, opts = {}) {
  const { waitSeconds = OCCUPIED_WAIT_SECONDS, searchRadius = 3, accept = null } = opts;
  const cell = grid.worldToCell(targetWorld.x, targetWorld.z);

  if (occupancy.isFree(cell, id)) {
    occupancy.clearWait(id);
    return { status: "free", x: targetWorld.x, z: targetWorld.z, cell };
  }

  const since = occupancy.waitStart(id, cell, now);
  const waited = now - since;
  if (waited < waitSeconds) return { status: "waiting", x: null, z: null, cell: null, waited };

  const neighbor = nearestFreeCell(grid, cell, searchRadius, (c) =>
    occupancy.isFree(c, id) && (!accept || accept(c)));
  if (!neighbor) return { status: "waiting", x: null, z: null, cell: null, waited };

  occupancy.clearWait(id);
  const w = grid.cellToWorld(neighbor.i, neighbor.j);
  return { status: "redirected", x: w.x, z: w.z, cell: neighbor };
}

// Plan, smooth and start a walk for `walker` to `goalWorld`.
// Returns { status, path } where status is "free" or "redirected" (walking),
// "waiting" (target occupied, call again later) or "unreachable" (nothing changed).
// The goal cell is reserved for `id` as soon as a walk starts.
export function walkTo(walker, grid, occupancy, id, goalWorld, { now = occupancy.now() } = {}) {
  const here = { x: walker.x, z: walker.z };
  const resolved = resolveTarget(grid, occupancy, id, goalWorld, now, {
    accept: (c) => {
      const w = grid.cellToWorld(c.i, c.j);
      return findPath(grid, here, w) !== null;
    },
  });
  if (resolved.status === "waiting") return { status: "waiting", path: null };

  const goal = { x: resolved.x, z: resolved.z };
  const raw = findPath(grid, here, goal);
  if (!raw) return { status: "unreachable", path: null };

  let path = smoothPath(grid, raw);
  // End on the exact goal point (not just its cell center) when that is clear.
  if (path.length >= 2) {
    if (hasLineOfSight(grid, path[path.length - 2], goal)) path[path.length - 1] = goal;
  } else if (hasLineOfSight(grid, here, goal)) {
    path = [goal];
  }
  // Skip the start cell's center when the way to the next point is already clear.
  if (path.length >= 2 && hasLineOfSight(grid, here, path[1])) path.shift();

  occupancy.reserve(resolved.cell, id);
  walker.setPath(path);
  return { status: resolved.status, path };
}
