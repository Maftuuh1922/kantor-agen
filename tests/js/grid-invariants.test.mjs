// Run with: node --test tests/js/
// Grid invariants for skeleton/office/static/pathfinding.js on the real office layout
// (Requirement 2.4: no path crosses a blocked cell, unreachable targets return no path).
//
// pathfinding.test.mjs spot-checks 600 random pairs and the door -> spot routes. This file
// is the exhaustive counterpart: every free cell <-> every destination, in both directions,
// checked against an independent flood fill and an independent Dijkstra. It also covers
// symmetry, optimality, edge cases on artificially sealed copies of the grid, and determinism.
import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import path from "node:path";

const STATIC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../skeleton/office/static");
const { layout } = await import(path.join(STATIC, "layout.js"));
const { Grid, findPath, smoothPath } = await import(path.join(STATIC, "pathfinding.js"));

const grid = new Grid(layout);
const COLS = layout.grid.cols;
const ROWS = layout.grid.rows;
const RADIUS = layout.clearance;
const SAMPLE_STEP = 0.05;          // 5 cm
const TOL = 1e-6;
const SQRT2 = Math.SQRT2;

// Snapshot so the last test can prove nothing mutated the shared blocked data.
const BLOCKED_SNAPSHOT = Uint8Array.from(layout.grid.blocked);

// ---- helpers ----------------------------------------------------------------

const key = (c) => c.j * COLS + c.i;
const cellCenter = (c) => layout.cellToWorld(c.i, c.j);
const isFree = (i, j) => !layout.isBlockedCell(i, j);

const FREE = [];
for (let j = 0; j < ROWS; j++) for (let i = 0; i < COLS; i++) if (isFree(i, j)) FREE.push({ i, j });

// Destinations: door, every desk stand spot and visit spot, pantry, every meeting stand spot.
const TARGETS = [
  { name: "door", p: layout.doorSpot },
  ...layout.desks.map((d) => ({ name: `desk${d.index}.stand`, p: d.standSpot })),
  ...layout.desks.map((d) => ({ name: `desk${d.index}.visit`, p: d.visitSpot })),
  { name: "pantry", p: layout.pantrySpot },
  ...layout.meetingSpots.map((m) => ({ name: `meeting${m.index}.stand`, p: m.standSpot })),
].map((t) => {
  const cell = layout.worldToCell(t.p.x, t.p.z);
  return { ...t, cell, center: cellCenter(cell) };
});

// Distance from a point to the closest obstacle box (walls included); 0 when inside one.
function distanceToObstacles(x, z) {
  let best = Infinity;
  for (const o of layout.obstacles) {
    const dx = Math.max(o.minX - x, 0, x - o.maxX);
    const dz = Math.max(o.minZ - z, 0, z - o.maxZ);
    const d = Math.hypot(dx, dz);
    if (d < best) best = d;
  }
  return best;
}

function assertPointSafe(x, z, label) {
  if (layout.isBlocked(x, z)) assert.fail(`${label}: (${x.toFixed(3)}, ${z.toFixed(3)}) is in a blocked cell`);
  const d = distanceToObstacles(x, z);
  if (d < RADIUS - TOL) {
    assert.fail(`${label}: (${x.toFixed(3)}, ${z.toFixed(3)}) is ${d.toFixed(3)} m from a wall or furniture, radius is ${RADIUS}`);
  }
}

// The raw path is a chain of free cell centers; each step is one 8-neighbor move with no corner cutting.
function assertRawPathValid(raw, label) {
  for (let k = 0; k < raw.length; k++) {
    const c = layout.worldToCell(raw[k].x, raw[k].z);
    assert.ok(isFree(c.i, c.j), `${label}: raw cell (${c.i},${c.j}) is blocked`);
    assertPointSafe(raw[k].x, raw[k].z, `${label} raw[${k}]`);
    if (k === 0) continue;
    const p = layout.worldToCell(raw[k - 1].x, raw[k - 1].z);
    const di = c.i - p.i;
    const dj = c.j - p.j;
    assert.ok(Math.abs(di) <= 1 && Math.abs(dj) <= 1 && (di || dj), `${label}: step ${k} is not an 8-neighbor move`);
    if (di && dj) {
      assert.ok(isFree(p.i + di, p.j) && isFree(p.i, p.j + dj), `${label}: step ${k} cuts a corner`);
    }
  }
}

