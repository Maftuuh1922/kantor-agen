// Characters: loading, seeded looks, skeletal animation and procedural layers.
//
// ES module, no bundler. Uses the vendored three.js through the page's importmap.
//
//   const assets = await loadAssets("./assets/manifest.json", onProgress);
//   const c = createCharacter(assets, "agent-7");
//   scene.add(c.object3D);
//   office.onFrame((dt) => c.update(dt));
//   c.setBehavior("type");
//
// Frame conventions (same as layout.js): a character faces +z at yaw 0, units are meters,
// `object3D` stands on the floor. Its left side is +x.
//
// Sharing (Req 10): geometry, textures, skinned-mesh bind data and animation clips are
// loaded once and shared by every character. Only the materials (and the skeleton and
// mixer, which hold per-character pose state) are cloned per agent.
import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import * as SkeletonUtils from "three/addons/utils/SkeletonUtils.js";
import {
  stream, traitsForAgent, resolveGender, genderFromName, genderFromId, BEARD_ID,
  SHIRT_COLORS, PANTS_COLORS, SHOE_COLORS, HAIR_COLORS, SKIN_TONES, BLOUSE_COLORS, MEN_SHIRT_COLORS, BATIK_COLORS,
  HIJAB_COLORS, FEMALE_NAMES, MALE_NAMES,
} from "./char-look.js";

// ---- constants ---------------------------------------------------------------------

/** Cross-fade time between clips, in seconds (Req 2.3). */
export const CROSSFADE = 0.3;
/** Ramp time for the procedural layers, in seconds. */
export const LAYER_RAMP = 0.3;
/** Walk_Loop moves the body at this speed when played with timeScale 1 (m/s). */
export const WALK_NATURAL_SPEED = 0.97;
/** Default walking speed of the office (m/s). timeScale = 1.2 / 0.97 = about 1.24. */
export const WALK_SPEED = 1.2;
/** Seated pelvis: 0.54 m above the floor and 0.24 m behind the standing position. */
export const SEAT_PELVIS_HEIGHT = 0.54;
export const SEAT_PELVIS_BACK = 0.24;
/** Clip keys this module plays, resolved through manifest `clip_map` and `extra_clips`. */
export const REQUIRED_CLIPS = Object.freeze(["idle", "walk", "sit_down", "sit_idle", "talk"]);
export const OPTIONAL_CLIPS = Object.freeze(["sit_talk", "stand_up"]);
/** Behaviors that `setBehavior` accepts. `raise_hand` is an alias of `wave`. */
export const BEHAVIORS = Object.freeze([
  "idle", "walk", "sit", "sit_idle", "stand", "type", "talk", "wave", "raise_hand", "stretch", "drink",
]);

// The look (gender, palettes, proportions, clothes) is pure data picked by agent id in char-look.js.
// The palettes are re-exported here for callers that import them from this module.
export {
  SHIRT_COLORS, PANTS_COLORS, SHOE_COLORS, HAIR_COLORS, SKIN_TONES, BLOUSE_COLORS, MEN_SHIRT_COLORS, BATIK_COLORS,
  HIJAB_COLORS, FEMALE_NAMES, MALE_NAMES, resolveGender, genderFromName, genderFromId,
};
// The hair texture is a light grey, so a tint multiplies it down. Compensate a bit.
const HAIR_GAIN = 1.45;

// Desk geometry in the character frame while seated at a desk (see office.js / layout.js):
// the pelvis is above the seat, the keyboard is 0.55 m in front of the seat, the desk top is 0.73 m.
const TYPING = Object.freeze({
  wrist: { x: 0.105, y: 0.795, z: 0.205 },   // relative to the root, wrist rests here
  monitor: { x: 0, y: 1.07, z: 0.66 },        // gaze target while typing
});

// ---- small helpers -----------------------------------------------------------------

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const smooth = (x) => { const t = clamp(x, 0, 1); return t * t * (3 - 2 * t); };
const smoother = (x) => { const t = clamp(x, 0, 1); return t * t * t * (t * (t * 6 - 15) + 10); };
const approach = (v, goal, step) => (v < goal ? Math.min(goal, v + step) : Math.max(goal, v - step));

// ---- loading -----------------------------------------------------------------------

function disposeObject(root) {
  root.traverse((o) => {
    o.geometry?.dispose?.();
    const mats = Array.isArray(o.material) ? o.material : o.material ? [o.material] : [];
    for (const m of mats) {
      for (const v of Object.values(m)) if (v?.isTexture) v.dispose();
      m.dispose();
    }
  });
}

function skinnedMeshes(root) {
  const out = [];
  root.traverse((o) => { if (o.isSkinnedMesh) out.push(o); });
  return out;
}

function bonesByName(root) {
  const map = Object.create(null);
  root.traverse((o) => { if (o.isBone) map[o.name] = o; });
  return map;
}

// Vertex attribute `aCloth` (vec4): which part of a body vertex is clothing, from the skin
// weights of the body mesh. x = shirt (torso, shoulders, upper arm down to a short sleeve),
// y = trousers, z = shoes, w = extra long-sleeve part (forearm). Built once per body mesh.
function buildClothAttribute(mesh) {
  const geo = mesh.geometry;
  if (geo.getAttribute("aCloth")) return;
  const skel = mesh.skeleton;
  const names = skel.bones.map((b) => b.name);
  const idx = Object.fromEntries(names.map((n, i) => [n, i]));
  const bindPos = skel.boneInverses.map((m) => new THREE.Vector3().setFromMatrixPosition(m.clone().invert()));
  const pos = geo.getAttribute("position");
  const si = geo.getAttribute("skinIndex");
  const sw = geo.getAttribute("skinWeight");
  const out = new Float32Array(pos.count * 4);
  const bindOut = new Float32Array(pos.count * 3); // bind-pose position (metres), drives shirt patterns
  const v = new THREE.Vector3();
  const ab = new THREE.Vector3();
  const ap = new THREE.Vector3();
  // group of each bone: "shirt", "pants", "shoes", or the segment kinds below
  const kind = names.map((n) => {
    if (/^(spine_0\d|clavicle_[lr])$/.test(n)) return "shirt";
    if (/^upperarm_[lr]$/.test(n)) return "upper";
    if (/^lowerarm_[lr]$/.test(n)) return "lower";
    if (/^(pelvis|thigh_[lr])$/.test(n)) return "pants";
    if (/^calf_[lr]$/.test(n)) return "calf";
    if (/^(foot|ball)_[lr]$/.test(n) || /^ball_leaf_[lr]$/.test(n)) return "shoes";
    return "skin";
  });
  // segment end bone for the param t
  const segEnd = names.map((n, i) => {
    if (kind[i] === "upper") return idx[n.replace("upperarm", "lowerarm")];
    if (kind[i] === "lower") return idx[n.replace("lowerarm", "hand")];
    if (kind[i] === "calf") return idx[n.replace("calf", "foot")];
    return undefined;
  });
  for (let i = 0; i < pos.count; i++) {
    v.fromBufferAttribute(pos, i).applyMatrix4(mesh.bindMatrix);
    bindOut[i * 3] = v.x; bindOut[i * 3 + 1] = v.y; bindOut[i * 3 + 2] = v.z;
    let x = 0, y = 0, z = 0, w = 0;
    for (let k = 0; k < 4; k++) {
      const wt = sw.getComponent(i, k);
      if (wt <= 0) continue;
      const b = si.getComponent(i, k);
      const kd = kind[b];
      let t = 0;
      if (segEnd[b] !== undefined) {
        ab.subVectors(bindPos[segEnd[b]], bindPos[b]);
        ap.subVectors(v, bindPos[b]);
        t = ap.dot(ab) / Math.max(1e-6, ab.lengthSq());
      }
      if (kd === "shirt") x += wt;
      else if (kd === "upper") {
        const keep = 1 - smooth((t - 0.30) / 0.30); // short sleeve ends mid upper arm
        x += wt * keep;
        w += wt * (1 - keep);
      } else if (kd === "lower") w += wt * (1 - smooth((t - 0.72) / 0.18)); // cuff before the wrist
      else if (kd === "pants") y += wt;
      else if (kd === "calf") {
        const shoe = smooth((t - 0.80) / 0.14);
        y += wt * (1 - shoe);
        z += wt * shoe;
      } else if (kd === "shoes") z += wt;
    }
    out[i * 4] = x; out[i * 4 + 1] = y; out[i * 4 + 2] = z; out[i * 4 + 3] = w;
  }
  geo.setAttribute("aCloth", new THREE.BufferAttribute(out, 4));
  geo.setAttribute("aBind", new THREE.BufferAttribute(bindOut, 3));
}

