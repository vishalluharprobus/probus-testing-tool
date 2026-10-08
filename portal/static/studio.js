// Probus Test Studio - the page. Plain modules, no build step: edit and reload.
//
//   #/new         build a test (server, product, test, companies, MMV, RTO, ...)
//   #/runs        the team's runs
//   #/run/<id>    one run: live log, results, files and videos
//   #/guide       how it works

const view = document.getElementById("view");

/* ================================================================ helpers */
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const hue = (s) => [...String(s)].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 7);
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

const store = {
  get(k, d) { try { const v = localStorage.getItem("studio." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("studio." + k, JSON.stringify(v)); } catch { /* private window */ } },
};

function fmtDur(sec) {
  if (sec == null || isNaN(sec)) return "–";
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m) return `${m}m ${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}
function fmtMinutes(min) {
  if (min == null) return "–";
  if (min < 60) return `${min} min`;
  return `${Math.floor(min / 60)}h ${String(min % 60).padStart(2, "0")}m`;
}
function ago(ts) {
  if (!ts) return "";
  const s = Date.now() / 1000 - ts;
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}
function fmtSize(b) {
  if (b < 1024) return `${b} B`;
  if (b < 1048576) return `${(b / 1024).toFixed(0)} KB`;
  return `${(b / 1048576).toFixed(1)} MB`;
}
function initials(name) {
  const words = String(name || "?").replace(/[^\p{L}\p{N}\s]/gu, " ").trim().split(/\s+/).filter(Boolean);
  if (!words.length) return "?";
  return (words.length > 1 ? words[0][0] + words[1][0] : words[0][0]).toUpperCase();
}
function avatar(name, label = initials(name)) {
  return `<span class="avatar" style="--h:${hue(name)}" aria-hidden="true">${esc(label)}</span>`;
}
function rupees(n) {
  const v = Number(n);
  return isNaN(v) || !v ? "" : "₹" + Math.round(v).toLocaleString("en-IN");
}

/* ================================================================== icons */
const ICONS = {
  laptop: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M2 20h20"/>',
  flask: '<path d="M9 3h6"/><path d="M10 3v6L4.6 18.4A2 2 0 0 0 6.3 21.5h11.4a2 2 0 0 0 1.7-3.1L14 9V3"/><path d="M7.4 15h9.2"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a14 14 0 0 1 0 18a14 14 0 0 1 0-18z"/>',
  bike: '<circle cx="5.5" cy="17" r="3"/><circle cx="18.5" cy="17" r="3"/><path d="M5.5 17h5.5l3.5-7h3"/><path d="M14.5 5.5H17l1.5 4.5"/><path d="M8 11h5"/>',
  car: '<path d="M19 17h2v-4l-2.2-5.2A2 2 0 0 0 17 6.5H7a2 2 0 0 0-1.8 1.3L3 13v4h2"/><path d="M3 13h18"/><circle cx="7.5" cy="17" r="2.2"/><circle cx="16.5" cy="17" r="2.2"/><path d="M9.7 17h4.6"/>',
  radar: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><path d="M12 12l6.4-6.4"/><circle cx="12" cy="12" r="1" fill="currentColor"/>',
  microscope: '<path d="M6 18h8"/><path d="M3 22h18"/><path d="M14 22a7 7 0 1 0 0-14h-1"/><path d="M9 14h2"/><path d="M9 12a2 2 0 0 1-2-2V6h6v4a2 2 0 0 1-2 2z"/><path d="M12 6V3a1 1 0 0 0-1-1H9a1 1 0 0 0-1 1v3"/>',
  cart: '<circle cx="9" cy="20" r="1.5"/><circle cx="18" cy="20" r="1.5"/><path d="M2 3h3l2.7 12.4a2 2 0 0 0 2 1.6h7.7a2 2 0 0 0 2-1.6L21 8H6"/>',
  compass: '<circle cx="12" cy="12" r="9"/><path d="M15.5 8.5l-2 5-5 2 2-5z"/>',
  building: '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M9 7h1M14 7h1M9 11h1M14 11h1M9 15h1M14 15h1M10 21v-3h4v3"/>',
  pin: '<path d="M12 21s-7-6.1-7-11.5A7 7 0 0 1 19 9.5C19 14.9 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.5"/>',
  shield: '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  rocket: '<path d="M5 15c-1.5 1.3-2 4-2 6 2 0 4.7-.5 6-2"/><path d="M9 15l-3-3c1-4 4.5-9 12-9 0 7.5-5 11-9 12z"/><circle cx="14.5" cy="9.5" r="1.8"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-14.9-3.9L3 9"/><path d="M3 4v5h5"/><path d="M4 13a8 8 0 0 0 14.9 3.9L21 15"/><path d="M21 20v-5h-5"/>',
  copy: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  chevron: '<path d="M6 9l6 6 6-6"/>',
  left: '<path d="M15 6l-6 6 6 6"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  excel: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9.5 12.5l5 5M14.5 12.5l-5 5"/>',
  report: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 17v-3M12 17v-6M15 17v-2"/>',
  doc: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 13h6M9 17h4"/>',
  video: '<rect x="3" y="6" width="13" height="12" rx="2"/><path d="M16 10l5-3v10l-5-3"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="M21 16l-5-5-9 9"/>',
  terminal: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M13 15h4"/>',
  table: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M3 15h18M9 4v16"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  sparkles: '<path d="M12 3l1.8 4.7 4.7 1.8-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/>',
  alert: '<path d="M12 3.5l9.5 17h-19z"/><path d="M12 10v4M12 17.5v.01"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.01"/>',
  lock: '<rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
  external: '<path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/>',
  download: '<path d="M12 4v11"/><path d="M7 10l5 5 5-5"/><path d="M5 20h14"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="M10.8 12.2L20 3M16 7l3 3"/>',
  zap: '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>',
  layers: '<path d="M12 3l9 5-9 5-9-5z"/><path d="M3 13l9 5 9-5"/>',
  dice: '<rect x="3" y="3" width="18" height="18" rx="4"/><circle cx="8.5" cy="8.5" r="1.2" fill="currentColor"/><circle cx="15.5" cy="15.5" r="1.2" fill="currentColor"/><circle cx="12" cy="12" r="1.2" fill="currentColor"/>',
};
const icon = (name, extra = "") =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" ${extra}>${ICONS[name] || ""}</svg>`;

const TARGET_ICON = { local: "laptop", testsite: "flask", live: "globe" };
const TEST_ICON = { sweep: "radar", lab: "microscope", journey: "cart", best: "compass" };
const STATUS_ICON = { running: "radar", queued: "clock", passed: "check", bugs: "alert", failed: "x", stopped: "stop", interrupted: "alert" };
const STATUS_WORD = { running: "Running", queued: "Queued", passed: "Passed", bugs: "Bugs found", failed: "Failed", stopped: "Stopped", interrupted: "Interrupted" };
const LEVEL_HINT = { smoke: "1 journey", quick: "~15 journeys", standard: "12 journeys", full: "~50 journeys", deep: "~230 journeys" };

/* ==================================================================== api */
async function api(path, opts = {}) {
  const init = { headers: {}, ...opts };
  if (opts.json !== undefined) {
    init.method = init.method || "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.json);
  }
  const res = await fetch(path, init);
  if (res.status === 401) {
    await unlockModal();
    return api(path, opts);
  }
  let body = null;
  try { body = await res.json(); } catch { body = null; }
  if (!res.ok) throw new Error((body && body.error) || `${res.status} ${res.statusText}`);
  return body;
}

/* ================================================================== state */
const DEFAULT_FORM = {
  target: "testsite", product: "bike", test: "sweep", insurers: [], vehicles: [], rtos: [],
  policy: [], years: [], previous: [], ncb: [], claim: [], level: "standard", speed: "fast",
  journeys: "0", stage: "kyc-check", watch: false,
};
const S = {
  meta: null, health: null, runs: [],
  form: { ...DEFAULT_FORM, ...store.get("form", {}) },
  who: store.get("who", ""),
  plan: null, planBusy: false, planKey: "",
  ui: { moreOpen: store.get("moreOpen", false), planOpen: false, picker: {}, runFilter: "all", runSearch: "" },
  cleanup: [],
};

/* ================================================================= toasts */
function toast(msg, kind = "info", ms = 4200) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `${icon(kind === "ok" ? "check" : kind === "bad" ? "alert" : "info")}<div>${msg}</div>`;
  $("#toasts").appendChild(el);
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 320); }, ms);
}

/* ================================================================= modals */
function modal(html, { wide = false, dismiss = true, onClose } = {}) {
  const back = document.createElement("div");
  back.className = "modal-back";
  back.innerHTML = `<div class="modal${wide ? " wide" : ""}" role="dialog" aria-modal="true">${html}</div>`;
  $("#modal-root").appendChild(back);
  const last = document.activeElement;
  const close = () => {
    back.remove();
    document.removeEventListener("keydown", onKey);
    if (onClose) onClose();
    if (last && last.focus) last.focus();
  };
  const onKey = (e) => { if (e.key === "Escape" && dismiss) close(); };
  document.addEventListener("keydown", onKey);
  if (dismiss) back.addEventListener("mousedown", (e) => { if (e.target === back) close(); });
  setTimeout(() => { const f = $("input, button.btn.primary, button", back); if (f) f.focus(); }, 60);
  return { el: back, close };
}

function unlockModal() {
  return new Promise((resolve) => {
    const m = modal(`
      <div class="modal-icon">${icon("lock")}</div>
      <h2>This Studio has an access code</h2>
      <p>Ask whoever runs the Studio for it. You only type it once on this browser.</p>
      <form id="unlock-form">
        <label class="field">${icon("key")}<input id="unlock-code" type="password" autocomplete="current-password" placeholder="Access code" aria-label="Access code" required></label>
        <div class="modal-actions"><button class="btn primary" type="submit">${icon("lock")}Unlock</button></div>
      </form>`, { dismiss: false });
    $("#unlock-form", m.el).addEventListener("submit", async (e) => {
      e.preventDefault();
      const res = await fetch("/api/unlock", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: $("#unlock-code", m.el).value }) });
      if (res.ok) { m.close(); resolve(); } else toast("That is not the access code.", "bad");
    });
  });
}

function whoModal(required = false) {
  return new Promise((resolve) => {
    const m = modal(`
      <div class="modal-icon">${icon("user")}</div>
      <h2>${S.who ? "Change your name" : "Welcome! Who's testing?"}</h2>
      <p>Your name goes on the runs you start, so the team knows whose run is whose.</p>
      <form id="who-form">
        <label class="field">${icon("user")}<input id="who-name" maxlength="40" autocomplete="name" placeholder="e.g. Mayur" value="${esc(S.who)}" aria-label="Your name" required></label>
        <div class="modal-actions"><button class="btn primary" type="submit">${icon("check")}Save</button></div>
      </form>`, { dismiss: !required, onClose: () => resolve(S.who) });
    $("#who-form", m.el).addEventListener("submit", (e) => {
      e.preventDefault();
      const name = $("#who-name", m.el).value.trim();
      if (!name) return;
      S.who = name; store.set("who", name); paintWho(); m.close();
    });
  });
}

