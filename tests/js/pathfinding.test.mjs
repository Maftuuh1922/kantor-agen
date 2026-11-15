// Run with: node --test tests/js/
// Covers skeleton/office/static/pathfinding.js and walker.js (Requirement 2.4).
import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import path from "node:path";

const STATIC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../skeleton/office/static");
const { layout } = await import(path.join(STATIC, "layout.js"));
const { Grid, findPath, smoothPath, nearestFreeCell } = await import(path.join(STATIC, "pathfinding.js"));
const { Walker, OccupancyMap, resolveTarget, walkTo, angleDelta, WALK_SPEED, MAX_TURN_RATE } =
  await import(path.join(STATIC, "walker.js"));

const grid = new Grid(layout);
const CLEARANCE = layout.clearance;

// ---- helpers ----------------------------------------------------------------

function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function freeCells() {
  const out = [];
  for (let j = 0; j < layout.grid.rows; j++) {
    for (let i = 0; i < layout.grid.cols; i++) {
      if (!layout.isBlockedCell(i, j)) out.push({ i, j });
    }
  }
  return out;
}
const FREE = freeCells();

// A grid from ASCII rows ('#' blocked, anything else free), no obstacle boxes.
function asciiGrid(rows) {
  const cols = rows[0].length;
  const blocked = new Uint8Array(cols * rows.length);
  rows.forEach((row, j) => [...row].forEach((ch, i) => { blocked[j * cols + i] = ch === "#" ? 1 : 0; }));
  return new Grid({ grid: { cols, rows: rows.length, size: 0.5, originX: 0, originZ: 0, blocked }, obstacles: [] });
}

// Independent reachability check: BFS, 8 neighbors, no corner cutting.
function reachable(a, b) {
  const key = (c) => c.j * layout.grid.cols + c.i;
  const seen = new Set([key(a)]);
  const queue = [a];
  while (queue.length) {
    const c = queue.shift();
    if (c.i === b.i && c.j === b.j) return true;
    for (let dj = -1; dj <= 1; dj++) {
      for (let di = -1; di <= 1; di++) {
        if (!di && !dj) continue;
        const n = { i: c.i + di, j: c.j + dj };
        if (layout.isBlockedCell(n.i, n.j) || seen.has(key(n))) continue;
        if (di && dj && (layout.isBlockedCell(c.i + di, c.j) || layout.isBlockedCell(c.i, c.j + dj))) continue;
        seen.add(key(n));
        queue.push(n);
      }
    }
  }
  return false;
}

const inside = (o, x, z, grow) =>
  x > o.minX - grow && x < o.maxX + grow && z > o.minZ - grow && z < o.maxZ + grow;

// Walk the polyline in 2 cm steps; fail if any sample is in a blocked cell, inside
// an obstacle grown by the character radius, or inside a wall.
function assertPolylineClear(points, label) {
  for (let k = 0; k + 1 < points.length; k++) {
    const a = points[k];
    const b = points[k + 1];
    const n = Math.max(1, Math.ceil(Math.hypot(b.x - a.x, b.z - a.z) / 0.02));
    for (let s = 0; s <= n; s++) {
      const x = a.x + ((b.x - a.x) * s) / n;
      const z = a.z + ((b.z - a.z) * s) / n;
      if (layout.isBlocked(x, z)) assert.fail(`${label}: (${x.toFixed(3)}, ${z.toFixed(3)}) is in a blocked cell`);
      for (const o of layout.obstacles) {
        if (inside(o, x, z, CLEARANCE - 1e-6)) {
          assert.fail(`${label}: (${x.toFixed(3)}, ${z.toFixed(3)}) is inside ${o.kind} grown by the body radius`);
        }
      }
      for (const w of layout.walls) {
        if (inside(w, x, z, 0)) assert.fail(`${label}: (${x.toFixed(3)}, ${z.toFixed(3)}) crosses wall ${w.name}`);
      }
    }
  }
}

function pathLength(points) {
  let d = 0;
  for (let k = 0; k + 1 < points.length; k++) d += Math.hypot(points[k + 1].x - points[k].x, points[k + 1].z - points[k].z);
  return d;
}

function run(walker, dt, maxSeconds) {
  let t = 0;
  while (walker.moving && t < maxSeconds) { walker.update(dt); t += dt; }
  return t;
}