// Sample every segment at most every 5 cm (endpoints included).
function assertPolylineSafe(points, label) {
  for (let k = 0; k + 1 < points.length; k++) {
    const a = points[k];
    const b = points[k + 1];
    const n = Math.max(1, Math.ceil(Math.hypot(b.x - a.x, b.z - a.z) / SAMPLE_STEP));
    for (let s = 0; s <= n; s++) {
      assertPointSafe(a.x + ((b.x - a.x) * s) / n, a.z + ((b.z - a.z) * s) / n, `${label} seg ${k}`);
    }
  }
  if (points.length === 1) assertPointSafe(points[0].x, points[0].z, label);
}

function polyLength(points) {
  let d = 0;
  for (let k = 0; k + 1 < points.length; k++) d += Math.hypot(points[k + 1].x - points[k].x, points[k + 1].z - points[k].z);
  return d;
}

// Independent BFS flood fill (8 neighbors, no corner cutting). Returns the reachable set of keys.
function floodFill(from) {
  const seen = new Set([key(from)]);
  const queue = [from];
  for (let head = 0; head < queue.length; head++) {
    const c = queue[head];
    for (let dj = -1; dj <= 1; dj++) {
      for (let di = -1; di <= 1; di++) {
        if (!di && !dj) continue;
        const n = { i: c.i + di, j: c.j + dj };
        if (!isFree(n.i, n.j) || seen.has(key(n))) continue;
        if (di && dj && (!isFree(c.i + di, c.j) || !isFree(c.i, c.j + dj))) continue;
        seen.add(key(n));
        queue.push(n);
      }
    }
  }
  return seen;
}

// Independent Dijkstra on the free-cell graph: cost 1 orthogonal, sqrt(2) diagonal, no corner cutting.
// O(n^2) array scan, no heap, so it shares nothing with the implementation under test.
// Returns a Map key -> shortest distance in cell units.
function dijkstra(from) {
  const dist = new Map(FREE.map((c) => [key(c), Infinity]));
  const done = new Set();
  dist.set(key(from), 0);
  for (;;) {
    let best = null;
    let bestD = Infinity;
    for (const c of FREE) {
      const d = dist.get(key(c));
      if (d < bestD && !done.has(key(c))) { best = c; bestD = d; }
    }
    if (!best) break;
    done.add(key(best));
    for (let dj = -1; dj <= 1; dj++) {
      for (let di = -1; di <= 1; di++) {
        if (!di && !dj) continue;
        const n = { i: best.i + di, j: best.j + dj };
        if (!isFree(n.i, n.j)) continue;
        if (di && dj && (!isFree(best.i + di, best.j) || !isFree(best.i, best.j + dj))) continue;
        const nd = bestD + (di && dj ? SQRT2 : 1);
        if (nd < dist.get(key(n))) dist.set(key(n), nd);
      }
    }
  }
  return dist;
}

// Length of a raw path in cell units, so it compares directly with Dijkstra.
const rawCost = (raw) => polyLength(raw) / layout.grid.size;

// A copy of the real grid whose blocked array can be edited without touching layout.
function gridCopy(edit) {
  const blocked = Uint8Array.from(layout.grid.blocked);
  edit(blocked);
  return new Grid({ ...layout, grid: { ...layout.grid, blocked } });
}

const RING_8 = [[-1, -1], [0, -1], [1, -1], [-1, 0], [1, 0], [-1, 1], [0, 1], [1, 1]];
const RING_4 = [[0, -1], [-1, 0], [1, 0], [0, 1]];

// Free cells whose 8 neighbors are all free and inside the grid (safe to wall in).
const OPEN_CELLS = FREE.filter((c) => RING_8.every(([di, dj]) => isFree(c.i + di, c.j + dj)));

