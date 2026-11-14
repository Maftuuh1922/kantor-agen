// Bootstrap for the office page: scene, characters, behavior controller, backend feed.
//
//   /                 live mode: GET /api/office, then SSE /api/events
//   /?mock=1          offline demo: no backend call, a scripted loop drives 4 mock agents
//   ?quality=low      low-quality mode (also saved from the HUD button)
//
// The backend decides what each agent does (visual_state); this file only passes the
// events to the BehaviorController (behavior.js), which turns them into motion.

import { createOffice } from "./office.js";
import { loadAssets, createCharacter } from "./characters.js";
import { BehaviorController } from "./behavior.js";

const QUALITY_KEY = "kantor-agen.quality";
const params = new URLSearchParams(location.search);
const MOCK = params.get("mock") === "1";

const API_OFFICE = "/api/office";
const API_EVENTS = "/api/events";
const RECONNECT_BASE_MS = 500;
const RECONNECT_MAX_MS = 5000;
const STALE_AFTER_MS = 10000;

function readQuality() {
  const fromUrl = params.get("quality");
  if (fromUrl === "low" || fromUrl === "normal") return fromUrl;
  try {
    return localStorage.getItem(QUALITY_KEY) === "low" ? "low" : "normal";
  } catch {
    return "normal";
  }
}

function showFatal(message) {
  const el = document.getElementById("fatal");
  el.textContent = message;
  el.hidden = false;
  document.getElementById("loading")?.classList.add("done");
}

// ---- backend feed ---------------------------------------------------------------------

/** Delay before reconnect attempt number `attempt` (0, 1, 2, ...): 0.5 s, 1 s, 2 s, 4 s, then 5 s. */
export function reconnectDelay(attempt) {
  return Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * 2 ** attempt);
}

// Server-Sent Events with our own reconnect (exponential backoff, capped at 5 s).
// The server sends a fresh `snapshot` on every connection, so nothing is replayed here.
function connectEvents({ onSnapshot, onVisualState, onActivity, onStatus }) {
  let source = null;
  let attempt = 0;
  let timer = null;
  let closed = false;

  const parse = (handler) => (ev) => {
    try {
      handler(JSON.parse(ev.data));
    } catch (err) {
      console.warn("bad event payload", err, ev.data);
    }
  };
  function schedule() {
    if (closed || timer) return;
    const delay = reconnectDelay(attempt++);
    onStatus?.("reconnecting", delay);
    timer = setTimeout(() => { timer = null; open(); }, delay);
  }
  function open() {
    if (closed) return;
    source = new EventSource(API_EVENTS);
    source.addEventListener("open", () => { attempt = 0; onStatus?.("open", 0); });
    source.addEventListener("snapshot", parse(onSnapshot));
    source.addEventListener("visual_state", parse(onVisualState));
    source.addEventListener("activity", parse(onActivity));
    source.addEventListener("error", () => {
      source?.close();     // the browser would retry on its own (3 s); we use our backoff instead
      source = null;
      schedule();
    });
  }
  open();
  return { close() { closed = true; clearTimeout(timer); source?.close(); } };
}

async function fetchOffice() {
  const res = await fetch(API_OFFICE, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`${API_OFFICE} answered ${res.status}`);
  return res.json();
}

// ---- mock mode ------------------------------------------------------------------------------

const MOCK_OFFICE = Object.freeze({
  business: { id: 1, name: "Kantor Warung (demo)", type: "warung", paused: false },
  agents: [
    { id: "mock-1", name: "Sari", role: "cashier", desk_index: 0, paused: false },
    { id: "mock-2", name: "Budi", role: "stock", desk_index: 1, paused: false },
    { id: "mock-3", name: "Wati", role: "finance", desk_index: 2, paused: false },
    { id: "mock-4", name: "Joko", role: "marketing", desk_index: 3, paused: false },
  ],
  policy: {},
  spent_today_idr: 0,
  demo_mode: true,
});

