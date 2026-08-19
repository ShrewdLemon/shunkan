/* SHUNKAN Quant Lab 3D — three.js surfaces, fans and swarms.
   ES module (importmap maps 'three' to the vendored build). Exposes
   window.Viz3D for the classic-script app.js. Bloomberg discipline holds
   in 3D too: hairline grids, mono labels, and every axis named. */

import * as THREE from "three";
import { OrbitControls } from "./vendor/OrbitControls.js";

/* ---------- palette (mirrors styles.css) ---------- */

const C = {
  amber: 0xf0a826, up: 0x2ebd85, down: 0xf1564b,
  text: "#9aa1b0", faint: "#5d6470", grid: 0x1c212b,
};

/* teal -> green -> amber -> red height ramp */
const RAMP = [
  [0.00, [0x17, 0x4f, 0x63]],
  [0.35, [0x2e, 0xbd, 0x85]],
  [0.70, [0xf0, 0xa8, 0x26]],
  [1.00, [0xf1, 0x56, 0x4b]],
];
function ramp(t) {
  t = Math.min(1, Math.max(0, t));
  for (let i = 1; i < RAMP.length; i++) {
    if (t <= RAMP[i][0]) {
      const [t0, a] = RAMP[i - 1], [t1, b] = RAMP[i];
      const u = (t - t0) / (t1 - t0 || 1);
      return a.map((v, k) => (v + (b[k] - v) * u) / 255);
    }
  }
  return RAMP[RAMP.length - 1][1].map((v) => v / 255);
}

/* ---------- text sprites ---------- */

function textSprite(text, { color = C.text, px = 21, bold = false, scale = 0.00072,
                            depthTest = false } = {}) {
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d");
  const font = `${bold ? "600 " : ""}${px * 4}px 'SF Mono', Menlo, monospace`;
  ctx.font = font;
  const w = Math.ceil(ctx.measureText(text).width) + 16;
  canvas.width = w; canvas.height = px * 5;
  const c2 = canvas.getContext("2d");
  c2.font = font;
  c2.fillStyle = color;
  c2.textBaseline = "middle";
  c2.fillText(text, 8, canvas.height / 2);
  const tex = new THREE.CanvasTexture(canvas);
  tex.minFilter = THREE.LinearFilter;
  const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthTest });
  const sp = new THREE.Sprite(mat);
  sp.scale.set(canvas.width * scale, canvas.height * scale, 1);
  return sp;
}

/* ---------- scene shell ---------- */

class Scene3D {
  constructor(host) {
    this.host = host;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.domElement.className = "viz-canvas";
    host.appendChild(this.renderer.domElement);

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(42, 1, 0.01, 60);
    this.camera.position.set(1.9, 1.45, 2.05);

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.target.set(0, 0.3, 0);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.08;
    this.controls.minDistance = 0.8;
    this.controls.maxDistance = 8;

    this.scene.add(new THREE.AmbientLight(0xffffff, 0.85));
    const dir = new THREE.DirectionalLight(0xffffff, 1.1);
    dir.position.set(2, 3.2, 1.2);
    this.scene.add(dir);

    const grid = new THREE.GridHelper(2.4, 12, C.grid, C.grid);
    grid.position.y = -0.001;
    grid.material.transparent = true;
    grid.material.opacity = 0.85;
    this.scene.add(grid);

    this._tickers = [];
    this._disposed = false;
    this._resize = () => {
      const w = host.clientWidth, h = host.clientHeight;
      if (!w || !h) return;
      this.renderer.setSize(w, h, false);
      this.camera.aspect = w / h;
      this.camera.updateProjectionMatrix();
    };
    this._resize();
    this._ro = new ResizeObserver(this._resize);
    this._ro.observe(host);

    const loop = (t) => {
      if (this._disposed) return;
      if (!host.isConnected) { this.dispose(); return; }
      requestAnimationFrame(loop);
      this.controls.update();
      for (const fn of this._tickers) fn(t);
      this.renderer.render(this.scene, this.camera);
    };
    requestAnimationFrame(loop);
  }

