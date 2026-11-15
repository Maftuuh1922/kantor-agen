// Run with: node --test tests/js/
// Covers skeleton/office/static/ui-format.js (Requirements 2.6, 2.9, 1.3, 8.1): Rupiah, truncation,
// Jakarta time, state and status colors, policy form validation.
import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import path from "node:path";

const STATIC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../skeleton/office/static");
const F = await import(path.join(STATIC, "ui-format.js"));
const { MARKERS } = await import(path.join(STATIC, "behavior.js"));

// ---- Rupiah ---------------------------------------------------------------------

test("formatRupiah groups thousands with dots", () => {
  assert.equal(F.formatRupiah(0), "Rp 0");
  assert.equal(F.formatRupiah(999), "Rp 999");
  assert.equal(F.formatRupiah(1000), "Rp 1.000");
  assert.equal(F.formatRupiah(12500), "Rp 12.500");
  assert.equal(F.formatRupiah(100000), "Rp 100.000");
  assert.equal(F.formatRupiah(1234567890), "Rp 1.234.567.890");
});

test("formatRupiah rounds, keeps the sign and shows bad input as a dash", () => {
  assert.equal(F.formatRupiah(12500.6), "Rp 12.501");
  assert.equal(F.formatRupiah(-1500), "-Rp 1.500");
  for (const bad of [null, undefined, NaN, Infinity, "12500", {}]) assert.equal(F.formatRupiah(bad), "-");
  assert.equal(F.formatRupiah(1e21), "-"); // exponent form is not a money amount
});

test("parseRupiah accepts clean amounts and rejects the rest", () => {
  assert.equal(F.parseRupiah("100000"), 100000);
  assert.equal(F.parseRupiah(" 100.000 "), 100000);
  assert.equal(F.parseRupiah("Rp 100.000"), 100000);
  assert.equal(F.parseRupiah("rp100.000"), 100000);
  assert.equal(F.parseRupiah("1,234,567"), 1234567);
  assert.equal(F.parseRupiah("0"), 0);
  for (const bad of ["", "abc", "-5", "12.5", "1.00", "1..000", "10 0", "1e5", "Rp", null, undefined, 5, "100.0000"]) {
    assert.equal(F.parseRupiah(bad), null, `should reject ${JSON.stringify(bad)}`);
  }
});

test("parseRupiah reads back what formatRupiah wrote (random amounts)", () => {
  let seed = 12345;
  const rnd = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 2 ** 32);
  for (let i = 0; i < 500; i++) {
    const n = Math.floor(rnd() ** 3 * 10 ** 12);
    assert.equal(F.parseRupiah(F.formatRupiah(n)), n);
  }
});

// ---- policy form --------------------------------------------------------------------

test("validatePolicyInput returns the body PUT /api/policy expects", () => {
  const r = F.validatePolicyInput("Rp 100.000", ["spend", "customer_message", "spend"]);
  assert.equal(r.ok, true);
  assert.deepEqual(r.value, { daily_limit_idr: 100000, blocked_actions: ["customer_message", "spend"] });
  assert.deepEqual(r.errors, {});
  assert.equal(F.validatePolicyInput("0", []).ok, true, "a limit of 0 is allowed (nothing can be spent)");
});

test("validatePolicyInput explains every problem and returns no value", () => {
  const empty = F.validatePolicyInput("  ", []);
  assert.equal(empty.ok, false);
  assert.equal(empty.value, null);
  assert.match(empty.errors.limit, /Isi batas/);
  assert.match(F.validatePolicyInput("abc", []).errors.limit, /angka/);
  assert.match(F.validatePolicyInput("-5", []).errors.limit, /angka/);
  assert.match(F.validatePolicyInput(String(F.MAX_DAILY_LIMIT_IDR + 1), []).errors.limit, /tidak boleh lebih dari/);
  assert.equal(F.validatePolicyInput(String(F.MAX_DAILY_LIMIT_IDR), []).ok, true);
  assert.ok(F.validatePolicyInput("1000", ["launch_missiles"]).errors.blocked);
  assert.equal(F.validatePolicyInput(null, null).ok, false);
});

test("ACTION_KINDS matches the backend's allowed action types", () => {
  assert.deepEqual(F.ACTION_KINDS.map((k) => k.id).sort(), ["customer_message", "draft", "read", "spend"]);
});

// ---- truncation ---------------------------------------------------------------------

test("truncate keeps short text and cuts long text with an ellipsis", () => {
  assert.equal(F.truncate("Memeriksa stok"), "Memeriksa stok");
  const long = "x".repeat(100);
  const cut = F.truncate(long, 60);
  assert.equal(Array.from(cut).length, 60);
  assert.ok(cut.endsWith("…"));
  assert.equal(F.truncate("a".repeat(60), 60), "a".repeat(60), "exactly the limit is not cut");
  assert.equal(F.truncate("a".repeat(61), 60), `${"a".repeat(59)}…`);
});