// One scripted loop (seconds). Covers working, handoff, waiting, idle and paused.
const MOCK_PERIOD = 60;
const MOCK_SCRIPT = Object.freeze([
  { t: 0, agent: "mock-1", state: "working", bubble: "Serving the counter" },
  { t: 0, agent: "mock-2", state: "working", bubble: "Counting stock" },
  { t: 0, agent: "mock-3", state: "idle" },
  { t: 0, agent: "mock-4", state: "paused" },
  { t: 10, agent: "mock-2", state: "handoff", target: "mock-1", bubble: "Rice is low, please reorder" },
  { t: 24, agent: "mock-2", state: "working", bubble: "Counting stock" },
  { t: 28, agent: "mock-3", state: "waiting", bubble: "Waiting for your approval" },
  { t: 42, agent: "mock-3", state: "idle" },
  { t: 42, agent: "mock-4", state: "idle" },
]);

// Feeds MOCK_SCRIPT to the controller as visual_state events, driven by the frame clock.
function createMockFeed(controller) {
  let time = 0;
  let next = 0;
  return {
    update(dt) {
      time += dt;
      while (true) {
        const e = MOCK_SCRIPT[next];
        const at = e ? e.t : Infinity;
        if (at > time) break;
        controller.applyVisualState({ agent_id: e.agent, state: e.state, target_agent_id: e.target ?? null, bubble_text: e.bubble ?? null });
        next++;
        if (next >= MOCK_SCRIPT.length) {
          next = 0;
          time -= MOCK_PERIOD;
          break;
        }
      }
    },
  };
}

// ---- start ---------------------------------------------------------------------------------

const canvas = document.getElementById("office-canvas");
const qualityBtn = document.getElementById("quality-btn");