/** Absolute, normalized URL ("a/../b" becomes "b"), so the same file always has the same cache key. */
function normalizeUrl(url) {
  if (/^(data|blob):/i.test(url)) return url;
  try {
    return new URL(url, globalThis.location?.href ?? import.meta.url).href;
  } catch {
    return url;
  }
}

/**
 * Every character and hair file lists the same PNGs (skin, hair, eyes) as its own textures, so
 * after loading, the 8 agents' 10 files hold dozens of copies. With the URL cache on, copies of
 * one file wrap the same decoded image. Give those textures one shared `Source`, and three.js
 * uploads that image to the GPU once. Returns the number of textures that were re-pointed.
 */
export function shareTextureSources(roots) {
  const byImage = new Map(); // decoded image -> the Source everyone should use
  let shared = 0;
  for (const root of roots) {
    root.traverse((o) => {
      if (!o.material) return;
      for (const m of Array.isArray(o.material) ? o.material : [o.material]) {
        for (const t of Object.values(m)) {
          const image = t?.isTexture ? t.source?.data : null;
          if (!image) continue;
          const first = byImage.get(image);
          if (!first) byImage.set(image, t.source);
          else if (t.source !== first) {
            t.source = first;
            shared++;
          }
        }
      }
    });
  }
  return shared;
}

/**
 * Load the manifest, the character and hair glTFs and the animation GLBs, in parallel.
 *
 * @param {object|string|null} manifest  parsed manifest, or its URL (default "./assets/manifest.json"
 *                                       next to this module)
 * @param {(p:{loaded:number,total:number,fraction:number,label:string})=>void} [onProgress]
 * @param {{baseUrl?:string}} [options]  folder the manifest paths are relative to
 * @returns the shared assets for `createCharacter`
 */
export async function loadAssets(manifest, onProgress, options = {}) {
  const here = new URL("./assets/", import.meta.url).href;
  const baseUrl = options.baseUrl ?? here;
  const report = typeof onProgress === "function" ? onProgress : () => {};
  if (manifest == null || typeof manifest === "string") {
    const url = manifest ?? new URL("manifest.json", baseUrl).href;
    const res = await fetch(url);
    if (!res.ok) throw new Error(`could not load ${url}: ${res.status}`);
    manifest = await res.json();
  }
  // The URL cache lets the 10 glTF files share the 14 PNGs they list (see shareTextureSources).
  const cacheWasOn = THREE.Cache.enabled;
  THREE.Cache.enabled = true;
  const manager = new THREE.LoadingManager();
  manager.setURLModifier(normalizeUrl);
  const loader = new GLTFLoader(manager);
  // One progress slot per file. Fractions of finished files are 1, others use byte progress.
  const jobs = [];
  const add = (kind, def) => jobs.push({ kind, def, url: new URL(def.file, baseUrl).href, frac: 0, weight: kind === "anim" ? 3 : 1 });
  for (const def of manifest.characters) add("body", def);
  for (const def of manifest.hair) add("hair", def);
  for (const def of manifest.animations) add("anim", def);
  const totalWeight = jobs.reduce((s, j) => s + j.weight, 0);
  let doneCount = 0;
  const emit = (label) => {
    const f = jobs.reduce((s, j) => s + j.frac * j.weight, 0) / totalWeight;
    report({ loaded: doneCount, total: jobs.length, fraction: clamp(f, 0, 1), label });
  };
  emit("Loading characters");
  let results;
  try {
    results = await Promise.all(jobs.map(async (job) => {
      const gltf = await loader.loadAsync(job.url, (ev) => {
        if (ev.lengthComputable && ev.total > 0) {
          job.frac = Math.max(job.frac, Math.min(0.95, ev.loaded / ev.total));
          emit(`Loading ${job.def.id}`);
        }
      });
      job.frac = 1;
      doneCount++;
      emit(`Loaded ${job.def.id}`);
      return { job, gltf };
    }));
  } finally {
    THREE.Cache.enabled = cacheWasOn;
    THREE.Cache.clear(); // the decoded images now live in the textures only
  }
  shareTextureSources(results.map((r) => r.gltf.scene));

  const assets = {
    manifest,
    baseUrl,
    bodies: new Map(),   // id -> { def, scene, mesh, eyes, brows, pelvisRestY, ... }
    hair: new Map(),     // id -> { def, mesh, jointNames }
    clips: new Map(),    // key -> sanitized AnimationClip, shared by all characters
    mannequinPelvisY: 0.9167,
    dispose() {
      for (const b of assets.bodies.values()) disposeObject(b.scene);
      for (const h of assets.hair.values()) disposeObject(h.scene);
    },
  };

  const animFiles = new Map(); // file path -> gltf
  for (const { job, gltf } of results) {
    if (job.kind === "body") {
      const scene = gltf.scene;
      scene.updateMatrixWorld(true);
      const meshes = skinnedMeshes(scene);
      const matName = (m) => (Array.isArray(m.material) ? m.material[0] : m.material)?.name ?? "";
      const eyes = meshes.find((m) => /eyes/i.test(matName(m)));
      const brows = meshes.find((m) => /^MI_Hair/i.test(matName(m)));
      const body = meshes.filter((m) => m !== eyes && m !== brows).sort((a, b) => b.geometry.attributes.position.count - a.geometry.attributes.position.count)[0];
      if (!body) throw new Error(`no skinned body mesh in ${job.def.file}`);
      buildClothAttribute(body);
      const bones = bonesByName(scene);
      const p = new THREE.Vector3().setFromMatrixPosition(bones.pelvis.matrixWorld);
      // Rest position of the mouth, in the Head bone's local space (used by the drink layer).
      const mouthLocal = new THREE.Vector3(0, 0, 0.1);
      if (eyes) {
        eyes.geometry.computeBoundingBox();
        const c = eyes.geometry.boundingBox.getCenter(new THREE.Vector3()).applyMatrix4(eyes.matrixWorld);
        c.y -= 0.062; c.z += 0.012; // eyes -> mouth, measured on the pack's head
        mouthLocal.copy(bones.Head.worldToLocal(c));
      }
      assets.bodies.set(job.def.id, { def: job.def, scene, body, eyes, brows, pelvisRestY: p.y, mouthLocal });
    } else if (job.kind === "hair") {
      const mesh = skinnedMeshes(gltf.scene)[0];
      if (!mesh) throw new Error(`no skinned mesh in ${job.def.file}`);
      assets.hair.set(job.def.id, {
        def: job.def,
        scene: gltf.scene,
        mesh,
        jointNames: mesh.skeleton.bones.map((b) => b.name),
      });
    } else {
      animFiles.set(job.def.file, gltf);
    }
  }

  // Mannequin pelvis height: the animation rig is about 3 cm lower than the characters.
  const firstAnim = animFiles.values().next().value;
  if (firstAnim) {
    firstAnim.scene.updateMatrixWorld(true);
    const ab = bonesByName(firstAnim.scene);
    if (ab.pelvis) assets.mannequinPelvisY = new THREE.Vector3().setFromMatrixPosition(ab.pelvis.matrixWorld).y;
  }

  // Resolve clip keys through the manifest's clip map, keep only rotation tracks plus the
  // pelvis position. Position tracks of the other bones are the mannequin's bone offsets and
  // would stretch the characters (their proportions differ slightly). Scale tracks are dropped.
  const specs = { ...manifest.clip_map, ...manifest.extra_clips };
  for (const [key, spec] of Object.entries(specs)) {
    if (!spec) continue; // type, wave, stretch, drink: procedural
    const gltf = animFiles.get(spec.file);
    const src = gltf?.animations.find((c) => c.name === spec.clip);
    if (!src) {
      if (REQUIRED_CLIPS.includes(key)) throw new Error(`manifest clip "${spec.clip}" (${key}) not found in ${spec.file}`);
      continue;
    }
    const tracks = src.tracks.filter((t) => t.name.endsWith(".quaternion") || t.name === "pelvis.position").map((t) => t.clone());
    const clip = new THREE.AnimationClip(key, src.duration, tracks);
    assets.clips.set(key, clip);
  }
  for (const key of REQUIRED_CLIPS) if (!assets.clips.has(key)) throw new Error(`required clip "${key}" is not in the manifest clip_map`);
  // The animation GLBs also carry mannequin meshes; the clips are all we need.
  for (const gltf of animFiles.values()) disposeObject(gltf.scene);
  report({ loaded: jobs.length, total: jobs.length, fraction: 1, label: "Characters ready" });
  return assets;
}

