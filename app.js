/* All my OSM changes - MapLibre globe showing every object I ever edited. */
"use strict";

const CFG = window.APP_CONFIG;

/* sources: eager ones load on startup, lazy ones on first toggle */
const SOURCES = {
  polygons: { eager: true },
  lines: { eager: true },
  relations: { eager: false },
  points: { eager: true },
  vertices: { eager: false },
  deleted: { eager: false },
};

const state = loadState();
const loaded = {}; // source id -> GeoJSON (once fetched)
const inflight = {}; // source id -> {got, total} while downloading
let meta = null;
/* per-user data directory; initUser() switches it to data/<user> when a
 * users.json index exists (single-user layouts keep the flat data/) */
let dataDir = "data";
/* captured before the map writes its own position into the hash */
const hadStartHash = !!location.hash;

function loadState() {
  const dflt = {
    basemap: CFG.defaults.basemap,
    highlight: CFG.defaults.highlight,
    roofShapes: true,
    show: { polygons: true, lines: true, relations: true, points: true, vertices: false, deleted: false, extrude: false },
  };
  try {
    const saved = JSON.parse(localStorage.getItem("amc-state") || "{}");
    if (!CFG.basemaps[saved.basemap]) delete saved.basemap;
    if (!CFG.highlightStyles[saved.highlight]) delete saved.highlight;
    return { ...dflt, ...saved, show: { ...dflt.show, ...(saved.show || {}) } };
  } catch {
    return dflt;
  }
}
function saveState() {
  localStorage.setItem("amc-state", JSON.stringify(state));
}

/* ------------------------------------------------------------ basemap style */

function rasterStyle(bm) {
  return {
    version: 8,
    projection: { type: "globe" },
    sky: skySpec(),
    sources: {
      base: {
        type: "raster",
        tiles: bm.tiles,
        tileSize: 256,
        attribution: bm.attribution || "",
      },
    },
    layers: [
      { id: "bg", type: "background", paint: { "background-color": "#0b1020" } },
      { id: "base", type: "raster", source: "base" },
    ],
  };
}

function plainStyle(bm) {
  return {
    version: 8,
    projection: { type: "globe" },
    sky: skySpec(),
    sources: {},
    layers: [
      { id: "bg", type: "background", paint: { "background-color": bm.background || "#0b1020" } },
    ],
  };
}

function skySpec() {
  return {
    "atmosphere-blend": ["interpolate", ["linear"], ["zoom"], 0, 1, 6, 1, 8, 0],
  };
}

async function styleFor(id) {
  const bm = CFG.basemaps[id];
  if (bm.type === "raster") return rasterStyle(bm);
  if (bm.type === "none") return plainStyle(bm);
  const style = await (await fetch(bm.styleUrl)).json();
  style.projection = { type: "globe" };
  style.sky = style.sky || skySpec();
  return style;
}

/* ---------------------------------------------------------------- the map */

const map = new maplibregl.Map({
  container: "map",
  style: { version: 8, projection: { type: "globe" }, sources: {}, layers: [
    { id: "bg", type: "background", paint: { "background-color": "#0b1020" } },
  ] },
  center: [9.7, 52.6],
  zoom: 3,
  hash: true,
  attributionControl: { compact: false },
});
window._map = map; // handy for console debugging
map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
if (maplibregl.GlobeControl) map.addControl(new maplibregl.GlobeControl(), "top-right");

map.on("styledata", ensureDataLayers);

init();

async function init() {
  await initUser();
  try {
    meta = await (await fetch(`${dataDir}/meta.json`)).json();
  } catch {
    document.getElementById("stats").textContent =
      "no data yet - run the pipeline first (see README)";
  }
  buildPanel();
  await applyBasemap(state.basemap);
  await Promise.all(
    Object.entries(SOURCES)
      .filter(([id, s]) => s.eager || state.show[id])
      .map(([id]) => fetchSource(id))
  );
  document.getElementById("loading").style.display = "none";
  /* 3D roofs need the polygon data that just arrived - rebuild if enabled */
  if (state.show.extrude && use3dRoofs() && !map.getLayer("amc-3d")) {
    refreshLayers();
  }
  updateStats();
  if (meta && meta.bbox && !hadStartHash) {
    map.fitBounds(
      [[meta.bbox[0], meta.bbox[1]], [meta.bbox[2], meta.bbox[3]]],
      { padding: 40, maxZoom: 10, duration: 2500 }
    );
  }
}

/* ------------------------------------------------------------ data loading
 *
 * The pipeline writes <name>.geojson.gz by default (meta.json says so) -
 * gzipped GeoJSON is roughly a quarter of the size, which is what keeps a
 * prolific mapper's dataset inside GitHub's file and Pages size limits. The
 * browser inflates it here.
 */