/* ================================================================ top bar */
function paintWho() {
  const btn = $("#who-btn");
  btn.innerHTML = S.who ? `${avatar(S.who)}<span class="who-name">${esc(S.who)}</span>`
    : `<span class="avatar" style="--h:220">${icon("user", 'style="width:15px;height:15px"')}</span><span class="who-name">Your name</span>`;
}
function paintTheme() {
  const dark = (document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark")) === "dark";
  $("#theme-btn").innerHTML = icon(dark ? "sun" : "moon");
  $("#theme-btn").setAttribute("aria-label", dark ? "Switch to light" : "Switch to dark");
}
function paintServers() {
  const h = S.health || {};
  $("#servers").innerHTML = (S.meta?.targets || []).map((t) => {
    const s = h[t.id]?.state || "unknown";
    return `<span class="server-pill" title="${esc(t.label)}: ${esc(h[t.id]?.detail || "checking...")}">
      <span class="dot ${s}"></span><span>${esc(t.label.replace(" production", ""))}</span></span>`;
  }).join("");
}
function paintNav(route) {
  $$(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === route));
  const active = $(".nav a.active"), ink = $(".nav-ink");
  if (active && ink) {
    ink.style.width = active.offsetWidth + "px";
    ink.style.transform = `translateX(${active.offsetLeft - 4}px)`;
    ink.style.opacity = 1;
  } else if (ink) ink.style.opacity = 0;
}
function paintBadge() {
  const n = S.runs.filter((r) => r.status === "running").length;
  const b = $("#running-badge");
  b.hidden = !n; b.textContent = n;
}

async function refreshHealth(force = false) {
  try {
    S.health = await api(`/api/health${force ? "?force=true" : ""}`);
    paintOffline(false);
    paintServers();
    if (route().name === "new") paintStep("server");
  } catch (err) {
    if (err instanceof TypeError) paintOffline(true);   // no answer at all
  }
}

// The page stays open after the Studio stops (its window closed, the PC slept).
// Say so plainly instead of letting every button fail in its own way.
function paintOffline(down) {
  let bar = document.getElementById("offline");
  if (!down) { if (bar) bar.remove(); return; }
  if (bar) return;
  bar = document.createElement("div");
  bar.id = "offline";
  bar.className = "offline";
  bar.setAttribute("role", "alert");
  bar.innerHTML = `${icon("alert")}<div><b>The Studio has stopped.</b> On the host computer, start <code>start_studio.bat</code> again (keep its black window open), then press Retry.</div>
    <button class="btn" type="button">${icon("refresh")}Retry</button>`;
  bar.querySelector("button").addEventListener("click", () => location.reload());
  document.body.appendChild(bar);
}
async function refreshRuns() {
  try { const d = await api("/api/runs?limit=200"); S.runs = d.runs; paintBadge(); paintOffline(false); }
  catch (err) { if (err instanceof TypeError) paintOffline(true); }
}