// ---- looks (seeded by agent id) ------------------------------------------------------

/**
 * Pick the look of an agent. Pure function of the agent id, the options and the catalogue of
 * `assets`, so the same agent looks the same on every reload (Req 2.2). Returns plain data (see
 * `traitsForAgent` in char-look.js). Gender comes from `overrides.gender`, then the agent's
 * `overrides.name`, then the id. `overrides.look` replaces any seeded field.
 */
export function lookForAgent(agentId, assets, overrides = {}) {
  return traitsForAgent(agentId, { ...overrides, availableHair: assets ? [...assets.hair.keys()] : undefined });
}

/** Where `object3D` goes so that a seated character's pelvis is over `seat` (x, z) at yaw `rotation`. */
export function seatedRoot(seat, rotation) {
  return { x: seat.x + Math.sin(rotation) * SEAT_PELVIS_BACK, y: 0, z: seat.z + Math.cos(rotation) * SEAT_PELVIS_BACK };
}

// ---- materials -----------------------------------------------------------------------

function makeBodyMaterial(base, look) {
  const m = base.clone();
  m.name = `${base.name}:${look.agentId}`;
  m.color.setRGB(look.skin.rgb[0], look.skin.rgb[1], look.skin.rgb[2], THREE.LinearSRGBColorSpace);
  const uniforms = {
    uShirt: { value: new THREE.Color(look.shirt.hex) },
    uShirt2: { value: new THREE.Color(look.pattern.hex2 ?? look.shirt.hex) },
    uPattern: { value: look.pattern.id },
    uPants: { value: new THREE.Color(look.pants.hex) },
    uShoes: { value: new THREE.Color(look.shoes.hex) },
    uLong: { value: look.longSleeves ? 1 : 0 },
  };
  m.userData.clothUniforms = uniforms;
  m.onBeforeCompile = (shader) => {
    Object.assign(shader.uniforms, uniforms);
    shader.vertexShader = shader.vertexShader
      .replace("#include <common>", "#include <common>\nattribute vec4 aCloth;\nattribute vec3 aBind;\nvarying vec4 vCloth;\nvarying vec3 vBind;")
      .replace("#include <begin_vertex>", "#include <begin_vertex>\nvCloth = aCloth;\nvBind = aBind;");
    shader.fragmentShader = shader.fragmentShader
      .replace("#include <common>", `#include <common>
varying vec4 vCloth;
varying vec3 vBind;
uniform vec3 uShirt;
uniform vec3 uShirt2;
uniform vec3 uPants;
uniform vec3 uShoes;
uniform float uLong;
uniform float uPattern;
// Shirt pattern from the bind-pose position (cylindrical around the body), so it follows the
// body and needs no UVs. 0 plain, 1 stripes, 2 checks, 3 batik-like diamonds and dots.
// aa is the size of one pixel in cells: when a cell is about a pixel, the pattern fades to its
// average colour instead of shimmering.
vec3 shirtPattern(vec3 base) {
  if (uPattern < 0.5) return base;
  vec2 q = vec2(atan(vBind.x, vBind.z) * 0.17, vBind.y);
  vec2 w = max(fwidth(q), vec2(1e-4));
  float m, aa, avg;
  if (uPattern < 1.5) {                      // thin vertical stripes, 3.5 cm apart
    aa = w.x / 0.035;
    m = 1.0 - smoothstep(0.10, 0.10 + aa, abs(fract(q.x / 0.035) - 0.5));
    avg = 0.2;
  } else if (uPattern < 2.5) {               // checks: 5 cm cells with a thin line on every cell edge
    aa = max(w.x, w.y) / 0.05;
    vec2 e = 0.5 - abs(fract(q / 0.05) - 0.5);
    m = 1.0 - smoothstep(0.06, 0.06 + aa, min(e.x, e.y));
    avg = 0.22;
  } else {                                   // batik: a thin diamond lattice with a dot in every diamond, 8 cm cells
    vec2 cell = q / 0.08;
    vec2 g = fract(cell) - 0.5;
    vec2 h = fract(cell + 0.5) - 0.5;
    aa = max(w.x, w.y) / 0.08;
    float outline = 1.0 - smoothstep(0.02, 0.02 + aa, abs(abs(g.x) + abs(g.y) - 0.34));
    float dot1 = 1.0 - smoothstep(0.08, 0.08 + aa, length(g));
    float dot2 = 1.0 - smoothstep(0.05, 0.05 + aa, length(h));
    m = clamp(outline * 0.9 + dot1 + dot2 * 0.8, 0.0, 1.0);
    avg = 0.28;
  }
  float near = 1.0 - smoothstep(0.25, 0.6, aa);
  return mix(base, uShirt2, mix(avg, m, near));
}`)
      .replace("#include <map_fragment>", `#include <map_fragment>
{
  float shirtAmt = vCloth.x + vCloth.w * uLong;
  float tot = shirtAmt + vCloth.y + vCloth.z;
  float cover = smoothstep(0.45, 0.62, tot);
  float inv = 1.0 / max(tot, 1e-3);
  vec3 cloth = mix(shirtPattern(uShirt), uPants, smoothstep(0.4, 0.6, vCloth.y * inv));
  cloth = mix(cloth, uShoes, smoothstep(0.4, 0.6, vCloth.z * inv));
  diffuseColor.rgb = mix(diffuseColor.rgb, cloth, cover);
}`);
  };
  m.customProgramCacheKey = () => "kantor-cloth-v2";
  return m;
}

