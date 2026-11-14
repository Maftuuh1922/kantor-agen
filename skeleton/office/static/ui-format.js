// Pure helpers for the office UI: no DOM, no Three.js, no network.
// The browser modules (ui-*.js) and the node tests (tests/js/ui-format.test.mjs) both import this file.

export const TIME_ZONE = "Asia/Jakarta";
export const BUBBLE_MAX_CHARS = 60;
export const MAX_DAILY_LIMIT_IDR = 10 ** 15; // same ceiling as the backend (api.MAX_DAILY_LIMIT_IDR)

/** Action types the owner may block. Same list as the backend's ALLOWED_KINDS. */
export const ACTION_KINDS = Object.freeze([
  { id: "read", label: "Read data", hint: "Look at sales, stock and reports." },
  { id: "draft", label: "Draft text", hint: "Write drafts with the AI model." },
  { id: "spend", label: "Spend money", hint: "Place purchase orders and other spending." },
  { id: "customer_message", label: "Customer messages", hint: "Queue messages to customers (never sent in the MVP)." },
]);

// ---- money ---------------------------------------------------------------------------------

/** 12500 -> "Rp 12.500". Invalid input (null, NaN, text) gives "-" so bad data is visible, not hidden as zero. */
export function formatRupiah(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "-";
  const n = Math.round(value);
  const digits = String(Math.abs(n));
  if (!/^\d+$/.test(digits)) return "-"; // exponent form for huge numbers
  const grouped = digits.replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  return `${n < 0 ? "-" : ""}Rp ${grouped}`;
}

/**
 * Owner input -> whole rupiah, or null if it is not a clean amount.
 * Accepts "100000", "100.000", "Rp 100.000" and "100,000". Dots or commas must be thousands separators.
 */
export function parseRupiah(text) {
  if (typeof text !== "string") return null;
  const s = text.trim().replace(/^rp\.?\s*/i, "");
  if (!/^(\d+|\d{1,3}([.,]\d{3})+)$/.test(s)) return null;
  const n = Number(s.replace(/[.,]/g, ""));
  return Number.isSafeInteger(n) ? n : null;
}

/**
 * Check the policy form. Returns { ok, errors: {limit?, blocked?}, value: {daily_limit_idr, blocked_actions} | null }.
 * `blocked` is a list of action type ids.
 */
export function validatePolicyInput(limitText, blocked) {
  const errors = {};
  const raw = typeof limitText === "string" ? limitText.trim() : "";
  const limit = parseRupiah(raw);
  if (raw === "") errors.limit = "Enter the daily spending limit.";
  else if (limit === null) errors.limit = "Use digits only, for example 100000 or Rp 100.000.";
  else if (limit > MAX_DAILY_LIMIT_IDR) errors.limit = `The limit cannot be more than ${formatRupiah(MAX_DAILY_LIMIT_IDR)}.`;
  const known = new Set(ACTION_KINDS.map((k) => k.id));
  const list = Array.isArray(blocked) ? blocked : [];
  if (list.some((b) => !known.has(b))) errors.blocked = "Unknown action type.";
  const ok = Object.keys(errors).length === 0;
  return {
    ok,
    errors,
    value: ok ? { daily_limit_idr: limit, blocked_actions: [...new Set(list)].sort() } : null,
  };
}

// ---- text ----------------------------------------------------------------------------------

/** Cut text to at most `max` characters, ending with "…" when something was removed. Counts code points. */
export function truncate(text, max = BUBBLE_MAX_CHARS) {
  if (typeof text !== "string") return "";
  const clean = text.replace(/\s+/g, " ").trim();
  const chars = Array.from(clean);
  if (chars.length <= max) return clean;
  if (max <= 1) return max === 1 ? "…" : "";
  return `${chars.slice(0, max - 1).join("").trimEnd()}…`;
}

/** Text for a speech bubble: "" means "show no bubble". */
export function bubbleText(text) {
  return typeof text === "string" ? truncate(text, BUBBLE_MAX_CHARS) : "";
}

/** Server message for the owner. Handles a plain string and FastAPI's list of {msg}; never returns markup. */
export function errorMessage(detail, fallback = "Something went wrong. Try again.") {
  if (typeof detail === "string" && detail.trim()) return truncate(detail, 200);
  if (Array.isArray(detail)) {
    const msgs = detail.map((d) => (d && typeof d.msg === "string" ? d.msg : "")).filter(Boolean);
    if (msgs.length) return truncate(msgs.join("; "), 200);
  }
  return fallback;
}