/* ================================================================= router */
function route() {
  const h = location.hash.replace(/^#\/?/, "");
  const [name, id] = h.split("/");
  return { name: ["new", "runs", "run", "guide"].includes(name) ? name : "new", id };
}
function go(hash) { location.hash = hash; }
async function render() {
  S.cleanup.forEach((f) => { try { f(); } catch { /* already gone */ } });
  S.cleanup = [];
  const r = route();
  document.body.classList.toggle("is-live", r.name === "new" && S.form.target === "live");
  paintNav(r.name === "run" ? "runs" : r.name);
  window.scrollTo({ top: 0, behavior: "instant" });
  if (r.name === "new") viewNew();
  else if (r.name === "runs") viewRuns();
  else if (r.name === "run") viewRun(r.id);
  else viewGuide();
}

/* =============================================================== NEW TEST */
function testSpec(id = S.form.test) { return S.meta.tests.find((t) => t.id === id); }
function uses(what) { return testSpec().uses.includes(what); }
function insurerByCode(code) { return S.meta.insurers.find((i) => i.code === code) || { code, short: code, name: code }; }

function normaliseForm(f) {
  const allowed = testSpec(f.test)?.targets || [];
  if (!allowed.includes(f.target)) f.test = "sweep";
  const single = f.test === "lab" || f.test === "journey";
  if (single && f.insurers.length > 1) f.insurers = f.insurers.slice(-1);
  if (f.test === "lab") { f.vehicles = f.vehicles.slice(-1); f.rtos = f.rtos.slice(-1); }
  return f;
}
function setForm(patch, { steps = true } = {}) {
  const before = S.form.product;
  Object.assign(S.form, patch);
  if (S.form.product !== before) S.form.vehicles = [];
  normaliseForm(S.form);
  store.set("form", S.form);
  document.body.classList.toggle("is-live", S.form.target === "live");
  if (steps) paintSteps();
  paintSummary();
  schedulePlan();
}
function apiForm() {
  const f = S.form;
  return { test: f.test, target: f.target, product: f.product, insurers: f.insurers,
    vehicles: f.vehicles.map((v) => v.key), rtos: f.rtos, policy: f.policy, years: f.years,
    previous: f.previous, ncb: f.ncb, claim: f.claim, level: f.level, speed: f.speed || "fast", journeys: f.journeys,
    stage: f.stage, watch: f.watch, who: S.who };
}
function problems() {
  const f = S.form, t = testSpec();
  if (!t.targets.includes(f.target)) return `${t.label} cannot run on the live site.`;
  if (f.test === "sweep" && f.target === "live" && !f.insurers.length) return "Pick the companies to ask - the live site needs them named.";
  if (f.test === "lab" && f.insurers.length !== 1) return "Pick the ONE company to deep-dive.";
  if (f.test === "sweep" && S.plan && !S.plan.ok && S.plan.key === planKey()) return S.plan.error;
  return "";
}

function viewNew() {
  view.innerHTML = `
    <section class="hero view-enter">
      <span class="eyebrow" style="--i:0"><b>NEW</b> Pick only what matters - the Studio chooses the rest</span>
      <h1 class="display" style="--i:1">What shall we <span class="gradient-text">test today?</span></h1>
      <p class="lede" style="--i:2">Choose a server, a product and a test. Companies, vehicle, RTO and policy details are all optional - leave them empty and the Studio picks a smart spread for you.</p>
    </section>
    <div class="builder">
      <div class="steps view-enter" id="steps"></div>
      <aside class="panel summary view-enter" id="summary" aria-live="polite"></aside>
    </div>`;
  paintSteps(true);
  paintSummary();
  schedulePlan(0);
}

const STEPS = ["server", "test", "companies", "where", "policy", "depth"];
function paintSteps(first = false) {
  const host = $("#steps");
  if (!host) return;
  host.innerHTML = STEPS.map((s) => `<section class="panel step" id="step-${s}"></section>`).join("");
  STEPS.forEach((s, i) => { const el = $(`#step-${s}`); el.style.setProperty("--i", i); paintStep(s); });
  if (!first) host.classList.remove("view-enter");
  requestAnimationFrame(placeThumbs);
}
function paintStep(name) {
  const el = $(`#step-${name}`);
  if (!el) return;
  const html = { server: stepServer, test: stepTest, companies: stepCompanies, where: stepWhere, policy: stepPolicy, depth: stepDepth }[name]();
  el.className = `panel step ${html.cls || ""}`;
  el.innerHTML = html.body;
  if (name === "where") wirePickers();
  if (name === "test") requestAnimationFrame(placeThumbs);
}
function head(n, title, sub, extra = "") {
  return `<div class="step-head"><span class="step-num">${String(n).padStart(2, "0")}</span>
    <div style="flex:1;min-width:0"><h2 class="step-title">${title}${extra}</h2><p class="step-sub">${sub}</p></div></div>`;
}

function stepServer() {
  const h = S.health || {};
  const live = h.live_login || {};
  const tiles = S.meta.targets.map((t) => {
    const st = h[t.id]?.state || "unknown";
    const on = S.form.target === t.id;
    return `<button class="tile${t.live ? " live" : ""}" role="radio" aria-checked="${on}" data-act="target" data-v="${t.id}">
      <span class="tick">${icon("check")}</span>
      <span class="tile-icon">${icon(TARGET_ICON[t.id])}</span>
      <span class="tile-title">${esc(t.label)} ${t.live ? '<span class="pill live">Quotes only</span>' : ""}</span>
      <span class="tile-text" style="display:block">${esc(t.blurb)}</span>
      <span class="tile-foot"><span class="dot ${st}"></span>${st === "up" ? "Online" : st === "down" ? "Not answering" : "Checking ..."}
        ${h[t.id]?.ms ? `<span style="color:var(--text-3);font-weight:500">· ${h[t.id].ms} ms</span>` : ""}</span>
    </button>`;
  }).join("");
  let liveRow = "";
  if (S.form.target === "live") {
    const state = live.saved ? (live.expired ? `Saved login expired ${ago(live.expires)}` : `Live login saved ${ago(live.saved_at)}`) : "No live login saved yet";
    liveRow = `<div class="notice live" style="margin-top:14px;align-items:center">${icon("shield")}
      <div style="flex:1"><b>Live production - quotes only, enforced in code.</b><br>Each journey sends a real quote request to every company you pick. ${esc(state)}.</div>
      <button class="btn" data-act="live-login" style="background:rgba(255,255,255,.18);border-color:rgba(255,255,255,.35);color:#fff">${icon("key")}${live.saved && !live.expired ? "Log in again" : "Log in"}</button></div>`;
  }
  return { cls: "done", body: head(1, "Where should it run?", "The server the tests drive. Status is checked from the computer that hosts this Studio.")
    + `<div class="tiles c3" role="radiogroup" aria-label="Server">${tiles}</div>${liveRow}` };
}

function stepTest() {
  const seg = S.meta.products.map((p) => `<button role="radio" aria-checked="${S.form.product === p.id}" data-act="product" data-v="${p.id}">${icon(p.id === "bike" ? "bike" : "car")}${esc(p.label)}</button>`).join("");
  const tiles = S.meta.tests.map((t) => {
    const ok = t.targets.includes(S.form.target);
    return `<button class="tile" role="radio" aria-checked="${S.form.test === t.id}" data-act="test" data-v="${t.id}" ${ok ? "" : "disabled"} ${ok ? "" : 'title="Not on the live site - it is quotes only"'}>
      <span class="tick">${icon("check")}</span>
      <span class="tile-icon">${icon(TEST_ICON[t.id])}</span>
      <span class="tile-title">${esc(t.label)} ${ok ? "" : '<span class="pill soft">Not on live</span>'}</span>
      <span class="tile-text" style="display:block">${esc(t.blurb)}</span>
    </button>`;
  }).join("");
  return { cls: "done", body: head(2, "Product & test", "What to insure, and what kind of test to run.")
    + `<div class="seg" role="radiogroup" aria-label="Product"><span class="seg-thumb"></span>${seg}</div>
       <div class="tiles c2" role="radiogroup" aria-label="Test">${tiles}</div>` };
}

function stepCompanies() {
  const f = S.form, single = f.test === "lab" || f.test === "journey";
  const on = uses("insurers") || uses("insurer");
  const required = (f.test === "sweep" && f.target === "live") || f.test === "lab";
  const sub = !on ? "This test finds its own companies." :
    f.test === "lab" ? "Pick the ONE company to deep-dive." :
    f.test === "journey" ? "Pick one company - or none, and the tool drives whichever priced and is least likely to redirect." :
    f.target === "live" ? "Required on live: only these companies are asked; everyone else's quote call is stopped in the browser." :
    "Leave empty to ask every company - or pick a few to keep the run focused.";
  const presets = single ? "" : S.meta.presets.map((p) => {
    const hot = p.codes.length === f.insurers.length && p.codes.every((c) => f.insurers.includes(c));
    return `<button class="ghost${hot ? " hot" : ""}" data-act="preset" data-v="${p.id}">${icon("sparkles")}${esc(p.label)}</button>`;
  }).join("") + `<button class="ghost" data-act="all-insurers">${icon("layers")}All</button>`;
  const chips = S.meta.insurers.map((i) => {
    const pressed = f.insurers.includes(i.code);
    return `<button class="chip" aria-pressed="${pressed}" data-act="insurer" data-v="${i.code}" title="${esc(i.name)} · company id ${esc(i.id ?? "")}">
      ${avatar(i.code, i.code.length <= 4 ? i.code : initials(i.short))}
      <span style="min-width:0"><span class="chip-name" style="display:block">${esc(i.short)}</span><span class="chip-code">${esc(i.code)}</span></span>
      <span class="chip-check">${icon("check")}</span></button>`;
  }).join("");
  const count = f.insurers.length ? `${f.insurers.length} picked` : (f.test === "journey" ? "the tool chooses" : "every company");
  return { cls: `${on ? "" : "off"} ${f.insurers.length ? "done" : ""}`,
    body: head(3, "Companies", sub, required ? ' <span class="pill warn">Required</span>' : ' <span class="tag-opt">Optional</span>')
    + `<div class="step-body"><div class="toolbar">${presets}${f.insurers.length ? `<button class="ghost" data-act="clear-insurers">${icon("x")}Clear</button>` : ""}<span class="count">${count}</span></div>
       <div class="chips">${chips}</div></div>` };
}

function stepWhere() {
  const f = S.form;
  const vOn = uses("vehicles") || uses("vehicle"), rOn = uses("rtos") || uses("rto");
  const on = vOn || rOn;
  const vMax = f.test === "lab" ? 1 : 6, rMax = f.test === "lab" ? 1 : 8;
  const vTags = f.vehicles.map((v, i) => `<span class="tag"><span>${esc(v.label)}</span><small>${esc(v.band || "")}</small>
      <button data-act="rm-vehicle" data-i="${i}" aria-label="Remove ${esc(v.label)}">${icon("x")}</button></span>`).join("");
  const rTags = f.rtos.map((r, i) => `<span class="tag"><span>${esc(r)}</span>
      <button data-act="rm-rto" data-i="${i}" aria-label="Remove ${esc(r)}">${icon("x")}</button></span>`).join("");
  const popular = ["MH-01 Mumbai", "DL-01 Delhi", "KA-01 Bengaluru", "TN-01 Chennai", "MH-12 Pune", "WB-01 Kolkata", "GJ-01 Ahmedabad", "TS-09 Hyderabad"]
    .filter((r) => !f.rtos.includes(r));
  return { cls: `${on ? "" : "off"} ${f.vehicles.length || f.rtos.length ? "done" : ""}`,
    body: head(4, "Vehicle & RTO", on ? "Search the portal's own lists. Leave empty and the Studio picks a spread." : "This test picks its own vehicle and RTO.", ' <span class="tag-opt">Optional</span>')
    + `<div class="step-body pickers">
        <div class="picker" data-picker="vehicles">
          <div class="picker-label">${icon(f.product === "bike" ? "bike" : "car")}Make · model · variant <span class="count" style="margin-left:auto">${f.vehicles.length}/${vMax}</span></div>
          <label class="field">${icon("search")}<input data-picker-input="vehicles" placeholder="${f.product === "bike" ? "e.g. activa, classic 350, ather" : "e.g. swift vxi, creta, nexon ev"}" autocomplete="off" role="combobox" aria-expanded="false" aria-controls="menu-vehicles" aria-label="Search vehicles" ${vOn ? "" : "disabled"}><kbd>↵</kbd></label>
          <div class="menu" id="menu-vehicles" role="listbox" hidden></div>
          <div class="tags">${vTags}</div>
          ${f.vehicles.length ? "" : `<p class="hint">${icon("dice")}Empty = ${f.test === "lab" ? "a popular, proven vehicle" : "one per engine size, untried ones first"}</p>`}
        </div>
        <div class="picker" data-picker="rtos">
          <div class="picker-label">${icon("pin")}RTO <span class="count" style="margin-left:auto">${f.rtos.length}/${rMax}</span></div>
          <label class="field">${icon("search")}<input data-picker-input="rtos" placeholder="e.g. MH-12, Pune, KA 05" autocomplete="off" role="combobox" aria-expanded="false" aria-controls="menu-rtos" aria-label="Search RTOs" ${rOn ? "" : "disabled"}><kbd>↵</kbd></label>
          <div class="menu" id="menu-rtos" role="listbox" hidden></div>
          <div class="tags">${rTags}</div>
          ${f.rtos.length ? "" : `<p class="hint">${icon("dice")}Empty = ${f.test === "lab" ? "GJ-01 Ahmedabad" : "one RTO per state, across 4 states"}</p>`}
          ${rOn && f.rtos.length < rMax ? `<div class="quick">${popular.slice(0, 6).map((r) => `<button data-act="quick-rto" data-v="${esc(r)}">+ ${esc(r)}</button>`).join("")}</div>` : ""}
        </div>
      </div>` };
}

function stepPolicy() {
  const f = S.form, on = uses("policy");
  const c = S.meta.choices, rules = c.rules[f.product];
  const group = (field, label, values, small = "", cls = "") => `<div><div class="group-label">${label}${small ? `<small>${small}</small>` : ""}</div>
    <div class="toggles ${cls}">${values.map((v) => `<button class="toggle" aria-pressed="${f[field].includes(String(v))}" data-act="toggle" data-field="${field}" data-v="${esc(v)}">${esc(v === "default" ? "App default" : v)}</button>`).join("")}</div></div>`;
  const years = Array.from({ length: 21 }, (_, i) => String(c.this_year - i));
  const picked = ["policy", "years", "previous", "ncb", "claim"].reduce((n, k) => n + f[k].length, 0);
  return { cls: `${on ? "" : "off"} ${picked ? "done" : ""}`,
    body: `<details class="more" ${S.ui.moreOpen && on ? "open" : ""} data-details="more">
      <summary>${head(5, "Policy details", on ? (picked ? `${picked} choice${picked > 1 ? "s" : ""} pinned - everything else varies` : "Pin policy type, year, previous policy, NCB or claim - or let every option be tried.") : "Only the Quote sweep uses these.", ' <span class="tag-opt">Optional</span>').replace("</div></div>", `</div>${icon("chevron", 'class="chev"')}</div>`)}</summary>
      <div class="groups step-body">
        ${group("policy", "Policy type", c.policy, `OD Only up to ${rules.od_max_age} years · Comprehensive up to ${rules.cp_max_age}`)}
        ${group("years", "Registration year", years, "scroll for older", "years")}
        ${group("previous", "Previous policy", c.previous)}
        ${group("ncb", "NCB", c.ncb, "never more than the vehicle's age allows")}
        ${group("claim", "Claim made last year", c.claim)}
      </div></details>` };
}

function stepDepth() {
  const f = S.form;
  let body;
  if (f.test === "sweep") {
    const speed = f.speed || "fast";
    body = `<div class="group-label">Speed</div>
      <div class="tiles c2" role="radiogroup" aria-label="Speed" style="margin-bottom:18px">${S.meta.speeds.map((sp) => `
      <button class="tile level" role="radio" aria-checked="${speed === sp.id}" data-act="speed" data-v="${sp.id}">
        <span class="tick">${icon("check")}</span>
        <span class="tile-title">${icon(sp.id === "fast" ? "zap" : "laptop", 'style="width:17px;height:17px;color:var(--c1)"')}${esc(sp.label)} ${sp.id === "fast" ? '<span class="pill ok">Recommended</span>' : ""}</span>
        <span class="tile-text" style="display:block">${esc(sp.blurb)}</span></button>`).join("")}</div>
      <div class="group-label">How thorough</div>
      <div class="tiles c5" role="radiogroup" aria-label="How thorough">${S.meta.levels.map((l) => `
      <button class="tile level" role="radio" aria-checked="${f.level === l.id}" data-act="level" data-v="${l.id}">
        <span class="tick">${icon("check")}</span>
        <span class="tile-title">${esc(l.label)}</span>
        <span class="tile-text" style="display:block">${esc(l.blurb)}</span>
        <span class="big" style="display:block">${LEVEL_HINT[l.id] || ""}</span></button>`).join("")}</div>`;
  } else if (f.test === "lab") {
    const opts = [["0", "Quotes only", "~150 API quotes, nothing created"], ["1", "1 to payment", "The best quote on through KYC & proposal"], ["3", "3 to payment", "Three quotes, ~3 min each"], ["all", "All priced", "Every priced quote - long"]];
    body = `<div class="tiles c2" role="radiogroup">${opts.map(([v, t, d]) => `<button class="tile level" role="radio" aria-checked="${f.journeys === v}" data-act="journeys" data-v="${v}">
      <span class="tick">${icon("check")}</span><span class="tile-title">${t}</span><span class="tile-text" style="display:block">${d}</span></button>`).join("")}</div>
      <p class="hint">${icon("info")}Going past quotes needs the server's write ceiling raised in config/settings.local.json - otherwise the run stops safely at the quote.</p>`;
  } else if (f.test === "journey") {
    body = `<div class="tiles c3" role="radiogroup">${S.meta.stages.map((s) => `<button class="tile level" role="radio" aria-checked="${f.stage === s.id}" data-act="stage" data-v="${s.id}">
      <span class="tick">${icon("check")}</span><span class="tile-title">${esc(s.label)}</span></button>`).join("")}</div>
      <p class="hint">${icon("shield")}It never pays. Submitting KYC calls the insurer's KYC service for real.</p>`;
  } else {
    body = `<p class="hint" style="margin:0">${icon("compass")}About 16 big cities × a handful of popular vehicles, straight to the API - a few minutes.</p>`;
  }
  return { cls: "done", body: head(6, f.test === "sweep" ? "How thorough?" : f.test === "lab" ? "How far?" : f.test === "journey" ? "How far?" : "What it does",
    f.test === "sweep" ? "More journeys, more combinations - the summary shows the real count." : "")
    + body + `<div class="switch-row"><button class="switch" role="switch" aria-checked="${f.watch}" data-act="watch" aria-label="Show the browser on the host computer"></button>
      <div><strong>Show the browser</strong><p>Opens a visible browser on the computer hosting the Studio - handy when that is yours. Videos are recorded either way.</p></div></div>` };
}

/* ---------------------------------------------------------- summary */
function sentence() {
  const f = S.form, t = testSpec(), target = S.meta.targets.find((x) => x.id === f.target);
  const product = f.product === "bike" ? "two-wheelers" : "private cars";
  const names = f.insurers.map((c) => insurerByCode(c).short);
  const companies = names.length ? (names.length > 3 ? `${names.slice(0, 3).join(", ")} +${names.length - 3}` : names.join(", ")) : null;
  const veh = f.vehicles.length ? f.vehicles.map((v) => `${v.make} ${v.model}`).slice(0, 2).join(", ") + (f.vehicles.length > 2 ? ` +${f.vehicles.length - 2}` : "") : null;
  const rto = f.rtos.length ? f.rtos.slice(0, 2).join(", ") + (f.rtos.length > 2 ? ` +${f.rtos.length - 2}` : "") : null;
  if (f.test === "sweep") {
    const level = S.meta.levels.find((l) => l.id === f.level)?.label;
    const fast = (f.speed || "fast") === "fast" ? " fast" : "";
    return `A <b>${esc(level)}${fast}</b> quote sweep of <b>${product}</b> on <b>${esc(target.label)}</b>, asking <b>${esc(companies || "every company")}</b>, for <b>${esc(veh || "a spread of vehicles")}</b> in <b>${esc(rto || "a spread of RTOs")}</b>.`;
  }
  if (f.test === "lab") return `A deep-dive into <b>${esc(companies || "one company")}</b> for <b>${product}</b> on <b>${esc(target.label)}</b>, starting from <b>${esc(veh || "a proven vehicle")}</b> in <b>${esc(rto || "GJ-01 Ahmedabad")}</b>.`;
  if (f.test === "journey") return `A buy journey for <b>${product}</b> with <b>${esc(companies || "the best company the tool finds")}</b> on <b>${esc(target.label)}</b> - <b>${esc(S.meta.stages.find((s) => s.id === f.stage)?.label)}</b>.`;
  return `Find the city and vehicle that get <b>${product}</b> prices from the most companies on <b>${esc(target.label)}</b>.`;
}
function paintSummary() {
  const el = $("#summary");
  if (!el) return;
  const f = S.form, p = S.plan && S.plan.key === planKey() ? S.plan : null;
  const problem = problems();
  const isLive = f.target === "live";
  let stats;
  if (f.test === "sweep") {
    if (S.planBusy || !p) stats = ["Journeys", "Time", "Coverage"].map((l) => `<div class="stat"><div class="stat-num"><span class="skeleton"></span></div><div class="stat-label">${l}</div></div>`).join("");
    else if (p.ok) stats = `<div class="stat"><div class="stat-num" data-count="${p.journeys}">${p.journeys}</div><div class="stat-label">Journeys</div></div>
      <div class="stat"><div class="stat-num">${fmtMinutes(p.minutes)}</div><div class="stat-label">About</div></div>
      <div class="stat"><div class="stat-num">${p.wanted ? Math.round((100 * p.covered) / p.wanted) + "%" : "–"}</div><div class="stat-label">${p.wanted ? `${p.covered}/${p.wanted} ${esc(p.unit)}` : "Coverage"}</div></div>`;
    else stats = "";
  } else {
    const est = { lab: ["~150", "quotes", "5–20 min"], journey: ["1", "journey", "3–6 min"], best: ["~60", "quotes", "3–8 min"] }[f.test];
    stats = `<div class="stat"><div class="stat-num">${est[0]}</div><div class="stat-label">${est[1]}</div></div>
      <div class="stat"><div class="stat-num" style="font-size:18px">${est[2]}</div><div class="stat-label">About</div></div>
      <div class="stat"><div class="stat-num" style="font-size:18px">${icon(TEST_ICON[f.test], 'style="width:22px;height:22px"')}</div><div class="stat-label">${esc(testSpec().label)}</div></div>`;
  }
  const planList = p && p.ok && p.items?.length ? `<details ${S.ui.planOpen ? "open" : ""} data-details="plan"><summary class="ghost" style="width:max-content">${icon("layers")}See the ${p.journeys} journeys</summary>
      <div class="plan-list">${p.items.map((it) => `<div class="plan-item"><span class="n">${it.n}</span><div><span class="k">${esc(it.kind)}</span> <span class="t">${esc(it.text)}</span></div></div>`).join("")}
      ${p.journeys > p.items.length ? `<div class="plan-item"><span class="n">…</span><div class="t">${p.journeys - p.items.length} more</div></div>` : ""}</div></details>` : "";
  const cmd = p?.command || (S.lastCommand && S.lastCommand.key === planKey() ? S.lastCommand.text : "");
  el.innerHTML = `
    <h2>Your test</h2>
    <p class="sentence">${sentence()}</p>
    <div class="stats">${stats}</div>
    ${p && !p.ok && f.test === "sweep" ? `<div class="notice bad">${icon("alert")}<div>${esc(p.error)}</div></div>` : ""}
    ${isLive ? `<div class="notice warn">${icon("shield")}<div>Live site: quotes only. Nothing is bought, no KYC, no proposal - fixed in code.</div></div>` : ""}
    ${planList}
    ${cmd ? `<div class="cmd" title="The same run from a terminal in the project folder">${esc(cmd)}<button data-act="copy" data-v="${esc(cmd)}" aria-label="Copy command">${icon("copy")}</button></div>` : ""}
    <button class="launch${isLive ? " live" : ""}" data-act="launch" ${problem ? "disabled" : ""}>${isLive ? "Launch on LIVE" : "Launch test"} ${icon("rocket")}</button>
    ${problem ? `<p class="why-not">${esc(problem)}</p>` : `<p class="why-not">Runs join the team queue · you can watch it live</p>`}`;
  countUp(el);
  paintMobileBar(p, problem, isLive);
}
function paintMobileBar(p, problem, isLive) {
  let bar = $("#mobile-bar");
  if (!bar) {
    bar = document.createElement("div");
    bar.id = "mobile-bar"; bar.className = "mobile-bar";
    view.appendChild(bar);
  }
  const what = S.form.test === "sweep" ? (p && p.ok ? `${p.journeys} journeys · ${fmtMinutes(p.minutes)}` : S.planBusy ? "Planning ..." : "") : testSpec().label;
  bar.innerHTML = `<div class="mb-text"><b>${esc(testSpec().label)}</b>${esc(problem || what)}</div>
    <button class="launch${isLive ? " live" : ""}" data-act="launch" ${problem ? "disabled" : ""}>${isLive ? "Launch on LIVE" : "Launch"} ${icon("rocket")}</button>`;
}
function countUp(root) {
  if (reduced) return;
  $$("[data-count]", root).forEach((n) => {
    const end = +n.dataset.count, t0 = performance.now();
    const step = (t) => { const k = Math.min(1, (t - t0) / 650); n.textContent = Math.round(end * (1 - Math.pow(1 - k, 3))); if (k < 1) requestAnimationFrame(step); };
    requestAnimationFrame(step);
  });
}

/* ------------------------------------------------------------ plan */
const planKey = () => JSON.stringify({ ...apiForm(), who: undefined, watch: undefined });
let planCtl = null;
const runPlan = async () => {
  const key = planKey();
  if (planCtl) planCtl.abort();
  planCtl = new AbortController();
  S.planBusy = true; paintSummary();
  try {
    const res = await fetch("/api/plan", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(apiForm()), signal: planCtl.signal });
    const body = await res.json();
    if (key !== planKey()) return;
    if (!res.ok) S.plan = { ok: false, error: body.error || "Cannot plan this run.", key };
    else if (body.journeys == null && body.ok) { S.plan = null; S.lastCommand = { key, text: body.command }; }
    else S.plan = { ...body, key };
  } catch (e) {
    if (e.name === "AbortError") return;
    S.plan = { ok: false, error: "The Studio is not answering. Is its black window still open on the host computer? Start start_studio.bat again, then reload this page.", key };
    paintOffline(true);
  } finally {
    if (key === planKey()) { S.planBusy = false; paintSummary(); }
  }
};
const planSoon = debounce(runPlan, 650);
function schedulePlan(delay) { if (delay === 0) runPlan(); else planSoon(); }

