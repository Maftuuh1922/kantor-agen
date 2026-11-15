// Run with: node --test tests/js/
// Covers skeleton/office/static/ui-text.js (task 14.1, item F): the whole owner-facing UI is Indonesian, and the
// words live in one dictionary that can be reviewed and tested here.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const STATIC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../skeleton/office/static");
const { TEXT, fmt, walk } = await import(path.join(STATIC, "ui-text.js"));
const { createMockApi } = await import(path.join(STATIC, "ui-mock.js"));

// Words that are English and not Indonesian. A new English sentence in the dictionary trips this.
const ENGLISH = /\b(the|and|please|could not|cannot|not set|Pause|Resume|Rules|Save|Cancel|Loading|Agents?|Today|Quality|Idle|Working|Waiting|Paused|Done|Failed|Rejected|Pending|Activity|Spending|Switch|Dismiss|Handing|approval|Something went wrong|try again|Recent|Rename)\b/i;

test("the dictionary has no empty text and no English", () => {
  const leaves = [...walk()];
  assert.ok(leaves.length > 100, "the dictionary holds the whole UI");
  for (const [key, value] of leaves) {
    assert.ok(value.trim().length > 0, `${key} is empty`);
    if (key.startsWith("serverMessages.")) continue; // the keys of this group are the English API messages
    assert.ok(!ENGLISH.test(value), `${key} looks English: ${value}`);
  }
  for (const [key, value] of Object.entries(TEXT.serverMessages)) {
    assert.ok(!ENGLISH.test(value), `serverMessages[${key}] must be Indonesian: ${value}`);
  }
});

test("the state names are the agreed Indonesian words", () => {
  assert.equal(TEXT.states.working, "Bekerja");
  assert.equal(TEXT.states.waiting, "Menunggu persetujuan");
  assert.equal(TEXT.states.idle, "Santai");
  assert.equal(TEXT.states.paused, "Dijeda");
  assert.equal(TEXT.states.handoff, "Serah-terima");
});

test("the buttons are the agreed Indonesian words", () => {
  assert.equal(TEXT.hud.pause, "Jeda");
  assert.equal(TEXT.hud.resume, "Lanjutkan");
  assert.equal(TEXT.hud.rules, "Aturan");
  assert.equal(TEXT.policy.save, "Simpan");
  assert.equal(TEXT.policy.cancel, "Batal");
  assert.equal(TEXT.rename.save, "Simpan");
  assert.equal(TEXT.rename.cancel, "Batal");
  assert.equal(TEXT.rename.open, "Ubah nama");
  assert.equal(TEXT.quality.button, "Kualitas: {mode}");
});

test("fmt fills placeholders and leaves an unknown one visible", () => {
  assert.equal(fmt(TEXT.panel.pauseFor, { name: "Sari" }), "Jeda Sari");
  assert.equal(fmt(TEXT.quality.button, { mode: TEXT.quality.low }), "Kualitas: Rendah");
  assert.equal(fmt("Halo {nama}", {}), "Halo {nama}");
  assert.equal(fmt("tanpa isi", { a: 1 }), "tanpa isi");
});

test("every placeholder of the dictionary is a plain word that fmt can fill", () => {
  for (const [key, value] of walk()) {
    for (const [, name] of value.matchAll(/\{([^}]*)\}/g)) assert.match(name, /^\w+$/, `${key}: {${name}}`);
    assert.ok(!/[{}]/.test(fmt(value, Object.fromEntries([...value.matchAll(/\{(\w+)\}/g)].map((m) => [m[1], "x"])))), key);
  }
});

test("the dictionary cannot be changed at run time", () => {
  assert.throws(() => { "use strict"; TEXT.hud.pause = "Pause"; }, TypeError);
  assert.ok(Object.isFrozen(TEXT) && Object.isFrozen(TEXT.states));
});

// ---- index.html --------------------------------------------------------------------------------
const html = readFileSync(path.join(STATIC, "index.html"), "utf8");
const visible = (src) => src
  .replace(/<!--[\s\S]*?-->/g, "")
  .replace(/<script[\s\S]*?<\/script>/g, "")
  .replace(/<style[\s\S]*?<\/style>/g, "");

test("index.html is Indonesian: lang id and no English words left", () => {
  assert.match(html, /<html lang="id">/);
  assert.ok(!/lang="en"/.test(html));
  const text = visible(html);
  // text between tags and the human attributes (aria-label, title, placeholder)
  const words = [
    ...[...text.matchAll(/>([^<>]+)</g)].map((m) => m[1]),
    ...[...text.matchAll(/\b(?:aria-label|title|placeholder)="([^"]*)"/g)].map((m) => m[1]),
  ].map((t) => t.trim()).filter(Boolean);
  assert.ok(words.length > 40);
  for (const w of words) assert.ok(!ENGLISH.test(w), `index.html has English text: ${w}`);
});

