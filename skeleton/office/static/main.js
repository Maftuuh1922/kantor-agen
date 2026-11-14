// Bootstrap for the office page: scene, characters, behavior controller, backend feed.
//
//   /                 live mode: GET /api/office, then SSE /api/events
//   /?mock=1          offline demo: no backend call, a scripted loop drives 4 mock agents
//   /?mock=1&policy=unset   same, but the policy is not set yet: the first-run policy dialog opens
//   /?mock=1&onboarding=1   same, but from the very first screen: name and business type, then the policy dialog
//   ?mock=1&agents=8        mock office with N agents (1 to 8, default 4), for performance checks
//
// First run on the live backend: GET /api/office answers { onboarding_required: true, business_types },
// so the first-run screen (ui-onboarding.js) opens, then the policy dialog (Requirement 1.3), then the office.
//   ?quality=low      low-quality mode (also saved from the HUD button)
//   ?perf=1           performance probe: overlay plus window.__perf.snapshot() (perf.js)
//
// The backend decides what each agent does (visual_state); this file only passes the
// events to the BehaviorController (behavior.js), which turns them into motion.

import { createOffice } from "./office.js";
import { loadAssets, createCharacter } from "./characters.js";
import { BehaviorController } from "./behavior.js";
import { createApi } from "./ui-api.js";
import { createMockApi } from "./ui-mock.js";
import { createOverlay } from "./ui-overlay.js";
import { createPanel } from "./ui-panel.js";
import { createHud } from "./ui-hud.js";
import { createPolicyDialog } from "./ui-policy.js";
import { createOnboarding, createOnboardingApi, createMockOnboardingApi } from "./ui-onboarding.js";
import { createFeed } from "./ui-feed.js";
import { startPerfProbe, createSlowFrameMonitor } from "./perf.js";

const QUALITY_KEY = "kantor-agen.quality";
const params = new URLSearchParams(location.search);
const MOCK = params.get("mock") === "1";
const MOCK_ONBOARDING = MOCK && params.get("onboarding") === "1";

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

// ---- mock mode ------------------------------------------------------------------------------

const MOCK_AGENT_ROWS = Object.freeze([
  { name: "Sari", role: "cashier" }, { name: "Budi", role: "stock" },
  { name: "Wati", role: "finance" }, { name: "Joko", role: "marketing" },
  { name: "Rina", role: "cashier" }, { name: "Agus", role: "stock" },
  { name: "Dewi", role: "finance" }, { name: "Hadi", role: "marketing" },
]);

/** Number of mock agents from `?agents=N`: a whole number from 1 to 8, default 4. */
export function mockAgentCount(search) {
  const n = Number(new URLSearchParams(search).get("agents"));
  return Number.isInteger(n) && n >= 1 && n <= MOCK_AGENT_ROWS.length ? n : 4;
}

const MOCK_OFFICE = Object.freeze({
  business: { id: 1, name: "Kantor Warung (demo)", type: "warung", paused: false },
  agents: MOCK_AGENT_ROWS.slice(0, mockAgentCount(location.search)).map((a, i) => (
    { id: `mock-${i + 1}`, name: a.name, role: a.role, desk_index: i, paused: false }
  )),
  policy: {},   // the mock backend (ui-mock.js) supplies the policy and the spending
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
  // Agents 5 to 8 only exist with ?agents=5 and up.
  { t: 0, agent: "mock-5", state: "working", bubble: "Serving the counter" },
  { t: 0, agent: "mock-6", state: "working", bubble: "Counting stock" },
  { t: 0, agent: "mock-7", state: "idle" },
  { t: 0, agent: "mock-8", state: "working", bubble: "Drafting a promo" },
  { t: 10, agent: "mock-2", state: "handoff", target: "mock-1", bubble: "Rice is low, please reorder" },
  { t: 16, agent: "mock-6", state: "idle" },
  { t: 24, agent: "mock-2", state: "working", bubble: "Counting stock" },
  { t: 28, agent: "mock-3", state: "waiting", bubble: "Waiting for your approval" },
  { t: 34, agent: "mock-7", state: "working", bubble: "Preparing the summary" },
  { t: 42, agent: "mock-3", state: "idle" },
  { t: 42, agent: "mock-4", state: "idle" },
  { t: 50, agent: "mock-8", state: "idle" },
]);

