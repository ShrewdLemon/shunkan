/* SHUNKAN web terminal — vanilla JS + lightweight-charts. No build step. */

"use strict";

/* ---------- helpers ---------- */

const $ = (sel, root = document) => root.querySelector(sel);
const elv = (tag, cls, html) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = html;
  return e;
};

const apiStats = { lastMs: 0, lastPath: "", calls: 0 };
async function getJSON(url) {
  const t0 = performance.now();
  const r = await fetch(url);
  apiStats.lastMs = Math.round(performance.now() - t0);
  apiStats.lastPath = url.split("?")[0];
  apiStats.calls++;
  updateStatusbar();
  if (!r.ok) {
    let msg = `${r.status}`, detail = null;
    try { detail = (await r.json()).detail; } catch {}
    if (detail && typeof detail === "object") msg = detail.error || msg;
    else if (detail) msg = detail;
    // Structured details (source trails) ride along for views that render them.
    const err = new Error(msg);
    err.detail = detail;
    throw err;
  }
  return r.json();
}
async function postJSON(url, body, method = "POST") {
  const t0 = performance.now();
  const r = await fetch(url, {
    method, headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  apiStats.lastMs = Math.round(performance.now() - t0);
  apiStats.lastPath = url;
  apiStats.calls++;
  updateStatusbar();
  if (!r.ok) {
    let msg = `${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return r.json();
}

const fmt = {
  n: (v, d = 2) => v === null || v === undefined ? "—" :
    Number(v).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d }),
  i: (v) => v === null || v === undefined ? "—" : Math.round(v).toLocaleString("en-IN"),
  pct: (v, d = 2) => v === null || v === undefined ? "—" :
    `${v >= 0 ? "+" : ""}${(v * 100).toFixed(d)}%`,
  compact: (v) => {
    if (v === null || v === undefined) return "—";
    const a = Math.abs(v);
    if (a >= 1e7) return (v / 1e7).toFixed(2) + "Cr";
    if (a >= 1e5) return (v / 1e5).toFixed(2) + "L";
    if (a >= 1e3) return (v / 1e3).toFixed(1) + "K";
    return String(Math.round(v));
  },
  age: (mins) => {
    if (mins === null || mins === undefined) return "";
    if (mins < 60) return `${Math.round(mins)}m`;
    if (mins < 60 * 24) return `${(mins / 60).toFixed(1)}h`;
    return `${Math.round(mins / 1440)}d`;
  },
  // Accepts a Date or an epoch millis. lastTickAt is a number so that
  // staleness is a plain subtraction; every other caller passes a Date.
  ist: (date = new Date()) => (date = typeof date === "number" ? new Date(date) : date) &&
    date.toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false }),
};
const esc = (v) => String(v ?? "").replace(/[&<>"']/g,
  (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
const cls = (v) => (v == null || isNaN(v) ? "" : v >= 0 ? "up" : "down");
// Price precision tracks magnitude: an index needs no paise, USD/INR and
// INDIA VIX are unreadable without them.
const dp = (v) => (Math.abs(v) < 100 ? 2 : Math.abs(v) < 1000 ? 1 : 0);

function toast(msg, kind = "") {
  const t = elv("div", `toast ${kind}`, msg);
  $("#toasts").appendChild(t);
  setTimeout(() => { t.style.opacity = "0"; t.style.transition = "opacity .4s"; }, 5200);
  setTimeout(() => t.remove(), 5700);
}

/* ---------- panel + insight components ---------- */

function panel({ title, meta = "", body = "", flush = false, id = "" }) {
  return `
  <section class="panel" ${id ? `id="${id}"` : ""}>
    <div class="panel-head">
      <span class="panel-title">${title}</span>
      <span class="panel-meta">${meta}</span>
    </div>
    <div class="panel-body${flush ? " flush" : ""}">${body}</div>
  </section>`;
}

function stamp(extra = "") {
  return `<span class="upd">UPD ${fmt.ist()}</span>${extra ? " · " + extra : ""}`;
}

/* ---------- provenance (ⓘ marks) ----------
   Every calculative score carries its derivation from the API. iM(prov)
   renders an ⓘ; clicking it shows formula, real input values, sources,
   method, caveats and the compute timestamp. */

const provRegistry = new Map();
let provSeq = 0;

function iM(provObj, label = "") {
  if (!provObj) return "";
  const id = `pv${++provSeq}`;
  provRegistry.set(id, { ...provObj, label });
  return `<button class="imark" data-prov="${id}" title="How is this number computed?">i</button>`;
}

function showProvPop(id, anchor) {
  closeProvPop();
  const p = provRegistry.get(id);
  if (!p) return;
  const pop = elv("div", "prov-pop");
  pop.id = "prov-pop";
  pop.innerHTML = `
    <div class="pp-title"><span>DERIVATION${p.label ? " — " + p.label : ""}</span>
      <span class="pp-close" onclick="closeProvPop()">CLOSE</span></div>
    <div class="pp-formula">${p.formula}</div>
    ${p.inputs && p.inputs.length ? `<div class="pp-section">INPUTS (ACTUAL VALUES)</div>
      ${p.inputs.map((i) => `<div class="pp-input"><span class="k">${i.name}</span>
        <span><span class="v">${i.value}</span>${i.source ? ` <span class="src">· ${i.source}</span>` : ""}</span></div>`).join("")}` : ""}
    ${p.method ? `<div class="pp-section">METHOD</div><div class="pp-note">${p.method}</div>` : ""}
    <div class="pp-section">DATA SOURCE</div><div class="pp-note">${p.source}</div>
    ${p.caveat ? `<div class="pp-caveat">CAVEAT: ${p.caveat}</div>` : ""}
    <div class="pp-foot">computed ${p.computed_at ? p.computed_at.replace("T", " ").replace("+00:00", " UTC") : "—"}</div>`;
  document.body.appendChild(pop);
  const r = anchor.getBoundingClientRect();
  const W = pop.offsetWidth, H = pop.offsetHeight;
  let left = Math.min(r.left, window.innerWidth - W - 12);
  let top = r.bottom + 8;
  if (top + H > window.innerHeight - 8) top = Math.max(r.top - H - 8, 8);
  pop.style.left = `${Math.max(left, 8)}px`;
  pop.style.top = `${top}px`;
}
function closeProvPop() { const e = $("#prov-pop"); if (e) e.remove(); }
window.closeProvPop = closeProvPop;

document.addEventListener("click", (e) => {
  const mark = e.target.closest(".imark");
  if (mark) { showProvPop(mark.dataset.prov, mark); return; }
  if (!e.target.closest(".prov-pop")) closeProvPop();
});

/* Every insight states WHAT and WHY — conclusions without reasons are noise. */
function insightBlock(items) {
  if (!items.length) return "";
  return `
  <div class="insight">
    <div class="insight-title">INSIGHT</div>
    ${items.map((it) => `
      <div class="insight-item">
        <span class="marker">▸</span>
        <span><span class="what">${it.what}</span>${it.why ? ` <span class="why">— ${it.why}</span>` : ""}</span>
      </div>`).join("")}
  </div>`;
}

/* ---------- lightweight-charts ---------- */

const LWC = window.LightweightCharts;
const chartTheme = {
  layout: {
    background: { type: "solid", color: "transparent" },
    textColor: "#9aa1b0", fontSize: 10,
    fontFamily: "'SF Mono', ui-monospace, Menlo, monospace",
    attributionLogo: false,
  },
  grid: {
    vertLines: { color: "rgba(255,255,255,0.04)" },
    horzLines: { color: "rgba(255,255,255,0.04)" },
  },
  rightPriceScale: { borderColor: "rgba(255,255,255,0.08)" },
  timeScale: { borderColor: "rgba(255,255,255,0.08)", timeVisible: true },
  crosshair: {
    vertLine: { color: "rgba(240,168,38,0.45)", labelBackgroundColor: "#3a2c0c" },
    horzLine: { color: "rgba(240,168,38,0.45)", labelBackgroundColor: "#3a2c0c" },
  },
};
const liveCharts = [];
function mkChart(host, opts = {}) {
  const chart = LWC.createChart(host, { ...chartTheme, autoSize: true, ...opts });
  liveCharts.push(chart);
  return chart;
}
function destroyCharts() {
  while (liveCharts.length) { try { liveCharts.pop().remove(); } catch {} }
}

/* ---------- canvas plots ---------- */

function linePlot(canvas, seriesList, opts = {}) {
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth || canvas.parentElement.clientWidth || 600;
  const H = opts.height || 300;
  canvas.width = W * dpr; canvas.height = H * dpr;
  canvas.style.height = H + "px";
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);

  const all = seriesList.flatMap((s) => s.points).filter((p) => p.y !== null && p.y !== undefined);
  if (!all.length) return;
  const xs = all.map((p) => p.x), ys = all.map((p) => p.y);
  let xmin = Math.min(...xs), xmax = Math.max(...xs);
  let ymin = Math.min(...ys, opts.zeroLine ? 0 : Infinity);
  let ymax = Math.max(...ys, opts.zeroLine ? 0 : -Infinity);
  const ypad = (ymax - ymin) * 0.08 || 1;
  ymin -= ypad; ymax += ypad;
  const ML = 58, MR = 12, MT = 10, MB = 24;
  const X = (x) => ML + ((x - xmin) / (xmax - xmin || 1)) * (W - ML - MR);
  const Y = (y) => MT + (1 - (y - ymin) / (ymax - ymin || 1)) * (H - MT - MB);

  ctx.font = "9.5px 'SF Mono', Menlo, monospace";
  ctx.strokeStyle = "rgba(255,255,255,0.04)";
  ctx.fillStyle = "#5d6470";
  for (let i = 0; i <= 4; i++) {
    const yv = ymin + ((ymax - ymin) * i) / 4;
    ctx.beginPath(); ctx.moveTo(ML, Y(yv)); ctx.lineTo(W - MR, Y(yv)); ctx.stroke();
    ctx.fillText(opts.fmtY ? opts.fmtY(yv) : fmt.compact(yv), 4, Y(yv) + 3);
  }
  for (let i = 0; i <= 5; i++) {
    const xv = xmin + ((xmax - xmin) * i) / 5;
    ctx.fillText(opts.fmtX ? opts.fmtX(xv) : fmt.i(xv), X(xv) - 16, H - 8);
  }
  if (ymin < 0 && ymax > 0) {
    ctx.strokeStyle = "rgba(255,255,255,0.16)"; ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(ML, Y(0)); ctx.lineTo(W - MR, Y(0)); ctx.stroke();
    ctx.setLineDash([]);
  }
  (opts.vlines || []).forEach((v) => {
    ctx.strokeStyle = v.color; ctx.setLineDash(v.dash || [4, 3]);
    ctx.beginPath(); ctx.moveTo(X(v.x), MT); ctx.lineTo(X(v.x), H - MB); ctx.stroke();
    ctx.setLineDash([]);
    if (v.label) { ctx.fillStyle = v.color; ctx.fillText(v.label, X(v.x) + 3, MT + 9); }
  });
  for (const s of seriesList) {
    const pts = s.points;
    if (s.fillZero) {
      for (const sign of [1, -1]) {
        ctx.beginPath();
        ctx.moveTo(X(pts[0].x), Y(0));
        for (const p of pts) ctx.lineTo(X(p.x), Y(sign > 0 ? Math.max(p.y, 0) : Math.min(p.y, 0)));
        ctx.lineTo(X(pts[pts.length - 1].x), Y(0));
        ctx.closePath();
        ctx.fillStyle = sign > 0 ? "rgba(46,189,133,0.13)" : "rgba(241,86,75,0.13)";
        ctx.fill();
      }
    }
    ctx.beginPath();
    ctx.strokeStyle = s.color; ctx.lineWidth = s.width || 1.6; ctx.lineJoin = "round";
    let pen = false;
    for (const p of pts) {
      if (p.y === null || p.y === undefined) { pen = false; continue; }
      if (!pen) { ctx.moveTo(X(p.x), Y(p.y)); pen = true; }
      else ctx.lineTo(X(p.x), Y(p.y));
    }
    ctx.stroke();
  }
}

function sparkline(canvas, closes, w = 88, h = 24) {
  if (!closes || closes.length < 2) return;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = w * dpr; canvas.height = h * dpr;
  canvas.style.width = w + "px"; canvas.style.height = h + "px";
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  const min = Math.min(...closes), max = Math.max(...closes);
  const X = (i) => (i / (closes.length - 1)) * (w - 2) + 1;
  const Y = (v) => 2 + (1 - (v - min) / (max - min || 1)) * (h - 4);
  // Deliberately NOT coloured by direction. This sparkline spans a month while
  // the CHG% beside it is the day, so colouring both left every row saying two
  // things at once: NIFTY down 0.5% on the day in red, next to a green line
  // because the month was up. The column header names the period; the line
  // carries shape, and CHG% is the only thing on the row allowed to carry
  // direction.
  ctx.strokeStyle = "rgba(255, 166, 43, 0.75)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  closes.forEach((v, i) => (i === 0 ? ctx.moveTo(X(i), Y(v)) : ctx.lineTo(X(i), Y(v))));
  ctx.stroke();
}

/* ---------- state / routing ---------- */

const state = {
  view: "pulse", symbol: "NIFTY", chartPeriod: "6mo",
  chartType: "candles", chartInterval: "1d", chartIndicators: [],
  timers: new Map(), tickStore: new Map(),
  // `feedClaim` is what the server said the feed IS (kite | demo | null).
  // `lastTickAt` is what we have actually SEEN. The badge is derived from both,
  // because the server stamps live=true when the ticker constructs, not when a
  // tick arrives: a dead token used to show a pulsing green LIVE all session
  // next to a tick counter stuck on zero.
  feedClaim: null, tickCount: 0, lastTickAt: null,
  // Declared, not implied. It is read in three places but was only ever
  // assigned by ws.onopen/onclose, so before the first socket event it was
  // undefined and worked purely because undefined is falsy.
  wsDown: false,
  // Rolling print log for the tape, newest first. Bounded: a full session at
  // index tick rates is hundreds of thousands of prints, and an unbounded
  // array is a memory leak with a tidy name.
  tape: [], tapeMax: 500,
  // Spot at the moment the book was last fully marked, so the delta P&L
  // between polls is measured from a known point rather than from nothing.
  markSpot: new Map(),
};
// Keyed and idempotent: re-registering the same key is a no-op, so a render
// function that reschedules itself can no longer double the interval count
// on every tick. clearTimers() on view switch is still the only teardown.
function addTimer(key, fn, ms) {
  if (state.timers.has(key)) return state.timers.get(key);
  const id = setInterval(fn, ms);
  state.timers.set(key, id);
  return id;
}
function clearTimers() { state.timers.forEach(clearInterval); state.timers.clear(); }

// Anything a view attaches OUTSIDE its own DOM has to be taken back, because
// show() only clears timers, charts and main.innerHTML. A listener on
// `document` survives the view that made it: renderChain's ticket dismiss
// added one on every visit and they all kept firing forever.
//
// Deliberately shaped like addTimer so there is one lifecycle idea in this
// file, not two. Forgetting to call it leaves today's behaviour rather than
// breaking the view, which matters with 19 render functions and no tests.
const _teardowns = [];
function onTeardown(fn) { _teardowns.push(fn); }
function runTeardowns() {
  while (_teardowns.length) {
    // One bad teardown must not strand the rest, or a single throw leaks
    // everything registered after it.
    try { _teardowns.pop()(); } catch (e) { console.warn("teardown failed", e); }
  }
}

/* Attach a listener that dies with the view. Same arguments as
   addEventListener, so a leaking call site is fixed by adding one word. */
function onView(target, type, handler, opts) {
  target.addEventListener(type, handler, opts);
  onTeardown(() => target.removeEventListener(type, handler, opts));
}

const VIEWS = [
  { id: "pulse",     code: "PLS", label: "Pulse" },
  { id: "brief",     code: "BRF", label: "Daily" },
  { id: "workspace", code: "WSP", label: "Work" },
  { id: "chart",     code: "CHT", label: "Chart" },
  { id: "chain",     code: "OPT", label: "Chain" },
  { id: "payoff",    code: "PAY", label: "Payoff" },
  { id: "iv",        code: "VOL", label: "Volty" },
  { id: "volume",    code: "FLW", label: "Flow" },
  { id: "news",      code: "NWS", label: "News" },
  { id: "backtest",  code: "BTL", label: "Lab" },
  { id: "viz",       code: "QNT", label: "Quant" },
  { id: "mlstudio",  code: "MLS", label: "ML" },
  { id: "tape",      code: "TPE", label: "Tape" },
  { id: "screener",  code: "SCR", label: "Screen" },
  { id: "portfolio", code: "PRT", label: "Folio" },
  { id: "alerts",    code: "ALR", label: "Alerts" },
  { id: "datastore", code: "DTA", label: "Store" },
];

function buildRail() {
  const rail = $("#rail");
  VIEWS.forEach((v) => {
    const b = elv("button", "rail-btn",
      `<span class="code">${v.code}</span><span class="lbl">${v.label}</span>`);
    b.id = `rail-${v.id}`;
    b.title = `${v.label} (${v.code})`;
    b.onclick = () => show(v.id);
    rail.appendChild(b);
  });
}

/* ---------- routing --------------------------------------------------------
   The URL is a PROJECTION of state, never a second copy of it. show() stays
   the only thing that renders and gains exactly one new job: after folding
   params into state, it writes the address bar. hashchange reads the address
   bar and calls show() with that write suppressed.

   That ordering is the whole safety argument. Every existing caller of show()
   keeps working untouched, including the three inline onclick attributes in
   generated HTML, and there is only ever one path that renders. A design
   where the rail wrote the hash and the hash drove the rail would give two
   independent entry points that can double-render, in a file with no tests.

   Deliberately view + symbol only. Per-view modifiers (chart period, chain
   expiry, payoff strategy, quant tab) are where the breakage risk lives and
   they are worth roughly a fifth of the value. */

// Exchange tickers, not free text: M&M, BAJAJ-AUTO and ^NSEI must pass, and a
// hash someone typed by hand must not reach state.symbol unchecked.
const SYM_RE = /^[\^A-Z0-9][A-Z0-9._&^-]{0,23}$/;

// Views that take a symbol. The rest refuse one, so #/tape/RELIANCE does not
// quietly set the global symbol as a side effect of a malformed link.
const SYMBOL_VIEWS = new Set([
  "chart", "chain", "payoff", "iv", "volume", "news", "backtest",
  "viz", "mlstudio", "brief",
]);

let _routing = false;   // true while show() is being driven BY the hash

function hashFor(viewId, symbol) {
  return SYMBOL_VIEWS.has(viewId) && symbol ? `#/${viewId}/${symbol}` : `#/${viewId}`;
}

function parseHash() {
  const raw = (location.hash || "").replace(/^#\/?/, "");
  if (!raw) return null;
  const [viewId, sym] = raw.split("/");
  if (!viewId || !RENDER[viewId]) return null;          // unknown view: ignore
  // A symbol only survives if this view actually takes one. SYMBOL_VIEWS was
  // being consulted when WRITING the hash but not when reading it, so
  // #/tape/RELIANCE set the global symbol as a side effect of a malformed
  // link and the next chain you opened was silently the wrong instrument.
  if (!SYMBOL_VIEWS.has(viewId)) return { viewId, symbol: "" };
  const symbol = sym ? decodeURIComponent(sym).toUpperCase() : "";
  return { viewId, symbol: SYM_RE.test(symbol) ? symbol : "" };
}

function syncHash() {
  const want = hashFor(state.view, state.symbol);
  if (location.hash === want) return;
  // replaceState, not assignment: a 60s auto-refresh or a repaint must not
  // stack history entries you then have to press Back through.
  history.replaceState(null, "", want);
}

function show(viewId, params = {}) {
  clearTimers();
  runTeardowns();
  destroyCharts();
  vizDispose();
  state.view = viewId;
  if (params.symbol && SYM_RE.test(String(params.symbol).toUpperCase())) {
    state.symbol = String(params.symbol).toUpperCase();
  }
  document.querySelectorAll(".rail-btn").forEach((b) => b.classList.remove("active"));
  const btn = $(`#rail-${viewId}`);
  if (btn) btn.classList.add("active");
  const main = $("#main");
  main.innerHTML = "";
  const view = elv("div", "view");
  main.appendChild(view);
  if (!_routing) syncHash();
  RENDER[viewId](view, params);
}

// Back, forward, and a hash someone edited or pasted. Suppressed while it is
// this handler doing the driving, so the write above cannot loop.
window.addEventListener("hashchange", () => {
  const r = parseHash();
  if (!r) return;
  if (r.viewId === state.view && (!r.symbol || r.symbol === state.symbol)) return;
  _routing = true;
  try { show(r.viewId, r.symbol ? { symbol: r.symbol } : {}); }
  finally { _routing = false; }
});
window.show = show;

const loading = (msg = "loading") => `<div class="loading"><span class="spin"></span>${msg}</div>`;

/* ---------- PULSE ---------- */

function pulseRow(q, sparkId) {
  const has = q.price !== undefined && q.price !== null;
  // data-chart-symbol is what the row routes on. It comes from the server via
  // denormalize_symbol rather than being derived from the display name here:
  // the old `name.replace(/\s.*/, "")` sent BANK NIFTY to "BANK", INDIA VIX to
  // "INDIA" and S&P 500 to "S&P". Delegated below, so nothing is interpolated
  // into an onclick attribute where a quote in a name would break out of it.
  return `<tr data-symbol="${esc(q.symbol || "")}" data-chart-symbol="${esc(q.chart_symbol || "")}">
    <td class="txt sym">${esc(q.name)}</td>
    <td class="px">${has ? fmt.n(q.price) : "—"}</td>
    <td class="chg ${has ? cls(q.change_pct) : "faint"}">${has ? fmt.pct(q.change_pct) : "—"}</td>
    <td class="${has ? cls(q.change) : "faint"}">${has ? fmt.n(q.change) : "—"}</td>
    <td class="faint">${has && q.day_low ? `${fmt.n(q.day_low, dp(q.day_low))}–${fmt.n(q.day_high, dp(q.day_high))}` : "—"}</td>
    <td>${sparkId ? `<canvas class="spark" id="${sparkId}"></canvas>` : ""}</td>
  </tr>`;
}

async function renderPulse(view) {
  view.innerHTML = `
    <div class="row cols-main-side">
      <div style="display:grid;gap:12px">
        ${panel({ title: "INDIA", meta: "—", id: "p-india", flush: true, body: loading("quotes") })}
        ${panel({ title: "GLOBAL", meta: "—", id: "p-global", flush: true, body: loading("quotes") })}
      </div>
      <div style="display:grid;gap:12px;align-content:start">
        ${panel({ title: `NIFTY 50 — 3M DAILY`, id: "p-mini", flush: true,
                  body: `<div class="chart-host mini" id="pulse-chart"></div>` })}
        ${panel({ title: "NEWS BIAS", id: "p-bias", body: loading("scoring headlines") })}
        ${panel({ title: "WORLD SESSIONS", id: "p-globe", flush: true, meta: "—",
                  body: `<div class="globe-host" id="globe-host">${loading("spinning up")}</div>` })}
      </div>
    </div>`;

  let globeHandle = null;
  const drawGlobe = async () => {
    try {
      const s = await getJSON("/api/sessions");
      const open = s.exchanges.filter((e) => e.open).map((e) => e.code);
      const gp = $("#p-globe");
      if (!gp) return;
      gp.querySelector(".panel-meta").innerHTML =
        stamp(`${open.length ? "OPEN: " + open.join(" · ") : "ALL CLOSED"} · HOLIDAYS NOT MODELED`);
      if (!globeHandle && window.Viz3D && $("#globe-host")) {
        globeHandle = Viz3D.mountGlobe($("#globe-host"), s.exchanges);
      } else if (globeHandle) {
        globeHandle.update(s.exchanges);
      }
    } catch { /* globe is a bonus — pulse tables stay primary */ }
  };
  drawGlobe();
  addTimer("pulse:globe", drawGlobe, 30000);

  const paintBoards = (data, live) => {
    for (const [key, pid] of [["india", "p-india"], ["global", "p-global"]]) {
      const p = $(`#${pid}`);
      if (!p) return;
      // A snapshot says how old it is; only a live paint claims freshness.
      p.querySelector(".panel-meta").innerHTML = live
        ? stamp("REST 12s + WS TICKS")
        : ageStamp(data.as_of) + ' <span class="faint">· REFRESHING</span>';
      p.querySelector(".panel-body").innerHTML = `
        <table class="tbl"><thead><tr>
          <th>INSTRUMENT</th><th>LAST</th><th>CHG%</th><th>CHG</th><th>DAY RANGE</th><th>1M</th>
        </tr></thead><tbody>
          ${data[key].map((q, i) => pulseRow(q, `${key}-spark-${i}`)).join("")}
        </tbody></table>`;
    }
  };

  // Cold start used to sit on spinners for the ~20s the live quote fan-out
  // takes. Paint the last real snapshot immediately, aged honestly, while the
  // live fetch runs. 404 just means no snapshot yet, which is fine.
  getJSON("/api/pulse?cached=1")
    .then((snap) => { if (state.view === "pulse" && !state.pulseLive) paintBoards(snap, false); })
    .catch(() => {});

  const drawQuotes = async () => {
    try {
      const data = await getJSON("/api/pulse");
      state.pulseLive = true;
      paintBoards(data, true);
      // sparklines (cached server-side)
      for (const [key] of [["india"], ["global"]]) {
        const syms = (key === "india")
          ? ["NIFTY", "BANKNIFTY", "SENSEX", "INDIAVIX", "USDINR"]
          : ["^GSPC", "^IXIC", "^DJI", "^FTSE", "^GDAXI", "^N225", "^HSI", "BZ=F", "GC=F", "^TNX"];
        getJSON(`/api/sparks?symbols=${encodeURIComponent(syms.join(","))}`).then((sp) => {
          syms.forEach((s, i) => {
            const c = $(`#${key}-spark-${i}`);
            const closes = sp[s.toUpperCase()];
            if (c && closes) sparkline(c, closes);
          });
        }).catch(() => {});
      }
    } catch (e) { toast(`Pulse: ${e.message}`, "err"); }
  };

  const drawMini = async () => {
    try {
      const d = await getJSON(`/api/history/NIFTY?period=3mo`);
      const host = $("#pulse-chart");
      if (!host) return;
      host.innerHTML = "";
      const chart = mkChart(host, { timeScale: { visible: false }, rightPriceScale: { visible: true } });
      const s = chart.addSeries(LWC.CandlestickSeries, {
        upColor: "#2ebd85", downColor: "#f1564b",
        wickUpColor: "#2ebd85", wickDownColor: "#f1564b", borderVisible: false,
      });
      s.setData(d.candles);
      // autoSize lays out a frame later — fit after that, or candles bunch right
      requestAnimationFrame(() => chart.timeScale().fitContent());
    } catch {}
  };

  const drawBias = async () => {
    try {
      const news = await getJSON("/api/news?limit=20");
      const b = news.bias;
      const p = $("#p-bias");
      if (!p) return;
      const klass = b.label.includes("bullish") ? "up" : b.label.includes("bearish") ? "down" : "dim";
      p.querySelector(".panel-meta").innerHTML = stamp("FEED LAG 5–15M");
      p.querySelector(".panel-body").innerHTML = `
        <div style="display:flex;gap:10px;align-items:baseline;flex-wrap:wrap">
          <span class="${klass}" style="font-size:17px;font-weight:700">${b.label.toUpperCase()}</span>
          <span class="mono dim">${b.score >= 0 ? "+" : ""}${b.score.toFixed(2)}</span>
          <span class="faint" style="font-size:10px">${b.n_items} headlines · 6h half-life decay</span>
        </div>
        ${b.gap_call ? `<div class="dim" style="margin-top:6px;font-size:11px">${b.gap_call}</div>` : ""}
        <div style="margin-top:8px"><span class="badge" style="cursor:pointer" onclick="show('news')">OPEN NWS →</span></div>`;
    } catch {
      const p = $("#p-bias");
      if (p) p.querySelector(".panel-body").innerHTML = `<div class="empty">news feed unavailable</div>`;
    }
  };

  // Delegated on the view root rather than per-row: the rows are replaced on
  // every 12s repaint, so per-row handlers would be re-bound constantly. The
  // view root is destroyed by show(), so this needs no teardown.
  view.addEventListener("click", (ev) => {
    const row = ev.target.closest("tr[data-chart-symbol]");
    const sym = row && row.dataset.chartSymbol;
    if (sym) show("chart", { symbol: sym });
  });

  drawQuotes(); drawMini(); drawBias();
  addTimer("pulse:quotes", drawQuotes, 12000);
  addTimer("pulse:bias", drawBias, 60000);
}

/* ---------- CHART ---------- */

let CHART_CAT = null;            // /api/chart/catalog, fetched once
const CHART_INTERVALS = ["1m", "5m", "15m", "1h", "1d"];
const CHART_PERIODS = ["1mo", "3mo", "6mo", "1y", "2y", "5y", "10y"];

function _dropChart(ch) {
  if (!ch) return;
  try { ch.remove(); } catch {}
  const i = liveCharts.indexOf(ch);
  if (i >= 0) liveCharts.splice(i, 1);
}

async function renderChart(view) {
  const sym = state.symbol;
  state.chartType ||= "candles";
  state.chartInterval ||= "1d";
  state.chartIndicators ||= [];
  state.chartDrawings ||= [];
  if (!CHART_CAT) {
    try { CHART_CAT = (await getJSON("/api/chart/catalog")).indicators; }
    catch { CHART_CAT = {}; }
  }
  // Per-symbol saved config (best-effort) overrides session defaults.
  try {
    const cfg = await getJSON(`/api/chart/config/${sym}`);
    if (cfg.type) state.chartType = cfg.type;
    if (cfg.interval) state.chartInterval = cfg.interval;
    if (Array.isArray(cfg.indicators)) state.chartIndicators = cfg.indicators;
    state.chartDrawings = Array.isArray(cfg.drawings) ? cfg.drawings : [];
  } catch { state.chartDrawings = []; }

  view.innerHTML = panel({
    title: `CHART — <span class="hl">${sym}</span>`, id: "chart-panel", flush: true,
    meta: chartControls(sym),
    body: `<div class="chart-indbar" id="chart-indbar"></div>
      <div class="chart-wrap">
        <div class="chart-tools" id="chart-tools"></div>
        <div class="chart-legend" id="chart-legend"></div>
        <div class="chart-host tall" id="chart-main">${loading("candles")}</div>
      </div>
      <div id="chart-insight"></div>
      <div class="shn-drawer">
        <div class="shn-head">
          <span class="shn-title">⟨/⟩ SHUN SCRIPT</span>
          <select id="shn-tpl" class="viz-select">
            <option value="">templates…</option>
            <option value="ema_cross">EMA crossover strategy</option>
            <option value="rsi_reversion">RSI mean reversion</option>
            <option value="bb_fade">Bollinger band fade</option>
            <option value="overlay_only">Overlays only (no strategy)</option>
          </select>
          <button class="viz-mini run" id="shn-run">RUN ▶</button>
          <span class="faint" id="shn-note">safe DSL · vectorized · backtests on the same engine</span>
        </div>
        <textarea id="shn-code" class="shn-code" rows="7" spellcheck="false"
          placeholder="fast = ema(close, 12)&#10;slow = ema(close, 26)&#10;plot(fast, color='amber')&#10;plot(slow, color='blue')&#10;long_when(cross_above(fast, slow))&#10;short_when(cross_below(fast, slow))"></textarea>
        <div id="shn-out"></div>
      </div>`,
  });
  wireChartControls();
  renderIndBar();
  renderDrawTools();
  wireShnDrawer();
  await loadChart();
}

const SHN_TEMPLATES = {
  ema_cross: `fast = ema(close, 12)\nslow = ema(close, 26)\nplot(fast, color="amber", title="EMA 12")\nplot(slow, color="blue", title="EMA 26")\nlong_when(cross_above(fast, slow))\nshort_when(cross_below(fast, slow))`,
  rsi_reversion: `r = rsi(close, 14)\nplot(sma(close, 20), color="gray", title="SMA 20")\nlong_when(r < 30)\nexit_when(r > 55)`,
  bb_fade: `upper = bb_upper(close, 20, 2)\nlower = bb_lower(close, 20, 2)\nplot(upper, color="red", title="BB upper")\nplot(lower, color="green", title="BB lower")\nlong_when(close < lower)\nshort_when(close > upper)\nexit_when(cross_above(close, sma(close, 20)) or cross_below(close, sma(close, 20)))`,
  overlay_only: `plot(ema(close, 21), color="amber", title="EMA 21")\nplot(vwap(), color="blue", title="VWAP")\nplot(highest(high, 20), color="gray", title="20d high")`,
};

function wireShnDrawer() {
  const code = $("#shn-code");
  if (state.shnCode) code.value = state.shnCode;
  code.addEventListener("input", () => { state.shnCode = code.value; });
  $("#shn-tpl").onchange = (e) => {
    if (SHN_TEMPLATES[e.target.value]) {
      code.value = SHN_TEMPLATES[e.target.value];
      state.shnCode = code.value;
    }
  };
  $("#shn-run").onclick = runShnScript;
  code.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") runShnScript();
  });
}

async function runShnScript() {
  const code = $("#shn-code").value.trim();
  const out = $("#shn-out");
  if (!code) { out.innerHTML = `<div class="empty">Write a script or pick a template.</div>`; return; }
  const btn = $("#shn-run");
  btn.disabled = true; btn.textContent = "RUNNING…";
  try {
    const d = await postJSON("/api/script/run", {
      symbol: state.symbol, code,
      period: state.chartPeriod, interval: state.chartInterval,
    });
    if (!d.ok) {
      out.innerHTML = `<div class="shn-err">✕ ${d.error}</div>`;
      return;
    }
    // replace previous script overlays
    for (const s of _shnSeries) { try { _chartObj.removeSeries(s); } catch {} }
    _shnSeries = [];
    if (_chartObj) {
      for (const p of d.plots) {
        const s = _chartObj.addSeries(LWC.LineSeries, {
          color: p.color, lineWidth: 1.5, priceLineVisible: false, lastValueVisible: false,
        });
        s.setData(p.points.map((x) => ({ time: x.time, value: x.value })));
        _shnSeries.push(s);
      }
      if (d.markers && _mainSeries && typeof LWC.createSeriesMarkers === "function") {
        LWC.createSeriesMarkers(_mainSeries, d.markers.map((m) => ({
          time: m.time, position: m.dir > 0 ? "belowBar" : "aboveBar",
          color: m.dir > 0 ? "#2ebd85" : m.dir < 0 ? "#f1564b" : "#9aa1b0",
          shape: m.dir > 0 ? "arrowUp" : m.dir < 0 ? "arrowDown" : "circle",
          text: m.dir > 0 ? "L" : m.dir < 0 ? "S" : "X",
        })));
      }
    }
    const bt = d.backtest;
    out.innerHTML = `
      <div class="shn-metrics">
        <span>${d.plots.length} plot${d.plots.length === 1 ? "" : "s"} · ${d.bars} bars · ${d.elapsed_ms.toFixed(1)}ms</span>
        ${bt ? `
          <span class="${bt.metrics.total_return >= 0 ? "up" : "down"}">RET ${fmt.pct(bt.metrics.total_return)}</span>
          <span>SHARPE ${bt.metrics.sharpe.toFixed(2)}</span>
          <span class="down">MAXDD ${fmt.pct(bt.metrics.max_drawdown)}</span>
          <span>TRADES ${fmt.i(bt.metrics.num_trades)}</span>
          ${iM(bt.prov, "SCRIPT BACKTEST")}` : `<span class="faint">no strategy verbs — overlays only</span>`}
      </div>`;
  } catch (e) {
    out.innerHTML = `<div class="shn-err">✕ ${e.message}</div>`;
  } finally {
    btn.disabled = false; btn.textContent = "RUN ▶";
  }
}

const DRAW_TOOLS = [
  { id: "cursor", label: "⌖", title: "Cursor / pan" },
  { id: "trend", label: "╱", title: "Trendline" },
  { id: "horiz", label: "─", title: "Horizontal line" },
  { id: "rect", label: "▭", title: "Rectangle / zone" },
  { id: "fib", label: "𝄜", title: "Fibonacci retracement" },
];
let _drawTool = "cursor";

function renderDrawTools() {
  const bar = $("#chart-tools");
  if (!bar) return;
  bar.innerHTML = DRAW_TOOLS.map((t) =>
    `<button class="draw-tool ${t.id === _drawTool ? "active" : ""}" data-tool="${t.id}"
      title="${t.title}">${t.label}</button>`).join("") +
    `<button class="draw-tool" id="draw-undo" title="Undo last">↶</button>
     <button class="draw-tool" id="draw-clear" title="Clear all">🗑</button>`;
  bar.querySelectorAll("[data-tool]").forEach((b) => {
    b.onclick = () => {
      _drawTool = b.dataset.tool;
      renderDrawTools();
      _drawLayer?.setTool(_drawTool);
    };
  });
  $("#draw-undo").onclick = () => _drawLayer?.undo();
  $("#draw-clear").onclick = () => _drawLayer?.clear();
}

function chartControls(sym) {
  const sel = (id, opts, val) => `<select class="in" id="${id}">${opts.map((o) =>
    `<option ${o === val ? "selected" : ""}>${o}</option>`).join("")}</select>`;
  return `<span class="controls">
    <input class="in" id="chart-sym" value="${sym}" size="8">
    ${sel("chart-interval", CHART_INTERVALS, state.chartInterval)}
    ${sel("chart-period", CHART_PERIODS, state.chartPeriod)}
    <select class="in" id="chart-type">
      ${["candles", "line", "area"].map((t) =>
        `<option value="${t}" ${t === state.chartType ? "selected" : ""}>${t.toUpperCase()}</option>`).join("")}
    </select>
    <button class="btn ghost" id="chart-ind-btn">+ INDICATOR</button>
    <button class="btn" id="chart-go">LOAD</button></span>`;
}

function wireChartControls() {
  $("#chart-go").onclick = () => {
    state.chartPeriod = $("#chart-period").value;
    state.chartInterval = $("#chart-interval").value;
    show("chart", { symbol: $("#chart-sym").value });
  };
  $("#chart-sym").onkeydown = (e) => { if (e.key === "Enter") $("#chart-go").click(); };
  $("#chart-interval").onchange = () => {
    state.chartInterval = $("#chart-interval").value; saveChartConfig(); loadChart();
  };
  $("#chart-period").onchange = () => {
    state.chartPeriod = $("#chart-period").value; loadChart();
  };
  $("#chart-type").onchange = () => {
    state.chartType = $("#chart-type").value; saveChartConfig(); loadChart();
  };
  $("#chart-ind-btn").onclick = (e) => { e.stopPropagation(); toggleIndMenu(); };
}

function toggleIndMenu() {
  const existing = $("#chart-ind-menu");
  if (existing) { existing.remove(); return; }
  const groups = { price: [], lower: [] };
  for (const [kind, m] of Object.entries(CHART_CAT)) groups[m.pane]?.push([kind, m]);
  const row = (kind, m) => `<button class="ind-opt" data-kind="${kind}" data-period="${m.default || 0}">
    ${m.label}${m.period ? ` (${m.default})` : ""}</button>`;
  const menu = elv("div", "chart-ind-menu");
  menu.id = "chart-ind-menu";
  menu.innerHTML = `<div class="ind-grp">OVERLAYS</div>${groups.price.map(([k, m]) => row(k, m)).join("")}
    <div class="ind-grp">OSCILLATORS</div>${groups.lower.map(([k, m]) => row(k, m)).join("")}`;
  $("#chart-panel").appendChild(menu);
  menu.querySelectorAll(".ind-opt").forEach((b) => {
    b.onclick = () => {
      const spec = `${b.dataset.kind.toLowerCase()}:${b.dataset.period}`;
      if (!state.chartIndicators.includes(spec)) state.chartIndicators.push(spec);
      menu.remove(); renderIndBar(); saveChartConfig(); loadChart();
    };
  });
  setTimeout(() => document.addEventListener("click", function close() {
    menu.remove(); document.removeEventListener("click", close);
  }, { once: true }), 0);
}

function renderIndBar() {
  const bar = $("#chart-indbar");
  if (!bar) return;
  if (!state.chartIndicators.length) {
    bar.innerHTML = `<span class="faint">No indicators — click + INDICATOR to overlay SMA/EMA/Bollinger/VWAP or add RSI/MACD/ATR panes.</span>`;
    return;
  }
  bar.innerHTML = state.chartIndicators.map((spec, i) => {
    const [kind, p] = spec.split(":");
    const m = CHART_CAT[kind.toUpperCase()] || { label: kind };
    return `<span class="chip ind-chip">${m.label}${m.period && +p ? " " + p : ""}
      <span class="chip-x" data-i="${i}">✕</span></span>`;
  }).join("");
  bar.querySelectorAll(".chip-x").forEach((x) => {
    x.onclick = () => {
      state.chartIndicators.splice(+x.dataset.i, 1);
      renderIndBar(); saveChartConfig(); loadChart();
    };
  });
}

function saveChartConfig() {
  postJSON(`/api/chart/config/${state.symbol}`, {
    type: state.chartType, interval: state.chartInterval,
    indicators: state.chartIndicators, drawings: state.chartDrawings,
  }).catch(() => {});
}

let _chartObj = null;
let _mainSeries = null;
let _shnSeries = [];
async function loadChart() {
  const host = $("#chart-main");
  if (!host) return;
  const sym = state.symbol;
  const q = `period=${state.chartPeriod}&interval=${state.chartInterval}`;
  host.innerHTML = loading("candles");
  try {
    const specs = state.chartIndicators.join(",");
    const [data, ind] = await Promise.all([
      getJSON(`/api/history/${sym}?${q}`),
      specs ? getJSON(`/api/chart/indicators/${sym}?${q}&specs=${specs}`)
        : Promise.resolve({ indicators: [] }),
    ]);
    host.innerHTML = "";
    drawChartView(host, data, ind.indicators);
    $("#chart-panel .panel-meta .upd")?.remove();
    const head = $("#chart-panel .panel-meta");
    if (head && !$("#chart-stamp")) {
      head.insertAdjacentHTML("afterbegin",
        `<span id="chart-stamp" class="faint" style="margin-right:8px">${data.candles.length} bars · ${fmt.ist()}</span>`);
    } else if ($("#chart-stamp")) {
      $("#chart-stamp").textContent = `${data.candles.length} bars · ${fmt.ist()}`;
    }
    const c = data.candles, last = c[c.length - 1], first = c[0];
    $("#chart-insight").innerHTML = insightBlock([
      { what: `${fmt.pct(last.close / first.close - 1)} over ${state.chartPeriod} (${state.chartInterval})`,
        why: `close ${fmt.n(first.close)} → ${fmt.n(last.close)}` },
    ]);
  } catch (e) {
    host.innerHTML = `<div class="empty">${e.message}</div>`;
  }
}

function drawChartView(host, data, indicators) {
  _dropChart(_chartObj);
  _shnSeries = [];
  const chart = mkChart(host);
  _chartObj = chart;
  const c = data.candles;
  const legendSeries = [];

  // Main price series by type.
  let main;
  if (state.chartType === "line") {
    main = chart.addSeries(LWC.LineSeries, { color: "#f0a826", lineWidth: 2 });
    main.setData(c.map((x) => ({ time: x.time, value: x.close })));
  } else if (state.chartType === "area") {
    main = chart.addSeries(LWC.AreaSeries, {
      lineColor: "#f0a826", topColor: "rgba(240,168,38,0.30)", bottomColor: "rgba(240,168,38,0.02)",
      lineWidth: 2,
    });
    main.setData(c.map((x) => ({ time: x.time, value: x.close })));
  } else {
    main = chart.addSeries(LWC.CandlestickSeries, {
      upColor: "#2ebd85", downColor: "#f1564b",
      wickUpColor: "#2ebd85", wickDownColor: "#f1564b", borderVisible: false,
    });
    main.setData(c);
  }

  _mainSeries = main;

  // Volume overlay on the price pane.
  const vol = chart.addSeries(LWC.HistogramSeries, {
    priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false,
  });
  chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.86, bottom: 0 } });
  vol.setData(data.volume);

  // Indicators: price-pane overlays, oscillators each in their own pane.
  let paneIdx = 1;
  for (const ind of indicators) {
    if (ind.pane === "price") {
      for (const ln of ind.lines) {
        const s = chart.addSeries(LWC.LineSeries, {
          color: ln.color, lineWidth: 1.5, priceLineVisible: false, lastValueVisible: false,
        });
        s.setData(ln.data);
        legendSeries.push({ label: ln.title, color: ln.color, series: s });
      }
    } else {
      const p = paneIdx++;
      let first = null;
      for (const ln of ind.lines) {
        const s = chart.addSeries(LWC.LineSeries, {
          color: ln.color, lineWidth: 1.5, priceLineVisible: false, lastValueVisible: false,
        }, p);
        s.setData(ln.data);
        legendSeries.push({ label: ln.title, color: ln.color, series: s });
        first ||= s;
      }
      if (ind.hist) {
        const h = chart.addSeries(LWC.HistogramSeries, { priceLineVisible: false, lastValueVisible: false }, p);
        h.setData(ind.hist.map((d) => ({ time: d.time, value: d.value,
          color: d.value >= 0 ? "rgba(46,189,133,0.5)" : "rgba(241,86,75,0.5)" })));
      }
      for (const g of (ind.guides || [])) {
        first?.createPriceLine({ price: g, color: "rgba(154,161,176,0.4)", lineWidth: 1,
          lineStyle: 2, axisLabelVisible: true, title: String(g) });
      }
      try {
        const panes = chart.panes();
        panes[0]?.setStretchFactor?.(3);
        panes[p]?.setStretchFactor?.(1);
      } catch {}
    }
  }
  chart.timeScale().fitContent();

  // Crosshair legend: OHLC of the hovered bar + each overlay/oscillator value.
  const legend = $("#chart-legend");
  const fmtOHLC = (b) => `O <b>${fmt.n(b.open)}</b> H <b>${fmt.n(b.high)}</b> L <b>${fmt.n(b.low)}</b> C <b>${fmt.n(b.close)}</b>`;
  const baseLegend = () => {
    const last = c[c.length - 1];
    legend.innerHTML = `<span class="lg-sym">${state.symbol}</span> · ${state.chartInterval} · ` +
      (state.chartType === "candles" ? fmtOHLC(last) : `C <b>${fmt.n(last.close)}</b>`);
  };
  baseLegend();
  chart.subscribeCrosshairMove((param) => {
    if (!param.time || !param.seriesData?.size) { baseLegend(); return; }
    const bar = param.seriesData.get(main);
    let html = `<span class="lg-sym">${state.symbol}</span> · ${state.chartInterval} · `;
    html += bar ? (bar.open !== undefined ? fmtOHLC(bar) : `C <b>${fmt.n(bar.value)}</b>`) : "";
    for (const ls of legendSeries) {
      const d = param.seriesData.get(ls.series);
      if (d && d.value !== undefined)
        html += `  <span style="color:${ls.color}">${ls.label} ${fmt.n(d.value)}</span>`;
    }
    legend.innerHTML = html;
  });

  // Drawing layer (trendlines / levels / zones / fib) on an overlay canvas.
  if (_drawLayer) { _drawLayer.destroy(); _drawLayer = null; }
  _drawLayer = createDrawingLayer($(".chart-wrap"), chart, main);
  _drawLayer.setTool(_drawTool);
}

