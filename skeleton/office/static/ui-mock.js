// In-memory stand-in for the backend, used by `?mock=1`. It has the same methods as ui-api.js.
//
//   ?mock=1                 policy already set, spending Rp 92.000 of Rp 100.000
//   ?mock=1&policy=unset    policy not set yet: the first-run policy dialog opens
//
// The approval queue (task 14.2) starts with two requests, always the same: a purchase order of Rp 232.500 (Stok)
// and an invoice of Rp 1.850.000 (Keuangan). Approving the purchase order while the limit is Rp 100.000 answers
// 422 "Melebihi batas harian ..." (the "Buka Aturan" path); after the limit is raised in Aturan it works.
//
// New actions arrive every few seconds through the same "kantor:activity" window event that
// the live SSE feed uses, so the panel and the feed run the same code in both modes.

import { TEXT } from "./ui-text.js";

const DAILY_LIMIT = 100000;
const NEW_ACTION_EVERY_S = 12;

// Same friendly action words as the live roles (apps/kantor_warung/roles.py): the feed reads "Catat penjualan",
// not the task kind. Keyed by the role label the mock agents carry (see MOCK_AGENT_ROWS in main.js).
const TEMPLATES = Object.freeze({
  Kasir: [
    { action: "Catat penjualan", reason: "Catat penjualan: 3x Mie Instan Goreng, 1x Teh Celup 25 pcs", cost: 0, status: "done" },
    { action: "Catat penjualan", reason: "Catat penjualan: 2x Minyak Goreng 1 L", cost: 0, status: "done" },
  ],
  Stok: [
    { action: "Pesan stok", reason: "Stok menipis, Kecap Manis 275 ml tinggal 2 (min 4): pesan 10 pcs (Rp 92.000)", cost: 92000, status: "done" },
    { action: "Pesan stok", reason: "Pesan stok Teh Botol Rp 232.500 ditolak: batas harian Rp 100.000 tercapai. Tugas: Stok menipis", cost: 0, status: "rejected" },
  ],
  Keuangan: [
    { action: "Ringkasan harian", reason: "Ringkasan harian 2025-03-10", cost: 0, status: "done" },
    { action: "Ajukan pesanan stok", reason: "Stok menipis, Gula Pasir 1 kg tinggal 3: menunggu pemilik", cost: 0, status: "pending" },
  ],
  Marketing: [
    { action: "Kirim promo", reason: "Promo pelanggan setia: penjualan turun 3 hari (masuk outbox, belum dikirim)", cost: 0, status: "pending" },
    { action: "Ide promo", reason: "Ide promo: penjualan turun 3 hari berturut-turut", cost: 0, status: "info" },
  ],
});

function isoMinutesAgo(minutes) {
  return new Date(Date.now() - minutes * 60000).toISOString();
}

const MAX_NOTE_CHARS = 200;
const rupiahText = (n) => `Rp ${String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ".")}`;
// The window events the live SSE feed also produces, so the page runs the same code in both modes.
function emit(name, detail) {
  if (typeof window !== "undefined" && typeof CustomEvent === "function") window.dispatchEvent(new CustomEvent(name, { detail }));
}

/**
 * @param office  the mock office object (business, agents, ...)
 * @param opts    { policySet: boolean, applyVisualState(evt) }  the hook moves the characters when pausing
 */
