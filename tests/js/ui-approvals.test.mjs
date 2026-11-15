// Run with: node --test tests/js/
// Covers skeleton/office/static/ui-approvals-logic.js (task 14.2: the owner's approval queue) and the approval part of
// the mock backend (ui-mock.js): the badge text, the item text, and what the owner reads for every answer of
// POST /api/approvals/{id}/decision. The DOM part (ui-approvals.js) needs a browser and was checked in one.
import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import path from "node:path";

const STATIC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../skeleton/office/static");
const L = await import(path.join(STATIC, "ui-approvals-logic.js"));
const { TEXT, walk } = await import(path.join(STATIC, "ui-text.js"));
const { createMockApi } = await import(path.join(STATIC, "ui-mock.js"));

const NOW = Date.parse("2025-03-10T03:30:00Z");
const row = (over = {}) => ({
  id: 1, agent_id: 2, agent_name: "Budi", kind: "purchase_order", subject: "Pesan stok Gula Pasir 1 kg", amount_idr: 232500,
  summary: "15 pcs x Rp 15.500", reason: "Stok tinggal 3", status: "pending", created_at: "2025-03-10T03:00:00.000000+00:00", ...over,
});

// ---- badge ---------------------------------------------------------------------------------------
test("the badge text and state follow the count", () => {
  assert.equal(L.badgeText(0), "Menunggu keputusan (0)");
  assert.equal(L.badgeText(3), "Menunggu keputusan (3)");
  assert.equal(L.badgeState(0), "none");
  assert.equal(L.badgeState(1), "pending");
  for (const bad of [-1, 1.5, NaN, null, undefined, "3", Infinity]) {
    assert.equal(L.normalizeCount(bad), 0, String(bad));
    assert.equal(L.badgeText(bad), "Menunggu keputusan (0)");
  }
});

// ---- rows ----------------------------------------------------------------------------------------
test("normalizeApprovals keeps usable rows, oldest first, and survives bad data", () => {
  const rows = L.normalizeApprovals([
    row({ id: 3, created_at: "2025-03-10T03:20:00+00:00" }),
    row({ id: 1, created_at: "2025-03-10T03:00:00+00:00" }),
    row({ id: 2, created_at: "2025-03-10T03:00:00+00:00" }),
    { id: "x", subject: "bad id" }, { id: 4 }, null, 7, "text",
    row({ id: 5, amount_idr: "much", summary: null, reason: undefined, agent_name: null }),
  ]);
  assert.deepEqual(rows.map((r) => r.id), [1, 2, 5, 3]); // by time, the same time by id
  const odd = rows.find((r) => r.id === 5);
  assert.equal(odd.amount_idr, 0);
  assert.equal(odd.summary, "");
  assert.equal(odd.reason, "");
  assert.equal(odd.agent_name, "");
  for (const bad of [null, undefined, {}, "x", 5]) assert.deepEqual(L.normalizeApprovals(bad), []);
});

test("rowsForAgent filters on the agent and compares ids as text", () => {
  const rows = L.normalizeApprovals([row({ id: 1, agent_id: 2 }), row({ id: 2, agent_id: "mock-2" }), row({ id: 3, agent_id: null })]);
  assert.deepEqual(L.rowsForAgent(rows, "2").map((r) => r.id), [1]);
  assert.deepEqual(L.rowsForAgent(rows, 2).map((r) => r.id), [1]);
  assert.deepEqual(L.rowsForAgent(rows, "mock-2").map((r) => r.id), [2]);
  assert.deepEqual(L.rowsForAgent(rows, null).map((r) => r.id), [1, 2, 3]);
  assert.deepEqual(L.rowsForAgent(rows, "99"), []);
});