let _drawLayer = null;

function createDrawingLayer(wrap, chart, series) {
  const canvas = elv("canvas", "chart-draw-canvas");
  wrap.appendChild(canvas);
  const ctx = canvas.getContext("2d");
  const drawings = state.chartDrawings;  // shared reference, persisted via config
  let tool = "cursor", pending = null, mouse = null;
  const FIB = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];

  const ts = () => chart.timeScale();
  const toXY = (a) => {
    const x = ts().timeToCoordinate(a.time), y = series.priceToCoordinate(a.price);
    return (x === null || y === null) ? null : { x, y };
  };
  const fromXY = (x, y) => {
    const time = ts().coordinateToTime(x), price = series.coordinateToPrice(y);
    return (time === null || price === null) ? null : { time, price };
  };

  function resize() {
    const dpr = window.devicePixelRatio || 1;
    const w = wrap.clientWidth, h = wrap.clientHeight;
    canvas.width = w * dpr; canvas.height = h * dpr;
    canvas.style.width = w + "px"; canvas.style.height = h + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    redraw();
  }

  function seg(a, b, color, width = 1.5, dash = []) {
    ctx.save(); ctx.strokeStyle = color; ctx.lineWidth = width; ctx.setLineDash(dash);
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke(); ctx.restore();
  }
  function tag(x, y, text, color) {
    ctx.save(); ctx.fillStyle = color; ctx.font = "10px ui-monospace,monospace";
    ctx.fillText(text, x, y); ctx.restore();
  }

  function renderOne(d) {
    if (d.kind === "horiz") {
      const y = series.priceToCoordinate(d.price);
      if (y === null) return;
      seg({ x: 0, y }, { x: canvas.clientWidth, y }, "#f0a826", 1.2, [4, 3]);
      tag(6, y - 3, fmt.n(d.price), "#f0a826");
      return;
    }
    const a = toXY(d.a), b = toXY(d.b);
    if (!a || !b) return;
    if (d.kind === "trend") seg(a, b, "#58a6ff", 1.8);
    else if (d.kind === "rect") {
      ctx.save(); ctx.fillStyle = "rgba(88,166,255,0.10)";
      ctx.strokeStyle = "rgba(88,166,255,0.7)"; ctx.lineWidth = 1;
      ctx.fillRect(a.x, a.y, b.x - a.x, b.y - a.y);
      ctx.strokeRect(a.x, a.y, b.x - a.x, b.y - a.y); ctx.restore();
    } else if (d.kind === "fib") {
      const x0 = Math.min(a.x, b.x), x1 = Math.max(a.x, b.x);
      for (const lv of FIB) {
        const price = d.a.price + (d.b.price - d.a.price) * lv;
        const y = series.priceToCoordinate(price);
        if (y === null) continue;
        seg({ x: x0, y }, { x: x1, y }, "rgba(188,140,255,0.8)", 1, lv === 0 || lv === 1 ? [] : [3, 3]);
        tag(x1 + 4, y + 3, `${(lv * 100).toFixed(1)}% ${fmt.n(price)}`, "rgba(188,140,255,0.9)");
      }
    }
  }

  function redraw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    drawings.forEach(renderOne);
    if (pending && mouse) {
      const a = toXY(pending);
      if (a) {
        if (tool === "rect") {
          ctx.save(); ctx.strokeStyle = "rgba(88,166,255,0.5)"; ctx.setLineDash([4, 3]);
          ctx.strokeRect(a.x, a.y, mouse.x - a.x, mouse.y - a.y); ctx.restore();
        } else seg(a, mouse, "rgba(154,161,176,0.7)", 1.2, [4, 3]);
      }
    }
  }

  canvas.onmousedown = (e) => {
    if (tool === "cursor") return;
    const r = canvas.getBoundingClientRect();
    const x = e.clientX - r.left, y = e.clientY - r.top;
    if (tool === "horiz") {
      const p = series.coordinateToPrice(y);
      if (p !== null) { drawings.push({ kind: "horiz", price: p }); commit(); }
      return;
    }
    const pt = fromXY(x, y);
    if (!pt) return;
    if (!pending) { pending = pt; mouse = { x, y }; }
    else { drawings.push({ kind: tool, a: pending, b: pt }); pending = null; commit(); }
  };
  canvas.onmousemove = (e) => {
    if (tool === "cursor" || !pending) return;
    const r = canvas.getBoundingClientRect();
    mouse = { x: e.clientX - r.left, y: e.clientY - r.top };
    redraw();
  };

  function commit() { mouse = null; redraw(); saveChartConfig(); }
  const rangeSub = () => redraw();
  ts().subscribeVisibleLogicalRangeChange(rangeSub);
  const ro = new ResizeObserver(resize); ro.observe(wrap);
  resize();

  return {
    setTool(t) {
      tool = t; pending = null; mouse = null;
      canvas.style.pointerEvents = t === "cursor" ? "none" : "auto";
      canvas.style.cursor = t === "cursor" ? "default" : "crosshair";
      redraw();
    },
    undo() { drawings.pop(); commit(); },
    clear() { drawings.length = 0; commit(); },
    destroy() {
      try { ts().unsubscribeVisibleLogicalRangeChange(rangeSub); } catch {}
      ro.disconnect(); canvas.remove();
    },
  };
}

/* ---------- OPTION CHAIN ----------
   The shell is built once and every refresh writes text into cells it already
   owns. Re-rendering the whole view on the timer flashed a spinner, reset the
   strike scroll, wiped a half-typed symbol and leaked one chart plus six
   provenance entries per minute. */

/* A modelled chain invents OI, ΔOI and volume — and the API still labels that
   fabricated oi_change "source-provided (NSE prev-day)". Reads derived from
   invented flow are suppressed, not merely labelled. */
const isModelledChain = (source) => /^(synthetic|model)/i.test(source || "");

