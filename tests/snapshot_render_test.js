/* Renders the SHIPPED company snapshot over payload shapes that actually
   occur, including the one that breaks summary screens: a company with almost
   nothing filed.

   A snapshot is the most dangerous screen in a terminal, because it is the one
   people read WITHOUT scrolling to the provenance underneath. Anything it
   shows is taken as fact at a glance. So the bar here is not "does it render"
   - it is that no gap may ever become a number, and every gap must say why.

   The functions are lifted out of app.js rather than re-typed, so editing them
   breaks this test. */
const fs = require("fs");

const src = fs.readFileSync("src/shunkan/server/static/app.js", "utf8");
const lines = src.split("\n");

function slice(startNeedle, endNeedle) {
  const a = src.indexOf(startNeedle);
  const b = src.indexOf(endNeedle, a);
  if (a < 0 || b < 0) {
    console.error(`FAIL: could not locate ${startNeedle} .. ${endNeedle}`);
    process.exit(1);
  }
  return src.slice(a, b);
}

const fmtBlock = slice("const fmt = {", "\n};") + "\n};";
const stripStart = src.indexOf("function metricsStrip(pairs) {");
const stripBlock = src.slice(stripStart, src.indexOf("\n}\n", stripStart) + 2);
const snapBlock = slice("function snapNum(v) {", "async function renderCompany");

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const cls = (v) => (v >= 0 ? "up" : "down");

const M = new Function("esc", "cls", "$", "getJSON",
  fmtBlock + "\n" + stripBlock + "\n" + snapBlock +
  "\nreturn {companySnapshot, snapNum, snapCr, snapFetched};"
)(esc, cls, () => null, async () => ({}));

// Fixed clock: companySnapshot takes `now` precisely so this is deterministic.
const NOW = new Date("2026-09-11T04:00:00Z");

let bad = 0;
const fail = (m, d) => { bad++; console.error("FAIL:", m, d === undefined ? "" : d); };
const ok = (c, m, d) => { if (!c) fail(m, d); };
const text = (h) => h.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();

/* ---- fixtures ---------------------------------------------------------- */

const RICH = {
  fetched_at: "2026-09-11T03:00:00Z",
  profile: {
    name: "Rich Industries Limited", hq: "Mumbai, India", employees: 404501,
    market_cap: 17017080000000, trailing_pe: 22.8, price_to_book: 1.9,
    dividend_yield_pct: 0.47, beta: 0.15,
    "52w_low": 1249.8, "52w_high": 1611.8,
    yahoo_sector: "Energy", yahoo_industry: "Refining",
    website: "https://example.com", source: "Yahoo Finance",
  },
  ownership: {
    promoter_pct: 50.5, public_pct: 49.5, inst_domestic_pct: 21.2,
    inst_foreign_pct: 17.2, as_of: "2026-06-30",
    source: "NSE shareholding pattern XBRL",
  },
  financials: {
    annual: {
      revenue: { 2024: 9010640000000, 2025: 9646930000000, 2026: 10572190000000 },
      net_income: { 2024: 696210000000, 2025: 696480000000, 2026: 807750000000 },
    },
    net_margin_pct: { 2026: 7.6 }, source: "Yahoo Finance annual statements",
  },
  pledge: { pledged_shares: 0, pledged_pct: 0 },
  board_meetings: [{ date: "2026-10-14", is_results: true, purpose: "Q2 results" }],
};

// A company that listed weeks ago: no annual report, no filing history, no
// extraction. EMMVEE and PWL are the real examples.
const SPARSE = {
  fetched_at: "2026-09-11T03:00:00Z",
  profile: {
    name: "Just Listed Limited", hq: "Bengaluru, India", employees: null,
    market_cap: 239550000000, trailing_pe: 18.8, price_to_book: 6.5,
    dividend_yield_pct: null, beta: null,
    "52w_low": 171.51, "52w_high": 371.55,
    yahoo_sector: "Technology", yahoo_industry: "Solar",
    website: "https://example.com", source: "Yahoo Finance",
  },
  ownership: { promoter_pct: 80.0, inst_domestic_pct: 12.7, as_of: null,
               source: "NSE shareholding pattern XBRL" },
  financials: {},
  pledge: null,
  board_meetings: [],
};