test("truncate never exceeds the limit, collapses whitespace and counts emoji as one character", () => {
  assert.equal(F.truncate("  a \n  b\t c  "), "a b c");
  const emoji = "😀".repeat(70);
  const cut = F.truncate(emoji, 60);
  assert.equal(Array.from(cut).length, 60);
  assert.ok(!/[\ud800-\udbff]$/.test(cut), "no half of a surrogate pair at the end");
  for (const bad of [null, undefined, 5, {}]) assert.equal(F.truncate(bad), "");
  assert.equal(F.truncate("hello", 1), "…");
  assert.equal(F.truncate("hello", 0), "");
  let seed = 7;
  const rnd = () => ((seed = (seed * 1103515245 + 12345) >>> 0) / 2 ** 32);
  for (let i = 0; i < 300; i++) {
    const text = Array.from({ length: Math.floor(rnd() * 200) }, () => "ab é😀 "[Math.floor(rnd() * 6)] ?? "a").join("");
    const max = 1 + Math.floor(rnd() * 80);
    assert.ok(Array.from(F.truncate(text, max)).length <= max);
  }
});

test("bubbleText is empty for missing or blank text", () => {
  for (const v of [null, undefined, "", "   ", 3]) assert.equal(F.bubbleText(v), "");
  assert.equal(F.bubbleText("Beras menipis, tolong pesan"), "Beras menipis, tolong pesan");
  assert.equal(Array.from(F.bubbleText("y".repeat(200))).length, F.BUBBLE_MAX_CHARS);
});

test("errorMessage reads a string or FastAPI's list, speaks Indonesian and never returns markup-only junk", () => {
  assert.equal(F.errorMessage("agent not found"), "Staf tidak ditemukan.", "fixed API messages are translated");
  assert.equal(F.errorMessage("Pesan dari server"), "Pesan dari server", "other text passes through");
  assert.equal(F.errorMessage([{ msg: "must not be blank" }]), "Tidak boleh kosong.");
  // validator text we cannot translate is not shown in English
  assert.equal(F.errorMessage([{ msg: "Input should be 0 or more" }, { msg: "bad list" }]), "Isian belum benar.");
  assert.equal(F.errorMessage("constructor"), "constructor", "no prototype lookups");
  assert.equal(F.errorMessage(undefined), "Terjadi kesalahan. Coba lagi.");
  assert.equal(F.errorMessage(undefined, "fallback"), "fallback");
  assert.equal(F.errorMessage([], "fallback"), "fallback");
  assert.ok(Array.from(F.errorMessage("e".repeat(500))).length <= 200);
});

// ---- time ----------------------------------------------------------------------------

test("formatJakartaTime shows Asia/Jakarta time (UTC+7)", () => {
  assert.equal(F.formatJakartaTime("2025-11-14T07:30:00+00:00"), "14:30");
  assert.equal(F.formatJakartaTime("2025-11-14T07:30:00.123456+00:00"), "14:30");
  assert.equal(F.formatJakartaTime("2025-11-14T07:30:00Z"), "14:30");
  assert.equal(F.formatJakartaTime("2025-11-14T17:45:00+00:00"), "00:45", "crosses midnight");
  assert.equal(F.formatJakartaTime("2025-11-14T14:30:00+07:00"), "14:30", "other offsets are converted");
  assert.equal(F.formatJakartaTime("2025-11-14T07:30:00"), "14:30", "no zone is read as UTC, not browser time");
  assert.equal(F.formatJakartaTime(new Date(Date.UTC(2025, 0, 1, 0, 5))), "07:05");
});

test("formatJakartaTime gives a placeholder for bad input", () => {
  for (const bad of [null, undefined, "", "yesterday", 12, {}]) assert.equal(F.formatJakartaTime(bad), "--:--");
  assert.equal(F.formatJakartaDateTime("nope"), "");
  assert.match(F.formatJakartaDateTime("2025-11-14T07:30:00+00:00"), /14 Nov.*14:30.*WIB/);
  assert.match(F.formatJakartaDateTime("2025-08-17T01:00:00+00:00"), /17 Agu.*08:00.*WIB/, "month names are Indonesian");
  assert.match(F.formatJakartaDateTime("2025-10-05T01:00:00+00:00"), /5 Okt/);
});

// ---- colors -----------------------------------------------------------------------

test("stateColor: green idle, blue working, amber waiting, grey paused", () => {
  assert.equal(F.stateColor("idle"), "green");
  assert.equal(F.stateColor("working"), "blue");
  assert.equal(F.stateColor("handoff"), "blue");
  assert.equal(F.stateColor("waiting"), "amber");
  assert.equal(F.stateColor("paused"), "grey");
  assert.equal(F.stateColor("nonsense"), "grey");
  assert.equal(F.stateColor(undefined), "grey");
});

