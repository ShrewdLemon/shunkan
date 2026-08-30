/* Renders the SHIPPED entity-network functions out of app.js against payloads
   shaped like real API responses.

   Two of this repo's worst bugs were invisible to a green suite and obvious on
   screen: GOLD listed as a Reliance product, and insider dealing reading
   "0% -> 0%". This file exists for the third of that kind - HDFC Bank's entire
   related-party book, every figure real and every one displayed as "0 Cr",
   because the crore formatter was fixed at zero decimal places.

   The functions are lifted from app.js rather than re-typed, so editing them
   breaks this test. */
const fs = require("fs");

const src = fs.readFileSync("src/shunkan/server/static/app.js", "utf8");
const start = src.indexOf("const NET = { d: null");
const end = src.indexOf("const RENDER = {");
if (start < 0 || end < 0 || end < start) {
  console.error("FAIL: could not locate the NET block in app.js");
  process.exit(1);
}
const block = src.slice(start, end);

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const secBlock = (t, c, b) => `<details><summary>${t}</summary>${b}</details>`;
const panel = ({ body }) => body, loading = (s) => s, stamp = () => "", $ = () => null;
const state = { symbol: "TEST" }, show = () => {}, getJSON = async () => ({});
const win = {}, doc = { body: {} };
const M = new Function(
  "esc", "secBlock", "panel", "loading", "stamp", "$", "state", "show",
  "getJSON", "window", "document",
  block + "\nreturn {netSankey,netPeriodBars,netSpark,netTable,netStructure," +
          "netChain,netOwners,netCr,NET,NET_PEOPLE,NET_EXPAND,NET_TOP};"
)(esc, secBlock, panel, loading, stamp, $, state, show, getJSON, win, doc);

let bad = 0;
const fail = (m, d) => { bad++; console.error("FAIL:", m, d === undefined ? "" : d); };
const ok = (c, m, d) => { if (!c) fail(m, d); };

/* ---- the formatter, which is where the money bug lived ---- */
[[null, "—"], [0, "0"], [50000, "<0.01"], [240000, "0.02"], [26500000, "2.65"],
 [123456789, "12.3"], [6170859550000, "6,17,086"]].forEach(([v, want]) => {
  const got = M.netCr(v);
  ok(got === want, `netCr(${v})`, `got "${got}" want "${want}"`);
});
// the invariant, not just the examples: no non-zero value may render as "0"
for (let e = 0; e < 14; e++) {
  for (const m of [1, 1.5, 3, 7.7]) {
    const v = m * 10 ** e;
    const s = M.netCr(v);
    ok(s !== "0" && s !== "-0", `netCr(${v}) rendered a real amount as zero`, s);
  }
}

/* ---- payload fixtures, each shaped like /api/entity/{sym} ---- */
const PER = ["Mar 2022", "Sep 2022", "Mar 2023", "Sep 2023", "Mar 2024", "Sep 2024"];
const cp = (id, name, periods) => ({
  id, name, periods,
  total: Object.values(periods).reduce((a, b) => a + b, 0),
});
const base = (over) => Object.assign({
  symbol: "TEST", node: "company:TEST", name: "Test Industries Limited",
  trade: { sells_to: [], buys_from: [] }, periods: PER,
  structure: {}, structure_counts: {},
  disclosed: {}, owners: [], schemes: [],
  sources: { trade: "t", structure: "s", disclosed: "d", owners: "o" },
}, over);

