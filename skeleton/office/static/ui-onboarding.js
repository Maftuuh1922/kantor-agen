// First-run screen (Requirements 1.1, 1.2): the owner types the business name and picks a business type.
// The backend then creates the business and its AI staff (POST /api/onboarding) and main.js goes on to the
// policy dialog (Requirement 1.3) and the office.
//
// The screen is generic: the business types, their texts and their staff come from GET /api/office
// ({ onboarding_required: true, business_types: [...] }), so Kantor Warung and Kantor Jasa use the same file.
// The text is Indonesian (the owner is an Indonesian shop or service owner).
//
// Top level of this file touches no DOM, so node can import the pure helpers (tests/js/ui-onboarding.test.mjs).
// Server text goes in with textContent only, never as markup.

export const MAX_BUSINESS_NAME_CHARS = 60;
const MAX_TEXT_CHARS = 300;
const MAX_TYPES = 12;
const MAX_STAFF = 12;
const REQUEST_TIMEOUT_MS = 20000;
const BACKGROUND_IDS = ["office-canvas", "overlay", "hud", "side-panel", "approvals-panel", "activity-feed", "policy-modal"];

// ---- pure helpers -----------------------------------------------------------------------------

export const NAME_MESSAGES = Object.freeze({
  empty: "Nama usaha harus diisi.",
  tooLong: `Nama usaha terlalu panjang. Maksimal ${MAX_BUSINESS_NAME_CHARS} huruf.`,
  control: "Nama usaha tidak boleh berisi karakter kontrol.",
  notText: "Nama usaha harus berupa teks.",
});
export const TYPE_MESSAGE = "Pilih jenis usaha.";

/** Business name from the input: trimmed, 1 to 60 characters, no control characters. Same rules as the backend. */
export function validateBusinessName(text) {
  if (typeof text !== "string") return { ok: false, value: null, error: NAME_MESSAGES.notText };
  const value = text.trim();
  if (value === "") return { ok: false, value: null, error: NAME_MESSAGES.empty };
  const chars = Array.from(value);
  if (chars.length > MAX_BUSINESS_NAME_CHARS) return { ok: false, value: null, error: NAME_MESSAGES.tooLong };
  if (/[\p{C}]/u.test(value)) return { ok: false, value: null, error: NAME_MESSAGES.control };
  return { ok: true, value, error: "" };
}

function cleanText(value, max = MAX_TEXT_CHARS) {
  return typeof value === "string" ? value.replace(/\s+/g, " ").trim().slice(0, max) : "";
}

/**
 * Business types from the server, made safe to render: only well-formed entries, texts cut to a sane length.
 * Entry: { key, label, description, agents: [{ name, role }] }. Anything else is dropped.
 */
export function normalizeBusinessTypes(raw) {
  if (!Array.isArray(raw)) return [];
  const seen = new Set();
  const types = [];
  for (const item of raw.slice(0, MAX_TYPES)) {
    const key = cleanText(item?.key, 32);
    const label = cleanText(item?.label, 80);
    if (!key || !label || seen.has(key)) continue;
    const agents = (Array.isArray(item.agents) ? item.agents : [])
      .slice(0, MAX_STAFF)
      .map((a) => ({ name: cleanText(a?.name, 40), role: cleanText(a?.role, 40) }))
      .filter((a) => a.name);
    seen.add(key);
    types.push({ key, label, description: cleanText(item.description), agents });
  }
  return types;
}

/** Check the form. Returns { ok, errors: {name?, type?}, value: {name, type} | null }. */
export function validateOnboarding(nameText, typeKey, types) {
  const errors = {};
  const name = validateBusinessName(nameText);
  if (!name.ok) errors.name = name.error;
  const known = Array.isArray(types) && types.some((t) => t.key === typeKey);
  if (!known) errors.type = TYPE_MESSAGE;
  const ok = Object.keys(errors).length === 0;
  return { ok, errors, value: ok ? { name: name.value, type: typeKey } : null };
}

/** First letter of a staff name for the round badge. "?" when there is none. */
export function staffInitial(name) {
  const first = Array.from(typeof name === "string" ? name.trim() : "")[0];
  return first ? first.toLocaleUpperCase("id-ID") : "?";
}

/** "Sari, Budi, Wati, Joko" for a card. */
export function staffNames(agents) {
  return (Array.isArray(agents) ? agents : []).map((a) => a.name).filter(Boolean).join(", ");
}

