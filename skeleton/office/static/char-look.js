// Look of a character: pure data, no Three.js (so Node can test it).
//
//   const look = traitsForAgent("7", { name: "Sari" });
//
// Everything is chosen from seeded streams of the agent id (Req 2.2): the same agent has the
// same look on every reload, and adding a trait never changes the others. Nothing here is unseeded.
//
// The free asset pack only has the "Superhero" body (broad, muscular) in a male and a female
// variant. To read as ordinary office staff, a look also carries body proportions (height,
// shoulder width, arm thickness) that characters.js applies as bone scales, plus clothes
// (blouse, shirt, batik, stripes), hair and an optional headscarf.

// ---- seeded random streams ---------------------------------------------------------------

/** 32-bit string hash (FNV-1a). */
export function hash32(str) {
  let h = 0x811c9dc5;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}
/** mulberry32 PRNG: a function returning floats in [0, 1). */
export function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
/** One independent random stream per (agent, trait). */
export function stream(agentId, salt) {
  return mulberry32(hash32(`${agentId}\u0000${salt}`));
}
const pick = (rng, list) => list[Math.floor(rng() * list.length) % list.length];
const between = (rng, lo, hi) => lo + rng() * (hi - lo);
/** Pick from [[item, weight], ...]. */
function pickWeighted(rng, list) {
  const total = list.reduce((s, [, w]) => s + w, 0);
  let r = rng() * total;
  for (const [item, w] of list) {
    r -= w;
    if (r < 0) return item;
  }
  return list[list.length - 1][0];
}

// ---- palettes ----------------------------------------------------------------------------

// Women's tops (blouses and fitted tops) and men's shirts, in office colours.
export const BLOUSE_COLORS = Object.freeze([
  ["white", 0xf0ede6], ["cream", 0xefe3cf], ["rose", 0xd98ba3], ["dusty blue", 0x8fb3d9],
  ["lilac", 0xb39ddb], ["teal", 0x2a9d8f], ["sage", 0x8fb08a], ["coral", 0xe27a63],
  ["navy", 0x2b4a6f], ["mustard", 0xd9a93c], ["plum", 0x7b3f78], ["maroon", 0x8a2f3f],
]);
export const MEN_SHIRT_COLORS = Object.freeze([
  ["white", 0xf0ede6], ["light blue", 0xa9c8e8], ["sky", 0x5aa9e6], ["navy", 0x2b4a6f],
  ["grey", 0x8a8f99], ["cream", 0xefe9dc], ["charcoal", 0x4a4e57], ["sage", 0x7da36a],
  ["olive", 0x8a8f3d], ["teal", 0x2a9d8f], ["brick", 0xb5483a], ["lavender", 0xaaa4d4],
]);
/** Union of both lists (kept as one export for callers that want "all shirt colours"). */
export const SHIRT_COLORS = Object.freeze([...MEN_SHIRT_COLORS, ...BLOUSE_COLORS.filter(([n]) => !MEN_SHIRT_COLORS.some(([m]) => m === n))]);
// Batik-like base colours (the motif is drawn in a gold-cream colour on top).
export const BATIK_COLORS = Object.freeze([
  ["batik brown", 0x6b4423], ["batik indigo", 0x2e3a6e], ["batik maroon", 0x7a2e2e], ["batik green", 0x1f5a4a],
]);
export const BATIK_MOTIF = 0xe3c78a;
export const MEN_PANTS_COLORS = Object.freeze([
  ["black", 0x1f2125], ["charcoal", 0x3a3d44], ["navy", 0x25324a], ["khaki", 0xb3a07a], ["bark", 0x5b4636],
]);
export const WOMEN_PANTS_COLORS = Object.freeze([
  ["black", 0x1f2125], ["charcoal", 0x3a3d44], ["navy", 0x25324a], ["camel", 0xb89b72], ["grey", 0x6b6e75], ["plum", 0x4b2f4a],
]);
export const PANTS_COLORS = Object.freeze([
  ["ink", 0x25324a], ["graphite", 0x3a3d44], ["khaki", 0xb3a07a], ["bark", 0x5b4636],
  ["black", 0x1f2125], ["denim", 0x3f5f8a],
]);
export const SHOE_COLORS = Object.freeze([
  ["black", 0x1c1c1f], ["brown", 0x4a3426], ["white", 0xe8e6e1], ["grey", 0x6b6e75],
]);
// Mostly black and dark brown. Grey and silver stand for older staff (age shows in hair only).
export const HAIR_COLORS = Object.freeze([
  ["black", 0x1d1b1b], ["dark brown", 0x3a281d], ["brown", 0x5b3d28], ["chestnut", 0x7a4a2a],
  ["ash", 0x9a958f], ["silver", 0xcfcfcf],
]);
const HAIR_COLOR_WEIGHTS = Object.freeze({ black: 56, "dark brown": 28, brown: 6, chestnut: 2, ash: 5, silver: 3 });
// Skin tone = multiplier on the skin texture (the Dark and Light textures are almost the same).
// `variant` is the texture used with it.
export const SKIN_TONES = Object.freeze([
  { name: "fair", variant: "light", rgb: [1.16, 1.07, 1.02] },
  { name: "light", variant: "light", rgb: [1.06, 0.98, 0.93] },
  { name: "medium", variant: "light", rgb: [0.92, 0.82, 0.74] },
  { name: "tan", variant: "dark", rgb: [0.86, 0.72, 0.62] },
  { name: "brown", variant: "dark", rgb: [0.66, 0.51, 0.42] },
  { name: "deep", variant: "dark", rgb: [0.46, 0.34, 0.29] },
]);
const SKIN_WEIGHTS = Object.freeze({ light: 22, medium: 36, tan: 28, brown: 14 });