const FIX = {
  // a full book, every period filed
  rich: base({
    trade: {
      sells_to: Array.from({ length: 22 }, (_, i) =>
        cp(`company:C${i}`, `Customer Number ${i} Private Limited`,
           Object.fromEntries(PER.map((p, j) => [p, (22 - i) * 1e11 + j * 1e9])))),
      buys_from: Array.from({ length: 15 }, (_, i) =>
        cp(`company:S${i}`, `Supplier ${i} Limited`,
           Object.fromEntries(PER.map((p) => [p, (15 - i) * 5e10])))),
    },
    structure: {
      subsidiary_of: [{ id: "company:SUB", name: "Sub Ltd", rel: "subsidiary_of", source: "BSE" }],
      key_management_of: [{ id: "person:X", name: "Mr. A Person", rel: "key_management_of", source: "BSE" }],
      relative_of_kmp: [{ id: "person:Y", name: "Ms. B Person", rel: "relative_of_kmp", source: "BSE" }],
    },
    structure_counts: { subsidiary_of: 1, key_management_of: 1, relative_of_kmp: 1 },
    disclosed: {
      produces: [{ id: "output:W", name: "Widgets", quote: "We make widgets.", match: "exact" }],
      consumes: [{ id: "input:S", name: "Steel", quote: null, match: "prefix" }],
    },
    owners: [{ id: "h:1", name: "Promoter Holdings", pct: 50.25 }],
    schemes: [{ id: "s:1", name: "Some Fund", value: 4.2e9 }],
  }),
  // the HDFC case: real rupees, all under one crore
  tiny: base({
    trade: {
      sells_to: [cp("p:1", "Mr. Srinivasan Vaidyanathan", { "Sep 2023": 240000 }),
                 cp("p:2", "Ms. Madhumita Ganguli", { "Sep 2023": 50000 })],
      buys_from: [cp("c:1", "Aurionpro Solutions Limited", { "Sep 2024": 26500000 })],
    },
  }),
  // gaps: a counterparty absent from most periods must not read as zero
  gappy: base({
    trade: {
      sells_to: [cp("c:1", "Occasional Counterparty Ltd",
                    { "Mar 2022": 5e10, "Sep 2024": 9e10 })],
      buys_from: [],
    },
  }),
  // one side entirely empty
  oneSided: base({
    trade: { sells_to: [cp("c:1", "Only Customer Ltd", { "Mar 2023": 1e12 })], buys_from: [] },
  }),
  // a single filed period - a sparkline cannot be drawn from one point
  single: base({
    periods: ["Sep 2024"],
    trade: { sells_to: [cp("c:1", "Sole Ltd", { "Sep 2024": 3e11 })], buys_from: [] },
  }),
  // nothing at all
  empty: base({}),
};

const FINITE = /^-?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/;
for (const [name, d] of Object.entries(FIX)) {
  M.NET.d = d;
  const s = d.trade.sells_to, b = d.trade.buys_from;
  const parts = [];
  if (s.length || b.length) {
    parts.push(["sankey", M.netSankey(d, s, b)], ["table", M.netTable(d, s, b)]);
  }
  if (d.periods.length > 1) parts.push(["bars", M.netPeriodBars(d, s, b)]);
  if (Object.keys(d.structure).length) parts.push(["structure", M.netStructure(d)]);
  if (Object.values(d.disclosed).some((v) => v.length)) parts.push(["chain", M.netChain(d)]);
  parts.push(["owners", M.netOwners(d)]);

  for (const [what, html] of parts) {
    const w = `${name}/${what}`;
    ok(typeof html === "string" && html.length > 20, `${w} produced nothing`);
    for (const junk of ["NaN", "undefined", "Infinity", "[object "]) {
      const at = html.indexOf(junk);
      ok(at < 0, `${w} contains ${junk}`, at < 0 ? "" : html.slice(Math.max(0, at - 60), at + 30));
    }
    // every geometry attribute must be a finite number
    for (const m of html.matchAll(/\s(x|y|x1|y1|x2|y2|cx|cy|r|width|height)="([^"]*)"/g)) {
      ok(m[2] === "transparent" || FINITE.test(m[2].trim()),
         `${w} bad geometry ${m[1]}="${m[2]}"`);
    }
    for (const m of html.matchAll(/\sd="([^"]+)"/g)) {
      ok(!/NaN|Infinity/.test(m[1]), `${w} bad path`, m[1].slice(0, 70));
    }
    ok((html.match(/<svg/g) || []).length === (html.match(/<\/svg>/g) || []).length,
       `${w} unbalanced <svg>`);
  }
}