function renderChain(view) {
  const sym = state.symbol;
  let expiry = "";      // "" = let the server pick the front month
  let expiryKey = "";   // rebuild the <select> only when the ladder changes
  let strikeKey = "";   // rebuild the strike rows only when the strikes change
  let cells = [];       // one cell-reference set per rendered strike row
  let painted = false;
  let straddleChart = null, straddleSeries = null;

  view.innerHTML = `
    ${panel({ title: `OPTION CHAIN — <span class="hl">${sym}</span>
        <span class="badge" id="chain-src">—</span><span class="badge" id="chain-dte">—</span>`,
      id: "chain-top", flush: true,
      meta: `<span id="chain-upd">LOADING…</span>
        <span class="controls" style="display:inline-flex;margin-left:8px">
        <select class="in" id="chain-exp"></select>
        <input class="in" id="chain-sym" value="${sym}" size="8">
        <button class="btn" id="chain-go">LOAD</button></span>`,
      body: `
      <div class="kv-strip">
        <div class="kv"><div class="k">SPOT</div><div class="v" id="cv-spot">—</div></div>
        <div class="kv"><div class="k">PCR OI<button class="imark" data-prov="chain:pcr" title="How is this number computed?" style="display:none">i</button></div><div class="v" id="cv-pcr">—</div></div>
        <div class="kv"><div class="k">MAX PAIN<button class="imark" data-prov="chain:maxpain" title="How is this number computed?" style="display:none">i</button></div><div class="v amber" id="cv-mp">—</div></div>
        <div class="kv"><div class="k">ATM IV<button class="imark" data-prov="chain:atmiv" title="How is this number computed?" style="display:none">i</button></div><div class="v" id="cv-iv">—</div></div>
        <div class="kv"><div class="k">EXP MOVE<button class="imark" data-prov="chain:move" title="How is this number computed?" style="display:none">i</button></div><div class="v" id="cv-move">—</div></div>
        <div class="kv"><div class="k">SUPPORT</div><div class="v up" id="cv-sup">—</div></div>
        <div class="kv"><div class="k">RESIST</div><div class="v down" id="cv-res">—</div></div>
        <div class="kv"><div class="k">STRADDLE</div><div class="v" id="cv-str">—</div></div>
        <div class="kv"><div class="k">EXPIRY</div><div class="v sm" id="cv-exp">—</div></div>
        <div class="kv"><div class="k">LOT</div><div class="v sm" id="cv-lot">—</div></div>
      </div>
      <div class="insight">
        <div class="insight-title">INSIGHT</div>
        <div class="insight-item"><span class="marker">▸</span>
          <span><span class="what" id="ci-bias-w">—</span><button class="imark" data-prov="chain:bias" title="How is this number computed?" style="display:none">i</button>
            <span class="why" id="ci-bias-y"></span></span></div>
        <div class="insight-item"><span class="marker">▸</span>
          <span><span class="what" id="ci-move-w">—</span> <span class="why" id="ci-move-y"></span></span></div>
        <div class="insight-item" id="ci-unusual" style="display:none"><span class="marker">▸</span>
          <span><span class="what" id="ci-unusual-w"></span> <span class="why" id="ci-unusual-y"></span></span></div>
      </div>` })}
    ${panel({ title: `STRIKES <span class="badge" id="chain-win">—</span>`, id: "chain-strikes", flush: true,
      meta: `<span id="chain-doi">—</span><button class="imark" data-prov="chain:doi" title="How is this number computed?" style="display:none">i</button>`,
      body: `<div class="tbl-scroll" id="chain-scroll">
        <table class="tbl"><thead><tr>
          <th class="oi-h call">C·OI</th><th>C·ΔOI</th><th>C·VOL</th><th>C·IV</th><th>C·LTP</th>
          <th class="k-strike">STRIKE</th>
          <th>P·LTP</th><th>P·IV</th><th>P·VOL</th><th>P·ΔOI</th><th class="oi-h put">P·OI</th>
        </tr></thead><tbody id="chain-rows">
          <tr><td colspan="11">${loading("chain")}</td></tr>
        </tbody></table></div>` })}
    <div id="chain-straddle" style="display:contents"></div>`;

  const symIn = $("#chain-sym"), expSel = $("#chain-exp");
  $("#chain-go").onclick = () => show("chain", { symbol: symIn.value });
  symIn.onkeydown = (e) => { if (e.key === "Enter") $("#chain-go").click(); };
  expSel.onchange = () => { expiry = expSel.value; load(); };

  /* ⓘ marks belong to the shell; only their derivation is replaced, so the
     registry cannot grow one entry per refresh. */
  const setProv = (id, p, label) => {
    const b = $(`.imark[data-prov="${id}"]`);
    if (!p) { provRegistry.delete(id); if (b) b.style.display = "none"; return; }
    provRegistry.set(id, { ...p, label });
    if (b) b.style.display = "";
  };

  const buildRows = (rows) => {
    const tb = $("#chain-rows");
    tb.innerHTML = rows.map(() => `
      <tr>
        <td class="oi-cell call"><div class="oi-bar call"></div><span class="oi-num">—</span></td>
        <td>—</td><td class="faint">—</td><td>—</td><td class="ltp-cell" data-right="CE">—</td>
        <td class="k-strike">—</td>
        <td class="ltp-cell" data-right="PE">—</td><td>—</td><td class="faint">—</td><td>—</td>
        <td class="oi-cell put"><div class="oi-bar put"></div><span class="oi-num">—</span></td>
      </tr>`).join("");
    cells = [...tb.rows].map((tr) => ({
      tr,
      cBar: tr.cells[0].firstElementChild, cOI: tr.cells[0].lastElementChild,
      cDOI: tr.cells[1], cVol: tr.cells[2], cIV: tr.cells[3], cLTP: tr.cells[4],
      strike: tr.cells[5],
      pLTP: tr.cells[6], pIV: tr.cells[7], pVol: tr.cells[8], pDOI: tr.cells[9],
      pBar: tr.cells[10].firstElementChild, pOI: tr.cells[10].lastElementChild,
    }));
  };

  const paint = (c) => {
    const a = c.analytics, pv = c.prov || {};
    const modelled = isModelledChain(c.source);
    const exps = c.expiries && c.expiries.length ? c.expiries : [c.expiry];
    // Both dates read in IST: the expiry is an NSE trading date, so a
    // UTC-based diff is a day out after 18:30 IST.
    const istToday = new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
    const dteOf = (d) => Math.round((Date.parse(d) - Date.parse(istToday)) / 86400000);

    const src = $("#chain-src");
    src.textContent = modelled ? `MODELLED · ${c.source.toUpperCase()}` : c.source.toUpperCase();
    src.className = modelled ? "badge amb" : "badge bull";
    src.title = (c.source_trail || []).join("\n") || c.source;
    const dte = dteOf(c.expiry);
    const dteEl = $("#chain-dte");
    dteEl.textContent = dte === 0 ? "EXPIRY TODAY" : dte < 0 ? "EXPIRED" : `${dte} DTE`;
    dteEl.className = dte <= 1 ? "badge amb" : "badge";

    const ladder = exps.join(",");
    if (ladder !== expiryKey) {
      expiryKey = ladder;
      expSel.innerHTML = exps.map((e) => `<option value="${e}">${e} · ${dteOf(e)}D</option>`).join("");
    }
    expSel.value = c.expiry;
    $("#chain-upd").innerHTML = ageStamp(c.as_of) + ' <span class="faint">· AUTO 60s</span>';

    $("#cv-spot").textContent = fmt.n(c.spot);
    $("#cv-pcr").textContent = a.pcr_oi.toFixed(2);
    $("#cv-mp").textContent = fmt.i(a.max_pain);
    $("#cv-iv").textContent = a.atm_iv ? (a.atm_iv * 100).toFixed(1) + "%" : "—";
    $("#cv-move").textContent = `±${(a.expected_move_pct * 100).toFixed(1)}%`;
    $("#cv-sup").textContent = fmt.i(a.support);
    $("#cv-res").textContent = fmt.i(a.resistance);
    $("#cv-str").textContent = fmt.i(a.straddle_price);
    $("#cv-exp").textContent = c.expiry;
    $("#cv-lot").textContent = c.lot_size;
    setProv("chain:pcr", pv.pcr_oi, "PCR");
    setProv("chain:maxpain", pv.max_pain, "MAX PAIN");
    setProv("chain:atmiv", pv.atm_iv, "ATM IV");
    setProv("chain:move", pv.expected_move_pct, "EXPECTED MOVE");
    setProv("chain:bias", pv.bias, "POSITIONING BIAS");
    setProv("chain:doi", modelled ? null : pv.delta_oi, "ΔOI");

    $("#ci-bias-w").textContent = `Positioning: ${a.bias.toUpperCase()}`;
    $("#ci-bias-y").textContent = a.bias_reason ? `— ${a.bias_reason}` : "";
    $("#ci-move-w").textContent =
      `Implied move ±${(a.expected_move_pct * 100).toFixed(1)}% by ${c.expiry}`;
    $("#ci-move-y").textContent =
      `— ATM straddle ${fmt.i(a.straddle_price)} / spot ${fmt.i(c.spot)} — the market's own range estimate`;
    // Unusual activity is volume vs OI; on a modelled chain both are invented
    // (synthetic.py:73-75 plants two volume spikes so the detector finds something).
    const un = modelled ? [] : (a.unusual || []).slice(0, 3);
    $("#ci-unusual").style.display = un.length ? "" : "none";
    if (un.length) {
      $("#ci-unusual-w").textContent =
        `Unusual activity: ${un.map((u) => `${u.side} ${fmt.i(u.strike)}`).join(", ")}`;
      $("#ci-unusual-y").textContent =
        `— volume ${un.map((u) => u.ratio.toFixed(1) + "×").join(", ")} of OI — fresh positioning, not rollover`;
    }

    const rows = c.rows || [];
    const key = rows.length
      ? `${c.expiry}|${rows[0].strike}|${rows[rows.length - 1].strike}|${rows.length}` : "";
    const rebuilt = key !== strikeKey;
    if (rebuilt) { strikeKey = key; buildRows(rows); }

    const maxC = Math.max(...rows.map((r) => r.call.oi), 1);
    const maxP = Math.max(...rows.map((r) => r.put.oi), 1);
    const dOI = (td, v) => {
      if (modelled || v === null || v === undefined) { td.textContent = "—"; td.className = "faint"; return; }
      td.textContent = fmt.compact(v);
      td.className = cls(v);
    };
    rows.forEach((r, i) => {
      const q = cells[i];
      if (!q) return;
      q.tr.className = r.atm ? "atm-row" : "";
      q.cBar.style.width = `${(r.call.oi / maxC) * 100}%`;
      q.cOI.textContent = fmt.compact(r.call.oi);
      dOI(q.cDOI, r.call.oi_change);
      q.cVol.textContent = fmt.compact(r.call.volume);
      q.cIV.textContent = r.call.iv ? (r.call.iv * 100).toFixed(1) : "—";
      q.cLTP.textContent = fmt.n(r.call.ltp);
      q.cLTP.dataset.strike = r.strike;
      q.cLTP.dataset.ltp = r.call.ltp;
      q.strike.textContent = fmt.i(r.strike);
      q.strike.className = `k-strike${r.atm ? " amber" : ""}`;
      q.pLTP.textContent = fmt.n(r.put.ltp);
      q.pLTP.dataset.strike = r.strike;
      q.pLTP.dataset.ltp = r.put.ltp;
      q.pIV.textContent = r.put.iv ? (r.put.iv * 100).toFixed(1) : "—";
      q.pVol.textContent = fmt.compact(r.put.volume);
      dOI(q.pDOI, r.put.oi_change);
      q.pBar.style.width = `${(r.put.oi / maxP) * 100}%`;
      q.pOI.textContent = fmt.compact(r.put.oi);
    });

    $("#chain-win").textContent = c.rows_total > rows.length
      ? `${rows.length} OF ${c.rows_total} STRIKES · ATM±${c.strike_window}`
      : `${rows.length} STRIKES`;
    $("#chain-doi").innerHTML = modelled
      ? `<span class="amber">ΔOI + UNUSUAL SUPPRESSED — MODELLED CHAIN</span>`
      : stamp(`ΔOI BASIS: ${c.delta_oi_basis.toUpperCase()}`);

    if (rebuilt) {
      // Centre the ATM row in the strike scroller. scrollIntoView walks every
      // scrollable ancestor and would jerk `main`, so scroll the box itself.
      const atm = cells[rows.findIndex((r) => r.atm)];
      const box = $("#chain-scroll");
      if (atm) box.scrollTop = Math.max(atm.tr.offsetTop - (box.clientHeight - atm.tr.offsetHeight) / 2, 0);
    }
    painted = true;

    // Straddle chart from today's REAL captured snapshots (>=2 needed).
    const sd = c.straddle_today || [];
    const host = $("#chain-straddle");
    if (sd.length >= 2) {
      const fresh = !straddleSeries;
      if (fresh) {
        host.innerHTML = panel({ title: "ATM STRADDLE — TODAY", id: "chain-str", flush: true,
          meta: `<span id="chain-str-meta">—</span>`,
          body: `<div class="chart-host mini" id="straddle-chart"></div>` });
        straddleChart = mkChart($("#straddle-chart"));
        straddleSeries = straddleChart.addSeries(LWC.LineSeries, { color: "#f0a826", lineWidth: 2 });
      }
      straddleSeries.setData(sd.map((s) => ({ time: s.time, value: s.value })));
      $("#chain-str-meta").innerHTML =
        stamp(`${sd.length} LOCAL SNAPSHOTS · CAPTURED EVERY ~10 MIN MARKET HOURS`);
      if (fresh) requestAnimationFrame(() => straddleChart.timeScale().fitContent());
    } else if (straddleSeries) {
      // mkChart handed this one to destroyCharts(); drop it by hand so an
      // expiry with no captured snapshots leaves no live chart behind.
      _dropChart(straddleChart);
      straddleChart = straddleSeries = null;
      host.innerHTML = "";
    }
  };

  // -- order ticket ---------------------------------------------------------
  // Click a premium to book that leg. Desk habits: it opens on SELL for an
  // option (premium selling is the default intent), S/B flip the side, digits
  // set lots, Enter books, Esc closes. Never a market order behind your back —
  // it books at the printed premium, which is what you just clicked.
  let ticket = null, last = null;
  const closeTicket = () => { ticket?.el.remove(); ticket = null; };

  const openTicket = (cell, chain) => {
    closeTicket();
    const right = cell.dataset.right;
    const strike = Number(cell.dataset.strike);
    const px = Number(cell.dataset.ltp);
    if (!Number.isFinite(px) || px <= 0) return toast("no premium on that strike", "err");

    const el = elv("div", "ticket");
    const label = `${chain.symbol} ${chain.expiry} ${fmt.i(strike)} ${right}`;
    let side = "SELL", lots = 1;
    const draw = () => {
      const lotTxt = chain.lot_size ? `${lots} LOT (${lots * chain.lot_size})` : `${lots} (no lot size)`;
      el.innerHTML = `
        <div class="ticket-head">${esc(label)}</div>
        <div class="ticket-row">
          <button class="tk-side ${side === "BUY" ? "on-buy" : ""}" data-side="BUY">BUY</button>
          <button class="tk-side ${side === "SELL" ? "on-sell" : ""}" data-side="SELL">SELL</button>
          <span class="tk-lots">${lotTxt}</span>
          <span class="tk-px">@ ${fmt.n(px)}</span>
        </div>
        <div class="ticket-foot">
          <span>${side === "SELL" ? "premium received" : "premium paid"}
            ${chain.lot_size ? `<b>${fmt.n(px * lots * chain.lot_size)}</b>` : "—"}</span>
          <span class="tk-keys">${chain.lot_size
            ? "S/B side · 1-9 lots · ↵ book · esc"
            : "no lot size — cannot book"}</span>
        </div>`;
      el.querySelectorAll(".tk-side").forEach((b) => {
        b.onclick = () => { side = b.dataset.side; draw(); };
      });
    };
    draw();

    const book = async () => {
      if (!chain.lot_size) {
        return toast(
          `No lot size for ${chain.symbol} — cannot size in lots. `
          + `Reconnect a source that names the contract lot.`, "err");
      }
      try {
        const r = await postJSON("/api/portfolio/trade", {
          side, symbol: chain.symbol, kind: right, expiry: chain.expiry,
          strike, lot_size: chain.lot_size, lots, price: px,
        });
        toast(`${side} ${r.label} x${r.quantity} @ ${fmt.n(r.price)}`, "ok");
        closeTicket();
      } catch (e) { toast(e.message, "err"); }
    };

    el.tabIndex = 0;
    el.onkeydown = (ev) => {
      if (ev.key === "Escape") return closeTicket();
      if (ev.key === "Enter") return book();
      if (ev.key.toLowerCase() === "s") { side = "SELL"; return draw(); }
      if (ev.key.toLowerCase() === "b") { side = "BUY"; return draw(); }
      if (/^[1-9]$/.test(ev.key)) { lots = Number(ev.key); return draw(); }
    };
    const box = cell.getBoundingClientRect();
    el.style.top = `${box.bottom + 4}px`;
    el.style.left = `${Math.min(box.left, innerWidth - 260)}px`;
    document.body.appendChild(el);
    el.focus();
    ticket = { el };
  };

  const load = async () => {
    try {
      const c = await getJSON(`/api/chain/${sym}${expiry ? `?expiry=${expiry}` : ""}`);
      if (!document.body.contains(view)) return;  // view switched mid-flight
      paint(c);
      last = c;
    } catch (e) {
      // A failed poll must never wipe the last good table.
      $("#chain-upd").innerHTML = `<span class="down">REFRESH FAILED ${fmt.ist()}</span>`;
      if (painted) return;  // a failed poll must never wipe the last good table
      const trail = (e.detail && e.detail.source_trail) || [];
      $("#chain-rows").innerHTML = `<tr><td colspan="11">
        <div class="refusal">
          <div class="refusal-head">NO LIVE OPTION CHAIN</div>
          <p>Shunkan will not show a modelled book in place of one it could not
             source — every number below the chain would inherit the label.</p>
          ${trail.length ? `<ul class="refusal-trail">${
            trail.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>`
            : `<p class="refusal-why">${esc(e.message)}</p>`}
          <button class="tbtn" id="chain-retry">RETRY</button>
        </div></td></tr>`;
      const retry = $("#chain-retry");
      if (retry) retry.onclick = load;
    }
  };
  load();
  addTimer("chain:refresh", load, 60000);

  $("#chain-rows").addEventListener("click", (ev) => {
    const cell = ev.target.closest(".ltp-cell");
    if (cell && last) openTicket(cell, last);
  });
  onView(document, "mousedown", (ev) => {
    if (ticket && !ticket.el.contains(ev.target) && !ev.target.closest(".ltp-cell")) {
      closeTicket();
    }
  });
}

/* ---------- PAYOFF ---------- */

const STRAT_LIST = ["iron_condor", "iron_fly", "short_straddle", "long_straddle",
  "short_strangle", "long_strangle", "bull_call_spread", "bear_put_spread"];

async function renderPayoff(view, params) {
  const sym = state.symbol;
  const strat = params.strategy || "iron_condor";
  view.innerHTML = panel({
    title: `STRATEGY PAYOFF — <span class="hl">${sym}</span>`, id: "pay-panel", flush: true,
    meta: `<span class="controls">
      <input class="in" id="pay-sym" value="${sym}" size="7">
      <select class="in" id="pay-strat">${STRAT_LIST.map((s) =>
        `<option ${s === strat ? "selected" : ""}>${s}</option>`).join("")}</select>
      <input class="in" id="pay-legs" placeholder="+23200CE,-23400CE" size="18">
      <button class="btn" id="pay-go">BUILD</button></span>`,
    body: loading("pricing"),
  });
  $("#pay-go").onclick = () => loadPayoff($("#pay-sym").value, $("#pay-strat").value, $("#pay-legs").value);
  loadPayoff(sym, strat, "");
}

async function loadPayoff(sym, strat, legs) {
  state.symbol = sym.toUpperCase();
  const body = $("#pay-panel .panel-body");
  body.innerHTML = loading("pricing");
  try {
    const url = legs.trim()
      ? `/api/payoff/${sym}?legs=${encodeURIComponent(legs.trim())}`
      : `/api/payoff/${sym}?strategy=${strat}`;
    const p = await getJSON(url);
    // No source could name the contract lot -> every ₹ figure is per unit.
    const UNIT = p.per_lot ? "lot" : "unit";
    body.innerHTML = `
      <div class="row cols-main-side" style="padding:10px 12px">
        <div><canvas class="plot" id="pay-canvas"></canvas></div>
        <div class="stats">
          ${p.legs.map((l) => `<div class="stat"><span class="k">LEG</span><span class="v">${l}</span></div>`).join("")}
          <div class="stat"><span class="k">Net premium /${UNIT}</span>
            <span class="v ${cls(p.net_premium_lot)}">₹${fmt.n(Math.abs(p.net_premium_lot), 0)} ${p.net_premium_lot >= 0 ? "CR" : "DR"}</span></div>
          <div class="stat"><span class="k">Max profit /${UNIT}</span><span class="v up">${p.unlimited_profit ? "UNLIMITED" : "₹" + fmt.n(p.max_profit, 0)}</span></div>
          <div class="stat"><span class="k">Max loss /${UNIT}</span><span class="v down">${p.unlimited_loss ? "UNLIMITED" : "₹" + fmt.n(p.max_loss, 0)}</span></div>
          <div class="stat"><span class="k">Breakevens</span><span class="v">${p.breakevens.map((b) => fmt.i(b)).join(" · ") || "—"}</span></div>
          <div class="stat"><span class="k">POP (model)${iM((p.prov || {}).pop, "PROBABILITY OF PROFIT")}</span><span class="v amber">${(p.pop * 100).toFixed(0)}%</span></div>
          <div class="stat"><span class="k">Delta /${UNIT}${iM((p.prov || {}).greeks, "POSITION GREEKS")}</span><span class="v">${fmt.n(p.greeks.delta)}</span></div>
          <div class="stat"><span class="k">Gamma /${UNIT}</span><span class="v">${fmt.n(p.greeks.gamma, 4)}</span></div>
          <div class="stat"><span class="k">Theta /${UNIT} /day</span><span class="v ${cls(p.greeks.theta)}">₹${fmt.n(p.greeks.theta)}</span></div>
          <div class="stat"><span class="k">Vega /${UNIT} /volpt</span><span class="v">₹${fmt.n(p.greeks.vega)}</span></div>
        </div>
      </div>
      ${insightBlock([
        { what: p.net_premium_lot >= 0
            ? `Theta-positive structure earning ₹${fmt.n(p.greeks.theta, 0)}/day per ${UNIT}`
            : `Theta-negative: paying ₹${fmt.n(-p.greeks.theta, 0)}/day per ${UNIT} for the position`,
          why: p.net_premium_lot >= 0
            ? "credit structures profit from time decay while price holds the range"
            : "debit structures need the move to arrive before decay erodes the premium" },
        { what: `Model POP ${(p.pop * 100).toFixed(0)}% vs risk:reward ${p.max_loss && p.max_profit ? Math.abs(p.max_profit / p.max_loss).toFixed(2) : "—"}`,
          why: "lognormal estimate at ATM IV — high POP with poor R:R means many small wins, rare large losses" },
        { what: `Breakevens ${p.breakevens.map((b) => fmt.i(b)).join(" / ")}`,
          why: `spot ${fmt.i(p.spot)} must stay ${p.net_premium_lot >= 0 ? "inside" : "outside"} this band by ${p.expiry}` },
      ])}
      <div class="faint" style="padding:6px 12px;font-size:10px">${p.source} · POP is model-based, not market-implied · excludes margin, fees, slippage · not advice</div>`;
    $("#pay-panel .panel-meta").innerHTML = stamp(`LOT ${p.lot_size ?? "—"} · EXP ${p.expiry}`) +
      ` <span class="controls" style="display:inline-flex;margin-left:8px">
        <input class="in" id="pay-sym" value="${sym}" size="7">
        <select class="in" id="pay-strat">${STRAT_LIST.map((s) =>
          `<option ${s === strat ? "selected" : ""}>${s}</option>`).join("")}</select>
        <input class="in" id="pay-legs" placeholder="+23200CE,-23400CE" size="18">
        <button class="btn" id="pay-go">BUILD</button></span>`;
    $("#pay-go").onclick = () => loadPayoff($("#pay-sym").value, $("#pay-strat").value, $("#pay-legs").value);
    linePlot($("#pay-canvas"), [
      { points: p.curve, color: "#f0a826", width: 2, fillZero: true },
    ], {
      height: 340, zeroLine: true,
      vlines: [
        { x: p.spot, color: "rgba(88,166,255,0.85)", label: `SPOT ${fmt.i(p.spot)}`, dash: [2, 2] },
        ...p.breakevens.map((b) => ({ x: b, color: "rgba(255,255,255,0.35)", label: fmt.i(b) })),
      ],
      fmtY: (v) => "₹" + fmt.compact(v),
    });
  } catch (e) {
    body.innerHTML = `<div class="empty">${e.message}</div>`;
  }
}

/* ---------- IV ---------- */

async function renderIV(view) {
  const sym = state.symbol;
  view.innerHTML = panel({
    title: `VOLATILITY — <span class="hl">${sym}</span>`, id: "iv-panel", flush: true,
    meta: `<span class="controls"><input class="in" id="iv-sym" value="${sym}" size="8">
           <button class="btn" id="iv-go">LOAD</button></span>`,
    body: loading("solving surface"),
  });
  $("#iv-go").onclick = () => show("iv", { symbol: $("#iv-sym").value });
  try {
    const r = await getJSON(`/api/iv/${sym}`);
    const pv = r.prov || {};
    const rank = r.iv_rank_local || {};
    const rankCell = rank.available
      ? `<div class="v amber">${(rank.rank * 100).toFixed(0)}%</div>`
      : `<div class="v sm faint">${rank.days_captured ?? 0}d/${rank.days_required ?? 20}d</div>`;
    $("#iv-panel .panel-meta").innerHTML = stamp(`EXP ${r.expiry}`);
    $("#iv-panel .panel-body").innerHTML = `
      <div class="kv-strip">
        <div class="kv"><div class="k">ATM IV</div><div class="v">${(r.atm_iv * 100).toFixed(1)}%</div></div>
        <div class="kv"><div class="k">RV 21D CC${iM(pv.rv_cc_21, "REALIZED VOL")}</div><div class="v">${(r.rv_cc_21 * 100).toFixed(1)}%</div></div>
        <div class="kv"><div class="k">RV 21D PARK${iM(pv.rv_park_21, "PARKINSON VOL")}</div><div class="v">${(r.rv_park_21 * 100).toFixed(1)}%</div></div>
        <div class="kv"><div class="k">IV PREMIUM${iM(pv.iv_premium, "IV PREMIUM")}</div><div class="v ${cls(r.iv_premium)}">${(r.iv_premium * 100).toFixed(1)}pt</div></div>
        <div class="kv"><div class="k">RV %ILE 1Y${iM(pv.rv_percentile, "RV PERCENTILE")}</div><div class="v">${(r.rv_percentile * 100).toFixed(0)}%</div></div>
        <div class="kv"><div class="k">IV RANK LOCAL${iM(pv.iv_rank_local, "IV RANK (LOCAL)")}</div>${rankCell}</div>
      </div>
      <div class="row cols-main-side" style="padding:10px 12px">
        <div><canvas class="plot" id="iv-canvas"></canvas></div>
        <div>
          <div class="panel-title" style="margin-bottom:7px">EXPECTED MOVE ±1σ/±2σ${iM(pv.cone, "EXPECTED-MOVE CONE")}</div>
          <table class="tbl"><thead><tr><th>HORIZON</th><th>-2σ</th><th>-1σ</th><th>+1σ</th><th>+2σ</th></tr></thead>
          <tbody>${Object.entries(r.cone).map(([d, [lo2, lo1, hi1, hi2]]) => `
            <tr><td class="txt">${d}D</td><td class="down">${fmt.i(lo2)}</td><td>${fmt.i(lo1)}</td>
            <td>${fmt.i(hi1)}</td><td class="up">${fmt.i(hi2)}</td></tr>`).join("")}</tbody></table>
        </div>
      </div>
      ${insightBlock([
        { what: `Options priced ${r.iv_premium > 0.02 ? "RICH" : r.iv_premium < -0.02 ? "CHEAP" : "FAIR"} vs realized`,
          why: `ATM IV ${(r.atm_iv * 100).toFixed(1)}% against ${(r.rv_cc_21 * 100).toFixed(1)}% actually realized over 21 days (${(r.iv_premium * 100).toFixed(1)}pt spread)` },
        rank.available
          ? { what: `IV rank ${(rank.rank * 100).toFixed(0)}% over ${rank.days_captured} locally captured days`,
              why: `range ${(rank.min_iv * 100).toFixed(1)}–${(rank.max_iv * 100).toFixed(1)}% since ${rank.first_day} — real observations, no proxy` }
          : { what: `IV rank: not reported yet (${rank.days_captured ?? 0}/${rank.days_required ?? 20} days captured)`,
              why: "a rank needs real local IV history; Shunkan refuses to fabricate one — RV percentile is the honest stand-in meanwhile" },
        ...r.notes.map((n) => {
          const [what, ...why] = n.split(" — ");
          return { what, why: why.join(" — ") };
        }),
      ])}`;
    linePlot($("#iv-canvas"), [
      { points: r.smile.map((s) => ({ x: s.strike, y: s.call_iv ? s.call_iv * 100 : null })), color: "#58a6ff", width: 1.6 },
      { points: r.smile.map((s) => ({ x: s.strike, y: s.put_iv ? s.put_iv * 100 : null })), color: "#f0a826", width: 1.6 },
    ], {
      height: 300,
      vlines: [{ x: r.spot, color: "rgba(255,255,255,0.3)", label: "SPOT" }],
      fmtY: (v) => v.toFixed(0) + "%",
    });
  } catch (e) {
    $("#iv-panel .panel-body").innerHTML = `<div class="empty">${e.message}</div>`;
  }
}

/* ---------- VOLUME / FLOW ---------- */

async function renderVolume(view) {
  const sym = state.symbol;
  view.innerHTML = panel({
    title: `VOLUME FLOW — <span class="hl">${sym}</span>`, id: "vol-panel", flush: true,
    meta: `<span class="controls"><input class="in" id="vol-sym" value="${sym}" size="8">
           <button class="btn" id="vol-go">ANALYZE</button></span>`,
    body: loading("profiling"),
  });
  $("#vol-go").onclick = () => show("volume", { symbol: $("#vol-sym").value });
  try {
    const r = await getJSON(`/api/volume/${sym}`);
    const maxV = Math.max(...r.profile.map((p) => p.volume), 1);
    const pocVol = Math.max(...r.profile.map((p) => p.volume));
    $("#vol-panel .panel-meta").innerHTML = stamp("120 BARS PROFILE");
    $("#vol-panel .panel-body").innerHTML = `
      <div class="kv-strip">
        <div class="kv"><div class="k">LAST</div><div class="v">${fmt.n(r.last_close)}</div></div>
        <div class="kv"><div class="k">VOL VS AVG</div><div class="v">${r.surge_ratio.toFixed(2)}×</div></div>
        <div class="kv"><div class="k">Z-SCORE${iM((r.prov || {}).surge_z, "VOLUME Z-SCORE")}</div><div class="v ${cls(r.surge_z)}">${r.surge_z >= 0 ? "+" : ""}${r.surge_z.toFixed(2)}σ</div></div>
        <div class="kv"><div class="k">POC${iM((r.prov || {}).poc, "POINT OF CONTROL")}</div><div class="v amber">${fmt.i(r.poc)}</div></div>
        <div class="kv"><div class="k">VALUE AREA</div><div class="v sm">${fmt.i(r.value_area[0])}–${fmt.i(r.value_area[1])}</div></div>
        <div class="kv"><div class="k">DAY TYPE</div><div class="v sm">${r.day_type.split(" (")[0].toUpperCase()}</div></div>
      </div>
      <div style="padding:10px 12px" id="vol-profile"></div>
      ${insightBlock([
        { what: `Day type: ${r.day_type.split(" (")[0]}`,
          why: r.day_type.includes("(") ? r.day_type.split("(")[1].replace(")", "") : `volume ${r.surge_ratio.toFixed(2)}× its 20-bar average` },
        ...(r.obv_divergence !== "none" ? [{
          what: r.obv_divergence.split(" (")[0],
          why: r.obv_divergence.includes("(") ? r.obv_divergence.split("(")[1].replace(")", "") : "" }] : []),
        ...r.notes.map((n) => {
          const [what, ...why] = n.split(" — ");
          return { what, why: why.join(" — ") };
        }),
      ])}`;
    const host = $("#vol-profile");
    [...r.profile].reverse().forEach((p) => {
      const inVA = p.price >= r.value_area[0] && p.price <= r.value_area[1];
      const isPoc = p.volume === pocVol;
      host.appendChild(elv("div", "vp-row", `
        <span class="vp-price">${fmt.i(p.price)}</span>
        <div class="vp-track"><div class="vp-fill ${isPoc ? "poc" : inVA ? "va" : ""}"
          style="width:${(p.volume / maxV) * 100}%"></div></div>
        <span class="faint mono" style="font-size:9px;width:48px">${fmt.compact(p.volume)}</span>`));
    });
  } catch (e) {
    $("#vol-panel .panel-body").innerHTML = `<div class="empty">${e.message}</div>`;
  }
}

/* ---------- NEWS ---------- */

async function renderNews(view, params) {
  const sym = params.symbol || "";
  view.innerHTML = panel({
    title: `NEWS INTELLIGENCE ${sym ? `— <span class="hl">${sym}</span>` : "— INDIAN MARKETS"}`,
    id: "news-panel", flush: true,
    meta: `<span class="controls"><input class="in" id="news-sym" value="${sym}" placeholder="market-wide" size="9">
           <button class="btn" id="news-go">LOAD</button></span>`,
    body: loading("reading wires"),
  });
  $("#news-go").onclick = () => show("news", { symbol: $("#news-sym").value });

  const draw = async () => {
    try {
      const n = await getJSON(`/api/news${sym ? `?symbol=${sym}` : ""}`);
      const b = n.bias;
      const host = $("#news-panel .panel-body");
      if (!host) return;
      $("#news-panel .panel-meta").innerHTML = stamp("AUTO 60s · SOURCE LAG 5–15M");
      const biasCls = b.label.includes("bullish") ? "up" : b.label.includes("bearish") ? "down" : "dim";
      host.innerHTML = `
        <div class="feed-note">SOURCE: GOOGLE NEWS RSS (IN) — aggregator runs 5–15 min behind the original wire.
          Ages use publish time. Very fresh items may already be priced in.</div>
        <div style="padding:9px 12px;border-bottom:1px solid var(--stroke-soft);display:flex;gap:12px;align-items:baseline;flex-wrap:wrap">
          <span class="${biasCls}" style="font-weight:700;font-size:14px">AGGREGATE: ${b.label.toUpperCase()}</span>
          <span class="mono dim">${b.score >= 0 ? "+" : ""}${b.score.toFixed(2)}</span>${iM(b.prov, "AGGREGATE BIAS")}
          <span class="faint" style="font-size:10px">${b.n_items} items · recency-decayed (6h half-life)</span>
          ${b.gap_call ? `<span class="badge amb">${b.gap_call.toUpperCase()}</span>` : ""}
        </div>
        ${n.items.map((it) => {
          const sb = it.sentiment_label.includes("bullish") ? "bull" :
                     it.sentiment_label.includes("bearish") ? "bear" : "";
          return `<div class="news-row">
            <div class="n-line1">
              <span class="n-age">${fmt.age(it.age_minutes)}</span>
              <span class="n-title">${it.link ? `<a href="${it.link}" target="_blank" rel="noopener">${it.title}</a>` : it.title}</span>
            </div>
            <div class="n-line2">
              <span class="badge ${sb}">${it.sentiment_label} ${it.sentiment >= 0 ? "+" : ""}${it.sentiment.toFixed(2)}</span>${iM((it.prov || {}).sentiment, "SENTIMENT SCORE")}
              <span class="badge">${it.impact.category.replace(/_/g, " ")}</span>
              <span class="faint" style="font-size:10px">${it.source}${it.published ? ` · ${new Date(it.published).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false })} IST` : ""}</span>
            </div>
            <div class="n-impact">CALL: <b class="${it.impact.direction === "bullish" ? "up" : it.impact.direction === "bearish" ? "down" : "dim"}">${it.impact.direction.toUpperCase()}</b>
              ${(it.impact.confidence * 100).toFixed(0)}% conf${iM((it.prov || {}).confidence, "IMPACT CONFIDENCE")} · ${it.impact.magnitude} · ${it.impact.horizon} · ${it.impact.segment}
              ${it.summary ? `<span class="why"> — ${it.summary}</span>` : ""}</div>
          </div>`;
        }).join("")}`;
    } catch (e) {
      const host = $("#news-panel .panel-body");
      if (host) host.innerHTML = `<div class="empty">${e.message}</div>`;
    }
  };
  draw();
  addTimer("news:draw", draw, 60000);
}

/* ---------- BACKTEST LAB ---------- */

async function renderBacktest(view) {
  const sym = state.symbol;
  view.innerHTML = panel({
    title: `STRATEGY LAB — <span class="hl">${sym}</span>`, id: "bt-panel", flush: true,
    meta: `<span class="controls">
      <button class="tbtn active" id="bt-tab-preset">PRESET</button>
      <button class="tbtn" id="bt-tab-builder">BUILDER</button></span>`,
    body: `<div id="bt-content"></div>`,
  });
  const tabs = { preset: showPresetTab, builder: showBuilderTab };
  for (const k of Object.keys(tabs)) {
    $(`#bt-tab-${k}`).onclick = () => {
      $("#bt-tab-preset").classList.toggle("active", k === "preset");
      $("#bt-tab-builder").classList.toggle("active", k === "builder");
      tabs[k]();
    };
  }
  showPresetTab();
}

async function showPresetTab() {
  const sym = state.symbol;
  $("#bt-content").innerHTML = `
    <div class="controls" style="padding:9px 12px">
      <input class="in" id="bt-sym" value="${sym}" size="7">
      <select class="in" id="bt-strat"></select>
      <input class="in" id="bt-params" placeholder="fast=20 slow=50" size="13">
      <select class="in" id="bt-period">${["1y","2y","5y","10y"].map((p) =>
        `<option ${p === "5y" ? "selected" : ""}>${p}</option>`).join("")}</select>
      <select class="in" id="bt-mode">
        <option value="backtest">BACKTEST</option>
        <option value="walkforward">WALK-FWD</option>
        <option value="montecarlo">MONTE CARLO</option>
      </select>
      <button class="btn" id="bt-go">RUN</button>
    </div>
    <div id="bt-result"><div class="empty">Backtest, then validate: walk-forward asks "do optimized params survive out-of-sample?", Monte Carlo asks "is the equity curve luck?"</div></div>`;
  try {
    const strats = await getJSON("/api/strategies");
    $("#bt-strat").innerHTML = Object.keys(strats).filter((s) => s !== "buy_hold")
      .map((s) => `<option>${s}</option>`).join("");
  } catch {}
  $("#bt-go").onclick = runBacktest;
}

async function runBacktest() {
  const body = $("#bt-result");
  const mode = $("#bt-mode").value;
  body.innerHTML = loading(mode === "montecarlo" ? "bootstrapping 2,000 histories" :
    mode === "walkforward" ? "optimizing in-sample / testing out-of-sample" : "vectorized backtest");
  const params = {};
  $("#bt-params").value.split(/\s+/).filter(Boolean).forEach((kv) => {
    const [k, v] = kv.split("=");
    if (k && v && !isNaN(parseFloat(v))) params[k] = parseFloat(v);
  });
  try {
    const r = await postJSON("/api/backtest", {
      symbol: $("#bt-sym").value, strategy: $("#bt-strat").value,
      params, period: $("#bt-period").value, mode,
    });
    state.symbol = $("#bt-sym").value.toUpperCase();
    $("#bt-panel .panel-meta") && ($("#bt-panel .panel-head .panel-meta") || {});
    if (r.mode === "backtest") drawBacktest(body, r);
    else if (r.mode === "walkforward") drawWalkforward(body, r);
    else drawMonteCarlo(body, r);
  } catch (e) {
    body.innerHTML = `<div class="empty">${e.message}</div>`;
  }
}

function metricsStrip(pairs) {
  return `<div class="kv-strip">${pairs.map(([k, v, c]) =>
    `<div class="kv"><div class="k">${k}</div><div class="v ${c || ""}">${v}</div></div>`).join("")}</div>`;
}

function drawBacktest(body, r) {
  const alpha = r.metrics.total_return - r.bench_return;
  body.innerHTML = `
    ${metricsStrip([
      ["TOTAL RET", fmt.pct(r.metrics.total_return), cls(r.metrics.total_return)],
      ["VS B&H", fmt.pct(alpha), cls(alpha)],
      ["SHARPE", r.metrics.sharpe.toFixed(2)],
      ["SORTINO", r.metrics.sortino === null ? "∞" : r.metrics.sortino.toFixed(2)],
      ["MAX DD", fmt.pct(r.metrics.max_drawdown), "down"],
      ["WIN RATE", (r.metrics.win_rate * 100).toFixed(0) + "%"],
      ["TRADES", r.metrics.num_trades],
      ["EXPOSURE", (r.metrics.exposure * 100).toFixed(0) + "%"],
    ])}
    <div class="chart-host short" id="bt-chart" style="margin-top:2px"></div>
    ${insightBlock([
      { what: alpha >= 0 ? `Strategy beat buy & hold by ${fmt.pct(alpha)}` : `Strategy LOST to buy & hold by ${fmt.pct(-alpha)}`,
        why: `strategy ${fmt.pct(r.metrics.total_return)} vs benchmark ${fmt.pct(r.bench_return)} — before validating, run WALK-FWD and MONTE CARLO` },
      { what: `Drawdown ${fmt.pct(r.metrics.max_drawdown)} with Sharpe ${r.metrics.sharpe.toFixed(2)}`,
        why: r.metrics.sharpe > 1 ? "decent risk-adjusted ratio, but single-history — validate" : "weak risk-adjusted return; edge may be noise" },
    ])}`;
  const chart = mkChart($("#bt-chart"));
  chart.addSeries(LWC.LineSeries, { color: "#f0a826", lineWidth: 2 }).setData(r.equity);
  chart.addSeries(LWC.LineSeries, { color: "rgba(88,166,255,0.65)", lineWidth: 1 }).setData(r.bench_equity);
  chart.timeScale().fitContent();
}

function drawWalkforward(body, r) {
  const vCls = r.verdict.includes("robust") ? "good" : r.verdict.includes("fragile") ? "warn" : "bad";
  body.innerHTML = `
    ${metricsStrip([
      ["OOS RETURN", fmt.pct(r.oos_return), cls(r.oos_return)],
      ["OOS SHARPE", r.oos_sharpe.toFixed(2)],
      ["OOS MAX DD", fmt.pct(r.oos_max_dd), "down"],
      ["EFFICIENCY", (r.efficiency * 100).toFixed(0) + "%"],
      ["PARAM STABILITY", (r.param_stability * 100).toFixed(0) + "%"],
    ])}
    <div style="padding:9px 12px"><div class="verdict ${vCls}">${r.verdict.toUpperCase()}</div></div>
    <div class="chart-host short" id="wf-chart"></div>
    <div style="padding:0 12px 10px">
    <table class="tbl"><thead><tr><th>TEST FROM</th><th>IS SHARPE</th><th>OOS SHARPE</th><th>OOS RET</th><th>PARAMS</th></tr></thead>
    <tbody>${r.windows.map((w) => `<tr>
      <td class="txt">${w.test_start}</td><td>${w.is_sharpe.toFixed(2)}</td>
      <td class="${cls(w.oos_sharpe)}">${w.oos_sharpe.toFixed(2)}</td>
      <td class="${cls(w.oos_return)}">${fmt.pct(w.oos_return)}</td>
      <td class="faint">${Object.entries(w.params).map(([k, v]) => `${k}=${v}`).join(" ")}</td></tr>`).join("")}
    </tbody></table></div>
    ${insightBlock([
      { what: `Out-of-sample efficiency ${(r.efficiency * 100).toFixed(0)}%`,
        why: `OOS Sharpe ${r.oos_sharpe.toFixed(2)} vs in-sample mean ${r.is_sharpe_mean.toFixed(2)} — below ~50% means the optimizer fit noise, not signal` },
      { what: `Parameters ${r.param_stability >= 0.75 ? "stable" : "unstable"} across windows (${(r.param_stability * 100).toFixed(0)}%)`,
        why: "if each era picks different parameters, there is no single tradable configuration" },
    ])}`;
  if (r.equity.length) {
    const chart = mkChart($("#wf-chart"));
    chart.addSeries(LWC.LineSeries, { color: "#f0a826", lineWidth: 2 }).setData(r.equity);
    chart.timeScale().fitContent();
  }
}

function drawMonteCarlo(body, r) {
  const vCls = r.verdict.includes("favorable") && !r.verdict.includes("un") ? "good" :
    r.verdict.includes("coin") ? "warn" : "bad";
  body.innerHTML = `
    ${metricsStrip([
      ["P(LOSS)", (r.prob_loss * 100).toFixed(0) + "%", r.prob_loss > 0.4 ? "down" : "up"],
      ["TERM P5", fmt.pct(r.terminal.p5 - 1), "down"],
      ["TERM MEDIAN", fmt.pct(r.terminal.p50 - 1), cls(r.terminal.p50 - 1)],
      ["TERM P95", fmt.pct(r.terminal.p95 - 1), "up"],
      ["DD MEDIAN", fmt.pct(r.max_dd_median), "down"],
      ["DD TAIL 5%", fmt.pct(r.max_dd_p95), "down"],
      ["PATHS", r.n_paths.toLocaleString()],
    ])}
    <div style="padding:9px 12px"><div class="verdict ${vCls}">${r.verdict.toUpperCase()}</div></div>
    <div class="chart-host short" id="mc-chart"></div>
    ${insightBlock([
      { what: `${(r.prob_loss * 100).toFixed(0)}% of resampled histories end at a loss`,
        why: "block-bootstrap reshuffles the strategy's own daily returns — if most orderings lose, the original curve was sequence luck" },
      { what: `Drawdown tail: 5% of histories exceed ${fmt.pct(r.max_dd_p95)}`,
        why: "size positions for the tail, not the median — the tail is what ends accounts" },
    ])}`;
  const chart = mkChart($("#mc-chart"));
  [["p95", "rgba(46,189,133,0.6)", 1], ["p50", "#f0a826", 2],
   ["p5", "rgba(241,86,75,0.6)", 1], ["actual", "rgba(88,166,255,0.8)", 1]]
    .forEach(([key, color, width]) => {
      chart.addSeries(LWC.LineSeries, { color, lineWidth: width }).setData(r.bands[key]);
    });
  chart.timeScale().fitContent();
}

/* ---------- STRATEGY BUILDER (no-code) ---------- */

let BUILDER_CAT = null;  // /api/builder/indicators catalog, fetched once

function _indOptions(selected) {
  const cats = {};
  for (const [kind, m] of Object.entries(BUILDER_CAT.indicators)) {
    (cats[m.category] ||= []).push([kind, m]);
  }
  return Object.entries(cats).map(([cat, items]) =>
    `<optgroup label="${cat.toUpperCase()}">${items.map(([kind, m]) =>
      `<option value="${kind}" data-period="${m.period ? 1 : 0}" data-default="${m.default || 14}"
        ${kind === selected ? "selected" : ""}>${m.label}</option>`).join("")}</optgroup>`).join("");
}

function _opOptions(selected) {
  return Object.entries(BUILDER_CAT.operators).map(([op, label]) =>
    `<option value="${op}" ${op === selected ? "selected" : ""}>${label}</option>`).join("");
}

function _modeOptions(selected) {
  const labels = { none: "off", percent: "%", pips: "pips", atr: "× ATR" };
  return BUILDER_CAT.sl_tp_modes.map((m) =>
    `<option value="${m}" ${m === selected ? "selected" : ""}>${labels[m]}</option>`).join("");
}

function condRowHtml(preset = {}) {
  const ind = preset.ind || "RSI", op = preset.op || "<", period = preset.period || 14;
  const val = preset.value !== undefined ? preset.value : 30;
  return `<div class="bld-cond">
    <select class="in join" title="join with previous">
      <option value="AND">AND</option><option value="OR">OR</option></select>
    <select class="in c-ind" onchange="syncPeriod(this)">${_indOptions(ind)}</select>
    <input class="in c-period" type="number" min="2" max="200" value="${period}" size="3" title="period">
    <select class="in c-op">${_opOptions(op)}</select>
    <select class="in c-rhs" onchange="toggleRhs(this)">
      <option value="value">vs value</option><option value="ind">vs indicator</option></select>
    <input class="in c-val" type="number" value="${val}" size="6" step="any">
    <select class="in c-ind2" style="display:none">${_indOptions("EMA")}</select>
    <input class="in c-period2" type="number" min="2" max="200" value="50" size="3" style="display:none">
    <button class="btn c-del" title="remove" onclick="this.closest('.bld-cond').remove();renumberJoins()">✕</button>
  </div>`;
}

function syncPeriod(sel) {  // grey out the period box for indicators that ignore it
  const opt = sel.options[sel.selectedIndex];
  const box = $(".c-period", sel.closest(".bld-cond"));
  box.disabled = opt.dataset.period === "0";
  box.style.opacity = box.disabled ? "0.3" : "1";
}

function toggleRhs(sel) {
  const row = sel.closest(".bld-cond");
  const isInd = sel.value === "ind";
  $(".c-val", row).style.display = isInd ? "none" : "";
  $(".c-ind2", row).style.display = isInd ? "" : "none";
  $(".c-period2", row).style.display = isInd ? "" : "none";
}

function renumberJoins() {  // first condition in each block hides its AND/OR
  ["#bld-entry", "#bld-exit"].forEach((sel) => {
    [...document.querySelectorAll(`${sel} .bld-cond`)].forEach((row, i) => {
      $(".join", row).style.visibility = i === 0 ? "hidden" : "";
    });
  });
}

function addCond(containerSel, preset) {
  $(containerSel).insertAdjacentHTML("beforeend", condRowHtml(preset));
  const row = $(containerSel).lastElementChild;
  syncPeriod($(".c-ind", row));
  renumberJoins();
}

async function showBuilderTab() {
  const host = $("#bt-content");
  if (!BUILDER_CAT) {
    host.innerHTML = loading("loading indicator catalog");
    try { BUILDER_CAT = await getJSON("/api/builder/indicators"); }
    catch (e) { host.innerHTML = `<div class="empty">${e.message}</div>`; return; }
  }
  const sym = state.symbol;
  const intervals = BUILDER_CAT.intervals.filter((i) => !i.endsWith("wk") && i !== "1mo");
  host.innerHTML = `
    <div class="bld">
      <div class="controls bld-top">
        <input class="in" id="bld-sym" value="${sym}" size="8" title="symbol">
        <select class="in" id="bld-int" title="timeframe">${intervals.map((i) =>
          `<option ${i === "1d" ? "selected" : ""}>${i}</option>`).join("")}</select>
        <select class="in" id="bld-period" title="history window">${
          ["3mo","6mo","1y","2y","5y","10y"].map((p) =>
            `<option ${p === "2y" ? "selected" : ""}>${p}</option>`).join("")}</select>
        <select class="in" id="bld-dir" title="direction">
          <option value="long">LONG</option><option value="short">SHORT</option></select>
        <button class="btn" id="bld-go">RUN BACKTEST</button>
      </div>

      <div class="bld-sec"><div class="bld-h">ENTRY WHEN</div><div id="bld-entry"></div>
        <button class="btn ghost" onclick="addCond('#bld-entry')">+ condition</button></div>

      <div class="bld-sec"><div class="bld-h">EXIT WHEN <span class="faint">(optional — SL/TP can exit instead)</span></div>
        <div id="bld-exit"></div>
        <button class="btn ghost" onclick="addCond('#bld-exit')">+ condition</button></div>

      <div class="bld-sec bld-risk">
        <div class="bld-h">RISK</div>
        <div class="controls">
          <label class="bld-lbl">STOP</label>
          <select class="in" id="bld-sl-mode">${_modeOptions("atr")}</select>
          <input class="in" id="bld-sl-val" type="number" value="2" size="5" step="any">
          <label class="bld-lbl">TARGET</label>
          <select class="in" id="bld-tp-mode">${_modeOptions("atr")}</select>
          <input class="in" id="bld-tp-val" type="number" value="3" size="5" step="any">
          <label class="bld-lbl"><input type="checkbox" id="bld-trail"> trail stop</label>
        </div>
      </div>

      <div class="bld-sec">
        <div class="bld-h">FILTERS <span class="faint">(blank = off)</span></div>
        <div class="controls">
          <label class="bld-lbl">SESSION</label>
          <input class="in" id="bld-sess-start" placeholder="09:15" size="5">
          <span class="faint">→</span>
          <input class="in" id="bld-sess-end" placeholder="15:30" size="5">
          <label class="bld-lbl">COOLDOWN</label>
          <input class="in" id="bld-cooldown" type="number" value="0" size="4" title="bars between trades">
          <label class="bld-lbl">ATR</label>
          <input class="in" id="bld-atr-min" placeholder="min" size="5">
          <input class="in" id="bld-atr-max" placeholder="max" size="5">
        </div>
      </div>

      <div id="bld-result"><div class="empty">Define entry/exit rules and risk, then RUN. Fills are next-bar-open; stops &amp; targets are checked intrabar against each bar's high/low.</div></div>
    </div>`;
  addCond("#bld-entry", { ind: "RSI", op: "<", period: 14, value: 30 });
  addCond("#bld-exit", { ind: "RSI", op: ">", period: 14, value: 70 });
  $("#bld-go").onclick = runBuilder;
}

function readConditions(containerSel) {
  return [...document.querySelectorAll(`${containerSel} .bld-cond`)].map((row, i) => {
    const cond = {
      left: { indicator: $(".c-ind", row).value, period: +$(".c-period", row).value },
      op: $(".c-op", row).value,
    };
    if ($(".c-rhs", row).value === "ind") {
      cond.right = { indicator: $(".c-ind2", row).value, period: +$(".c-period2", row).value };
    } else {
      cond.value = parseFloat($(".c-val", row).value);
    }
    if (i > 0) cond.join = $(".join", row).value;
    return cond;
  });
}

async function runBuilder() {
  const res = $("#bld-result");
  res.innerHTML = loading("simulating bar-by-bar with stops & targets");
  const dir = $("#bld-dir").value;
  const spec = { direction: dir };
  spec[`${dir}_entry`] = readConditions("#bld-entry");
  spec[`${dir}_exit`] = readConditions("#bld-exit");
  const num = (sel) => { const v = $(sel).value.trim(); return v === "" ? null : +v; };
  const body = {
    symbol: $("#bld-sym").value, interval: $("#bld-int").value, period: $("#bld-period").value,
    spec,
    sl_mode: $("#bld-sl-mode").value, sl_value: +$("#bld-sl-val").value,
    tp_mode: $("#bld-tp-mode").value, tp_value: +$("#bld-tp-val").value,
    trailing: $("#bld-trail").checked,
    session_start: $("#bld-sess-start").value.trim() || null,
    session_end: $("#bld-sess-end").value.trim() || null,
    cooldown_bars: num("#bld-cooldown") || 0,
    atr_min: num("#bld-atr-min"), atr_max: num("#bld-atr-max"),
    allow_short: true,
  };
  try {
    const r = await postJSON("/api/backtest/build", body);
    state.symbol = body.symbol.toUpperCase();
    drawBuilderResult(res, r);
  } catch (e) { res.innerHTML = `<div class="empty">${e.message}</div>`; }
}

function drawBuilderResult(body, r) {
  const m = r.metrics, alpha = m.total_return - r.bench_return;
  const ex = r.exit_breakdown || {};
  const exChips = Object.entries(ex).map(([k, v]) =>
    `<span class="chip">${k} ${v}</span>`).join("") || "<span class='faint'>no trades</span>";
  body.innerHTML = `
    ${metricsStrip([
      ["TOTAL RET", fmt.pct(m.total_return), cls(m.total_return)],
      ["VS B&H", fmt.pct(alpha), cls(alpha)],
      ["SHARPE", m.sharpe.toFixed(2)],
      ["MAX DD", fmt.pct(m.max_drawdown), "down"],
      ["WIN RATE", (m.win_rate * 100).toFixed(0) + "%"],
      ["PROFIT FACTOR", m.profit_factor === null ? "∞" : m.profit_factor.toFixed(2)],
      ["TRADES", m.num_trades],
      ["EXPOSURE", (m.exposure * 100).toFixed(0) + "%"],
    ])}
    <div class="chart-host short" id="bld-chart" style="margin-top:2px"></div>
    <div class="bld-meta">${r.bars} ${r.interval} bars · exits → ${exChips}
      ${iM(r.prov.execution, "execution model")}${iM(r.prov.metrics, "metrics")}
      <div class="faint" style="margin-top:3px">${r.data_note}</div></div>
    ${tradesTable(r.trades)}
    ${insightBlock([
      { what: alpha >= 0 ? `Beat buy & hold by ${fmt.pct(alpha)}` : `Lost to buy & hold by ${fmt.pct(-alpha)}`,
        why: `strategy ${fmt.pct(m.total_return)} vs benchmark ${fmt.pct(r.bench_return)} — validate any edge with PRESET tab's walk-forward + Monte Carlo` },
      { what: `${m.num_trades} trades, ${Object.keys(ex).includes("stop") ? (ex.stop || 0) : 0} stopped out`,
        why: m.num_trades < 20 ? "few trades — results are sample-thin, treat the metrics as indicative not significant"
          : "stops/targets are filled intrabar from bar high/low; same-bar SL+TP assumes the stop hit first" },
    ])}`;
  const chart = mkChart($("#bld-chart"));
  chart.addSeries(LWC.LineSeries, { color: "#f0a826", lineWidth: 2 }).setData(r.equity);
  chart.addSeries(LWC.LineSeries, { color: "rgba(88,166,255,0.65)", lineWidth: 1 }).setData(r.bench_equity);
  chart.timeScale().fitContent();
}

function tradesTable(trades) {
  if (!trades || !trades.length) return "";
  const recent = trades.slice(-40).reverse();
  const reasonCls = { stop: "down", target: "up", signal: "", "end-of-data": "faint" };
  return `<div class="bld-trades"><table class="tbl">
    <thead><tr><th>ENTRY</th><th>SIDE</th><th>ENTRY ₹</th><th>EXIT ₹</th>
      <th>RET</th><th>BARS</th><th>EXIT</th></tr></thead>
    <tbody>${recent.map((t) => `<tr>
      <td class="txt">${t.entry_time.replace("T", " ").slice(0, 16)}</td>
      <td class="${t.direction > 0 ? "up" : "down"}">${t.direction > 0 ? "LONG" : "SHORT"}</td>
      <td>${fmt.n(t.entry_price)}</td><td>${fmt.n(t.exit_price)}</td>
      <td class="${cls(t.return_pct)}">${fmt.pct(t.return_pct)}</td>
      <td>${t.bars_held}</td>
      <td class="${reasonCls[t.exit_reason] || ""}">${t.exit_reason}</td></tr>`).join("")}
    </tbody></table>${trades.length > 40 ? `<div class="faint" style="padding:4px 12px">showing last 40 of ${trades.length}</div>` : ""}</div>`;
}

/* ---------- TAPE ---------- */

function renderTape(view) {
  // Two halves. The running print log is what a desk means by "the tape": a
  // snapshot table cannot show you that a level was hit six times in two
  // seconds, which is the thing you are watching the tape for.
  view.innerHTML = `<div class="row cols-main-side">
    ${panel({ title: "TAPE", id: "tape-panel", flush: true, meta: "—",
      body: `<div class="tbl-scroll" style="max-height:calc(100vh - 120px)">
        <table class="tbl"><thead><tr>
          <th class="txt">TIME</th><th class="txt">SYMBOL</th><th>LTP</th>
          <th>CHG%</th><th>VOLUME</th></tr></thead>
        <tbody id="tape-log"></tbody></table></div>` })}
    ${panel({ title: "LAST PRINT", id: "tape-snap", flush: true, meta: "—",
      body: `<table class="tbl"><thead><tr>
        <th class="txt">SYMBOL</th><th>LTP</th><th>CHG%</th><th>DAY RANGE</th>
        <th>VOLUME</th><th>OI</th><th>LAST TICK</th>
        </tr></thead><tbody id="tape-body"></tbody></table>` })}
  </div>`;
  drawTape();
  addTimer("tape:draw", drawTape, 300);
}

/* ---------- live book exposure ------------------------------------------- */
/* PRT marks the whole book off the chain every 30s, which is the honest number
   but a slow one: between marks a desk is blind to a move it can see happening
   on the tape.
   Delta is the first-order answer. Net delta times the spot move since the
   last mark is what the book has made or lost from direction, and it is exact
   to first order. It is NOT the P&L: gamma, theta and vega are all moving too.
   So it is labelled as what it is, and it resets to zero on every full mark
   rather than accumulating drift. */

function paintLiveDelta() {
  const cell = $("#pf-livedelta");
  if (!cell || !state.lastRisk) return;
  const v = cell.querySelector(".v");
  const live = liveDeltaPnl(state.lastRisk, Object.fromEntries(state.markSpot));
  if (!live || feedState() !== "LIVE") {
    // No tick, or a feed that has gone quiet. Either way there is no move to
    // measure, and a stale spot would report a move that already happened as
    // if it were happening now.
    v.textContent = "—";
    v.className = "v faint";
    cell.title = feedState() === "LIVE"
      ? "no ticking underlying in this book"
      : `feed is ${feedState()}; no live spot to measure against`;
    return;
  }
  v.textContent = (live.pnl >= 0 ? "+" : "") + fmt.n(live.pnl, 0);
  v.className = `v ${cls(live.pnl)}`;
  cell.title = `net delta x spot move since the last full mark `
    + `(${live.legs} underlying${live.legs > 1 ? "s" : ""}). `
    + `First-order only: gamma, theta and vega are moving too.`;
}

function liveDeltaPnl(risk, marks) {
  if (!risk || !risk.by_underlying) return null;
  let pnl = 0, seen = 0;
  for (const [sym, g] of Object.entries(risk.by_underlying)) {
    const mark = marks[sym];
    const now = state.tickStore.get(sym);
    if (!mark || !now || !g.delta) continue;
    pnl += g.delta * (now.ltp - mark);
    seen++;
  }
  return seen ? { pnl, legs: seen } : null;
}

function drawTapeLog() {
  const tb = $("#tape-log");
  if (!tb) return;
  if (!state.tape.length) {
    tb.innerHTML = `<tr><td colspan="5" class="empty">No prints yet on this
      connection. The tape fills from the tick feed as it arrives; nothing is
      replayed or backfilled into it.</td></tr>`;
    return;
  }
  // Direction is against this symbol's own previous print, not against the
  // previous close. An uptick into a down day is still an uptick.
  tb.innerHTML = state.tape.map((t) => `<tr>
    <td class="txt faint">${fmt.ist(new Date(t.at))}</td>
    <td class="txt sym">${esc(t.symbol)}</td>
    <td class="${t.dir > 0 ? "up" : t.dir < 0 ? "down" : ""}">${fmt.n(t.ltp)}${
      t.dir > 0 ? " ▲" : t.dir < 0 ? " ▼" : ""}</td>
    <td class="${cls(t.change_pct)}">${fmt.pct(t.change_pct)}</td>
    <td class="faint">${t.volume ? fmt.compact(t.volume) : "—"}</td></tr>`).join("");
}

function drawTape() {
  const tb = $("#tape-body");
  if (!tb) return;
  const meta = $("#tape-panel .panel-meta");
  if (meta) meta.innerHTML =
    (() => { const f = state.wsDown ? "DOWN" : feedState(); const u = FEED_UI[f];
             return u.cls ? `<span class="${u.cls}">${u.label}</span>` : u.label; })()
    + ` · ${state.tickCount.toLocaleString()} TICKS`
    + (state.lastTickAt ? ` · LAST ${fmt.ist(state.lastTickAt)}` : "");
  drawTapeLog();
  const rows = [...state.tickStore.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  tb.innerHTML = rows.map(([sym, t]) => `
    <tr><td class="txt sym" onclick="show('chart',{symbol:'${sym}'})">${sym}</td>
      <td>${fmt.n(t.ltp)}</td>
      <td class="${cls(t.change_pct)}">${fmt.pct(t.change_pct)}</td>
      <td class="faint">${t.high ? `${fmt.n(t.low, 1)}–${fmt.n(t.high, 1)}` : "—"}</td>
      <td>${t.volume ? fmt.compact(t.volume) : "—"}</td>
      <td>${t.oi ? fmt.compact(t.oi) : "—"}</td>
      <td class="faint">${t._at ? fmt.ist(t._at) : "—"}</td></tr>`).join("") ||
    `<tr><td colspan="7" class="empty">waiting for ticks…</td></tr>`;
}

/* ---------- SCREENER ---------- */

async function renderScreener(view) {
  view.innerHTML = panel({
    title: "SCREENER", id: "scr-panel", flush: true,
    meta: `<span id="scr-upd">IDLE</span>
      <span class="controls" style="display:inline-flex;margin-left:8px">
      <select class="in" id="scr-uni">${["nifty50","banks","it","fno","mega","tech","semis","etf"]
        .map((u) => `<option>${u}</option>`).join("")}</select>
      <input class="in" id="scr-rules" placeholder="rsi<40, above_sma200" size="20">
      <button class="btn" id="scr-go">SCREEN</button></span>`,
    body: `<div class="empty">Rules AND together: rsi, ret_1w/1mo/3mo, vol_ann, from_high, vol_surge, above_sma50/200</div>`,
  });

  // The query the timer repeats — the last one that SUCCEEDED, not whatever is
  // currently typed: half-finished text in the box must never fire a sweep.
  // Null until the trader screens once, because a universe sweep is a history
  // fetch per symbol and nobody asked for one by opening the view.
  let query = null, inflight = false;

  const run = async (q, manual) => {
    if (inflight) {
      if (manual) toast("a screen is already running", "err");
      return;
    }
    inflight = true;
    const body = $("#scr-panel .panel-body");
    if (manual) body.innerHTML = loading("scanning");
    try {
      const r = await getJSON(
        `/api/screen?universe=${q.universe}&rules=${encodeURIComponent(q.rules)}`);
      if (!document.body.contains(view)) return;  // view switched mid-flight
      query = q;
      $("#scr-upd").innerHTML = stamp(
        `${r.rows.length}/${r.universe_size} PASS${r.errors ? ` · ${r.errors} ERRORS` : ""} · AUTO 5m`);
      body.innerHTML = `
        <table class="tbl"><thead><tr><th>SYMBOL</th><th>PRICE</th><th>1W</th><th>1M</th><th>3M</th>
        <th>RSI</th><th>VOL ANN</th><th>OFF HIGH</th><th>SMA50</th><th>SMA200</th></tr></thead>
        <tbody>${r.rows.map((row) => `<tr>
          <td class="txt sym" onclick="show('chart',{symbol:'${row.symbol}'})">${row.symbol}</td>
          <td>${fmt.n(row.price)}</td>
          <td class="${cls(row.ret_1w)}">${fmt.pct(row.ret_1w)}</td>
          <td class="${cls(row.ret_1mo)}">${fmt.pct(row.ret_1mo)}</td>
          <td class="${cls(row.ret_3mo)}">${fmt.pct(row.ret_3mo)}</td>
          <td>${row.rsi ? row.rsi.toFixed(1) : "—"}</td>
          <td>${row.vol_ann ? (row.vol_ann * 100).toFixed(0) + "%" : "—"}</td>
          <td class="${cls(row.from_high)}">${fmt.pct(row.from_high)}</td>
          <td class="${row.above_sma50 ? "up" : "faint"}">${row.above_sma50 ? "ABOVE" : "below"}</td>
          <td class="${row.above_sma200 ? "up" : "faint"}">${row.above_sma200 ? "ABOVE" : "below"}</td>
        </tr>`).join("")}</tbody></table>`;
    } catch (e) {
      if (!document.body.contains(view)) return;  // view switched mid-flight
      // A failed poll must never wipe the last good table; a failed manual
      // screen must always say why.
      $("#scr-upd").innerHTML = `<span class="down">SCREEN FAILED ${fmt.ist()}</span>`;
      if (manual || !query) body.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    } finally {
      inflight = false;
    }
  };

  $("#scr-go").onclick = () => run({
    universe: $("#scr-uni").value,
    rules: $("#scr-rules").value.split(",").map((s) => s.trim()).filter(Boolean).join(","),
  }, true);
  // Every metric here is derived from daily candles behind a 15-minute history
  // cache (provider.py:169-170), so 5 minutes picks up a cache turnover well
  // inside its life without re-deriving identical rows every minute.
  addTimer("screener:refresh", () => { if (query) run(query, false); }, 300000);
}

/* ---------- PORTFOLIO ---------- */

/* Margin is the exchange's number or nothing. Priced, it shows the SPAN split
   and what the netting saved — the hedge benefit is the whole reason a condor
   is affordable and one naked short is not. Unpriced, it names the missing
   round trip instead of filling the gap with arithmetic of our own. */
function marginLine(p) {
  if (!p.positions.length) return "";
  const st = p.margin_status || {}, m = p.margin || {};
  const btn = `<button class="tbtn" id="pf-reprice" style="padding:2px 9px;font-size:9px">${
    st.state === "priced" ? "REPRICE" : "PRICE NOW"}</button>`;
  // A stale `margin` still holds the PREVIOUS basket's SPAN numbers, so the
  // breakdown is shown only when the status says it is the current book's.
  if (st.state !== "priced" || !m.final) {
    return `<div class="margin-line"><span>MARGIN —</span>
      <span class="why">${esc(st.reason || "not priced")}</span>${btn}</div>`;
  }
  return `<div class="margin-line">
    <span>SPAN <b>${fmt.i(m.final.span)}</b></span>
    <span>EXPOSURE <b>${fmt.i(m.final.exposure)}</b></span>
    <span>PREMIUM <b>${fmt.i(m.final.option_premium)}</b></span>
    <span>HEDGE BENEFIT <b class="up">${fmt.i(m.hedge_benefit)}</b></span>
    <span>${esc(m.source || "")}</span>${btn}</div>`;
}

async function renderPortfolio(view) {
  view.innerHTML = panel({ title: "BOOK", id: "pf-panel", flush: true, meta: "—",
    body: loading("valuing") });

  const gk = (v, d = 0) => (v === null || v === undefined ? "—" : fmt.n(v, d));
  // The same identity margin is priced against server-side — position key AND
  // net size — so this changes exactly when the exchange's number stops
  // applying, and never more often.
  const bookKey = (p) => p.positions.map((x) => `${x.symbol}@${x.quantity}`).join("|");
  let asked = null;    // book state the exchange has already been asked about
  let ticket = null;   // open settlement ticket, if any

  const priceMargin = async (force) => {
    try { await postJSON(`/api/portfolio/margin${force ? "?force=true" : ""}`); }
    catch (e) { toast(e.message, "err"); }
    if (document.body.contains(view)) draw();
  };

  // Settlement ticket. Assignment is a real cash event, so the price is the
  // trader's to state: nothing here derives one from a spot we happen to hold,
  // and nothing pre-fills it. Lives inside the view, so switching views takes
  // it along — no document-level listener to leak, no ticket left floating.
  const closeTicket = () => { ticket?.remove(); ticket = null; };
  const openSettle = (tr) => {
    closeTicket();
    const key = tr.dataset.key, label = tr.dataset.label;
    const qty = Number(tr.dataset.qty), avg = Number(tr.dataset.avg);
    const el = elv("div", "ticket");
    el.innerHTML = `
      <div class="ticket-head">SETTLE ${esc(label)}</div>
      <div class="ticket-row">
        <span class="tk-lots" style="margin-left:0">${fmt.n(qty, 0)} @ AVG ${fmt.n(avg)}</span>
        <input class="in st-px" placeholder="settle px" size="8" style="margin-left:auto">
      </div>
      <div class="ticket-foot"><span class="st-note">price per unit — 0 is a number you type</span>
        <span class="tk-keys">↵ record · esc</span></div>
      <div class="tk-note">your number, not the exchange's — journalled as a settlement,
        not as a fill. Cash only: a physically settled stock option's delivery leg is a
        separate trade.</div>`;
    const pxIn = el.querySelector(".st-px"), note = el.querySelector(".st-note");
    // Preview only — both figures are arithmetic on numbers already on screen,
    // and the position is one-sided, so realized is exact rather than an
    // approximation of what the book will record.
    const preview = () => {
      const px = Number(pxIn.value);
      note.innerHTML = pxIn.value.trim() === "" || !(px >= 0)
        ? "price per unit — 0 is a number you type"
        : `${qty < 0 ? "pay" : "receive"} <b>${fmt.n(Math.abs(qty) * px, 0)}</b>
           · realized <span class="${cls((px - avg) * qty)}">${fmt.n((px - avg) * qty, 0)}</span>`;
    };
    const record = async () => {
      const px = Number(pxIn.value);
      if (pxIn.value.trim() === "" || !(px >= 0)) {
        return toast("settlement price required — type it, including 0", "err");
      }
      try {
        const r = await postJSON("/api/portfolio/settle", { symbol: key, price: px });
        toast(`SETTLED ${r.label} ${fmt.n(r.quantity, 0)} @ ${fmt.n(r.price)}`
          + ` · realized ${fmt.n(r.realized, 0)}`, "ok");
        closeTicket();
        draw();
      } catch (e) { toast(e.message, "err"); }
    };
    pxIn.oninput = preview;
    el.onkeydown = (ev) => {
      if (ev.key === "Escape") return closeTicket();
      if (ev.key === "Enter") return record();
    };
    const box = tr.getBoundingClientRect();
    el.style.top = `${box.bottom + 4}px`;
    el.style.left = `${Math.min(box.left, innerWidth - 260)}px`;
    view.appendChild(el);
    pxIn.focus();
    ticket = el;
  };

  const draw = async () => {
    try {
      const p = await getJSON("/api/portfolio");
      const host = $("#pf-panel .panel-body");
      if (!host) return;
      const r = p.risk || { net: {}, by_underlying: {}, unmarked: [], complete: true };
      const net = r.net || {};
      state.lastRisk = r;

      // Snapshot the spot each underlying was marked at, so the live delta
      // number below measures from a known point and resets on every mark.
      state.markSpot = new Map();
      for (const sym of Object.keys(r.by_underlying || {})) {
        const t = state.tickStore.get(sym);
        if (t) state.markSpot.set(sym, t.ltp);
      }
      state.lastMarkAt = Date.now();

      $("#pf-panel .panel-meta").innerHTML = ageStamp(p.as_of)
        + ' <span class="faint">· AUTO 30s</span>'
        + (r.summary ? ` · <span class="badge">${esc(r.summary)}</span>` : "");

      host.innerHTML = `
        ${metricsStrip([
          ["EQUITY", "\u20b9" + fmt.n(p.equity, 0)],
          ["CASH", "\u20b9" + fmt.n(p.cash, 0)],
          ["UNREALIZED", "\u20b9" + fmt.n(p.unrealized_pnl, 0), cls(p.unrealized_pnl)],
          ["REALIZED", "\u20b9" + fmt.n(p.realized_pnl, 0), cls(p.realized_pnl)],
          // margin is the exchange's number or nothing — never an estimate
          ["MARGIN", p.margin_used === null || p.margin_used === undefined
            ? "—" : "\u20b9" + fmt.n(p.margin_used, 0)],
        ])}
        ${marginLine(p)}

        <div class="risk-strip">
          <div class="risk-cell"><span class="k">NET DELTA</span><span class="v ${cls(net.delta)}">${gk(net.delta, 1)}</span></div>
          <div class="risk-cell"><span class="k">GAMMA</span><span class="v ${cls(net.gamma)}">${gk(net.gamma, 4)}</span></div>
          <div class="risk-cell"><span class="k">THETA / DAY</span><span class="v ${cls(net.theta)}">${gk(net.theta, 0)}</span></div>
          <div class="risk-cell"><span class="k">VEGA / PT</span><span class="v ${cls(net.vega)}">${gk(net.vega, 0)}</span></div>
          <div class="risk-cell"><span class="k">RHO</span><span class="v">${gk(net.rho, 0)}</span></div>
          <div class="risk-cell" id="pf-livedelta"><span class="k">DELTA P&amp;L SINCE MARK</span>
            <span class="v faint">—</span></div>
        </div>
        ${r.complete ? "" : `<div class="risk-warn">NET EXCLUDES ${r.unmarked.length}
          UNMARKED LEG${r.unmarked.length > 1 ? "S" : ""}: ${esc(r.unmarked.join(" · "))}
          — no chain to mark against, so they are left out rather than counted as zero.</div>`}

        ${p.positions.length ? `<div class="tbl-scroll"><table class="tbl"><thead><tr>
          <th>CONTRACT</th><th>QTY</th><th>AVG</th><th>LAST</th><th>VALUE</th><th>P&amp;L</th></tr></thead>
          <tbody>${p.positions.map((pos) => `<tr class="${pos.expired ? "row-dead" : ""}"
            data-key="${esc(pos.symbol)}" data-label="${esc(pos.label || pos.symbol)}"
            data-qty="${pos.quantity}" data-avg="${pos.avg_cost}">
            <td class="txt sym">${esc(pos.label || pos.symbol)}
              ${pos.is_short ? '<span class="tag-short">SHORT</span>' : ""}
              ${pos.expired ? '<span class="tag-dead">EXPIRED</span>' : ""}
              ${pos.settleable ? '<button class="tbtn pf-settle" style="padding:1px 7px;font-size:9px">SETTLE</button>' : ""}</td>
            <td class="${pos.is_short ? "down" : "up"}">${fmt.n(pos.quantity, 0)}${
              pos.lot_size ? `<span class="faint"> (${fmt.n(pos.quantity / pos.lot_size, 0)}L)</span>` : ""}</td>
            <td>${fmt.n(pos.avg_cost)}</td><td>${fmt.n(pos.last)}</td>
            <td>\u20b9${fmt.n(pos.market_value, 0)}</td>
            <td class="${cls(pos.unrealized)}">\u20b9${fmt.n(pos.unrealized, 0)}</td></tr>`).join("")}
          </tbody></table></div>`
          : `<div class="empty">Flat. Paper-only — orders never reach a broker.
             Book a leg from the OPT chain by clicking a premium.</div>`}

        ${(p.history || []).length ? `
          <div class="panel-head" style="border-top:1px solid var(--stroke)">
            <span class="panel-title">JOURNAL</span>
            <span class="panel-meta">LAST ${Math.min((p.history || []).length, 30)} ·
              SETTLEMENTS ARE ASSERTED, NOT EXECUTED</span></div>
          <div class="tbl-scroll" style="max-height:26vh"><table class="tbl"><thead><tr>
            <th class="txt">TIME</th><th class="txt">SIDE</th><th class="txt">CONTRACT</th>
            <th>QTY</th><th>PRICE</th><th>REALIZED</th></tr></thead><tbody>
            ${p.history.slice(-30).reverse().map((h) => `<tr>
              <td class="txt faint">${esc((h.time || "").replace("T", " ").slice(5, 16))}</td>
              <td class="txt ${h.side === "BUY" ? "up" : "down"}">${esc(h.side || "")}${
                h.settlement ? ' <span class="tag-dead" title="closed at a price the trader supplied, not an executed fill">SETTLED</span>' : ""}</td>
              <td class="txt sym">${esc(h.label || h.symbol || "")}</td>
              <td>${fmt.n(h.qty, 0)}</td>
              <td>${fmt.n(h.price)}</td>
              <td class="${h.realized != null ? cls(h.realized) : "faint"}">${
                h.realized != null ? "\u20b9" + fmt.n(h.realized, 0) : "\u2014"}</td></tr>`).join("")}
          </tbody></table></div>` : ""}`;

      const rep = $("#pf-reprice");
      // Re-asking is the trader's call, and force is the only way past the
      // server's one-answer-per-book memo.
      if (rep) rep.onclick = () => { asked = bookKey(p); priceMargin(true); };
      // Fill the tile unprompted, at most one exchange round trip per book
      // state: SPAN nets across the whole basket, so margin cannot be kept
      // fresh leg by leg, and a desk that adjusts all day should not have to
      // click for the number after every fill. `asked` is the client-side half
      // of the guard — price_margin() is idempotent on the same fingerprint,
      // but a refusal that never reaches it (no broker) must not loop here.
      const key = bookKey(p);
      if ((p.margin_used === null || p.margin_used === undefined)
          && p.positions.length && asked !== key) {
        asked = key;
        priceMargin(false);
      }
    } catch (e) {
      const host = $("#pf-panel .panel-body");
      if (host) host.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    }
  };

  // The panel shell outlives every redraw, so one delegated listener covers
  // rows that do not exist yet and cannot stack up per draw.
  $("#pf-panel").addEventListener("click", (ev) => {
    const b = ev.target.closest(".pf-settle");
    if (b) openSettle(b.closest("tr"));
  });
  draw();
  addTimer("portfolio:draw", draw, 30000);
}

/* ---------- ALERTS ---------- */

async function renderAlerts(view) {
  view.innerHTML = panel({ title: "ALERTS", id: "al-panel", flush: true, meta: "CHECKED EVERY 60s SERVER-SIDE",
    body: loading() });
  const draw = async () => {
    try {
      const a = await getJSON("/api/alerts");
      const host = $("#al-panel .panel-body");
      if (!host) return;
      $("#al-panel .panel-meta").innerHTML = stamp("CHECK INTERVAL 60s") +
        ` <span class="controls" style="display:inline-flex;margin-left:8px">
          <input class="in" id="al-rule" placeholder="NIFTY > 23500 · RELIANCE rsi < 30" size="26">
          <button class="btn" id="al-go">ARM</button></span>`;
      host.innerHTML = a.alerts.length
        ? `<table class="tbl"><thead><tr><th>STATE</th><th>RULE</th><th>FIRED</th><th></th></tr></thead><tbody>
           ${a.alerts.map((al) => `<tr>
             <td class="txt ${al.armed ? "up" : "faint"}">${al.armed ? "ARMED" : "FIRED"}</td>
             <td class="txt">${al.symbol}${al.metric === "price" ? "" : " " + al.metric} ${al.op} ${al.value}</td>
             <td class="faint">${al.fired_at ? `${al.fired_value.toFixed(2)} @ ${al.fired_at.slice(11, 16)}Z` : "—"}</td>
             <td><button class="btn danger" style="padding:2px 9px;font-size:9px" onclick="deleteAlert(${al.index})">REMOVE</button></td>
           </tr>`).join("")}</tbody></table>`
        : `<div class="empty">No alerts. Syntax: SYM &gt; price · SYM rsi &lt; 30 · SYM vol_surge &gt; 2</div>`;
      $("#al-go").onclick = async () => {
        try {
          const res = await postJSON("/api/alerts", { rule: $("#al-rule").value });
          toast(`ARMED: ${res.text}`, "ok");
          draw();
        } catch (e) { toast(e.message, "err"); }
      };
    } catch (e) { $("#al-panel .panel-body").innerHTML = `<div class="empty">${e.message}</div>`; }
  };
  window.deleteAlert = async (i) => {
    try { await postJSON(`/api/alerts/${i}`, undefined, "DELETE"); draw(); }
    catch (e) { toast(e.message, "err"); }
  };
  draw();
}

/* ---------- WORKSPACE (multi-widget canvas, 915-style linking) ----------
   Widgets live on a 12-col gridstack: drag by the header, resize from the
   corner. Each widget can join a link channel (A/B/C); selecting a symbol
   in any widget on that channel retunes every other widget on it. Layout
   persists server-side (~/.shunkan/layout.json). */

let gsGrid = null;
let wsSeq = 0;
const wsInstances = new Map(); // domId -> { type, config, el, onSymbol?, destroy? }

const CHANNELS = { A: "NIFTY", B: "BANKNIFTY", C: "RELIANCE" };

function chanBroadcast(ch, symbol) {
  if (!ch || !CHANNELS.hasOwnProperty(ch)) return;
  CHANNELS[ch] = symbol.toUpperCase();
  wsInstances.forEach((inst) => {
    if (inst.config.channel === ch && inst.onSymbol) inst.onSymbol(CHANNELS[ch]);
  });
  saveLayoutSoon();
}

const WS_WIDGETS = {
  watchlist: { title: "WATCHLIST", w: 3, h: 5 },
  chart:     { title: "CHART", w: 5, h: 5, hasSymbol: true },
  chain:     { title: "CHAIN ATM±6", w: 4, h: 5, hasSymbol: true },
  tape:      { title: "TAPE", w: 3, h: 4 },
  news:      { title: "NEWS", w: 5, h: 4 },
  straddle:  { title: "STRADDLE (CAPTURED)", w: 4, h: 4, hasSymbol: true },
};

const WS_DEFAULT = [
  { type: "watchlist", x: 0, y: 0, w: 3, h: 5, config: { channel: "A" } },
  { type: "chart",     x: 3, y: 0, w: 5, h: 5, config: { channel: "A", symbol: "NIFTY", period: "3mo" } },
  { type: "chain",     x: 8, y: 0, w: 4, h: 5, config: { channel: "A", symbol: "NIFTY" } },
  { type: "tape",      x: 0, y: 5, w: 3, h: 4, config: {} },
  { type: "news",      x: 3, y: 5, w: 5, h: 4, config: {} },
  { type: "straddle",  x: 8, y: 5, w: 4, h: 4, config: { channel: "A", symbol: "NIFTY" } },
];

let saveTimer = null;
function saveLayoutSoon() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveLayoutNow, 800);
}
function saveLayoutNow() {
  if (!gsGrid) return;
  const widgets = [];
  gsGrid.engine.nodes.forEach((n) => {
    const inst = wsInstances.get(n.el.id);
    if (inst) widgets.push({ type: inst.type, x: n.x, y: n.y, w: n.w, h: n.h, config: inst.config });
  });
  postJSON("/api/layout", { widgets, channels: CHANNELS }).catch(() => {});
}

function wsAddWidget(type, pos = {}, config = {}) {
  const spec = WS_WIDGETS[type];
  if (!spec) return;
  const id = `wsw-${++wsSeq}`;
  const item = elv("div", "grid-stack-item");
  item.id = id;
  const chanOpts = ["—", "A", "B", "C"].map((c) =>
    `<option value="${c === "—" ? "" : c}" ${config.channel === c ? "selected" : ""}>${c === "—" ? "·" : "CH " + c}</option>`).join("");
  item.innerHTML = `
    <div class="grid-stack-item-content"><div class="widget">
      <div class="w-head">
        <span class="w-title">${spec.title}</span>
        ${spec.hasSymbol ? `<input class="w-sym" value="${config.symbol || "NIFTY"}" spellcheck="false">` : ""}
        <select class="w-chan ${config.channel ? "ch-" + config.channel : ""}">${chanOpts}</select>
        <span class="w-close" title="Remove widget">×</span>
      </div>
      <div class="w-body"><div class="loading"><span class="spin"></span>loading</div></div>
    </div></div>`;
  $(".grid-stack").appendChild(item);
  gsGrid.makeWidget(item, { x: pos.x, y: pos.y, w: pos.w || spec.w, h: pos.h || spec.h, autoPosition: pos.x === undefined });

  const inst = { type, config: { ...config }, el: item };
  wsInstances.set(id, inst);

  const body = item.querySelector(".w-body");
  const symInput = item.querySelector(".w-sym");
  const chanSel = item.querySelector(".w-chan");

  chanSel.onchange = () => {
    inst.config.channel = chanSel.value || undefined;
    chanSel.className = `w-chan ${inst.config.channel ? "ch-" + inst.config.channel : ""}`;
    if (inst.config.channel && inst.onSymbol) inst.onSymbol(CHANNELS[inst.config.channel]);
    saveLayoutSoon();
  };
  // Stop the drag handle from hijacking input/select interaction.
  [symInput, chanSel].forEach((el) => el && el.addEventListener("pointerdown", (e) => e.stopPropagation()));
  item.querySelector(".w-close").onclick = () => {
    if (inst.destroy) inst.destroy();
    wsInstances.delete(id);
    gsGrid.removeWidget(item);
    saveLayoutSoon();
  };
  if (symInput) {
    symInput.onkeydown = (e) => {
      if (e.key === "Enter") {
        inst.config.symbol = symInput.value.toUpperCase();
        if (inst.config.channel) chanBroadcast(inst.config.channel, inst.config.symbol);
        else if (inst.onSymbol) inst.onSymbol(inst.config.symbol);
        saveLayoutSoon();
      }
    };
  }
  WS_RENDER[type](body, inst, symInput);
  return inst;
}

/* -- widget bodies -- */

function wWatchlist(body, inst) {
  const paint = async () => {
    try {
      const wl = (await getJSON("/api/watchlist")).symbols;
      const quotes = await getJSON(`/api/quotes?symbols=${encodeURIComponent(wl.join(","))}`);
      if (!document.body.contains(body)) return;
      body.innerHTML = `<table class="tbl"><tbody>
        ${wl.map((s) => {
          const q = quotes[s];
          const live = state.tickStore.get(s);
          const px = live ? live.ltp : q ? q.price : null;
          const chg = live ? live.change_pct : q ? q.change_pct : null;
          return `<tr data-ws-sym="${s}" style="cursor:pointer">
            <td class="txt sym">${s}</td>
            <td>${px !== null ? fmt.n(px) : "—"}</td>
            <td class="${chg !== null ? cls(chg) : "faint"}">${chg !== null ? fmt.pct(chg) : "—"}</td></tr>`;
        }).join("")}</tbody></table>`;
      body.querySelectorAll("[data-ws-sym]").forEach((row) => {
        row.onclick = () => {
          const ch = inst.config.channel;
          if (ch) chanBroadcast(ch, row.dataset.wsSym);
        };
      });
    } catch { /* next tick */ }
  };
  paint();
  addTimer("wq:paint", paint, 12000);
}

function wChart(body, inst, symInput) {
  let chart = null;
  const load = async (symbol) => {
    inst.config.symbol = symbol;
    if (symInput) symInput.value = symbol;
    body.innerHTML = `<div class="w-chart-host"></div>`;
    try {
      const d = await getJSON(`/api/history/${symbol}?period=${inst.config.period || "3mo"}`);
      if (!document.body.contains(body)) return;
      chart = mkChart(body.firstChild, { timeScale: { visible: false } });
      chart.addSeries(LWC.CandlestickSeries, {
        upColor: "#2ebd85", downColor: "#f1564b",
        wickUpColor: "#2ebd85", wickDownColor: "#f1564b", borderVisible: false,
      }).setData(d.candles);
      requestAnimationFrame(() => chart && chart.timeScale().fitContent());
    } catch (e) { body.innerHTML = `<div class="empty">${e.message}</div>`; }
  };
  inst.onSymbol = load;
  load(inst.config.symbol || "NIFTY");
}

function wChain(body, inst, symInput) {
  const load = async (symbol) => {
    inst.config.symbol = symbol;
    if (symInput) symInput.value = symbol;
    try {
      const c = await getJSON(`/api/chain/${symbol}`);
      if (!document.body.contains(body)) return;
      const atm = c.rows.findIndex((r) => r.atm);
      const lo = Math.max(atm - 6, 0), hi = Math.min(atm + 7, c.rows.length);
      const rows = c.rows.slice(lo, hi);
      const maxOI = Math.max(...rows.map((r) => Math.max(r.call.oi, r.put.oi)), 1);
      body.innerHTML = `
        <div style="display:flex;gap:10px;padding:5px 9px;border-bottom:1px solid var(--stroke-soft);font-size:10px" class="mono">
          <span class="dim">SPOT <b>${fmt.n(c.spot, 0)}</b></span>
          <span class="dim">PCR <b>${c.analytics.pcr_oi.toFixed(2)}</b></span>
          <span class="dim">MP <b class="amber">${fmt.i(c.analytics.max_pain)}</b></span>
          <span class="dim">±<b>${(c.analytics.expected_move_pct * 100).toFixed(1)}%</b></span>
        </div>
        <table class="tbl"><thead><tr><th>C·OI</th><th>C·LTP</th><th style="text-align:center">STRK</th><th>P·LTP</th><th>P·OI</th></tr></thead>
        <tbody>${rows.map((r) => `
          <tr class="${r.atm ? "atm-row" : ""}">
            <td class="oi-cell"><div class="oi-bar call" style="width:${(r.call.oi / maxOI) * 100}%"></div>
              <span class="oi-num">${fmt.compact(r.call.oi)}</span></td>
            <td>${fmt.n(r.call.ltp)}</td>
            <td style="text-align:center;font-weight:700" class="${r.atm ? "amber" : ""}">${fmt.i(r.strike)}</td>
            <td>${fmt.n(r.put.ltp)}</td>
            <td class="oi-cell"><div class="oi-bar put" style="width:${(r.put.oi / maxOI) * 100}%"></div>
              <span class="oi-num">${fmt.compact(r.put.oi)}</span></td>
          </tr>`).join("")}</tbody></table>`;
    } catch (e) { body.innerHTML = `<div class="empty">${e.message}</div>`; }
  };
  inst.onSymbol = load;
  load(inst.config.symbol || "NIFTY");
  addTimer("wchain:load", () => load(inst.config.symbol || "NIFTY"), 60000);
}

function wTape(body, inst) {
  const paint = () => {
    if (!document.body.contains(body)) return;
    const rows = [...state.tickStore.entries()].sort((a, b) => a[0].localeCompare(b[0]));
    body.innerHTML = rows.length ? `<table class="tbl"><tbody>
      ${rows.map(([sym, t]) => `<tr data-ws-sym="${sym}" style="cursor:pointer">
        <td class="txt sym">${sym}</td><td>${fmt.n(t.ltp)}</td>
        <td class="${cls(t.change_pct)}">${fmt.pct(t.change_pct)}</td></tr>`).join("")}
      </tbody></table>` : `<div class="empty">waiting for ticks…</div>`;
    body.querySelectorAll("[data-ws-sym]").forEach((row) => {
      row.onclick = () => inst.config.channel && chanBroadcast(inst.config.channel, row.dataset.wsSym);
    });
  };
  paint();
  addTimer("wtape:paint", paint, 500);
}

function wNews(body, inst) {
  const paint = async () => {
    try {
      const n = await getJSON("/api/news?limit=8");
      if (!document.body.contains(body)) return;
      body.innerHTML = n.items.map((it) => {
        const sb = it.sentiment_label.includes("bullish") ? "bull" :
                   it.sentiment_label.includes("bearish") ? "bear" : "";
        return `<div style="padding:6px 9px;border-bottom:1px solid var(--stroke-soft)">
          <div style="display:flex;gap:7px;align-items:baseline">
            <span class="n-age">${fmt.age(it.age_minutes)}</span>
            <span style="font-size:11px;font-weight:600;line-height:1.35">${it.title}</span>
          </div>
          <div style="padding-left:49px;margin-top:2px">
            <span class="badge ${sb}" style="font-size:8px">${it.sentiment_label}</span>
            <span class="faint" style="font-size:9px">${it.impact.direction} ${(it.impact.confidence * 100).toFixed(0)}%</span>
          </div></div>`;
      }).join("");
    } catch { /* retry on timer */ }
  };
  paint();
  addTimer("wbias:paint", paint, 60000);
}

function wStraddle(body, inst, symInput) {
  const load = async (symbol) => {
    inst.config.symbol = symbol;
    if (symInput) symInput.value = symbol;
    try {
      const c = await getJSON(`/api/chain/${symbol}`);
      if (!document.body.contains(body)) return;
      const s = c.straddle_today || [];
      if (s.length >= 2) {
        body.innerHTML = `<div class="w-chart-host"></div>
          <div class="faint" style="padding:3px 9px;font-size:9px">${s.length} captured snapshots · ATM ${fmt.i(s[s.length - 1].strike)} · last ₹${fmt.n(s[s.length - 1].value, 1)}</div>`;
        const chart = mkChart(body.firstChild);
        chart.addSeries(LWC.LineSeries, { color: "#f0a826", lineWidth: 2 })
          .setData(s.map((p) => ({ time: p.time, value: p.value })));
        requestAnimationFrame(() => chart.timeScale().fitContent());
      } else {
        body.innerHTML = `<div class="empty">straddle chart needs ≥2 captured snapshots today
          (${s.length} so far) — accumulates automatically during market hours</div>`;
      }
    } catch (e) { body.innerHTML = `<div class="empty">${e.message}</div>`; }
  };
  inst.onSymbol = load;
  load(inst.config.symbol || "NIFTY");
  addTimer("wnews:load", () => load(inst.config.symbol || "NIFTY"), 300000);
}

const WS_RENDER = {
  watchlist: wWatchlist, chart: wChart, chain: wChain,
  tape: wTape, news: wNews, straddle: wStraddle,
};

async function renderWorkspace(view) {
  wsInstances.clear();
  gsGrid = null;
  view.innerHTML = `
    <div class="ws-toolbar">
      <select class="in" id="ws-add-type">
        ${Object.entries(WS_WIDGETS).map(([k, v]) => `<option value="${k}">${v.title}</option>`).join("")}
      </select>
      <button class="btn" id="ws-add">ADD WIDGET</button>
      <button class="btn ghost" id="ws-reset">RESET LAYOUT</button>
      <span class="ws-hint">DRAG HEADER TO MOVE · CORNER TO RESIZE · CH A/B/C LINK WIDGETS: CLICK A SYMBOL, LINKED WIDGETS FOLLOW · LAYOUT AUTOSAVES</span>
    </div>
    <div class="grid-stack"></div>`;

  gsGrid = GridStack.init(
    { cellHeight: 86, margin: 0, float: false, handle: ".w-head", animate: true },
    view.querySelector(".grid-stack"),
  );
  gsGrid.on("change", saveLayoutSoon);

  let layout = null;
  try {
    const saved = await getJSON("/api/layout");
    if (saved.widgets && saved.widgets.length) {
      layout = saved.widgets;
      Object.assign(CHANNELS, saved.channels || {});
    }
  } catch {}
  (layout || WS_DEFAULT).forEach((wdg) =>
    wsAddWidget(wdg.type, { x: wdg.x, y: wdg.y, w: wdg.w, h: wdg.h }, wdg.config || {}));

  $("#ws-add").onclick = () => { wsAddWidget($("#ws-add-type").value); saveLayoutSoon(); };
  $("#ws-reset").onclick = async () => {
    await postJSON("/api/layout", { widgets: WS_DEFAULT.map((w) => ({ ...w })) });
    show("workspace");
  };
}

/* ---------- DATA STORE (capture coverage — what's real) ---------- */

async function renderDatastore(view) {
  view.innerHTML = `
    ${panel({ title: "BULK EXPORT — HISTORICAL CANDLES", id: "exp-panel", meta: "UP TO 20 SYMBOLS · LONG FORMAT", body: `
      <div class="exp-row">
        <input id="exp-syms" class="exp-input" spellcheck="false"
               placeholder="NIFTY, BANKNIFTY, RELIANCE, TCS…">
        <select id="exp-period" class="viz-select">
          ${["1mo", "6mo", "1y", "2y", "5y", "10y", "max"].map((p) => `<option${p === "5y" ? " selected" : ""}>${p}</option>`).join("")}
        </select>
        <select id="exp-interval" class="viz-select">
          ${["1d", "1h", "15m", "5m"].map((i) => `<option>${i}</option>`).join("")}
        </select>
        <button class="viz-mini run" id="exp-csv">↓ CSV</button>
        <button class="viz-mini run" id="exp-parquet">↓ PARQUET</button>
        <button class="viz-mini" id="exp-backfill" title="Pull period=max daily history for every symbol Shunkan knows (indexes, universes, watchlist, all F&O underlyings when a broker is connected) into the local archive">⛁ BACKFILL EVERYTHING · MAX HISTORY</button>
      </div>
      <div class="feed-note">Every row carries a <b>source</b> column naming the provider —
        offline demo data exports labeled <i>synthetic-demo</i>, never disguised as market data.
        Intraday intervals are limited by what the source keeps (Yahoo: ~2y of 1h, ~60d of 15m).</div>` })}
    ${panel({ title: "HISTORY ARCHIVE — GROWS WHILE THE TERMINAL RUNS", id: "arc-panel", flush: true, meta: "—",
              body: loading("reading archive") })}
    ${panel({ title: "LOCAL DATA STORE", id: "dta-panel", flush: true, meta: "—",
              body: loading("reading store") })}`;

  const dl = (fmt) => {
    const syms = $("#exp-syms").value.trim() || "NIFTY,BANKNIFTY";
    window.location.href = `/api/export/history?symbols=${encodeURIComponent(syms)}` +
      `&period=${$("#exp-period").value}&interval=${$("#exp-interval").value}&fmt=${fmt}`;
    toast(`Export started — check your downloads`, "ok");
  };
  $("#exp-csv").onclick = () => dl("csv");
  $("#exp-parquet").onclick = () => dl("parquet");

  const arcMeta = (html) => { const p = $("#arc-panel"); if (p) p.querySelector(".panel-meta").innerHTML = html; };
  const pollBackfill = async () => {
    try {
      const s = await getJSON("/api/archive/backfill");
      if (s.running) {
        arcMeta(`<b class="warn">BACKFILL ${s.done}/${s.total}</b> · ${s.current || "…"} · ${s.ok} OK` +
                (s.failed.length ? ` · ${s.failed.length} FAILED` : ""));
        setTimeout(pollBackfill, 2500);
      } else if (s.finished_at) {
        arcMeta(`BACKFILL DONE — ${s.ok}/${s.total} SYMBOLS` +
                (s.failed.length ? ` · FAILED: ${s.failed.slice(0, 8).join(", ")}${s.failed.length > 8 ? "…" : ""}` : ""));
        toast(`Backfill complete: ${s.ok}/${s.total} symbols archived`, "ok");
        if (state.view === "datastore") show("datastore");
      }
    } catch {}
  };
  $("#exp-backfill").onclick = async () => {
    try {
      const r = await postJSON("/api/archive/backfill");
      toast(`Backfill started — ${r.total} symbols, period=max`, "ok");
      pollBackfill();
    } catch (e) { toast(`Backfill: ${e.message}`, "err"); }
  };
  getJSON("/api/archive/backfill").then((s) => { if (s.running) pollBackfill(); }).catch(() => {});
  getJSON("/api/watchlist").then((w) => {
    const el = $("#exp-syms");
    if (el && !el.value && Array.isArray(w.symbols || w))
      el.value = (w.symbols || w).slice(0, 12).join(",");
  }).catch(() => {});

  try {
    const a = await getJSON("/api/store/archive");
    const entries = Object.entries(a.symbols);
    $("#arc-panel .panel-meta").innerHTML =
      stamp(`${entries.length} SYMBOLS · ${(a.size_bytes / 1048576).toFixed(1)} MB · SYNCS EVERY 6H WHILE LIVE`);
    $("#arc-panel .panel-body").innerHTML = entries.length ? `
      <table class="tbl"><thead><tr>
        <th>SYMBOL</th><th>ROWS</th><th>FIRST</th><th>LAST</th><th>SOURCE</th></tr></thead>
        <tbody>${entries.map(([s, e]) => `<tr>
          <td class="txt sym">${s}</td><td>${e.rows}</td>
          <td class="faint">${e.first}</td><td class="faint">${e.last}</td>
          <td class="faint">${e.source}</td></tr>`).join("")}</tbody></table>`
      : `<div class="empty">Archive is empty — it fills automatically ~25s after a live
         (non-offline) server start, then re-syncs every 6 hours. Synthetic data is never written.</div>`;
  } catch (e) {
    $("#arc-panel .panel-body").innerHTML = `<div class="empty">${e.message}</div>`;
  }

  try {
    const s = await getJSON("/api/store/stats");
    $("#dta-panel .panel-meta").innerHTML = stamp(`${(s.size_bytes / 1048576).toFixed(1)} MB ON DISK`);
    const chainRows = Object.entries(s.chains);
    const barRows = Object.entries(s.bars);
    $("#dta-panel .panel-body").innerHTML = `
      <div class="feed-note">Everything below is REAL captured market data — the basis for ΔOI,
        local IV rank and straddle charts. Numbers derived from it cite their snapshot timestamps (ⓘ).
        Derived metrics refuse to report until enough history accumulates; nothing is back-filled or simulated.</div>
      <div class="row cols-2" style="padding:10px 12px">
        <div>
          <div class="panel-title" style="margin-bottom:7px">OPTION-CHAIN SNAPSHOTS</div>
          ${chainRows.length ? `<table class="tbl"><thead><tr>
            <th>SYMBOL</th><th>DAYS</th><th>FIRST</th><th>LAST</th><th>SNAPS TODAY</th></tr></thead>
            <tbody>${chainRows.map(([sym, c]) => `<tr>
              <td class="txt sym">${sym}</td><td>${c.days}</td>
              <td class="faint">${c.first}</td><td class="faint">${c.last}</td>
              <td>${c.snapshots_today}</td></tr>`).join("")}</tbody></table>`
            : `<div class="empty">No chain snapshots yet — they capture automatically every ~10 min
               during market hours and whenever the OPT view loads a live chain.</div>`}
          <div class="faint" style="margin-top:8px;font-size:10.5px">
            Unlocks: ΔOI vs stored basis · ATM-IV history → true IV rank (needs 20d) · intraday straddle chart</div>
        </div>
        <div>
          <div class="panel-title" style="margin-bottom:7px">1-MINUTE BARS (FROM LIVE TICKS)</div>
          ${barRows.length ? `<table class="tbl"><thead><tr>
            <th>SYMBOL</th><th>DAYS</th><th>FIRST</th><th>LAST</th></tr></thead>
            <tbody>${barRows.map(([sym, b]) => `<tr>
              <td class="txt sym">${sym}</td><td>${b.days}</td>
              <td class="faint">${b.first}</td><td class="faint">${b.last}</td></tr>`).join("")}</tbody></table>`
            : `<div class="empty">No bars yet — they build from the Kite tick stream while the
               server runs during market hours (demo-feed ticks are never stored).</div>`}
          <div class="faint" style="margin-top:8px;font-size:10.5px">
            Unlocks: tick-level charts · session VWAP from our own tape · intraday backtests</div>
        </div>
      </div>
      <div class="faint" style="padding:0 12px 10px;font-size:10px;font-family:var(--mono)">${s.root}</div>`;
  } catch (e) {
    $("#dta-panel .panel-body").innerHTML = `<div class="empty">${e.message}</div>`;
  }
}

/* ---------- QUANT LAB 3D (QNT) ----------
   WebGL surfaces over the same engines as everything else. The IV surface
   marks which slice is market data; the swarm optimizer replays a real PSO
   run where every fitness evaluation was a full backtest. */

const VIZ_TABS = [
  { id: "surface",    label: "IV SURFACE" },
  { id: "greeks",     label: "GREEKS" },
  { id: "montecarlo", label: "MONTE CARLO" },
  { id: "heston",     label: "HESTON" },
  { id: "correlation", label: "CORRELATION" },
  { id: "var",        label: "VAR" },
  { id: "frontier",   label: "FRONTIER" },
  { id: "kalman",     label: "KALMAN" },
  { id: "attention",  label: "ATTENTION" },
  { id: "swarm",      label: "SWARM OPT" },
];
const vizState = { tab: "surface", greek: "gamma", side: "call", horizon: 60,
                   strategy: "sma_cross", period: "5y", handle: null,
                   universe: "indices", corrPeriod: "6mo",
                   start: "", end: "", xi: 0.6, rho: -0.7, q: 1e-5 };

/* researcher date window — appended to any request that supports it */
function vizWindowQS() {
  let qs = "";
  if (vizState.start) qs += `&start=${vizState.start}`;
  if (vizState.end) qs += `&end=${vizState.end}`;
  return qs;
}

function vizDispose() {
  if (vizState.handle) { try { vizState.handle.dispose(); } catch {} vizState.handle = null; }
}

async function renderViz(view, params = {}) {
  if (params.tab) vizState.tab = params.tab;
  vizDispose();
  view.innerHTML = `
    <div class="viz-bar">
      <input id="viz-sym" class="viz-sym" value="${state.symbol}" spellcheck="false"
             title="Symbol — Enter to reload">
      <div class="viz-tabs">
        ${VIZ_TABS.map((t) => `<button class="viz-tab${t.id === vizState.tab ? " active" : ""}"
          data-tab="${t.id}">${t.label}</button>`).join("")}
      </div>
      <span class="viz-controls" id="viz-controls"></span>
      <span class="viz-window" title="Optional exact date window — applies to CORRELATION, VAR, FRONTIER, KALMAN, ATTENTION and SWARM. Blank = rolling period.">
        <input type="date" id="viz-start" class="viz-date" value="${vizState.start}">
        <span class="faint">→</span>
        <input type="date" id="viz-end" class="viz-date" value="${vizState.end}">
      </span>
    </div>
    <div class="row viz-row">
      ${panel({ title: "3D — DRAG TO ORBIT · SCROLL TO ZOOM", meta: "—", id: "viz-panel",
                flush: true, body: `<div class="viz-host" id="viz-host">${loading("preparing scene")}</div>` })}
      <div style="display:grid;gap:12px;align-content:start">
        ${panel({ title: "READOUT", id: "viz-side", body: loading("…") })}
      </div>
    </div>`;

  $("#viz-sym").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { state.symbol = e.target.value.toUpperCase().trim(); loadVizTab(); }
  });
  $("#viz-start").onchange = (e) => { vizState.start = e.target.value; loadVizTab(); };
  $("#viz-end").onchange = (e) => { vizState.end = e.target.value; loadVizTab(); };
  view.querySelectorAll(".viz-tab").forEach((b) => {
    b.onclick = () => {
      vizState.tab = b.dataset.tab;
      view.querySelectorAll(".viz-tab").forEach((x) => x.classList.toggle("active", x === b));
      loadVizTab();
    };
  });
  loadVizTab();
}

function vizMeta(html) { const p = $("#viz-panel"); if (p) p.querySelector(".panel-meta").innerHTML = html; }
function vizSide(html) { const p = $("#viz-side"); if (p) p.querySelector(".panel-body").innerHTML = html; }
function vizHost() { return $("#viz-host"); }

async function loadVizTab() {
  vizDispose();
  const host = vizHost();
  if (!host) return;
  if (!window.Viz3D) {   // module script still parsing — retry shortly
    setTimeout(loadVizTab, 120);
    return;
  }
  $("#viz-controls").innerHTML = "";
  const title = $("#viz-panel .panel-title");
  if (title) title.textContent = {
    correlation: "PEARSON MATRIX — DAILY RETURNS",
    kalman: "KALMAN FILTER — LEVEL · TREND · SURPRISE",
  }[vizState.tab] || "3D — DRAG TO ORBIT · SCROLL TO ZOOM";
  host.innerHTML = loading("computing");
  vizSide(loading("…"));
  try {
    if (vizState.tab === "surface") await vizSurfaceTab(host);
    else if (vizState.tab === "greeks") await vizGreeksTab(host);
    else if (vizState.tab === "montecarlo") await vizMonteCarloTab(host);
    else if (vizState.tab === "correlation") await vizCorrelationTab(host);
    else if (vizState.tab === "var") await vizVarTab(host);
    else if (vizState.tab === "frontier") await vizFrontierTab(host);
    else if (vizState.tab === "heston") await vizHestonTab(host);
    else if (vizState.tab === "kalman") await vizKalmanTab(host);
    else if (vizState.tab === "attention") await vizAttentionTab(host);
    else await vizSwarmTab(host);
  } catch (e) {
    host.innerHTML = `<div class="empty">${e.message}</div>`;
    vizSide(`<div class="empty">${e.message}</div>`);
  }
}

async function vizSurfaceTab(host) {
  const d = await getJSON(`/api/viz/surface/${state.symbol}`);
  vizState.handle = Viz3D.mountSurface(host, {
    xs: d.strikes, zs: d.days, grid: d.iv,
    xLabel: "STRIKE", zLabel: "DAYS TO EXPIRY", yLabel: "IV",
    yFmt: (v) => `${(v * 100).toFixed(0)}%`,
    highlightRow: d.market_row, spotX: d.spot,
  });
  vizMeta(stamp(`SRC ${d.source.toUpperCase()} · GRID ${d.days.length}×${d.strikes.length} · ${d.elapsed_ms.toFixed(1)}ms`));
  const wingIdx = 0, atmIdx = d.iv[d.market_row].length >> 1;
  const skew = d.iv[d.market_row][wingIdx] - d.iv[d.market_row][atmIdx];
  vizSide(`
    <table class="viz-kv">
      <tr><td>SPOT</td><td>${fmt.n(d.spot)}</td></tr>
      <tr><td>ATM IV</td><td>${(d.atm_iv * 100).toFixed(1)}% ${iM(d.prov.surface, "IV SURFACE")}</td></tr>
      <tr><td>MARKET EXPIRY</td><td>${Math.round(d.chain_days)}d (amber slice)</td></tr>
      <tr><td>SMILE STRIKES</td><td>${d.strikes.length}</td></tr>
      <tr><td>SOURCE</td><td class="faint">${d.source}</td></tr>
    </table>
    ${insightBlock([
      { what: `Smile is ${skew > 0.01 ? "put-skewed" : skew < -0.01 ? "call-skewed" : "balanced"}`,
        why: `deep wing IV ${(d.iv[d.market_row][wingIdx] * 100).toFixed(1)}% vs ATM ${(d.atm_iv * 100).toFixed(1)}% on the market slice` },
      { what: "Only the amber slice is quoted data",
        why: "other maturities damp the smile by √(T_chain/T) — a documented model extension, not quotes" },
    ])}`);
}

async function vizGreeksTab(host) {
  $("#viz-controls").innerHTML = `
    ${["delta", "gamma", "theta", "vega"].map((g) =>
      `<button class="viz-mini${vizState.greek === g ? " active" : ""}" data-g="${g}">${g.toUpperCase()}</button>`).join("")}
    <span class="status-sep"></span>
    ${["call", "put"].map((s) =>
      `<button class="viz-mini${vizState.side === s ? " active" : ""}" data-s="${s}">${s.toUpperCase()}</button>`).join("")}`;
  document.querySelectorAll(".viz-mini[data-g]").forEach((b) => {
    b.onclick = () => { vizState.greek = b.dataset.g; loadVizTab(); };
  });
  document.querySelectorAll(".viz-mini[data-s]").forEach((b) => {
    b.onclick = () => { vizState.side = b.dataset.s; loadVizTab(); };
  });

  const d = await getJSON(`/api/viz/greeks/${state.symbol}?greek=${vizState.greek}&side=${vizState.side}`);
  vizState.handle = Viz3D.mountSurface(host, {
    xs: d.strikes, zs: d.days, grid: d.values,
    xLabel: "STRIKE", zLabel: "DAYS TO EXPIRY", yLabel: d.greek.toUpperCase(),
    spotX: d.spot,
  });
  vizMeta(stamp(`BS MODEL AT LIVE SPOT/IV · ${d.elapsed_ms.toFixed(1)}ms`));
  // locate the extreme cell to state where the exposure concentrates
  let mi = 0, mj = 0, mv = -Infinity;
  d.values.forEach((row, j) => row.forEach((v, i) => {
    if (Math.abs(v) > mv) { mv = Math.abs(v); mi = i; mj = j; }
  }));
  vizSide(`
    <table class="viz-kv">
      <tr><td>GREEK</td><td>${d.greek.toUpperCase()} (${d.side.toUpperCase()}) ${iM(d.prov.surface, "GREEKS SURFACE")}</td></tr>
      <tr><td>SPOT</td><td>${fmt.n(d.spot)}</td></tr>
      <tr><td>ATM IV USED</td><td>${(d.sigma * 100).toFixed(1)}%</td></tr>
      <tr><td>PEAK |${d.greek.toUpperCase()}|</td><td>${d.values[mj][mi].toPrecision(3)}</td></tr>
    </table>
    ${insightBlock([
      { what: `${d.greek} concentrates at K≈${fmt.n(d.strikes[mi], 0)}, T≈${Math.round(d.days[mj])}d`,
        why: "the grid's largest absolute value — where hedging flow is most sensitive" },
      { what: "Flat-IV model surface",
        why: "real smiles shift the concentration; read it as shape, not as quotes" },
    ])}`);
}

async function vizMonteCarloTab(host) {
  $("#viz-controls").innerHTML = [30, 60, 90, 120].map((h) =>
    `<button class="viz-mini${vizState.horizon === h ? " active" : ""}" data-h="${h}">${h}D</button>`).join("");
  document.querySelectorAll(".viz-mini[data-h]").forEach((b) => {
    b.onclick = () => { vizState.horizon = +b.dataset.h; loadVizTab(); };
  });

  const d = await getJSON(`/api/viz/montecarlo/${state.symbol}?horizon=${vizState.horizon}`);
  vizState.handle = Viz3D.mountFan(host, d);
  vizMeta(stamp(`${d.n_paths} PATHS · BLOCK BOOTSTRAP OF ${d.hist_bars} REAL DAILY RETURNS · ${d.elapsed_ms.toFixed(0)}ms`));
  const last = (k) => d.envelope[k][d.envelope[k].length - 1];
  vizSide(`
    <table class="viz-kv">
      <tr><td>ANCHOR SPOT</td><td>${fmt.n(d.spot)} ${iM(d.prov.fan, "PRICE FAN")}</td></tr>
      <tr><td>HORIZON</td><td>${d.horizon_days} sessions</td></tr>
      <tr><td>P(UP)</td><td class="${d.prob_up >= 0.5 ? "up" : "down"}">${(d.prob_up * 100).toFixed(1)}%</td></tr>
      <tr><td>P5 TERMINAL</td><td>${fmt.n(last("p5"))}</td></tr>
      <tr><td>P50 TERMINAL</td><td>${fmt.n(last("p50"))}</td></tr>
      <tr><td>P95 TERMINAL</td><td>${fmt.n(last("p95"))}</td></tr>
    </table>
    ${insightBlock([
      { what: `${d.horizon_days}-session range ${fmt.n(last("p5"), 0)}–${fmt.n(last("p95"), 0)} (90% of resampled histories)`,
        why: `${d.n_paths} block-bootstrap paths of ${state.symbol}'s own returns — fat tails preserved, no normality assumed` },
      { what: "Not a forecast",
        why: "resampling the past cannot see regime changes; it prices the *shape* of recent risk" },
    ])}`);
}

function corrCell(c, isDiag) {
  if (isDiag) return `<td class="cc diag">1.00</td>`;
  const a = Math.min(Math.abs(c), 1);
  const bg = c >= 0
    ? `rgba(241,86,75,${(a * 0.52).toFixed(3)})`
    : `rgba(88,166,255,${(a * 0.52).toFixed(3)})`;
  return `<td class="cc" style="background:${bg}">${c.toFixed(2)}</td>`;
}

async function vizCorrelationTab(host) {
  const UNIS = ["indices", "nifty50", "banks", "it", "fno", "mega", "tech", "etf"];
  $("#viz-controls").innerHTML = `
    <select id="corr-uni" class="viz-select">
      ${UNIS.map((u) => `<option${u === vizState.universe ? " selected" : ""}>${u}</option>`).join("")}
    </select>
    ${["3mo", "6mo", "1y"].map((p) =>
      `<button class="viz-mini${vizState.corrPeriod === p ? " active" : ""}" data-p="${p}">${p.toUpperCase()}</button>`).join("")}`;
  $("#corr-uni").onchange = (e) => { vizState.universe = e.target.value; loadVizTab(); };
  document.querySelectorAll(".viz-mini[data-p]").forEach((b) => {
    b.onclick = () => { vizState.corrPeriod = b.dataset.p; loadVizTab(); };
  });

  const d = await getJSON(`/api/viz/correlation?universe=${vizState.universe}&period=${vizState.corrPeriod}${vizWindowQS()}`);
  const n = d.symbols.length;
  host.innerHTML = `
    <div class="corr-wrap">
      <table class="corr-tbl">
        <thead><tr><th></th>${d.symbols.map((s) => `<th>${s}</th>`).join("")}</tr></thead>
        <tbody>
          ${d.matrix.map((row, j) => `<tr><th>${d.symbols[j]}</th>
            ${row.map((c, i) => corrCell(c, i === j)).join("")}</tr>`).join("")}
        </tbody>
      </table>
      <div class="corr-legend">
        <span class="lg neg">−1.0 inverse</span><span class="lg zero">0 independent</span>
        <span class="lg pos">+1.0 lockstep</span>
      </div>
    </div>`;
  vizMeta(stamp(`${n}×${n} · ${d.n_obs} OVERLAPPING DAYS · ${d.period.toUpperCase()} · ${d.elapsed_ms.toFixed(0)}ms`));
  const top = d.top_pairs[0], hedge = d.hedge_pairs[0];
  vizSide(`
    <table class="viz-kv">
      <tr><td>UNIVERSE</td><td>${d.universe} (${n}) ${iM(d.prov.matrix, "CORRELATION")}</td></tr>
      <tr><td>AVG CORR</td><td>${d.avg_corr.toFixed(2)}</td></tr>
      <tr><td>MOST CORRELATED</td><td>${top.a}↔${top.b} <span class="${top.corr >= 0 ? "down" : "up"}">${top.corr >= 0 ? "+" : ""}${top.corr.toFixed(2)}</span></td></tr>
      <tr><td>BEST HEDGE</td><td>${hedge.a}↔${hedge.b} <span class="${hedge.corr < 0 ? "up" : ""}">${hedge.corr >= 0 ? "+" : ""}${hedge.corr.toFixed(2)}</span></td></tr>
      ${d.dropped.length ? `<tr><td>DROPPED (THIN)</td><td class="faint">${d.dropped.join(", ")}</td></tr>` : ""}
    </table>
    <div class="panel-title" style="margin:10px 0 5px">TIGHTEST PAIRS</div>
    <table class="viz-kv">${d.top_pairs.map((p) =>
      `<tr><td>${p.a}↔${p.b}</td><td>${p.corr >= 0 ? "+" : ""}${p.corr.toFixed(2)}</td></tr>`).join("")}
    </table>
    <div class="panel-title" style="margin:10px 0 5px">DIVERSIFIERS</div>
    <table class="viz-kv">${d.hedge_pairs.map((p) =>
      `<tr><td>${p.a}↔${p.b}</td><td>${p.corr >= 0 ? "+" : ""}${p.corr.toFixed(2)}</td></tr>`).join("")}
    </table>
    ${insightBlock([
      { what: `Portfolio diversification is ${Math.abs(d.avg_corr) < 0.35 ? "healthy" : Math.abs(d.avg_corr) < 0.6 ? "moderate" : "poor"}`,
        why: `average pairwise correlation ${d.avg_corr.toFixed(2)} across ${d.n_obs} overlapping days` },
      { what: "Regime-dependent number",
        why: "correlations converge toward 1 in a crash — a calm-period matrix understates joint risk" },
    ])}`);
}

const VIZ_UNIVERSES = ["indices", "nifty50", "banks", "it", "fno", "mega", "tech", "etf"];

function vizUniverseControls(periods) {
  $("#viz-controls").innerHTML = `
    <select id="vu-uni" class="viz-select">
      ${VIZ_UNIVERSES.map((u) => `<option${u === vizState.universe ? " selected" : ""}>${u}</option>`).join("")}
    </select>
    ${periods.map((p) =>
      `<button class="viz-mini${vizState.corrPeriod === p ? " active" : ""}" data-p="${p}">${p.toUpperCase()}</button>`).join("")}`;
  $("#vu-uni").onchange = (e) => { vizState.universe = e.target.value; loadVizTab(); };
  document.querySelectorAll(".viz-mini[data-p]").forEach((b) => {
    b.onclick = () => { vizState.corrPeriod = b.dataset.p; loadVizTab(); };
  });
}

async function vizVarTab(host) {
  vizUniverseControls(["6mo", "1y", "2y"]);
  const d = await getJSON(`/api/viz/var?universe=${vizState.universe}&period=${vizState.corrPeriod}${vizWindowQS()}`);
  vizState.handle = Viz3D.mountSurface(host, {
    xs: d.surface_bins.map((b) => b * 100), zs: d.horizons, grid: d.surface,
    xLabel: "P&L %", zLabel: "HORIZON (SESSIONS)", yLabel: "DENSITY",
    yFmt: (v) => v.toFixed(1),
  });
  vizMeta(stamp(`${d.n_paths} JOINT BOOTSTRAP PATHS · ${d.n_obs} OVERLAPPING DAYS · ${d.elapsed_ms.toFixed(0)}ms`));
  const hz = d.horizons;
  const rows = [0, 3, 5, hz.length - 1].filter((i, k, a) => a.indexOf(i) === k)
    .map((i) => `<tr><td>${hz[i]}D VAR / ES</td>
      <td class="down">−${(d.var_curve[i] * 100).toFixed(2)}% / −${(d.es_curve[i] * 100).toFixed(2)}%</td></tr>`).join("");
  vizSide(`
    <table class="viz-kv">
      <tr><td>BASKET</td><td>equal-wt ${d.symbols.length} ${iM(d.prov.var, "VALUE AT RISK")}</td></tr>
      ${rows}
      <tr><td>${hz[hz.length - 1]}D UPSIDE P95</td><td class="up">+${(d.p95_curve[hz.length - 1] * 100).toFixed(2)}%</td></tr>
    </table>
    ${insightBlock([
      { what: `A 1-in-20 bad ${hz[hz.length - 1]}-session stretch loses ${(d.var_curve[hz.length - 1] * 100).toFixed(1)}%+`,
        why: `5th percentile of ${d.n_paths} joint-bootstrap paths; tail average (ES) −${(d.es_curve[hz.length - 1] * 100).toFixed(1)}%` },
      { what: "History-bounded estimate",
        why: "a loss larger than anything in the sample window cannot appear — VaR is a floor on surprise, not a ceiling" },
    ])}`);
}

async function vizFrontierTab(host) {
  if (vizState.universe === "indices") vizState.universe = "nifty50";
  vizUniverseControls(["1y", "2y"]);
  const d = await getJSON(`/api/viz/frontier?universe=${vizState.universe}&period=${vizState.corrPeriod}${vizWindowQS()}`);
  vizState.handle = Viz3D.mountScatter(host, {
    points: d.points.map((p) => [p[0] * 100, p[1] * 100, p[2]]),
    xLabel: "VOL % (ANN)", yLabel: "RETURN %", zLabel: "SHARPE",
    marks: [
      { x: d.max_sharpe.vol * 100, y: d.max_sharpe.ret * 100, c: d.max_sharpe.sharpe,
        color: 0xf0a826, label: "MAX SHARPE" },
      { x: d.min_vol.vol * 100, y: d.min_vol.ret * 100, c: d.min_vol.sharpe,
        color: 0x58a6ff, label: "MIN VOL" },
    ],
  });
  vizMeta(stamp(`${d.n_portfolios} RANDOM LONG-ONLY PORTFOLIOS · ${d.n_obs} DAYS · ${d.elapsed_ms.toFixed(0)}ms`));
  const wRows = (p) => Object.entries(p.weights).sort((a, b) => b[1] - a[1]).slice(0, 5)
    .map(([s, w]) => `<tr><td>${s}</td><td>${(w * 100).toFixed(1)}%</td></tr>`).join("");
  vizSide(`
    <table class="viz-kv">
      <tr><td>MAX SHARPE</td><td>${d.max_sharpe.sharpe.toFixed(2)} ${iM(d.prov.frontier, "FRONTIER")}</td></tr>
      <tr><td>· RET / VOL</td><td>${(d.max_sharpe.ret * 100).toFixed(1)}% / ${(d.max_sharpe.vol * 100).toFixed(1)}%</td></tr>
      <tr><td>MIN VOL</td><td>${(d.min_vol.vol * 100).toFixed(1)}% ann</td></tr>
    </table>
    <div class="panel-title" style="margin:10px 0 5px">MAX-SHARPE TOP WEIGHTS</div>
    <table class="viz-kv">${wRows(d.max_sharpe)}</table>
    <div class="panel-title" style="margin:10px 0 5px">MIN-VOL TOP WEIGHTS</div>
    <table class="viz-kv">${wRows(d.min_vol)}</table>
    ${insightBlock([
      { what: "The cloud's upper-left hull is the attainable frontier",
        why: `${d.n_portfolios} Dirichlet portfolios over ${d.symbols.length} names — no optimizer, no shorting` },
      { what: "Weights are illustrative, not advice",
        why: "μ from one year of history is the noisiest input in finance; re-run across periods before acting" },
    ])}`);
}

async function vizHestonTab(host) {
  $("#viz-controls").innerHTML = `
    <span class="faint" style="font-size:9.5px">ξ</span>
    ${[0.2, 0.6, 1.2].map((x) => `<button class="viz-mini${vizState.xi === x ? " active" : ""}" data-xi="${x}">${x}</button>`).join("")}
    <span class="status-sep"></span>
    <span class="faint" style="font-size:9.5px">ρ</span>
    ${[-0.9, -0.7, -0.3].map((r) => `<button class="viz-mini${vizState.rho === r ? " active" : ""}" data-rho="${r}">${r}</button>`).join("")}`;
  document.querySelectorAll(".viz-mini[data-xi]").forEach((b) => {
    b.onclick = () => { vizState.xi = +b.dataset.xi; loadVizTab(); };
  });
  document.querySelectorAll(".viz-mini[data-rho]").forEach((b) => {
    b.onclick = () => { vizState.rho = +b.dataset.rho; loadVizTab(); };
  });
  const d = await getJSON(`/api/viz/heston/${state.symbol}?xi=${vizState.xi}&rho=${vizState.rho}`);
  vizState.handle = Viz3D.mountFan(host, d);
  vizMeta(stamp(`v₀ FROM LIVE ATM IV (${(Math.sqrt(d.v0) * 100).toFixed(1)}%) · ${d.n_paths} PATHS · ${d.elapsed_ms.toFixed(0)}ms`));
  vizSide(`
    <table class="viz-kv">
      <tr><td>SPOT / v₀</td><td>${fmt.n(d.spot)} / ${d.v0.toFixed(4)} ${iM(d.prov.fan, "HESTON")}</td></tr>
      <tr><td>κ MEAN REVERSION</td><td>${d.kappa}</td></tr>
      <tr><td>ξ VOL-OF-VOL / ρ</td><td>${d.xi} / ${d.rho}</td></tr>
      <tr><td>FELLER 2κθ ≥ ξ²</td><td class="${d.feller_ok ? "up" : "down"}">${d.feller_ok ? "SATISFIED" : "VIOLATED"}</td></tr>
      <tr><td>P(UP) ${d.horizon_days}D</td><td class="${d.prob_up >= 0.5 ? "up" : "down"}">${(d.prob_up * 100).toFixed(1)}%</td></tr>
    </table>
    ${insightBlock([
      { what: `With ρ=${d.rho}, vol-of-vol buys crash risk`,
        why: `downside percentile deepens as ξ rises — the smile's skew is exactly this asymmetry priced in` },
      { what: "Scenario machine, not a forecast",
        why: "v₀ is anchored to the live chain; κ/θ/ξ/ρ are your inputs — calibrate to the smile before quoting" },
    ])}`);
}

async function vizKalmanTab(host) {
  $("#viz-controls").innerHTML = `
    <span class="faint" style="font-size:9.5px">TREND AGILITY</span>
    ${[["1e-7", "SMOOTH"], ["1e-5", "BALANCED"], ["1e-3", "FAST"]].map(([v, l]) =>
      `<button class="viz-mini${vizState.q === +v ? " active" : ""}" data-q="${v}">${l}</button>`).join("")}`;
  document.querySelectorAll(".viz-mini[data-q]").forEach((b) => {
    b.onclick = () => { vizState.q = +b.dataset.q; loadVizTab(); };
  });
  const d = await getJSON(`/api/viz/kalman/${state.symbol}?q=${vizState.q}${vizWindowQS()}`);
  vizDispose();
  host.innerHTML = `
    <div style="padding:12px">
      <canvas id="kal-price"></canvas>
      <div class="panel-title" style="margin:10px 0 4px">INNOVATION Z — SURPRISE PER BAR (±2σ RAILS)</div>
      <canvas id="kal-z"></canvas>
    </div>`;
  const X = (i) => i;
  const fx = (v) => d.times[Math.round(v)] ? d.times[Math.round(v)].slice(2) : "";
  linePlot($("#kal-price"), [
    { points: d.close.map((v, i) => ({ x: X(i), y: v })), color: "#5d6470", width: 1.1 },
    { points: d.level.map((v, i) => ({ x: X(i), y: v + 0 })), color: "#f0a826", width: 1.8 },
    { points: d.level.map((v, i) => ({ x: X(i), y: v * Math.exp(d.band[i]) })), color: "rgba(240,168,38,0.35)", width: 1 },
    { points: d.level.map((v, i) => ({ x: X(i), y: v * Math.exp(-d.band[i]) })), color: "rgba(240,168,38,0.35)", width: 1 },
  ], { height: 260, fmtX: fx, fmtY: (v) => fmt.compact(v) });
  linePlot($("#kal-z"), [
    { points: d.innovation_z.map((v, i) => ({ x: X(i), y: v })), color: "#58a6ff", width: 1.2, fillZero: true },
  ], { height: 120, zeroLine: true, fmtX: fx, fmtY: (v) => v.toFixed(1) });
  vizMeta(stamp(`${d.bars} BARS · q=${d.q} r=${d.r} · ${d.elapsed_ms.toFixed(1)}ms`));
  const slopeAnn = d.slope[d.slope.length - 1] * 252;
  vizSide(`
    <table class="viz-kv">
      <tr><td>FILTERED LEVEL</td><td>${fmt.n(d.level[d.level.length - 1])} ${iM(d.prov.filter, "KALMAN")}</td></tr>
      <tr><td>TREND (ANN.)</td><td class="${slopeAnn >= 0 ? "up" : "down"}">${(slopeAnn * 100).toFixed(1)}%/yr</td></tr>
      <tr><td>LAST SURPRISE z</td><td class="${Math.abs(d.last_z) > 2 ? "down" : ""}">${d.last_z.toFixed(2)}σ</td></tr>
    </table>
    ${insightBlock([
      { what: Math.abs(d.last_z) > 2
          ? `Latest bar was a ${d.last_z.toFixed(1)}σ surprise vs the filter`
          : "Price is tracking the filtered trend",
        why: `innovation z ${d.last_z.toFixed(2)} against the ±2σ rails` },
      { what: `Trend estimate ${(slopeAnn * 100).toFixed(1)}%/yr drift`,
        why: `state-space slope; agility set by q=${d.q} — a filter smooths the past, it does not know the future` },
    ])}`);
}

async function vizAttentionTab(host) {
  const d = await getJSON(`/api/viz/attention/${state.symbol}?window=90${vizWindowQS()}`);
  const n = d.dates.length;
  vizState.handle = Viz3D.mountSurface(host, {
    xs: d.dates.map((_, i) => i), zs: d.dates.map((_, i) => i), grid: d.matrix,
    xLabel: "KEY DAY →", zLabel: "QUERY DAY →", yLabel: "ATTN",
    yFmt: (v) => v.toFixed(2), highlightRow: n - 1,
  });
  vizMeta(stamp(`${n}×${n} UNTRAINED KERNEL ATTENTION · ${d.elapsed_ms.toFixed(1)}ms · AMBER ROW = TODAY`));
  vizSide(`
    <table class="viz-kv">
      <tr><td>TODAY</td><td>${d.dates[n - 1]} ${iM(d.prov.attention, "ATTENTION")}</td></tr>
      <tr><td>ANALOG ${d.fwd_days}D MEAN</td><td class="${d.analog_fwd_mean >= 0 ? "up" : "down"}">${fmt.pct(d.analog_fwd_mean)}</td></tr>
    </table>
    <div class="panel-title" style="margin:10px 0 5px">TODAY'S TOP ANALOG DAYS</div>
    <table class="viz-kv">
      ${d.top_analogs.map((t) => `<tr><td>${t.date} · w=${(t.weight * 100).toFixed(1)}%</td>
        <td class="${(t.fwd ?? 0) >= 0 ? "up" : "down"}">${t.fwd === null || t.fwd === undefined ? "—" : fmt.pct(t.fwd)}</td></tr>`).join("")}
    </table>
    ${insightBlock([
      { what: `After the days most like today, ${state.symbol} averaged ${fmt.pct(d.analog_fwd_mean)} over ${d.fwd_days} sessions`,
        why: "similarity-weighted history of the top analogs' forward returns" },
      { what: "Untrained attention — analog research, not prediction",
        why: "state similarity says setups rhyme; it cannot say they resolve the same way" },
    ])}`);
}

async function vizSwarmTab(host) {
  const strategies = await getJSON("/api/strategies");
  const eligible = Object.entries(strategies).filter(([, s]) => {
    const numeric = Object.values(s.param_grid || {}).filter((v) =>
      Array.isArray(v) && v.length && v.every((x) => typeof x === "number"));
    return numeric.length >= 2;
  }).map(([name]) => name);
  if (!eligible.includes(vizState.strategy)) vizState.strategy = eligible[0];

  $("#viz-controls").innerHTML = `
    <select id="swarm-strat" class="viz-select">
      ${eligible.map((n) => `<option${n === vizState.strategy ? " selected" : ""}>${n}</option>`).join("")}
    </select>
    <select id="swarm-period" class="viz-select">
      ${["2y", "5y", "10y"].map((p) => `<option${p === vizState.period ? " selected" : ""}>${p}</option>`).join("")}
    </select>
    <button class="viz-mini run" id="swarm-run">RUN SWARM</button>
    <button class="viz-mini" id="swarm-replay" disabled>REPLAY</button>
    <span class="viz-iter" id="swarm-iter"></span>`;
  $("#swarm-strat").onchange = (e) => { vizState.strategy = e.target.value; };
  $("#swarm-period").onchange = (e) => { vizState.period = e.target.value; };
  $("#swarm-run").onclick = runSwarm;
  host.innerHTML = `<div class="empty">Pick a strategy and RUN SWARM — 24 particles × 30 iterations,
    every fitness evaluation a full backtest on real ${state.symbol} history.
    The surface is the actual Sharpe landscape; watch the flock find its peak.</div>`;
  vizSide(`<div class="feed-note">Particle-swarm optimization (PSO): particles share their best
    discoveries and orbit toward them — swarm intelligence over the parameter plane.
    In-sample by construction: validate winners with walk-forward (BTL view) before believing them.</div>`);
}

async function runSwarm() {
  const host = vizHost();
  const btn = $("#swarm-run");
  btn.disabled = true; btn.textContent = "FLYING…";
  host.innerHTML = loading(`backtesting the parameter plane of ${vizState.strategy} on ${state.symbol}`);
  try {
    const d = await postJSON("/api/swarm", {
      symbol: state.symbol, strategy: vizState.strategy, period: vizState.period,
      start: vizState.start || null, end: vizState.end || null,
    });
    vizDispose();
    const iterEl = $("#swarm-iter");
    vizState.handle = Viz3D.mountSwarm(host, d, {
      onIter: (i, playing) => {
        if (iterEl) iterEl.textContent =
          `ITER ${i + 1}/${d.iterations.length} · GBEST SHARPE ${d.iterations[i].gf.toFixed(2)}${playing ? "" : " · DONE"}`;
      },
    });
    const rp = $("#swarm-replay");
    rp.disabled = false;
    rp.onclick = () => vizState.handle && vizState.handle.replay();
    vizMeta(stamp(`${d.n_evals} UNIQUE BACKTESTS · ${d.bars} BARS EACH · ${(d.elapsed_ms / 1000).toFixed(1)}s TOTAL`));
    const [px, py] = d.param_names;
    const m = d.best_metrics;
    vizSide(`
      <table class="viz-kv">
        <tr><td>BEST ${px.toUpperCase()}</td><td>${d.best_params[px]}</td></tr>
        <tr><td>BEST ${py.toUpperCase()}</td><td>${d.best_params[py]}</td></tr>
        <tr><td>SHARPE</td><td class="${d.best_fitness >= 0 ? "up" : "down"}">${d.best_fitness.toFixed(2)} ${iM(d.prov.fitness, "SWARM FITNESS")}</td></tr>
        <tr><td>TOTAL RETURN</td><td class="${(m.total_return || 0) >= 0 ? "up" : "down"}">${fmt.pct(m.total_return)}</td></tr>
        <tr><td>MAX DRAWDOWN</td><td class="down">${fmt.pct(m.max_drawdown)}</td></tr>
        <tr><td>WIN RATE</td><td>${m.win_rate === undefined ? "—" : (m.win_rate * 100).toFixed(1) + "%"}</td></tr>
        <tr><td>TRADES</td><td>${fmt.i(m.num_trades)}</td></tr>
        <tr><td>EVALUATIONS</td><td>${d.n_evals} real backtests</td></tr>
      </table>
      ${insightBlock([
        { what: d.verdict.split(" — ")[0], why: d.verdict.split(" — ")[1] || "" },
        { what: "In-sample optimum", why: `run wf ${state.symbol} ${d.strategy} in the Lab to test it out-of-sample before trusting it` },
      ])}`);
  } catch (e) {
    host.innerHTML = `<div class="empty">${e.message}</div>`;
    toast(`Swarm: ${e.message}`, "err");
  } finally {
    btn.disabled = false; btn.textContent = "RUN SWARM";
  }
}

/* ---------- MORNING BRIEF (BRF) ----------
   The whole research loop in one screen: cues → positioning → vol setup →
   quant reads → news → a transparent vote count. Degraded sources are
   flagged on the vote itself — the brief never hides its weak legs. */

const CUE_LABELS = { "^GSPC": "S&P 500", "^IXIC": "NASDAQ", INDIAVIX: "INDIA VIX",
                     USDINR: "USD/INR", "GC=F": "GOLD", "BZ=F": "BRENT", "^TNX": "US 10Y" };
const DIR_CHIP = { bullish: ["▲", "up"], bearish: ["▼", "down"], neutral: ["•", "faint"] };

async function renderBrief(view, params = {}) {
  // The daily analysis: root first (what the underlying did), then vol, then
  // derivatives positioning, then who moved, then base rates, then news.
  // Modelled on a desk end-of-day note with one deliberate difference: it
  // ends at the facts. No verdict, no trade call.
  if (params.symbol) state.symbol = params.symbol.toUpperCase();
  view.innerHTML = `
    <div class="viz-bar">
      <input id="brf-sym" class="viz-sym" value="${esc(state.symbol)}" spellcheck="false">
      <span class="faint" style="font-size:10.5px">DAILY ANALYSIS — root to derivatives · facts and base rates, no verdict by design</span>
      <button class="viz-mini run" id="brf-run">REFRESH ⟳</button>
    </div>
    <div id="brf-body">${loading("composing: chart · vol · positioning · participants · base rates · news")}</div>`;
  $("#brf-sym").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { state.symbol = e.target.value.toUpperCase().trim(); loadDaily(); }
  });
  $("#brf-run").onclick = loadDaily;
  loadDaily();
}

const secErr = (v) => `<div class="empty" style="padding:8px 12px">${esc(v.error || "unavailable")}</div>`;

async function loadDaily() {
  const host = $("#brf-body");
  if (!host) return;
  try {
    const d = await getJSON(`/api/analysis/daily/${encodeURIComponent(state.symbol)}`);
    const ch = d.chart || {}, vol = d.vol || {}, pos = d.positioning || {};
    const parts = d.participants || {}, ev = d.events || {}, news = d.news || {};

    const chartBody = ch.error ? secErr(ch) : `
      ${metricsStrip([
        ["CLOSE", fmt.n(ch.close), ""],
        ["DAY", fmt.pct(ch.chg_pct / 100), cls(ch.chg_pct)],
        ["GAP", fmt.pct(ch.gap_pct / 100), cls(ch.gap_pct)],
        ["INTRADAY", fmt.pct(ch.intraday_pct / 100), cls(ch.intraday_pct)],
        ["WEEK", fmt.pct(ch.week_chg_pct / 100), cls(ch.week_chg_pct)],
        ["CANDLE", esc((ch.daily_candle || "").toUpperCase()),
          ch.daily_candle && ch.daily_candle.includes("bear") ? "down"
          : ch.daily_candle && ch.daily_candle.includes("bull") ? "up" : ""],
      ])}
      <div class="empty" style="padding:6px 12px">closed at ${
        ch.range_pos_pct == null ? "—" : fmt.n(ch.range_pos_pct, 0) + "% of the day range"} ·
        realised vol ${fmt.n(ch.rv5, 1)}% (5d) / ${fmt.n(ch.rv21, 1)}% (21d)</div>`;

    const volBody = vol.error ? secErr(vol) : `
      ${metricsStrip([
        ["INDIA VIX", fmt.n(vol.vix, 2), ""],
        ["PERCENTILE", fmt.n(vol.vix_pctile, 1) + "th", vol.vix_pctile < 20 ? "down" : vol.vix_pctile > 80 ? "up" : ""],
        ["ATM IV", pos.atm_iv_pct ? fmt.n(pos.atm_iv_pct, 2) + "%" : "—", ""],
        ["IMPLIED MOVE", pos.implied_move_pct ? "±" + fmt.n(pos.implied_move_pct, 2) + "%" : "—", ""],
      ])}
      <div class="empty" style="padding:6px 12px">VIX percentile is against every session since 2008 in the local archive (as of ${esc(vol.vix_date || "—")})</div>`;

    const posBody = pos.error
      ? `<div class="empty" style="padding:8px 12px">${esc(pos.error)}${
          (pos.source_trail || []).map((t) => `<br><span class="faint">· ${esc(t)}</span>`).join("")}</div>`
      : `${metricsStrip([
          ["PCR (OI)", fmt.n(pos.pcr_oi, 2), pos.pcr_oi > 1.1 ? "up" : pos.pcr_oi < 0.9 ? "down" : ""],
          ["MAX PAIN", fmt.i(pos.max_pain), ""],
          ["VS SPOT", pos.dist_to_max_pain_pct == null ? "—" : fmt.pct(pos.dist_to_max_pain_pct / 100), cls(pos.dist_to_max_pain_pct)],
          ["OI SUPPORT", fmt.i(pos.support), "up"],
          ["OI RESIST", fmt.i(pos.resistance), "down"],
        ])}
        <div class="empty" style="padding:6px 12px">${esc(pos.source || "")} · expiry ${esc(pos.expiry || "—")}${
          pos.is_model ? ' · <span class="warn">MODELLED — positioning here is not market data</span>' : ""}</div>`;

    const partBody = parts.error ? secErr(parts) : `
      <table class="tbl"><thead><tr><th class="txt">WHO</th>
        <th>IDX FUT NET</th><th>Δ1D</th><th>IDX OPT NET</th><th>Δ1D</th><th class="txt">READ</th>
      </tr></thead><tbody>
        ${Object.entries(parts.by_participant || {}).map(([who, r]) => `<tr>
          <td class="txt sym">${esc(who)}</td>
          <td class="${cls(r.idx_fut_net)}">${fmt.n(r.idx_fut_net, 0)}</td>
          <td class="${cls(r.idx_fut_net_chg)}">${r.idx_fut_net_chg == null ? "—" : fmt.n(r.idx_fut_net_chg, 0)}</td>
          <td class="${cls(r.idx_opt_net)}">${fmt.n(r.idx_opt_net, 0)}</td>
          <td class="${cls(r.idx_opt_net_chg)}">${r.idx_opt_net_chg == null ? "—" : fmt.n(r.idx_opt_net_chg, 0)}</td>
          <td class="txt ${r.read === "added bullish exposure" ? "up" : r.read === "added bearish exposure" ? "down" : "faint"}">${esc(r.read || "")}</td></tr>`).join("")}
      </tbody></table>
      <div class="empty" style="padding:6px 12px">NSE participant-wise OI, ${esc(parts.date || "")} vs ${esc(parts.prev_date || "")} ·
        contracts, direction-sign convention (long calls + short puts = bullish) · levels are structural, the CHANGE is the read</div>`;

    const evToday = (ev.today && ev.today.classification) || "—";
    const study = (key, label) => {
      const s0 = ev[key]; if (!s0 || s0.error) return "";
      const rows = ["1", "5", "10", "21"].map((h) => {
        const c = s0.horizons[h], b = s0.baseline[h];
        if (!c || !c.n) return "";
        return `<tr><td class="txt">${label} → +${h}d</td>
          <td class="${cls(c.mean_pct)}">${fmt.n(c.mean_pct, 2)}%</td>
          <td>${fmt.n(c.hit_rate * 100, 0)}%</td>
          <td class="faint">${fmt.n(b.mean_pct, 2)}%</td>
          <td class="faint">${c.t_stat == null ? "—" : fmt.n(c.t_stat, 1)}</td>
          <td class="faint">${c.n}</td></tr>`;
      }).join("");
      return rows;
    };
    const evBody = ev.error ? secErr(ev) : `
      <div class="empty" style="padding:6px 12px">latest session classifies as
        <b class="${evToday.includes("down") ? "down" : evToday.includes("up") ? "up" : ""}">${esc(evToday)}</b>
        (z = ${ev.today && ev.today.z != null ? ev.today.z : "—"}) ·
        base rates below are history, not a forecast</div>
      <table class="tbl"><thead><tr><th class="txt">AFTER A…</th>
        <th>MEAN</th><th>HIT</th><th>ANY-DAY MEAN</th><th>t</th><th>n</th></tr></thead>
      <tbody>${study("down_2s", "−2σ day")}${study("up_2s", "+2σ day")}</tbody></table>`;

    const newsBody = news.error ? secErr(news) : `
      <div class="empty" style="padding:6px 12px">bias ${esc(String(news.bias_label || "—"))}
        (${news.bias_score == null ? "—" : fmt.n(news.bias_score, 2)}) · ${news.n_items || 0} headlines</div>
      ${(news.headlines || []).map((h) => `<div class="empty" style="padding:2px 12px">· ${esc(h.title)}
        <span class="faint">${esc(h.source || "")}</span></div>`).join("")}`;

    const sec = (title, body) => panel({ title, flush: true, meta: "", body });
    host.innerHTML = `
      <div class="empty" style="padding:4px 2px">${ageStamp(d.as_of ? d.as_of + "T15:30:00+05:30" : null)}
        <span class="faint">· root → derivatives · every section fails to a named reason, never a filled box</span></div>
      <div style="display:grid;gap:6px">
        ${sec("CHART — THE UNDERLYING", chartBody)}
        ${sec("VOLATILITY", volBody)}
        ${sec("OPEN INTEREST & POSITIONING", posBody)}
        ${sec("PARTICIPANTS — WHO MOVED (FII / DII / CLIENT / PRO)", partBody)}
        ${sec("EVENT BASE RATES — WHAT HISTORY SAYS ABOUT A DAY LIKE TODAY", evBody)}
        ${sec("NEWS", newsBody)}
      </div>`;
  } catch (e) {
    host.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
}

/* ---------- ML STUDIO (MLS) ----------
   Pure-numpy models on real history: chronological split, majority-class
   baseline always shown, and the caveat that a small edge on one split is
   exploration, not a trading system. */

const mlsState = { model: "stumps", horizon: 5, period: "5y",
                   features: ["ret1", "ret5", "rsi14", "ema_gap", "vol20"] };

async function renderMLStudio(view, params = {}) {
  if (params.symbol) state.symbol = params.symbol.toUpperCase();
  let feats = {};
  try { feats = await getJSON("/api/ml/features"); } catch {}
  view.innerHTML = `
    <div class="row mls-row">
      ${panel({ title: "MODEL CONFIG", id: "mls-cfg", body: `
        <table class="viz-kv">
          <tr><td>SYMBOL</td><td><input id="mls-sym" class="viz-sym" style="width:100%" value="${state.symbol}"></td></tr>
          <tr><td>MODEL</td><td><select id="mls-model" class="viz-select" style="width:100%">
            <option value="stumps"${mlsState.model === "stumps" ? " selected" : ""}>boosted stumps (numpy)</option>
            <option value="ridge"${mlsState.model === "ridge" ? " selected" : ""}>ridge regression</option>
          </select></td></tr>
          <tr><td>HORIZON</td><td><select id="mls-h" class="viz-select" style="width:100%">
            ${[1, 5, 10, 21].map((h) => `<option value="${h}"${h === mlsState.horizon ? " selected" : ""}>${h} session${h > 1 ? "s" : ""}</option>`).join("")}
          </select></td></tr>
          <tr><td>HISTORY</td><td><select id="mls-p" class="viz-select" style="width:100%">
            ${["2y", "5y", "10y", "max"].map((p) => `<option${p === mlsState.period ? " selected" : ""}>${p}</option>`).join("")}
          </select></td></tr>
          <tr><td>WINDOW</td><td>
            <input type="date" id="mls-start" class="viz-date" value="${mlsState.start || ""}">
            <input type="date" id="mls-end" class="viz-date" value="${mlsState.end || ""}">
          </td></tr>
        </table>
        <div class="panel-title" style="margin:12px 0 6px">FEATURES</div>
        <div class="mls-feats">
          ${Object.entries(feats).map(([n, desc]) => `
            <label class="mls-feat" title="${desc}">
              <input type="checkbox" data-f="${n}"${mlsState.features.includes(n) ? " checked" : ""}> ${n}
            </label>`).join("")}
        </div>
        <button class="viz-mini run" id="mls-train" style="margin-top:12px;width:100%;padding:9px">TRAIN ▶</button>
        <div class="feed-note" style="margin-top:10px">Strictly chronological split — trains on the past,
          tests on the future. The majority-class baseline always ships next to accuracy.</div>` })}
      ${panel({ title: "RESULTS", id: "mls-out", meta: "—",
                body: `<div class="empty">Configure and hit TRAIN — models are pure numpy,
                       training takes well under a second.</div>` })}
    </div>`;
  $("#mls-sym").addEventListener("keydown", (e) => {
    if (e.key === "Enter") state.symbol = e.target.value.toUpperCase().trim();
  });
  $("#mls-train").onclick = trainML;
}

async function trainML() {
  state.symbol = $("#mls-sym").value.toUpperCase().trim() || state.symbol;
  mlsState.model = $("#mls-model").value;
  mlsState.horizon = +$("#mls-h").value;
  mlsState.period = $("#mls-p").value;
  mlsState.features = Array.from(document.querySelectorAll(".mls-feat input:checked"))
    .map((c) => c.dataset.f);
  mlsState.start = $("#mls-start").value;
  mlsState.end = $("#mls-end").value;
  const btn = $("#mls-train");
  const body = $("#mls-out .panel-body");
  btn.disabled = true; btn.textContent = "TRAINING…";
  body.innerHTML = loading(`fitting ${mlsState.model} on ${state.symbol}`);
  try {
    const d = await postJSON("/api/ml/train", {
      symbol: state.symbol, features: mlsState.features, model: mlsState.model,
      period: mlsState.period, horizon: mlsState.horizon,
      start: mlsState.start || null, end: mlsState.end || null,
    });
    $("#mls-out .panel-meta").innerHTML =
      stamp(`${d.n_train} TRAIN / ${d.n_test} TEST ROWS · ${d.elapsed_ms.toFixed(0)}ms`);
    const edgeCls = d.edge > 0.02 ? "up" : d.edge < -0.02 ? "down" : "faint";
    body.innerHTML = `
      <table class="viz-kv">
        <tr><td>TEST ACCURACY</td><td>${(d.acc_test * 100).toFixed(1)}% ${iM(d.prov.accuracy, "ML ACCURACY")}</td></tr>
        <tr><td>MAJORITY BASELINE</td><td>${(d.baseline_test * 100).toFixed(1)}%</td></tr>
        <tr><td>EDGE VS BASELINE</td><td class="${edgeCls}">${d.edge >= 0 ? "+" : ""}${(d.edge * 100).toFixed(1)}pp</td></tr>
        <tr><td>TRAIN ACCURACY</td><td class="faint">${(d.acc_train * 100).toFixed(1)}% ${d.acc_train - d.acc_test > 0.1 ? "· overfit gap!" : ""}</td></tr>
        <tr><td>FWD RET | MODEL SAYS UP</td><td class="${d.up_ret_test >= 0 ? "up" : "down"}">${fmt.pct(d.up_ret_test)}</td></tr>
        <tr><td>FWD RET | MODEL SAYS DOWN</td><td class="${d.down_ret_test >= 0 ? "up" : "down"}">${fmt.pct(d.down_ret_test)}</td></tr>
      </table>
      <div class="panel-title" style="margin:12px 0 6px">FEATURE IMPORTANCE</div>
      ${Object.entries(d.importances).map(([n, v]) => `
        <div class="mls-imp"><span class="lbl">${n}</span>
          <span class="bar"><span style="width:${(v * 100).toFixed(1)}%"></span></span>
          <span class="val">${(v * 100).toFixed(0)}%</span></div>`).join("")}
      <div class="panel-title" style="margin:14px 0 6px">TEST SEGMENT — FOLLOW THE CALLS (FRICTIONLESS)</div>
      <canvas id="mls-eq"></canvas>
      ${insightBlock([
        { what: d.edge > 0.03
            ? `${(d.edge * 100).toFixed(1)}pp above baseline on unseen data`
            : d.edge > 0
              ? "Barely above the majority baseline"
              : "No edge over always guessing the majority class",
          why: `${(d.acc_test * 100).toFixed(1)}% vs ${(d.baseline_test * 100).toFixed(1)}% on ${d.n_test} future rows the model never saw` },
        { what: "One split, one market, no costs",
          why: "non-stationarity and test-set luck are real — treat as exploration, not a system" },
      ])}`;
    linePlot($("#mls-eq"), [
      { points: d.equity_bh.map((v, i) => ({ x: i, y: v })), color: "#5d6470", width: 1.3 },
      { points: d.equity_model.map((v, i) => ({ x: i, y: v })), color: "#f0a826", width: 1.8 },
    ], { height: 160, fmtY: (v) => v.toFixed(2), fmtX: (v) => d.test_index[Math.round(v)] ? d.test_index[Math.round(v)].slice(5) : "" });
  } catch (e) {
    body.innerHTML = `<div class="empty">${e.message}</div>`;
  } finally {
    btn.disabled = false; btn.textContent = "TRAIN ▶";
  }
}

const RENDER = {
  pulse: renderPulse, chart: renderChart, chain: renderChain, payoff: renderPayoff,
  iv: renderIV, volume: renderVolume, news: renderNews, backtest: renderBacktest,
  tape: renderTape, screener: renderScreener, portfolio: renderPortfolio, alerts: renderAlerts,
  datastore: renderDatastore, workspace: renderWorkspace, viz: renderViz,
  mlstudio: renderMLStudio, brief: renderBrief,
};

/* ---------- websocket ---------- */

let ws = null;
function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws/ticks`);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "hello") {
      state.feedClaim = msg.live ? "kite" : "demo";
      state.wsDown = false;
      paintFeed();
      updateStatusbar();
    } else if (msg.type === "ticks") {
      state.tickCount += msg.data.length;
      state.lastTickAt = Date.now();
      paintFeed();
      msg.data.forEach((t) => {
        t._at = new Date(state.lastTickAt);
        const prev = state.tickStore.get(t.symbol);
        state.tickStore.set(t.symbol, t);
        // Uptick / downtick against this symbol's own previous print, which is
        // what a tape shows. Not against the previous close.
        state.tape.unshift({
          at: state.lastTickAt, symbol: t.symbol, ltp: t.ltp,
          dir: prev ? Math.sign(t.ltp - prev.ltp) : 0,
          change_pct: t.change_pct, volume: t.volume,
        });
        if (state.tape.length > state.tapeMax) state.tape.length = state.tapeMax;
        liveUpdatePulse(t);
        if (state.view === "portfolio") paintLiveDelta();
      });
      updateStatusbar();
    } else if (msg.type === "alert") {
      toast(msg.message, "alert");
    }
  };
  ws.onclose = () => {
    state.wsDown = true;
    paintFeed();
    $("#stream-dot").className = "dot down";
    $("#stream-label").textContent = "RECONNECTING";
    setTimeout(connectWS, 2500);
  };
}

