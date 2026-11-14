// In-memory stand-in for the backend, used by `?mock=1`. It has the same methods as ui-api.js.
//
//   ?mock=1                 policy already set, spending Rp 45.000 of Rp 100.000
//   ?mock=1&policy=unset    policy not set yet: the first-run policy dialog opens
//
// New actions arrive every few seconds through the same "kantor:activity" window event that
// the live SSE feed uses, so the panel and the feed run the same code in both modes.

const DAILY_LIMIT = 100000;
const NEW_ACTION_EVERY_S = 12;

const TEMPLATES = Object.freeze({
  cashier: [
    { action: "Record sale: 3 items", reason: "Customer paid cash at the counter", cost: 0, status: "done" },
    { action: "Close the till for lunch", reason: "Counted cash and matched it with sales", cost: 0, status: "done" },
  ],
  stock: [
    { action: "Reorder rice, 10 kg", reason: "Stock fell below the minimum of 5 kg", cost: 45000, status: "done" },
    { action: "Reorder cooking oil, 12 liters", reason: "Daily spending limit would be exceeded", cost: 80000, status: "rejected" },
  ],
  finance: [
    { action: "Prepare the daily summary", reason: "End of day report for the owner", cost: 0, status: "done" },
    { action: "Approve supplier payment", reason: "Waiting for the owner to confirm", cost: 0, status: "pending" },
  ],
  marketing: [
    { action: "Draft a promo message", reason: "Slow sales on Tuesday afternoon", cost: 0, status: "pending" },
    { action: "Check the promo budget", reason: "No budget set for this week", cost: 0, status: "info" },
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
    agents.set(String(a.id), { ...a, state: "idle", bubble_text: "", spent: 0, actions: [] });
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
    const list = TEMPLATES[a.role] ?? TEMPLATES.cashier;
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

    async putPolicy(body) {
      await sleep();
      if (!Number.isInteger(body?.daily_limit_idr) || body.daily_limit_idr < 0) fail("daily_limit_idr must be a whole number, 0 or more");
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
      const tpls = TEMPLATES[a.role] ?? TEMPLATES.cashier;
      const tpl = tpls[Math.floor(counter / list.length) % tpls.length];
      counter++;
      if (tpl.status === "done" && spent + tpl.cost > policy.daily_limit_idr) return;
      const row = addAction(a, tpl, 0);
      if (tpl.status === "done") spent += tpl.cost;
      window.dispatchEvent(new CustomEvent("kantor:activity", { detail: { type: "activity", business_id: business.id, ...row } }));
    },
  };
}