  onTick(fn) { this._tickers.push(fn); }

  dispose() {
    if (this._disposed) return;
    this._disposed = true;
    this._ro.disconnect();
    this.controls.dispose();
    this.scene.traverse((o) => {
      if (o.geometry) o.geometry.dispose();
      if (o.material) {
        (Array.isArray(o.material) ? o.material : [o.material]).forEach((m) => {
          if (m.map) m.map.dispose();
          m.dispose();
        });
      }
    });
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }
}

/* ---------- shared mappers ---------- */

function mapper(values, lo, hi) {
  const vmin = Math.min(...values), vmax = Math.max(...values);
  const span = vmax - vmin || 1;
  const f = (v) => lo + ((v - vmin) / span) * (hi - lo);
  f.min = vmin; f.max = vmax;
  return f;
}

function fmtTick(v) {
  const a = Math.abs(v);
  if (a >= 10000) return (v / 1000).toFixed(1) + "k";
  if (a >= 100) return v.toFixed(0);
  if (a >= 1) return v.toFixed(2);
  return v.toFixed(3);
}

/* axis tick labels along the three box edges */
function addAxisTicks(scene3d, { xs, zs, yMin, yMax, xLabel, zLabel, yLabel, yFmt }) {
  const g = new THREE.Group();
  const fy = yFmt || fmtTick;
  const xTicks = 4, zTicks = 4, yTicks = 3;
  for (let i = 0; i <= xTicks; i++) {
    const v = xs.min + ((xs.max - xs.min) * i) / xTicks;
    const sp = textSprite(fmtTick(v), { color: C.faint });
    sp.position.set(xs(v), 0.005, 1.06);
    g.add(sp);
  }
  for (let i = 0; i <= zTicks; i++) {
    const v = zs.min + ((zs.max - zs.min) * i) / zTicks;
    const sp = textSprite(fmtTick(v), { color: C.faint });
    sp.position.set(1.1, 0.005, zs(v));
    g.add(sp);
  }
  for (let i = 0; i <= yTicks; i++) {
    const yv = yMin + ((yMax - yMin) * i) / yTicks;
    const sp = textSprite(fy(yv), { color: C.faint });
    sp.position.set(-1.06, (0.75 * i) / yTicks + 0.01, -1.0);
    g.add(sp);
  }
  const lx = textSprite(xLabel, { color: C.text, bold: true, scale: 0.00082 });
  lx.position.set(0, 0.005, 1.24);
  const lz = textSprite(zLabel, { color: C.text, bold: true, scale: 0.00082 });
  lz.position.set(1.32, 0.005, 0);
  const ly = textSprite(yLabel, { color: C.text, bold: true, scale: 0.00082 });
  ly.position.set(-1.06, 0.86, -1.0);
  g.add(lx, lz, ly);
  scene3d.scene.add(g);
}

/* ---------- surface geometry ---------- */

function buildSurfaceGeometry(xsArr, zsArr, grid, mapX, mapZ, mapY) {
  const nx = xsArr.length, nz = zsArr.length;
  const pos = new Float32Array(nx * nz * 3);
  const col = new Float32Array(nx * nz * 3);
  let p = 0;
  for (let j = 0; j < nz; j++) {
    for (let i = 0; i < nx; i++) {
      const y = mapY(grid[j][i]);
      pos[p * 3] = mapX(xsArr[i]);
      pos[p * 3 + 1] = y;
      pos[p * 3 + 2] = mapZ(zsArr[j]);
      const [r, g, b] = ramp((y - 0) / 0.75);
      col[p * 3] = r; col[p * 3 + 1] = g; col[p * 3 + 2] = b;
      p++;
    }
  }
  const idx = [];
  for (let j = 0; j < nz - 1; j++) {
    for (let i = 0; i < nx - 1; i++) {
      const a = j * nx + i, b = a + 1, c = a + nx, d = c + 1;
      idx.push(a, c, b, b, c, d);
    }
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  geo.setAttribute("color", new THREE.BufferAttribute(col, 3));
  geo.setIndex(idx);
  geo.computeVertexNormals();
  return geo;
}

function addSurface(scene3d, geo) {
  const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({
    vertexColors: true, side: THREE.DoubleSide, roughness: 0.82, metalness: 0.08,
  }));
  const wire = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({
    wireframe: true, transparent: true, opacity: 0.07, color: 0xffffff,
  }));
  scene3d.scene.add(mesh, wire);
  return mesh;
}