export function createMockApi(office, { policySet = true, applyVisualState = () => {} } = {}) {
  const agents = new Map();
  let nextId = 1;
  for (const a of office.agents) {
    // `tpl` keeps the first role, so the scripted actions still fit after the owner renames the role.
    agents.set(String(a.id), { ...a, tpl: a.role, state: "idle", bubble_text: "", spent: 0, actions: [] });
  }
  const business = { ...office.business };
  let policy = policySet
    ? { daily_limit_idr: DAILY_LIMIT, blocked_actions: ["customer_message"], is_set: true }
    : { daily_limit_idr: null, blocked_actions: [], is_set: false };

  function addAction(agent, tpl, minutesAgo) {
    const row = {
      id: nextId++,
      agent_id: agent.id,
      action: tpl.action,
      reason: tpl.reason,
      cost_idr: tpl.cost,
      status: tpl.status,
      created_at: isoMinutesAgo(minutesAgo),
    };
    agent.actions.unshift(row);
    if (tpl.status === "done") agent.spent += tpl.cost;
    return row;
  }
  // A few actions to start with so the panel is not empty.
  for (const a of agents.values()) {
    const list = TEMPLATES[a.tpl] ?? TEMPLATES.Kasir;
    addAction(a, list[1], 95);
    addAction(a, list[0], 41);
  }
  let spent = [...agents.values()].reduce((sum, a) => sum + a.spent, 0);

  // ---- approvals: the same shapes as GET /api/approvals and POST /api/approvals/{id}/decision ----
  const agentList = [...agents.values()];
  const agentFor = (role) => agentList.find((a) => a.tpl === role) ?? agentList[0];
  const approvals = [
    {
      id: 1, agent_id: agentFor("Stok").id, kind: "purchase_order", subject: "Pesan stok Gula Pasir 1 kg", amount_idr: 232500,
      summary: "15 pcs x Rp 15.500 dari CV Sumber Pangan", reason: "Stok tinggal 3, batas minimum 10",
      status: "pending", created_at: isoMinutesAgo(14),
    },
    {
      id: 2, agent_id: agentFor("Keuangan").id, kind: "invoice", subject: "Faktur untuk Toko Maju Jaya", amount_idr: 1850000,
      summary: "Jasa desain kemasan, 3 revisi", reason: "Nilai faktur di atas Rp 1.000.000, perlu persetujuan pemilik",
      status: "pending", created_at: isoMinutesAgo(5),
    },
  ];
  const pendingApprovals = () => approvals.filter((a) => a.status === "pending");
  const approvalView = (a) => ({ ...a, agent_name: agents.get(String(a.agent_id))?.name ?? "" });

  const publicAgent = (a) => ({ id: a.id, name: a.name, role: a.role, desk_index: a.desk_index, paused: a.paused });
  const sleep = (ms = 60) => new Promise((r) => setTimeout(r, ms));
  const fail = (message) => { throw new Error(message); };

  let clock = 0;
  let counter = 0;

  return {
    mock: true,

    async getOffice() {
      await sleep();
      return {
        business: { ...business },
        agents: [...agents.values()].map(publicAgent),
        policy: { ...policy, blocked_actions: [...policy.blocked_actions] },
        spent_today_idr: spent,
        demo_mode: true,
        pending_approvals: pendingApprovals().length,
      };
    },

    async getAgent(id) {
      await sleep();
      const a = agents.get(String(id)) ?? fail("agent not found");
      return {
        ...publicAgent(a),
        state: a.state,
        bubble_text: a.bubble_text,
        spent_today_idr: a.spent,
        recent_actions: a.actions.slice(0, 10).map((r) => ({ ...r })),
      };
    },

    async getActivity(limit = 5) {
      await sleep();
      const all = [...agents.values()].flatMap((a) => a.actions).sort((x, y) => (x.created_at < y.created_at ? 1 : -1));
      return all.slice(0, limit).map((r) => ({ ...r, business_id: business.id }));
    },

    async pauseOffice(paused) {
      await sleep();
      business.paused = !!paused;
      for (const a of agents.values()) {
        if (!a.paused) applyVisualState({ agent_id: a.id, state: paused ? "paused" : "idle" });
      }
      return { paused: business.paused };
    },

    async pauseAgent(id, paused) {
      await sleep();
      const a = agents.get(String(id)) ?? fail("agent not found");
      a.paused = !!paused;
      applyVisualState({ agent_id: a.id, state: paused || business.paused ? "paused" : "idle" });
      return { id: a.id, paused: a.paused, business_paused: business.paused };
    },

    /** Rename an agent or change its role label, like PATCH /api/agents/{id}. */
    async patchAgent(id, changes) {
      await sleep();
      const a = agents.get(String(id)) ?? fail(TEXT.serverMessages["agent not found"]);
      const name = changes?.name;
      const role = changes?.role;
      if (name === undefined && role === undefined) fail(TEXT.serverMessages["give a name, a role, or both"]);
      for (const text of [name, role]) {
        if (text !== undefined && (typeof text !== "string" || !text.trim())) fail(TEXT.serverMessages["must not be blank"]);
      }
      if (name !== undefined) a.name = name.trim();
      if (role !== undefined) a.role = role.trim();
      return publicAgent(a);
    },

    async putPolicy(body) {
      await sleep();
      if (!Number.isInteger(body?.daily_limit_idr) || body.daily_limit_idr < 0) fail("daily_limit_idr harus bilangan bulat, 0 atau lebih");
      policy = { daily_limit_idr: body.daily_limit_idr, blocked_actions: [...body.blocked_actions], is_set: true };
      return { ...policy };
    },

    /** Pending requests, oldest first, like GET /api/approvals. */
    async getApprovals() {
      await sleep();
      return pendingApprovals().map(approvalView);
    },

    /**
     * Like POST /api/approvals/{id}/decision. Resolves with { status, data } for every HTTP answer (200, 404, 409, 422).
     * Approving a purchase order goes through the limit like the real policy engine: the amount must fit what is left today.
     */
    async decideApproval(id, approve, note = null) {
      await sleep();
      const row = approvals.find((a) => a.id === Number(id));
      if (!row) return { status: 404, data: { detail: "approval not found" } };
      if (typeof approve !== "boolean" || (note != null && (typeof note !== "string" || Array.from(note).length > MAX_NOTE_CHARS))) {
        return { status: 422, data: { detail: [{ loc: ["body", "note"], msg: "String should have at most 200 characters", type: "string_too_long" }] } };
      }
      if (row.status !== "pending") return { status: 409, data: { detail: "approval is not pending", status: row.status } };
      let cost = 0;
      if (approve && row.kind === "purchase_order") {
        if (!policy.is_set) return { status: 422, data: { detail: "Batas harian belum diatur; atur lewat Aturan lalu setujui lagi", status: "pending" } };
        if (policy.blocked_actions.includes("spend")) {
          return { status: 422, data: { detail: "Belanja diblokir di Aturan; buka blokirnya lewat Aturan lalu setujui lagi", status: "pending" } };
        }
        if (spent + row.amount_idr > policy.daily_limit_idr) {
          return { status: 422, data: { detail: `Melebihi batas harian ${rupiahText(policy.daily_limit_idr)}; naikkan batas lewat Aturan lalu setujui lagi`, status: "pending" } };
        }
        cost = row.amount_idr;
      }
      row.status = approve ? "approved" : "rejected";
      const agent = agents.get(String(row.agent_id));
      const message = approve ? (cost > 0 ? "Pesanan dibuat dan dicatat" : "Faktur disetujui") : (row.kind === "invoice" ? "Faktur dibatalkan" : "Pesanan dibatalkan");
      if (agent) {
        const reason = `Pemilik ${approve ? "menyetujui" : "menolak"}: ${row.subject}${row.amount_idr > 0 ? ` ${rupiahText(row.amount_idr)}` : ""}. ${message}${note ? `. Catatan: ${note}` : ""}`;
        const action = addAction(agent, { action: "Keputusan pemilik", reason, cost, status: approve ? "done" : "rejected" }, 0);
        spent += cost;
        emit("kantor:activity", { type: "activity", business_id: business.id, ...action });
      }
      const pending_count = pendingApprovals().length;
      emit("kantor:approval", { type: "approval_changed", approval_id: row.id, agent_id: row.agent_id, status: row.status, pending_count });
      return { status: 200, data: { id: row.id, status: row.status, message, cost_idr: cost, pending_count } };
    },

    /** Called every frame with the frame time; emits one new action every NEW_ACTION_EVERY_S seconds. */
    tick(dt) {
      if (!policy.is_set || business.paused) return;
      clock += dt;
      if (clock < NEW_ACTION_EVERY_S) return;
      clock = 0;
      const list = [...agents.values()].filter((a) => !a.paused);
      if (!list.length) return;
      const a = list[counter % list.length];
      const tpls = TEMPLATES[a.tpl] ?? TEMPLATES.Kasir;
      const tpl = tpls[Math.floor(counter / list.length) % tpls.length];
      counter++;
      if (tpl.status === "done" && spent + tpl.cost > policy.daily_limit_idr) return;
      const row = addAction(a, tpl, 0);
      if (tpl.status === "done") spent += tpl.cost;
      emit("kantor:activity", { type: "activity", business_id: business.id, ...row });
    },
  };
}
