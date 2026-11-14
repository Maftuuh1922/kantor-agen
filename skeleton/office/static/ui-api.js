// The browser's view of the backend REST API. One object with the calls the UI needs.
// Live mode uses fetch() against the same origin. Mock mode (?mock=1) uses ui-mock.js, which has the same methods.
//
// Every method returns a promise. Failures throw an Error whose message is safe to show to the owner
// (fixed text or the server's short message, never markup).

import { errorMessage } from "./ui-format.js";

const REQUEST_TIMEOUT_MS = 8000;

async function request(method, url, body) {
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
    throw new Error(err?.name === "AbortError" ? "The server did not answer in time." : "Cannot reach the server.");
  } finally {
    clearTimeout(timer);
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* no JSON body: handled below */
  }
  if (!res.ok) throw new Error(errorMessage(data?.detail, `The server answered ${res.status}.`));
  if (data === null) throw new Error("The server sent an unreadable answer.");
  return data;
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
    putPolicy: (policy) => request("PUT", "/api/policy", policy),
  };
}