// ---- time ----------------------------------------------------------------------------------

function toDate(iso) {
  if (iso instanceof Date) return Number.isNaN(iso.getTime()) ? null : iso;
  if (typeof iso !== "string" || !iso) return null;
  // The backend sends UTC ("+00:00"). A string without any zone is read as UTC too, not as browser time.
  const hasZone = /([zZ]|[+-]\d{2}:?\d{2})$/.test(iso);
  const d = new Date(hasZone ? iso : `${iso}Z`);
  return Number.isNaN(d.getTime()) ? null : d;
}

const timeFmt = new Intl.DateTimeFormat("en-GB", { timeZone: TIME_ZONE, hour: "2-digit", minute: "2-digit", hour12: false });
const dateTimeFmt = new Intl.DateTimeFormat("en-GB", {
  timeZone: TIME_ZONE, day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false,
});

/** ISO timestamp -> "14:32" in Asia/Jakarta. Invalid input gives "--:--". */
export function formatJakartaTime(iso) {
  const d = toDate(iso);
  return d ? timeFmt.format(d) : "--:--";
}

/** ISO timestamp -> "14 Nov, 14:32" in Asia/Jakarta (used for tooltips). */
export function formatJakartaDateTime(iso) {
  const d = toDate(iso);
  return d ? `${dateTimeFmt.format(d)} (WIB)` : "";
}

// ---- state and status colors -------------------------------------------------------------

export const MARKER_COLORS = Object.freeze(["green", "blue", "amber", "grey"]);

const STATE_COLOR = Object.freeze({ idle: "green", working: "blue", handoff: "blue", waiting: "amber", paused: "grey" });
const STATE_LABEL = Object.freeze({
  idle: "Idle",
  working: "Working",
  handoff: "Handing over a task",
  waiting: "Waiting for your approval",
  paused: "Paused",
});
const ACTIVITY_LABEL = Object.freeze({
  stretch: "stretching",
  drink: "getting a drink",
  chat: "chatting with a colleague",
});

/**
 * Marker color of an agent state: green = idle, blue = working (and handing over), amber = waiting for the owner,
 * grey = paused. Unknown states are grey. Agrees with BehaviorController.markerFor for amber and grey.
 */
export function stateColor(state) {
  return STATE_COLOR[state] ?? "grey";
}

/** Short text for a state, e.g. "Working". Unknown states give "Unknown". */
export function stateLabel(state) {
  return STATE_LABEL[state] ?? "Unknown";
}

/** "Idle", or "Idle (getting a drink)" when an idle behavior runs. Other states ignore `activity`. */
export function stateDetail(state, activity) {
  const base = stateLabel(state);
  const extra = state === "idle" ? ACTIVITY_LABEL[activity] : null;
  return extra ? `${base} (${extra})` : base;
}

const ACTION_STATUS_COLOR = Object.freeze({ done: "green", pending: "amber", rejected: "red", failed: "red", info: "grey" });
const ACTION_STATUS_LABEL = Object.freeze({
  done: "Done", pending: "Pending", rejected: "Rejected", failed: "Failed", info: "Info",
});

/** Color of an activity status: done green, pending amber, rejected and failed red, info grey. */
export function actionStatusColor(status) {
  return ACTION_STATUS_COLOR[status] ?? "grey";
}

export function actionStatusLabel(status) {
  return ACTION_STATUS_LABEL[status] ?? "Unknown";
}

/** "Rp 45.000 / Rp 100.000"; without a limit (not set yet): "Rp 45.000 / not set". */
export function spendSummary(spent, limit) {
  const left = formatRupiah(spent);
  return typeof limit === "number" && Number.isFinite(limit) ? `${left} / ${formatRupiah(limit)}` : `${left} / not set`;
}

/** Spending as a fraction of the limit, clamped to 0..1. No limit gives 0. A limit of 0 with spending gives 1. */
export function spendRatio(spent, limit) {
  if (typeof spent !== "number" || !Number.isFinite(spent) || spent <= 0) return 0;
  if (typeof limit !== "number" || !Number.isFinite(limit)) return 0;
  if (limit <= 0) return 1;
  return Math.min(1, spent / limit);
}