/** Hair styles per gender: women longer styles, men short styles. */
export const HAIR_BY_GENDER = Object.freeze({
  male: [["hair_buzzed", 5], ["hair_simpleparted", 6]],
  female: [["hair_long", 6], ["hair_buns", 4], ["hair_buzzedfemale", 1]],
});
/** Headscarf colours: quiet office colours. */
export const HIJAB_COLORS = Object.freeze([
  ["ivory", 0xe9e2d0], ["dusty rose", 0xc98b98], ["navy", 0x2c3e66], ["mocha", 0x7a5a48],
  ["sage", 0x8da58a], ["lilac", 0xa593c4], ["black", 0x24242a], ["terracotta", 0xb86a4e],
]);
export const HIJAB_CHANCE = 0.35;
export const BEARD_ID = "hair_beard";
export const BEARD_CHANCE = 0.18;

/** Body proportions (multipliers; 1 is the pack's Superhero body). */
export const BUILD_RANGES = Object.freeze({
  male: { height: [0.97, 1.04], width: [0.88, 0.95], depth: [0.96, 1.0], arm: [0.85, 0.93] },
  female: { height: [0.92, 0.99], width: [0.84, 0.93], depth: [0.94, 1.0], arm: [0.85, 0.92] },
});
/** Pattern kinds of the shirt shader. */
export const PATTERNS = Object.freeze({ plain: 0, stripe: 1, check: 2, batik: 3 });

// ---- gender ------------------------------------------------------------------------------