async function fetchSource(id) {
  if (loaded[id]) return;
  const gz = !meta || meta.gzip !== false;
  const t0 = performance.now();
  inflight[id] = { got: 0, total: 0 };
  try {
    loaded[id] = await fetchGeoJSON(`${dataDir}/${id}.geojson${gz ? ".gz" : ""}`, id);
  } catch (first) {
    try {
      /* a dataset rebuilt with the other AMC_GZIP setting than its meta.json
       * claims still loads instead of showing an empty layer */
      loaded[id] = await fetchGeoJSON(`${dataDir}/${id}.geojson${gz ? "" : ".gz"}`, id);
    } catch (second) {
      console.warn(`amc: ${id} not loaded`, first, second);
      loaded[id] = { type: "FeatureCollection", features: [] };
    }
  }
  if (!Array.isArray(loaded[id].features)) {
    loaded[id] = { type: "FeatureCollection", features: [] };
  }
  const bytes = inflight[id].got;
  delete inflight[id];
  renderLoading();
  console.log(
    `amc: ${id} - ${loaded[id].features.length} features, ` +
    `${fmtBytes(bytes)} in ${((performance.now() - t0) / 1000).toFixed(1)}s`
  );
  const src = map.getSource("edits-" + id);
  if (src) src.setData(loaded[id]);
}

