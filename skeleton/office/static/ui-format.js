// Pure helpers for the office UI: no DOM, no Three.js, no network.
// The browser modules (ui-*.js) and the node tests (tests/js/ui-format.test.mjs) both import this file.
// The only import is ui-text.js, the pure dictionary of Indonesian words the owner reads.

import { TEXT, fmt } from "./ui-text.js";

export const TIME_ZONE = "Asia/Jakarta";
export const BUBBLE_MAX_CHARS = 60;
export const MAX_DAILY_LIMIT_IDR = 10 ** 15; // same ceiling as the backend (api.MAX_DAILY_LIMIT_IDR)
export const MAX_NAME_CHARS = 80; // same limit as the backend's PATCH /api/agents/{id} (api.MAX_NAME_CHARS)

/** Action types the owner may block. Same list as the backend's ALLOWED_KINDS. */
export const ACTION_KINDS = Object.freeze(
  ["read", "draft", "spend", "customer_message"].map((id) => Object.freeze({ id, ...TEXT.actionKinds[id] })),
);

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
  if (raw === "") errors.limit = TEXT.policy.limitEmpty;
  else if (limit === null) errors.limit = TEXT.policy.limitFormat;
  else if (limit > MAX_DAILY_LIMIT_IDR) errors.limit = fmt(TEXT.policy.limitTooBig, { max: formatRupiah(MAX_DAILY_LIMIT_IDR) });
  const known = new Set(ACTION_KINDS.map((k) => k.id));
  const list = Array.isArray(blocked) ? blocked : [];
  if (list.some((b) => !known.has(b))) errors.blocked = TEXT.policy.unknownKind;
  const ok = Object.keys(errors).length === 0;
  return {
    ok,
    errors,
    value: ok ? { daily_limit_idr: limit, blocked_actions: [...new Set(list)].sort() } : null,
  };
}

/**
 * Check the "Ubah nama" form (Requirement 1.4). Returns { ok, errors: {name?, role?}, value: {name, role} | null }.
 * Both texts are trimmed and need 1 to 80 characters without control characters, like PATCH /api/agents/{id}.
 */
export function validateRename(nameText, roleText) {
  const errors = {};
  const check = (text, emptyMsg, longMsg) => {
    const value = typeof text === "string" ? text.trim() : "";
    if (value === "") return { value, error: emptyMsg };
    if (Array.from(value).length > MAX_NAME_CHARS) return { value, error: fmt(longMsg, { max: MAX_NAME_CHARS }) };
    if (/[\p{C}]/u.test(value)) return { value, error: TEXT.rename.control };
    return { value, error: "" };
  };
  const name = check(nameText, TEXT.rename.nameEmpty, TEXT.rename.nameTooLong);
  const role = check(roleText, TEXT.rename.roleEmpty, TEXT.rename.roleTooLong);
  if (name.error) errors.name = name.error;
  if (role.error) errors.role = role.error;
  const ok = Object.keys(errors).length === 0;
  return { ok, errors, value: ok ? { name: name.value, role: role.value } : null };
}

