// Run with: node --test tests/js/
// Covers the pure helpers of skeleton/office/static/ui-onboarding.js (Requirements 1.1, 1.2): the business
// name rules (same as the backend), business-type normalising, form validation, failure messages, staff
// preview text and the mock backend. The screen itself needs a DOM and was checked in a real browser.
import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import path from "node:path";

const STATIC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../skeleton/office/static");
const O = await import(path.join(STATIC, "ui-onboarding.js"));

const TYPES = O.normalizeBusinessTypes([
  { key: "warung", label: "Warung / Toko", description: "desc", agents: [{ name: "Sari", role: "Kasir" }, { name: "Budi", role: "Stok" }] },
  { key: "jasa", label: "Usaha Jasa", description: "desc", agents: [{ name: "Dewi", role: "Booking" }] },
]);

// ---- business name ----------------------------------------------------------------------
test("validateBusinessName trims and accepts 1 to 60 characters", () => {
  assert.deepEqual(O.validateBusinessName("  Warung Bu Tini  "), { ok: true, value: "Warung Bu Tini", error: "" });
  assert.equal(O.validateBusinessName("a").ok, true);
  assert.equal(O.validateBusinessName("x".repeat(60)).ok, true);
  assert.equal(O.validateBusinessName("\u00e9".repeat(60)).ok, true);
  // Characters beyond the BMP count once, as in Python's len().
  assert.equal(O.validateBusinessName("\u{1F35A}".repeat(60)).ok, true);
  assert.equal(O.validateBusinessName("\u{1F35A}".repeat(61)).ok, false);
});

test("validateBusinessName refuses empty, too long, control characters and non-text", () => {
  for (const bad of ["", "   ", "\t\n"]) {
    const r = O.validateBusinessName(bad);
    assert.deepEqual([r.ok, r.value, r.error], [false, null, O.NAME_MESSAGES.empty]);
  }
  assert.equal(O.validateBusinessName("x".repeat(61)).error, O.NAME_MESSAGES.tooLong);
  for (const bad of ["a\u0000b", "a\nb", "a\u202eb", "a\u007fb"]) assert.equal(O.validateBusinessName(bad).error, O.NAME_MESSAGES.control, JSON.stringify(bad));
  for (const bad of [null, undefined, 5, {}, ["x"]]) assert.equal(O.validateBusinessName(bad).error, O.NAME_MESSAGES.notText);
  assert.equal(O.MAX_BUSINESS_NAME_CHARS, 60);
});

test("every name message is Indonesian text for the owner", () => {
  for (const message of Object.values(O.NAME_MESSAGES)) assert.match(message, /^Nama usaha /);
  assert.equal(O.TYPE_MESSAGE, "Pilih jenis usaha.");
});

// ---- business types ---------------------------------------------------------------------
test("normalizeBusinessTypes keeps well formed entries and drops the rest", () => {
  const out = O.normalizeBusinessTypes([
    { key: "warung", label: " Warung  /  Toko ", description: "x".repeat(500), agents: [{ name: "Sari", role: "Kasir" }, { name: "", role: "?" }, null] },
    { key: "warung", label: "Duplicate key", agents: [] },
    { key: "", label: "No key" },
    { key: "nolabel" },
    null,
    "text",
    { key: "jasa", label: "Jasa" },
  ]);
  assert.deepEqual(out.map((t) => t.key), ["warung", "jasa"]);
  assert.equal(out[0].label, "Warung / Toko");
  assert.equal(out[0].description.length, 300);
  assert.deepEqual(out[0].agents, [{ name: "Sari", role: "Kasir" }]);
  assert.deepEqual(out[1], { key: "jasa", label: "Jasa", description: "", agents: [] });
});

test("normalizeBusinessTypes treats a non-list as no types and caps the list", () => {
  for (const bad of [undefined, null, "warung", 3, {}]) assert.deepEqual(O.normalizeBusinessTypes(bad), []);
  const many = Array.from({ length: 50 }, (_, i) => ({ key: `t${i}`, label: `Type ${i}` }));
  assert.equal(O.normalizeBusinessTypes(many).length, 12);
});

test("normalizeBusinessTypes never passes markup through as anything but text", () => {
  const [type] = O.normalizeBusinessTypes([{ key: "warung", label: "<img src=x onerror=alert(1)>", agents: [{ name: "<b>Sari</b>", role: "Kasir" }] }]);
  // The strings are kept as they are: the screen writes them with textContent, so they stay text.
  assert.equal(type.label, "<img src=x onerror=alert(1)>");
  assert.equal(type.agents[0].name, "<b>Sari</b>");
});

