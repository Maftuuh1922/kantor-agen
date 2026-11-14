// Agent panel (#side-panel): role, live state, current action, spending today, recent actions, pause button.
//
// Data comes from GET /api/agents/{id} (or the mock). It is reloaded when an `activity` event names this
// agent and every 10 seconds while the panel is open. The live parts (state, current action) are read from
// the behavior controller a few times a second, so they match what the scene shows.
// All server text is set with textContent.

import {
  actionStatusColor, actionStatusLabel, bubbleText, formatJakartaDateTime, formatJakartaTime,
  formatRupiah, stateColor, stateDetail,
} from "./ui-format.js";

const REFRESH_MS = 10000;
const LIVE_EVERY_S = 0.25;
const ACTIVITY_DEBOUNCE_MS = 300;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

/**
 * @param opts.root         the <aside id="side-panel"> element
 * @param opts.api          ui-api.js or ui-mock.js object
 * @param opts.controller   BehaviorController
 * @param opts.agentInfo    (id) => { name, role, paused } | undefined, the /api/office summary
 * @param opts.isOfficePaused  () => boolean
 * @param opts.onClose      (id) => void, after the panel closed (return focus here)
 * @param opts.onPauseChanged  () => void, after a pause or resume succeeded
 */
export function createPanel({ root, api, controller, agentInfo, isOfficePaused, onClose, onPauseChanged }) {
  // ---- structure (built once) ----
  const nameEl = el("h2", "panel-name");
  nameEl.tabIndex = -1;
  const roleEl = el("p", "panel-role");
  const closeBtn = el("button", "panel-close", "\u00d7");
  closeBtn.type = "button";
  closeBtn.setAttribute("aria-label", "Close agent panel");
  closeBtn.title = "Close (Esc)";
  const head = el("header", "panel-head");
  const headText = el("div", "panel-head-text");
  headText.append(nameEl, roleEl);
  head.append(headText, closeBtn);

  const marker = el("span", "marker marker-grey");
  marker.setAttribute("aria-hidden", "true");
  const stateEl = el("span", "panel-state-text");
  const stateRow = el("div", "panel-state");
  stateRow.append(marker, stateEl);
  const bubbleEl = el("p", "panel-bubble");

  const spendLabel = el("dt", null, "Spending today");
  const spendValue = el("dd", "panel-spend");
  const facts = el("dl", "panel-facts");
  facts.append(spendLabel, spendValue);

  const pauseBtn = el("button", "panel-btn", "Pause agent");
  pauseBtn.type = "button";
  const officeNote = el("p", "panel-note");
  const errorEl = el("p", "panel-error");
  errorEl.setAttribute("role", "alert");
  errorEl.hidden = true;

  const listTitle = el("h3", "panel-subtitle", "Recent actions");
  const listEl = el("ul", "action-list");
  listEl.setAttribute("aria-label", "Recent actions, newest first");
  const emptyEl = el("p", "panel-empty");

  root.replaceChildren(head, stateRow, bubbleEl, facts, pauseBtn, officeNote, errorEl, listTitle, listEl, emptyEl);

  // ---- state ----
  let openId = null;
  let data = null;          // last good answer from the backend
  let loading = false;
  let loadedOnce = false;
  let seq = 0;              // makes sure an old answer never overwrites a newer one
  let timer = null;
  let activityTimer = null;
  let liveClock = 0;
  let busy = false;         // pause request in flight

  const isOpen = () => openId !== null;

  function showError(message) {
    errorEl.textContent = message ?? "";
    errorEl.hidden = !message;
  }

  function currentInfo() {
    const info = agentInfo?.(openId) ?? {};
    return {
      name: data?.name ?? info.name ?? openId,
      role: data?.role ?? info.role ?? "",
      paused: data ? !!data.paused : !!info.paused,
    };
  }

  function renderStatic() {
    const info = currentInfo();
    nameEl.textContent = info.name;
    roleEl.textContent = info.role;
    root.setAttribute("aria-label", `Agent panel: ${info.name}`);
    spendValue.textContent = data ? formatRupiah(data.spent_today_idr) : "-";
    pauseBtn.textContent = info.paused ? "Resume agent" : "Pause agent";
    pauseBtn.setAttribute("aria-label", `${info.paused ? "Resume" : "Pause"} ${info.name}`);
    pauseBtn.disabled = busy || !data;
    officeNote.textContent = isOfficePaused?.() ? "The whole office is paused, so no agent starts new work." : "";
    officeNote.hidden = !officeNote.textContent;
    renderActions();
  }

  function renderLive() {
    const status = openId != null ? controller.getStatus(openId) : null;
    const state = status?.state ?? data?.state ?? "idle";
    const color = status ? controller.markerFor(openId) ?? stateColor(state) : stateColor(state);
    marker.className = `marker marker-${color}`;
    const detail = stateDetail(state, status?.activity);
    stateEl.textContent = detail;
    marker.title = detail;
    const text = bubbleText(status ? status.bubble_text : data?.bubble_text);
    bubbleEl.textContent = text ? `\u201c${text}\u201d` : "No current action";
    bubbleEl.classList.toggle("is-empty", !text);
    const full = String((status ? status.bubble_text : data?.bubble_text) ?? "").trim();
    if (full && full !== text) bubbleEl.title = full;
    else bubbleEl.removeAttribute("title");
  }

  function renderActions() {
    const rows = data?.recent_actions ?? [];
    const items = rows.map((r) => {
      const color = actionStatusColor(r.status);
      const li = el("li", `action-item status-${color}`);
      const top = el("div", "action-top");
      const time = el("span", "action-time", formatJakartaTime(r.created_at));
      const full = formatJakartaDateTime(r.created_at);
      if (full) time.title = full;
      const chip = el("span", `action-status chip-${color}`, actionStatusLabel(r.status));
      const cost = el("span", "action-cost", formatRupiah(r.cost_idr));
      top.append(time, chip, cost);
      li.append(top, el("div", "action-name", String(r.action ?? "")), el("div", "action-reason", String(r.reason ?? "")));
      return li;
    });
    listEl.replaceChildren(...items);
    if (items.length) emptyEl.textContent = "";
    else if (!loadedOnce) emptyEl.textContent = loading ? "Loading\u2026" : "";
    else emptyEl.textContent = "No actions yet today.";
    emptyEl.hidden = !emptyEl.textContent;
  }

  // ---- loading ----
  async function refresh() {
    if (openId == null) return;
    const id = openId;
    const mine = ++seq;
    loading = true;
    try {
      const result = await api.getAgent(id);
      if (mine !== seq || openId !== id) return;
      data = result;
      loadedOnce = true;
      if (!busy) showError("");
    } catch (err) {
      if (mine !== seq || openId !== id) return;
      showError(`Could not load the latest data. ${err.message}`);
      loadedOnce = true;
    } finally {
      if (mine === seq) loading = false;
    }
    if (mine === seq && openId === id) {
      renderStatic();
      renderLive();
    }
  }

  function open(id) {
    id = String(id);
    const changed = id !== openId;
    openId = id;
    if (changed) {
      data = null;
      loadedOnce = false;
      busy = false;
      showError("");
    }
    root.hidden = false;
    renderStatic();
    renderLive();
    clearInterval(timer);
    timer = setInterval(refresh, REFRESH_MS);
    refresh();
    if (changed || !root.contains(document.activeElement)) nameEl.focus({ preventScroll: true });
  }

  function close({ restoreFocus = true } = {}) {
    if (openId == null) return;
    const id = openId;
    const hadFocus = root.contains(document.activeElement);
    openId = null;
    seq++;
    clearInterval(timer);
    clearTimeout(activityTimer);
    timer = null;
    root.hidden = true;
    onClose?.(id, restoreFocus && hadFocus);
  }

  closeBtn.addEventListener("click", () => close());

  pauseBtn.addEventListener("click", async () => {
    if (openId == null || busy || !data) return;
    const id = openId;
    const want = !currentInfo().paused;
    busy = true;
    showError("");
    pauseBtn.disabled = true;
    try {
      const res = await api.pauseAgent(id, want);
      if (openId === id) data = { ...data, paused: !!res.paused };
      onPauseChanged?.(res);
    } catch (err) {
      if (openId === id) showError(`Could not ${want ? "pause" : "resume"} the agent. ${err.message}`);
    } finally {
      busy = false;
      if (openId === id) {
        renderStatic();
        renderLive();
        refresh();
      }
    }
  });

  // Escape closes the panel, unless a modal dialog is open (it handles Escape itself).
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || openId == null || e.defaultPrevented) return;
    if (document.body.classList.contains("modal-open")) return;
    e.preventDefault();
    close();
  });

  /** An `activity` event: reload when it is about the open agent. */
  function handleActivity(evt) {
    if (openId == null || !evt || String(evt.agent_id) !== openId) return;
    clearTimeout(activityTimer);
    activityTimer = setTimeout(refresh, ACTIVITY_DEBOUNCE_MS);
  }

  /** Call every frame with the frame time. */
  function update(dt) {
    if (openId == null) return;
    liveClock += dt;
    if (liveClock < LIVE_EVERY_S) return;
    liveClock = 0;
    renderLive();
  }

  /** The office summary changed (pause flags, names): redraw the static parts. */
  function sync() {
    if (openId != null) renderStatic();
  }

  root.hidden = true;
  return { open, close, refresh, handleActivity, update, sync, isOpen, get openId() { return openId; } };
}