function makeHairMaterial(base, hex, label) {
  const m = base.clone();
  m.name = `${base.name}:${label}`;
  m.color.set(hex).multiplyScalar(HAIR_GAIN);
  return m;
}

// ---- ground contact ------------------------------------------------------------------
// The characters' legs are a little longer than the animation mannequin's, so a clip puts the
// soles slightly below the floor. Pose a throwaway copy of the body in each pose once and
// measure how far the lowest vertex is below y = 0. `createCharacter` lifts the model by this.
function measureGround(assets, src, pelvisK) {
  if (src.ground) return src.ground;
  const tmp = SkeletonUtils.clone(src.scene);
  const mesh = skinnedMeshes(tmp).find((m) => m.geometry === src.body.geometry);
  const pelvis = bonesByName(tmp).pelvis;
  const mixer = new THREE.AnimationMixer(tmp);
  const v = new THREE.Vector3();
  const lowest = () => {
    tmp.updateMatrixWorld(true);
    let lo = Infinity;
    const n = mesh.geometry.attributes.position.count;
    for (let i = 0; i < n; i += 2) { // every second vertex is plenty for a 1 cm measure
      mesh.getVertexPosition(i, v).applyMatrix4(mesh.matrixWorld);
      if (v.y < lo) lo = v.y;
    }
    return lo;
  };
  const ground = {};
  for (const [name, key, samples] of [["idle", "idle", 1], ["walk", "walk", 4], ["sit", "sit_idle", 1]]) {
    const clip = assets.clips.get(key);
    const action = mixer.clipAction(clip);
    action.play();
    let sum = 0;
    for (let k = 0; k < samples; k++) {
      mixer.setTime((clip.duration * k) / samples);
      pelvis.position.multiplyScalar(pelvisK);
      sum += lowest();
    }
    action.stop();
    mixer.uncacheAction(clip);
    ground[name] = Math.max(0, -sum / samples);
  }
  mixer.uncacheRoot(tmp);
  src.ground = ground;
  return ground;
}

// ---- body proportions ------------------------------------------------------------------
// The pack has one broad, muscular body per gender. Per character we change it with bone scales
// (cheap, the geometry and the clips stay shared; scale tracks are not in the clips):
//   model     uniform scale = height (about the floor)
//   spine_01  x = shoulder and chest width, z = depth. The clavicles and arms hang below it, so
//             shoulders get narrower and arms thinner with it.
//   neck_01   the inverse of the spine scale, so the neck and head keep their size
//   upperarm  extra thinning of the arm (the forearm inherits it)
//   hand      the inverse of the arm thinning, so hands keep their size
// Bone y axes run along the bones, x and z across them, so none of this changes bone lengths.
function applyBuild(model, bones, build) {
  model.scale.setScalar(build.height);
  bones.spine_01.scale.set(build.width, 1, build.depth);
  bones.neck_01.scale.set(1 / build.width, 1, 1 / build.depth);
  for (const side of ["l", "r"]) {
    bones[`upperarm_${side}`].scale.set(build.arm, 1, build.arm);
    bones[`hand_${side}`].scale.set(1 / build.arm, 1, 1 / build.arm);
  }
}

// ---- headscarf -------------------------------------------------------------------------
// A tinted cloth shell on the Head bone: a lathe around the head and neck down to the collar bones,
// with an oval opening for the face cut in the fragment shader. Shared geometry, own material.
// Numbers are in the Head bone's space (x across, y up, z forward) measured on the pack's head:
// skull x +-0.089, y 0.036 (chin) to 0.217 (top), z -0.104 (back) to 0.122 (nose).
const HIJAB_PROFILE = [
  [0.150, -0.150], [0.125, -0.115], [0.095, -0.080], [0.082, -0.040], [0.090, 0.000], [0.098, 0.050],
  [0.103, 0.090], [0.105, 0.140], [0.097, 0.185], [0.075, 0.215], [0.040, 0.232], [0.000, 0.238],
];
let _hijabGeo = null;
function makeHijab(hex, label) {
  _hijabGeo ??= new THREE.LatheGeometry(HIJAB_PROFILE.map(([r, y]) => new THREE.Vector2(r, y)), 40);
  const mat = new THREE.MeshStandardMaterial({ color: hex, roughness: 0.92, metalness: 0 });
  mat.name = `hijab:${label}`;
  mat.onBeforeCompile = (shader) => {
    shader.vertexShader = shader.vertexShader
      .replace("#include <common>", "#include <common>\nvarying vec3 vShell;")
      .replace("#include <begin_vertex>", "#include <begin_vertex>\nvShell = position;");
    shader.fragmentShader = shader.fragmentShader
      .replace("#include <common>", "#include <common>\nvarying vec3 vShell;")
      .replace("#include <clipping_planes_fragment>", `#include <clipping_planes_fragment>
  { // oval opening for the face (front of the head only)
    vec2 f = vec2(vShell.x / 0.073, (vShell.y - 0.113) / 0.09);
    if (vShell.z > 0.0 && dot(f, f) < 1.0) discard;
  }`);
  };
  mat.customProgramCacheKey = () => "kantor-hijab-v1";
  const mesh = new THREE.Mesh(_hijabGeo, mat);
  mesh.name = "hijab";
  mesh.scale.set(0.93, 1, 1);
  mesh.position.set(0, 0, 0.008);
  mesh.frustumCulled = false;
  mesh.castShadow = true;
  return mesh;
}

// ---- the character -------------------------------------------------------------------

const _v1 = new THREE.Vector3();
const _v2 = new THREE.Vector3();
const _v3 = new THREE.Vector3();
const _v4 = new THREE.Vector3();
const _q1 = new THREE.Quaternion();
const _q2 = new THREE.Quaternion();
const _q3 = new THREE.Quaternion();
const _qId = new THREE.Quaternion();
const _s = new THREE.Vector3();
const AXIS_X = new THREE.Vector3(1, 0, 0);
const AXIS_Y = new THREE.Vector3(0, 1, 0);