/* ============================================================
   PUBLIC: generic value surface (IV / greeks)
   cfg: { xs, zs, grid[j над zs][i над xs], xLabel, zLabel, yLabel,
          yFmt, highlightRow, spotX }
   ============================================================ */

function mountSurface(host, cfg) {
  host.innerHTML = "";
  const s3 = new Scene3D(host);
  const flat = cfg.grid.flat().filter((v) => Number.isFinite(v));
  const yMin = Math.min(...flat), yMax = Math.max(...flat);
  const mapX = mapper(cfg.xs, -0.9, 0.9);
  const mapZ = mapper(cfg.zs, -0.9, 0.9);
  const spanY = yMax - yMin || 1;
  const mapY = (v) => ((Math.min(Math.max(v, yMin), yMax) - yMin) / spanY) * 0.75;

  addSurface(s3, buildSurfaceGeometry(cfg.xs, cfg.zs, cfg.grid, mapX, mapZ, mapY));
  addAxisTicks(s3, {
    xs: mapX, zs: mapZ, yMin, yMax,
    xLabel: cfg.xLabel, zLabel: cfg.zLabel, yLabel: cfg.yLabel, yFmt: cfg.yFmt,
  });

  if (cfg.highlightRow !== undefined && cfg.highlightRow !== null) {
    const j = cfg.highlightRow;
    const pts = cfg.xs.map((x, i) => new THREE.Vector3(
      mapX(x), mapY(cfg.grid[j][i]) + 0.008, mapZ(cfg.zs[j])));
    const line = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineBasicMaterial({ color: C.amber }));
    s3.scene.add(line);
    const tag = textSprite("MARKET EXPIRY", { color: "#f0a826", bold: true });
    tag.position.set(1.16, mapY(cfg.grid[j][cfg.xs.length - 1]) + 0.05, mapZ(cfg.zs[j]));
    s3.scene.add(tag);
  }

  if (cfg.spotX !== undefined) {
    const x = mapX(cfg.spotX);
    const line = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints([
        new THREE.Vector3(x, 0.002, -0.95), new THREE.Vector3(x, 0.002, 1.0)]),
      new THREE.LineDashedMaterial({ color: 0xffffff, transparent: true, opacity: 0.4, dashSize: 0.04, gapSize: 0.03 }));
    line.computeLineDistances();
    s3.scene.add(line);
    const tag = textSprite("SPOT", { color: C.text });
    tag.position.set(x, 0.02, 1.14);
    s3.scene.add(tag);
  }
  return { dispose: () => s3.dispose() };
}

/* ============================================================
   PUBLIC: Monte Carlo price fan
   data: { days[], paths[][], envelope{p5,p25,p50,p75,p95}, spot,
           terminal_bins[], terminal_freq[] }
   ============================================================ */