/** The fields of a rename that really changed, for PATCH. `{}` when nothing changed. */
export function renameChanges(current, next) {
  const changes = {};
  if (next.name !== current?.name) changes.name = next.name;
  if (next.role !== current?.role) changes.role = next.role;
  return changes;
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

/**
 * Server message for the owner, in Indonesian. Handles a plain string and FastAPI's list of {msg}; never returns markup.
 * The fixed English messages of the API are translated (TEXT.serverMessages). A list of validation problems whose
 * messages are not known gives "Isian belum benar." instead of English text from the validator.
 */
export function errorMessage(detail, fallback = TEXT.errors.generic) {
  const known = (text) => (Object.hasOwn(TEXT.serverMessages, text.trim()) ? TEXT.serverMessages[text.trim()] : undefined);
  if (typeof detail === "string" && detail.trim()) return truncate(known(detail) ?? detail, 200);
  if (Array.isArray(detail) && detail.length) {
    const msgs = detail.map((d) => (d && typeof d.msg === "string" ? d.msg : "")).filter(Boolean);
    const translated = msgs.map((m) => known(m) ?? "").filter(Boolean);
    if (translated.length) return truncate([...new Set(translated)].join("; "), 200);
    if (msgs.length) return TEXT.errors.invalidInput;
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

// The clock uses the en-GB pattern ("14:32"); id-ID would write "14.32". Month names are Indonesian ("Agu", "Okt").
const timeFmt = new Intl.DateTimeFormat("en-GB", { timeZone: TIME_ZONE, hour: "2-digit", minute: "2-digit", hour12: false });
const dayFmt = new Intl.DateTimeFormat("id-ID", { timeZone: TIME_ZONE, day: "numeric", month: "short" });

/** ISO timestamp -> "14:32" in Asia/Jakarta. Invalid input gives "--:--". */
export function formatJakartaTime(iso) {
  const d = toDate(iso);
  return d ? timeFmt.format(d) : "--:--";
}

/** ISO timestamp -> "14 Nov, 14:32" in Asia/Jakarta (used for tooltips). */
export function formatJakartaDateTime(iso) {
  const d = toDate(iso);
  return d ? `${dayFmt.format(d)}, ${timeFmt.format(d)} (${TEXT.time.zone})` : "";
}

// ---- state and status colors -------------------------------------------------------------

export const MARKER_COLORS = Object.freeze(["green", "blue", "amber", "grey"]);

const STATE_COLOR = Object.freeze({ idle: "green", working: "blue", handoff: "blue", waiting: "amber", paused: "grey" });
const STATE_LABEL = TEXT.states;
const ACTIVITY_LABEL = TEXT.idleActivity;

/**
 * Marker color of an agent state: green = idle, blue = working (and handing over), amber = waiting for the owner,
 * grey = paused. Unknown states are grey. Agrees with BehaviorController.markerFor for amber and grey.
 */
export function stateColor(state) {
  return STATE_COLOR[state] ?? "grey";
}

/** Short text for a state, e.g. "Bekerja". Unknown states give "Tidak diketahui". */
export function stateLabel(state) {
  return STATE_LABEL[state] ?? STATE_LABEL.unknown;
}

/** "Santai", or "Santai (mengambil minum)" when an idle behavior runs. Other states ignore `activity`. */
export function stateDetail(state, activity) {
  const base = stateLabel(state);
  const extra = state === "idle" ? ACTIVITY_LABEL[activity] : null;
  return extra ? `${base} (${extra})` : base;
}

const ACTION_STATUS_COLOR = Object.freeze({ done: "green", pending: "amber", rejected: "red", failed: "red", info: "grey" });
const ACTION_STATUS_LABEL = TEXT.status;

/** Color of an activity status: done green, pending amber, rejected and failed red, info grey. */
export function actionStatusColor(status) {
  return ACTION_STATUS_COLOR[status] ?? "grey";
}

export function actionStatusLabel(status) {
  return ACTION_STATUS_LABEL[status] ?? ACTION_STATUS_LABEL.unknown;
}

/** "Rp 45.000 / Rp 100.000"; without a limit (not set yet): "Rp 45.000 / belum diatur". */
export function spendSummary(spent, limit) {
  const left = formatRupiah(spent);
  return typeof limit === "number" && Number.isFinite(limit) ? `${left} / ${formatRupiah(limit)}` : `${left} / ${TEXT.hud.notSet}`;
}

/** Spending as a fraction of the limit, clamped to 0..1. No limit gives 0. A limit of 0 with spending gives 1. */
export function spendRatio(spent, limit) {
  if (typeof spent !== "number" || !Number.isFinite(spent) || spent <= 0) return 0;
  if (typeof limit !== "number" || !Number.isFinite(limit)) return 0;
  if (limit <= 0) return 1;
  return Math.min(1, spent / limit);
}