// Shared cup prop for the drink layer.
let _cupGeo = null;
let _cupMat = null;

// Bones the procedural layers rotate. Their local rotation is restored before the mixer runs.
const PROC_BONES = [
  "spine_01", "spine_02", "spine_03", "neck_01", "Head", "clavicle_l", "clavicle_r",
  "upperarm_l", "upperarm_r", "lowerarm_l", "lowerarm_r", "hand_l", "hand_r",
];

/**
 * Build one character for `agentId`.
 *
 * options:
 *   gender, name           "male" | "female", and the agent's display name. Gender is the explicit
 *                          option, else a common Indonesian first name, else alternation by id.
 *   look                   partial look (see `traitsForAgent`) that replaces seeded fields
 *   body, hair, skinTone   force a part of the look (the rest stays seeded)
 *   quality                "normal" | "low" (low hides eyebrows and beard)
 *   castShadow             default true
 *
 * Returns { id, look, object3D, model, mixer, bones, play, setBehavior, lookAt, setWalkSpeed,
 *           setQuality, update, dispose, ... }. Call `update(dt)` once per frame.
 */
export function createCharacter(assets, agentId, options = {}) {
  const look = lookForAgent(agentId, assets, options);
  const src = assets.bodies.get(look.bodyId);
  if (!src) throw new Error(`body "${look.bodyId}" was not loaded`);
  const rng = stream(look.agentId, "motion");

  const object3D = new THREE.Group();
  object3D.name = `character-${look.agentId}`;
  const model = SkeletonUtils.clone(src.scene);
  object3D.add(model);
  const bones = bonesByName(model);
  applyBuild(model, bones, look.build);
  const ownMaterials = [];
  const meshes = skinnedMeshes(model);
  let browMesh = null;
  let bodyMesh = null;
  for (const mesh of meshes) {
    const isBody = mesh.geometry === src.body.geometry;
    const isBrows = src.brows && mesh.geometry === src.brows.geometry;
    if (isBody) {
      mesh.material = makeBodyMaterial(mesh.material, look);
      bodyMesh = mesh;
    } else if (isBrows) {
      mesh.material = makeHairMaterial(mesh.material, look.hair.color.hex, look.agentId);
      browMesh = mesh;
    } else {
      mesh.material = mesh.material.clone(); // eyes: own copy, untinted
    }
    ownMaterials.push(mesh.material);
    // Eyes and eyebrows are far too small to show a shadow, but each costs a draw call in the shadow pass.
    mesh.castShadow = options.castShadow !== false && isBody;
    mesh.receiveShadow = true;
    mesh.frustumCulled = false; // bounding spheres come from the bind pose
  }
  model.updateMatrixWorld(true);

  // Hair: shares geometry and bind data with the loaded hair mesh and is bound to THIS
  // character's skeleton (by joint name), then parented next to the body mesh.
  const bodySkeleton = bodyMesh.skeleton;
  const extras = [];
  const ownSkeletons = [];
  function addHair(id, colorHex) {
    const h = assets.hair.get(id);
    if (!h) return null;
    const mat = makeHairMaterial(h.mesh.material, colorHex, `${look.agentId}:${id}`);
    ownMaterials.push(mat);
    const mesh = new THREE.SkinnedMesh(h.mesh.geometry, mat);
    mesh.name = id;
    mesh.position.copy(h.mesh.position);
    mesh.quaternion.copy(h.mesh.quaternion);
    mesh.scale.copy(h.mesh.scale);
    bodyMesh.parent.add(mesh);
    mesh.updateMatrixWorld(true);
    // Same joint order and same inverse bind matrices as the body: reuse its skeleton.
    const sameOrder = h.jointNames.length === bodySkeleton.bones.length && h.jointNames.every((n, i) => n === bodySkeleton.bones[i].name);
    let same = sameOrder;
    if (same) {
      for (let i = 0; i < h.jointNames.length && same; i++) {
        const a = h.mesh.skeleton.boneInverses[i].elements;
        const b = bodySkeleton.boneInverses[i].elements;
        for (let k = 0; k < 16; k++) if (Math.abs(a[k] - b[k]) > 1e-4) { same = false; break; }
      }
    }
    let skeleton = bodySkeleton;
    if (!same) {
      skeleton = new THREE.Skeleton(h.jointNames.map((n) => bones[n]), h.mesh.skeleton.boneInverses);
      ownSkeletons.push(skeleton);
    }
    mesh.bind(skeleton, h.mesh.bindMatrix);
    mesh.castShadow = options.castShadow !== false;
    mesh.receiveShadow = true;
    mesh.frustumCulled = false;
    extras.push(mesh);
    return mesh;
  }
  if (look.hair.id) addHair(look.hair.id, look.hair.color.hex);
  if (look.hijab) {
    const hijab = makeHijab(look.hijab.hex, look.agentId);
    hijab.castShadow = options.castShadow !== false;
    ownMaterials.push(hijab.material);
    bones.Head.add(hijab);
  }
  const beardMesh = look.beard ? addHair(BEARD_ID, look.hair.color.hex) : null;
  if (beardMesh) beardMesh.castShadow = false; // a thin shell on the face: no visible shadow, but a draw call

  // ---- animation ----
  const mixer = new THREE.AnimationMixer(model);
  const phase = rng(); // 0..1: where idle loops start, so characters do not breathe in sync
  const speedJitter = 0.97 + rng() * 0.06;
  const actions = new Map();
  let current = null; // { key, action }
  const state = {
    behavior: "idle",
    seated: false,
    sitReady: false,
    pendingSit: false,
    afterOnce: null,
    t: 0,
    duration: Infinity,
    onDone: null,
    walkSpeed: WALK_SPEED,
  };
  const pelvisK = src.pelvisRestY / assets.mannequinPelvisY; // character legs are about 3.5% longer
  const pelvisRaw = bones.pelvis.position.clone();
  const ground = measureGround(assets, src, pelvisK);
  // Height scales the model about the floor, so the sole offset scales with it. A seated person
  // keeps the pelvis on the cushion: a shorter character is lifted half of the missing leg length
  // (feet hover a little under the desk, the hips sink less), a taller one is not pushed down.
  const hs = look.build.height;
  const seatComp = Math.max(0, 1 - hs) * SEAT_PELVIS_HEIGHT * 0.5;
  const groundFor = (key) => {
    if (key === "walk") return ground.walk * hs;
    if ((key && key.startsWith("sit")) || key === "stand_up") return ground.sit * hs + seatComp;
    return ground.idle * hs;
  };
  let lift = ground.idle * hs;

  function actionFor(key) {
    let a = actions.get(key);
    if (!a) {
      const clip = assets.clips.get(key);
      if (!clip) throw new Error(`no clip "${key}"; known: ${[...assets.clips.keys()].join(", ")}`);
      a = mixer.clipAction(clip);
      actions.set(key, a);
    }
    return a;
  }

  /**
   * Play a clip by key (idle, walk, sit_down, sit_idle, talk, sit_talk, stand_up), cross-fading
   * from the current one over `fade` seconds (default 0.3). `loop`: "repeat" | "once" | "pingpong";
   * a once-clip clamps on its last frame. Playing the clip that is already current only updates
   * its time scale.
   */
  function play(key, opts = {}) {
    const fade = opts.fade ?? CROSSFADE;
    const next = actionFor(key);
    const loopOpt = opts.loop ?? (/^(sit_down|stand_up)$/.test(key) ? "once" : "repeat");
    const ts = opts.timeScale ?? (key === "walk" ? state.walkSpeed / WALK_NATURAL_SPEED : key === "idle" || key === "sit_idle" ? speedJitter : 1);
    if (current && current.key === key && !opts.restart && current.action.enabled && current.action.isRunning()) {
      next.setEffectiveTimeScale(ts);
      return next;
    }
    next.reset();
    next.enabled = true;
    next.setEffectiveTimeScale(ts);
    next.setEffectiveWeight(1);
    if (loopOpt === "once") {
      next.setLoop(THREE.LoopOnce, 1);
      next.clampWhenFinished = true;
    } else {
      next.setLoop(loopOpt === "pingpong" ? THREE.LoopPingPong : THREE.LoopRepeat, Infinity);
      next.clampWhenFinished = false;
    }
    const dur = next.getClip().duration;
    if (opts.time !== undefined) next.time = opts.time;
    else if (loopOpt !== "once" && key !== "walk") next.time = phase * dur;
    next.play();
    if (current && current.action !== next && fade > 0) current.action.crossFadeTo(next, fade, false);
    else if (current && current.action !== next) current.action.stop();
    current = { key, action: next };
    return next;
  }

  // ---- procedural layers ----
  const sideOf = {}; // +1 for the left side (x > 0), -1 for the right
  {
    const l = _v1.setFromMatrixPosition(bones.hand_l.matrixWorld);
    const r = _v2.setFromMatrixPosition(bones.hand_r.matrixWorld);
    sideOf.l = l.x >= r.x ? 1 : -1;
    sideOf.r = -sideOf.l;
  }
  const layers = {
    type: { w: 0, goal: 0, t: 0 },
    wave: { w: 0, goal: 0, t: 0 },
    stretch: { w: 0, goal: 0, t: 0 },
    drink: { w: 0, goal: 0, t: 0 },
  };
  const savedQ = PROC_BONES.map((n) => ({ bone: bones[n], q: bones[n].quaternion.clone() }));
  const look_ = { target: null, yaw: 0, pitch: 0 };
  const frameInv = new THREE.Matrix4();
  const frameQInv = new THREE.Quaternion();
  const lookTarget = new THREE.Vector3();
  const mouthLocal = src.mouthLocal;
  const desired = { yaw: 0, pitch: 0 };

  // Cup prop (only visible during the drink layer).
  _cupGeo ??= new THREE.CylinderGeometry(0.036, 0.03, 0.085, 14);
  _cupMat ??= new THREE.MeshStandardMaterial({ color: 0xf2efe8, roughness: 0.6 });
  const cup = new THREE.Mesh(_cupGeo, _cupMat);
  cup.name = "cup";
  cup.visible = false;
  cup.castShadow = true;
  object3D.add(cup);

  const posIn = (bone, out) => out.setFromMatrixPosition(bone.matrixWorld).applyMatrix4(frameInv);

  /** Rotate `bone` by quaternion `R` given in the character frame, scaled by weight `w`. */
  function rotateBone(bone, R, w) {
    if (w <= 1e-4) return;
    _q1.slerpQuaternions(_qId, R, w); // weighted rotation in the character frame
    bone.parent.matrixWorld.decompose(_v4, _q2, _s);
    _q2.premultiply(frameQInv); // parent orientation in the character frame
    _q3.copy(_q2).invert().multiply(_q1).multiply(_q2); // the same rotation in the parent's axes
    bone.quaternion.premultiply(_q3).normalize();
    bone.updateWorldMatrix(false, true);
  }
  const _R = new THREE.Quaternion();
  /** Turn `bone` so that the direction to `child` becomes `dir` (character frame). */
  function aim(bone, child, dir, w) {
    posIn(bone, _v1);
    posIn(child, _v2);
    _v2.sub(_v1);
    if (_v2.lengthSq() < 1e-8) return;
    _R.setFromUnitVectors(_v2.normalize(), _v3.copy(dir).normalize());
    rotateBone(bone, _R, w);
  }
  const _S = new THREE.Vector3();
  const _E = new THREE.Vector3();
  const _H = new THREE.Vector3();
  const _T = new THREE.Vector3();
  const _U = new THREE.Vector3();
  const _P = new THREE.Vector3();
  /** Two-bone arm solver: hand of `side` ("l"|"r") to `target`, elbow toward `pole` (character frame). */
  function arm(side, target, pole, w) {
    if (w <= 1e-4) return;
    const up = bones[`upperarm_${side}`];
    const lo = bones[`lowerarm_${side}`];
    const ha = bones[`hand_${side}`];
    posIn(up, _S); posIn(lo, _E); posIn(ha, _H);
    const l1 = _E.distanceTo(_S);
    const l2 = _H.distanceTo(_E);
    _U.subVectors(target, _S);
    const d = clamp(_U.length(), Math.abs(l1 - l2) + 0.02, (l1 + l2) * 0.998);
    _U.normalize();
    const a = (l1 * l1 - l2 * l2 + d * d) / (2 * d);
    const h = Math.sqrt(Math.max(l1 * l1 - a * a, 0));
    _P.copy(pole).addScaledVector(_U, -pole.dot(_U));
    if (_P.lengthSq() < 1e-8) _P.set(0, -1, 0).addScaledVector(_U, _U.y);
    _P.normalize();
    _E.copy(_S).addScaledVector(_U, a).addScaledVector(_P, h); // elbow
    _T.copy(_S).addScaledVector(_U, d);                         // reachable hand position
    const elbowDir = _v4.subVectors(_E, _S);
    aim(up, lo, elbowDir, w);
    posIn(lo, _E);
    const wristDir = _v4.subVectors(_T, _E);
    aim(lo, ha, wristDir, w);
  }

  const tmpTarget = new THREE.Vector3();
  const tmpPole = new THREE.Vector3();
  const shoulder = new THREE.Vector3();
  const mouth = new THREE.Vector3();

  function applyTyping(w, t) {
    for (const side of ["l", "r"]) {
      const sx = sideOf[side];
      const flip = side === "l" ? 0 : Math.PI;
      // two slightly different rates, so the pattern is not mechanical (about 6 Hz)
      const k1 = Math.sin(t * 2 * Math.PI * 6.0 + flip);
      const k2 = Math.sin(t * 2 * Math.PI * 4.3 + flip * 0.7 + 1.1);
      const busy = 0.65 + 0.35 * Math.sin(t * 2 * Math.PI * 0.7 + (side === "l" ? 0 : 2.1));
      tmpTarget.set(
        sx * (TYPING.wrist.x + 0.012 * k2 * busy),
        TYPING.wrist.y + 0.007 * k1 * busy,
        TYPING.wrist.z + 0.014 * k2 * busy,
      );
      tmpPole.set(sx * 0.55, -1, -0.45);
      arm(side, tmpTarget, tmpPole, w);
      // fingertips tap: small wrist flex about the character's x axis
      _R.setFromAxisAngle(AXIS_X, 0.12 * k1 * busy);
      rotateBone(bones[`hand_${side}`], _R, w);
    }
  }

  function applyWave(w, t) {
    const side = "r";
    const sx = sideOf[side];
    posIn(bones[`upperarm_${side}`], shoulder);
    const raise = smoother(t / 0.55);
    const sway = Math.sin(t * 2 * Math.PI * 1.7) * 0.075 * raise;
    // hand above the shoulder, forearm roughly upright, elbow out and down
    tmpTarget.set(shoulder.x + sx * (0.27 + sway), shoulder.y + 0.27 * raise + 0.01, shoulder.z + 0.10);
    tmpPole.set(sx * 0.6, -1, -0.15);
    arm(side, tmpTarget, tmpPole, w);
    _R.setFromAxisAngle(new THREE.Vector3(0, 0, 1), -sx * Math.sin(t * 2 * Math.PI * 1.7 + 0.6) * 0.3 * raise);
    rotateBone(bones[`hand_${side}`], _R, w);
  }

  function envelope(t, dur) {
    const edge = Math.min(1.0, dur / 3);
    return smoother(t / edge) * (1 - smoother((t - (dur - edge)) / edge));
  }

  function applyStretch(w, t, dur) {
    const p = envelope(t, dur);
    const e = w * p;
    // lean back through the spine and tip the head back a little
    _R.setFromAxisAngle(AXIS_X, -0.075);
    rotateBone(bones.spine_01, _R, e);
    rotateBone(bones.spine_02, _R, e);
    rotateBone(bones.spine_03, _R, e);
    _R.setFromAxisAngle(AXIS_X, -0.14);
    rotateBone(bones.Head, _R, e);
    for (const side of ["l", "r"]) {
      const sx = sideOf[side];
      posIn(bones[`upperarm_${side}`], shoulder);
      // hands come together above the head, with a small sway
      tmpTarget.set(shoulder.x - sx * 0.12, shoulder.y + 0.6, shoulder.z - 0.04 + Math.sin(t * 2.2) * 0.01 * p);
      tmpPole.set(sx * 1, 0.15, -0.5);
      arm(side, tmpTarget, tmpPole, e);
    }
  }

  const cupOffset = new THREE.Vector3(0, 0.07, -0.045);
  function applyDrink(w, t, dur) {
    const p = envelope(t, dur);
    const e = w * p;
    const sip = smoother((t - dur * 0.3) / 0.3) * (1 - smoother((t - dur * 0.62) / 0.3));
    // head tips back while sipping
    _R.setFromAxisAngle(AXIS_X, -0.2 * sip);
    rotateBone(bones.Head, _R, w);
    const side = "r";
    const sx = sideOf[side];
    mouth.copy(mouthLocal).applyMatrix4(bones.Head.matrixWorld).applyMatrix4(frameInv);
    tmpTarget.copy(mouth).add(_v1.set(sx * 0.015, -0.085, 0.115));
    tmpPole.set(sx * 0.5, -1, -0.4);
    arm(side, tmpTarget, tmpPole, e);
    // cup follows the hand
    posIn(bones[`hand_${side}`], _v2);
    cup.position.copy(_v2).add(cupOffset);
    cup.rotation.set(-0.5 * sip, 0, 0);
    cup.visible = w > 0.02;
  }

  function applyLook(dt, gazeInFrame) {
    const target = look_.target ?? gazeInFrame;
    if (target) {
      posIn(bones.Head, _v1);
      lookTarget.copy(target);
      if (target === look_.target) lookTarget.applyMatrix4(frameInv); // world point -> character frame
      _v2.subVectors(lookTarget, _v1);
      desired.yaw = clamp(Math.atan2(_v2.x, _v2.z), -1.0, 1.0);
      desired.pitch = clamp(Math.atan2(-_v2.y, Math.hypot(_v2.x, _v2.z)), -0.35, 0.45);
    } else {
      desired.yaw = 0;
      desired.pitch = 0;
    }
    const k = 1 - Math.exp(-dt * 5);
    look_.yaw += (desired.yaw - look_.yaw) * k;
    look_.pitch += (desired.pitch - look_.pitch) * k;
    if (Math.abs(look_.yaw) < 1e-3 && Math.abs(look_.pitch) < 1e-3 && !target) return;
    // yaw about the vertical axis, pitch about the character's x axis (positive looks down)
    _R.setFromAxisAngle(AXIS_Y, look_.yaw * 0.4);
    rotateBone(bones.neck_01, _R, 1);
    _R.setFromAxisAngle(AXIS_Y, look_.yaw * 0.6);
    rotateBone(bones.Head, _R, 1);
    _R.setFromAxisAngle(AXIS_X, look_.pitch * 0.3);
    rotateBone(bones.neck_01, _R, 1);
    _R.setFromAxisAngle(AXIS_X, look_.pitch * 0.7);
    rotateBone(bones.Head, _R, 1);
  }

  const monitorFrame = new THREE.Vector3(TYPING.monitor.x, TYPING.monitor.y, TYPING.monitor.z);

  /**
   * Make the head follow a world-space point (THREE.Vector3). Rotation is clamped (about 57 degrees
   * of yaw, a little pitch) and smoothed. `lookAt(null)` returns the head to the animation.
   */
  function lookAt(point) {
    look_.target = point ? point.clone() : null;
  }

  // ---- behaviors ----
  function ensureSeated() {
    if (!state.seated) {
      play("sit_down", { loop: "once" });
      state.seated = true;
      state.sitReady = false;
      state.pendingSit = true;
    } else if (state.sitReady && current?.key !== "sit_idle") {
      play("sit_idle");
    }
  }
  function ensureStanding(clip = "idle") {
    state.seated = false;
    state.sitReady = false;
    state.pendingSit = false;
    play(clip);
  }
  const ALIASES = { raise_hand: "wave" };
  const TIMED = { wave: 3, stretch: 3, drink: 3 };

  /**
   * Switch behavior. Names: idle, walk, sit (sit_down once, then sit_idle), sit_idle, stand (stand_up
   * once, then idle), type, talk, wave (raise_hand), stretch, drink.
   * The procedural behaviors fade in and out over 0.3 s on top of idle (standing) or sit_idle (seated).
   * wave, stretch and drink run for `duration` seconds (default 3), then fall back to idle/sit_idle and call
   * `opts.onDone(name)` and `character.onBehaviorEnd(name)`. Other options: walk `speed` (m/s).
   */
  function setBehavior(name, opts = {}) {
    name = ALIASES[name] ?? name;
    if (!BEHAVIORS.includes(name) && !Object.values(ALIASES).includes(name)) throw new RangeError(`unknown behavior "${name}"`);
    state.behavior = name;
    state.t = 0;
    state.onDone = opts.onDone ?? null;
    state.afterOnce = null;
    state.duration = opts.duration ?? TIMED[name] ?? Infinity;
    for (const l of Object.values(layers)) l.goal = 0;
    switch (name) {
      case "idle":
        ensureStanding("idle");
        break;
      case "walk":
        if (opts.speed) state.walkSpeed = opts.speed;
        ensureStanding("walk");
        break;
      case "sit":
      case "sit_idle":
      case "type":
        ensureSeated();
        break;
      case "stand":
        if (state.seated) {
          play("stand_up", { loop: "once" });
          state.seated = false;
          state.sitReady = false;
          state.pendingSit = false;
          state.afterOnce = "idle";
        } else play("idle");
        break;
      case "talk":
        play(state.seated ? "sit_talk" : "talk");
        break;
      case "wave":
      case "stretch":
      case "drink":
        play(state.seated ? "sit_idle" : "idle");
        layers[name].t = 0;
        layers[name].goal = 1;
        break;
      default:
        break;
    }
    if (current && name === "walk") current.action.setEffectiveTimeScale(state.walkSpeed / WALK_NATURAL_SPEED);
    return api;
  }

  function finishBehavior() {
    const name = state.behavior;
    const cb = state.onDone;
    setBehavior(state.seated ? "sit_idle" : "idle");
    cb?.(name);
    api.onBehaviorEnd?.(name);
  }

  function setWalkSpeed(mps) {
    state.walkSpeed = mps;
    if (current?.key === "walk") current.action.setEffectiveTimeScale(mps / WALK_NATURAL_SPEED);
  }

  // ---- per frame ----
  function update(dt) {
    dt = Math.min(Math.max(dt, 0), 0.1);
    // 1. undo last frame's procedural pose on bones the clips may not drive
    for (const s of savedQ) s.bone.quaternion.copy(s.q);
    bones.pelvis.position.copy(pelvisRaw);
    // 2. clip pose
    mixer.update(dt);
    pelvisRaw.copy(bones.pelvis.position);
    bones.pelvis.position.multiplyScalar(pelvisK);
    for (const s of savedQ) s.q.copy(s.bone.quaternion); // pure clip pose, restored next frame
    // keep the soles on the floor (and the seat contact right) for the current pose
    const liftGoal = groundFor(current?.key);
    lift += (liftGoal - lift) * (1 - Math.exp(-dt * 12));
    model.position.y = lift;
    // hand-overs between clips
    if (current) {
      const a = current.action;
      const clipDur = a.getClip().duration;
      if (state.pendingSit && current.key === "sit_down" && a.time >= clipDur - CROSSFADE - 1e-3) {
        state.pendingSit = false;
        state.sitReady = true;
        play("sit_idle");
      } else if (state.afterOnce && current.key === "stand_up" && a.time >= clipDur - CROSSFADE - 1e-3) {
        const next = state.afterOnce;
        state.afterOnce = null;
        state.behavior = next; // "stand" has finished: now plainly idle
        play(next);
      }
    }
    // 3. timed behaviors
    state.t += dt;
    if (Number.isFinite(state.duration)) {
      const l = layers[state.behavior];
      if (l && state.t >= state.duration - LAYER_RAMP) l.goal = 0;
      if (state.t >= state.duration) finishBehavior();
    }
    // The typing layer belongs to "type" only: idle behaviors can never show it.
    layers.type.goal = state.behavior === "type" && state.sitReady ? 1 : 0;
    for (const l of Object.values(layers)) {
      l.w = approach(l.w, l.goal, dt / LAYER_RAMP);
      if (l.goal > 0 || l.w > 0) l.t += dt;
      if (l.goal === 0 && l.w === 0) l.t = 0;
    }
    // 4. procedural layers on top of the clip pose
    object3D.updateMatrixWorld(true);
    frameInv.copy(object3D.matrixWorld).invert();
    object3D.getWorldQuaternion(frameQInv).invert();
    const wType = smooth(layers.type.w);
    const wWave = smooth(layers.wave.w);
    const wStretch = smooth(layers.stretch.w);
    const wDrink = smooth(layers.drink.w);
    if (wStretch > 1e-3) applyStretch(wStretch, layers.stretch.t, Number.isFinite(state.duration) && state.behavior === "stretch" ? state.duration : 3);
    if (wType > 1e-3) applyTyping(wType, layers.type.t);
    if (wWave > 1e-3) applyWave(wWave, layers.wave.t);
    if (wDrink > 1e-3) applyDrink(wDrink, layers.drink.t, Number.isFinite(state.duration) && state.behavior === "drink" ? state.duration : 3);
    else cup.visible = false;
    // 5. gaze: look at the monitor while typing unless the caller set a target
    let gaze = null;
    if (!look_.target && wType > 0.5) gaze = monitorFrame;
    applyLook(dt, gaze);
  }

  // Low quality also drops the normal and roughness maps of this character's own materials. The
  // textures stay on the shared assets, so switching back to normal restores them. On the first
  // frame in low mode they are never uploaded to the GPU at all (about 16 MB per body, 5 MB per hair).
  const detailMaps = ownMaterials.map((m) => ({ m, normalMap: m.normalMap ?? null, roughnessMap: m.roughnessMap ?? null }));
  function setQuality(q) {
    const low = q === "low";
    if (browMesh) browMesh.visible = !low;
    if (beardMesh) beardMesh.visible = !low;
    for (const d of detailMaps) {
      const normalMap = low ? null : d.normalMap;
      const roughnessMap = low ? null : d.roughnessMap;
      if (d.m.normalMap === normalMap && d.m.roughnessMap === roughnessMap) continue;
      d.m.normalMap = normalMap;
      d.m.roughnessMap = roughnessMap;
      d.m.needsUpdate = true;
    }
  }
  setQuality(options.quality ?? "normal");

  function dispose() {
    mixer.stopAllAction();
    mixer.uncacheRoot(model);
    for (const m of ownMaterials) m.dispose(); // textures and geometry stay with the shared assets
    for (const s of ownSkeletons) s.dispose();
    if (bodySkeleton.dispose) bodySkeleton.dispose();
    object3D.removeFromParent();
  }

  const api = {
    id: look.agentId,
    look,
    object3D,
    model,
    mixer,
    bones,
    play,
    setBehavior,
    setWalkSpeed,
    lookAt,
    setQuality,
    update,
    dispose,
    onBehaviorEnd: null,
    /** Current behavior name, seated flag and clip key. */
    get behavior() { return state.behavior; },
    get seated() { return state.seated; },
    get clip() { return current?.key ?? null; },
    get layerWeights() { return Object.fromEntries(Object.entries(layers).map(([k, l]) => [k, l.w])); },
    /** Place the character so its pelvis sits over `seat` when seated. */
    placeAtSeat(seat, rotation) {
      const p = seatedRoot(seat, rotation);
      object3D.position.set(p.x, p.y, p.z);
      object3D.rotation.y = rotation;
    },
  };
  // start in idle, with the first pose already applied
  setBehavior("idle");
  update(0);
  return api;
}