// ---- A* ---------------------------------------------------------------------

test("simple path along an open row is straight and ends on the goal cell", () => {
  const start = { x: 0.25, z: -4.75 };
  const goal = { x: 3.25, z: -4.75 };
  const raw = findPath(grid, start, goal);
  assert.ok(raw);
  assert.equal(raw.length, 7);
  assert.deepEqual(raw[0], grid.cellToWorld(...Object.values(grid.worldToCell(start.x, start.z))));
  assert.deepEqual(raw.at(-1), { x: 3.25, z: -4.75 });
  const smooth = smoothPath(grid, raw);
  assert.equal(smooth.length, 2);
  assert.ok(Math.abs(pathLength(smooth) - 3) < 1e-9);
});

test("path goes around a desk", () => {
  const desk = layout.desks[0];
  const start = { x: desk.standSpot.x, z: desk.standSpot.z };   // behind desk 0
  const goal = { x: desk.position.x, z: desk.position.z + 1.3 }; // in front of it
  const direct = Math.hypot(goal.x - start.x, goal.z - start.z);
  const raw = findPath(grid, start, goal);
  assert.ok(raw);
  const smooth = smoothPath(grid, raw);
  assert.ok(smooth.length >= 3, "a detour needs at least one corner");
  assert.ok(pathLength(smooth) > direct + 0.5, "the detour must be longer than the straight line");
  assertPolylineClear(raw, "raw");
  assertPolylineClear(smooth, "smooth");
});

test("unreachable, blocked, out-of-grid and invalid goals return null", () => {
  const sealed = asciiGrid([
    "#######",
    "#.....#",
    "#.###.#",
    "#.#.#.#",
    "#.###.#",
    "#.....#",
    "#######",
  ]);
  const outside = sealed.cellToWorld(1, 1);
  const pocket = sealed.cellToWorld(3, 3);
  assert.equal(findPath(sealed, outside, pocket), null);
  assert.equal(findPath(sealed, pocket, outside), null);
  assert.ok(findPath(sealed, outside, sealed.cellToWorld(5, 5)));
  assert.equal(findPath(sealed, outside, sealed.cellToWorld(2, 2)), null, "blocked goal cell");
  assert.equal(findPath(grid, { x: 0, z: 0 }, { x: 99, z: 99 }), null, "outside the grid");
  assert.equal(findPath(grid, { x: 0, z: 0 }, { x: NaN, z: 0 }), null, "NaN goal");
});

test("allowDiagonal false only moves along axes", () => {
  const raw = findPath(grid, { x: 0.25, z: -4.75 }, { x: 2.25, z: -4.25 }, { allowDiagonal: false });
  assert.ok(raw);
  assert.equal(raw.length, 6, "4 steps along x plus 1 along z");
  assert.ok(findPath(grid, { x: 0.25, z: -4.75 }, { x: 2.25, z: -4.25 }).length < raw.length, "diagonals shorten it");
  for (let k = 0; k + 1 < raw.length; k++) {
    const dx = Math.abs(raw[k + 1].x - raw[k].x);
    const dz = Math.abs(raw[k + 1].z - raw[k].z);
    assert.ok(dx < 1e-9 || dz < 1e-9);
  }
});

test("a path never cuts a corner between two blocked cells", () => {
  // The only gap is diagonal; with no corner cutting there is no way through.
  const g = asciiGrid([
    "#####",
    "#.#.#",
    "##.##",
    "#.#.#",
    "#####",
  ]);
  assert.equal(findPath(g, g.cellToWorld(1, 1), g.cellToWorld(3, 3)), null);
});

test("a start inside a blocked cell snaps to the nearest free cell", () => {
  const desk = layout.desks[0];
  assert.ok(layout.isBlocked(desk.seat.x, desk.seat.z), "the seat cell is blocked");
  const goal = layout.desks[1].standSpot;
  const raw = findPath(grid, desk.seat, goal);
  assert.ok(raw, "snapped start must still find a path");
  assert.ok(!layout.isBlocked(raw[0].x, raw[0].z));
  assert.ok(Math.hypot(raw[0].x - desk.seat.x, raw[0].z - desk.seat.z) <= 1.0);
  assertPolylineClear(raw, "snapped");
  // Fully enclosed start: nothing free within the radius.
  const walled = asciiGrid(["#####", "#####", "#####", "#####", "#####"]);
  assert.equal(findPath(walled, { x: 1.25, z: 1.25 }, { x: 0.25, z: 0.25 }), null);
});

