// The owner's approval queue (task 14.2, Requirements 5.2, 7.1, 8.2).
//
//   * a badge in the top bar: "Menunggu keputusan (N)", muted at 0, amber above 0 (#approvals-btn)
//   * the queue panel "Antrian persetujuan" (#approvals-panel), opened by the badge
//   * the same request list inside the agent panel (mountList), filtered on that agent
//
// One store feeds every list: the requests (GET /api/approvals, oldest first), the count, and the state of each
// request (decision in flight, inline message, note text). The words are in ui-text.js, the pure rules (message for
// each answer, item text, badge text) in ui-approvals-logic.js. Everything from the server goes in with textContent.
//
// A decision never changes the amber marker or the bubble: the backend sets agent state "waiting" while a request is
// pending, and an `approval_changed` event tells us to reload.

import {
  MAX_NOTE_CHARS, POLL_MS, badgeState, badgeText, interpretDecision, interpretDecisionFailure, itemView, noteForRequest,
  normalizeApprovals, normalizeCount, rowsForAgent, sortRows,
} from "./ui-approvals-logic.js";
import { errorMessage } from "./ui-format.js";
import { TEXT, fmt } from "./ui-text.js";

const CONFIRM_MS = 6000;
const EVENT_DEBOUNCE_MS = 120;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}
function setText(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

let listUid = 0;

/**
 * @param opts.api        ui-api.js or ui-mock.js object (getApprovals, decideApproval)
 * @param opts.openRules  () => void, opens the policy dialog ("Buka Aturan")
 * @param opts.onDecided  (outcome) => void, after a decision was applied (the top bar reloads the spending)
 * @param opts.onOpen     () => void, the queue panel was opened
 * @param opts.onClose    () => void, the queue panel was closed
 * @param opts.now        () => ms, the clock (tests)
 */
export function createApprovals({ api, openRules, onDecided, onOpen, onClose, now = () => Date.now() }) {
  const $ = (id) => document.getElementById(id);
  const badge = $("approvals-btn");
  const badgeLabel = $("approvals-btn-text");
  const live = $("approvals-live");
  const root = $("approvals-panel");

  // ---- store ----
  let rows = [];                // pending requests, oldest first
  let count = 0;                // what the badge shows
  let countKnown = false;       // false until the first count: no announcement for the first load
  let loadedOnce = false;
  let loading = false;
  let loadError = "";
  let changeSeq = 0;            // bumped by every applied decision: a list that was requested before it is thrown away
  let confirmation = "";
  let confirmTimer = null;
  let eventTimer = null;
  let pollTimer = null;
  let retainers = 0;
  const states = new Map();     // approval id -> { busy, message, tone, offerRules, noteOpen, note, closed, row, ... }
  const lists = new Set();
  let focusedItemId = null;     // the request whose button had the focus while its decision is sent

  const stateOf = (id) => {
    let st = states.get(id);
    if (!st) {
      st = { busy: null, pressed: null, refocus: false, message: "", tone: "error", offerRules: false, noteOpen: false, note: "", closed: false, row: null };
      states.set(id, st);
    }
    return st;
  };

  function announce(text) {
    if (live) live.textContent = text;
  }

  // ---- badge ----
  function renderBadge() {
    const text = badgeText(count);
    setText(badgeLabel ?? badge, text);
    badge.dataset.state = badgeState(count);
    badge.title = TEXT.approvals.badgeTitle;
    badge.setAttribute("aria-expanded", String(isOpen()));
  }
  function setCount(value) {
    const next = normalizeCount(value);
    const rose = countKnown && next > count;
    count = next;
    countKnown = true;
    renderBadge();
    if (rose) announce(fmt(TEXT.approvals.badgeAnnounce, { count: next }));
  }

  // ---- loading ----
  // One request at a time. A call while one is running asks for one more round afterwards.
  let reloading = null;
  let reloadAgain = false;
  async function loadOnce() {
    const startedAt = changeSeq;
    try {
      const list = await api.getApprovals();
      if (startedAt !== changeSeq) { reloadAgain = true; return; } // a decision landed meanwhile: ask again
      rows = normalizeApprovals(list);
      loadedOnce = true;
      loadError = "";
      // A request that is gone from the server no longer needs its inline state, except a closed note.
      const ids = new Set(rows.map((r) => r.id));
      for (const [id, st] of states) if (!ids.has(id) && !st.closed) states.delete(id);
      setCount(rows.length);
    } catch (err) {
      loadedOnce = true;
      loadError = fmt(TEXT.approvals.loadFailed, { error: errorMessage(err?.message, TEXT.errors.unreachable) });
    }
  }
  function reload() {
    if (reloading) { reloadAgain = true; return reloading; }
    loading = true;
    renderAll();
    reloading = (async () => {
      try {
        do {
          reloadAgain = false;
          await loadOnce();
        } while (reloadAgain);
      } finally {
        loading = false;
        reloading = null;
        renderAll();
      }
    })();
    return reloading;
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(reload, POLL_MS);
  }
  function stopPolling() {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  /** A visible list wants fresh data: poll every POLL_MS while at least one is shown. Returns the release function. */
  function retain() {
    retainers++;
    startPolling();
    let released = false;
    return () => {
      if (released) return;
      released = true;
      if (--retainers <= 0) { retainers = 0; stopPolling(); }
    };
  }

  /** An `approval_changed` event from the stream (or the mock): update the count at once, reload the list. */
  function handleEvent(evt) {
    if (!evt || typeof evt !== "object") return;
    if (Number.isSafeInteger(evt.pending_count) && evt.pending_count >= 0) setCount(evt.pending_count);
    clearTimeout(eventTimer);
    eventTimer = setTimeout(reload, EVENT_DEBOUNCE_MS);
  }

  // ---- decisions ----
  function showConfirmation(text) {
    confirmation = text;
    clearTimeout(confirmTimer);
    confirmTimer = setTimeout(() => { confirmation = ""; renderAll(); }, CONFIRM_MS);
  }

  async function decide(row, approve) {
    const st = stateOf(row.id);
    if (st.busy) return;
    const focused = document.activeElement?.closest?.(".approval-item");
    const hadFocus = !!focused && focused.dataset.approvalId === String(row.id);
    focusedItemId = hadFocus ? row.id : null;
    st.busy = approve ? "approve" : "reject";
    st.pressed = st.busy;
    st.message = "";
    st.offerRules = false;
    renderAll();
    let outcome;
    try {
      outcome = interpretDecision(approve, row, await api.decideApproval(row.id, approve, noteForRequest(st.note)));
    } catch (err) {
      outcome = interpretDecisionFailure(err);
    }
    st.busy = null;
    changeSeq++;
    if (outcome.kind === "decided") {
      rows = rows.filter((r) => r.id !== row.id);
      states.delete(row.id);
      setCount(outcome.pendingCount ?? rows.length);
      showConfirmation(outcome.message);
      announce(outcome.message);
    } else if (outcome.kind === "stale") {
      st.closed = true;
      st.row = row;
      st.message = outcome.message;
      st.tone = outcome.tone;
      st.offerRules = false;
      rows = rows.filter((r) => r.id !== row.id);
      if (outcome.pendingCount != null) setCount(outcome.pendingCount);
    } else {
      st.message = outcome.message;
      st.tone = outcome.tone;
      st.offerRules = outcome.offerRules;
    }
    st.refocus = hadFocus && outcome.kind !== "decided";
    renderAll();
    focusedItemId = null;
    onDecided?.(outcome);
    if (outcome.refresh) reload();
  }

  /** The owner saved new rules: a request that was refused because of them can be tried again. */
  function rulesSaved() {
    let any = false;
    const focused = document.activeElement?.closest?.(".approval-item")?.dataset.approvalId;
    for (const [id, st] of states) {
      if (!st.offerRules) continue;
      // The dialog gave the focus back to "Buka Aturan", which goes away now: "Setujui" of the same request takes it.
      if (focused === String(id)) { st.refocus = true; st.pressed = "approve"; }
      st.offerRules = false;
      st.message = TEXT.approvals.rulesSaved;
      st.tone = "info";
      any = true;
    }
    if (any) renderAll();
  }

  function dismiss(id) {
    states.delete(id);
    renderAll();
  }

  // ---- one list (queue panel or agent panel) ----
  function visibleEntries(agentId) {
    const closed = [...states.values()].filter((st) => st.closed && st.row && !rows.some((r) => r.id === st.row.id)).map((st) => st.row);
    return rowsForAgent(sortRows([...rows, ...closed]), agentId);
  }

  /**
   * @param container  element that receives the list
   * @param opts.agentId  show only this agent's requests (null: all)
   * @param opts.title    heading above the list, shown only while there is something to list (null: none)
   * @param opts.emptyText  text for an empty list (null: show nothing)
   * @param opts.live     true for the one list whose confirmation is announced
   * @param opts.focusFallback  () => void, where the focus goes when the focused request disappears and nothing is left
   */
  function mountList(container, { agentId = null, title = null, emptyText = null, live: isLive = false, focusFallback = null } = {}) {
    const uid = ++listUid;
    const wrap = el("div", "approvals-wrap");
    const confirmEl = el("p", "approvals-confirm");
    if (isLive) confirmEl.setAttribute("role", "status");
    confirmEl.hidden = true;
    const titleEl = title ? el("h3", "panel-subtitle approvals-subtitle", title) : null;
    const errorEl = el("p", "approvals-error");
    errorEl.setAttribute("role", "alert");
    errorEl.hidden = true;
    const listEl = el("ul", "approvals-list");
    listEl.setAttribute("aria-label", TEXT.approvals.listLabel);
    const emptyEl = el("p", "approvals-empty");
    emptyEl.hidden = true;
    wrap.append(...[confirmEl, titleEl, errorEl, listEl, emptyEl].filter(Boolean));
    container.append(wrap);

    const records = new Map(); // approval id -> { li, refs }
    let filterAgent = agentId;
    let shown = false;
    let release = null;

    function createItem(id) {
      const li = el("li", "approval-item");
      li.dataset.approvalId = String(id);
      const pre = `approval-${uid}-${id}`;

      const head = el("div", "approval-head");
      const kind = el("span", "approval-kind");
      const amount = el("span", "approval-amount");
      head.append(kind, amount);
      const subject = el("p", "approval-subject");
      const meta = el("p", "approval-meta");
      const who = el("span", "approval-who");
      const time = el("time", "approval-time");
      meta.append(who, document.createTextNode(" \u00b7 "), time);
      const summary = el("p", "approval-summary");
      const reason = el("p", "approval-reason");

      const noteToggle = el("button", "approval-note-toggle");
      noteToggle.type = "button";
      noteToggle.setAttribute("aria-expanded", "false");
      noteToggle.setAttribute("aria-controls", `${pre}-note-wrap`);
      const noteWrap = el("div", "approval-note");
      noteWrap.id = `${pre}-note-wrap`;
      noteWrap.hidden = true;
      const noteLabel = el("label", "field-label", TEXT.approvals.noteLabel);
      noteLabel.htmlFor = `${pre}-note`;
      const noteInput = el("input");
      noteInput.type = "text";
      noteInput.id = `${pre}-note`;
      noteInput.maxLength = MAX_NOTE_CHARS;
      noteInput.autocomplete = "off";
      noteInput.spellcheck = false;
      const noteHelp = el("p", "field-help", fmt(TEXT.approvals.noteHelp, { max: MAX_NOTE_CHARS }));
      noteHelp.id = `${pre}-note-help`;
      noteInput.setAttribute("aria-describedby", noteHelp.id);
      noteWrap.append(noteLabel, noteInput, noteHelp);

      const msg = el("p", "approval-msg");
      msg.setAttribute("role", "alert");
      msg.id = `${pre}-msg`;
      msg.hidden = true;
      const rulesBtn = el("button", "approval-rules-btn", TEXT.approvals.openRules);
      rulesBtn.type = "button";
      rulesBtn.title = TEXT.approvals.openRulesTitle;
      rulesBtn.hidden = true;
      const dismissBtn = el("button", "approval-dismiss-btn", TEXT.approvals.dismiss);
      dismissBtn.type = "button";
      dismissBtn.hidden = true;

      const approveBtn = el("button", "approval-btn approval-approve", TEXT.approvals.approve);
      approveBtn.type = "button";
      const rejectBtn = el("button", "approval-btn approval-reject", TEXT.approvals.reject);
      rejectBtn.type = "button";
      const actions = el("div", "approval-actions");
      actions.append(approveBtn, rejectBtn);

      li.append(head, subject, meta, summary, reason, noteToggle, noteWrap, msg, rulesBtn, dismissBtn, actions);

      const rec = { li, kind, amount, subject, who, time, summary, reason, noteToggle, noteWrap, noteInput, msg, rulesBtn, dismissBtn, approveBtn, rejectBtn, actions, row: null };
      approveBtn.addEventListener("click", () => rec.row && decide(rec.row, true));
      rejectBtn.addEventListener("click", () => rec.row && decide(rec.row, false));
      rulesBtn.addEventListener("click", () => openRules?.());
      dismissBtn.addEventListener("click", () => dismiss(id));
      noteToggle.addEventListener("click", () => {
        const st = stateOf(id);
        st.noteOpen = !st.noteOpen;
        renderAll();
        if (st.noteOpen) noteInput.focus({ preventScroll: true });
      });
      noteInput.addEventListener("input", () => { stateOf(id).note = noteInput.value; renderAll(); });
      return rec;
    }

    function updateItem(rec, row, st) {
      const view = itemView(row, now());
      rec.row = row;
      const closed = st.closed;
      rec.li.classList.toggle("is-busy", !!st.busy);
      rec.li.classList.toggle("is-closed", closed);
      rec.li.dataset.kind = row.kind;
      if (st.busy) rec.li.setAttribute("aria-busy", "true"); else rec.li.removeAttribute("aria-busy");
      setText(rec.kind, view.kindLabel);
      setText(rec.amount, view.amountText);
      setText(rec.subject, view.subject);
      setText(rec.who, view.agentText);
      setText(rec.time, view.timeText);
      rec.time.title = view.timeTitle;
      rec.time.setAttribute("datetime", row.created_at);
      setText(rec.summary, view.summary);
      rec.summary.hidden = !view.summary;
      setText(rec.reason, view.reason);
      rec.reason.hidden = !view.reason;
      rec.li.setAttribute("aria-label", `${view.kindLabel}: ${view.subject}, ${view.amountText}`);

      // note
      setText(rec.noteToggle, st.noteOpen ? TEXT.approvals.noteHide : TEXT.approvals.noteAdd);
      rec.noteToggle.setAttribute("aria-expanded", String(st.noteOpen));
      rec.noteToggle.hidden = closed;
      rec.noteToggle.disabled = !!st.busy;
      rec.noteWrap.hidden = closed || !st.noteOpen;
      if (rec.noteInput.value !== st.note) rec.noteInput.value = st.note;
      rec.noteInput.disabled = !!st.busy;

      // inline message
      setText(rec.msg, st.message);
      rec.msg.hidden = !st.message;
      rec.msg.classList.toggle("is-ok", st.tone === "ok");
      rec.msg.classList.toggle("is-info", st.tone === "info");
      rec.msg.classList.toggle("is-error", st.tone === "error");
      rec.rulesBtn.hidden = !st.offerRules || closed;
      rec.rulesBtn.disabled = !!st.busy;
      rec.dismissBtn.hidden = !closed;
      rec.dismissBtn.setAttribute("aria-label", fmt(TEXT.approvals.dismissFor, { subject: view.subject }));

      // the two buttons
      rec.actions.hidden = closed;
      rec.approveBtn.disabled = !!st.busy;
      rec.rejectBtn.disabled = !!st.busy;
      setText(rec.approveBtn, st.busy === "approve" ? TEXT.approvals.approving : TEXT.approvals.approve);
      setText(rec.rejectBtn, st.busy === "reject" ? TEXT.approvals.rejecting : TEXT.approvals.reject);
      rec.approveBtn.setAttribute("aria-label", fmt(TEXT.approvals.approveFor, { subject: view.subject }));
      rec.rejectBtn.setAttribute("aria-label", fmt(TEXT.approvals.rejectFor, { subject: view.subject }));
      if (st.message) rec.li.setAttribute("aria-describedby", rec.msg.id); else rec.li.removeAttribute("aria-describedby");
    }

    function render() {
      const entries = visibleEntries(filterAgent);
      const active = document.activeElement;
      const oldChildren = [...listEl.children];
      // Where was the focus? If its request disappears, the request that takes its place gets it.
      let lostFocusAt = -1;
      const wantedIds = new Set(entries.map((r) => r.id));
      for (const [id, rec] of records) {
        if (wantedIds.has(id)) continue;
        const focusWasHere = rec.li.contains(active) || (id === focusedItemId && (!active || active === document.body));
        if (focusWasHere && lostFocusAt < 0) lostFocusAt = oldChildren.indexOf(rec.li);
        rec.li.remove();
        records.delete(id);
      }
      entries.forEach((row, index) => {
        let rec = records.get(row.id);
        if (!rec) { rec = createItem(row.id); records.set(row.id, rec); }
        const st = stateOf(row.id);
        updateItem(rec, row, st);
        if (listEl.children[index] !== rec.li) listEl.insertBefore(rec.li, listEl.children[index] ?? null);
        // A request that answered "try again" keeps the focus on the button the owner pressed.
        if (st.refocus && !st.busy && (!active || active === document.body || rec.li.contains(active))) {
          const target = st.closed ? rec.dismissBtn : st.pressed === "reject" ? rec.rejectBtn : rec.approveBtn;
          if (!target.disabled && !target.hidden) { target.focus({ preventScroll: true }); st.refocus = false; }
        }
      });

      setText(confirmEl, confirmation);
      confirmEl.hidden = !confirmation;
      setText(errorEl, loadError);
      errorEl.hidden = !loadError || !shown;
      let empty = "";
      if (!entries.length && !loadError) empty = loadedOnce ? emptyText ?? "" : emptyText ? TEXT.approvals.loading : "";
      setText(emptyEl, empty);
      emptyEl.hidden = !empty;
      if (titleEl) titleEl.hidden = !entries.length;
      listEl.hidden = !entries.length;
      wrap.classList.toggle("is-empty", !entries.length);

      if (lostFocusAt >= 0) {
        const next = listEl.children[Math.min(lostFocusAt, listEl.children.length - 1)];
        const target = next?.querySelector(".approval-approve:not(:disabled)");
        if (target) target.focus({ preventScroll: true });
        else focusFallback?.();
      }
    }

    function show(next) {
      if (next !== undefined) filterAgent = next;
      shown = true;
      release ??= retain();
      render();
      reload();
    }
    function hide() {
      shown = false;
      release?.();
      release = null;
    }
    function destroy() {
      hide();
      lists.delete(handle);
      wrap.remove();
    }
    const handle = { render, show, hide, destroy, el: wrap, get agentId() { return filterAgent; } };
    lists.add(handle);
    render();
    return handle;
  }

  function renderAll() {
    renderBadge();
    for (const list of lists) list.render();
  }

  // ---- queue panel ----
  const head = el("header", "panel-head");
  const heading = el("h2", "panel-name", TEXT.approvals.panelTitle);
  heading.id = "approvals-title";
  heading.tabIndex = -1;
  const closeBtn = el("button", "panel-close", "\u00d7");
  closeBtn.type = "button";
  closeBtn.setAttribute("aria-label", TEXT.approvals.close);
  closeBtn.title = TEXT.approvals.closeTitle;
  const headText = el("div", "panel-head-text");
  headText.append(heading);
  head.append(headText, closeBtn);
  root.replaceChildren(head);
  root.setAttribute("aria-labelledby", heading.id);
  const body = el("div", "approvals-body");
  root.append(body);
  const queueList = mountList(body, {
    agentId: null,
    emptyText: TEXT.approvals.empty,
    live: true,
    focusFallback: () => heading.focus({ preventScroll: true }),
  });

  function isOpen() {
    return !root.hidden;
  }
  function open() {
    if (isOpen()) { heading.focus({ preventScroll: true }); return; }
    root.hidden = false;
    renderBadge();
    queueList.show();
    heading.focus({ preventScroll: true });
    onOpen?.();
  }
  function close({ restoreFocus = true } = {}) {
    if (!isOpen()) return;
    const hadFocus = root.contains(document.activeElement);
    root.hidden = true;
    queueList.hide();
    renderBadge();
    if (restoreFocus && (hadFocus || document.activeElement === document.body)) badge.focus({ preventScroll: true });
    onClose?.();
  }

  badge.setAttribute("aria-controls", root.id);
  badge.addEventListener("click", () => (isOpen() ? close() : open()));
  closeBtn.addEventListener("click", () => close());
  // Escape closes the queue, unless a dialog is open (it handles Escape itself) or the focus is in the agent panel
  // (Escape closes that panel first).
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !isOpen() || e.defaultPrevented) return;
    if (document.body.classList.contains("modal-open")) return;
    const side = document.getElementById("side-panel");
    if (side && !side.hidden && side.contains(document.activeElement)) return;
    e.preventDefault();
    close();
  });

  root.hidden = true;
  renderBadge();

  return {
    setCount, reload, handleEvent, rulesSaved, mountList, open, close,
    isOpen,
    get count() { return count; },
    get rows() { return rows.map((r) => ({ ...r })); },
  };
}