// ---- form validation --------------------------------------------------------------------
test("validateOnboarding accepts a name and a known type", () => {
  const r = O.validateOnboarding("  Warung Bu Tini ", "warung", TYPES);
  assert.deepEqual(r, { ok: true, errors: {}, value: { name: "Warung Bu Tini", type: "warung" } });
});

test("validateOnboarding reports both problems at once and returns no value", () => {
  const r = O.validateOnboarding("   ", null, TYPES);
  assert.equal(r.ok, false);
  assert.equal(r.value, null);
  assert.equal(r.errors.name, O.NAME_MESSAGES.empty);
  assert.equal(r.errors.type, O.TYPE_MESSAGE);
});

test("validateOnboarding refuses a type the server did not offer", () => {
  for (const key of ["bengkel", "", undefined, 3]) assert.equal(O.validateOnboarding("Toko", key, TYPES).errors.type, O.TYPE_MESSAGE);
  assert.equal(O.validateOnboarding("Toko", "warung", []).errors.type, O.TYPE_MESSAGE);
  assert.equal(O.validateOnboarding("Toko", "warung", undefined).ok, false);
});

// ---- staff preview ----------------------------------------------------------------------
test("staffInitial and staffNames", () => {
  assert.equal(O.staffInitial("sari"), "S");
  assert.equal(O.staffInitial("  budi"), "B");
  assert.equal(O.staffInitial("\u{1F600}x"), "\u{1F600}");
  for (const bad of ["", "   ", null, undefined, 4]) assert.equal(O.staffInitial(bad), "?");
  assert.equal(O.staffNames([{ name: "Sari" }, { name: "Budi" }, { name: "" }]), "Sari, Budi");
  assert.equal(O.staffNames(undefined), "");
});

// ---- failure messages -------------------------------------------------------------------
test("describeOnboardingFailure gives a short Indonesian message and never repeats server input", () => {
  assert.match(O.describeOnboardingFailure(409, { error: "already_onboarded" }), /sudah dibuat/);
  assert.match(O.describeOnboardingFailure(409, { error: "onboarding_required" }), /Muat ulang/);
  assert.match(O.describeOnboardingFailure(422, { detail: [{ loc: ["body", "name"], msg: "<script>x</script>" }] }), /Nama usaha belum benar/);
  assert.equal(O.describeOnboardingFailure(422, { detail: [{ loc: ["body", "type"], msg: "unknown business type" }] }), O.TYPE_MESSAGE);
  assert.match(O.describeOnboardingFailure(422, { detail: "boom" }), /Isian belum benar/);
  assert.match(O.describeOnboardingFailure(500, null), /Server gagal/);
  assert.match(O.describeOnboardingFailure(0, null), /Tidak bisa menghubungi server/);
  assert.match(O.describeOnboardingFailure(418, null), /belum bisa dibuat/);
  for (const text of [409, 422, 500, 0, 418].map((s) => O.describeOnboardingFailure(s, { detail: [{ loc: ["x"], msg: "<script>" }] }))) {
    assert.ok(!text.includes("<"), text);
  }
});

// ---- mock backend -----------------------------------------------------------------------
test("the mock backend offers two business types and creates the office the real API would", async () => {
  const api = O.createMockOnboardingApi();
  const first = api.firstOffice();
  assert.equal(first.onboarding_required, true);
  assert.deepEqual(O.normalizeBusinessTypes(first.business_types).map((t) => t.key), ["warung", "jasa"]);
  const office = await api.createBusiness({ name: "Warung Bu Tini", type: "warung" });
  assert.equal(office.business.name, "Warung Bu Tini");
  assert.deepEqual(office.agents.map((a) => [a.name, a.role, a.desk_index]), [["Sari", "Kasir", 0], ["Budi", "Stok", 1], ["Wati", "Keuangan", 2], ["Joko", "Marketing", 3]]);
  assert.deepEqual(office.policy, { daily_limit_idr: null, blocked_actions: [], is_set: false });
  await assert.rejects(() => api.createBusiness({ name: "Toko", type: "nope" }), (err) => err.status === 422 && err.message === O.TYPE_MESSAGE);
});

test("the mock backend hands out copies, so one preview cannot change the next", () => {
  const api = O.createMockOnboardingApi();
  api.firstOffice().business_types[0].agents[0].name = "Changed";
  assert.equal(api.firstOffice().business_types[0].agents[0].name, "Sari");
});
