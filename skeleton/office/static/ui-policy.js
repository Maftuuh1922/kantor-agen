// First-run policy dialog (Requirement 1.3): the owner sets the daily spending limit and the blocked
// action types before any agent can spend. Saves with PUT /api/policy.
//
// The dialog cannot be dismissed with Escape or a click outside: until the policy exists the backend
// refuses every spend, so the owner has to answer it. The rest of the page is made inert while it is open.

import { ACTION_KINDS, formatRupiah, parseRupiah, validatePolicyInput } from "./ui-format.js";
import { TEXT, fmt } from "./ui-text.js";

const DEFAULT_LIMIT_IDR = 100000; // the default named in the design document
const BACKGROUND_IDS = ["office-canvas", "overlay", "hud", "side-panel", "approvals-panel", "activity-feed"];

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

/**
 * @param opts.api      ui-api.js or ui-mock.js object (putPolicy)
 * @param opts.onSaved  (policy) => void, with the policy the server stored
 */
export function createPolicyDialog({ api, onSaved }) {
  const $ = (id) => document.getElementById(id);
  const root = $("policy-modal");
  const form = $("policy-form");
  const limitInput = $("policy-limit");
  const limitError = $("policy-limit-error");
  const preview = $("policy-limit-preview");
  const blockedBox = $("policy-blocked");
  const blockedError = $("policy-blocked-error");
  const submit = $("policy-submit");
  const cancel = $("policy-cancel");
  const formError = $("policy-error");

  // One checkbox per action type.
  const boxes = new Map();
  for (const kind of ACTION_KINDS) {
    const id = `policy-block-${kind.id}`;
    const row = el("label", "check-row");
    row.htmlFor = id;
    const box = el("input");
    box.type = "checkbox";
    box.id = id;
    box.value = kind.id;
    const text = el("span", "check-text");
    text.append(el("span", "check-label", kind.label), el("span", "check-hint", kind.hint));
    row.append(box, text);
    blockedBox.append(row);
    boxes.set(kind.id, box);
  }

  let opener = null;
  let saving = false;
  // false: the first-run dialog (cannot be dismissed). true: the owner reopened it with the Rules button to
  // change the limit later (Requirement 8.2): it starts with the saved values and can be cancelled.
  let editing = false;
  const submitLabel = () => (editing ? TEXT.policy.save : TEXT.policy.saveAndStart);

  function setError(node, input, message) {
    node.textContent = message ?? "";
    node.hidden = !message;
    if (input) {
      if (message) input.setAttribute("aria-invalid", "true");
      else input.removeAttribute("aria-invalid");
    }
  }

  function updatePreview() {
    const n = parseRupiah(limitInput.value);
    preview.textContent = n === null ? "" : n === 0 ? TEXT.policy.zeroLimit : formatRupiah(n);
  }

  function readBlocked() {
    return [...boxes.entries()].filter(([, box]) => box.checked).map(([id]) => id);
  }

  function setInert(on) {
    for (const id of BACKGROUND_IDS) {
      const node = $(id);
      if (!node) continue;
      if (on) node.setAttribute("inert", "");
      else node.removeAttribute("inert");
    }
  }

  function focusables() {
    return [...root.querySelectorAll("input, button")].filter((n) => !n.disabled && !n.hidden);
  }

  /** @param current  the saved policy ({ is_set, daily_limit_idr, blocked_actions }) when the owner edits it later */
  function show(current) {
    editing = current?.is_set === true && Number.isSafeInteger(current.daily_limit_idr);
    opener = document.activeElement;
    limitInput.value = String(editing ? current.daily_limit_idr : DEFAULT_LIMIT_IDR);
    const blocked = new Set(editing && Array.isArray(current.blocked_actions) ? current.blocked_actions : []);
    for (const [id, box] of boxes) box.checked = blocked.has(id);
    cancel.hidden = !editing;
    submit.textContent = submitLabel();
    setError(limitError, limitInput, "");
    setError(blockedError, null, "");
    setError(formError, null, "");
    updatePreview();
    root.hidden = false;
    document.body.classList.add("modal-open");
    setInert(true);
    limitInput.focus();
    limitInput.select();
  }

  function hide() {
    root.hidden = true;
    document.body.classList.remove("modal-open");
    setInert(false);
    if (opener && typeof opener.focus === "function" && document.contains(opener)) opener.focus({ preventScroll: true });
    opener = null;
  }

  limitInput.addEventListener("input", () => {
    updatePreview();
    if (limitError.textContent) setError(limitError, limitInput, "");
  });

  // Keep Tab inside the dialog and ignore Escape.
  root.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      if (editing && !saving) hide(); // the first-run dialog ignores Escape
      return;
    }
    if (e.key !== "Tab") return;
    const items = focusables();
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  });

  cancel.addEventListener("click", () => {
    if (editing && !saving) hide();
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (saving) return;
    setError(formError, null, "");
    const result = validatePolicyInput(limitInput.value, readBlocked());
    setError(limitError, limitInput, result.errors.limit);
    setError(blockedError, null, result.errors.blocked);
    if (!result.ok) {
      if (result.errors.limit) limitInput.focus();
      return;
    }
    saving = true;
    submit.disabled = true;
    submit.textContent = TEXT.policy.saving;
    try {
      const policy = await api.putPolicy(result.value);
      hide();
      onSaved?.(policy);
    } catch (err) {
      setError(formError, null, fmt(TEXT.policy.saveFailed, { error: err.message }));
    } finally {
      saving = false;
      submit.disabled = false;
      submit.textContent = submitLabel();
    }
  });

  root.hidden = true;
  return { show, hide, get isOpen() { return !root.hidden; } };
}