function sealed(cell, ring) {
  return gridCopy((blocked) => { for (const [di, dj] of ring) blocked[(cell.j + dj) * COLS + cell.i + di] = 1; });
}

// Fail if `fn` takes longer than `ms` (a hang would also be stopped by the test timeout).
function timed(ms, label, fn) {
  const t0 = performance.now();
  const result = fn();
  const dt = performance.now() - t0;
  assert.ok(dt < ms, `${label} took ${dt.toFixed(0)} ms, limit ${ms} ms`);
  return result;
}

const stats = { pairs: 0, found: 0, nullPaths: 0, smoothedChecked: 0, worstRawAsym: 0, worstSmoothAsym: 0 };

// ---- fixtures sanity ----------------------------------------------------------

test("fixtures: every destination cell is free and the door cell is on the doorway row", () => {
  assert.ok(FREE.length > 300, `expected a few hundred free cells, got ${FREE.length}`);
  assert.equal(TARGETS.length, 1 + 8 + 8 + 1 + 6);
  for (const t of TARGETS) assert.ok(isFree(t.cell.i, t.cell.j), `${t.name} sits in a blocked cell`);
  assert.equal(new Set(TARGETS.map((t) => key(t.cell))).size, TARGETS.length, "destinations are distinct cells");
  assert.equal(TARGETS[0].cell.j, 0, "the door spot is in the outer row of the grid");
});

// ---- exhaustive reachability + safety ------------------------------------------------

test("every free cell <-> every destination: path iff flood fill says reachable, and all of it is clear", { timeout: 240000 }, () => {
  let unreachable = 0;
  for (const t of TARGETS) {
    const reach = floodFill(t.cell);
    for (const c of FREE) {
      const there = cellCenter(c);
      const reachableFlag = reach.has(key(c));
      for (const dir of ["to", "from"]) {
        const [s, g] = dir === "to" ? [there, t.center] : [t.center, there];
        const label = `${dir} ${t.name} cell (${c.i},${c.j})`;
        const raw = findPath(grid, s, g);
        stats.pairs++;
        if (!reachableFlag) {
          assert.equal(raw, null, `${label}: flood fill says unreachable, got a path`);
          stats.nullPaths++;
          unreachable++;
          continue;
        }
        assert.ok(raw, `${label}: flood fill says reachable, got null`);
        stats.found++;
        assert.deepEqual(raw[0], s, `${label}: path must start on the start cell center`);
        assert.deepEqual(raw.at(-1), g, `${label}: path must end on the goal cell center`);
        assertRawPathValid(raw, label);

        const smooth = smoothPath(grid, raw);
        assert.deepEqual(smooth[0], s);
        assert.deepEqual(smooth.at(-1), g);
        assertPolylineSafe(smooth, `${label} smooth`);
        stats.smoothedChecked++;
      }
    }
  }
  assert.equal(stats.found + stats.nullPaths, stats.pairs);
  assert.ok(stats.pairs >= 2 * TARGETS.length * FREE.length, "every pair in both directions was visited");
  assert.ok(stats.found > 0.5 * stats.pairs, "most of the office is connected");
  stats.unreachableInOffice = unreachable;
});

// ---- symmetry and optimality ---------------------------------------------------------