function mountFan(host, data) {
  host.innerHTML = "";
  const s3 = new Scene3D(host);
  const all = data.paths.flat();
  const yLo = Math.min(...all, data.spot), yHi = Math.max(...all, data.spot);
  const spanY = yHi - yLo || 1;
  const mapX = mapper(data.days, -0.9, 0.9);
  const mapY = (v) => ((v - yLo) / spanY) * 0.75;
  const n = data.paths.length;
  const mapZ = (k) => -0.75 + (k / (n - 1 || 1)) * 1.5;

  data.paths.forEach((path, k) => {
    const up = path[path.length - 1] >= data.spot;
    const pts = path.map((v, i) => new THREE.Vector3(mapX(data.days[i]), mapY(v), mapZ(k)));
    const line = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineBasicMaterial({
        color: up ? C.up : C.down, transparent: true, opacity: 0.5,
      }));
    s3.scene.add(line);
  });

  // envelope curves on the front wall
  const wallZ = 0.9;
  const env = [["p5", C.faint], ["p50", "#ffffff"], ["p95", C.faint]];
  for (const [key, color] of env) {
    const pts = data.envelope[key].map((v, i) =>
      new THREE.Vector3(mapX(data.days[i]), mapY(v), wallZ));
    const line = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineBasicMaterial({ color: new THREE.Color(color === "#ffffff" ? 0xffffff : color), transparent: true, opacity: key === "p50" ? 0.9 : 0.55 }));
    s3.scene.add(line);
    const tag = textSprite(key.toUpperCase(), { color: key === "p50" ? "#e6e9ef" : C.faint });
    tag.position.set(1.06, mapY(data.envelope[key][data.envelope[key].length - 1]), wallZ);
    s3.scene.add(tag);
  }

  // terminal distribution as bars on the far x wall
  const maxFreq = Math.max(...data.terminal_freq);
  data.terminal_bins.forEach((bin, i) => {
    if (bin < yLo || bin > yHi) return;
    const len = (data.terminal_freq[i] / maxFreq) * 0.42;
    const geo = new THREE.BoxGeometry(len, 0.012, 0.02);
    const mesh = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({
      color: bin >= data.spot ? C.up : C.down, transparent: true, opacity: 0.8,
    }));
    mesh.position.set(0.92 + len / 2, mapY(bin), -0.9);
    s3.scene.add(mesh);
  });

  // spot reference line
  const spotLine = new THREE.Line(
    new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(-0.9, mapY(data.spot), wallZ),
      new THREE.Vector3(0.9, mapY(data.spot), wallZ)]),
    new THREE.LineDashedMaterial({ color: 0xffffff, transparent: true, opacity: 0.35, dashSize: 0.04, gapSize: 0.03 }));
  spotLine.computeLineDistances();
  s3.scene.add(spotLine);

  addAxisTicks(s3, {
    xs: mapX, zs: mapper([0, n], -0.75, 0.75), yMin: yLo, yMax: yHi,
    xLabel: "DAYS AHEAD", zLabel: "PATHS (SORTED)", yLabel: "PRICE",
  });
  return { dispose: () => s3.dispose() };
}

/* ============================================================
   PUBLIC: swarm optimizer animation
   data: /api/swarm response. Returns a handle with play/pause/seek.
   ============================================================ */

const PENALTY_FLOOR = -4.5; // matches backend PENALTY=-5 sentinel

