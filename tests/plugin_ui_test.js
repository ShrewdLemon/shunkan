const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(path.join(__dirname, "..", "src/shunkan/server/static/app.js"), "utf8");
const start = src.indexOf("/* ---------- plugins ----------");
const end = src.indexOf("/* ---------- end plugins ---------- */");
if (start < 0 || end < 0) { console.error("plugin block markers missing"); process.exit(1); }
const RENDER = {}, CODE_ALIAS = {}, SYMBOL_VIEWS = new Set(), ANL_VIEWS = new Set();
const ANL_GROUPS = [["CONTEXT", []]];
const window = {};
eval(src.slice(start, end));
const render = () => "x";
window.shunkan.registerView({ id: "fake", code: "FAK", label: "Fake", render, symbol: true, blurb: "b" });
window.shunkan.registerView({ id: "fake", code: "FAK", label: "Fake", render, symbol: true, blurb: "b" });
const ok = RENDER.fake === render && CODE_ALIAS.FAK === "fake" && SYMBOL_VIEWS.has("fake")
  && ANL_VIEWS.has("fake") && ANL_GROUPS[0][1].length === 1
  && JSON.stringify(ANL_GROUPS[0][1][0]) === JSON.stringify(["fake", "FAK", "Fake", "b"]);
if (!ok) { console.error("registerView did not register", { RENDER, CODE_ALIAS, ANL_GROUPS }); process.exit(1); }

const rc = src.indexOf("function runCommand(line)");
if (rc < 0) { console.error("runCommand missing"); process.exit(1); }
const calls = [], toasts = [];
const show = (...a) => calls.push(a), toast = (m) => toasts.push(m), postJSON = async () => ({});
const state = {};
eval(src.slice(rc, src.indexOf("\n}\n", rc) + 2));
window.shunkan.registerView({ id: "flat", code: "FLT", label: "Flat", render });
runCommand(`${CODE_ALIAS.FAK} RELIANCE`);
runCommand(`${CODE_ALIAS.FLT} RELIANCE`);
runCommand("constructor");
const routed = JSON.stringify(calls) === JSON.stringify([["fake", { symbol: "RELIANCE" }], ["flat", {}]])
  && toasts.length === 1;
if (!routed) { console.error("typed plugin code did not open its view", { calls, toasts }); process.exit(1); }

const getJSON = async () => [{ id: "fake", code: "FAK", module: "data:text/javascript," }];
loadPlugins().then(() => {
  if (!window.__deskViews || window.__deskViews.fake?.code !== "FAK") {
    console.error("loadPlugins did not hand modules their view metadata", window.__deskViews); process.exit(1);
  }
  console.log("ok");
});
