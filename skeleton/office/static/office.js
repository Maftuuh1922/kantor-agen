// Kantor Agen: Three.js office scene.
//
// Plain ES module. `three` is resolved by the importmap in index.html, so the
// page works offline with the vendored copy in ./vendor/.
//
// Everything here is built from primitives (boxes, cylinders, low-poly spheres)
// so the office renders before any downloaded asset exists. Geometry and
// materials are shared, and all static furniture is grouped into InstancedMesh
// batches (one draw call per geometry/material pair).
//
// Layout data (desk positions, pantry spot, meeting spots, blocked grid cells)
// lives in ./layout.js, which has no Three.js dependency, so the pathfinding
// code can use it directly.

import * as THREE from "three";
import { layout, GRID_SIZE, MAX_AGENTS, ROOM, DOOR } from "./layout.js";
import { TEXT } from "./ui-text.js";

export { layout, GRID_SIZE, MAX_AGENTS };

export const QUALITY_LEVELS = Object.freeze(["normal", "low"]);

const PALETTE = Object.freeze({
  background: 0xe9e1d3,
  wall: 0xf0e8da,
  wallAccent: 0xcdd8c8,
  wallLow: 0xdccfb8,
  baseboard: 0x8a6f52,
  slab: 0x6f6657,
  deskTop: 0xe6d3b0,
  deskFrame: 0x5a5d66,
  metal: 0x3b3e45,
  chairBase: 0x2f3237,
  screenOff: 0x1c2026,
  screenOn: 0x7cc4ff,
  keyboard: 0x30343b,
  white: 0xf7f5ef,
  counter: 0xf2efe8,
  counterTop: 0x3d4148,
  cabinet: 0xe6dccb,
  fridge: 0xe3e6e8,
  coffee: 0xb23a3a,
  glass: 0x9ec9d9,
  pot: 0xc0724d,
  leaf: 0x4f9a5a,
  rug: 0x4f7f86,
  meetingTop: 0xcfae80,
  doorFrame: 0x6b5238,
  stone: 0xbdb5a5,
  sky: 0xbfe3f5,
});
const CHAIR_COLORS = Object.freeze([
  0x3d5a80, 0xe07a5f, 0x81b29a, 0xf2cc8f, 0x6d597a, 0x457b9d, 0xb56576, 0x5c8a6b,
]);
const LEAF_COLORS = Object.freeze([0x4f9a5a, 0x5fae63, 0x3f8650, 0x6bb36b]);
const NOTE_COLORS = Object.freeze([0xffe066, 0xff9ecb, 0x8fd3ff, 0xb5e48c, 0xffb870]);
const CUP_COLORS = Object.freeze([0xf7f5ef, 0xe07a5f, 0x81b29a, 0xf2cc8f, 0x8fb8de]);

// ---- small helpers ---------------------------------------------------------

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

const _pos = new THREE.Vector3();
const _quat = new THREE.Quaternion();
const _scl = new THREE.Vector3();
const _eul = new THREE.Euler();

/** Local transform: position, scale, optional Euler rotation. */
function xform(x, y, z, sx, sy, sz, rx = 0, ry = 0, rz = 0) {
  _pos.set(x, y, z);
  _quat.setFromEuler(_eul.set(rx, ry, rz));
  _scl.set(sx, sy, sz);
  return new THREE.Matrix4().compose(_pos, _quat, _scl);
}

/** Placement of a prop on the floor: position plus yaw (rotation about y). */
function place(x, z, yaw = 0, y = 0) {
  return new THREE.Matrix4().makeRotationY(yaw).setPosition(x, y, z);
}

// ---- floor texture ---------------------------------------------------------

function makePlankTexture(maxAnisotropy) {
  const size = 512;
  const planks = 8;
  const h = size / planks;
  const c = document.createElement("canvas");
  c.width = c.height = size;
  const g = c.getContext("2d");
  const rnd = mulberry32(7);
  for (let p = 0; p < planks; p++) {
    const light = 56 + Math.floor(rnd() * 8);
    g.fillStyle = `hsl(32, 38%, ${light}%)`;
    g.fillRect(0, p * h, size, h);
    // grain
    g.strokeStyle = "rgba(90, 60, 30, 0.10)";
    g.lineWidth = 1;
    for (let k = 0; k < 7; k++) {
      const y = p * h + 4 + rnd() * (h - 8);
      g.beginPath();
      g.moveTo(0, y);
      g.lineTo(size, y + (rnd() - 0.5) * 4);
      g.stroke();
    }
    // end joint
    const jx = Math.floor(rnd() * size);
    g.fillStyle = "rgba(70, 45, 25, 0.35)";
    g.fillRect(jx, p * h, 2, h);
    // seam
    g.fillStyle = "rgba(70, 45, 25, 0.35)";
    g.fillRect(0, p * h, size, 2);
  }
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  t.wrapS = t.wrapT = THREE.RepeatWrapping;
  t.repeat.set((ROOM.maxX - ROOM.minX) / 2, (ROOM.maxZ - ROOM.minZ) / 2);
  t.anisotropy = Math.min(4, maxAnisotropy);
  return t;
}