test("nearestFreeCell returns the closest free cell, or null beyond maxRadius", () => {
  const g = asciiGrid(["#####", "#####", "##.##", "#####", "#####"]);
  assert.deepEqual(nearestFreeCell(g, { i: 0, j: 0 }, 4), { i: 2, j: 2 });
  assert.equal(nearestFreeCell(g, { i: 0, j: 0 }, 1), null);
  assert.deepEqual(nearestFreeCell(g, { i: 2, j: 2 }, 0), { i: 2, j: 2 });
  assert.equal(nearestFreeCell(g, { i: 2, j: 2 }, 1, () => false), null);
});

// ---- many random paths -----------------------------------------------------

test("600 random start/goal pairs: null iff unreachable, and no path touches a blocked cell or wall", () => {
  const rng = mulberry32(20240607);
  const pick = () => FREE[Math.floor(rng() * FREE.length)];
  let found = 0;
  let shorter = 0;
  for (let n = 0; n < 600; n++) {
    const a = pick();
    const b = pick();
    const start = layout.cellToWorld(a.i, a.j);
    const goal = layout.cellToWorld(b.i, b.j);
    const raw = findPath(grid, start, goal);
    assert.equal(raw !== null, reachable(a, b), `pair ${n}: (${a.i},${a.j}) -> (${b.i},${b.j})`);
    if (!raw) continue;
    found++;
    assert.deepEqual(raw[0], start);
    assert.deepEqual(raw.at(-1), goal);
    assertPolylineClear(raw, `raw pair ${n}`);

    const smooth = smoothPath(grid, raw);
    assert.deepEqual(smooth[0], start);
    assert.deepEqual(smooth.at(-1), goal);
    assertPolylineClear(smooth, `smooth pair ${n}`);
    assert.ok(pathLength(smooth) <= pathLength(raw) + 1e-9, "smoothing must not lengthen the path");
    if (smooth.length < raw.length) shorter++;
  }
  assert.ok(found >= 400, `expected most pairs to connect, got ${found}`);
  assert.ok(shorter > 100, `smoothing should drop points on many paths, got ${shorter}`);
});

test("paths from the doorway to every stand, visit, pantry and meeting spot are clear", () => {
  const spots = [
    ...layout.desks.flatMap((d) => [d.standSpot, d.visitSpot]),
    layout.pantrySpot,
    ...layout.meetingSpots.map((m) => m.standSpot),
  ];
  const door = layout.doorSpot;
  for (const [n, s] of spots.entries()) {
    const raw = findPath(grid, door, s);
    assert.ok(raw, `spot ${n} reachable`);
    const smooth = smoothPath(grid, raw);
    assertPolylineClear(smooth, `spot ${n}`);
    assert.ok(findPath(grid, s, door), `spot ${n} back to the door`);
  }
});

// ---- walker -----------------------------------------------------------------

test("walker covers 1.2 m/s (within 2%) for several frame rates, corners included", () => {
  assert.equal(WALK_SPEED, 1.2);
  const points = [{ x: 0, z: 2 }, { x: 1.5, z: 3.5 }, { x: 1.5, z: 8 }];   // 45 degree corners
  for (const dt of [1 / 60, 1 / 30, 0.1]) {
    const w = new Walker({ x: 0, z: 0, yaw: 0 });
    w.setPath(points);
    let traveled = 0;
    let t = 0;
    while (t < 4 - 1e-9) {
      const x = w.x;
      const z = w.z;
      w.update(dt);
      traveled += Math.hypot(w.x - x, w.z - z);
      t += dt;
    }
    assert.ok(w.moving, "path is long enough to still be walking");
    const speed = traveled / t;
    assert.ok(Math.abs(speed - 1.2) / 1.2 < 0.02, `dt=${dt}: measured ${speed.toFixed(4)} m/s`);
  }
});

test("walker speed is configurable and the walker arrives exactly on the last point", () => {
  const w = new Walker({ speed: 2.4 });
  w.setPath([{ x: 0, z: 4.8 }]);
  const t = run(w, 1 / 60, 10);
  assert.ok(Math.abs(t - 2) < 0.05);
  assert.equal(w.arrived, true);
  assert.equal(w.moving, false);
  assert.equal(w.x, 0);
  assert.equal(w.z, 4.8);
});

