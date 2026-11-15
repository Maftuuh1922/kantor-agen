// The browser's view of the backend REST API. One object with the calls the UI needs.
// Live mode uses fetch() against the same origin. Mock mode (?mock=1) uses ui-mock.js, which has the same methods.
//
// Every method returns a promise. Failures throw an Error whose message is safe to show to the owner
// (fixed text or the server's short message, never markup).

import { errorMessage } from "./ui-format.js";
import { TEXT, fmt } from "./ui-text.js";

const REQUEST_TIMEOUT_MS = 8000;

// One HTTP call. Resolves with { res, data } for EVERY answer, also 4xx and 5xx. Rejects (with a message that is safe to
// show) only when there is no answer: network error, timeout.
async function send(method, url, body) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), REQUEST_TIMEOUT_MS);
  let res;
  try {
    res = await fetch(url, {
      method,
      headers: body === undefined ? { Accept: "application/json" } : { Accept: "application/json", "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: ctl.signal,
      cache: "no-store",
    });
  } catch (err) {
    throw new Error(err?.name === "AbortError" ? TEXT.errors.timeout : TEXT.errors.unreachable);
  } finally {
    clearTimeout(timer);
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* no JSON body: handled below */
  }
  return { res, data };
}

async function request(method, url, body) {
  const { res, data } = await send(method, url, body);
  if (!res.ok) throw new Error(errorMessage(data?.detail, fmt(TEXT.errors.statusCode, { status: res.status })));
  if (data === null) throw new Error(TEXT.errors.unreadable);
  return data;
}

// For calls where the answer to a 4xx matters (the approval decision): { status, data } for every HTTP answer.
async function requestWithStatus(method, url, body) {
  const { res, data } = await send(method, url, body);
  return { status: res.status, data };
}

export function createApi() {
  const agentUrl = (id, tail = "") => `/api/agents/${encodeURIComponent(String(id))}${tail}`;
  return {
    mock: false,
    getOffice: () => request("GET", "/api/office"),
    getAgent: (id) => request("GET", agentUrl(id)),
    getActivity: (limit = 5) => request("GET", `/api/activity?limit=${Math.max(1, Math.min(50, limit | 0))}`),
    pauseOffice: (paused) => request("POST", "/api/office/pause", { paused: !!paused }),
    pauseAgent: (id, paused) => request("POST", agentUrl(id, "/pause"), { paused: !!paused }),
    // Rename an agent or change its role label (Requirement 1.4). `changes` is { name?, role? }.
    patchAgent: (id, changes) => request("PATCH", agentUrl(id), changes),
    putPolicy: (policy) => request("PUT", "/api/policy", policy),
    // The owner's approval queue (task 14.2).
    getApprovals: () => request("GET", "/api/approvals"),
    // Resolves with { status, data } for every HTTP answer (200, 404, 409, 422, 503): ui-approvals-logic.js turns each
    // into an Indonesian message. Rejects only when the server cannot be reached.
    decideApproval: (id, approve, note = null) =>
      requestWithStatus("POST", `/api/approvals/${encodeURIComponent(String(id))}/decision`, { approve: approve === true, note: note || null }),
  };
}