// A feed with no tick for this long is not live, whatever it claimed on
// connect. Two minutes is comfortably longer than any real gap in NSE hours
// and far shorter than a session, so a dead token degrades within a screen
// refresh rather than at the closing bell.
const STALE_AFTER_MS = 120_000;

function feedState() {
  if (state.feedClaim === null) return "CONNECTING";
  if (state.feedClaim !== "kite") return "DEMO";
  if (!state.lastTickAt) return "WAITING";
  return (Date.now() - state.lastTickAt) > STALE_AFTER_MS ? "STALE" : "LIVE";
}

const FEED_UI = {
  CONNECTING: { dot: "",      label: "CONNECTING",  cls: "" },
  WAITING:    { dot: "wait",  label: "NO TICKS YET", cls: "warn" },
  LIVE:       { dot: "live",  label: "LIVE TICKS",  cls: "ok" },
  STALE:      { dot: "stale", label: "STALE",       cls: "warn" },
  DEMO:       { dot: "demo",  label: "DEMO FEED",   cls: "warn" },
  DOWN:       { dot: "down",  label: "RECONNECTING", cls: "warn" },
};

function paintFeed() {
  const ui = FEED_UI[state.wsDown ? "DOWN" : feedState()];
  const dot = $("#stream-dot"), lab = $("#stream-label");
  if (dot) dot.className = `dot ${ui.dot}`;
  if (lab) {
    lab.textContent = ui.label;
    lab.className = ui.cls;
    lab.title = state.lastTickAt
      ? `last tick ${fmt.ist(new Date(state.lastTickAt))}`
      : "no tick has arrived on this connection";
  }
  updateStatusbar();
}

