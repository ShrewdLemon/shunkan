/* Verifies the supply-map expand behaviour by extracting the REAL handler out
   of app.js and running it against a real DOM, rather than trusting a re-typed
   copy of it. */
const fs = require("fs");
const { JSDOM } = require("jsdom");

const src = fs.readFileSync("src/shunkan/server/static/app.js", "utf8");

// Pull the exact toggle + listener block that ships, so this test fails if
// someone edits it.
const start = src.indexOf("    const toggle = (el) => {");
const end = src.indexOf("    }\n", src.indexOf("      toggle(t);\n    });", start)) + 6;
const block = src.slice(start, end);
if (!block.includes("classList.toggle(\"open\")")) {
  console.error("FAIL: could not extract the toggle block from app.js"); process.exit(1);
}

const dom = new JSDOM(`<!doctype html><body><div id="cmp-splc">
  <div class="splc-node clip" tabindex="0" role="button" aria-expanded="false">
    <div class="splc-term">POLYESTER</div>
    <div class="splc-ev">${"x".repeat(300)}</div>
  </div>
  <div class="splc-node" tabindex="0" role="button" aria-expanded="false">
    <div class="splc-term">SHORT</div><div class="splc-ev">tiny</div>
  </div>
  <table><tbody>
    <tr class="fam-row clip" tabindex="0" role="button" aria-expanded="false">
      <td class="txt sym">Jio Platforms Limited</td>
      <td class="fam-ev">${"y".repeat(300)}</td>
    </tr>
  </tbody></table>
</div></body>`);
const { window } = dom;
global.window = window; global.document = window.document;
const h = window.document.getElementById("cmp-splc");

// eslint-disable-next-line no-eval
eval(block.replace(/^\s{4}/gm, ""));

const fire = (el, type, init = {}) =>
  el.dispatchEvent(new window[type === "keydown" ? "KeyboardEvent" : "MouseEvent"](
    type, { bubbles: true, ...init }));

let pass = 0, fail = 0;
const check = (name, cond) => {
  if (cond) { pass++; console.log(`  ok   ${name}`); }
  else { fail++; console.log(`  FAIL ${name}`); }
};

const tile = h.querySelector(".splc-node.clip");
const ev = tile.querySelector(".splc-ev");
check("tile starts closed", !tile.classList.contains("open"));
check("full evidence is in the DOM, not truncated", ev.textContent.length === 300);
check("no ellipsis character in the text", !ev.textContent.includes("…"));

fire(ev, "click");                       // click the INNER element, not the tile
check("click on inner text opens the tile", tile.classList.contains("open"));
check("aria-expanded reflects state", tile.getAttribute("aria-expanded") === "true");
fire(ev, "click");
check("second click closes it", !tile.classList.contains("open"));

const fam = h.querySelector(".fam-row.clip");
fire(fam.querySelector(".fam-ev"), "click");
check("family row expands too", fam.classList.contains("open"));
check("family evidence full length", fam.querySelector(".fam-ev").textContent.length === 300);

fire(fam, "keydown", { key: "Enter" });
check("Enter toggles (keyboard reachable)", !fam.classList.contains("open"));

const short = h.querySelectorAll(".splc-node")[1];
fire(short, "click");
check("un-clipped tile still toggles harmlessly", short.classList.contains("open"));

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
