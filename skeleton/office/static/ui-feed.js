// Small activity feed in the bottom-left corner: the last 5 actions, collapsible.
// Live data: GET /api/activity once, then `activity` events. Text only, set with textContent.

import { actionStatusColor, actionStatusLabel, formatJakartaDateTime, formatJakartaTime, formatRupiah } from "./ui-format.js";
import { TEXT } from "./ui-text.js";

export const FEED_SIZE = 5;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

/**
 * @param opts.root       the <aside id="activity-feed"> element (holds #feed-toggle and #feed-list)
 * @param opts.api        ui-api.js or ui-mock.js object
 * @param opts.agentInfo  (id) => { name } | undefined
 */
export function createFeed({ root, api, agentInfo }) {
  const toggle = root.querySelector("#feed-toggle");
  const list = root.querySelector("#feed-list");
  const empty = root.querySelector("#feed-empty");
  let items = [];

  function setExpanded(expanded) {
    toggle.setAttribute("aria-expanded", String(expanded));
    root.classList.toggle("collapsed", !expanded);
    list.hidden = !expanded;
    empty.hidden = !expanded || items.length > 0;
  }
  toggle.addEventListener("click", () => setExpanded(toggle.getAttribute("aria-expanded") !== "true"));
  // A small screen starts collapsed so the feed does not cover the office.
  setExpanded(!(window.matchMedia && window.matchMedia("(max-width: 700px)").matches));

  function render() {
    const nodes = items.map((r) => {
      const color = actionStatusColor(r.status);
      const who = r.agent_id == null ? TEXT.feed.office : agentInfo?.(r.agent_id)?.name ?? TEXT.feed.agent;
      const li = el("li", `feed-item status-${color}`);
      const time = el("span", "feed-time", formatJakartaTime(r.created_at));
      const full = formatJakartaDateTime(r.created_at);
      if (full) time.title = full;
      const dot = el("span", `feed-dot chip-${color}`);
      dot.title = actionStatusLabel(r.status);
      dot.setAttribute("role", "img");
      dot.setAttribute("aria-label", actionStatusLabel(r.status));
      const text = el("span", "feed-text");
      const line = el("span", "feed-line");
      line.append(el("strong", null, who), document.createTextNode(` ${String(r.action ?? "")}`));
      text.append(line);
      if (r.reason) {
        // The reason is what tells "Sari Catat penjualan" apart from the next one (Requirement 7.1).
        text.append(el("span", "feed-reason", String(r.reason)));
        text.title = String(r.reason);
      }
      li.append(time, dot, text);
      if (r.cost_idr > 0) li.append(el("span", "feed-cost", formatRupiah(r.cost_idr)));
      return li;
    });
    list.replaceChildren(...nodes);
    toggle.querySelector(".feed-count").textContent = String(items.length);
    empty.hidden = items.length > 0 || list.hidden;
  }

  function sorted(rows) {
    return [...rows].sort((a, b) => (a.created_at < b.created_at ? 1 : a.created_at > b.created_at ? -1 : (b.id ?? 0) - (a.id ?? 0)));
  }

  async function load() {
    try {
      const rows = await api.getActivity(FEED_SIZE);
      items = sorted(Array.isArray(rows) ? rows : []).slice(0, FEED_SIZE);
    } catch (err) {
      console.warn("could not load the activity feed", err);
    }
    render();
  }

  /** One `activity` event from the stream. */
  function push(evt) {
    if (!evt || typeof evt.action !== "string") return;
    if (evt.id != null && items.some((r) => r.id === evt.id)) return;
    items = sorted([evt, ...items]).slice(0, FEED_SIZE);
    render();
  }

  render();
  return { load, push, render };
}