/* ---- the flow diagram must not silently drop counterparties ---- */
{
  const d = FIX.rich; M.NET.d = d;
  const svg = M.netSankey(d, d.trade.sells_to, d.trade.buys_from);
  ok(/smaller counterparties/.test(svg),
     "22 customers exceed the top-12 cut and the remainder band is missing — " +
     "the picture would not add up to the header total");
  const shown = [...svg.matchAll(/₹([\d,.<]+) Cr<\/title>/g)].length;
  ok(shown >= 26, "flow ribbons missing", shown);
}

/* ---- labels must not sit on top of each other ----
   Ribbon height is proportional to rupees, so a counterparty worth a
   thousandth of the largest gets a ribbon barely a pixel tall. Placing its
   name at that ribbon's centre stacked eleven of Reliance's suppliers into
   one illegible block on screen. Label slots are therefore evenly spaced and
   this measures that they stay apart. */
{
  const MIN_GAP = 18;   // a two-line label (name + value) needs this much
  for (const name of ["rich", "tiny", "gappy", "oneSided"]) {
    const d = FIX[name]; M.NET.d = d;
    const svg = M.netSankey(d, d.trade.sells_to, d.trade.buys_from);
    // group label y-positions by column, using the text-anchor to tell sides
    const bySide = { end: [], start: [] };
    for (const m of svg.matchAll(
        /<text class="net-lb" x="([-\d.]+)" y="([-\d.]+)" text-anchor="(end|start)"/g)) {
      bySide[m[3]].push(parseFloat(m[2]));
    }
    for (const [side, ys] of Object.entries(bySide)) {
      const sorted = [...ys].sort((a, b) => a - b);
      for (let i = 1; i < sorted.length; i++) {
        const gap = sorted[i] - sorted[i - 1];
        ok(gap >= MIN_GAP,
           `${name}/${side}: labels ${gap.toFixed(1)}px apart, need ${MIN_GAP}`,
           `${sorted.length} labels`);
      }
    }
  }
}

/* ---- every label must still point at its own ribbon ----
   Evenly spacing the labels decouples them from the data, so the leader line
   is what keeps the picture honest. One per label, no more, no fewer. */
{
  const d = FIX.rich; M.NET.d = d;
  const svg = M.netSankey(d, d.trade.sells_to, d.trade.buys_from);
  const labels = (svg.match(/<text class="net-lb"/g) || []).length;
  const leads = (svg.match(/<path class="net-lead"/g) || []).length;
  ok(labels === leads,
     "every label needs exactly one leader to the ribbon it describes",
     `${labels} labels, ${leads} leaders`);
  ok(labels > 0, "no labels rendered at all");
}

/* ---- the diagram must not scale its own text ----
   viewBox scaling magnifies 10.5px label text to 21px on a 2000px window. */
{
  const css = fs.readFileSync("src/shunkan/server/static/styles.css", "utf8");
  const rule = css.slice(css.indexOf(".net-flow {"), css.indexOf(".net-flow {") + 200);
  ok(/width:\s*1000px/.test(rule), "the flow svg is not width-capped", rule.slice(0, 80));
  ok(/max-width:\s*100%/.test(rule), "capped but not responsive on narrow screens");
  ok(/\.net-flow-wrap\s*\{[^}]*overflow-x:\s*auto/.test(css),
     "wide diagram must scroll in its own container, not the page body");
}