/* ---------- data age ----------------------------------------------------- */
/* Three different claims, never conflated:
     AS OF <t>   the source said this was true at t. Real provenance.
     FETCHED <t> we asked at t; the source published no time of its own.
     RENDERED <t> the browser drew it at t and nobody knows how old it is.
   The old stamp() printed the browser clock in every case, so a chain that
   was ninety seconds stale looked exactly as current as one a tick old. */

function ageOf(iso) {
  if (!iso) return null;
  const ms = Date.now() - Date.parse(iso);
  return Number.isFinite(ms) ? ms : null;
}

function ageClass(ms, warn = 30_000, bad = 120_000) {
  if (ms === null) return "";
  return ms > bad ? "down" : ms > warn ? "warn" : "";
}

function ageText(ms) {
  if (ms === null) return "";
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m${String(s % 60).padStart(2, "0")}s` : `${Math.floor(m / 60)}h${m % 60}m`;
}

/* Renders the age badge for a panel. Pass the payload's as_of when it has one. */
function ageStamp(as_of, { fetched = true } = {}) {
  const ms = ageOf(as_of);
  if (ms === null) {
    return `<span class="age" title="the source published no timestamp; this is when the request was made">`
         + `${fetched ? "FETCHED" : "RENDERED"} ${fmt.ist()}</span>`;
  }
  return `<span class="age ${ageClass(ms)}" title="source timestamp ${new Date(as_of).toLocaleString()}">`
       + `AS OF ${fmt.ist(new Date(as_of))} · ${ageText(ms)} OLD</span>`;
}

const TICK_TO_PULSE = { NIFTY: "^NSEI", BANKNIFTY: "^NSEBANK" };
function liveUpdatePulse(t) {
  if (state.view !== "pulse") return;
  // Stricter than the old streamLive check: a feed that claimed kite but has
  // gone quiet must not keep painting either, because the last print it sent
  // stops being a price the moment it stops being current.
  if (feedState() !== "LIVE") return;
  const target = TICK_TO_PULSE[t.symbol] || `${t.symbol}.NS`;
  const row = document.querySelector(`tr[data-symbol="${target}"], tr[data-symbol="${t.symbol}"]`);
  if (!row) return;
  const px = row.querySelector(".px"), chg = row.querySelector(".chg");
  const old = parseFloat(px.textContent.replace(/,/g, ""));
  if (!isNaN(old) && Math.abs(old - t.ltp) > 1e-9) {
    px.textContent = fmt.n(t.ltp);
    chg.textContent = fmt.pct(t.change_pct);
    chg.className = `chg ${cls(t.change_pct)}`;
    row.classList.remove("flash-up", "flash-down");
    void row.offsetWidth;
    row.classList.add(t.ltp >= old ? "flash-up" : "flash-down");
  }
}

/* ---------- statusbar ---------- */

function updateStatusbar() {
  const wsEl = $("#sb-ws");
  if (wsEl) {
    const u = FEED_UI[state.wsDown ? "DOWN" : feedState()];
    wsEl.innerHTML = `WS ${u.cls ? `<span class="${u.cls}">${u.label}</span>` : u.label}`;
  }
  const ticksEl = $("#sb-ticks");
  if (ticksEl) ticksEl.textContent =
    `${state.tickCount.toLocaleString()} TICKS${state.lastTickAt ? ` · LAST ${fmt.ist(state.lastTickAt)}` : ""}`;
  const apiEl = $("#sb-api");
  if (apiEl && apiStats.calls) apiEl.textContent =
    `API ${apiStats.lastMs}ms ${apiStats.lastPath.replace("/api/", "")} · ${apiStats.calls} CALLS`;
}

/* ---------- command palette ---------- */

const PALETTE_HINTS = ["oc NIFTY", "pay NIFTY iron_condor", "iv BANKNIFTY",
  "qnt NIFTY", "swarm NIFTY", "c RELIANCE 1y", "news RELIANCE", "vol SBIN",
  "bt NIFTY momentum", "scr banks rsi<40", "tape", "alert NIFTY > 24000"];

function openPalette() {
  $("#palette-backdrop").hidden = false;
  const inp = $("#palette-input");
  inp.value = "";
  inp.focus();
  $("#palette-hints").innerHTML = PALETTE_HINTS.map((h) =>
    `<span class="badge" onclick="runCommand('${h}');closePalette()">${h}</span>`).join("");
}
function closePalette() { $("#palette-backdrop").hidden = true; }
window.closePalette = closePalette;

function runCommand(line) {
  const tok = line.trim().split(/\s+/);
  if (!tok[0]) return;
  const cmd = tok[0].toLowerCase();
  const sym = tok[1] ? tok[1].toUpperCase() : undefined;
  const route = {
    pulse: () => show("pulse"), p: () => show("pulse"),
    c: () => { if (tok[2]) state.chartPeriod = tok[2]; show("chart", { symbol: sym }); },
    chart: () => show("chart", { symbol: sym }), q: () => show("chart", { symbol: sym }),
    oc: () => show("chain", { symbol: sym }), chain: () => show("chain", { symbol: sym }),
    pay: () => show("payoff", { symbol: sym, strategy: tok[2] }),
    payoff: () => show("payoff", { symbol: sym, strategy: tok[2] }),
    iv: () => show("iv", { symbol: sym }),
    vol: () => show("volume", { symbol: sym }), volume: () => show("volume", { symbol: sym }),
    n: () => show("news", { symbol: sym }), news: () => show("news", { symbol: sym }),
    bt: () => show("backtest", { symbol: sym }), wf: () => show("backtest", { symbol: sym }),
    mc: () => show("backtest", { symbol: sym }),
    qnt: () => show("viz", { symbol: sym }), viz: () => show("viz", { symbol: sym }),
    ml: () => show("mlstudio", { symbol: sym }), mls: () => show("mlstudio", { symbol: sym }),
    brf: () => show("brief", { symbol: sym }), brief: () => show("brief", { symbol: sym }),
    surface: () => show("viz", { symbol: sym, tab: "surface" }),
    greeks: () => show("viz", { symbol: sym, tab: "greeks" }),
    swarm: () => show("viz", { symbol: sym, tab: "swarm" }),
    tape: () => show("tape"), live: () => show("tape"),
    wsp: () => show("workspace"), work: () => show("workspace"), workspace: () => show("workspace"),
    scr: () => show("screener"), screen: () => show("screener"),
    port: () => show("portfolio"), portfolio: () => show("portfolio"),
    alerts: () => show("alerts"),
    alert: async () => {
      try {
        const res = await postJSON("/api/alerts", { rule: tok.slice(1).join(" ") });
        toast(`ARMED: ${res.text}`, "ok");
      } catch (e) { toast(e.message, "err"); }
    },
  };
  if (route[cmd]) route[cmd]();
  else toast(`Unknown command "${cmd}" — try oc, pay, iv, c, news, bt, tape`, "err");
}
window.runCommand = runCommand;

/* ---------- status / clock / window controls ---------- */

async function refreshStatus() {
  try {
    const s = await getJSON("/api/status");
    $("#chip-session").innerHTML =
      `NSE <b class="${s.session.open ? "ok" : ""}">${s.session.phase.replace(/_/g, " ").toUpperCase()}</b>`;
    const chip = $("#chip-broker");
    if (!s.broker) {
      // Was a dead chip that told you to go and use the CLI. A terminal that
      // cannot be connected from inside itself is not self-contained.
      chip.innerHTML = s.offline ? `<b class="warn">OFFLINE MODE</b>`
        : `BROKER <b class="warn">NONE · CONNECT</b>`;
      chip.title = s.offline ? "" : "Add your Kite api_key and api_secret";
      chip.style.cursor = s.offline ? "" : "pointer";
      chip.onclick = s.offline ? null : brokerSetup;
    } else if (s.broker_healthy === false) {
      // configured is not healthy — say so, and make the fix one click
      chip.innerHTML = `BROKER <b class="warn">${s.broker.toUpperCase()} ⚠ RECONNECT</b>`;
      chip.title = s.broker_reason || "REST probe failed";
      chip.style.cursor = "pointer";
      chip.onclick = reconnectBroker;
    } else {
      chip.innerHTML = `BROKER <b class="ok">${s.broker.toUpperCase()}</b>`;
      chip.title = ""; chip.onclick = null; chip.style.cursor = "";
    }
    // Capture health in the status bar. This archive is the only asset that
    // compounds and it used to fail silently every morning until someone
    // logged in, so a stalled capture has to be visible without being asked for.
    const cap = s.capture || {};
    const capEl = $("#sb-capture");
    if (capEl) {
      const failing = (cap.failed || 0) > 0 && !cap.last_ok;
      const stale = cap.last_ok && (Date.now() - Date.parse(cap.last_ok)) > 300000;
      capEl.innerHTML = !s.session.open
        ? `<span class="faint">CAPTURE IDLE</span>`
        : failing ? `<span class="down">CAPTURE FAILING</span>`
        : stale ? `<span class="warn">CAPTURE STALE</span>`
        : `CAPTURE <span class="ok">${(cap.ok || 0).toLocaleString()}</span>`;
      capEl.title = cap.last_error
        ? `last error: ${cap.last_error}`
        : cap.last_ok ? `last capture ${cap.last_ok} from ${cap.last_source || "?"}`
        : "no capture yet on this run";
    }
    $("#sb-version").textContent = `SHUNKAN v${s.version}`;
  } catch {}
}

function brokerSetup() {
  // One-time credential entry. The secret goes to localhost and is written
  // 0600; it is never returned by the API, so this form is the only place it
  // ever exists in the browser and it is cleared as soon as it is sent.
  const back = elv("div", "palette-backdrop");
  back.innerHTML = `
    <div class="setup">
      <div class="setup-head">CONNECT ZERODHA</div>
      <p>One time. Create a <b>Connect</b> app at
        <a href="https://developers.kite.trade" target="_blank" rel="noopener">developers.kite.trade</a>
        and set its redirect URL to exactly
        <code>http://127.0.0.1:8722/callback</code>.</p>
      <label>API KEY<input id="bs-key" autocomplete="off" spellcheck="false"></label>
      <label>API SECRET<input id="bs-secret" type="password" autocomplete="off" spellcheck="false"></label>
      <p class="setup-note">Stored on this machine at <code>~/.shunkan/credentials.json</code>,
        owner-only. Never sent anywhere except Kite. You type your Zerodha password
        on Zerodha's own page, never here.</p>
      <div class="setup-actions">
        <button class="tbtn" id="bs-cancel">CANCEL</button>
        <button class="tbtn" id="bs-save">SAVE &amp; LOG IN</button>
      </div>
    </div>`;
  document.body.appendChild(back);
  const close = () => back.remove();
  $("#bs-cancel").onclick = close;
  back.onclick = (e) => { if (e.target === back) close(); };
  $("#bs-key").focus();

  const save = async () => {
    const api_key = $("#bs-key").value.trim();
    const api_secret = $("#bs-secret").value;
    try {
      await postJSON("/api/broker/setup", { api_key, api_secret });
      $("#bs-secret").value = "";        // out of the DOM immediately
      close();
      toast("Credentials saved — opening Zerodha login", "ok");
      reconnectBroker();
    } catch (e) { toast(e.message, "err"); }
  };
  $("#bs-save").onclick = save;
  back.onkeydown = (e) => {
    if (e.key === "Escape") close();
    if (e.key === "Enter") save();
  };
}

async function reconnectBroker() {
  try {
    const r = await postJSON("/api/broker/reconnect");
    window.open(r.login_url, "_blank");
    toast("Complete the Zerodha login in the new tab — the terminal captures the token itself", "alert");
    const t0 = Date.now();
    const poll = async () => {
      if (Date.now() - t0 > 5 * 60 * 1000) return;
      try {
        const s = await getJSON("/api/broker/status");
        if (s.healthy) { toast("Kite reconnected — live REST restored", "ok"); refreshStatus(); return; }
      } catch {}
      setTimeout(poll, 3000);
    };
    setTimeout(poll, 4000);
  } catch (e) { toast(`Reconnect: ${e.message}`, "err"); }
}

function tickClock() { $("#clock").textContent = fmt.ist() + " IST"; }

function toggleFullscreen() {
  if (document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen();
}

function popOut() {
  // Standalone app-like window: no tabs/toolbar. For a permanent install,
  // use the browser's "Install app" — manifest display=standalone.
  window.open(location.origin, "shunkan-terminal",
    "popup=yes,width=1480,height=940,left=80,top=60");
}

/* ---------- boot ---------- */

buildRail();
connectWS();
refreshStatus();
setInterval(refreshStatus, 30000);
tickClock();
setInterval(tickClock, 1000);
updateStatusbar();

$("#cmd-open").onclick = openPalette;
$("#btn-fullscreen").onclick = toggleFullscreen;
$("#btn-popout").onclick = popOut;
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    $("#palette-backdrop").hidden ? openPalette() : closePalette();
  } else if (e.key === "Escape") closePalette();
});
$("#palette-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { runCommand(e.target.value); closePalette(); }
});
$("#palette-backdrop").addEventListener("click", (e) => {
  if (e.target.id === "palette-backdrop") closePalette();
});

// Deep link on cold start. No hash still means Pulse: restoring a "last view"
// from storage would change cold-start behaviour in a way that reads as a bug
// the first time it surprises you.
(() => {
  const r = parseHash();
  if (r) {
    _routing = true;
    try { show(r.viewId, r.symbol ? { symbol: r.symbol } : {}); }
    finally { _routing = false; syncHash(); }
  } else {
    show("pulse");
  }
})();