const EMPTY = {};

const ESTIMATED = JSON.parse(JSON.stringify(RICH));
ESTIMATED.ownership.source = "Yahoo Finance estimate (exchange filing unavailable)";
ESTIMATED.ownership.label_note = "NSE filing unreadable: HTTP 404";

/* ---- it must render at all, for every shape ---------------------------- */

for (const [name, fx] of Object.entries({ RICH, SPARSE, EMPTY, ESTIMATED })) {
  let html;
  try {
    html = M.companySnapshot(fx, "TEST", NOW);
  } catch (e) {
    fail(`${name} threw`, `${e.constructor.name}: ${e.message}`);
    continue;
  }
  ok(typeof html === "string" && html.length > 100, `${name} produced nothing`);
  for (const junk of ["NaN", "undefined", "Infinity", "[object ", ">null<"]) {
    const at = html.indexOf(junk);
    ok(at < 0, `${name} contains ${junk}`,
       at < 0 ? "" : html.slice(Math.max(0, at - 70), at + 30));
  }
  ok((html.match(/<div/g) || []).length === (html.match(/<\/div>/g) || []).length,
     `${name} has unbalanced <div>`);
}

/* ---- THE CENTRAL RULE: a gap is never a number ------------------------- */
{
  const html = M.companySnapshot(SPARSE, "TEST", NOW);
  const t = text(html);
  // NO ESCAPE HATCH. An earlier version of this wrote
  //   !/PLEDGED\s*0(?!\s*sh)/
  // whose negative lookahead exempted "PLEDGED 0 shares" - which is EXACTLY
  // the bug being tested for. A company with no encumbrance filing rendering
  // as "0 shares pledged" would have sailed through green. Any leading zero
  // after these labels is now a failure, in every form.
  for (const label of ["DIV YLD", "BETA", "PLEDGED"]) {
    const m = t.match(new RegExp(label + "\\s+(\\S+)"));
    ok(m, `${label} is not on the snapshot at all`);
    if (!m) continue;
    ok(!/^-?0(\.0+)?$/.test(m[1]) && !/^0/.test(m[1]),
       `${label} rendered as a zero for a company that filed nothing — ` +
       `that is a claim, not an absence`, m[1]);
    ok(m[1] === "—", `${label} should dash when absent`, m[1]);
  }

  // THE OTHER HALF OF THE THESIS: a dash is only honest if it says why.
  // Nothing previously asserted the reasons existed, so gutting snapCell's
  // refusal branch stayed green while the whole NOT AVAILABLE block vanished.
  ok(/NOT AVAILABLE/.test(t),
     "gaps are dashed but no block states their causes");
  for (const label of ["DIV YLD", "BETA", "PLEDGED"]) {
    const re = new RegExp(label + ":\\s*([^·]{12,})");
    const m = t.match(re);
    ok(m, `${label} dashes with no stated reason`, t.slice(t.indexOf("NOT AVAILABLE"), 200));
  }
  // the pledge reason must distinguish "not filed" from "zero"
  ok(/not the same as zero/i.test(t),
     "the pledge gap does not say that no filing differs from zero pledged");
}

/* An empty payload is the strongest form of the same test. */
{
  const html = M.companySnapshot(EMPTY, "TEST", NOW);
  const t = text(html);
  ok(!/\b0\.00\b/.test(t), "an empty payload produced a 0.00 somewhere", t.slice(0, 200));
  ok(!/₹0\b/.test(t), "an empty payload produced ₹0", t.slice(0, 200));
}

/* ---- an estimate must not look like a filing --------------------------- */
{
  const filed = M.companySnapshot(RICH, "TEST", NOW);
  const est = M.companySnapshot(ESTIMATED, "TEST", NOW);
  ok(est !== filed,
     "a Yahoo estimate renders identically to a read exchange filing");
  ok(/≈/.test(est), "the estimate carries no visible mark");
  ok(!/≈/.test(filed), "a real filing was marked as an estimate");
}

/* ---- the numbers that ARE present must be right ------------------------ */
{
  const t = text(M.companySnapshot(RICH, "TEST", NOW));
  ok(/50\.5/.test(t), "promoter % missing from a payload that has it");
  ok(/22\.8/.test(t), "P/E missing from a payload that has it");
  ok(/0\.15/.test(t), "beta missing — it is fetched by the API and was never rendered before");
  ok(/17,01,708/.test(t), "market cap not in Indian digit grouping", t.slice(0, 160));
}