function mountSwarm(host, data, { onIter } = {}) {
  host.innerHTML = "";
  const s3 = new Scene3D(host);
  const L = data.landscape;

  const finite = L.z.flat().filter((v) => v > PENALTY_FLOOR);
  const zMin = Math.min(...finite), zMax = Math.max(...finite);
  const spanZ = zMax - zMin || 1;
  const clampFit = (v) => Math.max(v, zMin);
  const mapX = mapper(L.x, -0.9, 0.9);
  const mapZ = mapper(L.y, -0.9, 0.9);
  const mapY = (v) => ((clampFit(v) - zMin) / spanZ) * 0.75;

  const grid = L.z.map((row) => row.map(clampFit));
  addSurface(s3, buildSurfaceGeometry(L.x, L.y, grid, mapX, mapZ, mapY));
  addAxisTicks(s3, {
    xs: mapX, zs: mapZ, yMin: zMin, yMax: zMax,
    xLabel: data.param_names[0].toUpperCase(),
    zLabel: data.param_names[1].toUpperCase(),
    yLabel: "SHARPE", yFmt: (v) => v.toFixed(2),
  });

  // particles
  const iters = data.iterations;
  const nP = iters[0].p.length;
  const pGeo = new THREE.SphereGeometry(0.02, 10, 10);
  const particles = new THREE.InstancedMesh(
    pGeo, new THREE.MeshBasicMaterial({ color: 0xffffff }), nP);
  s3.scene.add(particles);

  // global-best marker: amber sphere + beacon line
  const gbest = new THREE.Mesh(new THREE.SphereGeometry(0.034, 14, 14),
    new THREE.MeshBasicMaterial({ color: C.amber }));
  s3.scene.add(gbest);
  const beamGeo = new THREE.BufferGeometry().setFromPoints([
    new THREE.Vector3(), new THREE.Vector3()]);
  const beam = new THREE.Line(beamGeo,
    new THREE.LineBasicMaterial({ color: C.amber, transparent: true, opacity: 0.5 }));
  s3.scene.add(beam);

  // gbest trail
  const trailPts = iters.map((it) => new THREE.Vector3(
    mapX(it.g[0]), mapY(it.gf) + 0.012, mapZ(it.g[1])));
  const trail = new THREE.Line(
    new THREE.BufferGeometry().setFromPoints(trailPts.slice(0, 1)),
    new THREE.LineBasicMaterial({ color: C.amber, transparent: true, opacity: 0.8 }));
  s3.scene.add(trail);

  const dummy = new THREE.Object3D();
  const setFrame = (fi, frac) => {
    const a = iters[Math.min(fi, iters.length - 1)];
    const b = iters[Math.min(fi + 1, iters.length - 1)];
    for (let i = 0; i < nP; i++) {
      const x = a.p[i][0] + (b.p[i][0] - a.p[i][0]) * frac;
      const y = a.p[i][1] + (b.p[i][1] - a.p[i][1]) * frac;
      const f = a.f[i] + (b.f[i] - a.f[i]) * frac;
      dummy.position.set(mapX(x), mapY(f) + 0.02, mapZ(y));
      dummy.updateMatrix();
      particles.setMatrixAt(i, dummy.matrix);
    }
    particles.instanceMatrix.needsUpdate = true;
    const g = a;
    gbest.position.set(mapX(g.g[0]), mapY(g.gf) + 0.02, mapZ(g.g[1]));
    beamGeo.setFromPoints([
      new THREE.Vector3(gbest.position.x, 0, gbest.position.z),
      new THREE.Vector3(gbest.position.x, 0.85, gbest.position.z)]);
    trail.geometry.dispose();
    trail.geometry = new THREE.BufferGeometry().setFromPoints(trailPts.slice(0, fi + 1));
  };

  let iter = 0, playing = true, t0 = null;
  const MS_PER_ITER = 560;
  s3.onTick((t) => {
    if (!playing) return;
    if (t0 === null) t0 = t;
    const prog = (t - t0) / MS_PER_ITER;
    const fi = Math.floor(prog);
    if (fi >= iters.length) { playing = false; setFrame(iters.length - 1, 0); if (onIter) onIter(iters.length - 1, false); return; }
    if (fi !== iter) { iter = fi; if (onIter) onIter(iter, true); }
    setFrame(fi, prog - fi);
  });
  setFrame(0, 0);
  if (onIter) onIter(0, true);

  return {
    dispose: () => s3.dispose(),
    replay: () => { t0 = null; iter = 0; playing = true; },
    pause: () => { playing = false; },
    isPlaying: () => playing,
    iterCount: iters.length,
  };
}

/* ============================================================
   PUBLIC: 3D scatter (efficient frontier)
   cfg: { points: [[x,y,c]...], xLabel, yLabel, marks: [{x,y,c,color,label}] }
   x -> X axis, y -> height, c -> color ramp + depth
   ============================================================ */

