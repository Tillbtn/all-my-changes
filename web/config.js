/* Configuration for the map: basemaps, highlight styles, defaults.
 * Everything here is meant to be edited - add your own basemaps and styles.
 */

/* color ramp used by the "Edit age" style: year of my last edit */
const AGE_COLOR = [
  "interpolate", ["linear"], ["coalesce", ["get", "y"], 2022],
  2022, "#3b82f6",
  2023, "#22c55e",
  2024, "#eab308",
  2025, "#f97316",
  2026, "#ef4444",
];

/* JS mirror of the AGE_COLOR ramp for the three.js roof renderer */
const AGE_STOPS = [
  [2022, [0x3b, 0x82, 0xf6]], [2023, [0x22, 0xc5, 0x5e]],
  [2024, [0xea, 0xb3, 0x08]], [2025, [0xf9, 0x73, 0x16]],
  [2026, [0xef, 0x44, 0x44]],
];
function ageColor(props) {
  const y = props.y || 2022;
  let lo = AGE_STOPS[0], hi = AGE_STOPS[AGE_STOPS.length - 1];
  for (let i = 0; i < AGE_STOPS.length - 1; i++) {
    if (y >= AGE_STOPS[i][0] && y <= AGE_STOPS[i + 1][0]) {
      lo = AGE_STOPS[i]; hi = AGE_STOPS[i + 1]; break;
    }
  }
  const t = hi[0] === lo[0] ? 0 : Math.min(Math.max((y - lo[0]) / (hi[0] - lo[0]), 0), 1);
  const c = lo[1].map((v, i) => Math.round(v + (hi[1][i] - v) * t));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}

window.APP_CONFIG = {

  defaults: {
    basemap: "dark",
    highlight: "redOutline",
  },

  /* 3D buildings: model roof shapes (Simple 3D Buildings) with three.js.
   * Set to false to fall back to flat-top fill-extrusion. */
  use3dRoofs: true,

  /* ------------------------------------------------------------ basemaps
   * type "vector": styleUrl is a full MapLibre style (no API key needed)
   * type "raster": classic xyz tiles
   * type "none":   plain colored globe
   */
  basemaps: {
    dark: {
      name: "Carto Dark",
      type: "raster",
      tiles: [
        "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png",
        "https://b.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png",
        "https://c.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png",
      ],
      attribution: "&copy; OpenStreetMap contributors &copy; CARTO",
    },
    positron: {
      name: "Carto Light",
      type: "raster",
      tiles: [
        "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png",
        "https://b.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png",
        "https://c.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png",
      ],
      attribution: "&copy; OpenStreetMap contributors &copy; CARTO",
    },
    liberty: {
      name: "OpenFreeMap Liberty (vector)",
      type: "vector",
      styleUrl: "https://tiles.openfreemap.org/styles/liberty",
      attribution: "&copy; OpenStreetMap contributors, OpenFreeMap",
    },
    osm: {
      name: "OpenStreetMap Standard",
      type: "raster",
      tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
      attribution: "&copy; OpenStreetMap contributors",
    },
    satellite: {
      name: "Esri World Imagery",
      type: "raster",
      tiles: [
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
      ],
      attribution: "Esri, Maxar, Earthstar Geographics",
    },
    plain: {
      name: "Plain globe",
      type: "none",
      background: "#0b1020",
    },
  },

  /* ------------------------------------------------------- highlight styles
   * Paint properties per layer kind. Optional keys:
   *   polygonFill, polygonLine, polygonLineHalo, line, lineHalo,
   *   point, vertex, deleted, extrusionColor
   * A missing halo key just means: no halo layer.
   */
  highlightStyles: {

    redOutline: {
      name: "Red outline",
      polygonFill: { "fill-color": "#ff3131", "fill-opacity": 0.07 },
      polygonLine: { "line-color": "#ff3131", "line-width": 2 },
      line: { "line-color": "#ff3131", "line-width": 2.4 },
      point: {
        "circle-radius": 4.5,
        "circle-color": "rgba(255,49,49,0.15)",
        "circle-stroke-color": "#ff3131",
        "circle-stroke-width": 1.8,
      },
      vertex: { "circle-radius": 1.8, "circle-color": "#ff3131", "circle-opacity": 0.6 },
      deleted: {
        "circle-radius": 4,
        "circle-color": "rgba(120,120,120,0.25)",
        "circle-stroke-color": "#9ca3af",
        "circle-stroke-width": 1.5,
      },
      extrusionColor: "#ff3131",
    },

    glow: {
      name: "Neon glow",
      polygonFill: { "fill-color": "#22d3ee", "fill-opacity": 0.10 },
      polygonLineHalo: {
        "line-color": "#22d3ee", "line-width": 7, "line-blur": 6, "line-opacity": 0.7,
      },
      polygonLine: { "line-color": "#e0feff", "line-width": 1.2 },
      lineHalo: {
        "line-color": "#22d3ee", "line-width": 8, "line-blur": 6, "line-opacity": 0.7,
      },
      line: { "line-color": "#e0feff", "line-width": 1.4 },
      point: {
        "circle-radius": 5,
        "circle-color": "rgba(34,211,238,0.35)",
        "circle-blur": 0.6,
        "circle-stroke-color": "#a5f3fc",
        "circle-stroke-width": 1.2,
      },
      vertex: { "circle-radius": 1.8, "circle-color": "#22d3ee", "circle-opacity": 0.65 },
      deleted: {
        "circle-radius": 4,
        "circle-color": "rgba(148,163,184,0.3)",
        "circle-stroke-color": "#94a3b8",
        "circle-stroke-width": 1,
      },
      extrusionColor: "#22d3ee",
    },

    fill: {
      name: "Solid fill",
      polygonFill: { "fill-color": "#f59e0b", "fill-opacity": 0.45 },
      polygonLine: { "line-color": "#b45309", "line-width": 1 },
      line: { "line-color": "#f59e0b", "line-width": 3, "line-opacity": 0.8 },
      point: { "circle-radius": 4.5, "circle-color": "#f59e0b", "circle-opacity": 0.8 },
      vertex: { "circle-radius": 1.8, "circle-color": "#f59e0b", "circle-opacity": 0.6 },
      deleted: {
        "circle-radius": 4,
        "circle-color": "rgba(120,120,120,0.3)",
        "circle-stroke-color": "#6b7280",
        "circle-stroke-width": 1.5,
      },
      extrusionColor: "#f59e0b",
    },

    age: {
      name: "Edit age (blue = old, red = recent)",
      polygonFill: { "fill-color": AGE_COLOR, "fill-opacity": 0.25 },
      polygonLine: { "line-color": AGE_COLOR, "line-width": 1.8 },
      line: { "line-color": AGE_COLOR, "line-width": 2.6 },
      point: {
        "circle-radius": 4.5,
        "circle-color": AGE_COLOR,
        "circle-opacity": 0.85,
      },
      vertex: { "circle-radius": 1.8, "circle-color": AGE_COLOR, "circle-opacity": 0.6 },
      deleted: {
        "circle-radius": 4,
        "circle-color": "rgba(120,120,120,0.3)",
        "circle-stroke-color": "#6b7280",
        "circle-stroke-width": 1.5,
      },
      extrusionColor: AGE_COLOR,
      /* the three.js renderer can't evaluate MapLibre expressions -
       * styles whose extrusionColor is an expression provide a JS twin */
      color3d: ageColor,
    },
  },
};