async function main() {
  let office;
  try {
    office = createOffice(canvas, { quality: readQuality(), autoHideLoading: false });
  } catch (err) {
    console.error(err);
    showFatal("Could not start the 3D office. This browser needs WebGL.");
    throw err;
  }
  office.setLoadingProgress(0.1, "Loading characters");

  // Characters and the backend answer in parallel.
  let data;
  let assets;
  try {
    [data, assets] = await Promise.all([
      MOCK ? Promise.resolve(MOCK_OFFICE) : fetchOffice(),
      loadAssets(null, (p) => office.setLoadingProgress(0.1 + 0.85 * p.fraction, p.label)),
    ]);
  } catch (err) {
    console.error(err);
    showFatal(`Could not load the office: ${err.message ?? err}`);
    throw err;
  }

  const controller = new BehaviorController({ idleDelay: MOCK ? [5, 9] : undefined });
  const characters = new Map();
  const deskOf = new Map();    // agent id -> desk_index the character was created for
  const unusable = new Set();  // agents the backend lists without a usable desk: do not reload for them

  // One character per agent, standing at its desk (desk_index comes from /api/office).
  function addAgent(agent) {
    const id = String(agent.id);
    const index = agent.desk_index;
    if (!Number.isInteger(index) || index < 0 || index >= office.layout.maxAgents) {
      console.warn(`agent ${id} has no usable desk_index (${index}), skipped`);
      unusable.add(id);
      return false;
    }
    if (characters.has(id)) {
      if (deskOf.get(id) === index) return false;
      removeAgent(id);                       // the agent moved to another desk: start again there
    }
    unusable.delete(id);
    office.addAgentDesk(index);
    const character = createCharacter(assets, id, { quality: office.getQuality() });
    office.scene.add(character.object3D);
    characters.set(id, character);
    deskOf.set(id, index);
    controller.addAgent({ id, character, deskIndex: index });
    return true;
  }
  function removeAgent(id) {
    controller.removeAgent(id);
    characters.get(id)?.dispose();
    characters.delete(id);
    deskOf.delete(id);
  }

  // Make the scene match /api/office: add new agents, move agents whose desk changed, drop removed ones.
  function applyOffice(office_data) {
    const nameEl = document.getElementById("business-name");
    if (nameEl && office_data.business?.name) nameEl.textContent = office_data.business.name;
    const seen = new Set();
    for (const agent of office_data.agents ?? []) {
      seen.add(String(agent.id));
      const added = addAgent(agent);
      // A new character knows nothing yet: show pause now, the snapshot corrects it.
      if (added && (agent.paused || office_data.business?.paused)) {
        controller.applyVisualState({ agent_id: agent.id, state: "paused" });
      }
    }
    for (const id of [...characters.keys()]) if (!seen.has(id)) removeAgent(id);
  }
  applyOffice(data);

  // A snapshot that names an agent we do not know means the office changed: reload /api/office once.
  let resyncing = false;
  // `pending` are events for agents we did not know yet: they are applied once the agent exists,
  // otherwise a new agent would show idle until its next change.
  async function resync(pending = []) {
    if (MOCK || resyncing) return;
    resyncing = true;
    try {
      applyOffice(await fetchOffice());
      for (const e of pending) if (controller.has(e.agent_id)) controller.applyVisualState(e);
    } catch (err) {
      console.warn("could not reload the office", err);
    } finally {
      resyncing = false;
    }
  }
  function onSnapshot(snapshot) {
    controller.applySnapshot(snapshot);
    const unknown = (snapshot?.agents ?? []).filter((e) => !controller.has(e.agent_id) && !unusable.has(String(e.agent_id)));
    if (unknown.length) resync(unknown);
  }
  function onVisualState(e) {
    if (controller.applyVisualState(e)) return;
    if (e && e.agent_id != null && !controller.has(e.agent_id) && !unusable.has(String(e.agent_id))) resync([e]);
  }

  // If the backend is out of reach for a while the scene must not keep showing work it
  // cannot confirm (Req 2.8): after STALE_AFTER_MS without a connection everybody idles.
  // The snapshot sent on reconnect puts the real states back.
  let staleTimer = null;
  function onConnection(status) {
    document.body.dataset.connection = status;
    if (status === "open") {
      clearTimeout(staleTimer);
      staleTimer = null;
    } else if (!staleTimer) {
      staleTimer = setTimeout(() => {
        for (const id of controller.agentIds()) {
          const st = controller.getStatus(id)?.state;
          if (st === "working" || st === "handoff") controller.applyVisualState({ agent_id: id, state: "idle" });
        }
      }, STALE_AFTER_MS);
    }
  }

  const mockFeed = MOCK ? createMockFeed(controller) : null;
  let feed = null;
  if (MOCK) {
    document.body.dataset.connection = "mock";
  } else {
    feed = connectEvents({
      onSnapshot,
      onVisualState,
      // Task 8.7 (agent panel) listens for this.
      onActivity: (a) => window.dispatchEvent(new CustomEvent("kantor:activity", { detail: a })),
      onStatus: onConnection,
    });
  }

  // Order matters: backend script, behavior controller (sets poses), then the characters' own animation.
  function step(dt) {
    mockFeed?.update(Math.min(dt, 0.1));
    controller.update(dt);
    for (const c of characters.values()) c.update(dt);
  }
  office.onFrame(step);

  function syncQualityButton() {
    const low = office.getQuality() === "low";
    qualityBtn.textContent = `Quality: ${low ? "Low" : "Normal"}`;
    qualityBtn.setAttribute("aria-pressed", String(low));
  }
  qualityBtn.addEventListener("click", () => {
    const next = office.getQuality() === "low" ? "normal" : "low";
    office.setQuality(next);
    for (const c of characters.values()) c.setQuality(next);
    try {
      localStorage.setItem(QUALITY_KEY, next);
    } catch {
      /* storage may be blocked; the choice then lasts for this visit only */
    }
    syncQualityButton();
  });
  syncQualityButton();

  window.office = office; // handy for the console and for later modules
  // `step` lets tests fast-forward the simulation without rendering.
  window.kantor = { office, controller, characters, assets, data, feed, mock: MOCK, step };
  office.start();
  office.ready.then(() => office.hideLoading());
}

main().catch((err) => console.error(err));