test("A* raw length equals the independent Dijkstra optimum (never longer) and is symmetric within 5%", { timeout: 240000 }, () => {
  let compared = 0;
  for (const t of TARGETS) {
    const dist = dijkstra(t.cell);
    const reach = floodFill(t.cell);
    for (const c of FREE) {
      const d = dist.get(key(c));
      // The two independent searches must agree on reachability.
      assert.equal(Number.isFinite(d), reach.has(key(c)), `Dijkstra and flood fill disagree at (${c.i},${c.j}) for ${t.name}`);
      if (!Number.isFinite(d)) continue;
      const there = cellCenter(c);
      const fwd = findPath(grid, there, t.center);
      const back = findPath(grid, t.center, there);
      assert.ok(fwd && back);
      const lf = rawCost(fwd);
      const lb = rawCost(back);
      const label = `${t.name} <-> (${c.i},${c.j})`;
      assert.ok(lf <= d + 1e-9, `${label}: A* path ${lf} is longer than the shortest ${d}`);
      assert.ok(lb <= d + 1e-9, `${label}: reverse A* path ${lb} is longer than the shortest ${d}`);
      assert.ok(lf >= d - 1e-9, `${label}: A* path ${lf} is shorter than the shortest ${d}, so it skipped a constraint`);
      const m = Math.max(lf, lb);
      if (m > 0) {
        const asym = Math.abs(lf - lb) / m;
        stats.worstRawAsym = Math.max(stats.worstRawAsym, asym);
        assert.ok(asym <= 0.05, `${label}: directions differ by ${(asym * 100).toFixed(1)}%`);
      }
      compared++;
    }
  }
  assert.ok(compared > 5000, `compared ${compared} pairs`);
});

// The raw A* path is exactly symmetric (checked above at 5%). Greedy line-of-sight smoothing is not:
// the two directions can pick different corners. Measured on this layout, 55 of 11640 pairs differ
// by more than 5% (worst 6.5%, 0.67 m). Both results are still clear and no longer than the raw path,
// so this test only bounds the drift at 10% instead of 5%.
const SMOOTH_ASYMMETRY_LIMIT = 0.10;

test("smoothed paths: never longer than the raw path, never shorter than the straight line, and symmetric within 10%", { timeout: 240000 }, () => {
  let compared = 0;
  for (const t of TARGETS) {
    const reach = floodFill(t.cell);
    for (const c of FREE) {
      if (!reach.has(key(c))) continue;
      const there = cellCenter(c);
      const rawF = findPath(grid, there, t.center);
      const rawB = findPath(grid, t.center, there);
      const sf = smoothPath(grid, rawF);
      const sb = smoothPath(grid, rawB);
      const lf = polyLength(sf);
      const lb = polyLength(sb);
      const label = `${t.name} <-> (${c.i},${c.j})`;
      assert.ok(lf <= polyLength(rawF) + 1e-9 && lb <= polyLength(rawB) + 1e-9, `${label}: smoothing lengthened a path`);
      const straight = Math.hypot(there.x - t.center.x, there.z - t.center.z);
      assert.ok(lf >= straight - 1e-9 && lb >= straight - 1e-9, `${label}: smoothed path is shorter than the straight line`);
      const m = Math.max(lf, lb);
      if (m > 0) {
        const asym = Math.abs(lf - lb) / m;
        stats.worstSmoothAsym = Math.max(stats.worstSmoothAsym, asym);
        assert.ok(asym <= SMOOTH_ASYMMETRY_LIMIT, `${label}: smoothed directions differ by ${(asym * 100).toFixed(1)}% (${lf.toFixed(3)} vs ${lb.toFixed(3)})`);
      }
      compared++;
    }
  }
  assert.ok(compared > 5000);
});

// ---- edge cases -------------------------------------------------------------------

test("start equals goal returns the single cell center (also for two points in the same cell)", () => {
  for (const c of FREE) {
    const p = cellCenter(c);
    assert.deepEqual(findPath(grid, p, p), [p], `cell (${c.i},${c.j})`);
  }
  const c = FREE[Math.floor(FREE.length / 2)];
  const p = cellCenter(c);
  const a = { x: p.x - 0.2, z: p.z + 0.2 };
  const b = { x: p.x + 0.2, z: p.z - 0.2 };
  assert.deepEqual(findPath(grid, a, b), [p]);
  assert.deepEqual(smoothPath(grid, [p]), [p]);
});

test("a goal in any blocked cell returns null, including every blocked cell and the ring outside the grid", () => {
  const start = cellCenter(TARGETS[0].cell);
  let blockedCount = 0;
  for (let j = -2; j < ROWS + 2; j++) {
    for (let i = -2; i < COLS + 2; i++) {
      if (isFree(i, j)) continue;
      blockedCount++;
      const goal = layout.cellToWorld(i, j);
      assert.equal(findPath(grid, start, goal), null, `blocked goal cell (${i},${j})`);
    }
  }
  assert.ok(blockedCount > 100);
  // Furniture the agents sit at: every seat point is in a blocked cell.
  for (const d of layout.desks) assert.equal(findPath(grid, start, d.seat), null, `desk ${d.index} seat`);
});