test("stateColor agrees with the behavior controller's markers", () => {
  for (const [state, marker] of Object.entries(MARKERS)) assert.equal(F.stateColor(state), marker, state);
  for (const color of ["idle", "working", "handoff", "waiting", "paused"].map(F.stateColor)) {
    assert.ok(F.MARKER_COLORS.includes(color));
  }
});

test("stateLabel and stateDetail are Indonesian and describe idle behaviors as idle", () => {
  assert.deepEqual(
    ["idle", "working", "handoff", "waiting", "paused"].map(F.stateLabel),
    ["Santai", "Bekerja", "Serah-terima", "Menunggu persetujuan", "Dijeda"],
  );
  assert.equal(F.stateLabel("???"), "Tidak diketahui");
  assert.equal(F.stateDetail("idle", null), "Santai");
  assert.equal(F.stateDetail("idle", "drink"), "Santai (mengambil minum)");
  assert.equal(F.stateDetail("idle", "chat"), "Santai (mengobrol dengan rekan)");
  assert.equal(F.stateDetail("working", "drink"), "Bekerja", "only idle shows an idle behavior");
});

test("actionStatusColor maps activity statuses", () => {
  assert.equal(F.actionStatusColor("done"), "green");
  assert.equal(F.actionStatusColor("pending"), "amber");
  assert.equal(F.actionStatusColor("rejected"), "red");
  assert.equal(F.actionStatusColor("failed"), "red");
  assert.equal(F.actionStatusColor("info"), "grey");
  assert.equal(F.actionStatusColor("other"), "grey");
  assert.deepEqual(
    ["done", "pending", "rejected", "failed", "info"].map(F.actionStatusLabel),
    ["Selesai", "Menunggu", "Ditolak", "Gagal", "Info"],
  );
  assert.equal(F.actionStatusLabel("other"), "Tidak diketahui");
});

// ---- spending summary --------------------------------------------------------------

test("spendSummary and spendRatio", () => {
  assert.equal(F.spendSummary(45000, 100000), "Rp 45.000 / Rp 100.000");
  assert.equal(F.spendSummary(0, null), "Rp 0 / belum diatur");
  assert.equal(F.spendRatio(45000, 100000), 0.45);
  assert.equal(F.spendRatio(150000, 100000), 1);
  assert.equal(F.spendRatio(0, 100000), 0);
  assert.equal(F.spendRatio(500, null), 0);
  assert.equal(F.spendRatio(500, 0), 1);
});

// ---- rename form (Requirement 1.4) --------------------------------------------------------
test("validateRename trims both texts and returns the PATCH body values", () => {
  const r = F.validateRename("  Sari Dewi ", " Kasir Utama ");
  assert.equal(r.ok, true);
  assert.deepEqual(r.value, { name: "Sari Dewi", role: "Kasir Utama" });
  assert.deepEqual(r.errors, {});
  assert.equal(F.validateRename("A".repeat(80), "B").ok, true, "80 characters is allowed");
});

test("validateRename explains every problem in Indonesian and returns no value", () => {
  const empty = F.validateRename("   ", "");
  assert.equal(empty.ok, false);
  assert.equal(empty.value, null);
  assert.equal(empty.errors.name, "Nama tidak boleh kosong.");
  assert.equal(empty.errors.role, "Peran tidak boleh kosong.");
  assert.match(F.validateRename("A".repeat(81), "x").errors.name, /terlalu panjang.*80/);
  assert.match(F.validateRename("x", "B".repeat(81)).errors.role, /terlalu panjang/);
  assert.match(F.validateRename("Bu\u0000Tini", "x").errors.name, /karakter kontrol/);
  assert.equal(F.validateRename(null, undefined).ok, false);
  assert.equal(F.validateRename("\u{1F600}".repeat(80), "x").ok, true, "emoji count as one character, like the server");
});

test("renameChanges sends only what changed", () => {
  const current = { name: "Sari", role: "Kasir" };
  assert.deepEqual(F.renameChanges(current, { name: "Sari", role: "Kasir" }), {});
  assert.deepEqual(F.renameChanges(current, { name: "Sari Dewi", role: "Kasir" }), { name: "Sari Dewi" });
  assert.deepEqual(F.renameChanges(current, { name: "Sari", role: "Kasir Utama" }), { role: "Kasir Utama" });
  assert.deepEqual(F.renameChanges(current, { name: "Dewi", role: "Bos" }), { name: "Dewi", role: "Bos" });
});

test("ACTION_KINDS come from the Indonesian dictionary", () => {
  assert.deepEqual(F.ACTION_KINDS.map((k) => k.label), ["Baca data", "Tulis draf", "Belanja", "Pesan ke pelanggan"]);
  assert.ok(F.ACTION_KINDS.every((k) => k.hint.length > 10));
});