/** Owner-facing Indonesian message for a failed POST /api/onboarding. Never includes server markup or input. */
export function describeOnboardingFailure(status, body) {
  if (status === 409) {
    return body?.error === "already_onboarded"
      ? "Usaha ini sudah dibuat. Muat ulang halaman ini."
      : "Server belum bisa membuat usaha. Muat ulang halaman ini.";
  }
  if (status === 422) {
    const loc = Array.isArray(body?.detail) ? body.detail.map((d) => (Array.isArray(d?.loc) ? d.loc.join(".") : "")).join(" ") : "";
    if (/\bname\b/.test(loc)) return `Nama usaha belum benar. Isi 1 sampai ${MAX_BUSINESS_NAME_CHARS} huruf.`;
    if (/\btype\b/.test(loc)) return TYPE_MESSAGE;
    return "Isian belum benar. Periksa nama dan jenis usaha.";
  }
  if (typeof status === "number" && status >= 500) return "Server gagal membuat usaha. Coba lagi.";
  if (!status) return "Tidak bisa menghubungi server. Pastikan aplikasinya masih berjalan.";
  return "Usaha belum bisa dibuat. Coba lagi.";
}

// ---- backends: the real one and a mock for ?mock=1&onboarding=1 -----------------------------

class OnboardingError extends Error {
  constructor(status, body) {
    super(describeOnboardingFailure(status, body));
    this.status = status;
    this.body = body;
  }
}

/** POST /api/onboarding. Resolves with the normal /api/office payload of the new business. */
export function createOnboardingApi() {
  return {
    mock: false,
    async createBusiness({ name, type }) {
      const ctl = new AbortController();
      const timer = setTimeout(() => ctl.abort(), REQUEST_TIMEOUT_MS);
      let res;
      try {
        res = await fetch("/api/onboarding", {
          method: "POST",
          headers: { Accept: "application/json", "Content-Type": "application/json" },
          body: JSON.stringify({ name, type }),
          signal: ctl.signal,
          cache: "no-store",
        });
      } catch {
        throw new OnboardingError(0, null);
      } finally {
        clearTimeout(timer);
      }
      let body = null;
      try {
        body = await res.json();
      } catch {
        /* no JSON body */
      }
      if (!res.ok) throw new OnboardingError(res.status, body);
      if (body === null || typeof body !== "object") throw new OnboardingError(500, null);
      return body;
    },
  };
}

const MOCK_TYPES = Object.freeze([
  {
    key: "warung",
    label: "Warung / Toko",
    description: "Warung, toko kelontong, atau kios. Staf AI mencatat penjualan, menjaga stok, merapikan keuangan, dan membuat promo.",
    agents: [
      { name: "Sari", role: "Kasir" }, { name: "Budi", role: "Stok" },
      { name: "Wati", role: "Keuangan" }, { name: "Joko", role: "Marketing" },
    ],
  },
  {
    key: "jasa",
    label: "Usaha Jasa",
    description: "Salon, bengkel, bimbel, atau jasa lain. Staf AI mengatur jadwal, menagih, dan menghubungi pelanggan lagi.",
    agents: [
      { name: "Dewi", role: "Booking" }, { name: "Agus", role: "Tagihan" }, { name: "Rina", role: "Tindak lanjut" },
    ],
  },
]);

/** Stand-in backend for previewing the screen without a server. Same methods as the real one, plus `firstOffice`. */
export function createMockOnboardingApi() {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  return {
    mock: true,
    firstOffice: () => ({ onboarding_required: true, business_types: MOCK_TYPES.map((t) => ({ ...t, agents: t.agents.map((a) => ({ ...a })) })) }),
    async createBusiness({ name, type }) {
      await sleep(400);
      const template = MOCK_TYPES.find((t) => t.key === type);
      if (!template) throw new OnboardingError(422, { detail: [{ loc: ["body", "type"], msg: "unknown business type" }] });
      return {
        business: { id: 1, name, type, paused: false },
        agents: template.agents.map((a, i) => ({ id: `mock-${i + 1}`, name: a.name, role: a.role, desk_index: i, paused: false })),
        policy: { daily_limit_idr: null, blocked_actions: [], is_set: false },
        spent_today_idr: 0,
        demo_mode: true,
      };
    },
  };
}

// ---- the screen ------------------------------------------------------------------------------

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

/**
 * @param opts.api  { createBusiness({name, type}) -> Promise<office payload> }
 * Returns { run(firstOffice) -> Promise<office payload> }. `run` shows the screen and resolves when the
 * business has been created, with the payload the server answered (the normal /api/office shape).
 */