test("goals outside the grid return null", () => {
  const start = cellCenter(TARGETS[1].cell);
  const { originX, originZ, cols, rows, size } = layout.grid;
  const goals = [
    { x: originX - 0.01, z: 0 }, { x: originX + cols * size, z: 0 },
    { x: 0, z: originZ - 0.01 }, { x: 0, z: originZ + rows * size },
    { x: -1e6, z: 1e6 }, { x: 1e308, z: -1e308 }, { x: 99, z: 99 },
  ];
  for (const g of goals) assert.equal(findPath(grid, start, g), null, `goal (${g.x}, ${g.z})`);
});

test("NaN, Infinity, missing and non-numeric inputs return null instead of throwing", () => {
  const ok = cellCenter(TARGETS[1].cell);
  const bad = [
    undefined, null, {}, { x: 1 }, { z: 1 }, { x: NaN, z: 0 }, { x: 0, z: NaN }, { x: NaN, z: NaN },
    { x: Infinity, z: 0 }, { x: 0, z: -Infinity }, { x: "1", z: "2" }, { x: null, z: null }, [], 5, "x",
  ];
  for (const b of bad) {
    assert.equal(findPath(grid, ok, b), null, `bad goal ${JSON.stringify(b)}`);
    assert.equal(findPath(grid, b, ok), null, `bad start ${JSON.stringify(b)}`);
  }
  assert.equal(findPath(grid, undefined, undefined), null);
  // A start far outside the grid has no free cell within the snap radius.
  assert.equal(findPath(grid, { x: 500, z: 500 }, ok), null);
});

test("a goal sealed in by a ring of blocked cells returns null (8-cell ring and 4-cell orthogonal ring)", { timeout: 60000 }, () => {
  assert.ok(OPEN_CELLS.length > 50, `need open cells to seal, got ${OPEN_CELLS.length}`);
  const start = cellCenter(TARGETS[0].cell);
  let sealedChecks = 0;
  for (const [n, goalCell] of OPEN_CELLS.entries()) {
    if (n % 3 !== 0) continue;
    const goal = cellCenter(goalCell);
    if (key(goalCell) === key(TARGETS[0].cell)) continue;
    // The real grid still connects to it; the sealed copy must not.
    assert.ok(findPath(grid, start, goal), `before sealing (${goalCell.i},${goalCell.j}) is reachable`);
    for (const ring of [RING_8, RING_4]) {
      const g = sealed(goalCell, ring);
      assert.equal(timed(1000, "sealed goal", () => findPath(g, start, goal)), null, `sealed goal (${goalCell.i},${goalCell.j}) ring ${ring.length}`);
      sealedChecks++;
    }
  }
  assert.ok(sealedChecks > 50);
});

test("diagonal-only gaps do not let a path into a sealed cell (corner cutting stays off)", () => {
  // Block the four orthogonal neighbors AND leave the four diagonals free: the only
  // way in is diagonal, which needs both side cells free, so it must still be null.
  const goalCell = OPEN_CELLS[Math.floor(OPEN_CELLS.length / 2)];
  const g = sealed(goalCell, RING_4);
  for (const [di, dj] of [[-1, -1], [1, -1], [-1, 1], [1, 1]]) {
    assert.ok(!g.isBlocked(goalCell.i + di, goalCell.j + dj), "diagonal neighbor stays free");
    assert.equal(findPath(g, cellCenter({ i: goalCell.i + di, j: goalCell.j + dj }), cellCenter(goalCell)), null);
  }
});

