// Top bar: business name, spending today against the daily limit, the global Pause / Resume button,
// the "Paused" badge and the "Demo mode" badge. Data comes from GET /api/office.

import { spendRatio, spendSummary } from "./ui-format.js";

const MESSAGE_MS = 6000;
const REFRESH_DEBOUNCE_MS = 400;
const REFRESH_EVERY_MS = 30000;

/**
 * @param opts.api       ui-api.js or ui-mock.js object
 * @param opts.onOffice  (officeData) => void, called every time /api/office was applied (agents, pause flags)
 * @param opts.onPauseChanged  (paused: boolean) => void, after the owner paused or resumed the office
 */
export function createHud({ api, onOffice, onPauseChanged }) {
  const $ = (id) => document.getElementById(id);
  const nameEl = $("business-name");
  const spendEl = $("spend-info");
  const spendValue = $("spend-value");
  const pausedBadge = $("paused-badge");
  const demoBadge = $("demo-badge");
  const pauseBtn = $("pause-btn");
  const message = $("hud-message");

  let office = null;
  let busy = false;
  let messageTimer = null;
  let refreshTimer = null;
  let refreshing = false;

  function showMessage(text) {
    clearTimeout(messageTimer);
    message.textContent = text ?? "";
    message.hidden = !text;
    if (text) messageTimer = setTimeout(() => showMessage(""), MESSAGE_MS);
  }

  function render() {
    if (!office) return;
    const paused = !!office.business?.paused;
    if (office.business?.name) nameEl.textContent = office.business.name;
    const limit = office.policy?.is_set ? office.policy.daily_limit_idr : null;
    spendValue.textContent = spendSummary(office.spent_today_idr, limit);
    const ratio = spendRatio(office.spent_today_idr, limit);
    spendEl.classList.toggle("near-limit", ratio >= 0.8 && ratio < 1);
    spendEl.classList.toggle("at-limit", ratio >= 1);
    spendEl.title = limit == null ? "Spent today. The daily limit is not set yet." : "Spent today / daily spending limit";
    pausedBadge.hidden = !paused;
    demoBadge.hidden = !office.demo_mode;
    pauseBtn.disabled = busy;
    pauseBtn.textContent = paused ? "Resume" : "Pause";
    pauseBtn.setAttribute("aria-label", paused ? "Resume all agents" : "Pause all agents");
    pauseBtn.title = paused ? "Let agents start new work again" : "Stop agents from starting new work";
    pauseBtn.classList.toggle("is-paused", paused);
    document.body.classList.toggle("office-paused", paused);
  }

  function apply(data) {
    office = data;
    render();
    onOffice?.(data);
  }

  async function refresh() {
    if (refreshing) return office;
    refreshing = true;
    try {
      const data = await api.getOffice();
      apply(data);
      return data;
    } catch (err) {
      showMessage(`Could not refresh the office. ${err.message}`);
      return office;
    } finally {
      refreshing = false;
    }
  }

  /** Reload soon; many activity events in a row cause one request. */
  function refreshSoon() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refresh, REFRESH_DEBOUNCE_MS);
  }

  pauseBtn.addEventListener("click", async () => {
    if (!office || busy) return;
    const want = !office.business?.paused;
    busy = true;
    showMessage("");
    render();
    try {
      const res = await api.pauseOffice(want);
      office = { ...office, business: { ...office.business, paused: !!res.paused } };
      onPauseChanged?.(!!res.paused);
    } catch (err) {
      showMessage(`Could not ${want ? "pause" : "resume"} the office. ${err.message}`);
    } finally {
      busy = false;
      render();
      onOffice?.(office);
    }
  });

  setInterval(refresh, REFRESH_EVERY_MS);

  return {
    apply,
    refresh,
    refreshSoon,
    showMessage,
    isPaused: () => !!office?.business?.paused,
    get office() { return office; },
    /** The policy was saved: show the new limit at once. */
    setPolicy(policy) {
      if (office) apply({ ...office, policy });
    },
  };
}
