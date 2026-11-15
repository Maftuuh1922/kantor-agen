// Top bar: business name, spending today against the daily limit, the global Pause / Resume button,
// the "Paused" badge and the "Demo mode" badge. Data comes from GET /api/office.
// The approvals badge ("Menunggu keputusan (N)") is drawn by ui-approvals.js; this file only hands it the count.

import { spendRatio, spendSummary } from "./ui-format.js";
import { TEXT, fmt } from "./ui-text.js";

const MESSAGE_MS = 6000;
const REFRESH_DEBOUNCE_MS = 400;
const REFRESH_EVERY_MS = 30000;

/**
 * @param opts.api       ui-api.js or ui-mock.js object
 * @param opts.onOffice  (officeData) => void, called every time /api/office was applied (agents, pause flags)
 * @param opts.onPauseChanged  (paused: boolean) => void, after the owner paused or resumed the office
 * @param opts.onPendingCount  (count: number) => void, the number of requests that wait for the owner (`pending_approvals`)
 */
export function createHud({ api, onOffice, onPauseChanged, onPendingCount }) {
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
    spendEl.title = limit == null ? TEXT.hud.spendTitleNoLimit : TEXT.hud.spendTitle;
    pausedBadge.hidden = !paused;
    demoBadge.hidden = !office.demo_mode;
    pauseBtn.disabled = busy;
    pauseBtn.textContent = paused ? TEXT.hud.resume : TEXT.hud.pause;
    pauseBtn.setAttribute("aria-label", paused ? TEXT.hud.resumeAll : TEXT.hud.pauseAll);
    pauseBtn.title = paused ? TEXT.hud.resumeTitle : TEXT.hud.pauseTitle;
    pauseBtn.classList.toggle("is-paused", paused);
    document.body.classList.toggle("office-paused", paused);
  }

  function apply(data) {
    office = data;
    render();
    if (Number.isSafeInteger(data?.pending_approvals)) onPendingCount?.(data.pending_approvals);
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
      showMessage(fmt(TEXT.hud.refreshFailed, { error: err.message }));
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
      showMessage(fmt(want ? TEXT.hud.pauseFailed : TEXT.hud.resumeFailed, { error: err.message }));
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
      // Not apply(): the count of pending requests in `office` may be older than the one the queue already knows.
      if (!office) return;
      office = { ...office, policy };
      render();
      onOffice?.(office);
    },
  };
}