export function createOnboarding({ api }) {
  const $ = (id) => document.getElementById(id);
  const root = $("onboarding");
  const form = $("onboarding-form");
  const nameInput = $("onboarding-name");
  const nameError = $("onboarding-name-error");
  const typesBox = $("onboarding-types");
  const typeError = $("onboarding-type-error");
  const previewTitle = $("onboarding-preview-title");
  const previewList = $("onboarding-preview-list");
  const previewHint = $("onboarding-preview-hint");
  const formError = $("onboarding-error");
  const submit = $("onboarding-submit");

  let types = [];
  let saving = false;
  let finish = null; // set by run(): closes the screen and resolves its promise

  function setError(node, input, message) {
    node.textContent = message ?? "";
    node.hidden = !message;
    if (input) {
      if (message) input.setAttribute("aria-invalid", "true");
      else input.removeAttribute("aria-invalid");
    }
  }

  function selectedKey() {
    return typesBox.querySelector("input[type=radio]:checked")?.value ?? null;
  }

  function setInert(on) {
    for (const id of BACKGROUND_IDS) {
      const node = $(id);
      if (!node) continue;
      if (on) node.setAttribute("inert", "");
      else node.removeAttribute("inert");
    }
  }

  function renderPreview() {
    const type = types.find((t) => t.key === selectedKey());
    previewList.replaceChildren();
    previewHint.hidden = !!type;
    previewList.hidden = !type;
    previewTitle.textContent = type ? `Staf AI untuk ${type.label}` : "Staf AI yang akan bekerja untukmu";
    if (!type) return;
    for (const agent of type.agents) {
      const item = el("li", "staff-item");
      const badge = el("span", "staff-badge", staffInitial(agent.name));
      badge.setAttribute("aria-hidden", "true");
      const text = el("span", "staff-text");
      text.append(el("span", "staff-name", agent.name), el("span", "staff-role", agent.role));
      item.append(badge, text);
      previewList.append(item);
    }
  }

  function renderTypes() {
    typesBox.replaceChildren();
    types.forEach((type, index) => {
      const id = `onboarding-type-${index}`;
      const label = el("label", "type-card");
      label.htmlFor = id;
      const radio = el("input", "type-radio");
      radio.type = "radio";
      radio.name = "business-type";
      radio.id = id;
      radio.value = type.key;
      const body = el("span", "type-card-body");
      body.append(el("span", "type-card-title", type.label));
      if (type.description) body.append(el("span", "type-card-text", type.description));
      const names = staffNames(type.agents);
      if (names) body.append(el("span", "type-card-staff", `Staf: ${names}`));
      label.append(radio, body);
      typesBox.append(label);
      radio.addEventListener("change", () => {
        setError(typeError, null, "");
        renderPreview();
      });
    });
    // One business type: nothing to choose, so it is already picked.
    if (types.length === 1) typesBox.querySelector("input[type=radio]").checked = true;
    renderPreview();
  }

  nameInput.addEventListener("input", () => {
    if (nameError.textContent) setError(nameError, nameInput, "");
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (saving) return;
    setError(formError, null, "");
    const result = validateOnboarding(nameInput.value, selectedKey(), types);
    setError(nameError, nameInput, result.errors.name);
    setError(typeError, null, result.errors.type);
    if (!result.ok) {
      if (result.errors.name) nameInput.focus();
      else typesBox.querySelector("input[type=radio]")?.focus();
      return;
    }
    saving = true;
    submit.disabled = true;
    submit.textContent = "Menyiapkan kantor\u2026";
    try {
      const payload = await api.createBusiness(result.value);
      finish?.(payload);
    } catch (err) {
      setError(formError, null, err?.message || "Usaha belum bisa dibuat. Coba lagi.");
      saving = false;
      submit.disabled = false;
      submit.textContent = "Buka kantor";
    }
  });

  function run(firstOffice) {
    types = normalizeBusinessTypes(firstOffice?.business_types);
    const opener = document.activeElement;
    return new Promise((resolve, reject) => {
      if (types.length === 0) {
        reject(new Error("Server tidak menawarkan jenis usaha."));
        return;
      }
      finish = (payload) => {
        finish = null;
        root.hidden = true;
        document.body.classList.remove("onboarding-open");
        setInert(false);
        if (opener && typeof opener.focus === "function" && document.contains(opener)) opener.focus({ preventScroll: true });
        resolve(payload);
      };
      nameInput.value = "";
      setError(nameError, nameInput, "");
      setError(typeError, null, "");
      setError(formError, null, "");
      saving = false;
      submit.disabled = false;
      submit.textContent = "Buka kantor";
      renderTypes();
      root.hidden = false;
      document.body.classList.add("onboarding-open");
      setInert(true);
      nameInput.focus();
    });
  }

  root.hidden = true;
  return { run, get isOpen() { return !root.hidden; } };
}