test("walker state flags: setPath, empty path, stop", () => {
  const w = new Walker();
  assert.equal(w.moving, false);
  w.setPath([{ x: 0, z: 3 }]);
  assert.equal(w.moving, true);
  assert.equal(w.arrived, false);
  w.update(0.5);
  w.stop();
  assert.equal(w.moving, false);
  assert.equal(w.arrived, false);
  const z = w.z;
  w.update(1);
  assert.equal(w.z, z, "a stopped walker stays put");
  w.setPath([]);
  assert.equal(w.arrived, true);
});

test("yaw eases toward the heading instead of snapping", () => {
  const dt = 1 / 60;
  const w = new Walker({ x: 0, z: 0, yaw: 0 });
  w.setPath([{ x: -6, z: 0 }]);   // heading -x is yaw -PI/2
  const target = -Math.PI / 2;
  const trace = [];
  let prev = w.yaw;
  let maxStep = 0;
  for (let n = 0; n < 120 && w.moving; n++) {
    w.update(dt);
    trace.push(w.yaw);
    maxStep = Math.max(maxStep, Math.abs(angleDelta(w.yaw, prev)));
    prev = w.yaw;
  }
  assert.ok(Math.abs(trace[0]) < Math.abs(target) * 0.2, "first frame must not jump to the target");
  assert.ok(trace.filter((y) => y > target + 0.01).length >= 5, "the turn takes several frames");
  assert.ok(maxStep <= MAX_TURN_RATE * dt + 1e-9, `turn rate cap exceeded: ${maxStep}`);
  for (let k = 1; k < trace.length; k++) assert.ok(trace[k] <= trace[k - 1] + 1e-12, "monotonic, no overshoot");
  assert.ok(Math.abs(trace.at(-1) - target) < 1e-6, "ends facing the heading");
});

test("yaw turns the short way across the +-PI seam, and 180 degree turns stay under the rate cap", () => {
  const dt = 1 / 60;
  const w = new Walker({ x: 0, z: 0, yaw: 3.0 });
  w.setPath([{ x: Math.sin(-3.0) * 3, z: Math.cos(-3.0) * 3 }]);
  let total = 0;
  let prev = w.yaw;
  for (let n = 0; n < 30; n++) {
    w.update(dt);
    const step = Math.abs(angleDelta(w.yaw, prev));
    assert.ok(step <= MAX_TURN_RATE * dt + 1e-9);
    total += step;
    prev = w.yaw;
  }
  assert.ok(total < 0.3, `should turn through PI (0.283 rad), turned ${total}`);

  const back = new Walker({ x: 0, z: 0, yaw: 0 });
  back.setPath([{ x: 0, z: -4 }]);   // directly behind
  let frames = 0;
  let last = back.yaw;
  while (back.moving && frames < 600) {
    back.update(dt);
    assert.ok(Math.abs(angleDelta(back.yaw, last)) <= MAX_TURN_RATE * dt + 1e-9);
    last = back.yaw;
    frames++;
  }
  assert.ok(back.arrived);
  assert.ok(Math.abs(Math.abs(back.yaw) - Math.PI) < 1e-6, "faces -z at the end");
});

// ---- occupancy ---------------------------------------------------------------

test("OccupancyMap: reserve, isFree, release", () => {
  const occ = new OccupancyMap(() => 0);
  const cell = { i: 5, j: 5 };
  assert.equal(occ.isFree(cell, "a"), true);
  assert.equal(occ.reserve(cell, "a"), true);
  assert.equal(occ.isFree(cell, "a"), true, "the owner sees its own cell as free");
  assert.equal(occ.isFree(cell, "b"), false);
  assert.equal(occ.reserve(cell, "b"), false);
  assert.equal(occ.ownerOf(cell), "a");
  assert.equal(occ.reserve({ i: 6, j: 5 }, "a"), true, "moving to a new cell frees the old one");
  assert.equal(occ.isFree(cell, "b"), true);
  occ.release("a");
  assert.equal(occ.isFree({ i: 6, j: 5 }, "b"), true);
  assert.equal(occ.cellOf("a"), null);
});