export const FEMALE_NAMES = Object.freeze([
  "sari", "wati", "dewi", "rina", "ani", "siti", "putri", "ayu", "lestari", "maya", "nina", "tika", "yuni",
  "fitri", "indah", "rani", "lina", "sri", "endang", "intan", "mega", "citra", "anita", "nia", "ika", "rini",
  "novi", "lia", "mira", "ningsih", "yanti", "susi", "tini", "fatimah", "aisyah", "nurul", "diah", "retno",
  "bunga", "melati", "ratih", "sinta", "tuti", "hesti", "widya", "ratna", "wulan", "sulastri", "mawar",
]);
export const MALE_NAMES = Object.freeze([
  "budi", "joko", "andi", "agus", "dedi", "eko", "rudi", "hadi", "bayu", "fajar", "wawan", "dimas", "reza",
  "ahmad", "muhammad", "slamet", "bambang", "heru", "hendra", "irfan", "rizki", "doni", "yoga", "adi", "anton",
  "arif", "bagus", "dani", "gilang", "iwan", "joni", "teguh", "tono", "yusuf", "putra", "rahmat", "fauzi",
  "hendri", "santoso", "wibowo", "supriyadi", "darmawan",
]);
const FEMALE_SET = new Set(FEMALE_NAMES);
const MALE_SET = new Set(MALE_NAMES);
const FEMALE_TITLES = new Set(["bu", "ibu", "mbak", "mba", "teh", "ms", "mrs", "miss"]);
const MALE_TITLES = new Set(["pak", "bapak", "mas", "bang", "abang", "om", "mr"]);

/** "female", "male" or null from a display name (first names and titles, any case). */
export function genderFromName(name) {
  if (typeof name !== "string") return null;
  const tokens = name.toLowerCase().split(/[^a-z]+/).filter(Boolean);
  for (const t of tokens) {
    if (FEMALE_TITLES.has(t)) return "female";
    if (MALE_TITLES.has(t)) return "male";
  }
  for (const t of tokens) {
    if (FEMALE_SET.has(t)) return "female";
    if (MALE_SET.has(t)) return "male";
  }
  return null;
}

/**
 * Alternation from the agent id. The number at the end of the id decides (odd = woman, even =
 * man), so any 4 consecutive ids hold exactly 2 women and 8 hold 4. Ids without a number fall
 * back to a hash of the id.
 */
export function genderFromId(agentId) {
  const id = String(agentId);
  const m = /(\d+)\D*$/.exec(id);
  const odd = m ? Number(m[1].slice(-9)) % 2 === 1 : (hash32(id) & 1) === 1;
  return odd ? "female" : "male";
}

/** Order: explicit gender ("male"/"female"), then the name, then the id. */
export function resolveGender(agentId, { gender, name } = {}) {
  if (gender === "male" || gender === "female") return gender;
  return genderFromName(name) ?? genderFromId(agentId);
}

// ---- colour helpers ----------------------------------------------------------------------

/** Mix two 0xRRGGBB colours (t = 0 is a, 1 is b). */
export function mixHex(a, b, t) {
  const ch = (shift) => Math.round(((a >> shift) & 255) * (1 - t) + ((b >> shift) & 255) * t);
  return (ch(16) << 16) | (ch(8) << 8) | ch(0);
}
const luma = (hex) => (0.2126 * ((hex >> 16) & 255) + 0.7152 * ((hex >> 8) & 255) + 0.0722 * (hex & 255)) / 255;

// ---- the look ----------------------------------------------------------------------------

/**
 * Pick the look of an agent. Pure function of the id and options, so the same agent looks the same
 * on every reload. Returns plain data.
 *
 * options:
 *   gender       "male" | "female" (wins over everything)
 *   name         display name; common Indonesian first names decide the gender
 *   body         alias of `gender`
 *   hair         force a hair style id
 *   skinTone     force a tone name from SKIN_TONES
 *   availableHair  ids of the hair files that are loaded (default: all styles)
 *   look         partial look; its fields replace the seeded ones (`build`, `skin`, ... merge by key)
 */