async function fetchGeoJSON(url, id) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${url}`);
  const bytes = await readAll(res, id);
  /* Decide on the gzip magic number, not the file name: whether a host
   * serves .gz raw or with Content-Encoding (leaving the browser to inflate
   * it) differs between GitHub Pages and a local python http.server. */
  if (bytes[0] === 0x1f && bytes[1] === 0x8b) {
    if (typeof DecompressionStream === "undefined") {
      throw new Error("this browser cannot inflate gzip (no DecompressionStream)");
    }
    const stream = new Blob([bytes]).stream()
      .pipeThrough(new DecompressionStream("gzip"));
    return await new Response(stream).json();
  }
  return JSON.parse(new TextDecoder().decode(bytes));
}

/* read the body chunk by chunk so the overlay can show download progress */
async function readAll(res, id) {
  const total = Number(res.headers.get("content-length")) || 0;
  if (!res.body) return new Uint8Array(await res.arrayBuffer());
  const reader = res.body.getReader();
  const chunks = [];
  let got = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    got += value.length;
    if (inflight[id]) {
      inflight[id] = { got, total };
      renderLoading();
    }
  }
  const out = new Uint8Array(got);
  let at = 0;
  for (const c of chunks) {
    out.set(c, at);
    at += c.length;
  }
  return out;
}

function fmtBytes(n) {
  if (!n) return "0 B";
  const units = ["B", "kB", "MB", "GB"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
}

/* "loading areas 12.3/33.1 MB - ways 4.0 MB" for everything in flight */
function renderLoading() {
  const el = document.getElementById("loading");
  const parts = Object.entries(inflight).map(([id, p]) =>
    `${id} ${fmtBytes(p.got)}${p.total ? "/" + fmtBytes(p.total) : ""}`
  );
  if (!parts.length) return;
  el.textContent = `loading ${parts.join(" \u00b7 ")}\u2026`;
}

/* pick the active user from ?u= / localStorage / first entry, point
 * dataDir at their directory and wire up the switcher dropdown */
async function initUser() {
  let users = [];
  try {
    const r = await fetch("data/users.json");
    if (r.ok) users = await r.json();
  } catch { /* no index -> legacy flat data/ layout */ }
  if (!Array.isArray(users) || !users.length) return;

  const want =
    new URLSearchParams(location.search).get("u") ||
    localStorage.getItem("amc-user");
  const current = users.find((u) => u.dir === want) || users[0];
  dataDir = "data/" + encodeURIComponent(current.dir);
  localStorage.setItem("amc-user", current.dir);
  document.title = `${current.user} · OSM changes`;
  document.querySelector("#panel h1").textContent =
    `OSM changes · ${current.user}`;

  const sel = document.getElementById("user");
  for (const u of users) {
    const label = `${u.user} (${(u.objects || 0).toLocaleString()})`;
    sel.add(new Option(label, u.dir, false, u.dir === current.dir));
  }
  if (users.length > 1) {
    document.getElementById("user-row").style.display = "";
  }
  sel.onchange = () => {
    localStorage.setItem("amc-user", sel.value);
    const url = new URL(location);
    url.searchParams.set("u", sel.value);
    url.hash = ""; // let the map fly to the new user's bbox
    location.href = url;
  };
}

async function applyBasemap(id) {
  state.basemap = id;
  saveState();
  const style = await styleFor(id);
  map.setStyle(style);
}

/* re-create sources + highlight layers; runs after every style change and
 * is guarded so repeated styledata events are cheap */
function ensureDataLayers() {
  if (!map.getStyle()) return;
  if (map.getSource("edits-polygons")) return;

  for (const id of Object.keys(SOURCES)) {
    map.addSource("edits-" + id, {
      type: "geojson",
      data: loaded[id] || { type: "FeatureCollection", features: [] },
    });
  }
  addHighlightLayers();
}

function layerDefs() {
  const hs = CFG.highlightStyles[state.highlight];
  const defs = [];
  const add = (id, type, source, paint, extra = {}) => {
    if (!paint) return;
    defs.push({ id: "amc-" + id, type, source: "edits-" + source, paint, ...extra });
  };

  add("polygons-fill", "fill", "polygons", hs.polygonFill);
  add("polygons-line-halo", "line", "polygons", hs.polygonLineHalo);
  add("polygons-line", "line", "polygons", hs.polygonLine);
  /* relations render with the way/area paints of the active style:
   * polygonal members as fills, all members as (route) lines */
  add("relations-fill", "fill", "relations", hs.polygonFill, {
    filter: ["==", ["geometry-type"], "Polygon"],
  });
  add("relations-halo", "line", "relations", hs.lineHalo);
  add("relations-line", "line", "relations", hs.line);
  add("lines-halo", "line", "lines", hs.lineHalo);
  add("lines", "line", "lines", hs.line);
  add("deleted", "circle", "deleted", hs.deleted);
  add("vertices", "circle", "vertices", hs.vertex);
  add("points", "circle", "points", hs.point);
  if (state.show.extrude && !use3dRoofs()) {
    defs.push({
      id: "amc-extrude",
      type: "fill-extrusion",
      source: "edits-polygons",
      filter: ["all", ["==", ["get", "b"], 1], ["!=", ["get", "op"], 1]],
      paint: {
        "fill-extrusion-color": hs.extrusionColor || "#ff3131",
        "fill-extrusion-height": ["coalesce", ["get", "h"], 8],
        "fill-extrusion-base": ["coalesce", ["get", "mh"], 0],
        "fill-extrusion-opacity": 0.85,
      },
    });
  }
  return defs;
}

/* three.js roof rendering available and enabled (config + UI toggle)? */
function use3dRoofs() {
  return (
    CFG.use3dRoofs !== false &&
    state.roofShapes !== false &&
    window.THREE && window.Roofs3D
  );
}

const VISIBILITY_GROUP = {
  "amc-polygons-fill": "polygons", "amc-polygons-line-halo": "polygons",
  "amc-polygons-line": "polygons",
  "amc-relations-fill": "relations", "amc-relations-halo": "relations",
  "amc-relations-line": "relations",
  "amc-lines-halo": "lines", "amc-lines": "lines",
  "amc-points": "points", "amc-vertices": "vertices", "amc-deleted": "deleted",
  "amc-extrude": "extrude",
};

function addHighlightLayers() {
  for (const def of layerDefs()) {
    const group = VISIBILITY_GROUP[def.id];
    def.layout = {
      ...(def.layout || {}),
      visibility: state.show[group] ? "visible" : "none",
    };
    map.addLayer(def);
  }
  /* three.js roofs: custom layers have no visibility - added when on */
  if (state.show.extrude && use3dRoofs() && loaded.polygons) {
    const hs = CFG.highlightStyles[state.highlight];
    const color = hs.color3d ||
      (typeof hs.extrusionColor === "string" ? hs.extrusionColor : "#ff3131");
    map.addLayer(Roofs3D.createLayer(loaded.polygons.features, { color }));
  }
}

function removeHighlightLayers() {
  for (const layer of (map.getStyle().layers || [])) {
    if (layer.id.startsWith("amc-")) map.removeLayer(layer.id);
  }
  /* custom layers are not serialized into getStyle().layers */
  if (map.getLayer("amc-3d")) map.removeLayer("amc-3d");
}

function refreshLayers() {
  removeHighlightLayers();
  addHighlightLayers();
}

/* ------------------------------------------------------------------ panel */

function buildPanel() {
  const bmSel = document.getElementById("basemap");
  for (const [id, bm] of Object.entries(CFG.basemaps)) {
    bmSel.add(new Option(bm.name, id, false, id === state.basemap));
  }
  bmSel.onchange = () => applyBasemap(bmSel.value);

  const hlSel = document.getElementById("highlight");
  for (const [id, hs] of Object.entries(CFG.highlightStyles)) {
    hlSel.add(new Option(hs.name, id, false, id === state.highlight));
  }
  hlSel.onchange = () => {
    state.highlight = hlSel.value;
    saveState();
    refreshLayers();
  };

  const roofBox = document.getElementById("toggle-roofshapes");
  roofBox.checked = state.roofShapes !== false;
  roofBox.onchange = () => {
    state.roofShapes = roofBox.checked;
    saveState();
    if (state.show.extrude) refreshLayers();
  };

  for (const group of Object.keys(state.show)) {
    const box = document.getElementById("toggle-" + (group === "extrude" ? "extrude" : group));
    if (!box) continue;
    box.checked = state.show[group];
    box.onchange = async () => {
      state.show[group] = box.checked;
      saveState();
      if (box.checked && SOURCES[group] && !loaded[group]) {
        document.getElementById("loading").style.display = "block";
        await fetchSource(group);
        document.getElementById("loading").style.display = "none";
      }
      if (group === "extrude") {
        refreshLayers();
        if (box.checked && map.getPitch() < 20) {
          map.easeTo({ pitch: 55, duration: 800 });
        } else if (!box.checked && map.getPitch() > 0) {
          map.easeTo({ pitch: 0, duration: 800 });
        }
      } else {
        for (const [layerId, g] of Object.entries(VISIBILITY_GROUP)) {
          if (g === group && map.getLayer(layerId)) {
            map.setLayoutProperty(layerId, "visibility", box.checked ? "visible" : "none");
          }
        }
      }
      updateStats();
    };
  }
}

function updateStats() {
  if (!meta) return;
  const c = meta.counts || {};
  const parts = [
    `<b>${(meta.objects || 0).toLocaleString()}</b> objects from ` +
    `<b>${(meta.changesets || 0).toLocaleString()}</b> changesets`,
    `${(c.polygons || 0).toLocaleString()} areas &middot; ` +
    `${(c.lines || 0).toLocaleString()} ways &middot; ` +
    `${(c.points || 0).toLocaleString()} points &middot; ` +
    `${(c.relations || 0).toLocaleString()} relations`,
    `updated ${(meta.generated_at || "").slice(0, 10)}`,
  ];
  document.getElementById("stats").innerHTML = parts.join("<br>");
}

/* ------------------------------------------------------------------ popups */

const OTYPE = { n: "node", w: "way", r: "relation" };

map.on("click", (e) => {
  const layers = Object.keys(VISIBILITY_GROUP).filter(
    (id) => id !== "amc-extrude" && map.getLayer(id)
  );
  const px = 5; // click tolerance in pixels (outlines are thin)
  const feats = map.queryRenderedFeatures(
    [[e.point.x - px, e.point.y - px], [e.point.x + px, e.point.y + px]],
    { layers }
  );
  if (!feats.length) return;
  const seen = new Set();
  const rows = [];
  for (const f of feats) {
    const p = f.properties;
    const key = p.t + p.id;
    if (seen.has(key)) continue;
    seen.add(key);
    rows.push(popupRow(p));
    if (rows.length >= 4) break;
  }
  new maplibregl.Popup({ maxWidth: "340px" })
    .setLngLat(e.lngLat)
    .setHTML(rows.join("<hr>"))
    .addTo(map);
});

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function popupRow(p) {
  const otype = OTYPE[p.t] || p.t;
  const url = `https://www.openstreetmap.org/${otype}/${p.id}`;
  const title = p.n ? esc(p.n) : (p.k ? esc(p.k) : `${otype} ${p.id}`);
  const bits = [`<div class="pp-title">${title}${p.d ? " <s>(deleted)</s>" : ""}</div>`];
  if (p.n && p.k) bits.push(`<div class="pp-tag">${esc(p.k)}</div>`);
  bits.push(
    `<div class="pp-meta">edited <b>${p.c || 1}&times;</b>` +
    (p.f && p.l && p.f !== p.l ? ` between ${p.f} and ${p.l}` : ` on ${p.l || p.f}`) +
    `</div>`,
    `<div class="pp-link"><a href="${url}" target="_blank">${otype}/${p.id} on osm.org</a></div>`
  );
  return bits.join("");
}

for (const id of Object.keys(VISIBILITY_GROUP)) {
  map.on("mouseenter", id, () => (map.getCanvas().style.cursor = "pointer"));
  map.on("mouseleave", id, () => (map.getCanvas().style.cursor = ""));
}

/* console debugging handle */
window._amc = { state, loaded, refreshLayers, use3dRoofs };
