/* All my OSM changes - MapLibre globe showing every object I ever edited. */
"use strict";

const CFG = window.APP_CONFIG;

/* sources: eager ones load on startup, lazy ones on first toggle */
const SOURCES = {
  polygons: { file: "data/polygons.geojson", eager: true },
  lines: { file: "data/lines.geojson", eager: true },
  relations: { file: "data/relations.geojson", eager: false },
  points: { file: "data/points.geojson", eager: true },
  vertices: { file: "data/vertices.geojson", eager: false },
  deleted: { file: "data/deleted.geojson", eager: false },
};

const state = loadState();
const loaded = {}; // source id -> GeoJSON (once fetched)
let meta = null;
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
  try {
    meta = await (await fetch("data/meta.json")).json();
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

async function fetchSource(id) {
  if (loaded[id]) return;
  try {
    loaded[id] = await (await fetch(SOURCES[id].file)).json();
  } catch {
    loaded[id] = { type: "FeatureCollection", features: [] };
  }
  const src = map.getSource("edits-" + id);
  if (src) src.setData(loaded[id]);
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