// ---- camera controls (pan + zoom, fixed orbit angles) ----------------------

function createControls(camera, dom, home) {
  const AZIMUTH = THREE.MathUtils.degToRad(38);
  const ELEVATION = THREE.MathUtils.degToRad(48);
  const MIN_ZOOM = 0.35; // fraction of the fit distance
  const MAX_ZOOM = 1.05;
  const PAN_RANGE = { x: 6, z: 4.5 }; // max pan at full zoom-in, meters

  const dir = new THREE.Vector3(
    Math.cos(ELEVATION) * Math.sin(AZIMUTH),
    Math.sin(ELEVATION),
    Math.cos(ELEVATION) * Math.cos(AZIMUTH),
  );
  const right = new THREE.Vector3(Math.cos(AZIMUTH), 0, -Math.sin(AZIMUTH));
  const groundForward = new THREE.Vector3(-Math.sin(AZIMUTH), 0, -Math.cos(AZIMUTH));

  const target = home.clone();
  const state = { zoom: 1, fit: 24, height: 600, dragged: false };
  const pointers = new Map();
  let gesture = null;

  function panLimit() {
    // No panning when the whole room is in view; more range as you zoom in.
    return THREE.MathUtils.clamp((1 - state.zoom) / (1 - MIN_ZOOM), 0, 1);
  }

  function apply() {
    state.zoom = THREE.MathUtils.clamp(state.zoom, MIN_ZOOM, MAX_ZOOM);
    const t = panLimit();
    target.x = home.x + THREE.MathUtils.clamp(target.x - home.x, -PAN_RANGE.x * t, PAN_RANGE.x * t);
    target.z = home.z + THREE.MathUtils.clamp(target.z - home.z, -PAN_RANGE.z * t, PAN_RANGE.z * t);
    const dist = state.zoom * state.fit;
    camera.position.copy(target).addScaledVector(dir, dist);
    camera.lookAt(target);
  }

  function worldPerPixel() {
    const dist = state.zoom * state.fit;
    return (2 * dist * Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2)) / Math.max(1, state.height);
  }

  function panBy(dxPx, dyPx) {
    const wpp = worldPerPixel();
    target.addScaledVector(right, -dxPx * wpp);
    target.addScaledVector(groundForward, (dyPx * wpp) / Math.sin(ELEVATION));
    apply();
  }

  function zoomBy(factor) {
    state.zoom *= factor;
    apply();
  }

  function onPointerDown(e) {
    dom.setPointerCapture?.(e.pointerId);
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.size === 1) state.dragged = false;
    gesture = { moved: 0, lastDist: pinchDistance(), lastMid: midpoint() };
  }
  function onPointerMove(e) {
    const p = pointers.get(e.pointerId);
    if (!p) return;
    const prev = { x: p.x, y: p.y };
    p.x = e.clientX;
    p.y = e.clientY;
    if (pointers.size === 1) {
      const dx = p.x - prev.x;
      const dy = p.y - prev.y;
      gesture.moved += Math.abs(dx) + Math.abs(dy);
      if (gesture.moved > 4) state.dragged = true;
      if (state.dragged) panBy(dx, dy);
    } else if (pointers.size === 2) {
      state.dragged = true;
      const d = pinchDistance();
      const mid = midpoint();
      if (gesture.lastDist > 0 && d > 0) zoomBy(gesture.lastDist / d);
      panBy(mid.x - gesture.lastMid.x, mid.y - gesture.lastMid.y);
      gesture.lastDist = d;
      gesture.lastMid = mid;
    }
  }
  function onPointerUp(e) {
    pointers.delete(e.pointerId);
    dom.releasePointerCapture?.(e.pointerId);
    gesture = { moved: 0, lastDist: pinchDistance(), lastMid: midpoint() };
  }
  function pinchDistance() {
    if (pointers.size < 2) return 0;
    const [a, b] = [...pointers.values()];
    return Math.hypot(a.x - b.x, a.y - b.y);
  }
  function midpoint() {
    if (pointers.size < 2) return { x: 0, y: 0 };
    const [a, b] = [...pointers.values()];
    return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
  }
  function onWheel(e) {
    e.preventDefault();
    const unit = e.deltaMode === 1 ? 16 : 1;
    zoomBy(Math.exp(e.deltaY * unit * 0.0012));
  }
  function onContextMenu(e) {
    e.preventDefault();
  }

  dom.style.touchAction = "none";
  dom.addEventListener("pointerdown", onPointerDown);
  dom.addEventListener("pointermove", onPointerMove);
  dom.addEventListener("pointerup", onPointerUp);
  dom.addEventListener("pointercancel", onPointerUp);
  dom.addEventListener("wheel", onWheel, { passive: false });
  dom.addEventListener("contextmenu", onContextMenu);

  return {
    target,
    state,
    /** True if the last pointer gesture moved the view (use to ignore clicks after a drag). */
    get dragged() {
      return state.dragged;
    },
    get zoom() {
      return state.zoom;
    },
    set zoom(v) {
      state.zoom = v;
      apply();
    },
    panBy,
    zoomBy,
    /** Set the distance at which the whole room is visible, then re-apply. */
    setFit(fit, height) {
      state.fit = fit;
      state.height = height;
      apply();
    },
    reset() {
      target.copy(home);
      state.zoom = 1;
      apply();
    },
    /** View direction from target to camera (unit vector). */
    direction: dir.clone(),
    dispose() {
      dom.removeEventListener("pointerdown", onPointerDown);
      dom.removeEventListener("pointermove", onPointerMove);
      dom.removeEventListener("pointerup", onPointerUp);
      dom.removeEventListener("pointercancel", onPointerUp);
      dom.removeEventListener("wheel", onWheel);
      dom.removeEventListener("contextmenu", onContextMenu);
    },
  };
}