// ---- time and item text -----------------------------------------------------------------------------
test("relativeTime: just now, minutes, hours, then the Jakarta clock", () => {
  const at = (ms) => new Date(NOW - ms).toISOString();
  assert.equal(L.relativeTime(at(20_000), NOW), "baru saja");
  assert.equal(L.relativeTime(at(5 * 60_000), NOW), "5 menit lalu");
  assert.equal(L.relativeTime(at(59 * 60_000), NOW), "59 menit lalu");
  assert.equal(L.relativeTime(at(2 * 3600_000 + 5000), NOW), "2 jam lalu");
  // 03:30 UTC is 10:30 WIB; 9 hours earlier it shows the clock: 01:30 WIB
  assert.equal(L.relativeTime("2025-03-09T18:30:00+00:00", NOW), "01:30");
  // the server sends microseconds and an offset; a clock a little behind the server's never shows "-1 menit"
  assert.equal(L.relativeTime("2025-03-10T03:24:59.123456+00:00", NOW), "5 menit lalu");
  assert.equal(L.relativeTime("2025-03-10T03:31:00+00:00", NOW), "10:31");
  assert.equal(L.relativeTime("not a time", NOW), "--:--");
});

test("itemView gives the owner what the task asks for: subject, Rupiah amount, agent, time, summary, reason", () => {
  const v = L.itemView(row(), NOW);
  assert.equal(v.subject, "Pesan stok Gula Pasir 1 kg");
  assert.equal(v.amountText, "Rp 232.500");
  assert.equal(v.agentText, "Diajukan oleh Budi");
  assert.equal(v.timeText, "30 menit lalu");
  assert.match(v.timeTitle, /\(WIB\)$/);
  assert.equal(v.kindLabel, "Pesanan stok");
  assert.equal(v.summary, "15 pcs x Rp 15.500");
  assert.equal(v.reason, "Alasan: Stok tinggal 3");
  assert.equal(L.itemView(row({ kind: "invoice" }), NOW).kindLabel, "Faktur");
  assert.equal(L.itemView(row({ kind: "something_new" }), NOW).kindLabel, "Permintaan");
  assert.equal(L.itemView(row({ kind: "constructor" }), NOW).kindLabel, "Permintaan");
  const bare = L.itemView(row({ agent_name: "  ", reason: "", summary: "" }), NOW);
  assert.equal(bare.agentText, "Diajukan oleh staf");
  assert.equal(bare.reason, "");
  assert.equal(bare.summary, "");
});

// ---- the answer to a decision ------------------------------------------------------------------------
const answer = (approve, status, data, r = row()) => L.interpretDecision(approve, r, { status, data });

test("200: approved with a cost, approved without one, rejected", () => {
  const spent = answer(true, 200, { id: 1, status: "approved", message: "ok", cost_idr: 232500, pending_count: 1 });
  assert.deepEqual([spent.kind, spent.tone, spent.refresh, spent.pendingCount], ["decided", "ok", true, 1]);
  assert.equal(spent.message, "Disetujui: Pesan stok Gula Pasir 1 kg (Rp 232.500).");
  const free = answer(true, 200, { status: "approved", cost_idr: 0, pending_count: 0 });
  assert.equal(free.message, "Disetujui: Pesan stok Gula Pasir 1 kg.");
  assert.equal(free.pendingCount, 0);
  const no = answer(false, 200, { status: "rejected", cost_idr: 0, pending_count: 2 });
  assert.equal(no.message, "Ditolak: Pesan stok Gula Pasir 1 kg.");
  assert.equal(no.kind, "decided");
  assert.equal(answer(true, 200, {}).pendingCount, null); // no count in the answer: the caller counts
});

test("422 over the limit: the server's own message, and the owner is offered Aturan", () => {
  const detail = "Melebihi batas harian Rp 50.000; naikkan batas lewat Aturan lalu setujui lagi";
  const r = answer(true, 422, { detail, status: "pending" });
  assert.deepEqual([r.kind, r.tone, r.offerRules, r.refresh], ["retry", "error", true, false]);
  assert.equal(r.message, detail);
  const blocked = answer(true, 422, { detail: "Belanja diblokir di Aturan; buka blokirnya lewat Aturan lalu setujui lagi", status: "pending" });
  assert.equal(blocked.offerRules, true);
  const unset = answer(true, 422, { detail: "Batas harian belum diatur; atur lewat Aturan lalu setujui lagi", status: "pending" });
  assert.equal(unset.offerRules, true);
  // a policy message that does not talk about the rules gets no button
  const other = answer(true, 422, { detail: "Jenis permintaan ini belum bisa diputuskan di aplikasi ini", status: "pending" });
  assert.equal(other.offerRules, false);
  assert.equal(other.kind, "retry");
});

