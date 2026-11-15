// Run with: node --test tests/js/
// Covers skeleton/office/static/char-look.js (Requirement 2.2, task 8.9): gender resolution, the
// alternation guarantee, determinism of the seeded look, and the ranges of the body proportions.
import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import path from "node:path";

const STATIC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../skeleton/office/static");
const L = await import(path.join(STATIC, "char-look.js"));

const women = (ids, opts = {}) => ids.filter((id) => L.resolveGender(id, opts) === "female").length;

test("gender order: explicit option, then name, then id", () => {
  // explicit wins over a name and over the id
  assert.equal(L.resolveGender("2", { gender: "female", name: "Budi" }), "female");
  assert.equal(L.resolveGender("1", { gender: "male", name: "Sari" }), "male");
  // the name wins over the id (id 1 would be a woman, id 2 a man)
  assert.equal(L.resolveGender("1", { name: "Budi" }), "male");
  assert.equal(L.resolveGender("2", { name: "Sari" }), "female");
  // unknown name or no name: the id decides
  assert.equal(L.resolveGender("1", { name: "Zzyzx" }), L.genderFromId("1"));
  assert.equal(L.resolveGender("2", {}), L.genderFromId("2"));
  assert.equal(L.resolveGender("2"), L.genderFromId("2"));
  // a bad explicit value is ignored
  assert.equal(L.resolveGender("1", { gender: "x" }), L.genderFromId("1"));
});

test("mock agents: Sari and Wati are women, Budi and Joko are men", () => {
  const mock = { "mock-1": "Sari", "mock-2": "Budi", "mock-3": "Wati", "mock-4": "Joko" };
  for (const [id, name] of Object.entries(mock)) {
    const want = ["Sari", "Wati"].includes(name) ? "female" : "male";
    assert.equal(L.resolveGender(id, { name }), want, name);
    assert.equal(L.traitsForAgent(id, { name }).gender, want, name);
  }
});

test("the name lookup ignores case and handles titles and surnames", () => {
  for (const n of ["sari", "SARI", "SaRi", "  Sari  ", "Sari Baru", "Dewi Lestari"]) assert.equal(L.genderFromName(n), "female", n);
  for (const n of ["budi", "BUDI", "Pak Joko", "Budi Santoso"]) assert.equal(L.genderFromName(n), "male", n);
  assert.equal(L.genderFromName("Bu Agus"), "female"); // the title wins
  assert.equal(L.genderFromName("Xyzzy"), null);
  assert.equal(L.genderFromName(""), null);
  assert.equal(L.genderFromName(undefined), null);
  assert.equal(L.genderFromName(42), null);
  // every listed name resolves to its own list, in any case
  for (const n of L.FEMALE_NAMES) assert.equal(L.genderFromName(n.toUpperCase()), "female", n);
  for (const n of L.MALE_NAMES) assert.equal(L.genderFromName(n.toUpperCase()), "male", n);
  // no name is in both lists
  assert.deepEqual(L.FEMALE_NAMES.filter((n) => L.MALE_NAMES.includes(n)), []);
  // the examples from the task
  for (const n of ["Sari", "Wati", "Dewi", "Rina", "Ani", "Siti", "Putri", "Ayu", "Lestari", "Maya", "Nina", "Tika", "Yuni"]) assert.equal(L.genderFromName(n), "female", n);
  for (const n of ["Budi", "Joko", "Andi", "Agus", "Dedi", "Eko", "Rudi", "Hadi", "Bayu", "Fajar", "Wawan", "Dimas", "Reza"]) assert.equal(L.genderFromName(n), "male", n);
});

test("alternation by id: 2 women in any 4 consecutive ids, 4 in 8", () => {
  for (const make of [(i) => String(i), (i) => `agent-${i}`, (i) => `mock-${i}`, (i) => `staff_${i}_x`]) {
    for (let start = 0; start < 60; start++) {
      const four = [0, 1, 2, 3].map((k) => make(start + k));
      assert.ok(women(four) >= 2, `${four} has fewer than 2 women`);
      const eight = Array.from({ length: 8 }, (_, k) => make(start + k));
      const w = women(eight);
      assert.ok(w >= 3 && w <= 5, `${eight} has ${w} women`);
    }
  }
  // ids that are numbers work too (the API gives integers, main.js passes strings)
  assert.equal(L.genderFromId(7), L.genderFromId("7"));
  // ids without a number still give a stable answer
  assert.equal(L.genderFromId("alice"), L.genderFromId("alice"));
  assert.ok(["male", "female"].includes(L.genderFromId("")));
});

test("the same agent has the same look every time, and ids and names change it", () => {
  for (const id of ["1", "2", "agent-3", "mock-4", "xyz"]) {
    const a = L.traitsForAgent(id, { name: "Sari" });
    const b = L.traitsForAgent(id, { name: "Sari" });
    assert.deepEqual(a, b);
    assert.equal(JSON.stringify(a), JSON.stringify(b));
  }
  const keys = new Set(Array.from({ length: 24 }, (_, i) => JSON.stringify(L.traitsForAgent(String(i + 1)))));
  assert.equal(keys.size, 24, "24 agents should have 24 different looks");
});

