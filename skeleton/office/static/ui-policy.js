// First-run policy dialog (Requirement 1.3): the owner sets the daily spending limit and the blocked
// action types before any agent can spend. Saves with PUT /api/policy.
//
// The dialog cannot be dismissed with Escape or a click outside: until the policy exists the backend
// refuses every spend, so the owner has to answer it. The rest of the page is made inert while it is open.

import { ACTION_KINDS, formatRupiah, parseRupiah, validatePolicyInput } from "./ui-format.js";

const DEFAULT_LIMIT_IDR = 100000; // the default named in the design document
const BACKGROUND_IDS = ["office-canvas", "overlay", "hud", "side-panel", "activity-feed"];

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
    preview.textContent = n === null ? "" : n === 0 ? "Rp 0: agents cannot spend anything." : formatRupiah(n);
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

  function show() {
    opener = document.activeElement;
    limitInput.value = String(DEFAULT_LIMIT_IDR);
    for (const box of boxes.values()) box.checked = false;
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
    submit.textContent = "Saving\u2026";
    try {
      const policy = await api.putPolicy(result.value);
      hide();
      onSaved?.(policy);
    } catch (err) {
      setError(formError, null, `Could not save the policy. ${err.message}`);
    } finally {
      saving = false;
      submit.disabled = false;
      submit.textContent = "Save and start";
    }
  });

  root.hidden = true;
  return { show, hide, get isOpen() { return !root.hidden; } };
}