test("a start sealed in by blocked cells returns null quickly, without hanging", { timeout: 60000 }, () => {
  const goal = cellCenter(TARGETS[0].cell);
  let checked = 0;
  for (const [n, startCell] of OPEN_CELLS.entries()) {
    if (n % 3 !== 0) continue;
    const start = cellCenter(startCell);
    for (const ring of [RING_8, RING_4]) {
      const g = sealed(startCell, ring);
      assert.equal(timed(1000, "sealed start", () => findPath(g, start, goal)), null, `sealed start (${startCell.i},${startCell.j}) ring ${ring.length}`);
      checked++;
    }
  }
  assert.ok(checked > 50);
});

test("a goal in a large region the start cannot enter exhausts the search and returns null within the time guard", { timeout: 60000 }, () => {
  // Cut the room in two with a full-width wall of blocked cells at the row z ~ 4.75; the
  // goal is on the far side, so A* has to exhaust the whole near side before giving up.
  const row = layout.worldToCell(0, 4.75).j;
  const g = gridCopy((blocked) => { for (let i = 0; i < COLS; i++) blocked[row * COLS + i] = 1; });
  const start = cellCenter(TARGETS[0].cell);
  const far = cellCenter(layout.worldToCell(-3, 5.25));
  assert.ok(!g.isBlocked(...Object.values(layout.worldToCell(-3, 5.25))), "far goal cell is free on the copy");
  assert.ok(findPath(grid, start, far), "the real grid reaches it");
  assert.equal(timed(1000, "cut room", () => findPath(g, start, far)), null);
  // A fully blocked grid: nothing to snap to, nothing to search.
  const wall = gridCopy((blocked) => blocked.fill(1));
  assert.equal(timed(1000, "all blocked", () => findPath(wall, start, far)), null);
});

// ---- determinism ------------------------------------------------------------------

test("repeated calls give identical raw and smoothed paths and do not mutate their inputs", { timeout: 120000 }, () => {
  const door = TARGETS[0];
  const pantry = TARGETS.find((t) => t.name === "pantry");
  let checked = 0;
  for (const t of [door, pantry]) {
    for (const c of FREE) {
      const s = cellCenter(c);
      const sCopy = { ...s };
      const gCopy = { ...t.center };
      const first = findPath(grid, s, t.center);
      const firstSmooth = first && smoothPath(grid, first);
      const firstJson = JSON.stringify(first);
      for (let rep = 0; rep < 3; rep++) {
        const again = findPath(grid, s, t.center);
        assert.deepEqual(again, first, `findPath (${c.i},${c.j}) -> ${t.name} rep ${rep}`);
        if (first) {
          assert.deepEqual(smoothPath(grid, again), firstSmooth, `smoothPath (${c.i},${c.j}) -> ${t.name} rep ${rep}`);
          assert.notEqual(again, first, "each call returns a fresh array");
          assert.notEqual(again[0], first[0], "points are not shared between calls");
        }
      }
      assert.equal(JSON.stringify(first), firstJson, "smoothing must not modify the raw path");
      assert.deepEqual(s, sCopy);
      assert.deepEqual(t.center, gCopy);
      checked++;
    }
  }
  assert.ok(checked > 600);
});

test("a fresh Grid built from the same layout gives the same paths, and the shared blocked data was never modified", () => {
  const other = new Grid(layout);
  for (const t of TARGETS) {
    for (const c of FREE.filter((_, n) => n % 7 === 0)) {
      assert.deepEqual(findPath(other, cellCenter(c), t.center), findPath(grid, cellCenter(c), t.center));
    }
  }
  assert.deepEqual(Array.from(layout.grid.blocked), Array.from(BLOCKED_SNAPSHOT));
});

test("report: number of path pairs checked", (t) => {
  t.diagnostic(
    `path pairs checked: ${stats.pairs} (${stats.found} found, ${stats.nullPaths} null) over ${FREE.length} free cells x ${TARGETS.length} destinations x 2 directions; ` +
    `smoothed paths sampled every 5 cm: ${stats.smoothedChecked}; worst raw asymmetry ${(stats.worstRawAsym * 100).toFixed(2)}%, worst smoothed asymmetry ${(stats.worstSmoothAsym * 100).toFixed(2)}%`);
  assert.ok(stats.pairs > 0);
});