test("body proportions stay inside the ranges, women shorter and slimmer on average", () => {
  const sums = { male: { h: 0, w: 0, n: 0 }, female: { h: 0, w: 0, n: 0 } };
  for (let i = 1; i <= 200; i++) {
    const l = L.traitsForAgent(`agent-${i}`);
    const r = L.BUILD_RANGES[l.gender];
    for (const k of ["height", "width", "depth", "arm"]) {
      assert.ok(l.build[k] >= r[k][0] && l.build[k] <= r[k][1], `${l.agentId} ${k}=${l.build[k]}`);
    }
    // the ranges themselves are what the task asks for
    assert.ok(l.build.height >= 0.92 && l.build.height <= 1.04);
    assert.ok(l.build.width >= 0.84 && l.build.width <= 0.97);
    assert.ok(l.build.arm >= 0.85 && l.build.arm <= 0.95);
    sums[l.gender].h += l.build.height;
    sums[l.gender].w += l.build.width;
    sums[l.gender].n++;
  }
  assert.ok(sums.female.h / sums.female.n < sums.male.h / sums.male.n);
  assert.ok(sums.female.w / sums.female.n < sums.male.w / sums.male.n);
  assert.ok(sums.female.n > 70 && sums.male.n > 70);
});

test("clothes, hair and skin come from the fixed palettes", () => {
  const hairIds = new Set(["hair_buns", "hair_long", "hair_buzzedfemale", "hair_buzzed", "hair_simpleparted"]);
  let hijabs = 0;
  let greys = 0;
  let long = 0;
  for (let i = 1; i <= 300; i++) {
    const l = L.traitsForAgent(`a${i}`);
    assert.ok(L.SKIN_TONES.some((t) => t.name === l.skin.name));
    assert.equal(l.bodyId, `superhero_${l.gender}_${l.skin.variant}`);
    assert.ok(L.HAIR_COLORS.some(([n, hex]) => n === l.hair.color.name && hex === l.hair.color.hex));
    assert.ok(hairIds.has(l.hair.style));
    if (l.gender === "male") {
      assert.ok(["hair_buzzed", "hair_simpleparted"].includes(l.hair.id), "men get the short styles");
      assert.equal(l.hijab, null);
    } else {
      assert.equal(l.beard, false);
      if (l.hijab) {
        hijabs++;
        assert.equal(l.hair.id, null, "no hair mesh under a headscarf");
        assert.ok(L.HIJAB_COLORS.some(([n]) => n === l.hijab.name));
      } else if (["hair_long", "hair_buns"].includes(l.hair.id)) long++;
    }
    if (["ash", "silver"].includes(l.hair.color.name)) greys++;
    assert.ok([0, 1, 2, 3].includes(l.pattern.id));
    assert.equal(l.pattern.id === 0, l.pattern.hex2 === null);
    assert.ok(["shirt", "blouse", "batik"].includes(l.outfit));
  }
  assert.ok(hijabs > 10 && hijabs < 100, `hijabs ${hijabs}`); // some women, not all
  assert.ok(greys > 3 && greys < 60, `greys ${greys}`);       // a few older staff
  assert.ok(long > 20);
});

test("hair choices respect the loaded files and overrides", () => {
  const only = ["hair_buzzed", "hair_long"];
  for (let i = 1; i <= 40; i++) {
    const l = L.traitsForAgent(`b${i}`, { availableHair: only });
    if (l.gender === "male") assert.equal(l.hair.style, "hair_buzzed");
    else assert.equal(l.hair.style, "hair_long");
    assert.equal(l.beard, false); // the beard file is not loaded
  }
  assert.equal(L.traitsForAgent("1", { hair: "hair_buns" }).hair.style, "hair_buns");
  assert.equal(L.traitsForAgent("1", { skinTone: "tan" }).skin.name, "tan");
  assert.equal(L.traitsForAgent("1", { body: "male" }).gender, "male");
  // a partial look replaces fields and merges objects
  const l = L.traitsForAgent("1", { look: { hijab: null, build: { height: 1.01 }, shirt: { hex: 0x123456 } } });
  assert.equal(l.hijab, null);
  assert.equal(l.build.height, 1.01);
  assert.ok(l.build.width > 0.8);
  assert.equal(l.shirt.hex, 0x123456);
  assert.ok(l.shirt.name);
});

test("mixHex mixes channels", () => {
  assert.equal(L.mixHex(0x000000, 0xffffff, 0.5), 0x808080);
  assert.equal(L.mixHex(0xff0000, 0x0000ff, 0), 0xff0000);
  assert.equal(L.mixHex(0xff0000, 0x0000ff, 1), 0x0000ff);
});