function mountScatter(host, cfg) {
  host.innerHTML = "";
  const s3 = new Scene3D(host);
  const xs = cfg.points.map((p) => p[0]);
  const ys = cfg.points.map((p) => p[1]);
  const cs = cfg.points.map((p) => p[2]);
  const mapX = mapper(xs, -0.9, 0.9);
  const yMin = Math.min(...ys), yMax = Math.max(...ys);
  const spanY = yMax - yMin || 1;
  const mapY = (v) => ((v - yMin) / spanY) * 0.75;
  const cMin = Math.min(...cs), cMax = Math.max(...cs);
  const mapC = (v) => (v - cMin) / (cMax - cMin || 1);

  const geo = new THREE.SphereGeometry(0.011, 6, 6);
  const mesh = new THREE.InstancedMesh(
    geo, new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.85 }),
    cfg.points.length);
  const dummy = new THREE.Object3D();
  const col = new THREE.Color();
  cfg.points.forEach((p, i) => {
    dummy.position.set(mapX(p[0]), mapY(p[1]), (mapC(p[2]) - 0.5) * 1.5);
    dummy.updateMatrix();
    mesh.setMatrixAt(i, dummy.matrix);
    const [r, g, b] = ramp(mapC(p[2]));
    mesh.setColorAt(i, col.setRGB(r, g, b));
  });
  mesh.instanceColor.needsUpdate = true;
  s3.scene.add(mesh);

  for (const m of cfg.marks || []) {
    const dot = new THREE.Mesh(new THREE.SphereGeometry(0.035, 14, 14),
      new THREE.MeshBasicMaterial({ color: m.color }));
    dot.position.set(mapX(m.x), mapY(m.y), (mapC(m.c) - 0.5) * 1.5);
    s3.scene.add(dot);
    const tag = textSprite(m.label, { color: `#${m.color.toString(16).padStart(6, "0")}`, bold: true, scale: 0.00068 });
    tag.position.set(dot.position.x, dot.position.y + 0.09, dot.position.z);
    s3.scene.add(tag);
    const beam = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints([
        new THREE.Vector3(dot.position.x, 0, dot.position.z), dot.position.clone()]),
      new THREE.LineBasicMaterial({ color: m.color, transparent: true, opacity: 0.4 }));
    s3.scene.add(beam);
  }

  addAxisTicks(s3, {
    xs: mapX, zs: mapper([cMin, cMax], -0.75, 0.75), yMin, yMax,
    xLabel: cfg.xLabel, zLabel: cfg.zLabel || "SHARPE", yLabel: cfg.yLabel,
    yFmt: cfg.yFmt,
  });
  return { dispose: () => s3.dispose() };
}

/* ============================================================
   PUBLIC: world session globe
   exchanges: rows from /api/sessions — markers lit by real session
   state, graticule sphere, slow spin. Decoration that tells the truth.
   ============================================================ */

const STATE_COLOR = { open: C.up, lunch: C.amber, closed: 0x39404d };

function latLonToVec(lat, lon, r) {
  const la = (lat * Math.PI) / 180, lo = (lon * Math.PI) / 180;
  return new THREE.Vector3(
    r * Math.cos(la) * Math.cos(lo),
    r * Math.sin(la),
    -r * Math.cos(la) * Math.sin(lo));
}

/* Coarse landmass as lat/lon boxes — a dot-matrix earth. Deliberately
   approximate (it is scenery, not data): enough geography that the eye
   reads "globe" instantly, small enough to live in source. */