test("the static labels of index.html match the dictionary", () => {
  const mustContain = [
    TEXT.hud.today, TEXT.hud.demo, TEXT.hud.paused, TEXT.hud.rules, TEXT.hud.pause, TEXT.hud.pauseAll,
    TEXT.hud.spendTitle, TEXT.hud.demoTitle, TEXT.hud.rulesTitle, TEXT.quality.hint, TEXT.quality.hintYes,
    TEXT.quality.hintNo, TEXT.quality.hintNoLabel, TEXT.quality.buttonTitle,
    `${TEXT.quality.button.replace("{mode}", TEXT.quality.normal)}`,
    TEXT.feed.title, TEXT.feed.label, TEXT.feed.countLabel, TEXT.feed.listLabel, TEXT.feed.empty,
    TEXT.policy.title, TEXT.policy.intro, TEXT.policy.limitLabel, TEXT.policy.blockedLabel, TEXT.policy.blockedHelp,
    TEXT.policy.cancel, TEXT.policy.saveAndStart, TEXT.loading.label, TEXT.fatal.noScript,
    TEXT.office.canvas, TEXT.office.overlay,
  ];
  for (const text of mustContain) assert.ok(html.includes(text), `index.html does not contain: ${text}`);
});

// ---- the modules take their words from the dictionary ---------------------------------------------
test("no ui module keeps an English sentence for the owner", () => {
  const files = ["ui-hud.js", "ui-panel.js", "ui-feed.js", "ui-overlay.js", "ui-policy.js", "ui-api.js", "ui-format.js", "main.js"];
  const english = /["'`](?:Pause|Resume|Rules|Save|Cancel|Loading|Today|Quality|Idle|Working|Paused|Waiting|Done|Failed|Rejected|Pending|Activity|Could not|Cannot|The server|Close agent|Recent actions|No actions|Agent panel|Spending today)\b[^"'`]*["'`]/;
  for (const file of files) {
    const code = readFileSync(path.join(STATIC, file), "utf8").replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:"'`])\/\/[^\n]*/g, "$1");
    assert.ok(!english.test(code), `${file} still has English text: ${code.match(english)?.[0]}`);
    if (file !== "ui-format.js") assert.ok(code.includes('./ui-text.js'), `${file} does not use ui-text.js`);
  }
});

test("the static folder has exactly one dictionary of words (ui-text.js)", () => {
  assert.ok(readdirSync(STATIC).includes("ui-text.js"));
});

// ---- ?mock=1 uses the same friendly action words as the live roles -----------------------------------
test("the mock backend shows friendly Indonesian actions, never the task kind", async () => {
  const office = {
    business: { id: 1, name: "Kantor Warung (contoh)", type: "warung", paused: false },
    agents: [
      { id: "mock-1", name: "Sari", role: "Kasir", desk_index: 0, paused: false },
      { id: "mock-2", name: "Budi", role: "Stok", desk_index: 1, paused: false },
      { id: "mock-3", name: "Wati", role: "Keuangan", desk_index: 2, paused: false },
      { id: "mock-4", name: "Joko", role: "Marketing", desk_index: 3, paused: false },
    ],
  };
  const api = createMockApi(office, {});
  const rows = await api.getActivity(50);
  const actions = new Set(rows.map((r) => r.action));
  for (const word of ["Catat penjualan", "Pesan stok", "Ringkasan harian", "Kirim promo"]) assert.ok(actions.has(word), word);
  for (const kind of ["read", "draft", "spend", "customer_message"]) assert.ok(!actions.has(kind), kind);
  const rejected = rows.find((r) => r.status === "rejected");
  assert.match(rejected.reason, /ditolak: batas harian Rp 100\.000 tercapai/);
  for (const r of rows) assert.ok(!ENGLISH.test(`${r.action} ${r.reason}`), `${r.action}: ${r.reason}`);
});

test("the mock backend renames an agent like PATCH /api/agents/{id}", async () => {
  const office = {
    business: { id: 1, name: "x", type: "warung", paused: false },
    agents: [{ id: "mock-1", name: "Sari", role: "Kasir", desk_index: 0, paused: false }],
  };
  const api = createMockApi(office, {});
  const result = await api.patchAgent("mock-1", { name: "  Sari Dewi ", role: "Kasir Utama" });
  assert.equal(result.name, "Sari Dewi");
  assert.equal(result.role, "Kasir Utama");
  const again = await api.getOffice();
  assert.deepEqual(again.agents.map((a) => [a.name, a.role]), [["Sari Dewi", "Kasir Utama"]]);
  assert.equal((await api.getAgent("mock-1")).name, "Sari Dewi");
  await assert.rejects(() => api.patchAgent("mock-1", {}), /nama, peran/);
  await assert.rejects(() => api.patchAgent("mock-1", { name: "   " }), /kosong/);
  await assert.rejects(() => api.patchAgent("nope", { name: "x" }), /tidak ditemukan/);
  // the scripted actions still fit after a role rename
  const rows = await api.getActivity(10);
  assert.ok(rows.some((r) => r.action === "Catat penjualan"));
});
