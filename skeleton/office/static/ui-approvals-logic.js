// Pure helpers for the owner's approval queue (task 14.2): no DOM, no network, no timers.
// ui-approvals.js (the DOM part) and tests/js/ui-approvals.test.mjs both import this file.
//
// The backend contract (skeleton/office/api.py):
//   GET  /api/approvals                  -> [{ id, agent_id, agent_name, kind, subject, amount_idr, summary, reason, status, created_at }]
//   POST /api/approvals/{id}/decision    -> 200 { id, status, message, cost_idr, pending_count }
//                                           404 { detail }, 409 { detail, status }, 409 failed { id, status: "failed", ... },
//                                           422 { detail: "<message>", status: "pending" } or { detail: [{ msg }] }, 503 { detail }

import { errorMessage, formatJakartaDateTime, formatJakartaTime, formatRupiah, truncate } from "./ui-format.js";
import { TEXT, fmt } from "./ui-text.js";

export const MAX_NOTE_CHARS = 200; // same limit as the backend (api.MAX_NOTE_CHARS)
export const POLL_MS = 15000;      // reload interval while the queue is open: the fallback for a lost event stream
export const INLINE_MESSAGE_MAX = 240;

// ---- count and badge ---------------------------------------------------------------------------

/** Any value -> a whole number of 0 or more. Bad data counts as 0. */
export function normalizeCount(value) {
  return Number.isSafeInteger(value) && value > 0 ? value : 0;
}

/** "Menunggu keputusan (3)". */
export function badgeText(count) {
  return fmt(TEXT.approvals.badge, { count: normalizeCount(count) });
}

/** "none" (muted) when nothing waits, "pending" (amber) otherwise. */
export function badgeState(count) {
  return normalizeCount(count) > 0 ? "pending" : "none";
}

// ---- rows ----------------------------------------------------------------------------------------

function isRow(r) {
  return !!r && typeof r === "object" && Number.isSafeInteger(r.id) && r.id >= 0 && typeof r.subject === "string";
}

const text = (v) => (typeof v === "string" ? v : "");

/** Keep the usable rows of GET /api/approvals, oldest first. Anything that is not a list gives []. */
export function normalizeApprovals(list) {
  if (!Array.isArray(list)) return [];
  const rows = list.filter(isRow).map((r) => ({
    id: r.id,
    agent_id: r.agent_id ?? null,
    agent_name: text(r.agent_name),
    kind: text(r.kind),
    subject: r.subject,
    amount_idr: Number.isFinite(r.amount_idr) ? r.amount_idr : 0,
    summary: text(r.summary),
    reason: text(r.reason),
    status: text(r.status) || "pending",
    created_at: text(r.created_at),
  }));
  return sortRows(rows);
}

/** Oldest first: by created_at, then by id. */
export function sortRows(rows) {
  return [...rows].sort((a, b) => (a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : a.id - b.id));
}

/** Rows of one agent. `agentId` null or undefined keeps every row. Ids are compared as text (live ids are numbers, mock ids text). */
export function rowsForAgent(rows, agentId) {
  if (agentId == null) return rows;
  return rows.filter((r) => r.agent_id != null && String(r.agent_id) === String(agentId));
}

// ---- time ----------------------------------------------------------------------------------------

/** "baru saja", "5 menit lalu", "2 jam lalu", and from 6 hours on the Jakarta clock ("14:32"). `nowMs` is Date.now(). */
export function relativeTime(iso, nowMs) {
  const then = Date.parse(/([zZ]|[+-]\d{2}:?\d{2})$/.test(String(iso)) ? iso : `${iso}Z`);
  if (!Number.isFinite(then) || !Number.isFinite(nowMs)) return formatJakartaTime(iso);
  const seconds = Math.floor((nowMs - then) / 1000);
  if (seconds < 0) return formatJakartaTime(iso); // a clock that is a little behind the server's
  if (seconds < 60) return TEXT.approvals.ago.justNow;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return fmt(TEXT.approvals.ago.minutes, { n: minutes });
  const hours = Math.floor(minutes / 60);
  if (hours < 6) return fmt(TEXT.approvals.ago.hours, { n: hours });
  return formatJakartaTime(iso);
}

// ---- item view model ------------------------------------------------------------------------------