/* ---------------------------------------------------------- pickers */
function wirePickers() {
  $$("[data-picker-input]").forEach((input) => {
    const name = input.dataset.pickerInput;
    const ui = S.ui.picker[name] = S.ui.picker[name] || { items: [], active: 0 };
    const search = debounce(() => fetchPicker(name, input.value), 140);
    input.addEventListener("input", search);
    input.addEventListener("focus", () => fetchPicker(name, input.value));
    input.addEventListener("blur", () => setTimeout(() => closeMenu(name), 120));
    input.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") { e.preventDefault(); ui.active = Math.min(ui.items.length - 1, ui.active + 1); paintMenu(name, input.value); }
      else if (e.key === "ArrowUp") { e.preventDefault(); ui.active = Math.max(0, ui.active - 1); paintMenu(name, input.value); }
      else if (e.key === "Enter") { e.preventDefault(); if (ui.items[ui.active]) pick(name, ui.items[ui.active]); }
      else if (e.key === "Escape") { closeMenu(name); input.blur(); }
    });
  });
}
async function fetchPicker(name, q) {
  const url = name === "vehicles" ? `/api/vehicles?product=${S.form.product}&q=${encodeURIComponent(q)}&limit=30` : `/api/rtos?q=${encodeURIComponent(q)}&limit=30`;
  try {
    const d = await api(url);
    const ui = S.ui.picker[name];
    ui.items = d.items; ui.active = 0; ui.total = d.total; ui.count = d.count;
    paintMenu(name, q);
  } catch { /* typing on */ }
}
function highlight(text, q) {
  let out = esc(text);
  q.trim().split(/\s+/).filter((w) => w.length > 1).forEach((w) => {
    out = out.replace(new RegExp(`(${w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "ig"), "<mark>$1</mark>");
  });
  return out;
}
function paintMenu(name, q) {
  const menu = $(`#menu-${name}`), input = $(`[data-picker-input="${name}"]`);
  if (!menu || !input || document.activeElement !== input) return;
  const ui = S.ui.picker[name];
  const chosen = name === "vehicles" ? S.form.vehicles.map((v) => v.key) : S.form.rtos;
  const rows = ui.items.map((it, i) => {
    const already = chosen.includes(name === "vehicles" ? it.key : it.name);
    if (name === "vehicles") {
      return `<button class="opt" role="option" aria-selected="${i === ui.active}" data-act="pick" data-picker="${name}" data-i="${i}" ${it.hidden ? "disabled" : ""}>
        <span class="code-badge">${esc(it.make.slice(0, 3))}</span>
        <span class="opt-main"><span class="opt-title" style="display:block">${highlight(`${it.make} ${it.model}`, q)}</span>
        <span class="opt-sub" style="display:block">${highlight(it.variant, q)}${it.hidden ? " · cannot be picked on the form" : ""}</span></span>
        <span class="pill ${it.band === "electric" ? "ok" : "soft"}">${esc(it.band || it.fuel)}</span>${already ? `<span class="pill info">Added</span>` : ""}</button>`;
    }
    return `<button class="opt" role="option" aria-selected="${i === ui.active}" data-act="pick" data-picker="${name}" data-i="${i}">
      <span class="code-badge">${esc(it.code)}</span>
      <span class="opt-main"><span class="opt-title" style="display:block">${highlight(it.city || it.name, q)}</span><span class="opt-sub" style="display:block">${esc(it.name)}${it.zone ? ` · zone ${esc(it.zone)}` : ""}</span></span>
      ${it.top ? '<span class="pill soft">Top city</span>' : ""}${already ? `<span class="pill info">Added</span>` : ""}</button>`;
  }).join("");
  const label = q.trim() ? `${ui.count} match${ui.count === 1 ? "" : "es"}` : (name === "vehicles" ? "Popular - type to search all " + ui.total : "Top cities - type to search all " + ui.total);
  menu.innerHTML = rows ? `<div class="menu-head">${label}</div>${rows}` : `<div class="menu-empty">Nothing matches “${esc(q)}”.</div>`;
  menu.hidden = false;
  input.setAttribute("aria-expanded", "true");
  const act = $('.opt[aria-selected="true"]', menu);
  if (act) act.scrollIntoView({ block: "nearest" });
}
function closeMenu(name) {
  const menu = $(`#menu-${name}`), input = $(`[data-picker-input="${name}"]`);
  if (menu) menu.hidden = true;
  if (input) input.setAttribute("aria-expanded", "false");
}
function pick(name, it) {
  const f = S.form, max = f.test === "lab" ? 1 : name === "vehicles" ? 6 : 8;
  if (name === "vehicles") {
    if (it.hidden) return toast("That variant can never be picked on the portal's form.", "bad");
    if (!f.vehicles.some((v) => v.key === it.key)) {
      const v = { key: it.key, make: it.make, model: it.model, label: `${it.make} ${it.model} ${it.variant}`, band: it.band };
      f.vehicles = max === 1 ? [v] : [...f.vehicles, v].slice(-max);
    }
  } else if (!f.rtos.includes(it.name)) {
    f.rtos = max === 1 ? [it.name] : [...f.rtos, it.name].slice(-max);
  }
  setForm({});
  const input = $(`[data-picker-input="${name}"]`);
  if (input) { input.value = ""; input.focus(); }
}

/* --------------------------------------------------------- actions */
view.addEventListener("mousedown", (e) => { if (e.target.closest(".opt")) e.preventDefault(); });
view.addEventListener("toggle", (e) => {
  const d = e.target.closest("details[data-details]");
  if (!d) return;
  if (d.dataset.details === "more") { S.ui.moreOpen = d.open; store.set("moreOpen", d.open); }
  if (d.dataset.details === "plan") S.ui.planOpen = d.open;
}, true);
view.addEventListener("click", async (e) => {
  const el = e.target.closest("[data-act]");
  if (!el || el.disabled) return;
  const act = el.dataset.act, v = el.dataset.v, f = S.form;
  const toggleIn = (list, x) => (list.includes(x) ? list.filter((y) => y !== x) : [...list, x]);
  switch (act) {
    case "target": setForm({ target: v }); if (v === "live") refreshHealth(); break;
    case "product": setForm({ product: v }); break;
    case "test": setForm({ test: v }); break;
    case "insurer": {
      const single = f.test === "lab" || f.test === "journey";
      setForm({ insurers: single ? (f.insurers[0] === v ? [] : [v]) : toggleIn(f.insurers, v) }); break;
    }
    case "preset": setForm({ insurers: [...S.meta.presets.find((p) => p.id === v).codes] }); break;
    case "all-insurers": setForm({ insurers: S.meta.insurers.map((i) => i.code) }); break;
    case "clear-insurers": setForm({ insurers: [] }); break;
    case "toggle": setForm({ [el.dataset.field]: toggleIn(f[el.dataset.field], v) }); break;
    case "level": setForm({ level: v }); break;
    case "speed": setForm({ speed: v }); break;
    case "journeys": setForm({ journeys: v }); break;
    case "stage": setForm({ stage: v }); break;
    case "watch": setForm({ watch: !f.watch }); break;
    case "rm-vehicle": f.vehicles.splice(+el.dataset.i, 1); setForm({}); break;
    case "rm-rto": f.rtos.splice(+el.dataset.i, 1); setForm({}); break;
    case "quick-rto": if (!f.rtos.includes(v)) setForm({ rtos: [...f.rtos, v].slice(f.test === "lab" ? -1 : -8) }); break;
    case "pick": pick(el.dataset.picker, S.ui.picker[el.dataset.picker].items[+el.dataset.i]); break;
    case "copy": copy(v); break;
    case "launch": launch(el); break;
    case "live-login": liveLoginModal(); break;
    default: break;
  }
});
async function copy(text) {
  try { await navigator.clipboard.writeText(text); toast("Copied - paste it in a terminal in the project folder.", "ok", 2600); }
  catch { toast("Could not copy - select the text instead.", "bad"); }
}
function placeThumbs() {
  $$(".seg").forEach((seg) => {
    const on = $('[aria-checked="true"]', seg), thumb = $(".seg-thumb", seg);
    if (on && thumb) { thumb.style.width = on.offsetWidth + "px"; thumb.style.transform = `translateX(${on.offsetLeft - 4}px)`; }
  });
}

async function launch(btn) {
  if (problems()) return;
  if (!S.who) { await whoModal(true); if (!S.who) return; }
  const form = apiForm();
  if (S.form.target === "live") {
    const ok = await confirmLive();
    if (!ok) return;
    form.confirm = "LIVE";
  }
  btn.disabled = true;
  try {
    const run = await api("/api/runs", { json: form });
    burst(btn);
    toast(`<b>Launched.</b> ${esc(run.title)}`, "ok");
    setTimeout(() => go(`#/run/${run.id}`), reduced ? 0 : 420);
  } catch (err) {
    toast(esc(err.message), "bad", 6000);
    btn.disabled = false;
  }
}
function burst(btn) {
  if (reduced) return;
  const r = btn.getBoundingClientRect(), b = document.createElement("div");
  b.className = "burst";
  b.style.left = `${r.left + r.width / 2 - 10}px`; b.style.top = `${r.top + r.height / 2 - 10}px`;
  if (S.form.target === "live") b.style.background = "linear-gradient(120deg,#ff4d6d,#ff8a3d)";
  document.body.appendChild(b);
  setTimeout(() => b.remove(), 800);
}
function confirmLive() {
  return new Promise((resolve) => {
    let done = false;
    const names = S.form.insurers.map((c) => insurerByCode(c).short).join(", ");
    const m = modal(`
      <div class="modal-icon live">${icon("globe")}</div>
      <h2>Run on the LIVE site?</h2>
      <p>This sends real quote requests to <b>${esc(names)}</b> on buy.probusinsurance.com. Quotes only - nothing is bought, no KYC, no proposal.</p>
      <form id="live-form">
        <label class="field">${icon("shield")}<input id="live-word" class="confirm-word" placeholder="Type LIVE" autocomplete="off" aria-label="Type LIVE to confirm"></label>
        <div class="modal-actions"><button class="btn" type="button" data-close>Cancel</button><button class="btn primary" type="submit" style="background:linear-gradient(120deg,#ff4d6d,#ff8a3d)">${icon("rocket")}Launch on live</button></div>
      </form>`, { onClose: () => { if (!done) resolve(false); } });
    $("[data-close]", m.el).addEventListener("click", () => m.close());
    $("#live-form", m.el).addEventListener("submit", (e) => {
      e.preventDefault();
      if ($("#live-word", m.el).value.trim().toUpperCase() !== "LIVE") return toast("Type LIVE to confirm.", "bad");
      done = true; m.close(); resolve(true);
    });
  });
}

/* ------------------------------------------------------- live login */
function liveLoginModal() {
  let timer = null, closed = false;
  const m = modal(`<div id="ll"></div>`, { wide: true, onClose: () => { closed = true; clearTimeout(timer); api("/api/live-login/cancel", { json: {} }).catch(() => {}); refreshHealth(true); } });
  const box = $("#ll", m.el);
  const draw = (st) => {
    const step = { starting: 1, credentials: 1, working: 2, otp: 2, done: 3 }[st.state] || 1;
    const shot = st.screenshot ? `<div class="shot-frame"><img src="${st.screenshot}" alt="What the live login page shows right now"></div>` : "";
    let body = "";
    if (st.state === "credentials") body = `<form id="ll-cred"><label class="field">${icon("user")}<input id="ll-user" autocomplete="username" placeholder="Live username" aria-label="Live username" required></label>
        <label class="field">${icon("lock")}<input id="ll-pass" type="password" autocomplete="current-password" placeholder="Password" aria-label="Password" required></label>
        <p class="hint">${icon("shield")}Typed into the login page on the host computer and forgotten - never saved.</p>
        <div class="modal-actions"><button class="btn primary" type="submit">${icon("key")}Log in</button></div></form>`;
    else if (st.state === "otp") body = `<form id="ll-otp"><label class="field">${icon("key")}<input id="ll-code" inputmode="numeric" maxlength="8" autocomplete="one-time-code" placeholder="OTP" aria-label="OTP" required></label>
        <div class="modal-actions"><button class="btn primary" type="submit">${icon("check")}Confirm OTP</button></div></form>`;
    else if (st.state === "done") body = `<div class="notice info">${icon("check")}<div><b>Logged in.</b> Live runs will use this login until it expires.</div></div><div class="modal-actions"><button class="btn primary" data-close>Done</button></div>`;
    else if (st.state === "failed" || st.state === "idle") body = `<div class="notice bad">${icon("alert")}<div>${esc(st.message || "The login stopped.")}</div></div><div class="modal-actions"><button class="btn primary" data-retry>${icon("refresh")}Try again</button></div>`;
    else body = `<p><span class="skeleton" style="width:60%"></span></p>`;
    box.innerHTML = `<div class="modal-icon live">${icon("key")}</div><h2>Log in to the live site</h2>
      <div class="steps-mini"><span class="${step >= 1 ? "on" : ""}"></span><span class="${step >= 2 ? "on" : ""}"></span><span class="${step >= 3 ? "on" : ""}"></span></div>
      <p>${esc(st.message || "Opening the live login page on the host computer ...")}</p>${shot}${body}`;
    const cred = $("#ll-cred", box), otp = $("#ll-otp", box);
    if (cred) { $("#ll-user", box).focus(); cred.addEventListener("submit", async (e) => { e.preventDefault(); await send("/api/live-login/credentials", { username: $("#ll-user", box).value, password: $("#ll-pass", box).value }); }); }
    if (otp) { $("#ll-code", box).focus(); otp.addEventListener("submit", async (e) => { e.preventDefault(); await send("/api/live-login/otp", { otp: $("#ll-code", box).value }); }); }
    const c = $("[data-close]", box); if (c) c.addEventListener("click", () => m.close());
    const r = $("[data-retry]", box); if (r) r.addEventListener("click", start);
  };
  const send = async (url, body) => { try { draw(await api(url, { json: body })); poll(); } catch (err) { toast(esc(err.message), "bad"); } };
  const poll = () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      if (closed) return;
      try {
        const st = await api("/api/live-login");
        const typing = box.contains(document.activeElement) && document.activeElement.tagName === "INPUT";
        if (!(typing && ["credentials", "otp"].includes(st.state))) draw(st);
        if (!["done", "failed", "idle", "credentials", "otp"].includes(st.state)) poll();
        else if (st.state === "done") { toast("Live login saved.", "ok"); refreshHealth(true); }
      } catch { poll(); }
    }, 1200);
  };
  async function start() { draw({ state: "starting" }); try { draw(await api("/api/live-login/start", { json: { who: S.who } })); } catch (err) { draw({ state: "failed", message: err.message }); } poll(); }
  start();
}

/* =================================================================== RUNS */
function runCard(r) {
  const pct = r.total ? Math.round((100 * r.done) / r.total) : 0;
  const side = r.status === "running" ? `<strong>${r.total ? `${r.done}/${r.total}` : "Starting"}</strong>${r.eta ? `~${fmtDur(r.eta)} left` : fmtDur(r.seconds)}`
    : r.status === "queued" ? `<strong>#${r.position || "–"} in queue</strong>${esc(r.outcome)}`
    : `<strong>${esc(STATUS_WORD[r.status] || r.status)}</strong>${fmtDur(r.seconds)}`;
  return `<a class="panel run" href="#/run/${r.id}">
    <span class="orb ${r.status}">${icon(STATUS_ICON[r.status] || "info")}</span>
    <div style="min-width:0">
      <div class="run-title">${esc(r.title)}</div>
      <div class="run-meta">${avatar(r.who)}<span>${esc(r.who)}</span>·<span class="server-tag ${r.target}">${esc(r.target === "testsite" ? "Test site" : r.target)}</span>·<span>${ago(r.created)}</span>
        ${r.status !== "running" && r.status !== "queued" ? `·<span>${esc(r.outcome)}</span>` : ""}</div>
      ${r.status === "running" ? `<div class="bar"><i style="width:${Math.max(4, pct)}%"></i></div>` : ""}
    </div>
    <div class="run-side">${side}</div></a>`;
}
function emptyArt() {
  return `<svg class="empty-art" viewBox="0 0 160 160" aria-hidden="true"><defs><linearGradient id="eg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#22c3ee"/><stop offset=".55" stop-color="#4f6bff"/><stop offset="1" stop-color="#a855f7"/></linearGradient></defs>
    <circle cx="80" cy="80" r="70" fill="url(#eg)" opacity=".12"/><circle cx="80" cy="80" r="48" fill="url(#eg)" opacity=".18"/>
    <g fill="none" stroke="url(#eg)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" transform="translate(40 40) scale(3.3)"><circle cx="5.5" cy="17" r="3"/><circle cx="18.5" cy="17" r="3"/><path d="M5.5 17h5.5l3.5-7h3"/><path d="M14.5 5.5H17l1.5 4.5"/></g></svg>`;
}
function viewRuns() {
  view.innerHTML = `
    <div class="page-head view-enter"><div><h1>Runs</h1><p>Everything the team has run from the Studio - newest first.</p></div><span class="spacer"></span>
      <div class="filters">
        <div class="seg" role="radiogroup" aria-label="Filter" style="margin:0"><span class="seg-thumb"></span>
          ${[["all", "All"], ["running", "Going"], ["mine", "Mine"], ["done", "Finished"]].map(([v, l]) => `<button role="radio" aria-checked="${S.ui.runFilter === v}" data-filter="${v}">${l}</button>`).join("")}</div>
        <label class="field search">${icon("search")}<input id="run-search" placeholder="Search runs" value="${esc(S.ui.runSearch)}" aria-label="Search runs"></label>
      </div></div>
    <div class="run-list" id="run-list"></div>`;
  requestAnimationFrame(placeThumbs);
  $$("[data-filter]").forEach((b) => b.addEventListener("click", () => {
    S.ui.runFilter = b.dataset.filter;
    $$("[data-filter]").forEach((x) => x.setAttribute("aria-checked", x === b));
    placeThumbs(); paintRunList();
  }));
  $("#run-search").addEventListener("input", (e) => { S.ui.runSearch = e.target.value; paintRunList(); });
  paintRunList(true);
  refreshRuns().then(() => paintRunList());
  const t = setInterval(() => refreshRuns().then(() => paintRunList()), 3500);
  S.cleanup.push(() => clearInterval(t));
}
function paintRunList(first = false) {
  const host = $("#run-list");
  if (!host) return;
  const q = S.ui.runSearch.trim().toLowerCase();
  const rows = S.runs.filter((r) => {
    if (S.ui.runFilter === "running" && !["running", "queued"].includes(r.status)) return false;
    if (S.ui.runFilter === "mine" && r.who !== S.who) return false;
    if (S.ui.runFilter === "done" && ["running", "queued"].includes(r.status)) return false;
    return !q || `${r.title} ${r.who} ${r.target} ${r.outcome}`.toLowerCase().includes(q);
  });
  host.innerHTML = rows.length ? rows.map(runCard).join("")
    : `<div class="panel empty">${emptyArt()}<h3>${S.runs.length ? "No runs match" : "No runs yet"}</h3><p>${S.runs.length ? "Try another filter." : "Your first test is two clicks away."}</p><a class="btn primary" href="#/new">${icon("rocket")}New test</a></div>`;
  if (first && !reduced) $$(".run", host).forEach((el, i) => { el.style.animation = `rise .5s var(--ease) ${i * 40}ms both`; });
}

/* ============================================================= RUN DETAIL */
const LINE_RULES = [
  [/^\$ /, "l-cmd"],
  [/^\s*\[\s*\d+\s*\/\s*\d+\]/, "l-step"],
  [/^(=+|QUOTE MATRIX|THE PLAN|THE CHOICES|THE JOURNEYS|DEFECTS|WORTH A LOOK|TWIN RULES|WHAT THE EVIDENCE|COVERAGE|INSURER LAB|  [A-Z][A-Z ]{6,}$)/, "l-head"],
  [/(Traceback|Error|FAILED|REFUSED|DEFECT|Bug:|NOT RUN|LOGIN FAILED|Failure|said no|never answered)/, "l-bad"],
  [/(WORTH A LOOK|LOOK|Worth a look|SKIPPED|warning|retry|trying once more)/i, "l-warn"],
  [/(priced|Success|passed|✓|login: done|saved session still works|cheapest)/, "l-ok"],
];
function lineHTML(line) {
  const rule = LINE_RULES.find(([re]) => re.test(line));
  return rule ? `<span class="${rule[1]}">${esc(line)}</span>` : esc(line);
}

async function viewRun(id) {
  view.innerHTML = `<a class="back" href="#/runs">${icon("left")}All runs</a><div class="panel" style="padding:30px"><span class="skeleton" style="width:40%"></span></div>`;
  let run;
  try { run = await api(`/api/runs/${encodeURIComponent(id)}`); }
  catch (err) { view.innerHTML = `<a class="back" href="#/runs">${icon("left")}All runs</a><div class="panel empty"><h3>Run not found</h3><p>${esc(err.message)}</p></div>`; return; }
  const cur = S.cur = { id, run, tab: store.get("runTab", "log"), lines: [], filter: "", auto: true, es: null, results: null, offset: 0, closed: false };
  S.cleanup.push(() => { cur.closed = true; if (cur.es) cur.es.close(); });
  paintRun(true);
  openLog(cur);
  const t = setInterval(async () => {
    if (!["running", "queued"].includes(cur.run.status)) return;
    try { cur.run = await api(`/api/runs/${encodeURIComponent(id)}`); paintRunHead(); if (cur.tab === "files") paintTab(); } catch { /* keep the old */ }
  }, 6000);
  S.cleanup.push(() => clearInterval(t));
}
function paintRun(first = false) {
  const r = S.cur.run;
  view.innerHTML = `
    <a class="back" href="#/runs">${icon("left")}All runs</a>
    <div class="detail-head ${first ? "view-enter" : ""}" id="run-head"></div>
    <div class="panel progress-hero ${first ? "view-enter" : ""}" id="run-progress"></div>
    <div class="tabs" role="tablist">
      <button role="tab" data-tab="log" aria-selected="${S.cur.tab === "log"}">${icon("terminal")}Live log</button>
      <button role="tab" data-tab="results" aria-selected="${S.cur.tab === "results"}">${icon("table")}Results <span class="n" id="n-results">–</span></button>
      <button role="tab" data-tab="files" aria-selected="${S.cur.tab === "files"}">${icon("folder")}Files &amp; videos <span class="n" id="n-files">${(r.files || []).length}</span></button>
    </div>
    <div id="tab-body"></div>`;
  $$("[data-tab]").forEach((b) => b.addEventListener("click", () => {
    S.cur.tab = b.dataset.tab; store.set("runTab", S.cur.tab);
    $$("[data-tab]").forEach((x) => x.setAttribute("aria-selected", x === b));
    paintTab();
  }));
  paintRunHead();
  paintTab();
  if (["passed", "bugs", "failed", "stopped", "interrupted"].includes(r.status)) loadResults();
}
function paintRunHead() {
  const r = S.cur.run, head = $("#run-head"), prog = $("#run-progress");
  if (!head) return;
  const going = ["running", "queued"].includes(r.status);
  head.innerHTML = `<div style="min-width:0">
      <h1>${esc(r.title)}</h1>
      <div class="run-meta"><span class="status-badge ${r.status}"><span class="dot"></span>${esc(STATUS_WORD[r.status] || r.status)}</span>
        <span class="server-tag ${r.target}">${esc(r.target === "testsite" ? "Test site" : r.target)}</span>
        ${avatar(r.who)}<span>${esc(r.who)}</span>·<span>${ago(r.created)}</span>·<span>${esc(r.outcome)}</span></div>
    </div>
    <div class="actions">
      ${going ? `<button class="btn danger" id="btn-stop">${icon("stop")}Stop</button>` : `<button class="btn" id="btn-again">${icon("refresh")}Run again</button>`}
      <button class="btn" id="btn-copy">${icon("copy")}Command</button>
    </div>`;
  const stop = $("#btn-stop"), again = $("#btn-again");
  if (stop) stop.addEventListener("click", async () => {
    if (!confirm("Stop this run? Its browsers are closed too.")) return;
    try { await api(`/api/runs/${r.id}/stop`, { json: {} }); toast("Stopping ...", "info"); } catch (err) { toast(esc(err.message), "bad"); }
  });
  if (again) again.addEventListener("click", async () => {
    const body = { who: S.who || r.who };
    if (r.target === "live") { if (!(await confirmLive2(r))) return; body.confirm = "LIVE"; }
    try { const n = await api(`/api/runs/${r.id}/again`, { json: body }); toast("Started again.", "ok"); go(`#/run/${n.id}`); } catch (err) { toast(esc(err.message), "bad"); }
  });
  $("#btn-copy").addEventListener("click", () => copy(r.command_text));
  const pct = r.total ? r.done / r.total : (["passed", "bugs"].includes(r.status) ? 1 : 0);
  const C = 2 * Math.PI * 40;
  prog.innerHTML = `
    <div class="ring"><svg viewBox="0 0 92 92"><defs><linearGradient id="ringGrad" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#22c3ee"/><stop offset=".55" stop-color="#4f6bff"/><stop offset="1" stop-color="#a855f7"/></linearGradient></defs>
      <circle class="track" cx="46" cy="46" r="40"/><circle class="fill" cx="46" cy="46" r="40" stroke-dasharray="${C}" stroke-dashoffset="${C * (1 - pct)}"/></svg>
      <div class="ring-num"><div>${r.total ? `${r.done}<small>of ${r.total}</small>` : `${Math.round(pct * 100)}%<small>${r.status === "queued" ? "queued" : "done"}</small>`}</div></div></div>
    <div class="now"><div class="now-label">${r.status === "running" ? "Now" : r.status === "queued" ? "Waiting" : "Result"}</div>
      <div class="now-text">${esc(r.status === "running" ? (r.headline || "Starting up - logging in ...") : r.status === "queued" ? `#${r.position} in the queue - ${r.outcome}` : r.outcome)}</div></div>
    <div class="times"><div><b>${fmtDur(r.seconds)}</b><span>elapsed</span></div><div><b>${r.eta ? "~" + fmtDur(r.eta) : "–"}</b><span>left</span></div></div>`;
}
function confirmLive2(r) { const saved = S.form.insurers; S.form.insurers = r.form.insurers || []; const p = confirmLive(); S.form.insurers = saved; return p; }

function openLog(cur) {
  const connect = () => {
    if (cur.closed) return;
    const es = cur.es = new EventSource(`/api/runs/${encodeURIComponent(cur.id)}/log?offset=${cur.offset}`);
    es.onmessage = (ev) => {
      const d = JSON.parse(ev.data);
      cur.offset = d.offset;
      if (d.text) {
        const fresh = d.text.replace(/\n$/, "").split("\n");
        cur.lines.push(...fresh);
        if (cur.lines.length > 20000) cur.lines.splice(0, cur.lines.length - 20000);
        appendLog(fresh);
      }
      const before = cur.run.status;
      Object.assign(cur.run, { status: d.status, outcome: d.outcome, done: d.done, total: d.total, eta: d.eta, seconds: d.seconds, headline: d.headline });
      paintRunHead();
      if (before !== d.status && !["running", "queued"].includes(d.status)) finished(cur);
    };
    es.addEventListener("end", () => { es.close(); finished(cur); });
    es.onerror = () => { es.close(); if (!cur.closed && ["running", "queued"].includes(cur.run.status)) setTimeout(connect, 2000); };
  };
  connect();
}
async function finished(cur) {
  if (cur.finishedOnce) return;
  cur.finishedOnce = true;
  try { cur.run = await api(`/api/runs/${encodeURIComponent(cur.id)}`); } catch { /* keep */ }
  paintRunHead();
  const n = $("#n-files"); if (n) n.textContent = (cur.run.files || []).length;
  await loadResults();
  if (cur.tab !== "log") paintTab();
  const st = cur.run.status;
  if (["passed", "bugs", "failed"].includes(st)) toast(`<b>${esc(STATUS_WORD[st])}:</b> ${esc(cur.run.title)}`, st === "passed" ? "ok" : st === "bugs" ? "info" : "bad", 6000);
  refreshRuns();
}
function appendLog(fresh) {
  const pre = $("#log-pre");
  if (!pre || S.cur.filter) { if (pre && S.cur.filter) paintLog(); return; }
  const cursor = $(".cursor", pre); if (cursor) cursor.remove();
  pre.insertAdjacentHTML("beforeend", fresh.map(lineHTML).join("\n") + "\n" + (["running", "queued"].includes(S.cur.run.status) ? '<span class="cursor"></span>' : ""));
  if (S.cur.auto) pre.scrollTop = pre.scrollHeight;
}
function paintLog() {
  const pre = $("#log-pre");
  if (!pre) return;
  const q = S.cur.filter.toLowerCase();
  const lines = q ? S.cur.lines.filter((l) => l.toLowerCase().includes(q)) : S.cur.lines;
  pre.innerHTML = lines.map(lineHTML).join("\n") + "\n" + (!q && ["running", "queued"].includes(S.cur.run.status) ? '<span class="cursor"></span>' : "");
  if (S.cur.auto) pre.scrollTop = pre.scrollHeight;
}
function paintTab() {
  const body = $("#tab-body");
  if (!body) return;
  const cur = S.cur;
  if (cur.tab === "log") {
    body.innerHTML = `<div class="console"><div class="console-bar"><span class="lights"><i></i><i></i><i></i></span><span>output.log</span>
        <label><input type="checkbox" id="log-auto" ${cur.auto ? "checked" : ""}> Follow</label>
        <a class="ghost" style="height:28px;color:#9fb0d6;border-color:rgba(255,255,255,.12)" href="/files/portal/${encodeURIComponent(cur.id)}/output.log?download=true">${icon("download")}Log</a>
        <input id="log-filter" placeholder="Filter lines" value="${esc(cur.filter)}" aria-label="Filter log lines"></div>
      <pre id="log-pre" tabindex="0" aria-label="Run output"></pre></div>`;
    paintLog();
    $("#log-auto").addEventListener("change", (e) => { cur.auto = e.target.checked; });
    $("#log-filter").addEventListener("input", debounce((e) => { cur.filter = e.target.value; paintLog(); }, 120));
    $("#log-pre").addEventListener("wheel", (e) => { if (e.deltaY < 0 && cur.auto) { cur.auto = false; $("#log-auto").checked = false; } });
  } else if (cur.tab === "results") paintResults();
  else paintFiles();
}

async function loadResults() {
  try {
    S.cur.results = await api(`/api/runs/${encodeURIComponent(S.cur.id)}/results`);
    const n = $("#n-results"); if (n) n.textContent = S.cur.results.total ?? 0;
    if (S.cur.tab === "results") paintResults();
  } catch { /* no results yet */ }
}
// Who said no, in the Excel's plain words (core/matrixexcel.py DECIDED_BY).
const DECIDED_BY = { "probus-rule": "Our rules (Probus)", http: "Our API", "our-defect": "Our integration",
  silent: "No answer", "insurer-down": "Insurer (service down)" };
const MATRIX_COLUMNS = ["journey", "insurer", "outcome", "premium", "reason", "decided_by", "vehicle", "rto", "year", "policy", "previous", "prev_insurer", "ncb", "claim", "idv", "quotation"];
function paintResults() {
  const body = $("#tab-body"), res = S.cur.results;
  if (!body) return;
  if (!res) { body.innerHTML = `<div class="panel empty"><h3>Results appear when the run finishes</h3><p>Watch the live log meanwhile.</p></div>`; if (!["running", "queued"].includes(S.cur.run.status)) loadResults(); return; }
  if (!res.rows.length) { body.innerHTML = `<div class="panel empty">${emptyArt()}<h3>No results table for this run</h3><p>${["running", "queued"].includes(S.cur.run.status) ? "It is still going." : "It stopped before writing one - the live log says why."}</p></div>`; return; }
  const ui = S.cur.resUI = S.cur.resUI || { show: "all", q: "", all: false };
  const cards = res.summary.map((s) => {
    const n = s.success + s.failure, rate = n ? Math.round((100 * s.success) / n) : 0;
    const ins = insurerByCode(s.company);
    return `<div class="panel co-card"><div class="co-head">${avatar(s.company, s.company.length <= 4 ? s.company : initials(ins.short))}<div><div class="co-name">${esc(ins.short)}</div><div class="chip-code">${esc(s.company)}</div></div>
      <div class="co-rate" style="color:${rate >= 70 ? "var(--ok)" : rate >= 30 ? "var(--warn)" : "var(--bad)"}">${rate}%</div></div>
      <div class="split"><span class="s" style="width:${n ? (100 * s.success) / n : 0}%"></span><span class="f" style="width:${n ? (100 * s.failure) / n : 0}%"></span></div>
      <div class="co-foot"><span>${s.success} success · ${s.failure} failure</span><span>${s.cheapest ? "from " + rupees(s.cheapest) : ""}</span></div></div>`;
  }).join("");
  const files = S.cur.run.files || [];
  const xlsx = files.find((f) => f.name === "results.xlsx"), html = files.find((f) => f.name === "report.html");
  const found = res.findings || [];
  const findings = found.length ? `<div class="panel findings"><div class="findings-head"><h3>What it found</h3>
      <span class="pill bad">${found.filter((x) => x.kind === "bug").length} bug${found.filter((x) => x.kind === "bug").length === 1 ? "" : "s"}</span>
      <span class="pill warn">${found.filter((x) => x.kind === "look").length} worth a look</span></div>
      ${found.map((x) => `<div class="finding ${x.kind}"><span class="pill ${x.kind === "bug" ? "bad" : "warn"}">${x.kind === "bug" ? "Bug" : "Worth a look"}</span>
        <div style="min-width:0"><div class="finding-title"><b>${esc(x.company)}</b> ${esc(x.title)}${x.journeys ? ` <span class="chip-code" style="display:inline">${esc(x.journeys)}</span>` : ""}</div>
        ${x.detail ? `<div class="finding-detail">${esc(x.detail)}</div>` : ""}</div></div>`).join("")}</div>` : "";
  body.innerHTML = `
    ${findings}
    ${cards ? `<div class="cards4">${cards}</div>` : ""}
    <div class="toolbar">
      <div class="seg" style="margin:0"><span class="seg-thumb"></span>${[["all", "All"], ["Success", "Success"], ["Failure", "Failure"]].map(([v, l]) => `<button role="radio" aria-checked="${ui.show === v}" data-show="${v}">${l}</button>`).join("")}</div>
      <label class="field search">${icon("search")}<input id="res-q" placeholder="Search rows" value="${esc(ui.q)}" aria-label="Search results"></label>
      <button class="ghost" id="res-all">${icon("table")}${ui.all ? "Key columns" : "All columns"}</button>
      <span class="count"></span>
      ${xlsx ? `<a class="btn" href="${xlsx.url}?download=true">${icon("excel")}Excel</a>` : ""}
      ${html ? `<a class="btn" href="${html.url}" target="_blank" rel="noopener">${icon("report")}Report</a>` : ""}
    </div>
    <div class="table-wrap"><table class="data" id="res-table"></table></div>`;
  requestAnimationFrame(placeThumbs);
  $$("[data-show]", body).forEach((b) => b.addEventListener("click", () => { ui.show = b.dataset.show; paintResults(); }));
  $("#res-q").addEventListener("input", debounce((e) => { ui.q = e.target.value; paintTable(); }, 120));
  $("#res-all").addEventListener("click", () => { ui.all = !ui.all; paintResults(); });
  paintTable();
}
function paintTable() {
  const res = S.cur.results, ui = S.cur.resUI, table = $("#res-table");
  if (!table) return;
  const cols = ui.all ? res.columns : (MATRIX_COLUMNS.filter((c) => res.columns.includes(c)).length >= 4 ? MATRIX_COLUMNS.filter((c) => res.columns.includes(c)) : res.columns);
  const status = res.status_column, q = ui.q.trim().toLowerCase();
  const rows = res.rows.filter((r) => (ui.show === "all" || r[status] === ui.show) && (!q || Object.values(r).join(" ").toLowerCase().includes(q)));
  const nice = (c) => c.replace(/_/g, " ");
  const cell = (c, v) => {
    if (c === status && v === "Success") return `<td><span class="cell-ok">${icon("check", 'style="width:12px;height:12px"')}Success</span></td>`;
    if (c === status && v === "Failure") return `<td><span class="cell-bad">${icon("x", 'style="width:12px;height:12px"')}Failure</span></td>`;
    if (["premium", "net", "gst", "od_part", "tp_part", "idv", "idv_min", "idv_max"].includes(c)) return `<td class="num">${v ? rupees(v) : ""}</td>`;
    if (c === "reason" && v) { const i = v.indexOf(" (expected"); return `<td class="wrap">${i > 0 ? `${esc(v.slice(0, i))} <span class="expected">${esc(v.slice(i))}</span>` : esc(v)}</td>`; }
    if (["reason", "why", "notes"].includes(c)) return `<td class="wrap">${esc(v)}</td>`;
    if (c === "decided_by" && v) return `<td>${esc(DECIDED_BY[v] || "Insurer")}</td>`;
    return `<td>${esc(v)}</td>`;
  };
  table.innerHTML = `<thead><tr>${cols.map((c) => `<th>${esc(nice(c))}</th>`).join("")}</tr></thead>
    <tbody>${rows.slice(0, 1500).map((r) => `<tr>${cols.map((c) => cell(c, r[c])).join("")}</tr>`).join("")}</tbody>`;
  const count = $("#tab-body .count");
  if (count) count.textContent = `${rows.length} of ${res.total} rows`;
}

function paintFiles() {
  const body = $("#tab-body"), files = S.cur.run.files || [];
  if (!body) return;
  if (!files.length) { body.innerHTML = `<div class="panel empty">${emptyArt()}<h3>No files yet</h3><p>Videos, screenshots and reports land here as the run goes.</p></div>`; return; }
  const docs = files.filter((f) => ["excel", "report", "csv", "text", "log"].includes(f.kind));
  const videos = files.filter((f) => f.kind === "video");
  const images = files.filter((f) => f.kind === "image");
  const traces = files.filter((f) => f.kind === "trace" || f.kind === "data");
  const ico = { excel: "excel", report: "report", csv: "table", text: "doc", log: "terminal", trace: "layers", data: "doc", image: "image" };
  const tile = (f) => `<a class="panel file" href="${f.url}${["excel", "trace", "csv"].includes(f.kind) ? "?download=true" : ""}" ${["report", "text", "log", "image"].includes(f.kind) ? 'target="_blank" rel="noopener"' : ""}>
      <span class="file-ico ${f.kind}">${icon(ico[f.kind] || "doc")}</span><span style="min-width:0"><span class="file-name" style="display:block">${esc(f.name)}</span><span class="file-size">${fmtSize(f.size)} · ${esc(f.group.split("/").pop())}</span></span></a>`;
  const label = (g) => { const m = /run-(\d{8})-(\d{2})(\d{2})(\d{2})/.exec(g); return m ? `Journey at ${m[2]}:${m[3]}:${m[4]}` : g.split("/").pop(); };
  body.innerHTML = `<div class="file-groups">
    ${docs.length ? `<div class="file-group"><h3>Reports</h3><div class="files">${docs.map(tile).join("")}</div></div>` : ""}
    ${videos.length ? `<div class="file-group"><h3>Videos · ${videos.length}</h3><div class="files" style="grid-template-columns:repeat(auto-fill,minmax(320px,1fr))">${videos.map((v) => `
      <figure class="panel video" style="margin:0"><video src="${v.url}" controls preload="metadata" muted></video><figcaption><span>${esc(label(v.group))}</span><span>${fmtSize(v.size)}</span></figcaption></figure>`).join("")}</div></div>` : ""}
    ${images.length ? `<div class="file-group"><h3>Screenshots · ${images.length}</h3><div class="files">${images.map((i) => `<a class="shot" href="${i.url}" target="_blank" rel="noopener"><img src="${i.url}" loading="lazy" alt="${esc(i.name)}"></a>`).join("")}</div></div>` : ""}
    ${traces.length ? `<div class="file-group"><h3>Traces &amp; evidence</h3><div class="files">${traces.map(tile).join("")}</div><p class="hint">${icon("info")}Open a trace.zip at trace.playwright.dev to step through the run frame by frame.</p></div>` : ""}
  </div>`;
}

/* ================================================================== GUIDE */
function viewGuide() {
  view.innerHTML = `
    <section class="hero view-enter"><span class="eyebrow" style="--i:0"><b>GUIDE</b> Two minutes to read</span>
      <h1 class="display" style="--i:1">How the <span class="gradient-text">Studio</span> works</h1>
      <p class="lede" style="--i:2">It is a friendly front door to the same test tools the team runs from the terminal - same checks, same guard rails, same reports.</p></section>
    <div class="guide view-enter">
      <div class="panel" style="--i:3"><span class="tile-icon">${icon("rocket")}</span><h2>Run a test</h2><ol>
        <li>Open <b>New test</b>, pick a server, product and test.</li>
        <li>Optionally pick companies, vehicles, RTOs and policy details. Empty = the Studio chooses.</li>
        <li>Check the summary - it shows the real number of journeys and the time.</li>
        <li>Press <b>Launch</b>. Watch the live log, then open Results, the Excel and the videos.</li></ol></div>
      <div class="panel" style="--i:4"><span class="tile-icon">${icon("layers")}</span><h2>Which test when</h2><ul>
        <li><b>Quote sweep</b> - many scenarios across many companies, to the quote list. The everyday check.</li>
        <li><b>Insurer deep-dive</b> - one company, ~150 API quotes, learns its rules. For integration work.</li>
        <li><b>Buy journey</b> - one company through KYC and the proposal form. Never pays.</li>
        <li><b>Best vehicle finder</b> - where to start: the city and vehicle most companies price.</li></ul></div>
      <div class="panel" style="--i:5"><span class="tile-icon">${icon("shield")}</span><h2>Safe by design</h2><ul>
        <li><b>Live production is quotes only</b> - enforced in the code, whatever anyone clicks.</li>
        <li>Live runs need the companies named and the word LIVE typed.</li>
        <li>Nothing is ever paid. The payment page is the furthest any test goes, on test servers only.</li>
        <li>Runs that write the same notes never overlap; the queue runs them one after another.</li></ul></div>
      <div class="panel" style="--i:6"><span class="tile-icon">${icon("terminal")}</span><h2>Same as the terminal</h2>
        <p>Every run shows its command - copy it and run it in the project folder:</p>
        <p><code>venv\\Scripts\\python run_quote_matrix.py --target testsite --insurers BAJAJ,TATA --all</code></p>
        <p>The Studio itself starts with <code>venv\\Scripts\\python -m portal</code>.</p></div>
      <div class="panel" style="--i:7"><span class="tile-icon">${icon("globe")}</span><h2>Sharing with the team</h2><ul>
        <li>The Studio runs on one office computer; everyone opens its address in a browser.</li>
        <li>"Local dev" means that computer's localhost - its Angular app and API.</li>
        <li>An optional access code keeps it to the team (<code>portal_access_code</code> in settings.local.json).</li></ul></div>
      <div class="panel" style="--i:8"><span class="tile-icon">${icon("key")}</span><h2>Live login</h2>
        <p>Live runs reuse a saved login. When it expires, pick <b>Live production</b> and press <b>Log in</b> - type the username, password and OTP right here. The password is never saved.</p></div>
    </div>`;
}

/* =================================================================== boot */
async function boot() {
  paintTheme();
  paintWho();
  $("#theme-btn").addEventListener("click", () => {
    const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    // Raw, not JSON: index.html reads it before the first paint.
    try { localStorage.setItem("studio.theme", next); } catch { /* fine */ }
    paintTheme();
  });
  $("#who-btn").addEventListener("click", () => whoModal(false));
  try {
    S.meta = await api("/api/meta");
    if (S.meta.locked) { await unlockModal(); S.meta = await api("/api/meta"); }
  } catch (err) {
    view.innerHTML = `<div class="panel empty"><h3>The Studio's server is not answering</h3><p>${esc(err.message)}</p></div>`;
    return;
  }
  normaliseForm(S.form);
  window.addEventListener("hashchange", render);
  window.addEventListener("resize", debounce(() => { paintNav(route().name === "run" ? "runs" : route().name); placeThumbs(); }, 100));
  await render();
  refreshHealth();
  refreshRuns();
  setInterval(refreshHealth, 60000);
  setInterval(refreshRuns, 8000);
  if (!S.who) setTimeout(() => whoModal(false), 900);
}
boot();
