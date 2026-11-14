// Bootstrap for the office page: creates the scene and wires the HUD.
// Later tasks replace the demo desk setup with real agents from GET /api/office.

import { createOffice } from "./office.js";

const QUALITY_KEY = "kantor-agen.quality";
const params = new URLSearchParams(location.search);

function readQuality() {
  const fromUrl = params.get("quality");
  if (fromUrl === "low" || fromUrl === "normal") return fromUrl;
  try {
    return localStorage.getItem(QUALITY_KEY) === "low" ? "low" : "normal";
  } catch {
    return "normal";
  }
}

function showFatal(message) {
  const el = document.getElementById("fatal");
  el.textContent = message;
  el.hidden = false;
  document.getElementById("loading")?.classList.add("done");
}

const canvas = document.getElementById("office-canvas");
const qualityBtn = document.getElementById("quality-btn");

let office;
try {
  office = createOffice(canvas, { quality: readQuality() });
} catch (err) {
  console.error(err);
  showFatal("Could not start the 3D office. This browser needs WebGL.");
  throw err;
}

// Demo: light up the first N desks (default 4) until agents come from the backend.
const demoAgents = Math.max(0, Math.min(office.layout.maxAgents, Number(params.get("agents") ?? 4) || 0));
for (let i = 0; i < demoAgents; i++) office.addAgentDesk(i);

function syncQualityButton() {
  const low = office.getQuality() === "low";
  qualityBtn.textContent = `Quality: ${low ? "Low" : "Normal"}`;
  qualityBtn.setAttribute("aria-pressed", String(low));
}
qualityBtn.addEventListener("click", () => {
  const next = office.getQuality() === "low" ? "normal" : "low";
  office.setQuality(next);
  try {
    localStorage.setItem(QUALITY_KEY, next);
  } catch {
    /* storage may be blocked; the choice then lasts for this visit only */
  }
  syncQualityButton();
});
syncQualityButton();

window.office = office; // handy for the console and for later modules
office.start();
