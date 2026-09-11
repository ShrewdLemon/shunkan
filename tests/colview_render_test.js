/* Renders the SHIPPED column-view cells out of app.js against payloads shaped
   like the real API responses — including one where every value is null.

   Two house rules live or die in the cell functions, and both are invisible to
   a green Python suite:

     * a column the user did not choose is NOT BUILT (no display:none, no
       spare <td>), because a rendered-then-hidden cell is a number the screen
       denies while the DOM holds it;
     * a value that is missing renders "—", never a blank <td>. A blank cell
       reads as a rendering fault and gets "fixed" into something plausible,
       which is this repo's worst failure mode. The insider table shipped
       "0% -> 0%" and HDFC Bank's related-party book shipped as "0 Cr" for
       exactly this reason.

   The functions are lifted from app.js rather than re-typed, so editing them
   breaks this test. */
const fs = require("fs");

const src = fs.readFileSync("src/shunkan/server/static/app.js", "utf8");
// Anchored at column 0: the doc comment above the block NAMES both markers,
// and matching those mentions would slice out the comment and nothing else -
// a test that then asserts happily about an empty string.
const start = src.indexOf("\nconst COLVIEW_LIFT_START = true;");
const end = src.indexOf("\nconst COLVIEW_LIFT_END = true;");
if (start < 0 || end < 0 || end < start) {
  console.error("FAIL: could not locate the COLVIEW lift markers in app.js");
  process.exit(1);
}
const block = src.slice(start, end);
if (!block.includes("const TABLE_COLS = {") || !block.includes("function colTable")) {
  console.error("FAIL: the lifted block is missing TABLE_COLS or colTable");
  process.exit(1);
}

/* ---- the stubs the block needs. Only these identifiers exist inside. ---- */
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const cls = (v) => (v == null || isNaN(v) ? "" : v >= 0 ? "up" : "down");
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
};
const elv = () => ({}), toast = () => {}, show = () => {};
const getJSON = async () => ({ tables: [] }), postJSON = async () => ({});
const doc = { querySelectorAll: () => [], addEventListener: () => {}, removeEventListener: () => {} };

const M = new Function(
  "esc", "cls", "fmt", "elv", "toast", "show", "getJSON", "postJSON", "document",
  block + "\nreturn {TABLE_COLS,COLVIEW,colVisible,colHead,colRows,colTable,"
        + "cvBar,cvSMA,cvRs,cvNoteHtml,cvBtnLabel,cvMenuHtml,cvActsHtml};"
)(esc, cls, fmt, elv, toast, show, getJSON, postJSON, doc);

let bad = 0;
const fail = (m, d) => { bad++; console.error("FAIL:", m, d === undefined ? "" : d); };
const ok = (c, m, d) => { if (!c) fail(m, d); };