test("422 validation (a list) and 503 and other statuses: a clear Indonesian message, no English from the server", () => {
  const list = answer(true, 422, { detail: [{ loc: ["body", "note"], msg: "String should have at most 200 characters", type: "string_too_long" }] });
  assert.equal(list.message, TEXT.errors.invalidInput);
  assert.equal(list.kind, "retry");
  assert.equal(list.offerRules, false);
  assert.equal(answer(true, 503, { detail: "database unavailable, try again" }).message, "Server sibuk, coba lagi");
  assert.equal(answer(true, 503, null).kind, "retry");
  const odd = answer(true, 500, null);
  assert.equal(odd.message, "Server menjawab 500.");
  assert.equal(odd.kind, "retry");
  assert.equal(answer(true, 0, undefined).message, "Server menjawab ?.");
  assert.equal(L.interpretDecision(true, row(), undefined).kind, "retry");
});

test("404 and 409: the request is closed, the queue reloads", () => {
  const gone = answer(true, 404, { detail: "approval not found" });
  assert.deepEqual([gone.kind, gone.refresh, gone.offerRules], ["stale", true, false]);
  assert.match(gone.message, /sudah tidak ada/);
  const done = answer(false, 409, { detail: "approval is not pending", status: "approved" });
  assert.deepEqual([done.kind, done.refresh], ["stale", true]);
  assert.match(done.message, /sudah diputuskan/);
  const failed = answer(true, 409, { id: 1, status: "failed", message: "Pesanan sudah dibatalkan", cost_idr: 0, pending_count: 0, detail: "Pesanan sudah dibatalkan" });
  assert.deepEqual([failed.kind, failed.refresh, failed.pendingCount], ["stale", true, 0]);
  assert.equal(failed.message, "Persetujuan gagal. Pesanan sudah dibatalkan");
  const bare = answer(true, 409, { status: "failed", detail: "" });
  assert.equal(bare.message, TEXT.approvals.failedNoMessage);
});

test("a message from the server is never longer than the inline limit", () => {
  const r = answer(true, 422, { detail: `Melebihi batas harian Rp 1; ${"aturan ".repeat(80)}` });
  assert.ok(Array.from(r.message).length <= L.INLINE_MESSAGE_MAX);
});

test("no answer at all (network error, timeout): say it, and reload to check", () => {
  const r = L.interpretDecisionFailure(new Error("Tidak bisa menghubungi server."));
  assert.deepEqual([r.kind, r.refresh, r.offerRules], ["retry", true, false]);
  assert.match(r.message, /^Keputusan belum terkirim\. Tidak bisa menghubungi server\./);
  assert.match(L.interpretDecisionFailure(undefined).message, /Tidak bisa menghubungi server/);
});

test("noteForRequest trims, folds blanks, cuts at 200 characters and gives null for empty", () => {
  assert.equal(L.noteForRequest("  harga naik \n\n besok "), "harga naik besok");
  assert.equal(L.noteForRequest(""), null);
  assert.equal(L.noteForRequest("   "), null);
  assert.equal(L.noteForRequest(undefined), null);
  assert.equal(L.noteForRequest(42), null);
  assert.equal(Array.from(L.noteForRequest("x".repeat(500))).length, 200);
  assert.equal(Array.from(L.noteForRequest("\u{1F35A}".repeat(300))).length, 200);
});

test("every approval text is Indonesian and has the placeholders the logic fills", () => {
  const A = [...walk(TEXT.approvals, "approvals")];
  assert.ok(A.length >= 40);
  for (const [key, value] of A) assert.ok(value.trim(), key);
  assert.equal(TEXT.approvals.panelTitle, "Antrian persetujuan");
  assert.equal(TEXT.approvals.empty, "Tidak ada yang menunggu keputusan");
  assert.equal(TEXT.approvals.approve, "Setujui");
  assert.equal(TEXT.approvals.reject, "Tolak");
  assert.equal(TEXT.approvals.openRules, "Buka Aturan");
  assert.equal(TEXT.approvals.busy, "Server sibuk, coba lagi");
});

// ---- the mock backend (?mock=1) -----------------------------------------------------------------------
function mock() {
  return createMockApi({
    business: { id: 1, name: "x", type: "warung", paused: false },
    agents: [
      { id: "mock-1", name: "Sari", role: "Kasir", desk_index: 0, paused: false },
      { id: "mock-2", name: "Budi", role: "Stok", desk_index: 1, paused: false },
      { id: "mock-3", name: "Wati", role: "Keuangan", desk_index: 2, paused: false },
    ],
  }, {});
}