const LAND_BOXES = [
  // North America
  [50, 72, -166, -60], [40, 50, -125, -60], [30, 40, -120, -78],
  [23, 30, -106, -80], [15, 23, -105, -86], [58, 70, -50, -30], // +Greenland
  // Central bridge + Caribbean
  [8, 15, -92, -77],
  // South America
  [0, 10, -80, -50], [-10, 0, -80, -35], [-25, -10, -71, -40],
  [-40, -25, -73, -53], [-55, -40, -74, -64],
  // Europe
  [48, 60, -10, 30], [36, 48, -10, 28], [60, 71, 4, 32], [50, 60, 30, 60],
  // Africa
  [20, 36, -17, 33], [5, 20, -17, 40], [-12, 5, 8, 42], [-35, -12, 12, 36],
  [-26, -12, 43, 50], // Madagascar
  // Middle East + Central Asia
  [12, 32, 34, 60], [36, 55, 60, 90],
  // Russia / North Asia
  [55, 72, 32, 178], [50, 55, 80, 140],
  // East Asia
  [30, 50, 100, 125], [20, 30, 98, 122], [30, 45, 125, 145], // +Japan
  // South Asia
  [24, 36, 60, 92], [8, 24, 68, 90],
  // SE Asia + Indonesia
  [8, 24, 92, 110], [-10, 8, 95, 141],
  // Australia + NZ
  [-20, -11, 113, 145], [-35, -20, 114, 152], [-47, -34, 166, 178],
  // Antarctica rim
  [-78, -70, -180, 180],
];

function isLand(lat, lon) {
  for (const [s_, n, w, e] of LAND_BOXES) {
    if (lat >= s_ && lat <= n && lon >= w && lon <= e) return true;
  }
  return false;
}

/* Subsolar point from the clock: declination by day of year, longitude by
   UTC time. Good to a degree or two, which is exactly what a terminator
   needs — it is a shading, not an ephemeris. */
function subsolar(now = new Date()) {
  const start = Date.UTC(now.getUTCFullYear(), 0, 0);
  const doy = (now.getTime() - start) / 86400000;
  const decl = 23.44 * Math.sin((2 * Math.PI * (doy - 81)) / 365.25);
  const utcH = now.getUTCHours() + now.getUTCMinutes() / 60;
  const lon = (12 - utcH) * 15;
  return { lat: decl, lon };
}

