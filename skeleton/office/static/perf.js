// Performance helpers (task 8.8, Requirement 2.10).
//
// Two independent parts:
//
//  1. The probe, only active with `?perf=1`. `startPerfProbe()` returns null without the flag, so
//     nothing is hooked, wrapped or drawn in a normal visit. With the flag it shows a small overlay
//     (frame time, draw calls, triangles, geometries, textures, JS update time per frame) and exposes
//     `window.__perf.snapshot()` which returns the same numbers (and more) as plain JSON.
//
//  2. `createSlowFrameMonitor()`, a pure function object that main.js feeds with every frame time.
//     It says "yes" once when frames stayed slow for a few seconds, so the page can suggest the
//     low-quality mode. It never switches anything by itself.
//
// No Three.js import and no browser access at import time, so tests can load this file in Node.

export const SLOW_FRAME_MS = 40;       // above this rolling frame time the frames count as slow
export const SLOW_FOR_SECONDS = 3;     // how long they must stay slow before the hint is offered
const WINDOW = 120;                    // frames in the rolling averages of the probe
const OVERLAY_REFRESH_MS = 500;
const SCENE_REFRESH_MS = 5000;
const MAX_FRAME_GAP_MS = 5000;         // a longer gap between frames is a pause, not a frame time

/** True when the page URL asks for the probe (`?perf=1`). */
export function perfEnabled(search) {
  try {
    return new URLSearchParams(search ?? "").get("perf") === "1";
  } catch {
    return false;
  }
}

// ---- pure helpers (tested in tests/js/perf.test.mjs) -------------------------------------

/** Rolling average over the last `size` numbers. No allocation after creation. */
export function createRollingAverage(size = WINDOW) {
  const values = new Float64Array(size);
  let count = 0;
  let next = 0;
  let sum = 0;
  let max = 0;
  return {
    push(v) {
      if (count === size) sum -= values[next];
      else count++;
      values[next] = v;
      sum += v;
      next = (next + 1) % size;
    },
    get average() { return count ? sum / count : 0; },
    get max() {
      max = 0;
      for (let i = 0; i < count; i++) if (values[i] > max) max = values[i];
      return max;
    },
    get count() { return count; },
    reset() { count = 0; next = 0; sum = 0; },
  };
}

/** Estimated GPU bytes of an RGBA8 texture, with a full mipmap chain when `mipmaps` is true. */
export function estimateTextureBytes(width, height, mipmaps = true) {
  if (!(width > 0) || !(height > 0)) return 0;
  const base = width * height * 4;
  return Math.round(mipmaps ? (base * 4) / 3 : base);
}

/**
 * Decides when to suggest the low-quality mode. Feed it every frame time in milliseconds with
 * `push(ms)`. It returns true exactly once: when the smoothed frame time has been above
 * `thresholdMs` for `forSeconds` in a row. Time spent in `warmupSeconds` after the first frame is
 * ignored (shader compilation makes the first frames slow on every machine).
 */
export function createSlowFrameMonitor({
  thresholdMs = SLOW_FRAME_MS,
  forSeconds = SLOW_FOR_SECONDS,
  warmupSeconds = 2,
  smoothing = 0.1,
} = {}) {
  let average = 0;
  let age = 0;
  let slow = 0;
  let fired = false;
  return {
    push(ms) {
      if (!(ms > 0) || !Number.isFinite(ms)) return false; // the first frame has dt 0
      average = average === 0 ? ms : average + (ms - average) * smoothing;
      age += ms / 1000;
      if (age < warmupSeconds) return false;
      if (average > thresholdMs) slow += ms / 1000;
      else slow = 0;
      if (slow >= forSeconds && !fired) {
        fired = true;
        return true;
      }
      return false;
    },
    get average() { return average; },
    get fired() { return fired; },
    /** Start counting again (after the owner dismissed the hint, for example). */
    reset() { average = 0; age = 0; slow = 0; fired = false; },
  };
}

// ---- the probe -----------------------------------------------------------------------------

const MB = 1024 * 1024;
const round = (v, digits = 2) => Math.round(v * 10 ** digits) / 10 ** digits;