// ---- main entry ------------------------------------------------------------

/**
 * Build the office scene on `canvas`.
 *
 * options:
 *   quality          "normal" (default) | "low"
 *   loadingElement   element shown until the first frame (default #loading)
 *   autoHideLoading  hide the loading element after the first frame (default true)
 *
 * Returns { scene, camera, renderer, layout, controls, ready, start, stop, resize,
 *           setQuality, getQuality, addAgentDesk, onFrame, setLoadingProgress,
 *           hideLoading, getStats, dispose }.
 */
export function createOffice(canvas, options = {}) {
  if (!(canvas instanceof HTMLCanvasElement)) throw new TypeError("createOffice needs a canvas element");
  let quality = options.quality ?? "normal";
  if (!QUALITY_LEVELS.includes(quality)) throw new RangeError(`unknown quality "${quality}"`);

  // -- loading bar --
  const loadingRoot = options.loadingElement ?? document.getElementById("loading");
  const loadingFill = loadingRoot?.querySelector("[data-fill]") ?? null;
  const loadingLabel = loadingRoot?.querySelector("[data-label]") ?? null;
  function setLoadingProgress(fraction, text) {
    const f = Math.max(0, Math.min(1, fraction));
    if (loadingFill) loadingFill.style.width = `${Math.round(f * 100)}%`;
    loadingRoot?.setAttribute("aria-valuenow", String(Math.round(f * 100)));
    if (loadingLabel && text) loadingLabel.textContent = text;
  }
  function hideLoading() {
    if (!loadingRoot) return;
    loadingRoot.classList.add("done");
    loadingRoot.setAttribute("aria-hidden", "true");
  }
  setLoadingProgress(0.05, TEXT.loading.renderer);

  // -- renderer --
  const renderer = new THREE.WebGLRenderer({
    canvas,
    antialias: quality !== "low",
    powerPreference: "high-performance",
  });
  renderer.shadowMap.type = THREE.PCFShadowMap; // PCFSoftShadowMap was removed from three
  renderer.setClearColor(PALETTE.background, 1);
  const maxAniso = renderer.capabilities.getMaxAnisotropy();

  const scene = new THREE.Scene();
  scene.name = "office";
  scene.background = new THREE.Color(PALETTE.background);
  scene.userData.layout = layout;

  const camera = new THREE.PerspectiveCamera(32, 1, 1, 200);
  camera.name = "office-camera";

  // -- shared geometry --
  const GEO = {
    box: new THREE.BoxGeometry(1, 1, 1),
    cyl: new THREE.CylinderGeometry(1, 1, 1, 20),
    ball: new THREE.IcosahedronGeometry(1, 1),
    plane: new THREE.PlaneGeometry(1, 1),
  };
  for (const [k, g] of Object.entries(GEO)) g.name = k;

  // -- shared materials --
  const materials = [];
  function std(name, color, roughness = 0.85, extra = {}) {
    const m = new THREE.MeshStandardMaterial({ color, roughness, metalness: 0, ...extra });
    m.name = name;
    materials.push(m);
    return m;
  }
  function basic(name, color, extra = {}) {
    const m = new THREE.MeshBasicMaterial({ color, ...extra });
    m.name = name;
    materials.push(m);
    return m;
  }
  const floorTexture = makePlankTexture(maxAniso);
  const MAT = {
    floor: std("floor", 0xffffff, 0.9, { map: floorTexture }),
    slab: std("slab", PALETTE.slab, 1),
    wall: std("wall", PALETTE.wall, 0.95),
    wallAccent: std("wallAccent", PALETTE.wallAccent, 0.95),
    wallLow: std("wallLow", PALETTE.wallLow, 0.95),
    baseboard: std("baseboard", PALETTE.baseboard, 0.7),
    deskTop: std("deskTop", PALETTE.deskTop, 0.6),
    deskFrame: std("deskFrame", PALETTE.deskFrame, 0.5, { metalness: 0.2 }),
    metal: std("metal", PALETTE.metal, 0.45, { metalness: 0.35 }),
    chairBase: std("chairBase", PALETTE.chairBase, 0.5, { metalness: 0.2 }),
    chairFabric: std("chairFabric", 0xffffff, 0.9), // tinted per instance
    keyboard: std("keyboard", PALETTE.keyboard, 0.6),
    white: std("white", PALETTE.white, 0.7),
    tinted: std("tinted", 0xffffff, 0.7), // tinted per instance
    screenOff: std("screenOff", PALETTE.screenOff, 0.3),
    screenOn: std("screenOn", PALETTE.screenOn, 0.3, { emissive: PALETTE.screenOn, emissiveIntensity: 0.9 }),
    counter: std("counter", PALETTE.counter, 0.6),
    counterTop: std("counterTop", PALETTE.counterTop, 0.35),
    cabinet: std("cabinet", PALETTE.cabinet, 0.7),
    fridge: std("fridge", PALETTE.fridge, 0.35, { metalness: 0.15 }),
    coffee: std("coffee", PALETTE.coffee, 0.4, { metalness: 0.2 }),
    glass: std("glass", PALETTE.glass, 0.1, { transparent: true, opacity: 0.55 }),
    pot: std("pot", PALETTE.pot, 0.85),
    leaf: std("leaf", 0xffffff, 0.8, { flatShading: true }),
    rug: std("rug", PALETTE.rug, 1),
    meetingTop: std("meetingTop", PALETTE.meetingTop, 0.55),
    doorFrame: std("doorFrame", PALETTE.doorFrame, 0.7),
    stone: std("stone", PALETTE.stone, 0.95),
    sky: basic("sky", PALETTE.sky),
    dark: std("dark", 0x2b2d31, 0.8),
  };

  // -- batching: one InstancedMesh per (geometry, material, shadow flags) --
  const batches = new Map();
  function batch(geo, mat, cast = true) {
    const key = `${geo.name}|${mat.name}|${cast ? 1 : 0}`;
    let b = batches.get(key);
    if (!b) {
      b = { geo, mat, cast, matrices: [], colors: [], tinted: false };
      batches.set(key, b);
    }
    return b;
  }
  const white = new THREE.Color(1, 1, 1);
  function push(geo, mat, local, placement, color, cast = true) {
    const b = batch(geo, mat, cast);
    const m = placement ? placement.clone().multiply(local) : local;
    b.matrices.push(m);
    if (color !== undefined) {
      b.tinted = true;
      b.colors.push(new THREE.Color(color));
    } else {
      b.colors.push(white);
    }
  }
  /** Box centered at (cx,cy,cz) with size (w,h,d), in `placement` space. */
  const box = (mat, cx, cy, cz, w, h, d, placement = null, color, cast = true) =>
    push(GEO.box, mat, xform(cx, cy, cz, w, h, d), placement, color, cast);
  /** Vertical cylinder centered at (cx,cy,cz), radius r, height h. */
  const cyl = (mat, cx, cy, cz, r, h, placement = null, color, cast = true) =>
    push(GEO.cyl, mat, xform(cx, cy, cz, r, h, r), placement, color, cast);
  /** Cylinder whose axis points along local z (for clocks, discs on walls). */
  const disc = (mat, cx, cy, cz, r, thick, placement = null, color, cast = false) =>
    push(GEO.cyl, mat, xform(cx, cy, cz, r, thick, r, Math.PI / 2, 0, 0), placement, color, cast);
  const ball = (mat, cx, cy, cz, r, placement = null, color, cast = true) =>
    push(GEO.ball, mat, xform(cx, cy, cz, r, r, r), placement, color, cast);

  const root = new THREE.Group();
  root.name = "office-root";
  scene.add(root);

  setLoadingProgress(0.15, TEXT.loading.floor);

  // -- floor, slab, rug --
  const floor = new THREE.Mesh(GEO.plane, MAT.floor);
  floor.name = "floor";
  floor.rotation.x = -Math.PI / 2;
  floor.scale.set(ROOM.maxX - ROOM.minX, ROOM.maxZ - ROOM.minZ, 1);
  floor.receiveShadow = true;
  root.add(floor);
  // Thick slab under the room so it reads as a diorama on the soft background.
  // Its top sits 3 cm below the floor plane so the two never z-fight.
  box(MAT.slab, 0, -0.23, 0, ROOM.maxX - ROOM.minX + 0.9, 0.4, ROOM.maxZ - ROOM.minZ + 0.9, null, undefined, false);

  const mt = layout.meetingTable;
  box(MAT.rug, mt.cx, 0.012, mt.cz, mt.w + 1.4, 0.024, mt.d + 2.9, null, undefined, false);

  // -- walls --
  for (const w of layout.walls) {
    const mat = w.height <= ROOM.lowWallHeight ? MAT.wallLow : w.name.startsWith("back") ? MAT.wallAccent : MAT.wall;
    box(mat, (w.minX + w.maxX) / 2, w.height / 2, (w.minZ + w.maxZ) / 2, w.maxX - w.minX, w.height, w.maxZ - w.minZ);
  }
  const half = ROOM.wallThickness / 2;
  const innerBack = ROOM.minZ + half;
  const innerLeft = ROOM.minX + half;
  // Door: lintel above the opening, jambs on each side, mat inside, step outside.
  const doorW = DOOR.maxX - DOOR.minX;
  const doorCx = (DOOR.minX + DOOR.maxX) / 2;
  box(MAT.wallAccent, doorCx, (DOOR.height + ROOM.wallHeight) / 2, ROOM.minZ, doorW, ROOM.wallHeight - DOOR.height, ROOM.wallThickness);
  for (const sx of [DOOR.minX, DOOR.maxX]) {
    box(MAT.doorFrame, sx, DOOR.height / 2, ROOM.minZ, 0.1, DOOR.height, ROOM.wallThickness + 0.06);
  }
  box(MAT.doorFrame, doorCx, DOOR.height + 0.05, ROOM.minZ, doorW + 0.1, 0.1, ROOM.wallThickness + 0.06);
  box(MAT.dark, doorCx, 0.015, innerBack + 0.55, doorW - 0.4, 0.03, 0.8, null, undefined, false);
  box(MAT.stone, doorCx, 0.06, ROOM.minZ - 0.9, doorW + 0.6, 0.12, 1.4);
  // Baseboards on the two tall inner faces.
  box(MAT.baseboard, (ROOM.minX + DOOR.minX) / 2, 0.07, innerBack + 0.02, DOOR.minX - ROOM.minX - 0.2, 0.14, 0.04, null, undefined, false);
  box(MAT.baseboard, (DOOR.maxX + ROOM.maxX) / 2, 0.07, innerBack + 0.02, ROOM.maxX - DOOR.maxX - 0.2, 0.14, 0.04, null, undefined, false);
  box(MAT.baseboard, innerLeft + 0.02, 0.07, 0, 0.04, 0.14, ROOM.maxZ - ROOM.minZ - 0.3, null, undefined, false);

  setLoadingProgress(0.35, TEXT.loading.desks);

  // -- desks, chairs, monitors --
  const deskRecords = [];
  for (const d of layout.desks) {
    const p = place(d.position.x, d.position.z, d.rotation);
    // Local frame: +z points from the occupant toward the far edge of the desk.
    box(MAT.deskTop, 0, 0.73, 0, 1.6, 0.04, 0.8, p);
    box(MAT.deskFrame, -0.76, 0.355, 0, 0.04, 0.71, 0.74, p);
    box(MAT.deskFrame, 0.76, 0.355, 0, 0.04, 0.71, 0.74, p);
    box(MAT.deskFrame, 0, 0.5, 0.37, 1.5, 0.4, 0.03, p);
    box(MAT.keyboard, 0.0, 0.76, -0.17, 0.42, 0.02, 0.14, p, undefined, false);
    box(MAT.keyboard, 0.34, 0.76, -0.16, 0.07, 0.02, 0.1, p, undefined, false);
    cyl(MAT.tinted, 0.62, 0.8, -0.1, 0.04, 0.09, p, CUP_COLORS[d.index % CUP_COLORS.length], false);
    // Monitor: foot, neck, frame. The screen is its own mesh so it can switch on/off.
    box(MAT.metal, 0, 0.765, 0.16, 0.22, 0.03, 0.16, p, undefined, false);
    box(MAT.metal, 0, 0.87, 0.18, 0.05, 0.2, 0.04, p, undefined, false);
    box(MAT.metal, 0, 1.07, 0.18, 0.58, 0.34, 0.03, p);

    // Chair: faces the desk, so its backrest is on the occupant's side.
    const cp = place(d.seat.x, d.seat.z, d.rotation);
    const color = CHAIR_COLORS[d.index % CHAIR_COLORS.length];
    box(MAT.chairFabric, 0, 0.46, 0, 0.46, 0.06, 0.46, cp, color);
    box(MAT.chairFabric, 0, 0.74, -0.21, 0.46, 0.5, 0.05, cp, color);
    cyl(MAT.chairBase, 0, 0.23, 0, 0.03, 0.4, cp);
    cyl(MAT.chairBase, 0, 0.04, 0, 0.24, 0.04, cp);

    const screen = new THREE.Mesh(GEO.box, MAT.screenOff);
    screen.name = `desk-${d.index}-screen`;
    screen.scale.set(0.54, 0.29, 0.004);
    screen.position.set(0, 1.07, 0.18 - 0.0165);
    const group = new THREE.Group();
    group.name = `desk-${d.index}`;
    group.position.set(d.position.x, 0, d.position.z);
    group.rotation.y = d.rotation;
    group.add(screen);
    root.add(group);
    deskRecords.push({ index: d.index, group, screen, occupied: false });
  }

  setLoadingProgress(0.55, TEXT.loading.pantry);

  // -- pantry --
  const pc = layout.pantry.counter;
  const pcx = (pc.minX + pc.maxX) / 2;
  const pcz = (pc.minZ + pc.maxZ) / 2;
  box(MAT.counter, pcx, pc.height / 2 - 0.02, pcz, pc.maxX - pc.minX, pc.height - 0.04, pc.maxZ - pc.minZ);
  box(MAT.counterTop, pcx + 0.02, pc.height - 0.01, pcz, pc.maxX - pc.minX + 0.06, 0.04, pc.maxZ - pc.minZ + 0.04);
  // sink and tap
  box(MAT.metal, pcx + 0.02, pc.height + 0.012, -1.1, 0.46, 0.012, 0.6, null, undefined, false);
  cyl(MAT.metal, pcx - 0.22, pc.height + 0.16, -1.1, 0.02, 0.3, null, undefined, false);
  box(MAT.metal, pcx - 0.1, pc.height + 0.3, -1.1, 0.26, 0.03, 0.03, null, undefined, false);
  // upper cabinets
  box(MAT.cabinet, innerLeft + 0.19, 1.95, pcz, 0.38, 0.65, pc.maxZ - pc.minZ - 0.1);
  for (let k = 0; k < 4; k++) {
    box(MAT.metal, innerLeft + 0.39, 1.8, pc.minZ + 0.4 + k * 0.72, 0.02, 0.2, 0.03, null, undefined, false);
  }
  // fridge
  const fr = layout.pantry.fridge;
  box(MAT.fridge, (fr.minX + fr.maxX) / 2, fr.height / 2, (fr.minZ + fr.maxZ) / 2, fr.maxX - fr.minX, fr.height, fr.maxZ - fr.minZ);
  box(MAT.metal, fr.maxX + 0.01, 1.2, fr.minZ + 0.1, 0.03, 0.5, 0.03, null, undefined, false);
  box(MAT.metal, fr.maxX + 0.01, 1.62, fr.minZ + 0.1, 0.03, 0.25, 0.03, null, undefined, false);
  // coffee machine (front faces +x)
  {
    const cm = layout.pantry.coffeeMachine;
    const mp = place(cm.x, cm.z, Math.PI / 2, cm.y);
    box(MAT.coffee, 0, 0.21, 0, 0.3, 0.42, 0.36, mp);
    box(MAT.dark, 0, 0.44, 0, 0.32, 0.04, 0.38, mp);
    box(MAT.dark, 0, 0.1, 0.08, 0.24, 0.03, 0.2, mp, undefined, false);
    cyl(MAT.glass, 0, 0.17, 0.2, 0.05, 0.1, mp, undefined, false);
    box(MAT.metal, 0, 0.3, 0.19, 0.12, 0.05, 0.03, mp, undefined, false);
    // a few cups on the counter
    for (let k = 0; k < 3; k++) {
      cyl(MAT.tinted, cm.x + 0.02, pc.height + 0.045, cm.z + 0.55 + k * 0.17, 0.04, 0.09, null, CUP_COLORS[(k + 1) % CUP_COLORS.length], false);
    }
  }

  // -- meeting table and chairs --
  box(MAT.meetingTop, mt.cx, 0.74, mt.cz, mt.w, 0.06, mt.d);
  box(MAT.deskFrame, mt.cx - 1.1, 0.355, mt.cz, 0.1, 0.71, mt.d - 0.4);
  box(MAT.deskFrame, mt.cx + 1.1, 0.355, mt.cz, 0.1, 0.71, mt.d - 0.4);
  for (const s of layout.meetingSpots) {
    const cp = place(s.position.x, s.position.z, s.rotation);
    box(MAT.chairFabric, 0, 0.46, 0, 0.46, 0.06, 0.46, cp, 0x4a6670);
    box(MAT.chairFabric, 0, 0.74, -0.21, 0.46, 0.5, 0.05, cp, 0x4a6670);
    cyl(MAT.chairBase, 0, 0.23, 0, 0.03, 0.4, cp);
    cyl(MAT.chairBase, 0, 0.04, 0, 0.24, 0.04, cp);
  }
  // table clutter: notepads, a water jug
  box(MAT.white, mt.cx - 0.9, 0.775, mt.cz - 0.2, 0.28, 0.01, 0.2, null, undefined, false);
  box(MAT.white, mt.cx + 0.6, 0.775, mt.cz + 0.25, 0.28, 0.01, 0.2, null, undefined, false);
  cyl(MAT.glass, mt.cx, 0.87, mt.cz, 0.07, 0.2, null, undefined, false);

  setLoadingProgress(0.75, TEXT.loading.decor);

  // -- plants (deterministic variation) --
  const rnd = mulberry32(42);
  for (const pl of layout.plants) {
    const s = pl.scale;
    cyl(MAT.pot, pl.x, 0.17 * s, pl.z, 0.2 * s, 0.34 * s);
    const blobs = [
      [0, 0.62, 0, 0.3], [0.13, 0.5, 0.08, 0.2], [-0.14, 0.52, -0.08, 0.2], [0.02, 0.86, 0.02, 0.22],
    ];
    for (const [bx, by, bz, br] of blobs) {
      const jitter = 0.04 * (rnd() - 0.5);
      ball(MAT.leaf, pl.x + (bx + jitter) * s, by * s, pl.z + (bz + jitter) * s, br * s, null,
        LEAF_COLORS[Math.floor(rnd() * LEAF_COLORS.length)]);
    }
  }

  // -- wall decor --
  const wz = innerBack; // back wall inner face (normal +z)
  const wx = innerLeft; // left wall inner face (normal +x)
  // whiteboard with sticky notes
  box(MAT.deskFrame, -2.5, 1.7, wz + 0.03, 2.5, 1.3, 0.06, null, undefined, false);
  box(MAT.white, -2.5, 1.7, wz + 0.065, 2.36, 1.16, 0.02, null, undefined, false);
  for (let k = 0; k < 7; k++) {
    const nx = -3.45 + (k % 4) * 0.55 + (rnd() - 0.5) * 0.08;
    const ny = 1.95 - Math.floor(k / 4) * 0.45 + (rnd() - 0.5) * 0.06;
    box(MAT.tinted, nx, ny, wz + 0.085, 0.2, 0.2, 0.01, null, NOTE_COLORS[k % NOTE_COLORS.length], false);
  }
  // clock
  disc(MAT.white, 0.8, 2.25, wz + 0.04, 0.28, 0.05);
  box(MAT.dark, 0.8, 2.3, wz + 0.07, 0.02, 0.14, 0.01, null, undefined, false);
  box(MAT.dark, 0.86, 2.25, wz + 0.07, 0.12, 0.02, 0.01, null, undefined, false);
  // pictures
  const picColors = [0xe07a5f, 0x3d5a80, 0xf2cc8f];
  [2.35, 3.15, 3.95].forEach((px, k) => {
    box(MAT.dark, px, 1.8, wz + 0.03, 0.6, 0.8, 0.05, null, undefined, false);
    box(MAT.tinted, px, 1.8, wz + 0.06, 0.5, 0.7, 0.02, null, picColors[k], false);
  });
  // windows
  box(MAT.deskFrame, -6.0, 1.7, wz + 0.02, 1.9, 1.3, 0.05, null, undefined, false);
  box(MAT.sky, -6.0, 1.7, wz + 0.05, 1.74, 1.14, 0.01, null, undefined, false);
  box(MAT.deskFrame, -6.0, 1.7, wz + 0.058, 0.04, 1.14, 0.012, null, undefined, false);
  box(MAT.deskFrame, wx + 0.02, 1.7, 3.6, 0.05, 1.3, 2.5, null, undefined, false);
  box(MAT.sky, wx + 0.05, 1.7, 3.6, 0.01, 1.14, 2.34, null, undefined, false);
  box(MAT.deskFrame, wx + 0.058, 1.7, 3.6, 0.012, 1.14, 0.04, null, undefined, false);

  setLoadingProgress(0.85, TEXT.loading.meshes);

  // -- build InstancedMesh per batch --
  const staticMeshes = [];
  for (const b of batches.values()) {
    const mesh = new THREE.InstancedMesh(b.geo, b.mat, b.matrices.length);
    mesh.name = `batch:${b.geo.name}:${b.mat.name}`;
    b.matrices.forEach((m, i) => mesh.setMatrixAt(i, m));
    if (b.tinted) b.colors.forEach((c, i) => mesh.setColorAt(i, c));
    mesh.instanceMatrix.needsUpdate = true;
    mesh.castShadow = b.cast;
    mesh.receiveShadow = true;
    mesh.matrixAutoUpdate = false;
    mesh.computeBoundingSphere();
    root.add(mesh);
    staticMeshes.push(mesh);
  }
  for (const rec of deskRecords) {
    rec.screen.castShadow = false;
    rec.screen.receiveShadow = false;
  }

  // -- lights --
  const hemi = new THREE.HemisphereLight(0xfff3df, 0xb8a58a, 0.95);
  hemi.name = "hemisphere";
  scene.add(hemi);
  const sun = new THREE.DirectionalLight(0xffe7c4, 3.0);
  sun.name = "sun";
  sun.position.set(9, 12, 5); // low enough that shadows fall visibly to the left and back
  sun.target.position.set(0, 0, 0);
  sun.shadow.mapSize.set(1024, 1024);
  sun.shadow.camera.left = -12;
  sun.shadow.camera.right = 12;
  sun.shadow.camera.top = 12;
  sun.shadow.camera.bottom = -12;
  sun.shadow.camera.near = 1;
  sun.shadow.camera.far = 45;
  sun.shadow.bias = -0.0004;
  sun.shadow.normalBias = 0.03;
  scene.add(sun, sun.target);

  // -- controls and fit --
  const home = new THREE.Vector3(0, 0.6, 0.2);
  const controls = createControls(camera, canvas, home);

  /** Distance at which every corner of the room box is inside the frustum. */
  function computeFitDistance() {
    const vFov = THREE.MathUtils.degToRad(camera.fov) / 2;
    const tanV = Math.tan(vFov);
    const tanH = tanV * camera.aspect;
    const back = controls.direction.clone(); // target -> camera
    const upRef = new THREE.Vector3(0, 1, 0);
    const rightV = new THREE.Vector3().crossVectors(upRef, back).normalize();
    const upV = new THREE.Vector3().crossVectors(back, rightV).normalize();
    const pad = 0.6;
    let fit = 0;
    for (const x of [ROOM.minX - pad, ROOM.maxX + pad]) {
      for (const y of [-0.4, ROOM.wallHeight]) {
        for (const z of [ROOM.minZ - pad, ROOM.maxZ + pad]) {
          const v = new THREE.Vector3(x, y, z).sub(home);
          const depthOffset = v.dot(back); // positive = toward camera
          fit = Math.max(fit, depthOffset + Math.abs(v.dot(rightV)) / tanH, depthOffset + Math.abs(v.dot(upV)) / tanV);
        }
      }
    }
    return fit * 1.04;
  }

  // -- quality --
  function pixelRatioFor(q) {
    const dpr = window.devicePixelRatio || 1;
    return q === "low" ? Math.min(dpr, 1) * 0.75 : Math.min(dpr, 2);
  }
  function applyShadows(on) {
    renderer.shadowMap.enabled = on;
    sun.castShadow = on;
    if (!on && sun.shadow.map) {
      sun.shadow.map.dispose();
      sun.shadow.map = null;
    }
    for (const m of materials) m.needsUpdate = true;
  }

  function resize() {
    const w = Math.max(1, canvas.clientWidth || window.innerWidth);
    const h = Math.max(1, canvas.clientHeight || window.innerHeight);
    renderer.setPixelRatio(pixelRatioFor(quality));
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    const fit = computeFitDistance();
    camera.far = fit * 4;
    camera.near = Math.max(0.5, fit * 0.05);
    camera.updateProjectionMatrix();
    controls.setFit(fit, h);
  }

  function setQuality(q) {
    if (!QUALITY_LEVELS.includes(q)) throw new RangeError(`unknown quality "${q}"`);
    quality = q;
    applyShadows(q !== "low");
    resize();
    return quality;
  }

  // -- agent desks --
  /**
   * Mark desk `index` (0..7) as used by an agent: its monitor turns on.
   * Returns the layout entry for the desk plus its scene group and screen mesh.
   * The desks themselves are always built; call this once per agent.
   */
  function addAgentDesk(index) {
    const d = layout.deskForAgent(index); // throws RangeError on a bad index
    const rec = deskRecords[index];
    rec.occupied = true;
    rec.screen.material = MAT.screenOn;
    return { ...d, group: rec.group, screen: rec.screen };
  }

  // -- loop --
  const frameCallbacks = new Set();
  let frameList = []; // the same callbacks as an array, rebuilt only when one is added or removed
  let running = false;
  let firstFrame = false;
  let lastTime = 0;
  let elapsed = 0;
  let resizeObserver = null;
  let resolveReady;
  const ready = new Promise((r) => {
    resolveReady = r;
  });

  function frame(timeMs) {
    const dt = lastTime ? Math.min(0.1, (timeMs - lastTime) / 1000) : 0;
    lastTime = timeMs;
    elapsed += dt;
    const list = frameList; // a callback may add or remove callbacks: this frame keeps its own list
    for (let i = 0; i < list.length; i++) list[i](dt, elapsed);
    renderer.render(scene, camera);
    if (!firstFrame) {
      firstFrame = true;
      setLoadingProgress(1, TEXT.loading.ready);
      if (options.autoHideLoading !== false) requestAnimationFrame(hideLoading);
      resolveReady(renderer.info.render);
    }
  }

  function start() {
    if (running) return ready;
    running = true;
    lastTime = 0;
    resize();
    if (typeof ResizeObserver !== "undefined") {
      resizeObserver = new ResizeObserver(() => resize());
      resizeObserver.observe(canvas);
    } else {
      window.addEventListener("resize", resize);
    }
    document.addEventListener("visibilitychange", onVisibility);
    if (!document.hidden) renderer.setAnimationLoop(frame);
    return ready;
  }
  // A hidden tab neither updates nor renders (some browsers only slow requestAnimationFrame down).
  // On return the clock restarts, so the paused time does not show up as one big step.
  function onVisibility() {
    if (!running) return;
    if (document.hidden) {
      renderer.setAnimationLoop(null);
    } else {
      lastTime = 0;
      renderer.setAnimationLoop(frame);
    }
  }
  function stop() {
    running = false;
    document.removeEventListener("visibilitychange", onVisibility);
    renderer.setAnimationLoop(null);
    resizeObserver?.disconnect();
    resizeObserver = null;
    window.removeEventListener("resize", resize);
  }
  function onFrame(cb) {
    frameCallbacks.add(cb);
    frameList = [...frameCallbacks];
    return () => {
      frameCallbacks.delete(cb);
      frameList = [...frameCallbacks];
    };
  }
  function getStats() {
    const r = renderer.info.render;
    return {
      drawCalls: r.calls,
      triangles: r.triangles,
      geometries: renderer.info.memory.geometries,
      textures: renderer.info.memory.textures,
      instancedBatches: staticMeshes.length,
      quality,
      pixelRatio: renderer.getPixelRatio(),
      shadows: renderer.shadowMap.enabled,
    };
  }
  function dispose() {
    stop();
    controls.dispose();
    for (const g of Object.values(GEO)) g.dispose();
    for (const m of materials) m.dispose();
    floorTexture.dispose();
    for (const m of staticMeshes) m.dispose();
    renderer.dispose();
  }

  // initial state
  applyShadows(quality !== "low");
  resize();
  controls.reset();
  setLoadingProgress(0.95, TEXT.loading.firstFrame);

  return {
    scene,
    camera,
    renderer,
    layout,
    controls,
    root,
    ready,
    start,
    stop,
    resize,
    setQuality,
    getQuality: () => quality,
    addAgentDesk,
    onFrame,
    setLoadingProgress,
    hideLoading,
    getStats,
    dispose,
  };
}