export function traitsForAgent(agentId, options = {}) {
  const id = String(agentId);
  const over = options.look ?? {};
  const gender = resolveGender(id, { gender: over.gender ?? options.gender ?? options.body, name: options.name });
  const female = gender === "female";

  // skin
  const tone = options.skinTone
    ? SKIN_TONES.find((t) => t.name === options.skinTone) ?? SKIN_TONES[1]
    : SKIN_TONES.find((t) => t.name === pickWeighted(stream(id, "skin"), Object.entries(SKIN_WEIGHTS))) ?? SKIN_TONES[1];

  // headscarf: some women (never forced on anyone: a look can say hijab: null)
  const hijabOn = female && stream(id, "hijab")() < HIJAB_CHANCE;
  const hijabColor = pick(stream(id, "hijabcolor"), HIJAB_COLORS);

  // hair
  const allowed = options.availableHair ? (list) => list.filter(([h]) => options.availableHair.includes(h)) : (list) => list;
  const styles = allowed(HAIR_BY_GENDER[gender]);
  const hairId = options.hair ?? (styles.length ? pickWeighted(stream(id, "hair"), styles) : HAIR_BY_GENDER[gender][0][0]);
  const hairName = pickWeighted(stream(id, "haircolor"), HAIR_COLORS.map(([n]) => [n, HAIR_COLOR_WEIGHTS[n]]));
  const hairHex = HAIR_COLORS.find(([n]) => n === hairName)[1];
  const beard = !female && (!options.availableHair || options.availableHair.includes(BEARD_ID)) && stream(id, "beard")() < BEARD_CHANCE;

  // clothes
  const styleRng = stream(id, "outfit");
  const patternKind = pickWeighted(styleRng, female
    ? [["plain", 56], ["stripe", 10], ["check", 6], ["batik", 28]]
    : [["plain", 55], ["stripe", 12], ["check", 11], ["batik", 22]]);
  let shirt;
  let pattern2 = null;
  if (patternKind === "batik") {
    const [name, hex] = pick(stream(id, "shirt"), BATIK_COLORS);
    shirt = { name, hex };
    pattern2 = BATIK_MOTIF;
  } else {
    const [name, hex] = pick(stream(id, "shirt"), female ? BLOUSE_COLORS : MEN_SHIRT_COLORS);
    shirt = { name, hex };
    if (patternKind !== "plain") pattern2 = luma(hex) > 0.55 ? mixHex(hex, 0x20283a, 0.42) : mixHex(hex, 0xffffff, 0.55);
  }
  const [pantsName, pantsHex] = pick(stream(id, "pants"), female ? WOMEN_PANTS_COLORS : MEN_PANTS_COLORS);
  const [shoeName, shoeHex] = pick(stream(id, "shoes"), SHOE_COLORS);
  const longSleeves = stream(id, "sleeves")() < (patternKind === "batik" ? 0.5 : 0.4);

  // proportions
  const r = BUILD_RANGES[gender];
  const bRng = stream(id, "build");
  const build = {
    height: between(bRng, ...r.height),
    width: between(bRng, ...r.width),
    depth: between(bRng, ...r.depth),
    arm: between(bRng, ...r.arm),
  };

  const look = {
    agentId: id,
    gender,
    body: gender,
    bodyId: `superhero_${gender}_${tone.variant}`,
    skin: { name: tone.name, variant: tone.variant, rgb: tone.rgb },
    hair: { id: hijabOn ? null : hairId, style: hairId, color: { name: hairName, hex: hairHex } },
    beard,
    shirt,
    pattern: { kind: patternKind, id: PATTERNS[patternKind], hex2: pattern2 },
    outfit: patternKind === "batik" ? "batik" : female ? "blouse" : "shirt",
    pants: { name: pantsName, hex: pantsHex },
    shoes: { name: shoeName, hex: shoeHex },
    longSleeves,
    hijab: hijabOn ? { name: hijabColor[0], hex: hijabColor[1] } : null,
    build,
  };
  // Overrides from `options.look`: top-level fields replace, objects merge by key.
  for (const [k, v] of Object.entries(over)) {
    if (k === "gender" || v === undefined) continue;
    look[k] = v && typeof v === "object" && !Array.isArray(v) && look[k] && typeof look[k] === "object" ? { ...look[k], ...v } : v;
  }
  if (over.skin?.name) {
    const t = SKIN_TONES.find((s) => s.name === over.skin.name);
    if (t) look.skin = { name: t.name, variant: t.variant, rgb: t.rgb, ...over.skin };
    look.bodyId = `superhero_${gender}_${look.skin.variant}`;
  }
  return look;
}
