# Porting Terminal v3 "Instrument" into shunkan

Three files. Two are drop-in replacements, one is a ~15-line app.js addition.

## 1. styles.css — drop-in replacement
`port/styles.css` is your current `src/shunkan/server/static/styles.css` with a
**V3 INSTRUMENT PASS** block appended at the end (same convention as your own
TERMINAL PASS — appended last so it wins on equal specificity, revert by
deleting the block). What it changes:

- Geometry: topbar 26px, statusbar 22px, rows 18px, and a new `--cmd-h: 26px`
  band for the command line. `main`/`.rail` shift down by `--cmd-h`.
- Full-bleed tiling: `main` padding 0, `.view`/`.row` gap 0, panels lose their
  side borders and meet on 1px rules.
- Section bars: thin blue-label header bands (blue = labels, amber = data —
  panel titles were amber in the TERMINAL PASS; v3 moves them to blue and
  reserves amber for `.hl` and premiums).
- `.ltp-cell` premiums render amber + bookable hover ring.
- `.kv` strips become inline label:value pairs; strike column gets its own
  darker gutter (`td.k-strike`).
- `.cmdline` styles for the new bar.

Bump the cache-buster: `styles.css?v=36`.

## 2. index.html — drop-in replacement
`port/index.html` is your current index.html plus one block after the topbar:

    <div class="cmdline">
      <span class="cl-caret">&gt;</span>
      <input id="cl-input" placeholder="OPT NIFTY · PRT · PLS · …" autocomplete="off" spellcheck="false">
      <span class="cl-hint">ENTER <b>GO</b> · ⌘K FULL PALETTE</span>
    </div>

Nothing else changed except `?v=` bumps.

## 3. app.js — one small addition
The command line reuses the palette's existing `runCommand`. Add this next to
the palette wiring and call `wireCmdline()` from boot:

    function wireCmdline() {
      const inp = $("#cl-input");
      if (!inp) return;
      inp.addEventListener("keydown", (e) => {
        if (e.key !== "Enter") return;
        const v = inp.value.trim();
        if (!v) return;
        runCommand(v);          // same parser the ⌘K palette uses
        inp.value = "";
        inp.blur();
      });
      // "/" focuses the command line from anywhere outside an input
      window.addEventListener("keydown", (e) => {
        if (e.key === "/" && !/INPUT|SELECT|TEXTAREA/.test(document.activeElement?.tagName || "")) {
          e.preventDefault();
          inp.focus();
        }
      });
    }

Aliases: if you want the bare mnemonics (OPT/PRT/PLS/ANL/…) to work in
runCommand, map them to the same handlers as the rail codes before parsing:

    const CODE_ALIAS = { OPT: "oc", PRT: "portfolio", PLS: "pulse", ANL: "analyse",
                         CHT: "c", TPE: "tape", ALR: "alerts", DTA: "datastore",
                         ANA: "brief", QNT: "qnt" };

## Phase 2 (optional, bigger)
The v3 mockup's OPT screen is a desk workspace: chain left, and a right-hand
336px stack (analytics · straddle · net book risk · live tape). That is a
`renderChain` layout change — wrap the existing panels in
`grid-template-columns: 1fr 336px` and mount the risk strip + a tape widget in
the right column. The mockup (`Terminal v3 — Instrument.dc.html`) is the spec:
every color, size and spacing in it is inline and copyable. Once the command
line has bedded in, the rail can be dropped (`--rail-w: 0`) since every rail
view is reachable by code.

## Not ported on purpose
- The mockup's static demo numbers — your app renders live data.
- The mockup's chain-state overrides (MODELLED/REFUSED commands) — your app
  derives these states from real sources.