const tds = (html) => html.match(/<td\b[\s\S]*?<\/td>/g) || [];
const ths = (html) => html.match(/<th\b[\s\S]*?<\/th>/g) || [];
const ENT = { "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'" };
const text = (html) => html.replace(/<[^>]*>/g, "")
  .replace(/&(amp|lt|gt|quot|#39);/g, (m) => ENT[m]).trim();

/* ---- fixtures, one full and one all-null per table ---------------------- */
const CHAIN_ROW = {
  strike: 24500, atm: true,
  call: { ltp: 132.4, oi: 8.1e6, oi_change: -142000, volume: 91234, iv: 0.1421, bid: 131.9 },
  put: { ltp: 88.05, oi: 5.4e6, oi_change: 310000, volume: 44100, iv: 0.1502, ask: 88.4 },
};
const CHAIN_NULL = {
  strike: 24500, atm: false,
  call: { ltp: null, oi: null, oi_change: null, volume: null, iv: null, bid: null },
  put: { ltp: null, oi: null, oi_change: null, volume: null, iv: null, ask: null },
};
const FIXTURES = {
  chain: {
    ctx: { modelled: false, maxC: 8.1e6, maxP: 5.4e6 },
    full: CHAIN_ROW,
    empty: CHAIN_NULL,
  },
  screener: {
    ctx: {},
    full: { symbol: "RELIANCE", price: 1402.5, ret_1d: 0.004, ret_1w: -0.012,
            ret_1mo: 0.031, ret_3mo: 0.08, rsi: 58.4, vol_ann: 0.244,
            vol_surge: 1.8, from_high: -0.06, above_sma50: true, above_sma200: false },
    empty: { symbol: "NEWLISTING", price: null, ret_1d: null, ret_1w: null,
             ret_1mo: null, ret_3mo: null, rsi: null, vol_ann: null,
             vol_surge: null, from_high: null, above_sma50: null, above_sma200: null },
  },
  positions: {
    ctx: {},
    full: { symbol: "NIFTY24500CE", label: "NIFTY 24500 CE", kind: "CE",
            expiry: "2026-09-24", strike: 24500, lot_size: 75, quantity: -150,
            is_short: true, avg_cost: 130.2, last: 118.4, market_value: -17760,
            unrealized: 1770, expired: false, settleable: false },
    empty: { symbol: "MYSTERY", label: null, kind: null, expiry: null, strike: null,
             lot_size: null, quantity: null, is_short: false, avg_cost: null,
             last: null, market_value: null, unrealized: null,
             expired: false, settleable: false },
  },
  insider: {
    ctx: {},
    full: { date: "12-Aug-2026 00:00:00", name: "N Kapoor", category: "Promoter",
            type: "Sell", mode: "Market Sale", security: "Equity Shares",
            qty: 2500, value: 3510000, shares_before: 3920, shares_after: 1420,
            pct_before: 0.02, pct_after: 0.01 },
    empty: { date: null, name: null, category: null, type: null, mode: null,
             security: null, qty: null, value: null, shares_before: null,
             shares_after: null, pct_before: null, pct_after: null },
  },
  fund_holdings: {
    ctx: {},
    full: { holding: "Reliance Industries Ltd", symbol: "RELIANCE", sector: "Energy",
            market_value_cr: 4102.6, weight_pct: 8.41, change_1m_pct: -0.32,
            as_of: "2026-08-31" },
    empty: { holding: "Unlisted Placement", symbol: null, sector: null,
             market_value_cr: null, weight_pct: null, change_1m_pct: null, as_of: null },
  },
  stock_holders: {
    ctx: {},
    full: { isin: "INF123", scheme: "Bluechip Fund", amc: "HDFC AMC",
            category: "Large Cap", weight_pct: 6.2, value_cr: 812.4, change_1m_pct: 0.4 },
    empty: { isin: "INF999", scheme: "Newly Filed Scheme", amc: null, category: null,
             weight_pct: null, value_cr: null, change_1m_pct: null },
  },
};

/* ---- 1. every cell of every column, over both fixtures ------------------ */
for (const [table, spec] of Object.entries(M.TABLE_COLS)) {
  const f = FIXTURES[table];
  ok(f, `no fixture for table ${table} — a new table needs one here`);
  if (!f) continue;
  for (const which of ["full", "empty"]) {
    for (const c of spec.cols) {
      const html = c.cell(f[which], f.ctx);
      ok(typeof html === "string" && /^<td\b/.test(html) && /<\/td>$/.test(html),
        `${table}.${c.id} (${which}) is not a single <td>`, html);
      for (const junk of ["NaN", "undefined", "Infinity", "[object ", "null%"]) {
        ok(!html.includes(junk), `${table}.${c.id} (${which}) rendered ${junk}`, html);
      }
      // The rule this file exists for: no blank cell, ever.
      ok(text(html).length > 0,
        `${table}.${c.id} (${which}) rendered an EMPTY cell — a missing value must be a dash`,
        JSON.stringify(html));
    }
  }
}

/* ---- 2. the shipped default set is EXACTLY today's table ---------------- */
/* Written out as literals on purpose: this is the "nothing regresses for
   someone who never opens the picker" guarantee, and it can only be checked
   against what the table looked like before saved views existed. */
const SHIPPED = {
  chain: ["C·OI", "C·ΔOI", "C·VOL", "C·IV", "C·BID", "C·LTP", "STRIKE",
          "P·LTP", "P·ASK", "P·IV", "P·VOL", "P·ΔOI", "P·OI"],
  screener: ["SYMBOL", "PRICE", "1W", "1M", "3M", "RSI", "VOL ANN", "OFF HIGH",
             "SMA50", "SMA200"],
  positions: ["CONTRACT", "QTY", "AVG", "LAST", "VALUE", "P&L"],
  insider: ["DATE", "PERSON", "RELATION", "DEAL", "QTY", "VALUE",
            "SHARES BEFORE→AFTER", "STAKE"],
  fund_holdings: ["HOLDING", "SYMBOL", "SECTOR", "₹Cr", "WEIGHT", "Δ1M"],
  stock_holders: ["SCHEME", "AMC", "CATEGORY", "WEIGHT", "₹Cr", "Δ1M"],
};
for (const [table, want] of Object.entries(SHIPPED)) {
  M.COLVIEW.store = {};                       // nothing saved: the built-in set
  const got = ths(M.colHead(table)).map(text);
  ok(JSON.stringify(got) === JSON.stringify(want),
    `${table}: the default columns are no longer the ones that shipped`,
    `got ${JSON.stringify(got)} want ${JSON.stringify(want)}`);
}

/* ---- 3. an unchosen column is NOT BUILT --------------------------------- */
M.COLVIEW.store = { chain: { working: ["strike", "call_ltp", "put_ltp"] } };
const narrow = M.colTable("chain", [CHAIN_ROW], { ctx: FIXTURES.chain.ctx });
ok(tds(narrow).length === 3, "narrowed chain row did not drop its <td>s", tds(narrow).length);
ok(ths(narrow).length === 3, "narrowed chain head did not drop its <th>s", ths(narrow).length);
ok(!narrow.includes("oi-bar"),
  "the OI column was still BUILT after being unticked — hiding is not dropping");
ok(!/display\s*:\s*none/i.test(narrow), "a column was hidden with CSS instead of dropped");
ok(narrow.includes("ltp-cell"), "the columns that ARE chosen must still render");

/* ---- 4. the identity column cannot be dropped --------------------------- */
M.COLVIEW.store = { chain: { working: ["call_oi", "put_oi"] } };
ok(M.colVisible("chain").some((c) => c.id === "strike"),
  "STRIKE was dropped — the numbers then belong to no strike at all");

/* ---- 5. order is the table's, not the click order ----------------------- */
M.COLVIEW.store = { screener: { working: ["above_sma200", "rsi", "symbol", "price"] } };
ok(JSON.stringify(M.colVisible("screener").map((c) => c.id))
   === JSON.stringify(["symbol", "price", "rsi", "above_sma200"]),
  "columns rendered in click order instead of registry order",
  JSON.stringify(M.colVisible("screener").map((c) => c.id)));

/* ---- 6. header and body always agree on width --------------------------- */
for (const [table, spec] of Object.entries(M.TABLE_COLS)) {
  const f = FIXTURES[table];
  if (!f) continue;
  M.COLVIEW.store = {};
  const html = M.colTable(table, [f.full, f.empty], { ctx: f.ctx });
  const nTh = ths(html).length;
  const rows = html.split("<tr").slice(2);          // drop "" and the header row
  for (const r of rows) {
    ok(tds(r).length === nTh,
      `${table}: a row has ${tds(r).length} cells under ${nTh} headers`);
  }
  ok(nTh === spec.def.length, `${table}: header width != default column count`);
}

/* ---- 7. the OI bar width is never NaN, at any magnitude ----------------- */
for (const v of [null, undefined, 0, 1, 1e3, 7.7e9, -5]) {
  for (const max of [0, 1, 1e6]) {
    const w = M.cvBar(v, max);
    ok(/^-?\d+(\.\d+)?%$/.test(w), `cvBar(${v}, ${max}) is not a plain percentage`, w);
  }
}

/* ---- 8. a missing SMA flag is not "below" ------------------------------- */
ok(text(M.cvSMA(null)) === "—", "a null SMA flag rendered as a claim", M.cvSMA(null));
ok(text(M.cvSMA(false)) === "below", "false must still say below", M.cvSMA(false));
ok(text(M.cvSMA(true)) === "ABOVE", "true must still say ABOVE", M.cvSMA(true));

/* ---- 9. a rupee sign is never left in front of nothing ------------------ */
ok(M.cvRs(null) === "—", "cvRs(null) printed a currency symbol with no amount", M.cvRs(null));
ok(M.cvRs(0) === "₹0", "cvRs(0) must still print a real zero", M.cvRs(0));

/* ---- 10. the picker never offers a control that cannot succeed ---------- */
M.COLVIEW.store = { screener: { active: null, views: {}, working: ["symbol"] } };
const acts = M.cvActsHtml("screener");
ok(!acts.includes("cv-save\"") && !acts.includes("cv-del"),
  "SAVE/DELETE offered while the built-in view is selected — neither can succeed", acts);
ok(acts.includes("cv-saveas"), "SAVE AS must always be available");
M.COLVIEW.store = { screener: { active: "mine", views: { mine: ["symbol"] }, working: ["symbol"] } };
const acts2 = M.cvActsHtml("screener");
ok(acts2.includes("cv-save") && acts2.includes("cv-del"),
  "SAVE/DELETE missing while a named view is selected", acts2);

/* ---- 11. an unsaved change says so, naming where a reload lands --------- */
M.COLVIEW.store = { screener: { active: "mine", views: { mine: ["symbol"] },
                                working: ["symbol", "rsi"], dirty: true } };
const note = M.cvNoteHtml("screener");
ok(note.includes("unsaved") && note.includes("mine"),
  "a dirty picker must say it is unsaved AND where a reload returns to", note);

/* ---- 12. a stale saved column is named in the picker -------------------- */
M.COLVIEW.store = { screener: { active: "old", views: { old: ["symbol"] },
                                stale: { old: ["beta_5y"] }, working: ["symbol"] } };
ok(M.cvNoteHtml("screener").includes("beta_5y"),
  "a column an upgrade removed vanished from the picker without being named");

console.log(`${bad} failed`);
process.exit(bad ? 1 : 0);