function mountGlobe(host, exchanges) {
  host.innerHTML = "";
  const s3 = new Scene3D(host);
  s3.scene.clear();
  s3.scene.add(new THREE.AmbientLight(0xffffff, 1.0));
  s3.camera.position.set(0, 0.5, 2.3);
  s3.controls.target.set(0, 0, 0);
  s3.controls.minDistance = 1.5;
  s3.controls.maxDistance = 5;

  const globe = new THREE.Group();
  s3.scene.add(globe);
  const R = 1.0;

  // base sphere: deep blue-black ocean
  globe.add(new THREE.Mesh(
    new THREE.SphereGeometry(R * 0.992, 48, 32),
    new THREE.MeshBasicMaterial({ color: 0x0a0e15 })));

  // graticule, fainter than before — the land carries the shape now
  const gmat = new THREE.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.07 });
  for (let lat = -60; lat <= 60; lat += 30) {
    const pts = [];
    for (let lon = 0; lon <= 360; lon += 5) pts.push(latLonToVec(lat, lon, R));
    globe.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), gmat));
  }
  for (let lon = 0; lon < 360; lon += 30) {
    const pts = [];
    for (let lat = -90; lat <= 90; lat += 5) pts.push(latLonToVec(lat, lon, R));
    globe.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), gmat));
  }

  // dot-matrix continents
  const landPts = [];
  for (let lat = -80; lat <= 80; lat += 2.6) {
    // constant surface density: thin the longitudinal step near the poles
    const step = 2.6 / Math.max(Math.cos((lat * Math.PI) / 180), 0.3);
    for (let lon = -180; lon < 180; lon += step) {
      if (isLand(lat, lon)) landPts.push(latLonToVec(lat, lon, R * 1.001));
    }
  }
  const landGeo = new THREE.BufferGeometry().setFromPoints(landPts);
  globe.add(new THREE.Points(landGeo, new THREE.PointsMaterial({
    color: 0x51606f, size: 0.016, sizeAttenuation: true,
    transparent: true, opacity: 0.95, depthWrite: false })));

  // night hemisphere, fixed in the EARTH frame so it stays true to the land
  // while the decorative spin turns the whole assembly
  const night = new THREE.Mesh(
    new THREE.SphereGeometry(R * 1.006, 48, 32, 0, Math.PI),
    new THREE.MeshBasicMaterial({ color: 0x000000, transparent: true,
                                  opacity: 0.48, depthWrite: false, side: THREE.DoubleSide }));
  globe.add(night);
  // The half-shell's intrinsic outward axis, measured from its own
  // vertices instead of guessed from geometry conventions - a wrong guess
  // here shades the wrong continents and no one notices for a season.
  const shellAxis = (() => {
    const pos = night.geometry.getAttribute("position");
    const c = new THREE.Vector3();
    for (let i = 0; i < pos.count; i++) {
      c.x += pos.getX(i); c.y += pos.getY(i); c.z += pos.getZ(i);
    }
    return c.normalize();
  })();
  const aimNight = () => {
    const sun = subsolar();
    const anti = latLonToVec(-sun.lat, sun.lon + 180, 1).normalize();
    night.quaternion.setFromUnitVectors(shellAxis, anti);
  };
  aimNight();

  // atmosphere: a whisper of blue on the limb
  globe.add(new THREE.Mesh(
    new THREE.SphereGeometry(R * 1.045, 48, 32),
    new THREE.MeshBasicMaterial({ color: 0x3a6ea8, transparent: true,
                                  opacity: 0.05, side: THREE.BackSide, depthWrite: false })));

  const markers = new Map();
  for (const ex of exchanges) {
    const surface = latLonToVec(ex.lat, ex.lon, R * 1.004);
    const anchor = latLonToVec(ex.lat, ex.lon, R * 1.10);
    const dot = new THREE.Mesh(
      new THREE.SphereGeometry(0.02, 12, 12),
      new THREE.MeshBasicMaterial({ color: STATE_COLOR[ex.state] ?? STATE_COLOR.closed }));
    dot.position.copy(surface);
    const halo = new THREE.Mesh(
      new THREE.SphereGeometry(0.042, 12, 12),
      new THREE.MeshBasicMaterial({ color: STATE_COLOR[ex.state] ?? STATE_COLOR.closed,
                                    transparent: true, opacity: ex.open ? 0.30 : 0.12,
                                    depthWrite: false }));
    halo.position.copy(surface);
    const leader = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints([surface, anchor]),
      new THREE.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.22 }));
    const label = textSprite(`${ex.code} ${ex.local_time}`, {
      color: ex.open ? "#2ebd85" : (ex.state === "lunch" ? "#f0a826" : "#5d6470"),
      scale: 0.00054, depthTest: true,
    });
    label.position.copy(latLonToVec(ex.lat, ex.lon, R * 1.15));
    globe.add(dot, halo, leader, label);
    markers.set(ex.code, { dot, halo, label, open: ex.open });
  }

  let t = 0;
  s3.onTick(() => {
    globe.rotation.y += 0.0014;
    t += 0.05;
    const pulse = 1 + 0.18 * Math.sin(t);
    for (const m of markers.values()) {
      if (m.open) m.halo.scale.setScalar(pulse);
    }
  });

  return {
    dispose: () => s3.dispose(),
    update(rows) {
      aimNight();                       // the terminator drifts 15°/hour
      for (const ex of rows) {
        const m = markers.get(ex.code);
        if (!m) continue;
        const col = STATE_COLOR[ex.state] ?? STATE_COLOR.closed;
        m.dot.material.color.setHex(col);
        m.halo.material.color.setHex(col);
        m.halo.material.opacity = ex.open ? 0.30 : 0.12;
        m.open = ex.open;
        const fresh = textSprite(`${ex.code} ${ex.local_time}`, {
          color: ex.open ? "#2ebd85" : (ex.state === "lunch" ? "#f0a826" : "#5d6470"),
          scale: 0.00054, depthTest: true,
        });
        fresh.position.copy(m.label.position);
        globe.add(fresh);
        globe.remove(m.label);
        m.label.material.map.dispose(); m.label.material.dispose();
        m.label = fresh;
      }
    },
  };
}

window.Viz3D = { mountSurface, mountFan, mountSwarm, mountGlobe, mountScatter };