test("the mock queue has one purchase order and one invoice, oldest first, with the real shape", async () => {
  const api = mock();
  const list = await api.getApprovals();
  assert.deepEqual(list.map((a) => a.kind), ["purchase_order", "invoice"]);
  assert.deepEqual(list.map((a) => a.agent_name), ["Budi", "Wati"]);
  assert.ok(list[0].created_at < list[1].created_at);
  for (const a of list) {
    assert.deepEqual(Object.keys(a).sort(), ["agent_id", "agent_name", "amount_idr", "created_at", "id", "kind", "reason", "status", "subject", "summary"]);
    assert.equal(a.status, "pending");
  }
  assert.equal(L.normalizeApprovals(list).length, 2);
  assert.equal((await api.getOffice()).pending_approvals, 2);
});

test("the mock refuses the first purchase order over the limit, and accepts it after the limit is raised", async () => {
  const api = mock();
  const over = await api.decideApproval(1, true, null);
  assert.equal(over.status, 422);
  assert.match(over.data.detail, /^Melebihi batas harian Rp 100\.000; naikkan batas lewat Aturan/);
  assert.equal(over.data.status, "pending");
  const shown = L.interpretDecision(true, row(), over);
  assert.equal(shown.offerRules, true);
  assert.equal((await api.getApprovals()).length, 2, "nothing changed");
  await api.putPolicy({ daily_limit_idr: 500000, blocked_actions: [] });
  const ok = await api.decideApproval(1, true, "harga dicek");
  assert.equal(ok.status, 200);
  assert.deepEqual([ok.data.status, ok.data.cost_idr, ok.data.pending_count], ["approved", 232500, 1]);
  assert.equal((await api.getApprovals()).length, 1);
  assert.equal((await api.getOffice()).pending_approvals, 1);
  assert.equal((await api.getOffice()).spent_today_idr, 92000 + 232500, "the spending today includes the purchase");
  const feed = await api.getActivity(1);
  assert.equal(feed[0].action, "Keputusan pemilik");
  assert.match(feed[0].reason, /Pemilik menyetujui: Pesan stok Gula Pasir 1 kg Rp 232\.500.*Catatan: harga dicek/);
  // decided before: 409, unknown: 404, bad note: 422 with a list
  const again = await api.decideApproval(1, true, null);
  assert.deepEqual([again.status, again.data.detail, again.data.status], [409, "approval is not pending", "approved"]);
  assert.equal((await api.decideApproval(99, true, null)).status, 404);
  const longNote = await api.decideApproval(2, true, "x".repeat(201));
  assert.equal(longNote.status, 422);
  assert.ok(Array.isArray(longNote.data.detail));
});

test("the mock: an invoice needs no limit, a rejection never spends, a blocked or unset policy says so", async () => {
  const api = mock();
  const no = await api.decideApproval(1, false, null);
  assert.deepEqual([no.status, no.data.status, no.data.cost_idr, no.data.pending_count], [200, "rejected", 0, 1]);
  const invoice = await api.decideApproval(2, true, null);
  assert.deepEqual([invoice.status, invoice.data.status, invoice.data.cost_idr, invoice.data.pending_count], [200, "approved", 0, 0]);
  assert.deepEqual(await api.getApprovals(), []);

  const blocked = mock();
  await blocked.putPolicy({ daily_limit_idr: 900000, blocked_actions: ["spend"] });
  const b = await blocked.decideApproval(1, true, null);
  assert.equal(b.status, 422);
  assert.match(b.data.detail, /^Belanja diblokir di Aturan/);

  const unset = createMockApi({ business: { id: 1, name: "x", type: "warung", paused: false }, agents: [{ id: "mock-1", name: "Sari", role: "Stok", desk_index: 0, paused: false }] }, { policySet: false });
  const u = await unset.decideApproval(1, true, null);
  assert.match(u.data.detail, /^Batas harian belum diatur/);
  // with one agent both requests belong to that agent
  assert.deepEqual((await unset.getApprovals()).map((a) => a.agent_name), ["Sari", "Sari"]);
});