// Feeds MOCK_SCRIPT to the controller as visual_state events, driven by the frame clock.
// `skip(agentId)` is true while the owner has paused that agent (or the office): the script then leaves it alone.
function createMockFeed(controller, skip = () => false) {
  let time = 0;
  let next = 0;
  return {
    update(dt) {
      time += dt;
      while (true) {
        const e = MOCK_SCRIPT[next];
        const at = e ? e.t : Infinity;
        if (at > time) break;
        if (!skip(e.agent)) controller.applyVisualState({ agent_id: e.agent, state: e.state, target_agent_id: e.target ?? null, bubble_text: e.bubble ?? null });
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
  // ?perf=1 only: null otherwise, and then nothing below costs anything.
  const perf = startPerfProbe(office, { getExtra: () => ({ agents: window.kantor?.characters?.size ?? 0, mock: MOCK }) });
  office.setLoadingProgress(0.1, "Loading characters");

  // The backend (or, with ?mock=1, an in-memory stand-in with the same methods).
  let controller = null; // set below; the mock backend uses it to move the characters when the owner pauses
  const makeMockApi = (officeData, policySet) => createMockApi(officeData, {
    policySet,
    applyVisualState: (e) => controller?.applyVisualState(e),
  });
  // With ?mock=1&onboarding=1 the mock office does not exist yet: it is created by the first-run screen.
  let api = MOCK_ONBOARDING ? null : MOCK ? makeMockApi(MOCK_OFFICE, params.get("policy") !== "unset") : createApi();

  // Characters and the backend answer in parallel.
  let data;
  let assets;
  let assetsLoaded = false;
  try {
    const assetsPromise = loadAssets(null, (p) => office.setLoadingProgress(0.1 + 0.85 * p.fraction, p.label))
      .then((loaded) => { assetsLoaded = true; return loaded; });
    assetsPromise.catch(() => {}); // a failure is reported where the promise is awaited below
    const first = MOCK_ONBOARDING ? createMockOnboardingApi() : null;
    data = first ? first.firstOffice() : await api.getOffice();
    if (data?.onboarding_required) {
      // First run (Requirements 1.1, 1.2): show the form at once, the characters keep loading behind it.
      office.hideLoading();
      data = await createOnboarding({ api: first ?? createOnboardingApi() }).run(data);
      if (MOCK_ONBOARDING) api = makeMockApi(data, false);
      if (!assetsLoaded) { // still loading: show the bar again
        const bar = document.getElementById("loading");
        bar?.classList.remove("done");
        bar?.removeAttribute("aria-hidden");
      }
    }
    assets = await assetsPromise;
  } catch (err) {
    console.error(err);
    showFatal(`Could not load the office: ${err.message ?? err}`);
    throw err;
  }

  perf?.mark("assetsLoaded");
  controller = new BehaviorController({ idleDelay: MOCK ? [5, 9] : undefined });
  const characters = new Map();
  const agentInfo = new Map(); // agent id -> { name, role, paused }, from /api/office (used by the UI layer)
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
    const character = createCharacter(assets, id, { quality: office.getQuality(), name: agent.name });
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
    const seen = new Set();
    agentInfo.clear();
    for (const agent of office_data.agents ?? []) {
      seen.add(String(agent.id));
      agentInfo.set(String(agent.id), { name: agent.name, role: agent.role, paused: !!agent.paused });
      const added = addAgent(agent);
      // A new character knows nothing yet: show pause now, the snapshot corrects it.
      if (added && (agent.paused || office_data.business?.paused)) {
        controller.applyVisualState({ agent_id: agent.id, state: "paused" });
      }
    }
    for (const id of [...characters.keys()]) if (!seen.has(id)) removeAgent(id);
  }

  // ---- UI layer: top bar, policy dialog, agent panel, bubbles, feed (ui-*.js) ----
  const lookup = (id) => agentInfo.get(String(id));
  const hud = createHud({
    api,
    onOffice: (d) => { applyOffice(d); panel.sync(); activityFeed.render(); },
    onPauseChanged: () => panel.sync(),
  });
  const overlay = createOverlay({
    container: document.getElementById("overlay"),
    office,
    controller,
    characters,
    agentInfo: lookup,
    topBar: document.getElementById("hud"),
    onSelect: (id) => { panel.open(id); overlay.setSelected(id); },
  });
  const panel = createPanel({
    root: document.getElementById("side-panel"),
    api,
    controller,
    agentInfo: lookup,
    isOfficePaused: () => hud.isPaused(),
    onClose: (id, restoreFocus) => {
      overlay.setSelected(null);
      if (restoreFocus) overlay.focusLabel(id);
    },
    onPauseChanged: () => hud.refresh(),
  });
  const activityFeed = createFeed({ root: document.getElementById("activity-feed"), api, agentInfo: lookup });
  const policyDialog = createPolicyDialog({ api, onSaved: (policy) => hud.setPolicy(policy) });
  window.addEventListener("kantor:activity", (e) => {
    activityFeed.push(e.detail);
    panel.handleActivity(e.detail);
    hud.refreshSoon();
  });

  perf?.mark("agentsCreated");
  hud.apply(data);
  activityFeed.load();
  const needsPolicy = data.policy?.is_set === false; // Requirement 1.3: ask before anything can spend

  // A snapshot that names an agent we do not know means the office changed: reload /api/office once.
  let resyncing = false;
  // `pending` are events for agents we did not know yet: they are applied once the agent exists,
  // otherwise a new agent would show idle until its next change.
  async function resync(pending = []) {
    if (MOCK || resyncing) return;
    resyncing = true;
    try {
      hud.apply(await api.getOffice());
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

  const mockFeed = MOCK
    ? createMockFeed(controller, (id) => hud.isPaused() || !!lookup(id)?.paused)
    : null;
  let feed = null;
  if (MOCK) {
    document.body.dataset.connection = "mock";
  } else {
    feed = connectEvents({
      onSnapshot,
      onVisualState,
      // The feed, the agent panel and the top bar listen for this.
      onActivity: (a) => window.dispatchEvent(new CustomEvent("kantor:activity", { detail: a })),
      onStatus: onConnection,
    });
  }

  // Order matters: backend script, behavior controller (sets poses), then the characters' own animation.
  function step(dt) {
    mockFeed?.update(Math.min(dt, 0.1));
    if (MOCK && api.tick) api.tick(Math.min(dt, 0.1));
    controller.update(dt);
    for (const c of characters.values()) c.update(dt);
  }
  office.onFrame(step);
  // After the characters moved: put bubbles, markers and labels on their heads.
  office.onFrame((dt) => {
    overlay.update(dt);
    panel.update(dt);
  });

  function syncQualityButton() {
    const low = office.getQuality() === "low";
    qualityBtn.textContent = `Quality: ${low ? "Low" : "Normal"}`;
    qualityBtn.setAttribute("aria-pressed", String(low));
  }
  function applyQuality(next) {
    office.setQuality(next);
    for (const c of characters.values()) c.setQuality(next);
    try {
      localStorage.setItem(QUALITY_KEY, next);
    } catch {
      /* storage may be blocked; the choice then lasts for this visit only */
    }
    syncQualityButton();
    document.getElementById("quality-hint").hidden = true; // the choice is made, whichever button made it
    perf?.reset();
  }
  qualityBtn.addEventListener("click", () => applyQuality(office.getQuality() === "low" ? "normal" : "low"));
  syncQualityButton();

  // Slow frames for a few seconds in normal mode: offer the low-quality mode. Never switches by itself.
  const qualityHint = document.getElementById("quality-hint");
  const slowMonitor = createSlowFrameMonitor();
  const hideHint = () => { qualityHint.hidden = true; };
  document.getElementById("quality-hint-yes").addEventListener("click", () => { hideHint(); applyQuality("low"); });
  document.getElementById("quality-hint-no").addEventListener("click", hideHint);
  // Real time between frames (the `dt` the office passes is capped at 0.1 s). A gap over 5 s is a
  // pause (hidden tab, sleeping laptop), not a slow frame.
  let lastFrameAt = 0;
  office.onFrame(() => {
    const t = performance.now();
    const ms = lastFrameAt ? t - lastFrameAt : 0;
    lastFrameAt = t;
    if (slowMonitor.fired || office.getQuality() === "low" || ms > 5000) return;
    if (slowMonitor.push(ms)) qualityHint.hidden = false;
  });

  window.office = office; // handy for the console and for later modules
  // `step` lets tests fast-forward the simulation without rendering.
  window.kantor = {
    office, controller, characters, assets, data, feed, mock: MOCK, step,
    ui: { api, hud, overlay, panel, feed: activityFeed, policy: policyDialog },
  };
  office.start();
  office.ready.then(() => {
    office.hideLoading();
    if (needsPolicy) policyDialog.show();
  });
}

main().catch((err) => console.error(err));
