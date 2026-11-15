// Office layout: pure data, no Three.js import.
//
// Units are meters, +y is up, the floor is y = 0. The room spans
// x in [-8, 8] and z in [-6, 6]. The back wall is at z = -6 (it has the
// doorway), the left wall at x = -8. The camera looks from +x, +z.
//
// Yaw convention: a character with yaw `r` faces (sin r, cos r) in (x, z).
// yaw 0 faces +z (toward the camera side), PI faces -z, -PI/2 faces -x.
//
// The 0.5 m grid is aligned to multiples of 0.5 m. A cell is blocked when its
// center lies inside a furniture/wall box grown by CLEARANCE (the character's
// radius), so a path made of free cell centers keeps the body out of furniture.
// Chairs are blocked too. Seats are reached by a short scripted move from
// `standSpot` to `seat` (the sit-down animation), not by pathfinding.

export const GRID_SIZE = 0.5;
export const CLEARANCE = 0.25;
export const MAX_AGENTS = 8;

export const ROOM = Object.freeze({
  minX: -8, maxX: 8, minZ: -6, maxZ: 6,
  wallThickness: 0.3, wallHeight: 3, lowWallHeight: 0.5,
});

// Doorway in the back wall (z = -6). Width 2 m.
export const DOOR = Object.freeze({ minX: 5.5, maxX: 7.5, height: 2.2 });

const STAND_OFFSET = 0.8;   // standSpot is this far behind/in front of the seat
const SEAT_OFFSET = 0.72;   // seat is this far from the desk center
const DESK_W = 1.6;
const DESK_D = 0.8;
const CHAIR = 0.5;          // chair footprint (square)

const DESK_COLS = [-4.5, -1.5, 1.5, 4.5];
const DESK_ROWS = [-2.8, 0.2];

const pt = (x, z, y = 0) => Object.freeze({ x, y, z });
const fwd = (yaw) => ({ x: Math.sin(yaw), z: Math.cos(yaw) });

// ---- desks -----------------------------------------------------------------
// Index 0..3 is the back row (left to right), 4..7 is the front row.
// Characters sit behind the desk (toward -z) and face +z.
function makeDesk(index) {
  const col = index % DESK_COLS.length;
  const row = Math.floor(index / DESK_COLS.length);
  const x = DESK_COLS[col];
  const z = DESK_ROWS[row];
  const rotation = 0;
  const f = fwd(rotation);
  const seat = pt(x - f.x * SEAT_OFFSET, z - f.z * SEAT_OFFSET);
  const standSpot = pt(seat.x - f.x * STAND_OFFSET, seat.z - f.z * STAND_OFFSET);
  // Where a visiting colleague stands (beside the chair, facing the occupant).
  const visitSpot = Object.freeze({ x: x + 1.25, y: 0, z: seat.z, rotation: -Math.PI / 2 });
  return Object.freeze({
    index, position: pt(x, z), rotation, seat, standSpot, visitSpot,
  });
}
const DESKS = Object.freeze(Array.from({ length: MAX_AGENTS }, (_, i) => makeDesk(i)));

// ---- pantry ----------------------------------------------------------------
// Counter and fridge along the left wall. The coffee machine sits on the counter.
const PANTRY = Object.freeze({
  counter: Object.freeze({ minX: -7.85, maxX: -7.15, minZ: -3.5, maxZ: -0.5, height: 0.9 }),
  fridge: Object.freeze({ minX: -7.85, maxX: -7.15, minZ: -0.5, maxZ: 0.3, height: 1.75 }),
  coffeeMachine: pt(-7.5, -2.0, 0.9),
});
// Where an agent stands to get coffee: in front of the machine, facing -x.
const pantrySpot = Object.freeze({ x: -6.5, y: 0, z: -2.0, rotation: -Math.PI / 2 });

// ---- meeting table ---------------------------------------------------------
const MEETING_TABLE = Object.freeze({ cx: 0, cz: 3.4, w: 3.2, d: 1.4 });
const MEETING_CHAIR_X = [-1.1, 0, 1.1];
function makeMeetingSpots() {
  const spots = [];
  const sides = [
    { z: MEETING_TABLE.cz - MEETING_TABLE.d / 2 - 0.3, rotation: 0 },        // north side, faces +z
    { z: MEETING_TABLE.cz + MEETING_TABLE.d / 2 + 0.3, rotation: Math.PI },  // south side, faces -z
  ];
  for (const s of sides) {
    for (const x of MEETING_CHAIR_X) {
      const f = fwd(s.rotation);
      spots.push(Object.freeze({
        index: spots.length,
        position: pt(x, s.z),            // chair center (seat)
        rotation: s.rotation,
        standSpot: pt(x - f.x * STAND_OFFSET, s.z - f.z * STAND_OFFSET),
      }));
    }
  }
  return Object.freeze(spots);
}
const MEETING_SPOTS = makeMeetingSpots();

// ---- plants ----------------------------------------------------------------
const PLANTS = Object.freeze([
  pt(-7.3, -5.2), pt(-7.2, 5.2), pt(7.2, 5.2), pt(-7.2, 1.4), pt(4.4, 3.2),
].map((p, i) => Object.freeze({ ...p, radius: 0.3, scale: i === 4 ? 1.35 : 1 })));

const DOOR_SPOT = pt((DOOR.minX + DOOR.maxX) / 2, ROOM.minZ - 0.25);