/** Everything one queue item shows, as plain text. */
export function itemView(row, nowMs) {
  const kinds = TEXT.approvals.kinds;
  const name = row.agent_name.trim();
  return {
    id: row.id,
    kindLabel: Object.hasOwn(kinds, row.kind) ? kinds[row.kind] : kinds.other,
    subject: row.subject,
    amountText: formatRupiah(row.amount_idr),
    agentText: name ? fmt(TEXT.approvals.by, { name }) : TEXT.approvals.byUnknown,
    timeText: relativeTime(row.created_at, nowMs),
    timeTitle: formatJakartaDateTime(row.created_at),
    summary: row.summary,
    reason: row.reason ? fmt(TEXT.approvals.reason, { text: row.reason }) : "",
  };
}

// ---- the answer to a decision -----------------------------------------------------------------------

/** True when a policy message tells the owner to change the rules (limit, blocked spending, limit not set). */
export function mentionsRules(message) {
  return /\baturan\b|\bbatas\b/i.test(String(message ?? ""));
}

function countOf(data) {
  return Number.isSafeInteger(data?.pending_count) && data.pending_count >= 0 ? data.pending_count : null;
}

/**
 * Turn the answer of POST /api/approvals/{id}/decision into what the UI does.
 *
 * @param approve  true for "Setujui", false for "Tolak"
 * @param row      the request (uses `subject`)
 * @param result   { status: HTTP status, data: parsed JSON body or null }
 * @returns {{ kind, tone, message, offerRules, refresh, pendingCount }}
 *   kind "decided": it worked, the item goes away. "stale": the request is not pending any more, the item stays as a
 *   closed note until the owner dismisses it. "retry": nothing changed, the owner may press the button again.
 */
export function interpretDecision(approve, row, result) {
  const status = Number.isInteger(result?.status) ? result.status : 0;
  const data = result?.data && typeof result.data === "object" ? result.data : {};
  const pendingCount = countOf(data);
  const out = (kind, tone, message, extra = {}) => ({
    kind, tone, message: truncate(message, INLINE_MESSAGE_MAX), offerRules: false, refresh: false, pendingCount, ...extra,
  });

  if (status === 200) {
    const cost = Number.isFinite(data.cost_idr) ? data.cost_idr : 0;
    const subject = row.subject;
    const done = !approve
      ? fmt(TEXT.approvals.rejected, { subject })
      : cost > 0
        ? fmt(TEXT.approvals.approvedCost, { subject, amount: formatRupiah(cost) })
        : fmt(TEXT.approvals.approved, { subject });
    return out("decided", "ok", done, { refresh: true });
  }
  if (status === 404) return out("stale", "error", TEXT.approvals.notFound, { refresh: true });
  if (status === 409) {
    if (data.status === "failed") {
      const reason = typeof data.detail === "string" ? errorMessage(data.detail, "") : "";
      return out("stale", "error", reason ? fmt(TEXT.approvals.failed, { message: reason }) : TEXT.approvals.failedNoMessage, { refresh: true });
    }
    return out("stale", "error", TEXT.approvals.alreadyDecided, { refresh: true });
  }
  if (status === 422) {
    if (typeof data.detail === "string" && data.detail.trim()) {
      // A message from the policy engine ("Melebihi batas harian Rp 50.000; naikkan batas lewat Aturan ..."): show it as is.
      const message = errorMessage(data.detail, TEXT.approvals.blocked);
      return out("retry", "error", message, { offerRules: mentionsRules(message) });
    }
    return out("retry", "error", errorMessage(data.detail, TEXT.errors.invalidInput));
  }
  if (status === 503) return out("retry", "error", TEXT.approvals.busy);
  const fallback = fmt(TEXT.errors.statusCode, { status: status || "?" });
  return out("retry", "error", errorMessage(data.detail, fallback));
}

/** The request never got an answer (network error, timeout). The server may or may not have applied it, so reload. */
export function interpretDecisionFailure(err) {
  const reason = err && typeof err.message === "string" && err.message ? err.message : TEXT.errors.unreachable;
  return {
    kind: "retry",
    tone: "error",
    message: truncate(fmt(TEXT.approvals.notSent, { error: reason }), INLINE_MESSAGE_MAX),
    offerRules: false,
    refresh: true,
    pendingCount: null,
  };
}

/** The note for the request body: trimmed, at most MAX_NOTE_CHARS characters, null when empty. */
export function noteForRequest(note) {
  const clean = typeof note === "string" ? note.replace(/\s+/g, " ").trim() : "";
  return clean ? Array.from(clean).slice(0, MAX_NOTE_CHARS).join("") : null;
}
