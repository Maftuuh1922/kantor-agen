// In-memory stand-in for the backend, used by `?mock=1`. It has the same methods as ui-api.js.
//
//   ?mock=1                 policy already set, spending Rp 92.000 of Rp 100.000
//   ?mock=1&policy=unset    policy not set yet: the first-run policy dialog opens
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
      window.dispatchEvent(new CustomEvent("kantor:activity", { detail: { type: "activity", business_id: business.id, ...row } }));
    },
  };
}