/* ---- provenance: the snapshot must carry its own sources --------------- */
{
  const html = M.companySnapshot(RICH, "TEST", NOW);
  ok(/title="/.test(html),
     "no hover provenance anywhere — a glance screen must still say where it got each figure");
  ok(/2026-06-30/.test(html) || /as of/i.test(html),
     "the ownership as-of date is not surfaced");
}

/* ---- the crore formatter, whose comment claims it prevents a real bug ----
   snapCr states it carries netCr's discipline: no real amount may ever print
   as "0". HDFC Bank's entire related-party book once rendered as zeroes
   because a formatter was fixed at zero decimal places, and MCAP and REVENUE
   are the same kind of number. A comment that claims a fix must be tested, or
   it is just a claim. */
{
  ok(M.snapCr(0) === "0", "a true zero must still print as 0");
  // 56 magnitudes: nothing non-zero may collapse to "0"
  for (let e = 0; e < 14; e++) {
    for (const mul of [1, 1.5, 3, 7.7]) {
      const v = mul * 10 ** e;
      const out = M.snapCr(v);
      ok(out !== "0" && out !== "-0",
         `snapCr(${v}) printed a real amount as zero`, String(out));
      ok(M.snapCr(-v) !== "0", `snapCr(${-v}) printed a real amount as zero`);
    }
  }
  // and the display floor is a statement about the DISPLAY, not the value
  ok(String(M.snapCr(1000)).includes("<0.01"),
     "a sub-lakh amount needs an explicit display floor, not a rounded 0",
     String(M.snapCr(1000)));
}

/* ---- the fetch stamp, which is a freshness CLAIM ------------------------
   snapFetched was lifted and exported by this file and then never asserted.
   Its whole job is to refuse to date a payload it cannot date: /api/company
   is served from a 6-hour disk cache, so falling back to the browser clock
   would stamp six-hour-old numbers as current. */
{
  const dated = M.snapFetched("2026-09-11T03:00:00Z", NOW);
  ok(/fetched/i.test(dated.text), "a dated payload does not say when", dated.text);
  ok(/2026-09-11/.test(dated.text), "the fetch date is not shown", dated.text);

  for (const missing of [null, undefined, "", "not-a-date"]) {
    const r = M.snapFetched(missing, NOW);
    ok(/not recorded/i.test(r.text),
       `snapFetched(${JSON.stringify(missing)}) invented a fetch time`, r.text);
    // the browser clock must not appear as if it were the fetch time
    ok(!/2026-09-11 04:0/.test(r.text),
       "the browser clock was stamped as the fetch time", r.text);
  }
}

/* ---- NEXT RESULTS is a future date, or it is not a next anything -------- */
{
  const past = {
    profile: { name: "X", source: "Y" }, ownership: {}, financials: {},
    pledge: null,
    board_meetings: [{ date: "2026-01-10", is_results: true, purpose: "Q3" }],
  };
  const t = text(M.companySnapshot(past, "TEST", NOW));
  ok(!/NEXT RESULTS\s+2026-01-10/.test(t),
     "a board meeting that already happened was printed as NEXT RESULTS", t.slice(0, 200));

  const future = JSON.parse(JSON.stringify(past));
  future.board_meetings = [{ date: "2026-01-10", is_results: true },
                           { date: "2026-10-14", is_results: true }];
  const t2 = text(M.companySnapshot(future, "TEST", NOW));
  ok(/2026-10-14/.test(t2),
     "the next future results date was not found past an older one", t2.slice(0, 220));
}

/* ---- helpers ----------------------------------------------------------- */
{
  ok(M.snapNum(null) === null, "snapNum turned null into a number");
  ok(M.snapNum(undefined) === null, "snapNum turned undefined into a number");
  ok(M.snapNum(0) === 0, "snapNum discarded a real zero");
  ok(M.snapCr(null) === null, "snapCr turned null into a number");
}

console.log(bad ? `${bad} FAILURES` : "snapshot render: all checks passed");
process.exit(bad ? 1 : 0);