// ---- walls -----------------------------------------------------------------
const T = ROOM.wallThickness / 2;
const WALLS = Object.freeze([
  { name: "back-left", minX: ROOM.minX - T, maxX: DOOR.minX, minZ: ROOM.minZ - T, maxZ: ROOM.minZ + T, height: ROOM.wallHeight },
  { name: "back-right", minX: DOOR.maxX, maxX: ROOM.maxX + T, minZ: ROOM.minZ - T, maxZ: ROOM.minZ + T, height: ROOM.wallHeight },
  { name: "left", minX: ROOM.minX - T, maxX: ROOM.minX + T, minZ: ROOM.minZ - T, maxZ: ROOM.maxZ + T, height: ROOM.wallHeight },
  { name: "right-low", minX: ROOM.maxX - T, maxX: ROOM.maxX + T, minZ: ROOM.minZ - T, maxZ: ROOM.maxZ + T, height: ROOM.lowWallHeight },
  { name: "front-low", minX: ROOM.minX - T, maxX: ROOM.maxX + T, minZ: ROOM.maxZ - T, maxZ: ROOM.maxZ + T, height: ROOM.lowWallHeight },
].map(Object.freeze));

// ---- obstacle boxes (for the blocked grid) ---------------------------------
const box = (kind, cx, cz, w, d) => ({
  kind, minX: cx - w / 2, maxX: cx + w / 2, minZ: cz - d / 2, maxZ: cz + d / 2,
});

function buildObstacles() {
  const o = [];
  for (const w of WALLS) o.push({ kind: "wall", minX: w.minX, maxX: w.maxX, minZ: w.minZ, maxZ: w.maxZ });
  for (const d of DESKS) {
    o.push(box("desk", d.position.x, d.position.z, DESK_W, DESK_D));
    o.push(box("chair", d.seat.x, d.seat.z, CHAIR, CHAIR));
  }
  o.push(box("meeting-table", MEETING_TABLE.cx, MEETING_TABLE.cz, MEETING_TABLE.w, MEETING_TABLE.d));
  for (const m of MEETING_SPOTS) o.push(box("chair", m.position.x, m.position.z, CHAIR, CHAIR));
  o.push({ kind: "pantry", ...PANTRY.counter });
  o.push({ kind: "pantry", ...PANTRY.fridge });
  for (const p of PLANTS) o.push(box("plant", p.x, p.z, p.radius * 2, p.radius * 2));
  return o;
}
const OBSTACLES = Object.freeze(buildObstacles().map(Object.freeze));

// ---- blocked grid ----------------------------------------------------------
// The grid covers the room plus one ring of cells outside the walls. Anything
// outside the grid counts as blocked, except the doorway row which is inside.
const ORIGIN_X = ROOM.minX - 0.5;
const ORIGIN_Z = ROOM.minZ - 0.5;
const COLS = Math.round((ROOM.maxX - ROOM.minX + 1) / GRID_SIZE);
const ROWS = Math.round((ROOM.maxZ - ROOM.minZ + 1) / GRID_SIZE);
const EPS = 1e-6;

function buildGrid() {
  const cells = new Uint8Array(COLS * ROWS);
  for (let j = 0; j < ROWS; j++) {
    for (let i = 0; i < COLS; i++) {
      const cx = ORIGIN_X + (i + 0.5) * GRID_SIZE;
      const cz = ORIGIN_Z + (j + 0.5) * GRID_SIZE;
      for (const o of OBSTACLES) {
        if (cx > o.minX - CLEARANCE + EPS && cx < o.maxX + CLEARANCE - EPS &&
            cz > o.minZ - CLEARANCE + EPS && cz < o.maxZ + CLEARANCE - EPS) {
          cells[j * COLS + i] = 1;
          break;
        }
      }
    }
  }
  return cells;
}
const BLOCKED = buildGrid();

export function worldToCell(x, z) {
  return { i: Math.floor((x - ORIGIN_X) / GRID_SIZE), j: Math.floor((z - ORIGIN_Z) / GRID_SIZE) };
}
export function cellToWorld(i, j) {
  return { x: ORIGIN_X + (i + 0.5) * GRID_SIZE, z: ORIGIN_Z + (j + 0.5) * GRID_SIZE };
}
export function isBlockedCell(i, j) {
  if (i < 0 || j < 0 || i >= COLS || j >= ROWS) return true;
  return BLOCKED[j * COLS + i] === 1;
}
export function isBlocked(x, z) {
  const { i, j } = worldToCell(x, z);
  return isBlockedCell(i, j);
}

export function deskForAgent(index) {
  if (!Number.isInteger(index) || index < 0 || index >= MAX_AGENTS) {
    throw new RangeError(`agent desk index must be an integer 0..${MAX_AGENTS - 1}, got ${index}`);
  }
  return DESKS[index];
}

export const layout = Object.freeze({
  gridSize: GRID_SIZE,
  clearance: CLEARANCE,
  maxAgents: MAX_AGENTS,
  room: ROOM,
  door: DOOR,
  doorSpot: DOOR_SPOT,
  desks: DESKS,
  deskForAgent,
  pantry: PANTRY,
  pantrySpot,
  meetingTable: MEETING_TABLE,
  meetingSpots: MEETING_SPOTS,
  plants: PLANTS,
  walls: WALLS,
  obstacles: OBSTACLES,
  grid: Object.freeze({
    size: GRID_SIZE, cols: COLS, rows: ROWS, originX: ORIGIN_X, originZ: ORIGIN_Z,
    blocked: BLOCKED,
  }),
  worldToCell,
  cellToWorld,
  isBlockedCell,
  isBlocked,
  // [[i, j], ...] for every blocked cell inside the grid.
  blockedCells() {
    const out = [];
    for (let j = 0; j < ROWS; j++) for (let i = 0; i < COLS; i++) if (BLOCKED[j * COLS + i]) out.push([i, j]);
    return out;
  },
});

export const DESK_SIZE = Object.freeze({ w: DESK_W, d: DESK_D });
export const CHAIR_SIZE = CHAIR;