test("resolveTarget waits 1 s, then picks the nearest free neighbor", () => {
  const occ = new OccupancyMap(() => 0);
  const target = layout.desks[2].standSpot;
  const cell = grid.worldToCell(target.x, target.z);
  occ.reserve(cell, "a");

  const free = resolveTarget(grid, occ, "a", target, 0);
  assert.equal(free.status, "free");
  assert.equal(free.x, target.x);

  const t0 = 10;
  assert.equal(resolveTarget(grid, occ, "b", target, t0).status, "waiting");
  assert.equal(resolveTarget(grid, occ, "b", target, t0 + 0.9).status, "waiting");
  const r = resolveTarget(grid, occ, "b", target, t0 + 1.0);
  assert.equal(r.status, "redirected");
  assert.ok(!(r.cell.i === cell.i && r.cell.j === cell.j));
  assert.ok(!layout.isBlockedCell(r.cell.i, r.cell.j));
  assert.ok(Math.max(Math.abs(r.cell.i - cell.i), Math.abs(r.cell.j - cell.j)) <= 1, "a direct neighbor is free here");

  // The wait restarts when the target changes.
  const other = layout.desks[3].standSpot;
  occ.reserve(grid.worldToCell(other.x, other.z), "c");
  assert.equal(resolveTarget(grid, occ, "b", other, t0 + 5).status, "waiting");
});

test("two walkers sent to the same spot end on different cells", () => {
  const occ = new OccupancyMap(() => 0);
  const goal = layout.desks[0].standSpot;
  const a = new Walker({ x: 0.25, z: -4.75 });
  const b = new Walker({ x: 1.75, z: -4.75 });

  const ra = walkTo(a, grid, occ, "a", goal, { now: 0 });
  assert.equal(ra.status, "free");
  assert.ok(a.moving);

  assert.equal(walkTo(b, grid, occ, "b", goal, { now: 0 }).status, "waiting");
  assert.equal(b.moving, false, "the second walker waits in place");
  assert.equal(walkTo(b, grid, occ, "b", goal, { now: 0.5 }).status, "waiting");
  const rb = walkTo(b, grid, occ, "b", goal, { now: 1.0 });
  assert.equal(rb.status, "redirected");
  assert.ok(b.moving);

  run(a, 1 / 30, 30);
  run(b, 1 / 30, 30);
  assert.ok(a.arrived && b.arrived);
  const ca = grid.worldToCell(a.x, a.z);
  const cb = grid.worldToCell(b.x, b.z);
  assert.notDeepEqual(ca, cb);
  assert.ok(!grid.isBlocked(cb.i, cb.j));
  assert.deepEqual(occ.cellOf("a"), ca);
  assert.deepEqual(occ.cellOf("b"), cb);
});

test("walkTo: smooth path, arrives at the goal point, reports unreachable goals", () => {
  const occ = new OccupancyMap(() => 0);
  const w = new Walker({ x: layout.doorSpot.x, z: layout.doorSpot.z });
  const goal = layout.pantrySpot;
  const r = walkTo(w, grid, occ, "w", goal, { now: 0 });
  assert.equal(r.status, "free");
  assertPolylineClear([{ x: w.x, z: w.z }, ...r.path], "walkTo");
  run(w, 1 / 60, 60);
  assert.ok(w.arrived);
  assert.ok(Math.hypot(w.x - goal.x, w.z - goal.z) < 1e-9, "ends on the exact goal point");

  const bad = walkTo(w, grid, occ, "w", { x: 0.0, z: 3.4 }, { now: 0 });   // inside the meeting table
  assert.equal(bad.status, "unreachable");
  assert.equal(w.arrived, true, "an unreachable request leaves the walker untouched");
});

test("a walker that slid onto a seat walks out to the stand spot", () => {
  const occ = new OccupancyMap(() => 0);
  const desk = layout.desks[5];
  const w = new Walker({ x: desk.seat.x, z: desk.seat.z });
  const r = walkTo(w, grid, occ, "w", desk.standSpot, { now: 0 });
  assert.equal(r.status, "free");
  run(w, 1 / 60, 30);
  assert.ok(w.arrived);
  assert.ok(Math.hypot(w.x - desk.standSpot.x, w.z - desk.standSpot.z) < 0.36);
});
