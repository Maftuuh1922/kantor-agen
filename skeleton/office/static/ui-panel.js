// Agent panel (#side-panel): role, live state, current action, spending today, recent actions, pause button,
// and the "Ubah nama" control that renames the agent and changes its role label (Requirement 1.4).
//
// Data comes from GET /api/agents/{id} (or the mock). It is reloaded when an `activity` event names this
// agent and every 10 seconds while the panel is open. The live parts (state, current action) are read from
// the behavior controller a few times a second, so they match what the scene shows.
// All server text is set with textContent.

import {
  MAX_NAME_CHARS, actionStatusColor, actionStatusLabel, bubbleText, formatJakartaDateTime, formatJakartaTime,
  formatRupiah, renameChanges, stateColor, stateDetail, validateRename,
} from "./ui-format.js";
import { TEXT, fmt } from "./ui-text.js";

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
 * @param opts.onRenamed    (agent) => void, after the agent was renamed (reload /api/office so every label follows)
 * @param opts.approvals    ui-approvals.js object: this agent's requests that wait for the owner are listed with Setujui / Tolak
 */
export function createPanel({ root, api, controller, agentInfo, isOfficePaused, onClose, onPauseChanged, onRenamed, approvals }) {
  // ---- structure (built once) ----
  const nameEl = el("h2", "panel-name");
  nameEl.tabIndex = -1;
  const roleEl = el("p", "panel-role");
  const closeBtn = el("button", "panel-close", "\u00d7");
  closeBtn.type = "button";
  closeBtn.setAttribute("aria-label", TEXT.panel.close);
  closeBtn.title = TEXT.panel.closeTitle;
  const head = el("header", "panel-head");
  const headText = el("div", "panel-head-text");
  headText.append(nameEl, roleEl);
  head.append(headText, closeBtn);

  // "Ubah nama": a link-style button that opens a small form (name, role label, Simpan / Batal).
  const renameBtn = el("button", "panel-link", TEXT.rename.open);
  renameBtn.type = "button";
  renameBtn.setAttribute("aria-expanded", "false");
  renameBtn.setAttribute("aria-controls", "rename-form");

  const renameForm = el("form", "rename-form");
  renameForm.id = "rename-form";
  renameForm.noValidate = true;
  renameForm.hidden = true;
  renameForm.setAttribute("aria-label", TEXT.rename.title);
  function renameField(id, label, help) {
    const wrap = el("div", "field");
    const lab = el("label", "field-label", label);
    lab.htmlFor = id;
    const input = el("input");
    input.type = "text";
    input.id = id;
    input.maxLength = MAX_NAME_CHARS;
    input.autocomplete = "off";
    input.spellcheck = false;
    const err = el("p", "field-error");
    err.id = `${id}-error`;
    err.setAttribute("role", "alert");
    err.hidden = true;
    const described = [];
    wrap.append(lab, input);
    if (help) {
      const note = el("p", "field-help", help);
      note.id = `${id}-help`;
      wrap.append(note);
      described.push(note.id);
    }
    described.push(err.id);
    input.setAttribute("aria-describedby", described.join(" "));
    wrap.append(err);
    return { wrap, input, err };
  }
  const nameField = renameField("rename-name", TEXT.rename.name);
  const roleField = renameField("rename-role", TEXT.rename.role, TEXT.rename.roleHelp);
  const renameError = el("p", "field-error");
  renameError.setAttribute("role", "alert");
  renameError.hidden = true;
  const renameSave = el("button", "btn-primary", TEXT.rename.save);
  renameSave.type = "submit";
  const renameCancel = el("button", "hud-btn", TEXT.rename.cancel);
  renameCancel.type = "button";
  const renameActions = el("div", "rename-actions");
  renameActions.append(renameCancel, renameSave);
  renameForm.append(nameField.wrap, roleField.wrap, renameError, renameActions);

  const marker = el("span", "marker marker-grey");
  marker.setAttribute("aria-hidden", "true");
  const stateEl = el("span", "panel-state-text");
  const stateRow = el("div", "panel-state");
  stateRow.append(marker, stateEl);
  const bubbleEl = el("p", "panel-bubble");

  const spendLabel = el("dt", null, TEXT.panel.spendToday);
  const spendValue = el("dd", "panel-spend");
  const facts = el("dl", "panel-facts");
  facts.append(spendLabel, spendValue);

  const pauseBtn = el("button", "panel-btn", TEXT.panel.pause);
  pauseBtn.type = "button";
  const officeNote = el("p", "panel-note");
  const errorEl = el("p", "panel-error");
  errorEl.setAttribute("role", "alert");
  errorEl.hidden = true;

  const listTitle = el("h3", "panel-subtitle", TEXT.panel.recent);
  const listEl = el("ul", "action-list");
  listEl.setAttribute("aria-label", TEXT.panel.recentLabel);
  const emptyEl = el("p", "panel-empty");

  // Requests of this agent that wait for the owner (task 14.2): the same list as the queue, filtered on this agent.
  const approvalsBox = el("div", "panel-approvals");
  const approvalList = approvals?.mountList(approvalsBox, {
    title: TEXT.approvals.agentTitle,
    focusFallback: () => nameEl.focus({ preventScroll: true }),
  });

  root.replaceChildren(head, renameBtn, renameForm, stateRow, bubbleEl, approvalsBox, facts, pauseBtn, officeNote, errorEl, listTitle, listEl, emptyEl);

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
  let renaming = false;     // the rename form is open
  let renameSaving = false; // PATCH in flight

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
    root.setAttribute("aria-label", fmt(TEXT.panel.ariaFor, { name: info.name }));
    renameBtn.setAttribute("aria-label", fmt(TEXT.rename.openLabel, { name: info.name }));
    spendValue.textContent = data ? formatRupiah(data.spent_today_idr) : "-";
    pauseBtn.textContent = info.paused ? TEXT.panel.resume : TEXT.panel.pause;
    pauseBtn.setAttribute("aria-label", fmt(info.paused ? TEXT.panel.resumeFor : TEXT.panel.pauseFor, { name: info.name }));
    pauseBtn.disabled = busy || !data;
    officeNote.textContent = isOfficePaused?.() ? TEXT.panel.officePaused : "";
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
    bubbleEl.textContent = text ? `\u201c${text}\u201d` : TEXT.panel.noAction;
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
    else if (!loadedOnce) emptyEl.textContent = loading ? TEXT.panel.loading : "";
    else emptyEl.textContent = TEXT.panel.empty;
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
      showError(fmt(TEXT.panel.loadFailed, { error: err.message }));
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
      closeRename({ restoreFocus: false });
      showError("");
    }
    root.hidden = false;
    approvalList?.show(id);
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
    closeRename({ restoreFocus: false });
    seq++;
    clearInterval(timer);
    clearTimeout(activityTimer);
    timer = null;
    root.hidden = true;
    approvalList?.hide();
    onClose?.(id, restoreFocus && hadFocus);
  }

  closeBtn.addEventListener("click", () => close());

  // ---- rename (Requirement 1.4) ----
  function setFieldError(field, message) {
    field.err.textContent = message ?? "";
    field.err.hidden = !message;
    if (message) field.input.setAttribute("aria-invalid", "true");
    else field.input.removeAttribute("aria-invalid");
  }
  function setRenameError(message) {
    renameError.textContent = message ?? "";
    renameError.hidden = !message;
  }
  function setRenameBusy(on) {
    renameSaving = on;
    nameField.input.disabled = on;
    roleField.input.disabled = on;
    renameSave.disabled = on;
    renameCancel.disabled = on;
    renameSave.textContent = on ? TEXT.rename.saving : TEXT.rename.save;
  }
  function openRename() {
    if (openId == null || renaming) return;
    const info = currentInfo();
    renaming = true;
    nameField.input.value = String(info.name ?? "");
    roleField.input.value = String(info.role ?? "");
    setFieldError(nameField, "");
    setFieldError(roleField, "");
    setRenameError("");
    setRenameBusy(false);
    renameForm.hidden = false;
    renameBtn.hidden = true;
    renameBtn.setAttribute("aria-expanded", "true");
    nameField.input.focus({ preventScroll: true });
    nameField.input.select();
  }
  /** Hide the form. `restoreFocus`: put the focus back on the "Ubah nama" button (cancel, save, Escape). */
  function closeRename({ restoreFocus = true } = {}) {
    if (!renaming) return;
    renaming = false;
    renameSaving = false;
    renameForm.hidden = true;
    renameBtn.hidden = false;
    renameBtn.setAttribute("aria-expanded", "false");
    if (restoreFocus) renameBtn.focus({ preventScroll: true });
  }
  renameBtn.addEventListener("click", openRename);
  renameCancel.addEventListener("click", () => { if (!renameSaving) closeRename(); });
  // Escape cancels the form and nothing more: preventDefault tells the panel's own Escape handler to stay open.
  renameForm.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    e.preventDefault();
    e.stopPropagation();
    if (!renameSaving) closeRename();
  });
  for (const field of [nameField, roleField]) {
    field.input.addEventListener("input", () => { if (!field.err.hidden) setFieldError(field, ""); });
  }
  renameForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (openId == null || renameSaving) return;
    const id = openId;
    setRenameError("");
    const result = validateRename(nameField.input.value, roleField.input.value);
    setFieldError(nameField, result.errors.name);
    setFieldError(roleField, result.errors.role);
    if (!result.ok) {
      (result.errors.name ? nameField : roleField).input.focus();
      return;
    }
    const changes = renameChanges(currentInfo(), result.value);
    if (Object.keys(changes).length === 0) {
      closeRename(); // nothing to save
      return;
    }
    setRenameBusy(true);
    try {
      const agent = await api.patchAgent(id, changes);
      if (openId !== id) return;
      data = data ? { ...data, name: agent.name, role: agent.role } : data;
      closeRename();
      renderStatic();
      renderLive();
      onRenamed?.(agent);
      refresh();
    } catch (err) {
      if (openId !== id || !renaming) return;
      setRenameBusy(false);
      setRenameError(fmt(TEXT.rename.saveFailed, { error: err.message }));
    }
  });

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
      if (openId === id) showError(fmt(want ? TEXT.panel.pauseFailed : TEXT.panel.resumeFailed, { error: err.message }));
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