function summarizeResources() {
  const groups = {};
  const add = (name, entry) => {
    const g = (groups[name] ??= { files: 0, transferBytes: 0, bodyBytes: 0 });
    g.files++;
    g.transferBytes += entry.transferSize || 0;
    g.bodyBytes += entry.encodedBodySize || 0;
  };
  let total = 0;
  for (const e of performance.getEntriesByType("resource")) {
    const path = new URL(e.name, location.href).pathname;
    const m = /\/(assets\/[^/]+|vendor)\//.exec(path);
    add(m ? m[1] : "code-and-html", e);
    add("total", e);
    total++;
  }
  for (const g of Object.values(groups)) {
    g.transferMB = round(g.transferBytes / MB);
    g.bodyMB = round(g.bodyBytes / MB);
  }
  return { entries: total, groups };
}

/**
 * Start the probe. Returns null (and does nothing at all) unless the URL has `perf=1`.
 *
 * It registers a frame callback that runs before every other callback, and wraps
 * `renderer.render` to time the render call. Everything the other callbacks do between those two
 * points (behavior, mixers, overlay projection) is the "JS update" time of the frame.
 *
 * @param office   createOffice() result
 * @param opts     { search, getExtra(): object merged into the snapshot }
 */
export function startPerfProbe(office, { search = globalThis.location?.search, getExtra } = {}) {
  if (!perfEnabled(search)) return null;
  const { renderer, scene } = office;
  const frameMs = createRollingAverage();
  const updateMs = createRollingAverage();
  const renderMs = createRollingAverage();
  const marks = {};
  let frameStart = 0;
  let lastStart = 0;
  let frames = 0;
  let firstFrameAt = null;

  const now = () => performance.now();
  office.onFrame(() => {
    frameStart = now();
    // Gaps after a hidden tab or a long stall are not frame times. (Software rendering can take
    // over a second per frame, so the cutoff is generous.)
    if (lastStart && frameStart - lastStart < MAX_FRAME_GAP_MS) frameMs.push(frameStart - lastStart);
    lastStart = frameStart;
  });
  const render = renderer.render.bind(renderer);
  renderer.render = (s, c) => {
    const t0 = now();
    if (frameStart) updateMs.push(t0 - frameStart);
    render(s, c);
    renderMs.push(now() - t0);
    frames++;
  };
  office.ready.then(() => { firstFrameAt = now(); });

  /** Name a moment of the page load, in ms since navigation start (main.js calls this). */
  function mark(name) {
    marks[name] = round(now(), 1);
  }

  function sceneStats() {
    const geometries = new Set();
    const materials = new Set();
    const skeletons = new Set();
    const textures = new Map();
    let skinnedMeshes = 0;
    let skinnedVertices = 0;
    let visibleMeshes = 0;
    scene.traverseVisible((o) => {
      if (!o.isMesh) return;
      visibleMeshes++;
      geometries.add(o.geometry);
      if (o.isSkinnedMesh) {
        skinnedMeshes++;
        skinnedVertices += o.geometry.attributes.position.count;
        if (o.skeleton) skeletons.add(o.skeleton);
      }
      for (const m of Array.isArray(o.material) ? o.material : [o.material]) {
        materials.add(m);
        for (const v of Object.values(m)) {
          // Texture objects that share one image (same Source) share one GPU upload: count it once.
          if (v?.isTexture && !textures.has(v.source ?? v) && renderer.properties.get(v).__webglTexture) textures.set(v.source ?? v, v);
        }
      }
    });
    const texList = [];
    let textureBytes = 0;
    for (const t of textures.values()) {
      const img = t.image ?? t.source?.data;
      const w = img?.width ?? 0;
      const h = img?.height ?? 0;
      const bytes = estimateTextureBytes(w, h, t.generateMipmaps);
      textureBytes += bytes;
      texList.push({ name: t.name || t.source?.data?.src?.split("/").pop() || t.uuid.slice(0, 8), size: `${w}x${h}`, MB: round(bytes / MB) });
    }
    texList.sort((a, b) => b.MB - a.MB);
    let shadowBytes = 0;
    scene.traverse((o) => {
      const map = o.isLight && o.shadow?.map;
      if (map) shadowBytes += map.width * map.height * 4; // one 32-bit depth value per texel
    });
    const size = { x: renderer.domElement.width, y: renderer.domElement.height }; // drawing buffer in device pixels
    return {
      visibleMeshes,
      uniqueGeometries: geometries.size,
      uniqueMaterials: materials.size,
      skinnedMeshes,
      skinnedVertices,
      skeletons: skeletons.size,
      boneCount: [...skeletons].reduce((n, s) => n + s.bones.length, 0),
      uploadedTextures: textures.size,
      textureMB: round(textureBytes / MB),
      textureTop: texList.slice(0, 8),
      shadowMapMB: round(shadowBytes / MB),
      framebufferMB: round((size.x * size.y * 4 * 2) / MB), // color + depth
      drawingBuffer: `${size.x}x${size.y}`,
    };
  }

  /** All numbers as plain JSON. Averages cover the last 120 frames. */
  function snapshot(withScene = true) {
    const info = renderer.info;
    const avg = frameMs.average;
    return {
      quality: office.getQuality(),
      frames,
      frameMsAvg: round(avg),
      frameMsMax: round(frameMs.max),
      fpsFromFrameMs: avg > 0 ? round(1000 / avg, 1) : 0,
      jsUpdateMsAvg: round(updateMs.average, 3),
      jsUpdateMsMax: round(updateMs.max, 3),
      renderCpuMsAvg: round(renderMs.average, 3),
      drawCalls: info.render.calls,
      triangles: info.render.triangles,
      geometries: info.memory.geometries,
      textures: info.memory.textures,
      programs: info.programs?.length ?? 0,
      pixelRatio: renderer.getPixelRatio(),
      shadows: renderer.shadowMap.enabled,
      hidden: typeof document !== "undefined" ? document.hidden : false,
      firstFrameMs: firstFrameAt == null ? null : round(firstFrameAt, 1),
      marks: { ...marks },
      scene: withScene ? sceneStats() : undefined,
      resources: withScene ? summarizeResources() : undefined,
      ...(getExtra ? getExtra() : null),
    };
  }

  function resetAverages() {
    frameMs.reset();
    updateMs.reset();
    renderMs.reset();
    lastStart = 0;
  }

  // ---- overlay ----
  const box = document.createElement("pre");
  box.id = "perf-overlay";
  box.setAttribute("aria-hidden", "true");
  box.style.cssText = "position:fixed;right:8px;bottom:8px;z-index:9999;margin:0;padding:6px 8px;"
    + "font:11px/1.35 ui-monospace,Menlo,Consolas,monospace;color:#d8ffd8;background:rgba(0,0,0,.72);"
    + "border-radius:6px;pointer-events:none;white-space:pre";
  document.body.append(box);
  let sceneMB = 0;
  let sceneMBAt = -Infinity;
  function refresh() {
    const s = snapshot(false); // the scene walk is heavier: do it only every few seconds
    if (now() - sceneMBAt > SCENE_REFRESH_MS) {
      sceneMB = sceneStats().textureMB;
      sceneMBAt = now();
    }
    box.textContent = [
      `frame   ${s.frameMsAvg.toFixed(1)} ms avg, ${s.frameMsMax.toFixed(1)} max`,
      `js      ${s.jsUpdateMsAvg.toFixed(2)} ms update, ${s.renderCpuMsAvg.toFixed(2)} ms render call`,
      `draws   ${s.drawCalls}   tris ${s.triangles}`,
      `geo     ${s.geometries}   tex ${s.textures} (${sceneMB} MB)`,
      `quality ${s.quality}  shadows ${s.shadows ? "on" : "off"}  px ${s.pixelRatio}`,
    ].join("\n");
  }
  const timer = setInterval(refresh, OVERLAY_REFRESH_MS);
  refresh();

  const api = {
    snapshot,
    mark,
    reset: resetAverages,
    stop() {
      clearInterval(timer);
      box.remove();
    },
  };
  window.__perf = api;
  return api;
}