/* ---- the remainder band must open ----
   A summary that names "8 smaller counterparties" and then refuses to say
   which is a dead end. It is a control, and it has to behave like one. */
{
  const d = FIX.rich; M.NET.d = d;
  const sells = d.trade.sells_to, buys = d.trade.buys_from;
  M.NET_EXPAND.L = M.NET_EXPAND.R = false;

  const collapsed = M.netSankey(d, sells, buys);
  ok(/data-expand="R"/.test(collapsed), "the remainder band carries no expand handle");
  ok(/show all/.test(collapsed), "the band does not say it can be opened");
  const nCollapsed = (collapsed.match(/<text class="net-lb"/g) || []).length;

  M.NET_EXPAND.R = true;
  const opened = M.netSankey(d, sells, buys);
  const nOpened = (opened.match(/<text class="net-lb"/g) || []).length;
  ok(nOpened > nCollapsed,
     "expanding a side did not reveal more counterparties",
     `${nCollapsed} -> ${nOpened}`);
  ok(/collapse/.test(opened), "expanded with no way back");
  // every real counterparty on that side must now be present
  for (const cp of sells) {
    const nm = cp.name.length > 30 ? cp.name.slice(0, 29) : cp.name;
    ok(opened.includes(esc(nm)), `expanded view is missing ${cp.name}`);
  }
  // labels must STILL not overlap once expanded
  const ys = [...opened.matchAll(
    /<text class="net-lb" x="[-\d.]+" y="([-\d.]+)" text-anchor="start"/g)]
    .map((m) => parseFloat(m[1])).sort((a, b) => a - b);
  for (let i = 1; i < ys.length; i++) {
    ok(ys[i] - ys[i - 1] >= 18,
       `expanded labels overlap: ${(ys[i] - ys[i - 1]).toFixed(1)}px apart`);
  }
  M.NET_EXPAND.L = M.NET_EXPAND.R = false;
}

/* ---- an empty side must say why it is empty ----
   Balrampur Chini files purchases, dividends and remuneration and NO sale
   rows at all, so "no customers" is the correct finding. Rendered as blank
   space it reads as a broken chart instead of an answer. */
{
  const d = FIX.oneSided; M.NET.d = d;   // customers only, no suppliers
  M.NET_EXPAND.L = M.NET_EXPAND.R = false;
  const svg = M.netSankey(d, d.trade.sells_to, d.trade.buys_from);
  ok(/no purchases from related parties/.test(svg),
     "the empty supplier side renders as blank space");
  ok(/NO PURCHASES FILED/.test(svg),
     "the header still counts an empty side as a real one");
  ok(!/Largest supplier <b/.test(svg),
     "names a largest supplier when none was filed");
  ok(/nothing to rank/.test(svg), "no explanation for the missing ranking");
  // the populated side must be unaffected
  ok(/Largest customer <b/.test(svg), "the populated side lost its ranking");
}

/* ---- a gap is not a zero ---- */
{
  const d = FIX.gappy; M.NET.d = d;
  const sp = M.netSpark(d.trade.sells_to[0], d.periods);
  const pts = (sp.match(/<circle/g) || []).length;
  ok(pts === 2, "sparkline drew a point for a period with no filing", pts);
  const single = M.netSpark(FIX.single.trade.sells_to[0], FIX.single.periods);
  ok(!/<svg/.test(single), "a single filed period must not be drawn as a trend");
}

/* ---- people are counted apart from corporate structure ---- */
{
  const d = FIX.rich; M.NET.d = d;
  const h = M.netStructure(d);
  ok(/1 entities/.test(h), "entity count wrong — people must not inflate it",
     (h.match(/[\d]+ entities/) || [])[0]);
  ok(/2 named individuals/.test(h), "named individuals not counted separately");
  ok(M.NET_PEOPLE.has("relative_of_kmp") && M.NET_PEOPLE.has("key_management_of"),
     "person relations missing from NET_PEOPLE");
}

/* ---- a missing quote must not print as "null" ---- */
{
  const d = FIX.rich; M.NET.d = d;
  const h = M.netChain(d);
  ok(!/>null</.test(h) && !/null<\/div>/.test(h), "a null quote reached the DOM");
}

console.log(bad ? `${bad} FAILURES` : "net render: all checks passed");
process.exit(bad ? 1 : 0);
